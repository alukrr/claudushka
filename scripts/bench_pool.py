"""Замер всех моделей пула router.apitoken.sale по всем полосам (ТЗ docs/claude/tz-no-anthropic.md, Шаг 0).

Запуск ВНУТРИ контейнера (ключ — из env контейнера, в выводе только длина). Вывод большой — сохранить в файл:
    ssh <SSH_HOST> 'docker exec -i claudushka python3 - ' < scripts/bench_pool.py | tee bench.txt
Флаги (после «-»):  --runs N (по умолчанию 3)   --images (платно: по картинке на провайдера, PNG в /tmp/bench_imgs
контейнера)   --only <подстрока>  (только модели с подстрокой в id)   --lanes messages,chat,responses,native
Ничего не пишет в БД. Стоимость текста — центы; с --images — ~5 картинок (~$0.02–0.2 каждая, цены Terra нет).
В конце — JSON всех измерений между маркерами BENCH_JSON_BEGIN/END.
"""
import os, sys, time, json, base64, statistics, argparse
import requests
import anthropic

ap = argparse.ArgumentParser()
ap.add_argument("--runs", type=int, default=3)
ap.add_argument("--images", action="store_true")
ap.add_argument("--only", default="")
ap.add_argument("--lanes", default="messages,chat,responses,native")
A = ap.parse_args()
LANES = set(A.lanes.split(","))

BASE = (os.environ.get("ANTHROPIC_BASE_URL") or "https://router.apitoken.sale").rstrip("/")
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BEARER = {"Authorization": f"Bearer {KEY}"}
GOOG = {"x-goog-api-key": KEY}

GPT = ["gpt-6-luna", "gpt-5.6-luna", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra"]
GEMINI = ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-2.5-flash", "gemini-3-flash-preview",
          "gemini-3.8-flash", "gemini-4-argon"]
CLAUDE = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]   # только проверка «живы ли»
EXTRA_IDS = ["gemini-3.6-flash"]     # GEMINI_AUDIO_MODEL_NAME — голос/видео; только наличие в каталоге

SYSTEM = ("Ты Клодушка — ироничная, остроумная участница дружеского чата. Отвечай по-русски, коротко, "
          "с лёгкой иронией, обращайся по имени.")
USER = "Алексей: Клодушка, объясни в трёх предложениях, зачем DevOps-инженеру Docker Compose без Dockerfile?"
# Служебный вызов как в боте (извлечение памяти): строгий JSON, малый лимит.
AUX_SYSTEM = "Ты извлекаешь факты. Ответь ТОЛЬКО JSON без пояснений и без markdown."
AUX_USER = ('Сообщение: «Меня зовут Пётр, я живу в Казани, работаю сварщиком, на выходных еду в Сочи».\n'
            'Верни {"facts": [строки долговременных фактов], "skip": [временное]}')
AUX_MAX = 300
RED_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000010000000100802000000909168360000001549444154789c63f8cfc0"
    "f09f0a1a8c2a1855306c0300d6e5ff01c2d56e6e0000000049454e44ae426082")).decode()
TOOL = {"name": "get_weather", "description": "Погода в городе", "input_schema": {
    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}

ac = anthropic.Anthropic(api_key=KEY, base_url=BASE, max_retries=0, timeout=120)
RESULTS = []


def short(s, n=160):
    s = (s or "").replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 2) if xs else None


def err_of(e):
    st = getattr(e, "status_code", None)
    body = getattr(e, "body", None) or getattr(e, "message", None) or str(e)
    return f"HTTP {st}: {short(str(body), 200)}" if st else f"{type(e).__name__}: {short(str(e), 200)}"


def sse_lines(r):
    r.encoding = "utf-8"
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            d = line[5:].strip()
            if d == "[DONE]":
                return
            try:
                yield json.loads(d)
            except ValueError:
                continue


# ---------- полосы: каждая возвращает dict(text, ttft, total, inp, out, reasoning, model, err) ----------
def lane_messages(model, system, user_content, max_tokens=2000, stream=True, extra=None):
    t0 = time.monotonic(); first = None
    kw = dict(model=model, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user_content}])
    kw.update(extra or {})
    try:
        if stream:
            with ac.messages.stream(**kw) as st:
                for ev in st:
                    if first is None and getattr(ev, "type", "") == "content_block_delta" \
                            and getattr(ev.delta, "type", "") == "text_delta":
                        first = time.monotonic() - t0
                msg = st.get_final_message()
        else:
            msg = ac.messages.create(**kw)
    except Exception as e:
        return {"err": err_of(e), "total": time.monotonic() - t0}
    text = "".join(getattr(b, "text", "") for b in msg.content if b.type == "text")
    tools = [b.name for b in msg.content if b.type == "tool_use"]
    return {"text": text, "ttft": first, "total": time.monotonic() - t0, "inp": msg.usage.input_tokens,
            "out": msg.usage.output_tokens, "model": msg.model, "stop": msg.stop_reason, "tools": tools,
            "blocks": [b.type for b in msg.content]}


def lane_chat(model, system, user_content, max_tokens=2000, stream=True, extra=None):
    t0 = time.monotonic(); first = None; text = ""; usage = None; mret = None; tools = []
    if isinstance(user_content, list):   # картинка: OpenAI-формат
        uc = [{"type": "text", "text": user_content[1]["text"]},
              {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{RED_PNG}"}}]
    else:
        uc = user_content
    p = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": uc}],
         "max_completion_tokens": max_tokens, "stream": stream}
    if stream:
        p["stream_options"] = {"include_usage": True}
    p.update(extra or {})
    try:
        with requests.post(f"{BASE}/v1/chat/completions", headers=BEARER, json=p, stream=stream,
                           timeout=(10, 120)) as r:
            if r.status_code != 200:
                return {"err": f"HTTP {r.status_code}: {short(r.text, 200)}", "total": time.monotonic() - t0}
            if stream:
                for ev in sse_lines(r):
                    mret = ev.get("model") or mret
                    usage = ev.get("usage") or usage
                    for ch in ev.get("choices") or []:
                        d = ch.get("delta") or {}
                        if d.get("content"):
                            first = first or (time.monotonic() - t0); text += d["content"]
                        for tc in d.get("tool_calls") or []:
                            if (tc.get("function") or {}).get("name"):
                                tools.append(tc["function"]["name"])
            else:
                j = r.json(); mret = j.get("model"); usage = j.get("usage")
                m = (j.get("choices") or [{}])[0].get("message") or {}
                text = m.get("content") or ""
                tools = [t["function"]["name"] for t in m.get("tool_calls") or []]
    except Exception as e:
        return {"err": err_of(e), "total": time.monotonic() - t0}
    u = usage or {}
    return {"text": text, "ttft": first, "total": time.monotonic() - t0, "inp": u.get("prompt_tokens"),
            "out": u.get("completion_tokens"),
            "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
            "model": mret, "tools": tools}


def lane_responses(model, system, user_content, max_tokens=2000, stream=True, extra=None):
    t0 = time.monotonic(); first = None; text = ""; usage = None; mret = None; tools = []
    if isinstance(user_content, list):
        inp = [{"role": "user", "content": [{"type": "input_text", "text": user_content[1]["text"]},
                                            {"type": "input_image",
                                             "image_url": f"data:image/png;base64,{RED_PNG}"}]}]
    else:
        inp = user_content
    p = {"model": model, "instructions": system, "input": inp, "max_output_tokens": max_tokens, "stream": stream}
    p.update(extra or {})
    try:
        with requests.post(f"{BASE}/v1/responses", headers=BEARER, json=p, stream=stream, timeout=(10, 120)) as r:
            if r.status_code != 200:
                return {"err": f"HTTP {r.status_code}: {short(r.text, 200)}", "total": time.monotonic() - t0}
            if stream:
                for ev in sse_lines(r):
                    t = ev.get("type", "")
                    if t == "response.output_text.delta":
                        first = first or (time.monotonic() - t0); text += ev.get("delta", "")
                    elif t in ("response.completed", "response.incomplete"):
                        resp = ev.get("response") or {}
                        usage = resp.get("usage"); mret = resp.get("model")
                        tools = [o.get("name") for o in resp.get("output") or [] if o.get("type") == "function_call"]
            else:
                j = r.json(); usage = j.get("usage"); mret = j.get("model")
                for o in j.get("output") or []:
                    if o.get("type") == "message":
                        text += "".join(c.get("text", "") for c in o.get("content") or [])
                    if o.get("type") == "function_call":
                        tools.append(o.get("name"))
    except Exception as e:
        return {"err": err_of(e), "total": time.monotonic() - t0}
    u = usage or {}
    return {"text": text, "ttft": first, "total": time.monotonic() - t0, "inp": u.get("input_tokens"),
            "out": u.get("output_tokens"), "reasoning": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "cached": (u.get("input_tokens_details") or {}).get("cached_tokens"), "model": mret, "tools": tools}


def lane_native(model, system, user_content, max_tokens=2000, stream=True, extra=None):
    t0 = time.monotonic(); first = None; text = ""; usage = None; mret = None; tools = []
    if isinstance(user_content, list):
        parts = [{"text": user_content[1]["text"]}, {"inline_data": {"mime_type": "image/png", "data": RED_PNG}}]
    else:
        parts = [{"text": user_content}]
    p = {"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": parts}],
         "generationConfig": {"maxOutputTokens": max_tokens}}
    p.update(extra or {})
    url = f"{BASE}/v1beta/models/{model}:" + ("streamGenerateContent?alt=sse" if stream else "generateContent")
    try:
        with requests.post(url, headers=GOOG, json=p, stream=stream, timeout=(10, 120)) as r:
            if r.status_code != 200:
                return {"err": f"HTTP {r.status_code}: {short(r.text, 200)}", "total": time.monotonic() - t0}
            evs = sse_lines(r) if stream else [r.json()]
            for ev in evs:
                usage = ev.get("usageMetadata") or usage; mret = ev.get("modelVersion") or mret
                for c in ev.get("candidates") or []:
                    for prt in (c.get("content") or {}).get("parts") or []:
                        if prt.get("text") and not prt.get("thought"):
                            first = first or (time.monotonic() - t0); text += prt["text"]
                        if prt.get("functionCall"):
                            tools.append(prt["functionCall"].get("name"))
    except Exception as e:
        return {"err": err_of(e), "total": time.monotonic() - t0}
    u = usage or {}
    return {"text": text, "ttft": first, "total": time.monotonic() - t0, "inp": u.get("promptTokenCount"),
            "out": u.get("candidatesTokenCount"), "reasoning": u.get("thoughtsTokenCount"), "model": mret,
            "tools": tools}


LANE_FN = {"messages": lane_messages, "chat": lane_chat, "responses": lane_responses, "native": lane_native}


def tools_extra(lane):
    if lane == "messages":
        return {"tools": [TOOL]}
    if lane == "chat":
        return {"tools": [{"type": "function", "function": {"name": TOOL["name"], "description": TOOL["description"],
                                                            "parameters": TOOL["input_schema"]}}]}
    if lane == "responses":
        return {"tools": [{"type": "function", "name": TOOL["name"], "description": TOOL["description"],
                           "parameters": TOOL["input_schema"]}]}
    return {"tools": [{"functionDeclarations": [{"name": TOOL["name"], "description": TOOL["description"],
                                                 "parameters": TOOL["input_schema"]}]}]}


def lanes_for(model):
    if model.startswith("gemini"):
        return [l for l in ("messages", "native", "chat") if l in LANES]
    return [l for l in ("messages", "chat", "responses") if l in LANES]


def bench(model, lane):
    fn = LANE_FN[lane]
    rec = {"model": model, "lane": lane}
    runs = []
    for i in range(A.runs):
        r = fn(model, SYSTEM, USER)
        if r.get("err"):
            rec["err"] = r["err"]; rec["err_time"] = round(r["total"], 2)
            break
        runs.append(r)
    if runs:
        rec.update(ttft=med([r["ttft"] for r in runs]), total=med([r["total"] for r in runs]),
                   totals=[round(r["total"], 2) for r in runs], out=runs[0].get("out"), inp=runs[0].get("inp"),
                   reasoning=runs[0].get("reasoning"), model_ret=runs[0].get("model"), sample=short(runs[0]["text"], 300))
        t = rec["ttft"]; tot = rec["total"]
        rec["streams"] = bool(t is not None and tot and (tot - t) > 0.3 and t / tot < 0.8)
        # Функции — по одному вызову
        aux = fn(model, AUX_SYSTEM, AUX_USER, max_tokens=AUX_MAX, stream=False)
        if aux.get("err"):
            rec["aux"] = aux["err"]
        else:
            txt = (aux.get("text") or "").strip().strip("`").removeprefix("json").strip()
            try:
                js = json.loads(txt); ok = isinstance(js, dict) and "facts" in js
            except ValueError:
                ok = False
            rec["aux"] = {"json_ok": ok, "time": round(aux["total"], 2), "out": aux.get("out"),
                          "reasoning": aux.get("reasoning"), "empty": not txt, "sample": short(aux.get("text"), 120)}
        img = fn(model, SYSTEM, [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                              "data": RED_PNG}},
                                 {"type": "text", "text": "Какого цвета картинка? Одним словом."}],
                 max_tokens=500, stream=False)
        rec["image_in"] = img.get("err") or short(img.get("text"), 40)
        tl = fn(model, SYSTEM, "Какая погода в Казани? Используй инструмент.", max_tokens=800, stream=False,
                extra=tools_extra(lane))
        rec["tools"] = tl.get("err") or (tl.get("tools") or "не вызвал")
        if lane == "messages":
            cc = lane_messages(model, [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                               "Скажи «ок».", max_tokens=500, stream=False)
            rec["cache_control"] = cc.get("err") or "200"
    RESULTS.append(rec)
    if rec.get("err"):
        print(f"  {model:24} {lane:9} ✗ {rec['err']}", flush=True)
    else:
        print(f"  {model:24} {lane:9} first {rec['ttft']}s total {rec['total']}s {rec['totals']} "
              f"stream={rec['streams']} out={rec['out']} reas={rec['reasoning']} ret={rec['model_ret']!r}\n"
              f"      aux={rec['aux']}\n      img={rec['image_in']!r} tools={rec['tools']!r} "
              f"cc={rec.get('cache_control', '-')}\n      «{rec['sample']}»", flush=True)


print(f"base={BASE} key_len={len(KEY)} runs={A.runs} images={A.images} lanes={sorted(LANES)}\n")
print("== каталог GET /v1/models")
try:
    r = requests.get(f"{BASE}/v1/models", headers=BEARER, timeout=20)
    ids = {m.get("id") for m in r.json().get("data", [])} if r.ok else set()
    bare = {i.split("/", 1)[-1] for i in ids}
    print(f"HTTP {r.status_code}, всего {len(ids)}; claude в каталоге: {sorted(i for i in ids if 'claude' in i)}")
    for m in GPT + GEMINI + EXTRA_IDS:
        print(f"  {m:24} {'есть' if m in bare else 'НЕТ'}")
except Exception as e:
    print("ошибка каталога:", e)

print("\n== Claude: жив ли (1 вызов, /v1/messages)")
for m in CLAUDE:
    r = lane_messages(m, SYSTEM, "Скажи «ок».", max_tokens=200, stream=False)
    print(f"  {m:24} {r.get('err') or 'ОТВЕЧАЕТ: ' + short(r.get('text'), 40)} ({r['total']:.1f}s)", flush=True)
    RESULTS.append({"model": m, "lane": "messages", "alive_check": r.get("err") or "ok"})

print("\n== Текст в характере, служебный JSON, картинка на входе, инструменты")
for m in GPT + GEMINI:
    if A.only and A.only not in m:
        continue
    for lane in lanes_for(m):
        bench(m, lane)

if A.images:
    os.makedirs("/tmp/bench_imgs", exist_ok=True)
    PROMPT = "A cozy red fox reading a book under a desk lamp, watercolor"
    print("\n== Генерация картинок (PNG → /tmp/bench_imgs)")
    for mid in ("gpt-image-2", "openai/gpt-image-2.5-flare", "openai/gpt-image-2.5-sunburst"):
        t0 = time.monotonic()
        try:
            r = requests.post(f"{BASE}/v1/images/generations", headers=BEARER, timeout=180,
                              json={"model": mid, "prompt": PROMPT, "n": 1, "size": "1024x1024"})
            dt = time.monotonic() - t0
            if r.ok:
                b = (r.json().get("data") or [{}])[0].get("b64_json") or ""
                fn = f"/tmp/bench_imgs/{mid.split('/')[-1]}.png"
                open(fn, "wb").write(base64.b64decode(b)) if b else None
                print(f"  {mid:32} 200 {dt:.1f}s → {fn if b else 'без b64'} usage={r.json().get('usage')}")
            else:
                print(f"  {mid:32} HTTP {r.status_code} {dt:.1f}s {short(r.text, 200)}")
            RESULTS.append({"image_model": mid, "status": r.status_code, "time": round(dt, 1)})
        except Exception as e:
            print(f"  {mid:32} {type(e).__name__}: {e}")
    for mid in ("gpt-6.1-terra",):
        t0 = time.monotonic()
        try:
            r = requests.post(f"{BASE}/v1/responses", headers=BEARER, timeout=180,
                              json={"model": mid, "input": PROMPT, "tools": [{"type": "image_generation"}]})
            dt = time.monotonic() - t0
            if r.ok:
                j = r.json()
                calls = [o for o in j.get("output") or [] if o.get("type") == "image_generation_call"]
                meta = {k: v for k, v in (calls[0] if calls else {}).items() if k != "result"}
                b = calls[0].get("result") if calls else ""
                fn = f"/tmp/bench_imgs/{mid}.png"
                if b:
                    open(fn, "wb").write(base64.b64decode(b))
                print(f"  {mid:32} 200 {dt:.1f}s → {fn if b else 'без картинки'} meta={meta} usage={j.get('usage')}")
            else:
                print(f"  {mid:32} HTTP {r.status_code} {dt:.1f}s {short(r.text, 200)}")
            RESULTS.append({"image_model": mid + " (responses)", "status": r.status_code, "time": round(dt, 1)})
        except Exception as e:
            print(f"  {mid:32} {type(e).__name__}: {e}")
    print("  забрать: docker cp claudushka:/tmp/bench_imgs ./bench_imgs  (на сервере), затем scp к себе")

print("\nBENCH_JSON_BEGIN")
print(json.dumps(RESULTS, ensure_ascii=False))
print("BENCH_JSON_END")

"""Проба gpt-6.1-terra на пуле: какие полосы принимают модель, скорость, русский в характере, картинки.

Запуск ВНУТРИ контейнера (ключ берётся из env контейнера, в выводе только длина):
    ssh <SSH_HOST> 'docker exec -i claudushka python3 - '          < scripts/probe_terra.py
    ssh <SSH_HOST> 'docker exec -i claudushka python3 - --images'  < scripts/probe_terra.py   # + генерация картинок (платно)
Ничего не пишет в БД и в файлы, только stdout. Стоимость текста — центы; с --images — до ~4 картинок.
"""
import os, sys, time, json, statistics
import requests
import anthropic

MODEL = os.environ.get("PROBE_MODEL", "gpt-6.1-terra")
BASE = (os.environ.get("ANTHROPIC_BASE_URL") or "https://router.apitoken.sale").rstrip("/")
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RUNS = 5
DO_IMAGES = "--images" in sys.argv
SKIP_BASE = "--skip-base" in sys.argv
H = {"Authorization": f"Bearer {KEY}"}

SYSTEM = ("Ты Клодушка — ироничная, остроумная участница дружеского чата. Отвечай по-русски, коротко, "
          "с лёгкой иронией, обращайся по имени.")
USER = "Алексей: Клодушка, объясни в трёх предложениях, зачем DevOps-инженеру Docker Compose без Dockerfile?"
BASELINES = ["claude-haiku-5-5", "gemini-3.1-flash-lite", "gemini-3.8-flash"]

print(f"base={BASE} key_len={len(KEY)} model={MODEL} images={DO_IMAGES}\n")


def short(s, n=220):
    s = (s or "").replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def stats(xs):
    xs = [x for x in xs if x is not None]
    return f"{' / '.join(f'{x:.1f}' for x in xs)} с (медиана {statistics.median(xs):.1f})" if xs else "—"


# 1. Каталог
print("== 1. GET /v1/models")
try:
    r = requests.get(f"{BASE}/v1/models", headers=H, timeout=20)
    ids = [m.get("id") for m in r.json().get("data", [])] if r.ok else []
    print(f"HTTP {r.status_code}; {MODEL} в каталоге: {MODEL in ids}")
    print("похожие:", [i for i in ids if "terra" in (i or "") or "gpt-6" in (i or "")])
except Exception as e:
    print("ошибка:", type(e).__name__, e)

# 2. Anthropic-полоса /v1/messages (так ходят Gemini-модели чата)
client = anthropic.Anthropic(api_key=KEY, base_url=BASE, max_retries=0, timeout=120)


def messages_probe(model, label):
    print(f"\n== {label}: /v1/messages, model={model}")
    tot, ttft = [], []
    for i in range(RUNS):
        t0 = time.monotonic(); first = None
        try:
            with client.messages.stream(model=model, max_tokens=2000, system=[{"type": "text", "text": SYSTEM}],
                                        messages=[{"role": "user", "content": USER}]) as st:
                for ev in st:
                    if first is None and getattr(ev, "type", "") == "content_block_delta" \
                            and getattr(ev.delta, "type", "") == "text_delta":
                        first = time.monotonic() - t0
                msg = st.get_final_message()
            dt = time.monotonic() - t0
            tot.append(dt); ttft.append(first)
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            if i == 0:
                print(f"  response.model={msg.model!r} stop={msg.stop_reason} usage=in {msg.usage.input_tokens} "
                      f"out {msg.usage.output_tokens}; блоки={[b.type for b in msg.content]}")
                print(f"  ответ: {short(text)}")
        except Exception as e:
            print(f"  прогон {i+1}: {type(e).__name__} HTTP {getattr(e, 'status_code', None)}: {short(str(e), 300)}")
            return False
    print(f"  первый токен: {stats(ttft)}\n  весь ответ:   {stats(tot)}")
    return True


ok_messages = messages_probe(MODEL, "2. Terra")
messages_probe(f"openai/{MODEL}", "2b. Terra с префиксом")
if ok_messages:
    for extra, kw in [("tools", {"tools": [{"name": "noop", "description": "x", "input_schema": {"type": "object"}}]}),
                      ("cache_control", {"system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]}),
                      ("thinking", {"thinking": {"type": "enabled", "budget_tokens": 1024}})]:
        try:
            args = {"model": MODEL, "max_tokens": 1500, "system": SYSTEM,
                    "messages": [{"role": "user", "content": "Скажи «ок»."}], **kw}
            client.messages.create(**args)
            print(f"  параметр {extra}: принят (200)")
        except Exception as e:
            print(f"  параметр {extra}: HTTP {getattr(e, 'status_code', None)} {short(str(e), 160)}")

# 3. OpenAI-полосы
def sse_probe(path, payload, label, text_of_event):
    print(f"\n== {label}: {path}")
    tot, ttft = [], []
    for i in range(RUNS):
        t0 = time.monotonic(); first = None; text = ""; last = None
        try:
            with requests.post(f"{BASE}{path}", headers=H, json={**payload, "stream": True}, stream=True,
                               timeout=(10, 120)) as r:
                r.encoding = "utf-8"   # text/event-stream без charset → requests берёт ISO-8859-1, кириллица ломается
                if r.status_code != 200:
                    print(f"  HTTP {r.status_code}: {short(r.text, 300)}"); return
                for line in r.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    last = ev
                    piece = text_of_event(ev)
                    if piece:
                        if first is None:
                            first = time.monotonic() - t0
                        text += piece
            tot.append(time.monotonic() - t0); ttft.append(first)
            if i == 0:
                print(f"  ответ: {short(text)}")
                u = (last or {}).get("usage") or ((last or {}).get("response") or {}).get("usage")
                if u: print(f"  usage (последнее событие): {u}")
        except Exception as e:
            print(f"  прогон {i+1}: {type(e).__name__}: {short(str(e), 300)}"); return
    print(f"  первый токен: {stats(ttft)}\n  весь ответ:   {stats(tot)}")


sse_probe("/v1/chat/completions",
          {"model": MODEL, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]},
          "3a. Terra",
          lambda ev: ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content"))
sse_probe("/v1/responses", {"model": MODEL, "instructions": SYSTEM, "input": USER}, "3b. Terra",
          lambda ev: ev.get("delta") if ev.get("type") == "response.output_text.delta" else None)
sse_probe("/v1/chat/completions",
          {"model": f"openai/{MODEL}", "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}]},
          "3a2. Terra с префиксом",
          lambda ev: ((ev.get("choices") or [{}])[0].get("delta") or {}).get("content"))
sse_probe("/v1/responses", {"model": MODEL, "instructions": SYSTEM, "input": USER, "reasoning": {"effort": "low"}},
          "3c. Terra, reasoning.effort=low",
          lambda ev: ev.get("delta") if ev.get("type") == "response.output_text.delta" else None)

# 4. Базовые линии — те же вопрос и system
for m in ([] if SKIP_BASE else BASELINES):
    messages_probe(m, "4. база")

# 5. Картинки (платно, только с --images)
if DO_IMAGES:
    PROMPT = "A cozy red fox reading a book under a desk lamp, watercolor"
    for mid in (MODEL, f"openai/{MODEL}"):
        print(f"\n== 5a. /v1/images/generations model={mid}")
        t0 = time.monotonic()
        try:
            r = requests.post(f"{BASE}/v1/images/generations", headers=H, timeout=180,
                              json={"model": mid, "prompt": PROMPT, "n": 1, "size": "1024x1024"})
            dt = time.monotonic() - t0
            if r.ok:
                d = (r.json().get("data") or [{}])[0]
                print(f"  200 за {dt:.1f} с; b64={len(d.get('b64_json') or '')} url={bool(d.get('url'))} "
                      f"usage={r.json().get('usage')}")
                break
            print(f"  HTTP {r.status_code} за {dt:.1f} с: {short(r.text, 300)}")
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
    print(f"\n== 5b. /v1/responses + tools=image_generation, model={MODEL}")
    t0 = time.monotonic()
    try:
        r = requests.post(f"{BASE}/v1/responses", headers=H, timeout=180,
                          json={"model": MODEL, "input": PROMPT, "tools": [{"type": "image_generation"}]})
        dt = time.monotonic() - t0
        if r.ok:
            outs = r.json().get("output") or []
            imgs = [o for o in outs if o.get("type") == "image_generation_call"]
            print(f"  200 за {dt:.1f} с; типы output={[o.get('type') for o in outs]}; "
                  f"картинка b64={len((imgs[0].get('result') or '') if imgs else '')}; usage={r.json().get('usage')}")
        else:
            print(f"  HTTP {r.status_code} за {dt:.1f} с: {short(r.text, 300)}")
    except Exception as e:
        print(f"  {type(e).__name__}: {e}")
print("\nготово")

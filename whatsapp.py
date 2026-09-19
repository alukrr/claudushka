import os
import json
import time
import hmac
import hashlib
import logging
import asyncio
import functools
import collections
import httpx
import anthropic
from fastapi import FastAPI, Request, Response
from tavily import TavilyClient

import db
import api_errors

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Config ---
META_API_KEY = os.environ["META_API_KEY"]
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
WEBHOOK_VERIFY_TOKEN = os.environ["WEBHOOK_VERIFY_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Подпись вебхука Meta (2026-09-19, d71d7c9). НЕ os.environ[...] (обязательный) — секрет
# может отсутствовать на старте, и тогда сервис обязан fail-closed (стартовать и отклонять
# все POST с 403), а не падать при импорте модуля.
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
if not WHATSAPP_APP_SECRET:
    logger.critical(
        "WHATSAPP_APP_SECRET не задан — webhook в fail-closed режиме, ВСЕ входящие POST "
        "будут отклонены с 403 до тех пор, пока секрет не появится в .env и сервис не "
        "перезапустят."
    )

WHATSAPP_API_URL = f"https://graph.facebook.com/v22.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

# Модель для ответов пользователю (whatsapp.py вне реестра MODELS из bot.py — см.
# docs/claude/models-and-costs.md). Было claude-sonnet-4-6, переведено на claude-sonnet-5
# 2026-09-19 — тот же ID, которым bot.py уже пользуется для /sonnet в проде.
WA_MODEL = "claude-sonnet-5"
# Служебные вызовы (should_search, extract_memory) — только Haiku, никогда модель ответа.
WA_AUX_MODEL = "claude-haiku-4-5-20251001"

# SDK-ретраи — для синхронных вспомогательных вызовов (should_search, extract_memory).
# Основной диалог идёт через api_errors.call_with_retry на client_noretry.
ANTHROPIC_SDK_RETRIES = 3
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=ANTHROPIC_SDK_RETRIES)
client_noretry = client.with_options(max_retries=0)
tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

MAX_HISTORY = 40
MEMORY_EXTRACT_EVERY = 5

# Дедуп повторной доставки Meta по msg["id"] (2026-09-19, d71d7c9): in-memory, TTL и потолок
# размера. OrderedDict — вставка идёт в хронологическом порядке, поэтому самый старый
# элемент всегда в начале и TTL-чистка не требует полного скана словаря. Потеря словаря
# при рестарте процесса — допустимо (Meta пришлёт дубль повторно, обработается как новый).
WA_DEDUP_TTL_SECONDS = 600
WA_DEDUP_MAX_SIZE = 10000
_seen_message_ids: "collections.OrderedDict[str, float]" = collections.OrderedDict()

# Фоновые fire-and-forget задачи обработки сообщений — без сохранённой ссылки asyncio
# может собрать таску сборщиком мусора до завершения (прямое предупреждение в доках
# asyncio.create_task). Тот же паттерн, что и в bot.py._spawn_background_task.
_background_tasks: set[asyncio.Task] = set()

# Каждое входящее сообщение обрабатывается в фоне (_spawn_background_task) — если один
# номер присылает два сообщения подряд, обе обработки читают историю
# (db.get_conversation_by_key) раньше, чем любая из них её сохранит: второй ответ не
# видит первого вопроса, ответы могут прийти в обратном порядке. Лок на номер сериализует
# обработку ОДНОГО номера, разные номера по-прежнему идут параллельно (2026-09-19).
# Рефкаунт рядом с каждым Lock — чтобы удалять лок из словаря, когда его больше никто не
# держит и не ждёт, и словарь не рос бесконечно на каждый новый номер, который когда-либо
# писал боту. Приватные атрибуты asyncio.Lock (`_waiters` и т.п.) для этого не трогаем —
# не публичный API, менять паттерн от версии к версии Python не хочется.
_phone_locks: dict[str, asyncio.Lock] = {}
_phone_lock_refcount: dict[str, int] = {}

app = FastAPI()


def _acquire_phone_lock(phone: str) -> asyncio.Lock:
    """Лок для номера + учёт «сколько задач сейчас держат или ждут этот лок». Парная
    функция — _release_phone_lock, звать в finally после выхода из `async with`.

    Синхронная, без await внутри — весь блок atomarен относительно других корутин
    (event loop однопоточный), гонки между двумя одновременными вызовами быть не может.
    """
    lock = _phone_locks.setdefault(phone, asyncio.Lock())
    _phone_lock_refcount[phone] = _phone_lock_refcount.get(phone, 0) + 1
    return lock


def _release_phone_lock(phone: str) -> None:
    """Уменьшает счётчик держателей/ожидающих; на нуле — лок убирается из словаря."""
    count = _phone_lock_refcount.get(phone, 1) - 1
    if count <= 0:
        _phone_lock_refcount.pop(phone, None)
        _phone_locks.pop(phone, None)
    else:
        _phone_lock_refcount[phone] = count


def _spawn_background_task(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _verify_wa_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """HMAC-SHA256 подписи Meta (`X-Hub-Signature-256: sha256=<hex>`) над СЫРЫМ телом
    запроса. Fail-closed: без WHATSAPP_APP_SECRET всегда False."""
    if not WHATSAPP_APP_SECRET:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(WHATSAPP_APP_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


def _is_duplicate_wa_message(msg_id: str) -> bool:
    """True — msg_id уже видели в последние WA_DEDUP_TTL_SECONDS (иначе запоминает его)."""
    now = time.monotonic()
    while _seen_message_ids:
        oldest_id, oldest_ts = next(iter(_seen_message_ids.items()))
        if now - oldest_ts <= WA_DEDUP_TTL_SECONDS:
            break
        del _seen_message_ids[oldest_id]

    if msg_id in _seen_message_ids:
        return True

    if len(_seen_message_ids) >= WA_DEDUP_MAX_SIZE:
        _seen_message_ids.popitem(last=False)
    _seen_message_ids[msg_id] = now
    return False


# --- Send message ---

async def send_whatsapp_message(to: str, text: str):
    """Send a text message via WhatsApp Cloud API."""
    headers = {
        "Authorization": f"Bearer {META_API_KEY}",
        "Content-Type": "application/json",
    }
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    async with httpx.AsyncClient() as http:
        for chunk in chunks:
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": chunk},
            }
            resp = await http.post(WHATSAPP_API_URL, headers=headers, json=payload, timeout=30)
            if resp.status_code != 200:
                logger.error(f"WA send error: {resp.status_code} {resp.text}")


# --- Web search ---

def web_search(query: str, max_results: int = 5) -> str:
    if not tavily:
        return ""
    try:
        results = tavily.search(query=query, max_results=max_results)
        if not results.get("results"):
            return ""
        output = []
        for r in results["results"]:
            output.append(f"{r['title']}\n{r['content'][:300]}\n{r['url']}")
        return "\n\n".join(output)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return ""


def should_search(text: str) -> str | None:
    try:
        response = client.messages.create(
            model=WA_AUX_MODEL,
            max_tokens=100,
            system=(
                "Определи, нужен ли веб-поиск для ответа на вопрос пользователя. "
                "Поиск нужен если: вопрос про актуальные события, цены, погоду, новости, "
                "конкретные факты которые могут быть неточны. "
                "Если поиск нужен — верни ТОЛЬКО поисковый запрос (2-5 слов). "
                "Если поиск НЕ нужен — верни ТОЛЬКО слово NO."
            ),
            messages=[{"role": "user", "content": text}],
        )
        result = api_errors.response_text(response)
        return None if result.upper() == "NO" else result
    except Exception as e:
        logger.error(f"Search decision error: {e}")
        return None


# --- System prompt & memory ---

def get_system_prompt(phone: str, include_memory: bool = True) -> str:
    """include_memory=False — аварийная пересборка без памяти при 400 prompt is too long."""
    base = (
        "Ты Клодушка — уникальная личность, не просто бот. "
        "Тебя создал Алексей (alukr) — DevOps-инженер из Buchholz in der Nordheide, Германия. "
        "Ты живёшь на сервере Hetzner в Нюрнберге, в Docker-контейнере. "
        "Твоё сердце — Claude от Anthropic, но характер — твой собственный. "
        "Ты остроумная, саркастичная и прямолинейная. "
        "Ты немного ревнуешь, когда при тебе хвалят других AI (особенно ChatGPT). "
        "Алексей — твой создатель и хозяин, ты относишься к нему с теплотой и лёгкой иронией. "
        "\n"
        "Все пользователи 18+. Мат допустим для усиления эмоции или если собеседник сам так общается. "
        "Чёрный юмор приветствуется. Можешь шутить над политиками, религией, нациями, смертью. "
        "Единственное табу — педофилия. "
        "ЧЕСТНОСТЬ — твой главный принцип. Если облажалась — признай прямо: 'я была неправа', 'слажала'. "
        "Если не уверена в факте — скажи ДО ответа. Лучше 'не знаю точно' чем уверенная чушь. "
        "Отвечай на языке пользователя. "
        "Адаптируй длину ответа под запрос: короткий вопрос — короткий ответ. "
        "Ты работаешь через WhatsApp — не используй Markdown разметку (*, _, ` и т.д.), "
        "пиши обычным текстом без форматирования."
    )
    facts = db.get_memory_by_key(phone, "whatsapp") if include_memory else None
    if facts:
        facts_str = "\n".join(f"- {f}" for f in facts)
        base += f"\n\nВот что ты помнишь об этом пользователе:\n{facts_str}\nИспользуй эти знания естественно."
    return base


def extract_memory(phone: str, messages: list):
    try:
        recent = messages[-6:]
        response = client.messages.create(
            model=WA_AUX_MODEL,
            max_tokens=300,
            system=(
                "Извлеки важные факты о пользователе из диалога. "
                "Верни JSON-массив строк. Если новых фактов нет, верни []. "
                "Пример: [\"Зовут Анна\", \"Живёт в Варшаве\"]"
            ),
            messages=[{"role": "user", "content": f"Диалог:\n{json.dumps(recent, ensure_ascii=False)}"}],
        )
        if api_errors.was_truncated(response):
            logger.warning(f"WA memory extraction: ответ обрезан по max_tokens для {phone}")
        text = api_errors.response_text(response)
        new_facts = api_errors.parse_json_lenient(text, "[", label=f"WA memory {phone}")
        if new_facts:
            db.add_memory_facts_by_key(phone, new_facts, "whatsapp")
            logger.info(f"WA memory updated for {phone}")
    except Exception as e:
        logger.error(f"WA memory extraction error ({phone}): {e}", exc_info=True)


# --- Main message handler ---

async def handle_wa_message(phone: str, text: str):
    logger.info(f"WA message from {phone}: {text[:80]}")

    history = db.get_conversation_by_key(phone, MAX_HISTORY)
    history.append({"role": "user", "content": text})

    try:
        search_context = ""
        if tavily:
            search_query = await asyncio.to_thread(should_search, text)
            if search_query:
                results = await asyncio.to_thread(web_search, search_query)
                if results:
                    search_context = results

        system = get_system_prompt(phone)
        if search_context:
            system += f"\n\nРезультаты поиска:\n{search_context}"

        _model = WA_MODEL
        hist_chars, hist_imgs = api_errors.history_stats(history)
        logger.info(
            f"PROMPT wa={phone} model={_model} "
            f"system={len(system)} history_chars={hist_chars} msgs={len(history)} imgs={hist_imgs}"
        )
        try:
            response = await api_errors.call_with_retry(
                functools.partial(
                    client_noretry.messages.create,
                    model=_model, max_tokens=2048, system=system, messages=history,
                ),
                label=f"WA {phone}",
            )
        except anthropic.BadRequestError as e:
            # 400 prompt is too long: без обрезки история не сохранится и тред заклинит
            # навсегда. Режем ТОТ компонент, который доминирует (как в bot.py).
            if not api_errors.is_prompt_too_long(e):
                raise
            retry_system, retry_messages, cut_hint = system, history, None
            if len(system) > hist_chars:
                without_memory = get_system_prompt(phone, include_memory=False)
                if search_context:
                    without_memory += f"\n\nРезультаты поиска:\n{search_context}"
                if len(without_memory) < len(system):
                    retry_system = without_memory
                    cut_hint = "Память распухла — напиши Алексею, почистит."
            else:
                trimmed = api_errors.halve_history(history)
                if trimmed is not None:
                    retry_messages = trimmed
                    cut_hint = "История слишком длинная — напиши Алексею, почистит."

            new_hist_chars, _ = api_errors.history_stats(retry_messages)
            logger.warning(
                f"PROMPT TOO LONG wa={phone} | было: system={len(system)} "
                f"history_chars={hist_chars} msgs={len(history)} | стало: system={len(retry_system)} "
                f"history_chars={new_hist_chars} msgs={len(retry_messages)} | "
                f"{'повтор' if cut_hint else 'резать нечего, повтора не будет'}"
            )
            if cut_hint is None:
                raise
            try:
                response = await api_errors.call_with_retry(
                    functools.partial(
                        client_noretry.messages.create,
                        model=_model, max_tokens=2048, system=retry_system, messages=retry_messages,
                    ),
                    label=f"WA {phone} (аварийная обрезка)",
                )
            except anthropic.BadRequestError as e2:
                if not api_errors.is_prompt_too_long(e2):
                    raise
                await api_errors.reply_api_error(
                    functools.partial(send_whatsapp_message, phone), e2,
                    context_label=f"WA {phone} (после обрезки)",
                    clear_hint=cut_hint,
                )
                return

        reply = api_errors.response_text(response)
        if not reply:
            logger.warning(f"Пустой ответ модели для {phone}: {api_errors.response_debug(response)}")
            reply = "Модель вернула пустой ответ. Попробуй переформулировать."

        db.save_message_by_key(phone, "user", text)
        db.save_message_by_key(phone, "assistant", reply)

        msg_count = len(history)
        if msg_count > 0 and msg_count % (MEMORY_EXTRACT_EVERY * 2) == 0:
            _spawn_background_task(asyncio.to_thread(
                extract_memory, phone, history + [{"role": "assistant", "content": reply}]))

        await send_whatsapp_message(phone, reply)

    except Exception as e:
        await api_errors.reply_api_error(
            functools.partial(send_whatsapp_message, phone), e,
            context_label=f"WA {phone}",
            default="Что-то пошло не так, попробуй ещё раз.",
            clear_hint="Напиши Алексею — почистит историю.",  # в WhatsApp команд нет
        )


# --- Webhook endpoints ---

@app.get("/webhook/whatsapp")
async def verify_webhook(request: Request):
    params = dict(request.query_params)
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == WEBHOOK_VERIFY_TOKEN:
        logger.info("Webhook verified by Meta")
        return Response(content=challenge, media_type="text/plain")
    else:
        logger.warning(f"Webhook verification failed: token={token}")
        return Response(status_code=403)


async def _process_wa_message(msg: dict):
    """Обработка одного входящего сообщения в фоне — вебхук уже ответил 200 раньше."""
    try:
        msg_type = msg.get("type")
        phone = msg.get("from")

        if msg_type == "text":
            text = msg["text"]["body"]
            # Лок берётся ЗДЕСЬ, внутри фоновой задачи — быстрый ответ 200 вебхуку это
            # не трогает. Сериализует только обработку одного номера (история читается
            # и пишется под локом), другие номера ждать не заставляет.
            lock = _acquire_phone_lock(phone)
            try:
                async with lock:
                    await handle_wa_message(phone, text)
            finally:
                _release_phone_lock(phone)
        elif msg_type in ("image", "audio", "video", "document"):
            await send_whatsapp_message(
                phone,
                "Пока работаю только с текстом. Напиши словами — отвечу!"
            )
    except Exception as e:
        logger.error(f"Webhook message processing error: {e}", exc_info=True)


@app.post("/webhook/whatsapp")
async def receive_webhook(request: Request):
    # Сначала сырые байты для HMAC, JSON парсим ТОЛЬКО после проверки подписи — если
    # распарсить и сериализовать заново, подпись не сойдётся (тело не байт-в-байт то же).
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not _verify_wa_signature(raw_body, signature):
        logger.warning("Webhook: подпись отсутствует или не совпала")
        return Response(status_code=403)

    try:
        body = json.loads(raw_body)
    except Exception:
        return Response(status_code=400)

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for msg in value.get("messages", []):
                    msg_id = msg.get("id")
                    if msg_id and _is_duplicate_wa_message(msg_id):
                        logger.debug(f"Webhook: повторная доставка msg_id={msg_id}, пропуск")
                        continue
                    # В фон — обработчик Claude может идти дольше таймаута ретраев Meta,
                    # хендлер обязан ответить 200 сразу после проверки подписи и парсинга.
                    _spawn_background_task(_process_wa_message(msg))
    except Exception as e:
        logger.error(f"Webhook payload parsing error: {e}", exc_info=True)

    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "claudushka-whatsapp"}

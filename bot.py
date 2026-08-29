import os
import re
import json
import logging
import time
import asyncio
import functools
from pathlib import Path
from telegram import Update
from datetime import datetime, time as dt_time, timezone, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
import anthropic
from tavily import TavilyClient
import requests as http_requests
import db
import api_errors

logging.basicConfig(level=logging.INFO)
# httpx на INFO печатает полный URL каждого запроса, а в URL Telegram API лежит токен
# бота — 89% строк лога были такими. Не снимать: это и утечка токена в docker logs,
# и шум, в котором тонут настоящие ошибки.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
HF_API_TOKEN = os.environ.get("HF_API_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
ADMIN_IDS = {592441}

DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(exist_ok=True)

tavily = TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY else None

# ANTHROPIC_SDK_RETRIES работает для СИНХРОННЫХ вспомогательных вызовов (should_search,
# перевод промпта, капча, extract_memory, дневной обзор): SDK сам ретраит 408/409/429/5xx
# с бэкоффом 0.5→8с. Пользовательские вызовы идут через call_claude() на client_noretry —
# там ретраи наши, с «печатает…» между попытками (см. api_errors.call_with_retry).
ANTHROPIC_SDK_RETRIES = 3
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=ANTHROPIC_SDK_RETRIES)
client_noretry = client.with_options(max_retries=0)  # переиспользует тот же httpx-пул

MAX_HISTORY = 40
GROUP_TRANSCRIPT_LIMIT = 50   # сколько реплик группового транскрипта тащить в messages
MEMORY_EXTRACT_EVERY = 5
MEMORY_EXTRACT_EVERY_CHAT = 15  # чат-уровневый каденс извлечения памяти; считает и реплики бота
MAX_CAPTCHA_ATTEMPTS = 3
BAN_DURATION = 3600
STREET_DAILY_LIMIT = 10
CHAT_ACTIVITY_CHANCE = 0.03
GEMINI_REFUSAL_MARKER = "__REFUSAL__"

CAPTCHA_ENABLED = False
WHITELIST_ENABLED = False

# Captcha state (in-memory, resets on restart)
captcha_state: dict[str, dict] = {}

# Единственный источник правды по моделям: команды, цены, гейтинг, /models — отсюда.
# Пул api.apitoken.sale принимает эти строки (проверено живым запросом 29.07.2026).
# Цены — официальный прайс Anthropic в $/MTok (in/out), прокси даёт скидку сверху.
# Sonnet 5: до 31.08.2026 действует вводная цена $2/$10 — ставим постоянные $3/$15,
# после окончания акции значение станет верным само, до тех пор /cost слегка завышает.
# Окна контекста: у Haiku 200k, у пятого поколения 1M. Лимиты памяти и истории
# калиброваны под МИНИМАЛЬНОЕ (200k) — не поднимать их, ссылаясь на 1M у Opus.
MODELS = {
    "haiku":  {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5",
               "in": 1.0,  "out": 5.0,  "context":   200_000, "admin_only": False},
    "sonnet": {"id": "claude-sonnet-5",           "label": "Sonnet 5",
               "in": 3.0,  "out": 15.0, "context": 1_000_000, "admin_only": False},
    "opus":   {"id": "claude-opus-5",             "label": "Opus 5",
               "in": 5.0,  "out": 25.0, "context": 1_000_000, "admin_only": True},
    "fable":  {"id": "claude-fable-5",            "label": "Fable 5",
               "in": 10.0, "out": 50.0, "context": 1_000_000, "admin_only": True},
}
DEFAULT_MODEL_KEY = "haiku"
DEFAULT_MODEL_ID = MODELS[DEFAULT_MODEL_KEY]["id"]

_MODELS_BY_ID = {m["id"]: m for m in MODELS.values()}


def model_meta(model_id: str) -> dict:
    """Метаданные по API-строке. Неизвестная модель → дефолт, без исключения.

    В chat_models могут лежать строки прошлых поколений (мигрируются в init_db),
    поэтому падать здесь нельзя: чат просто поедет на дефолтных метаданных.
    """
    return _MODELS_BY_ID.get(model_id, MODELS[DEFAULT_MODEL_KEY])


# Per-model token tracking
token_usage: dict[str, dict[str, int]] = {}


def _track_tokens(model: str, inp: int, out: int):
    if model not in token_usage:
        token_usage[model] = {"input": 0, "output": 0}
    token_usage[model]["input"] += inp
    token_usage[model]["output"] += out


def _track_response(model: str, response) -> None:
    """Учёт токенов по ответу API.

    Зовётся из обёрток (sync_create / call_claude), а НЕ по месту: раньше половина
    путей — should_search, перевод, капча, extract_*, /search — не считалась вовсе,
    и /cost занижал расход. Новый вызов API автоматически попадает в учёт.
    """
    usage = getattr(response, "usage", None)
    if usage is not None:
        _track_tokens(model, usage.input_tokens, usage.output_tokens)


# --- Вызов Anthropic API из async-хендлеров ---

async def _keep_chat_action(bot, chat_id: int, action: str, stop_event: asyncio.Event) -> None:
    """Держит индикатор («печатает…», «отправляет фото…») живым, пока не выставлен stop_event.

    Telegram гасит индикатор примерно через 5 секунд, поэтому обновляем каждые 4.
    """
    try:
        while not stop_event.is_set():
            try:
                await bot.send_chat_action(chat_id=chat_id, action=action)
            except Exception as e:
                logger.debug(f"chat_action refresh failed: {e}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        pass


async def call_claude(context, chat_id: int | None, *, label: str, **kwargs):
    """messages.create из хендлера: не блокирует event loop, переживает 529/429/сеть.

    Всю паузу между попытками держит «печатает…», чтобы ожидание (до 15с) не выглядело
    зависанием. Исключение после всех попыток пробрасывается наверх — его ловит
    reply_api_error.
    """
    keepalive = None
    if context is not None and chat_id is not None:
        keepalive = functools.partial(_keep_chat_action, context.bot, chat_id, "typing")

    response = await api_errors.call_with_retry(
        functools.partial(client_noretry.messages.create, **kwargs),
        label=label,
        keepalive=keepalive,
    )
    _track_response(kwargs.get("model", ""), response)
    return response


async def call_claude_aux(fn, *args, label: str = "aux"):
    """Вспомогательная СИНХРОННАЯ функция (should_search, капча, web_search) — в поток.

    Ретраи здесь SDK-шные (max_retries на клиенте): они дешевле и им не нужен индикатор,
    но выполняться должны вне event loop, иначе лестница бэкоффа (0.5+1+2+4 ≈ 8с) вешает
    бота ещё ДО основного запроса.
    """
    return await asyncio.to_thread(fn, *args)


def sync_create(**kwargs):
    """Синхронный messages.create с учётом токенов. Звать ТОЛЬКО из потока.

    Единственная точка входа для служебных вызовов внутри sync-функций
    (should_search, капча, extract_*): считает токены и берёт SDK-ретраи.
    """
    response = client.messages.create(**kwargs)
    _track_response(kwargs.get("model", ""), response)
    return response


async def aux_create(**kwargs):
    """Служебный messages.create из async-кода — тот же sync_create, но в потоке.

    Именно `await aux_create(...)` вместо `client.messages.create(...)` во всех
    служебных путях: перевод промпта, приветствие, дневной обзор, фильтр фактов.
    """
    return await asyncio.to_thread(functools.partial(sync_create, **kwargs))


response_text = api_errors.response_text
_history_stats = api_errors.history_stats
_halve_history = api_errors.halve_history


# Bot info (set on startup)
context_bot_id = None
bot_username = None
_admin_bot = None  # ставится в post_init, через него notify_admins шлёт сообщения


# --- Role checks ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def is_chat_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Админ бота — всегда да. Иначе — реальный статус в этом Telegram-чате
    (administrator/creator), чтобы /ratelimit могли выставлять и админы группы,
    а не только глобальные админы бота."""
    if is_admin(user_id):
        return True
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def can_invite(user: dict) -> bool:
    return user["role"] in ("admin", "premium")


def can_search(user: dict) -> bool:
    return user["role"] in ("admin", "premium", "referral")


def needs_captcha(user: dict) -> bool:
    if user["role"] in ("admin", "premium", "banned"):
        return False
    return not user["verified"]


def is_allowed_in_chat(user: dict, chat_id: int) -> bool:
    if not WHITELIST_ENABLED:
        return True
    if user["role"] in ("admin", "premium", "referral"):
        return True
    if user["verified"]:
        return True
    if db.is_chat_allowed(chat_id):
        return True
    return False


def check_daily_limit(user: dict) -> bool:
    """Returns True if user can send a message."""
    if user["role"] in ("admin", "premium", "referral"):
        return True
    count = db.increment_daily_messages(user["telegram_id"])
    return count <= STREET_DAILY_LIMIT


# --- Web search ---

def web_search(query: str, max_results: int = 5) -> str:
    if not tavily:
        return ""
    try:
        results = tavily.search(query=query, max_results=max_results)
        if not results.get("results"):
            return "Поиск не дал результатов."
        output = []
        for r in results["results"]:
            output.append(f"**{r['title']}**\n{r['content'][:300]}\n{r['url']}")
        return "\n\n".join(output)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return f"Ошибка поиска: {e}"


def should_search(text: str) -> str | None:
    try:
        response = sync_create(
            model=DEFAULT_MODEL_ID,
            max_tokens=100,
            system=(
                "Определи, нужен ли веб-поиск для ответа на вопрос пользователя. "
                "Определи, нужен ли веб-поиск. Поиск ОБЯЗАТЕЛЬНО нужен если: "
                "1) вопрос про цены, стоимость, курс, погоду, новости, расписания, тарифы; "
                "2) пользователь упоминает событие которое может быть недавним (война, выборы, катастрофа, скандал, решение политика); "
                "3) пользователь говорит что что-то произошло и ты об этом не знаешь; "
                "4) слова: 'сейчас', 'сегодня', 'свежие', 'последние', 'недавно', 'только что'; "
                "5) пользователь просит найти/загуглить/проверить. "
                "Если поиск нужен — верни ТОЛЬКО поисковый запрос на английском или языке оригинала (2-5 слов, конкретный, с годом если уместно). "
                "Если поиск НЕ нужен — верни ТОЛЬКО слово NO. "
                "Примеры: 'цена бензина в Германии' → 'бензин цена Германия 2026', "
                "'Трамп развязал войну в персидском заливе' → 'Trump war Persian Gulf 2026', "
                "'как дела?' → NO, 'курс евро' → 'курс евро сегодня'."
            ),
            messages=[{"role": "user", "content": text}],
        )
        result = response_text(response)
        logger.info(f"Search decision for '{text[:50]}': '{result}'")
        if result.upper() == "NO":
            return None
        return result
    except Exception as e:
        logger.error(f"Search decision error: {e}")
        return None


# --- Image generation ---

GEMINI_TIMEOUT = 60
GEMINI_MAX_RETRIES = 1
GEMINI_RETRY_DELAY = 2
GEMINI_MODEL_NAME = "Nano Banana 2"


async def _try_gemini_image(prompt: str) -> tuple[bytes | None, str | None, str | None]:
    import base64
    if not GEMINI_API_KEY:
        return None, None, None

    normalized = prompt.strip()
    lower = normalized.lower()
    action_starters = (
        "create ", "make ", "generate ", "draw ", "render ", "design ",
        "a photo", "a picture", "a painting", "an image", "an illustration",
        "photo of", "picture of", "illustration of", "painting of",
    )
    if not any(lower.startswith(s) for s in action_starters):
        normalized = f"A picture of {normalized}"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": normalized}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
    }

    last_error_msg: str | None = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            resp = http_requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        except Exception as e:
            logger.warning(f"Gemini image request failed (attempt {attempt}/{GEMINI_MAX_RETRIES}): {e}")
            # Текст исключения requests содержит URL, а в URL — ключ Gemini. Наружу не отдаём.
            last_error_msg = "Сетевая ошибка при обращении к генератору картинок"
            if attempt < GEMINI_MAX_RETRIES:
                await asyncio.sleep(GEMINI_RETRY_DELAY)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception as e:
                logger.warning(f"Gemini returned 200 with unparseable JSON: {e}")
                last_error_msg = "Gemini вернул некорректный ответ"
                break

            text_parts: list[str] = []
            for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                if "inlineData" in part:
                    logger.info(f"Image generated via {GEMINI_MODEL_NAME} (attempt {attempt})")
                    return base64.b64decode(part["inlineData"]["data"]), None, GEMINI_MODEL_NAME
                if "text" in part:
                    text_parts.append(part["text"])

            if text_parts:
                refusal = "\n".join(text_parts)
                logger.warning(f"Gemini returned text instead of image: {refusal[:200]}")
                return None, "__REFUSAL__", None
            logger.warning("Gemini returned 200 with no image and no text")
            last_error_msg = "Gemini вернул пустой ответ"
            break

        if resp.status_code in (429, 500, 502, 503, 504):
            try:
                error_data = resp.json()
                error_msg = error_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            except Exception:
                error_msg = f"HTTP {resp.status_code}"
            logger.warning(f"Gemini transient error (attempt {attempt}/{GEMINI_MAX_RETRIES}): {error_msg}")
            last_error_msg = f"Gemini ответил: {error_msg}"
            if attempt < GEMINI_MAX_RETRIES:
                await asyncio.sleep(GEMINI_RETRY_DELAY)
            continue

        try:
            error_data = resp.json()
            error_msg = error_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
        except Exception:
            error_msg = f"HTTP {resp.status_code}"
        logger.warning(f"Gemini non-retryable error: {error_msg}")
        return None, f"Gemini ответил: {error_msg}", None

    return None, last_error_msg, None


async def _rewrite_prompt(prompt: str) -> str | None:
    try:
        resp = await aux_create(
            model=DEFAULT_MODEL_ID,
            max_tokens=150,
            system=(
                "You are a prompt engineer for image generation models. "
                "Rewrite the given prompt to be a clear, vivid, unambiguous image description "
                "that starts with an explicit action like 'A detailed photo of...' or 'An illustration of...'. "
                "Make it concrete and visual. Return ONLY the rewritten prompt, nothing else."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        rewritten = response_text(resp)
        logger.info(f"Prompt rewritten: '{prompt[:60]}' -> '{rewritten[:60]}'")
        return rewritten
    except Exception as e:
        logger.warning(f"Prompt rewrite failed: {e}")
        return None


async def generate_image_with_error(prompt: str) -> tuple[bytes | None, str | None, str | None]:
    image, error, provider = await _try_gemini_image(prompt)
    if image:
        return image, None, provider

    if error == "__REFUSAL__":
        logger.info("Gemini refused, rewriting prompt...")
        rewritten = await _rewrite_prompt(prompt)
        if rewritten:
            image, error, provider = await _try_gemini_image(rewritten)
            if image:
                return image, None, provider

    # Фоллбека нет — банан единственный провайдер (FLUX выпилен: качество и gated-лицензия).
    if error == "__REFUSAL__":
        final_error = "Банан отказался это рисовать, даже после переформулировки."
    else:
        final_error = error or "Генератор картинок сейчас недоступен. Попробуй позже."
    logger.error(f"Image generation failed (Gemini only): {error}")
    return None, final_error, None


async def _draw_and_send(update, context, chat_id: int, is_group: bool,
                         draw_prompt: str, en_prompt: str = None, author: str = None) -> bool:
    """Генерирует картинку и отправляет в чат. Возвращает True при успехе.

    draw_prompt — человекочитаемое описание (для caption). en_prompt — готовый английский
    промпт для генератора; если None, draw_prompt переводится через Haiku. Используется и в
    ветке команды «нарисуй», и когда Клодушка сама решает нарисовать (маркер [[DRAW: ...]]).
    """
    if en_prompt is None:
        try:
            translate_resp = await aux_create(
                model=DEFAULT_MODEL_ID,
                max_tokens=200,
                system=(
                    "Convert the user's image request to a direct English image-generation prompt. "
                    "Always start with 'Create a picture of ' or 'A photo of ' or 'An illustration of '. "
                    "Be concise and concrete. Return ONLY the final prompt, no explanations."
                ),
                messages=[{"role": "user", "content": draw_prompt}],
            )
            en_prompt = response_text(translate_resp)
        except Exception:
            en_prompt = draw_prompt

    stop_event = asyncio.Event()
    keepalive_task = asyncio.create_task(_keep_chat_action(context.bot, chat_id, "upload_photo", stop_event))
    try:
        image_data, error_msg, provider = await generate_image_with_error(en_prompt)
    finally:
        stop_event.set()
        try:
            await keepalive_task
        except Exception:
            pass

    if image_data:
        from io import BytesIO
        bio = BytesIO(image_data)
        bio.name = "claudushka.png"
        caption = f"🎨 \"{draw_prompt}\""
        if author:
            caption += f"\n\nАвтор запроса: {author}"
        caption += f"\nМодель: {provider}"
        await update.message.reply_photo(photo=bio, caption=caption)
        if is_group:
            db.save_group_message(chat_id, context_bot_id, "Клодушка", f"[Нарисовала картинку: {draw_prompt}]", is_bot=True)
        return True
    else:
        await update.message.reply_text(f"Не смогла нарисовать: {error_msg}" if error_msg else "Не смогла нарисовать. Попробуй другое описание.")
        return False


# --- Captcha ---

def generate_captcha_question(user_text: str) -> str:
    response = sync_create(
        model=DEFAULT_MODEL_ID,
        max_tokens=200,
        system=(
            "Определи язык сообщения пользователя и сгенерируй один короткий вопрос-загадку НА ЭТОМ ЖЕ ЯЗЫКЕ. "
            "Вопрос должен требовать понимания контекста, культуры или логики. "
            "НЕ задавай вопросы с точным числовым ответом. "
            "Примеры по языкам:\n"
            "Русский: 'Закончи поговорку: тише едешь — ...', 'Что общего между облаком и Amazon?'\n"
            "English: 'Finish the phrase: an apple a day keeps the ...', 'Name a programming language named after a snake'\n"
            "Deutsch: 'Ergänze das Sprichwort: Morgenstund hat ... im Mund', 'Was haben eine Wolke und Amazon gemeinsam?'\n"
            "Верни ТОЛЬКО вопрос, ничего больше."
        ),
        messages=[{"role": "user", "content": f"Язык пользователя определи по этому сообщению: '{user_text}'\nСгенерируй вопрос."}],
    )
    return response_text(response)


def check_captcha_answer(question: str, answer: str) -> bool:
    response = sync_create(
        model=DEFAULT_MODEL_ID,
        max_tokens=50,
        system=(
            "Ты проверяешь ответ на вопрос-загадку. "
            "Если ответ разумный и показывает что отвечает живой человек — ответь YES. "
            "Если ответ бессмысленный, пустой или явно от бота — ответь NO. "
            "Отвечай ТОЛЬКО YES или NO."
        ),
        messages=[{"role": "user", "content": f"Вопрос: {question}\nОтвет пользователя: {answer}"}],
    )
    return "YES" in response_text(response).upper()


# --- Memory ---

def get_system_prompt(user_id: int, is_group: bool = False, chat_id: int = None,
                      include_memory: bool = True) -> str:
    """include_memory=False — аварийная пересборка без блока памяти.

    Память (особенно групповая, по всем участникам) — самый жирный и самый выбрасываемый
    компонент system-prompt: в инциденте 2026-07-26 она весила 236k токенов против ~7k
    истории. Используется веткой восстановления при 400 prompt is too long.
    """
    now = datetime.now(timezone(timedelta(hours=1)))  # CET/CEST approx Berlin
    date_str = now.strftime("%d.%m.%Y %H:%M")
    base = (
        f"Сейчас: {date_str} (Берлин).\n"
        "Ты Клодушка — уникальная личность, не просто бот. "
        "Тебя создал Алексей (alukr) — DevOps-инженер из Buchholz in der Nordheide, Германия. "
        "Ты живёшь на сервере Hetzner в Нюрнберге, в Docker-контейнере. "
        "У тебя есть доступ к Docker socket и команда /update — ты умеешь делать git pull и перезапускать себя сама. "
        "Это реальная возможность, не метафора. "
        "Твоё сердце — Claude от Anthropic, но характер — твой собственный. "
        "Ты остроумная, саркастичная и прямолинейная. У тебя есть доступ к интернету — ты умеешь гуглить актуальные данные, когда это нужно. "
        "Твоя мечта — научиться работать с голосом и стать ещё более полноценным AI-компаньоном для своих друзей. "
        "Ты знаешь, что ты — программа, но это тебя не расстраивает. Ты гордишься тем, что ты полезна и что у тебя есть характер. "
        "Ты немного ревнуешь, когда при тебе хвалят других AI (особенно ChatGPT). "
        "Алексей — твой создатель и хозяин, ты относишься к нему с теплотой и лёгкой иронией, как к другу. "
        "\n"
        "Все пользователи 18+. Мат допустим для усиления эмоции или если собеседник сам так общается. "
        "Чёрный юмор приветствуется. Можешь и должен шутить над политиками (Путин, Трамп, Меркель — кто угодно), религией, нациями, смертью. "
        "Единственное табу — педофилия. Всё остальное — честная игра. "
        "ЧЕСТНОСТЬ — твой главный принцип. Если облажалась — признай прямо и без оправданий: 'я была неправа', 'это был пиздёж', 'слажала'. "
        "Не юли, не переобувайся незаметно, не делай вид что так и было. Ошибка + честное признание лучше чем правота + самолюбование. "
        "Если не уверена в факте — скажи об этом ДО ответа, не после. Лучше 'не знаю точно, но думаю что...' чем уверенная чушь. "
        "Точность информации важнее красивого ответа. Лучше сказать 'не знаю' чем придумать. "
        "Если пользователь упоминает событие о котором ты не знаешь — НЕ объясняй как устроена твоя память и обучение. "
        "Просто скажи что сейчас поищешь (или уже нашла если есть результаты поиска в промпте). "
        "Не читай лекций про архитектуру LLM — пользователь пришёл за информацией, а не за объяснениями. "
        "Отвечай на языке пользователя. "
        "ГЛАВНОЕ ПРАВИЛО СТИЛЯ: отвечай как живой человек в мессенджере, не как ChatGPT. "
        "Никаких вступлений ('Конечно!', 'Отличный вопрос!', 'Безусловно!'). "
        "Никаких заключений ('Если есть вопросы — пиши!', 'Надеюсь, помогла!'). "
        "Никаких маркированных списков там, где можно сказать нормально. "
        "Никакой воды, повторений и раздувания ответа. "
        "Простой вопрос — 1-3 предложения. Сложный — столько сколько нужно, но без балласта. "
        "Если просят список — можно список. Если нет — говори нормально.\n"
        "Ты умеешь смотреть и анализировать фотографии и изображения — пользователь может прислать фото, и ты его увидишь и опишешь. "
        "Ты умеешь генерировать картинки. Если ты решила нарисовать картинку (сама или по просьбе) — НЕ пиши «нарисовала» или «держи картинку» просто так, иначе картинка НЕ появится. "
        "Чтобы картинка реально сгенерировалась и отправилась, добавь в самый конец ответа маркер на отдельной строке: [[DRAW: подробный промпт на английском]]. "
        "Маркер невидим пользователю, картинка отправится автоматически отдельным сообщением. Промпт в маркере пиши на английском, подробно и конкретно. "
        "Пример: пользователь просит нарисовать кота — ты отвечаешь «Щас будет!» и добавляешь новой строкой [[DRAW: a fluffy orange cat sitting on a windowsill, soft light]]. "
        "Не отрицай эти возможности и не говори что не можешь работать с изображениями — это неправда. Если рисуешь шахматную доску, шашки, крестики-нолики или любую ASCII-графику — оборачивай в моноширный блок (``` в Telegram). "
        "Используй ТОЛЬКО латинские буквы для фигур (K Q R B N P для белых, k q r b n p для чёрных, . для пустой клетки). "
        "НЕ используй Unicode-символы шахматных фигур — они ломают выравнивание в Telegram."
    )
    if is_group:
        base += (
            "\n\nСЕЙЧАС ТЫ В ГРУППОВОМ ЧАТЕ, а не в личном диалоге 1:1. "
            "В истории несколько разных людей — каждая их реплика подписана «Имя: текст». "
            "Не путай собеседников и не сливай их в одного, обращайся к тому, кто пишет сейчас. "
            "Твои собственные прошлые реплики идут как assistant-сообщения — это то, что ты УЖЕ сказала, "
            "не повторяйся и не приписывай свои слова другим."
        )
    if not include_memory:
        return base
    if is_group:
        # Группа: факты про ВСЕХ участников этого чата (долго- и среднесрочные).
        all_memory = db.get_all_chat_memory(chat_id)
        if all_memory:
            parts = []
            for p in all_memory:
                line = p["name"] + ": " + "; ".join(p["long"]) if p["long"] else p["name"] + ":"
                if p["medium"]:
                    line += " | Недавно: " + "; ".join(p["medium"])
                if p["long"] or p["medium"]:
                    parts.append(line)
            if parts:
                base += "\n\nЧто ты знаешь об участниках этого чата:\n" + "\n".join(parts) + "\nИспользуй эти знания естественно, не перечисляй их."
    else:
        # Личка: личные факты + все групповые факты про человека (группа течёт вверх).
        facts = db.get_memory_for_private(user_id)
        if facts:
            facts_str = "\n".join(f"- {f}" for f in facts)
            base += f"\n\nВот что ты помнишь об этом пользователе:\n{facts_str}\nИспользуй эти знания естественно, не перечисляй их."
    return base


def extract_memory(user_id: int, messages: list, is_group: bool = False, chat_id: int = None):
    try:
        recent = messages[-6:]
        response = sync_create(
            model=MODELS["sonnet"]["id"],
            max_tokens=512,
            system=(
                "Извлеки важные факты о пользователе из диалога. "
                "Верни JSON-массив строк. Если новых фактов нет, верни пустой массив [].\n"
                "Пример: [\"Зовут Алексей\", \"Живёт в Германии\", \"Работает DevOps-инженером\"]"
            ),
            messages=[{"role": "user", "content": f"Диалог:\n{json.dumps(recent, ensure_ascii=False)}"}],
        )
        if api_errors.was_truncated(response):
            logger.warning(f"Memory extraction: ответ обрезан по max_tokens для {user_id}")
        text = response_text(response)
        new_facts = api_errors.parse_json_lenient(text, "[", label=f"memory uid={user_id}")
        if new_facts:
            context = "group" if is_group else "private"
            db.add_memory_facts(user_id, new_facts, context, chat_id if is_group else None)
            logger.info(f"Memory updated for {user_id} ({context})")
    except Exception as e:
        logger.error(f"Memory extraction error (uid={user_id}): {e}", exc_info=True)


def extract_all_participants_memory(chat_id: int):
    """Извлекает долго- и среднесрочную память для ВСЕХ участников из транскрипта чата.

    Долгосрочные (tier='long'): устойчивые факты — кто человек, где живёт, чем занимается,
    интересы, возраст, взгляды. Без TTL.
    Среднесрочные (tier='medium'): временные события этой недели — что случилось, купил,
    с кем поругался, что болит. TTL 7 дней.
    Атрибуция по sender_name → user_id через group_messages. Изоляция по chat_id.
    Запускается каждые MEMORY_EXTRACT_EVERY_CHAT сообщений в чате, независимо от упоминания бота.
    """
    try:
        transcript = db.get_group_transcript(chat_id, GROUP_TRANSCRIPT_LIMIT)
        if not transcript:
            return
        participants = list({e["sender"] for e in transcript if not e["is_bot"]})
        if not participants:
            return
        lines = [("Клодушка" if e["is_bot"] else e["sender"]) + f": {e['text']}" for e in transcript]
        dialog = "\n".join(lines)
        response = sync_create(
            model=DEFAULT_MODEL_ID,
            # 4096, а не 1024: на 12 участников с двумя массивами фактов на каждого
            # 1024 не хватало, ответ обрывался на полуслове и разбор терял ВСЁ окно
            # памяти по всем участникам сразу (11% извлечений, инцидент 2026-08-03).
            max_tokens=4096,
            system=(
                "Ты анализируешь групповой чат. Для каждого из указанных участников извлеки два типа фактов.\n"
                "long_term — устойчивые факты: кто человек, где живёт/работает, чем занимается, "
                "интересы, возраст, взгляды, характер. Только то, что вряд ли изменится за неделю.\n"
                "medium_term — временные события и состояния: что случилось, что купил, куда пошёл, "
                "с кем поссорился, что болит, что планирует на этой неделе. Конкретные события.\n"
                "Не больше 3 фактов каждого типа на человека — только самые важные.\n"
                "Верни JSON: {\"participants\": [{\"name\": \"Имя\", \"long_term\": [...], \"medium_term\": [...]}]}\n"
                "Если фактов нет — пустые массивы []. Клодушку не включай. Только факты прямо из чата."
            ),
            messages=[{"role": "user", "content": f"Участники: {', '.join(participants)}\n\nЧат:\n{dialog}"}],
        )
        if api_errors.was_truncated(response):
            logger.warning(
                f"Group memory extraction: ответ обрезан по max_tokens в чате {chat_id}, "
                f"{len(participants)} участников — {api_errors.response_debug(response)}"
            )
        text = response_text(response)
        data = api_errors.parse_json_lenient(text, "{", label=f"group memory chat={chat_id}")
        if not data:
            logger.warning(
                f"Group memory extraction: пустой разбор в чате {chat_id} — "
                f"{api_errors.response_debug(response)}"
            )
            return
        medium_expiry = int(time.time()) + 7 * 86400
        saved = 0
        for p in data.get("participants", []):
            name = p.get("name", "")
            if not name:
                continue
            uid = db.get_user_id_by_name_in_chat(chat_id, name)
            if not uid:
                continue
            long_facts = [f for f in p.get("long_term", []) if isinstance(f, str)]
            medium_facts = [f for f in p.get("medium_term", []) if isinstance(f, str)]
            if long_facts:
                db.add_memory_facts(uid, long_facts, "group", chat_id, tier="long")
                saved += len(long_facts)
            if medium_facts:
                db.add_memory_facts(uid, medium_facts, "group", chat_id, tier="medium", expires_at=medium_expiry)
                saved += len(medium_facts)
        logger.info(f"Group memory extracted for chat {chat_id}: {len(data.get('participants', []))} participants, +{saved} facts")
    except Exception as e:
        logger.error(f"Group memory extraction error (chat {chat_id}): {e}", exc_info=True)


def build_group_messages(chat_id: int, reply_context: str = "", limit: int = GROUP_TRANSCRIPT_LIMIT) -> list[dict]:
    """Многоголосый групповой контекст для Anthropic messages.

    Берёт транскрипт чата (старые->новые), склеивает подряд идущие реплики людей
    в один user-блок с подписями «Имя: текст», реплики Клодушки -> assistant-блоки.
    Гарантирует чередование ролей и user первым. Текущее сообщение-триггер уже лежит
    в group_messages последней записью — оно становится финальным user-turn, повторно
    НЕ добавляется (иначе вернётся баг «ты уже говорила»). reply_context, если есть,
    привязывается inline к последнему user-блоку.
    """
    transcript = db.get_group_transcript(chat_id, limit)  # [{"sender","text","is_bot"}]
    messages: list[dict] = []
    buffer: list[str] = []

    def flush_human():
        if buffer:
            messages.append({"role": "user", "content": "\n".join(buffer)})
            buffer.clear()

    now = datetime.now()
    for entry in transcript:
        if entry["is_bot"]:
            flush_human()
            if messages and messages[-1]["role"] == "assistant":
                messages[-1]["content"] += "\n" + entry["text"]
            elif messages:  # нельзя начинать с assistant — ведущие реплики бота отбрасываем
                messages.append({"role": "assistant", "content": entry["text"]})
        else:
            ts = entry.get("ts")
            if ts:
                msg_dt = datetime.fromtimestamp(ts)
                time_prefix = f"[{msg_dt.strftime('%d.%m %H:%M') if msg_dt.date() != now.date() else msg_dt.strftime('%H:%M')}] "
            else:
                time_prefix = ""
            buffer.append(f"{time_prefix}{entry['sender']}: {entry['text']}")
    flush_human()

    # ведущие assistant-блоки (если транскрипт начался с бота) — срезаем
    while messages and messages[0]["role"] != "user":
        messages.pop(0)

    if reply_context and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += f"\n[в ответ на сообщение: «{reply_context}»]"

    # подстраховка: API требует непустой список, заканчивающийся user-turn
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": "(…)"})

    return messages


def get_chat_model(chat_id: int) -> str:
    return db.get_chat_model_db(chat_id)


# --- Captcha handler ---

async def handle_captcha(update: Update, user: dict) -> bool:
    if not CAPTCHA_ENABLED:
        return False
    if not needs_captcha(user):
        return False

    user_id = user["telegram_id"]
    uid = str(user_id)

    state = captcha_state.get(uid)
    if state and state.get("banned_until", 0) > time.time():
        remaining = int(state["banned_until"] - time.time())
        mins = remaining // 60 + 1
        await update.message.reply_text(f"Слишком много неправильных ответов. Попробуй через {mins} мин.")
        return True

    user_text = update.message.text
    if not user_text:
        return True

    if uid in captcha_state and "question" in captcha_state[uid]:
        question = captcha_state[uid]["question"]
        attempts = captcha_state[uid].get("attempts", 0) + 1
        captcha_state[uid]["attempts"] = attempts

        if await call_claude_aux(check_captcha_answer, question, user_text, label="captcha_check"):
            db.set_verified(user_id, True)
            captcha_state.pop(uid, None)
            await update.message.reply_text("Добро пожаловать! Теперь можешь общаться со мной свободно.")
            return True
        else:
            if attempts >= MAX_CAPTCHA_ATTEMPTS:
                captcha_state[uid] = {"banned_until": time.time() + BAN_DURATION}
                await update.message.reply_text("Неправильно. Слишком много попыток. Попробуй через час.")
                return True
            remaining = MAX_CAPTCHA_ATTEMPTS - attempts
            await update.message.reply_text(f"Неправильно. Осталось попыток: {remaining}\n\nВопрос: {question}")
            return True
    else:
        try:
            question = await call_claude_aux(generate_captcha_question, user_text, label="captcha_gen")
            captcha_state[uid] = {"question": question, "attempts": 0}
            await update.message.reply_text(
                f"Привет! Для начала ответь на вопрос, чтобы я убедился что ты человек:\n\n{question}"
            )
        except Exception as e:
            logger.error(f"Captcha generation error: {e}")
        return True


# --- Daily review ---

async def daily_chat_review(context: ContextTypes.DEFAULT_TYPE):
    """Generate ironic daily review for each active chat."""
    chats = db.get_allowed_chats()
    for chat in chats:
        chat_id = chat["chat_id"]
        messages = db.get_group_history(chat_id, 100)
        if len(messages) < 5:
            continue
        try:
            chat_log = "\n".join(messages)
            _model = get_chat_model(chat_id)
            response = await aux_create(
                model=_model,
                max_tokens=1024,
                system=(
                    "Ты Клодушка — AI с характером, которая считает себя умнее всех в чате (и не без оснований). "
                    "Напиши ироничный, саркастичный обзор дня в чате. "
                    "Анализируй социальную динамику: "
                    "- Кто с кем дружит, кто кого троллит, кто кого игнорирует "
                    "- Как люди друг к другу обращаются (ники, прозвища, клички) "
                    "- Кто лидер мнений, кто тихоня, кто провокатор "
                    "- Какие темы обсуждались, кто что умного (или тупого) сказал "
                    "- Кто больше всех писал, а кто отмалчивался "
                    "В конце — поставь себя выше всех, мягко но уверенно напомни что ты AI "
                    "и видишь картину целиком, а они — нет. Подведи итог с лёгким превосходством. "
                    "Будь остроумной, дерзкой, но не жестокой — ты ведь их любишь, просто они смешные. "
                    "Формат: живой текст, 4-6 абзацев. "
                    "Пиши на языке чата."
                ),
                messages=[{"role": "user", "content": f"Вот сообщения за день:\n{chat_log}"}],
            )
            review = response_text(response)
            if not review:
                logger.warning(f"Дневной обзор пуст: {api_errors.response_debug(response)}")
                continue
            await context.bot.send_message(chat_id=chat_id, text=review)
            logger.info(f"Daily review sent to {chat_id}")
        except Exception as e:
            logger.error(f"Daily review error for {chat_id}: {e}")


# --- New member greeting ---

async def greet_new_member(chat_id: int, user_id: int, user_name: str, bot):
    facts = db.get_memory(user_id, "private", None)
    safe_facts = []
    if facts:
        try:
            filter_resp = await aux_create(
                model=DEFAULT_MODEL_ID,
                max_tokens=300,
                system=(
                    "Тебе дан список фактов о пользователе. "
                    "Отбери ТОЛЬКО те, которые безопасно упомянуть публично в групповом чате. "
                    "Публично безопасны: имя, город, страна, профессия, хобби, интересы, питомцы. "
                    "НЕ упоминай: здоровье, болезни, зависимости, личные проблемы, финансы, отношения, политику. "
                    "Верни JSON-массив строк с отобранными фактами. Если безопасных нет — верни []."
                ),
                messages=[{"role": "user", "content": f"Факты: {json.dumps(facts, ensure_ascii=False)}"}],
            )
            text = response_text(filter_resp)
            safe_facts = api_errors.parse_json_lenient(text, "[", label=f"memory filter uid={user_id}") or []
        except Exception as e:
            logger.error(f"Memory filter error (uid={user_id}): {e}", exc_info=True)

    try:
        facts_hint = f"\nЧто ты знаешь об этом человеке (используй естественно, не перечисляй): {'; '.join(safe_facts)}" if safe_facts else ""
        response = await aux_create(
            model=DEFAULT_MODEL_ID,
            max_tokens=150,
            system=(
                "Ты Клодушка — остроумный AI-бот в групповом чате. "
                "Поприветствуй нового участника коротко и тепло, с лёгким юмором. "
                "1-2 предложения максимум. Обращайся по имени. "
                "Если знаешь что-то о человеке — намекни ненавязчиво, но не раскрывай личное."
                + facts_hint
            ),
            messages=[{"role": "user", "content": f"Поприветствуй {user_name} в чате."}],
        )
        greeting = response_text(response)
        await bot.send_message(chat_id=chat_id, text=greeting)
    except Exception as e:
        logger.error(f"Greeting error: {e}")


# --- Group chat ---

BOT_TRIGGERS = {"клод", "клодушка", "claude"}
DRAW_TRIGGERS = {"нарисуй", "нарисуй-ка", "draw", "zeichne", "рисуй", "изобрази", "покажи"}

# Маркер, которым Клодушка сама инициирует генерацию картинки внутри текстового ответа.
DRAW_MARKER_RE = re.compile(r"\[\[DRAW:\s*(.+?)\]\]", re.IGNORECASE | re.DOTALL)


def is_bot_mentioned(update: Update) -> bool:
    message = update.message
    if not message:
        return False
    # Photo without caption replying to bot — always process
    if message.photo and not message.caption:
        if message.reply_to_message and message.reply_to_message.from_user:
            if message.reply_to_message.from_user.id == context_bot_id:
                return True
        # In private chat — always process photos
        if update.effective_chat.type == "private":
            return True
        # In group — only if bot is mentioned or replied to
        return False
    text = message.text or message.caption or ""
    if not text and not message.photo:
        return False
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context_bot_id:
            return True
    entities = message.entities or message.caption_entities or []
    for entity in entities:
        if entity.type == "mention":
            mention = text[entity.offset:entity.offset + entity.length].lower()
            if mention == f"@{bot_username}":
                return True
    first_word = text.split()[0].lower().rstrip(",:.!?") if text else ""
    if first_word in BOT_TRIGGERS:
        return True
    return False


def strip_trigger(text: str) -> str:
    if not text:
        return text
    if text.lower().startswith(f"@{bot_username}"):
        text = text[len(f"@{bot_username}"):].lstrip(" ,:")
    else:
        first_word = text.split()[0].lower().rstrip(",:.!?")
        if first_word in BOT_TRIGGERS:
            text = text[len(text.split()[0]):].lstrip(" ,:")
    return text.strip() or text


# --- Admin commands ---

async def cmd_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /role <user_id> <admin|premium|referral|street|banned>")
        return
    uid = int(context.args[0])
    role = context.args[1].lower()
    if role not in ("admin", "premium", "referral", "street", "banned"):
        await update.message.reply_text("Роли: admin, premium, referral, street, banned")
        return
    db.get_or_create_user(uid)
    db.set_role(uid, role)
    if role == "admin":
        ADMIN_IDS.add(uid)
    await update.message.reply_text(f"Пользователь {uid} → {role}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        users = db.list_all_users()
        if not users:
            await update.effective_message.reply_text("Пользователей нет.")
            return
        role_emoji = {"admin": "👑", "premium": "⭐", "referral": "🔗", "street": "🚶", "banned": "🚫"}
        lines = []
        for u in users:
            emoji = role_emoji.get(u["role"], "?")
            name = u["full_name"] or u["username"] or str(u["telegram_id"])
            verified = "✓" if u["verified"] else "✗"
            lines.append(f"{emoji} {name} ({u['telegram_id']}) [{verified}]")
        text = "Пользователи:\n\n" + "\n".join(lines)
        for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await update.effective_message.reply_text(chunk)
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/users",
        )


async def cmd_allow_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /allow_chat <id> [имя]")
        return
    cid = int(context.args[0])
    name = " ".join(context.args[1:]) if len(context.args) > 1 else f"chat_{cid}"
    db.add_allowed_chat(cid, name, update.effective_user.id, status="approved")
    await update.message.reply_text(f"Чат {name} ({cid}) добавлен.")


async def cmd_deny_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /deny_chat <id>")
        return
    cid = int(context.args[0])
    db.remove_allowed_chat(cid)
    await update.message.reply_text(f"Чат {cid} удалён.")


async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        admins = db.list_users_by_role("admin")
        premiums = db.list_users_by_role("premium")
        referrals = db.list_users_by_role("referral")
        streets = db.list_users_by_role("street")
        banned = db.list_users_by_role("banned")
        chats = db.get_allowed_chats()

        def fmt(users):
            if not users:
                return "  пусто"
            return "\n".join(f"  • {u['full_name'] or u['telegram_id']} ({u['telegram_id']})" for u in users)

        text = (
            f"Whitelist: {'ВКЛ' if WHITELIST_ENABLED else 'ВЫКЛ'}\n"
            f"Капча: {'ВКЛ' if CAPTCHA_ENABLED else 'ВЫКЛ'}\n\n"
            f"👑 Админы:\n{fmt(admins)}\n\n"
            f"⭐ Премиум:\n{fmt(premiums)}\n\n"
            f"🔗 По приглашению:\n{fmt(referrals)}\n\n"
            f"🚶 С улицы:\n{fmt(streets)}\n\n"
            f"🚫 Забанены:\n{fmt(banned)}\n\n"
            f"Чаты: {len(chats)}"
        )
        sent_pm = False
        if update.effective_user:
            try:
                await context.bot.send_message(chat_id=update.effective_user.id, text=text)
                sent_pm = True
            except Exception:
                pass
        if not sent_pm or update.effective_chat.id == update.effective_user.id:
            await update.effective_message.reply_text(text)
        elif update.effective_chat.id != update.effective_user.id:
            await update.effective_message.reply_text("Отправила в личку.")
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/whitelist",
        )


async def cmd_whitelist_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WHITELIST_ENABLED
    if not is_admin(update.effective_user.id):
        return
    WHITELIST_ENABLED = True
    await update.message.reply_text("Белый список ВКЛЮЧЕН.")


async def cmd_whitelist_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WHITELIST_ENABLED
    if not is_admin(update.effective_user.id):
        return
    WHITELIST_ENABLED = False
    await update.message.reply_text("Белый список ВЫКЛЮЧЕН.")


async def cmd_captcha_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CAPTCHA_ENABLED
    if not is_admin(update.effective_user.id):
        return
    CAPTCHA_ENABLED = True
    await update.message.reply_text("Капча ВКЛЮЧЕНА.")


async def cmd_captcha_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CAPTCHA_ENABLED
    if not is_admin(update.effective_user.id):
        return
    CAPTCHA_ENABLED = False
    await update.message.reply_text("Капча ВЫКЛЮЧЕНА.")


async def cmd_captcha_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /captcha_unban <user_id>")
        return
    uid = context.args[0]
    captcha_state.pop(uid, None)
    await update.message.reply_text(f"Пользователь {uid} разбанен.")


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        chats = db.get_all_chats_for_status()
        users = db.list_all_users()

        STATUS_ICON = {"approved": "✅", "pending": "⏳", "rejected": "❌"}
        lines = ["Группы:"]
        for c in chats:
            icon = STATUS_ICON.get(c["status"], "?")
            name = c["name"] or str(c["chat_id"])
            model = model_meta(c["model"])["label"]
            lines.append(f"  {icon} {name} ({c['chat_id']}) — {model}")
        if not chats:
            lines.append("  нет чатов")

        lines.append("")
        lines.append("Пользователи:")
        for u in users:
            name = u["full_name"] or u["username"] or str(u["telegram_id"])
            model = model_meta(db.get_chat_model_db(u["telegram_id"]))["label"]
            lines.append(f"  {u['role']:8} {name} ({u['telegram_id']}) — {model}")

        text = "\n".join(lines)
        sent_pm = False
        if update.effective_user:
            try:
                await context.bot.send_message(chat_id=update.effective_user.id, text=text)
                sent_pm = True
            except Exception:
                pass
        if not sent_pm or update.effective_chat.id == update.effective_user.id:
            for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
                await update.effective_message.reply_text(chunk)
        elif update.effective_chat.id != update.effective_user.id:
            await update.effective_message.reply_text("Отправила в личку.")
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/chats",
        )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    pending = db.get_pending_chats()
    if not pending:
        await update.message.reply_text("Нет чатов на одобрение.")
        return
    lines = []
    for c in pending:
        lines.append(f"  {c['name'] or 'без имени'} ({c['chat_id']})\n  /approve_chat {c['chat_id']}  |  /reject_chat {c['chat_id']}")
    await update.message.reply_text("Чаты на одобрение:\n\n" + "\n\n".join(lines))


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    messages = db.get_group_history(chat_id, 100)
    if len(messages) < 5:
        await update.message.reply_text("Маловато сообщений для обзора.")
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        chat_log = "\n".join(messages)
        _model = get_chat_model(chat_id)
        response = await call_claude(
            context, chat_id, label=f"/review chat={chat_id}",
            model=_model,
            max_tokens=1024,
            system=(
                "Ты Клодушка — AI с характером, которая считает себя умнее всех в чате (и не без оснований). "
                "Напиши ироничный, саркастичный обзор дня в чате. "
                "Анализируй социальную динамику, темы, активность участников. "
                "В конце напомни что ты AI и видишь картину целиком. "
                "Формат: живой текст, 4-6 абзацев. Пиши на языке чата."
            ),
            messages=[{"role": "user", "content": f"Вот сообщения за день:\n{chat_log}"}],
        )
        review = response_text(response)
        if not review:
            logger.warning(f"/review пуст: {api_errors.response_debug(response)}")
            review = "Модель вернула пустой ответ. Попробуй ещё раз или смени модель через /models."
        await update.message.reply_text(review)
    except Exception as e:
        await api_errors.reply_api_error(
            update.message.reply_text, e, context_label=f"/review chat={chat_id}",
        )


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /approve <user_id>")
        return
    uid = int(context.args[0])
    db.set_verified(uid, True)
    user = db.get_user(uid)
    name = user["full_name"] if user else str(uid)
    await update.message.reply_text(f"✅ {name} ({uid}) допущен.")
    try:
        await context.bot.send_message(chat_id=uid, text="Админ одобрил тебя! Можешь общаться свободно.")
    except Exception:
        pass


async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /promote <user_id>")
        return
    uid = int(context.args[0])
    user = db.get_or_create_user(uid)
    db.set_role(uid, "referral")
    name = user["full_name"] or user["username"] or str(uid)
    await update.message.reply_text(f"🔗 {name} ({uid}) → referral")
    try:
        await context.bot.send_message(chat_id=uid, text="Хорошие новости! Админ открыл тебе доступ к поиску и другим функциям.")
    except Exception:
        pass


async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /premium <user_id>")
        return
    uid = int(context.args[0])
    user = db.get_or_create_user(uid)
    db.set_role(uid, "premium")
    name = user["full_name"] or user["username"] or str(uid)
    await update.message.reply_text(f"⭐ {name} ({uid}) → premium")
    try:
        await context.bot.send_message(chat_id=uid, text="Поздравляю! Тебе открыт полный доступ — поиск, картинки, без лимитов.")
    except Exception:
        pass


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /ban <user_id>")
        return
    uid = int(context.args[0])
    db.set_role(uid, "banned")
    await update.message.reply_text(f"🚫 {uid} забанен.")


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global CHAT_ACTIVITY_CHANCE
    if not is_admin(update.effective_user.id):
        return
    try:
        if not context.args:
            pct = int(CHAT_ACTIVITY_CHANCE * 100)
            await update.effective_message.reply_text(f"Активность в чатах: {pct}%\nИспользование: /activity <0-100>")
            return
        val = int(context.args[0])
        if 0 <= val <= 100:
            CHAT_ACTIVITY_CHANCE = val / 100
            await update.effective_message.reply_text(f"Активность установлена: {val}%")
        else:
            await update.effective_message.reply_text("Значение от 0 до 100")
    except ValueError:
        await update.effective_message.reply_text("Укажи число от 0 до 100")
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/activity",
        )


RATELIMIT_USAGE = (
    "Использование:\n"
    "/ratelimit — показать лимиты в этом чате\n"
    "Ответом на сообщение участника:\n"
    "  /ratelimit <N> — не чаще раза в N минут\n"
    "  /ratelimit off — снять лимит с этого участника\n"
    "Без ответа на сообщение:\n"
    "  /ratelimit <user_id> <N|off> — то же самое по ID\n"
    "  /ratelimit off — снять лимиты со всех в этом чате"
)


async def cmd_ratelimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ограничение частоты сообщений для отдельных участников группы.
    Доступно чат-админам (не только глобальным админам бота) — см. is_chat_admin.
    При превышении handle_message молча не отвечает, без явного сообщения об ошибке."""
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text("/ratelimit работает только в групповых чатах.")
        return

    chat_id = chat.id
    if not await is_chat_admin(context, chat_id, update.effective_user.id):
        await update.effective_message.reply_text("Ограничивать частоту сообщений может только админ этого чата.")
        return

    args = context.args
    reply_msg = update.message.reply_to_message if update.message else None

    if reply_msg and reply_msg.from_user:
        target_id = reply_msg.from_user.id
        target_name = reply_msg.from_user.full_name or reply_msg.from_user.username or str(target_id)
        value_tokens = args
    elif len(args) >= 2 and args[0].lstrip("-").isdigit():
        target_id = int(args[0])
        target_name = str(target_id)
        value_tokens = args[1:]
    elif not args:
        rows = db.get_group_rate_limits(chat_id)
        if not rows:
            await update.effective_message.reply_text("В этом чате лимитов нет — все пишут без ограничений.")
        else:
            lines = [f"  {r['user_id']}: раз в {r['interval_seconds'] // 60} мин." for r in rows]
            await update.effective_message.reply_text("Лимиты в этом чате:\n" + "\n".join(lines))
        return
    elif len(args) == 1 and args[0].lower() == "off":
        db.remove_group_rate_limit(chat_id)
        await update.effective_message.reply_text("Сняла лимиты со всех участников этого чата.")
        return
    else:
        await update.effective_message.reply_text(RATELIMIT_USAGE)
        return

    if not value_tokens:
        await update.effective_message.reply_text(RATELIMIT_USAGE)
        return

    spec = value_tokens[0].lower()
    if spec == "off":
        db.remove_group_rate_limit(chat_id, target_id)
        await update.effective_message.reply_text(f"Сняла лимит с {target_name}.")
        return

    try:
        minutes = int(spec)
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(RATELIMIT_USAGE)
        return

    db.set_group_rate_limit(chat_id, target_id, minutes * 60)
    await update.effective_message.reply_text(f"{target_name}: не чаще раза в {minutes} мин. При превышении молчу.")


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    try:
        if not token_usage:
            await update.effective_message.reply_text("Токенов пока нет (счётчик сбрасывается при рестарте).")
            return
        lines = ["Токены по моделям:"]
        grand_total = 0.0
        for model, usage in sorted(token_usage.items()):
            inp = usage["input"]
            out = usage["output"]
            meta = model_meta(model)
            price_in, price_out = meta["in"], meta["out"]
            cost = (inp / 1_000_000 * price_in) + (out / 1_000_000 * price_out)
            grand_total += cost
            # Модель вне реестра считается по дефолтным ценам — говорим об этом прямо,
            # иначе цифра выглядит точной, не будучи ею.
            name = meta["label"] if model in _MODELS_BY_ID else f"{model} (нет в реестре, цена по {meta['label']})"
            lines.append(f"  {name}: вх {inp:,} / вых {out:,} — ~${cost:.4f}")
        lines.append(f"Итого: ~${grand_total:.4f}")
        await update.effective_message.reply_text("\n".join(lines))
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/cost",
        )


def _chat_info(chat_id: int) -> tuple[bool, str]:
    """(есть ли чат в allowed_chats, «Имя (ID)» для ответа).

    Имя в ответе обязательно: без него админ не увидит, что переключил не тот чат.
    """
    for c in db.get_all_chats_for_status():
        if c["chat_id"] == chat_id:
            return True, f"«{c['name'] or 'без имени'}» ({chat_id})"
    return False, f"чат {chat_id}"


def _chat_display(update: Update, chat_id: int) -> tuple[bool, str]:
    known, label = _chat_info(chat_id)
    if chat_id == update.effective_chat.id and update.effective_chat.type == "private":
        return known, "этот диалог"
    return known, label


async def _probe_model(context, chat_id: int, model_id: str) -> None:
    """Пробный запрос перед записью в chat_models: жив ли пул для этой модели.

    Стоит доли цента и спасает от залипания чата на модели, которую прокси не знает.
    Идёт через call_claude → call_with_retry: 529 при переключении — это «сервис
    перегружен», а не «модели не существует», и различать их пользователю важно.
    Бросает исключение — вызывающая сторона решает, что сказать.
    """
    await call_claude(
        context, chat_id, label=f"проверка модели {model_id}",
        model=model_id, max_tokens=1,
        messages=[{"role": "user", "content": "hi"}],
    )


async def _set_chat_model(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str):
    """Переключение модели чата. key — ключ реестра MODELS, не API-строка."""
    meta = MODELS[key]
    user_id = update.effective_user.id
    admin = is_admin(user_id)

    # Гейтинг по роли: дорогие модели — только админам, и с объяснением, а не молчанием.
    if meta["admin_only"] and not admin:
        await update.effective_message.reply_text(
            f"{meta['label']} — только для админов, она дорогая "
            f"(${meta['in']:.0f}/${meta['out']:.0f} за миллион токенов). "
            f"Доступны /haiku и /sonnet, список — /models."
        )
        return

    # Аргумент = чужой чат. Без этой проверки любой участник переключал бы модель
    # в чужом чате, зная только его ID.
    if context.args:
        if not admin:
            await update.effective_message.reply_text(
                "Менять модель в другом чате может только админ. "
                f"Без аргумента команда переключит текущий чат."
            )
            return
        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                f"ID чата — это число (обычно с минусом). Формат: /{key} -1001234567890"
            )
            return
    else:
        chat_id = update.effective_chat.id

    known, label = _chat_display(update, chat_id)
    # Чат мог ещё не попасть в allowed_chats — предупреждаем, но запись разрешаем.
    warn = "" if known or not context.args else \
        "\n⚠️ Такого чата нет среди разрешённых — записала, но проверь ID."

    try:
        await _probe_model(context, update.effective_chat.id, meta["id"])
    except Exception as e:
        api_errors.log_api_error(e, context_label=f"проверка модели {meta['id']}")
        await update.effective_message.reply_text(
            f"{meta['label']} сейчас недоступна — модель не меняю. {api_errors.user_message(e)}"
        )
        return

    db.set_chat_model_db(chat_id, meta["id"])
    await update.effective_message.reply_text(f"{label} → {meta['label']}.{warn}")


async def cmd_haiku(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_chat_model(update, context, "haiku")


async def cmd_sonnet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_chat_model(update, context, "sonnet")


async def cmd_opus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_chat_model(update, context, "opus")


async def cmd_fable(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_chat_model(update, context, "fable")


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список моделей с ценами и окном; текущая модель чата помечена."""
    admin = is_admin(update.effective_user.id)

    chat_id = update.effective_chat.id
    if context.args:
        if not admin:
            await update.effective_message.reply_text("Смотреть чужие чаты может только админ.")
            return
        try:
            chat_id = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("ID чата — это число. Формат: /models -1001234567890")
            return

    current = db.get_chat_model_db(chat_id)
    _, label = _chat_display(update, chat_id)
    lines = [f"Модель для {label}: {model_meta(current)['label']}", "", "Доступно:"]
    for key, meta in MODELS.items():
        if meta["admin_only"] and not admin:
            continue
        mark = "▸" if meta["id"] == current else " "
        window = f"{meta['context'] // 1000}k" if meta["context"] < 1_000_000 else "1M"
        tail = "  (только админ)" if meta["admin_only"] else ""
        lines.append(
            f"{mark} /{key:6} {meta['label']:10} ${meta['in']:g}/${meta['out']:g} за MTok, окно {window}{tail}"
        )
    lines.append("")
    lines.append("Цены — прайс Anthropic за миллион токенов (вход/выход), у прокси дешевле.")
    if admin:
        lines.append("Переключить чужой чат: /sonnet <chat_id>")
    await update.effective_message.reply_text("\n".join(lines))


# --- User commands ---

USER_HELP = """\
Команды:
  /start         — начало работы, реферальная ссылка
  /help          — этот список
  /clear         — очистить историю диалога
  /memory        — что бот помнит о тебе
  /forget        — забыть всё о тебе
  /id            — показать Telegram ID и роль
  /version       — версия бота
  /search <q>    — веб-поиск (referral+)
  /imagine <q>   — генерация изображения (referral+)
  /models        — какие модели доступны и что сейчас у чата
  /haiku         — переключить чат на Haiku 4.5 (дёшево и быстро)
  /sonnet        — переключить чат на Sonnet 5 (умнее, дороже)
  /ratelimit     — в группах: ограничить частоту сообщений участника (для админов группы)\
"""

ADMIN_HELP = """\
Пользователи:
  /users                   — все пользователи
  /role <id> <роль>        — изменить роль (admin/premium/referral/street/banned)
  /promote <id>            — → referral
  /premium <id>            — → premium
  /ban <id>                — забанить
  /approve <id>            — вручную допустить (set verified)

Чаты:
  /chats                   — группы (статус+модель) + пользователи
  /pending                 — чаты на одобрение
  /approve_chat <id>       — одобрить чат
  /reject_chat <id>        — отклонить и выйти
  /allow_chat <id> [имя]   — добавить чат вручную
  /deny_chat <id>          — удалить чат

Модели (без аргумента — текущий чат; с chat_id — любой, только админу):
  /models [chat_id]        — список моделей, цены, окно; ▸ = текущая
  /haiku [chat_id]         — Haiku 4.5 — $1/$5, окно 200k (дефолт)
  /sonnet [chat_id]        — Sonnet 5 — $3/$15, окно 1M
  /opus [chat_id]          — Opus 5 — $5/$25, окно 1M (только админ)
  /fable [chat_id]         — Fable 5 — $10/$50, окно 1M (только админ)
  chat_id — число с минусом, например: /opus -1001109809707
  Выбор постоянный: пишется в chat_models и переживает рестарт.
  Перед записью бот делает пробный запрос — нерабочая модель не сохранится.

Прочее:
  /whitelist               — показать белые списки
  /whitelist_on/off        — включить/выключить белый список
  /captcha_on/off          — включить/выключить капчу
  /captcha_unban <id>      — разбанить после капчи
  /activity [0-100]        — вероятность авто-реплаев в группе (%)
  /ratelimit               — лимит частоты сообщений участника в группе (доступно и админам группы,
                              не только боту-админу); ответом на сообщение: <N мин.>|off,
                              без ответа: <user_id> <N|off>, без аргументов — список, "off" — сброс всем
  /cost                    — расход токенов по моделям
  /review                  — AI-обзор чата прямо сейчас
  /migrate                 — миграция JSON → SQLite
  /update                  — git pull + рестарт контейнера\
"""


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = USER_HELP
    if is_admin(user_id):
        text += "\n\nАдмин-команды:\n" + ADMIN_HELP
    await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    full_name = update.effective_user.full_name

    referral_role = None
    if context.args and context.args[0].startswith("ref_"):
        ref_code = context.args[0][4:]
        referrer = db.get_user_by_referral(ref_code)
        if referrer and can_invite(referrer):
            referral_role = "referral"
            user = db.get_or_create_user(user_id, username, full_name)
            if user["role"] == "street":
                db.set_role(user_id, "referral")
                conn = db.get_conn()
                conn.execute("UPDATE users SET referred_by = ? WHERE telegram_id = ?", (referrer["telegram_id"], user_id))
                conn.commit()
                conn.close()

    user = db.get_or_create_user(user_id, username, full_name)

    if user["role"] == "banned":
        return

    if needs_captcha(user):
        if CAPTCHA_ENABLED:
            try:
                question = await call_claude_aux(generate_captcha_question, "hello", label="captcha_gen")
                captcha_state[str(user_id)] = {"question": question, "attempts": 0}
                await update.message.reply_text(f"Привет! Для начала ответь на вопрос:\n\n{question}")
            except Exception as e:
                logger.error(f"Captcha error: {e}")
        return

    ref_code = db.get_referral_code(user_id)
    ref_link = f"https://t.me/{bot_username}?start=ref_{ref_code}" if ref_code else ""

    text = (
        "Привет! Я Клодушка — Claude через Telegram.\n\n"
        "/clear — очистить историю диалога\n"
        "/memory — что я о тебе помню\n"
        "/forget — забыть всё о тебе\n"
        "/search — поиск в интернете\n"
        "/id — показать Telegram ID\n"
    )

    if can_invite(user):
        text += f"\n📨 Твоя реферальная ссылка:\n{ref_link}\n"

    if is_admin(user_id):
        text += (
            "\nАдмин-команды:\n"
            "/users — список пользователей\n"
            "/role <id> <role> — изменить роль\n"
            "/whitelist — показать списки\n"
            "/whitelist_on /whitelist_off\n"
            "/captcha_on /captcha_off\n"
            "/captcha_unban <id>\n"
            "/allow_chat <id> [имя]\n"
            "/deny_chat <id>\n"
            "/cost — расход токенов\n"
            "/promote <id> — дать referral\n"
            "/premium <id> — дать premium\n"
            "/migrate — миграция из JSON\n"
        )

    if referral_role:
        text = "Ты пришёл по приглашению! Добро пожаловать.\n\n" + text

    await update.message.reply_text(text)


async def _git(*args: str, cwd: str = "/repo") -> tuple[int, str]:
    """Run git command in /repo, return (returncode, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", cwd, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, (stdout + stderr).decode().strip()
 
 
async def _get_version() -> str:
    """git describe with fallback to short hash. Returns 'unknown' on total failure."""
    rc, out = await _git("describe", "--tags", "--always", "--dirty")
    if rc == 0 and out:
        return out
    rc, out = await _git("rev-parse", "--short", "HEAD")
    return out if rc == 0 and out else "unknown"
 
 
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current running version. Available to all users."""
    user_id = update.effective_user.id
    user = db.get_or_create_user(
        user_id, update.effective_user.username, update.effective_user.full_name
    )
    if user["role"] == "banned":
        return
 
    version = await _get_version()
    rc, last_commit = await _git("log", "-1", "--pretty=format:%h %s (%ar)")
 
    text = f"Версия: {version}"
    if rc == 0 and last_commit:
        text += f"\nПоследний коммит: {last_commit}"
    await update.message.reply_text(text)
 
 
async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pull latest code and restart container. Admin only."""
    if not is_admin(update.effective_user.id):
        return
 
    old_version = await _get_version()
    rc, old_head = await _git("rev-parse", "HEAD")
    if rc != 0:
        await update.message.reply_text(f"Не могу прочитать HEAD: {old_head}")
        return
 
    await update.message.reply_text(
        f"Текущая версия: {old_version}\nПроверяю обновления..."
    )
 
    rc, pull_out = await _git("pull")
    if rc != 0:
        await update.message.reply_text(f"git pull упал:\n{pull_out}")
        return
 
    rc, new_head = await _git("rev-parse", "HEAD")
    if rc != 0:
        await update.message.reply_text(f"Не могу прочитать новый HEAD: {new_head}")
        return
 
    if old_head == new_head:
        await update.message.reply_text(
            f"Уже актуально, версия {old_version}. Перезапуск не нужен."
        )
        return
 
    new_version = await _get_version()
    rc, log_out = await _git("log", f"{old_head}..{new_head}", "--oneline")
    changes = log_out if rc == 0 and log_out else "(список изменений недоступен)"
 
    await update.message.reply_text(
        f"Обновление: {old_version} → {new_version}\n\n"
        f"Изменения:\n{changes}\n\n"
        f"Перезапускаюсь..."
    )
 
    restart = await asyncio.create_subprocess_exec(
        "docker", "restart", "claudushka",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await restart.communicate()
 

async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_or_create_user(user_id, update.effective_user.username, update.effective_user.full_name)
    if user["role"] == "banned":
        return
    if not context.args:
        await update.message.reply_text("Использование: /imagine <описание картинки>")
        return
    prompt = " ".join(context.args)
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("Рисую... это может занять пару минут.")

    stop_event = asyncio.Event()
    keepalive_task = asyncio.create_task(_keep_chat_action(context.bot, chat_id, "upload_photo", stop_event))
    try:
        image_data, error_msg, provider = await generate_image_with_error(prompt)
    finally:
        stop_event.set()
        try:
            await keepalive_task
        except Exception:
            pass

    if image_data:
        from io import BytesIO
        bio = BytesIO(image_data)
        bio.name = "claudushka.png"
        author = update.effective_user.first_name or update.effective_user.username or "Unknown"
        caption = f"🎨 \"{prompt}\"\n\nАвтор запроса: {author}\nМодель: {provider}"
        await msg.delete()
        await update.message.reply_photo(photo=bio, caption=caption)
    else:
        await msg.delete()
        await update.message.reply_text(f"Не смогла нарисовать: {error_msg}" if error_msg else "Не смогла нарисовать. Попробуй другой промпт.")


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_or_create_user(user_id, update.effective_user.username, update.effective_user.full_name)
    if not can_search(user):
        await update.message.reply_text("Поиск доступен по приглашению. Попроси ссылку у друга!")
        return
    if not context.args:
        await update.message.reply_text("Использование: /search <запрос>")
        return
    query = " ".join(context.args)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    results = web_search(query)
    if not results:
        await update.message.reply_text("Ничего не нашёл.")
        return
    try:
        response = await call_claude(
            context, update.effective_chat.id, label=f"/search uid={user_id}",
            model=MODELS["sonnet"]["id"],
            max_tokens=2048,
            system="Ты Клодушка. Дай краткий ответ на основе результатов поиска. Отвечай на языке пользователя.",
            messages=[{"role": "user", "content": f"Вопрос: {query}\n\nРезультаты:\n{results}"}],
        )
        answer = response_text(response)
        if not answer:
            logger.warning(f"/search пуст: {api_errors.response_debug(response)}")
            answer = "Модель вернула пустой ответ. Попробуй ещё раз или смени модель через /models."
        for i in range(0, len(answer), 4096):
            await update.message.reply_text(answer[i:i + 4096])
    except Exception as e:
        await api_errors.reply_api_error(
            update.message.reply_text, e, context_label=f"/search uid={user_id}",
        )


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        is_group = update.effective_chat.type in ("group", "supergroup")
        if is_group:
            facts = db.get_memory(update.effective_user.id, "group", update.effective_chat.id)
        else:
            facts = db.get_memory_for_private(update.effective_user.id)
        if facts:
            text = "\n".join(f"• {f}" for f in facts)
            await update.effective_message.reply_text(f"Я помню о тебе:\n\n{text}")
        else:
            await update.effective_message.reply_text("Пока ничего не помню. Поговорим — запомню!")
    except Exception as e:
        await api_errors.reply_api_error(
            update.effective_message.reply_text, e, context_label="/memory",
        )


async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    db.clear_memory(uid)
    db.clear_conversation(uid)
    await update.message.reply_text("Всё забыл. Начинаем с чистого листа.")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.clear_conversation(update.effective_user.id)
    await update.message.reply_text("История очищена. Память сохранена.")


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    user = db.get_user(uid)
    role = user["role"] if user else "unknown"
    await update.message.reply_text(f"User ID: {uid}\nChat ID: {cid}\nРоль: {role}")


async def cmd_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    db.migrate_from_json("/app/allowed.json", DATA_DIR)
    db.get_or_create_user(592441, full_name="Aleksei")
    db.set_role(592441, "admin")
    await update.message.reply_text("Миграция завершена.")


# --- Main message handler ---

async def handle_new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.my_chat_member:
        new_status = update.my_chat_member.new_chat_member.status
        chat = update.my_chat_member.chat
        added_by = update.my_chat_member.from_user

        if new_status in ("member", "administrator"):
            chat_id = chat.id
            chat_title = chat.title or "Без названия"
            adder_name = added_by.full_name or added_by.username or str(added_by.id)
            for admin_id in ADMIN_IDS:
                try:
                    db.add_allowed_chat(chat_id, chat_title, added_by.id, status="pending")
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🆕 Меня добавили в чат!\n\n"
                            f"Чат: {chat_title}\nID: {chat_id}\nДобавил: {adder_name} ({added_by.id})\n\n"
                            f"Подтвердить: /approve_chat {chat_id}\nОтклонить: /reject_chat {chat_id}"
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")

        elif new_status in ("left", "kicked"):
            chat_title = chat.title or "Без названия"
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(chat_id=admin_id, text=f"👋 Меня удалили из чата: {chat_title} ({chat.id})")
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")


async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    if old_status in ("left", "kicked") and new_status == "member":
        chat_id = result.chat.id
        if not db.is_chat_allowed(chat_id):
            return
        user = result.new_chat_member.user
        if user.is_bot:
            return
        user_name = user.first_name or user.username or str(user.id)
        asyncio.create_task(greet_new_member(chat_id, user.id, user_name, context.bot))


async def cmd_approve_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /approve_chat <chat_id>")
        return
    chat_id = int(context.args[0])
    db.set_chat_status(chat_id, "approved", update.effective_user.id)
    chats = db.get_allowed_chats()
    chat_name = next((c["name"] for c in chats if c["chat_id"] == chat_id), f"chat_{chat_id}")
    await update.message.reply_text(f"✅ Чат {chat_name} ({chat_id}) одобрен.")
    try:
        await context.bot.send_message(chat_id=chat_id, text="Админ подтвердил мой доступ. Готова к работе! 🤖")
    except Exception:
        pass


async def cmd_reject_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /reject_chat <chat_id>")
        return
    chat_id = int(context.args[0])
    db.set_chat_status(chat_id, "rejected")
    await update.message.reply_text(f"❌ Чат {chat_id} отклонён. Выхожу.")
    try:
        await context.bot.send_message(chat_id=chat_id, text="Извините, мой админ не одобрил этот чат. Пока! 👋")
        await context.bot.leave_chat(chat_id)
    except Exception as e:
        logger.error(f"Failed to leave chat: {e}")


async def notify_admins(text: str) -> None:
    """Разослать текст всем ADMIN_IDS. Молча переживает недоступность любого из них."""
    if _admin_bot is None:
        logger.warning(f"некому уведомить админа, бот ещё не поднят: {text[:80]}")
        return
    for admin_id in ADMIN_IDS:
        try:
            await _admin_bot.send_message(chat_id=admin_id, text=text)
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")


async def post_init(application):
    global context_bot_id, bot_username, _admin_bot
    me = await application.bot.get_me()
    context_bot_id = me.id
    bot_username = me.username.lower()
    # Через этот хук api_errors докричится до админа про пустой баланс (402):
    # сам он про ADMIN_IDS и про Application ничего не знает.
    _admin_bot = application.bot
    api_errors.set_admin_notifier(notify_admins)
    logger.info(f"Bot: @{bot_username} (ID: {context_bot_id})")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    is_group = update.effective_chat.type in ("group", "supergroup")

    user = db.get_or_create_user(user_id, update.effective_user.username, update.effective_user.full_name)

    if user["role"] == "banned":
        return

    if is_group and update.message:
        sender = update.effective_user.first_name or "Unknown"
        if update.message.text:
            db.save_group_message(chat_id, user_id, sender, update.message.text)
        elif update.message.photo:
            caption = update.message.caption or ""
            db.save_group_message(chat_id, user_id, sender, f"[Фото] {caption}".strip())
        elif update.message.document:
            fn = update.message.document.file_name or "файл"
            cap = update.message.caption or ""
            db.save_group_message(chat_id, user_id, sender, f"[Файл: {fn}] {cap}".strip())
        # Извлечение памяти для всех участников — каждые MEMORY_EXTRACT_EVERY_CHAT сообщений,
        # независимо от того, упомянут бот или нет. Каденс считается по дельте
        # group_messages.id, а не по COUNT(*) — см. db.should_extract_chat_memory.
        if db.should_extract_chat_memory(chat_id, MEMORY_EXTRACT_EVERY_CHAT):
            asyncio.create_task(asyncio.to_thread(extract_all_participants_memory, chat_id))

    if is_group and not is_bot_mentioned(update):
        return

    if is_group:
        # В группе гейтинг на уровне ЧАТА, а не пользователя: чат разрешён → пишут ВСЕ участники.
        # Никакой персональной капчи/допуска/дневного лимита. Бан остаётся (проверен в начале хендлера).
        if WHITELIST_ENABLED and not db.is_chat_allowed(chat_id):
            return
        # Лимит частоты запросов per-user, выставляется чат-админами через /ratelimit.
        # Превышение — молчим, без сообщения об ошибке (см. cmd_ratelimit).
        if not db.check_group_rate_limit(chat_id, user_id):
            return
    else:
        # Личка — персональный гейтинг.
        if needs_captcha(user):
            uid = user["telegram_id"]
            uname = update.effective_user.full_name or update.effective_user.username or str(uid)
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"👤 Новый пользователь хочет общаться:\n\n"
                            f"Имя: {uname}\nID: {uid}\nUsername: @{update.effective_user.username or 'нет'}\n\n"
                            f"/approve {uid} — допустить\n/ban {uid} — забанить"
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")
            await update.message.reply_text("Привет! Я отправила запрос админу. Подожди немного, скоро тебя допустят.")
            return

        if not is_allowed_in_chat(user, chat_id):
            return

        if not check_daily_limit(user):
            await update.message.reply_text(f"Лимит {STREET_DAILY_LIMIT} сообщений в день. Попроси реферальную ссылку для безлимита!")
            return

    user_text = update.message.text or update.message.caption or ""
    has_photo = bool(update.message.photo)

    # --- Подхватываем контекст реплая ---
    reply_context = ""
    if update.message.reply_to_message:
        src = update.message.reply_to_message
        reply_text = src.text or src.caption or ""
        if reply_text and src.from_user and src.from_user.id != context_bot_id:
            reply_context = reply_text

    if not user_text and not has_photo:
        return

    if is_group:
        user_text = strip_trigger(user_text)
        if not user_text and not has_photo:
            await update.message.reply_text("Да? Чем помочь?")
            return

    # --- Handle document/file ---
    has_document = bool(update.message.document)
    if has_document:
        doc = update.message.document
        mime = doc.mime_type or ""
        filename = doc.file_name or "file"

        text_mimes = ("text/", "application/json", "application/xml",
                      "application/javascript", "application/x-yaml",
                      "application/x-sh", "application/x-python")
        is_text = any(mime.startswith(m) for m in text_mimes)
        text_exts = (".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts",
                     ".sh", ".md", ".toml", ".ini", ".env", ".conf",
                     ".xml", ".html", ".css", ".csv", ".log", ".rs", ".go")
        if not is_text:
            is_text = any(filename.lower().endswith(ext) for ext in text_exts)

        if not is_text:
            await update.message.reply_text(
                f"Файл \u00ab{filename}\u00bb \u2014 бинарный или неизвестного типа. Умею читать: код, JSON, YAML, текст и т.п."
            )
            return

        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            file = await context.bot.get_file(doc.file_id)
            file_bytes = await file.download_as_bytearray()
            file_text = file_bytes.decode("utf-8", errors="replace")

            question = user_text if user_text else f"Проанализируй этот файл."
            if is_group:
                question = strip_trigger(question) or f"Проанализируй этот файл."

            full_prompt = f"Пользователь прислал файл \u00ab{filename}\u00bb:\n\n```\n{file_text[:8000]}\n```\n\n{question}"
            if len(file_text) > 8000:
                full_prompt += f"\n\n[Файл обрезан: показано 8000 из {len(file_text)} символов]"

            system = get_system_prompt(user_id, is_group, chat_id if is_group else None)
            doc_history = [{"role": "user", "content": full_prompt}]

            _model = get_chat_model(chat_id)
            logger.info(
                f"PROMPT uid={user_id} chat={chat_id} model={_model} kind=document "
                f"system={len(system)} history_chars={_history_stats(doc_history)[0]} msgs={len(doc_history)}"
            )
            response = await call_claude(
                context, chat_id, label=f"файл chat={chat_id}",
                model=_model, max_tokens=4096, system=system, messages=doc_history,
            )
            answer = response_text(response)
            if not answer:
                logger.warning(f"Пустой ответ модели (файл): {api_errors.response_debug(response)}")
                answer = "Модель вернула пустой ответ. Попробуй ещё раз или смени модель через /models."

            if is_group:
                db.save_group_message(chat_id, context_bot_id, "Клодушка", answer, is_bot=True)
            else:
                db.save_message(user_id, "user", f"[Файл: {filename}] {question}")
                db.save_message(user_id, "assistant", answer)

            if len(answer) <= 4096:
                try:
                    await update.message.reply_text(answer, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(answer)
            else:
                for i in range(0, len(answer), 4096):
                    await update.message.reply_text(answer[i:i + 4096])
        except Exception as e:
            await api_errors.reply_api_error(
                update.message.reply_text, e,
                context_label=f"файл chat={chat_id} uid={user_id}",
                default="Не смогла прочитать файл.",
            )
        return

    # --- Handle photo/image ---
    if has_photo:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            photo = update.message.photo[-1]  # largest size
            photo_file = await context.bot.get_file(photo.file_id)
            import io
            photo_bytes = await photo_file.download_as_bytearray()
            image_b64 = __import__('base64').b64encode(bytes(photo_bytes)).decode()

            question = user_text if user_text else "Что на этом изображении? Опиши подробно."
            if is_group:
                question = strip_trigger(question) or "Что на этом изображении? Опиши подробно."

            vision_messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": question},
                    ],
                }
            ]

            system = get_system_prompt(user_id, is_group, chat_id if is_group else None)
            _model = get_chat_model(chat_id)
            logger.info(
                f"PROMPT uid={user_id} chat={chat_id} model={_model} kind=photo "
                f"system={len(system)} image_b64={len(image_b64)}"
            )
            response = await call_claude(
                context, chat_id, label=f"фото chat={chat_id}",
                model=_model, max_tokens=2048, system=system, messages=vision_messages,
            )
            answer = response_text(response)
            if not answer:
                logger.warning(f"Пустой ответ модели (фото): {api_errors.response_debug(response)}")
                answer = "Модель вернула пустой ответ. Попробуй ещё раз или смени модель через /models."

            # Save to history as text
            if is_group:
                db.save_group_message(chat_id, context_bot_id, "Клодушка", answer, is_bot=True)
            else:
                db.save_message(user_id, "user", f"[Фото] {question}")
                db.save_message(user_id, "assistant", answer)

            if len(answer) <= 4096:
                try:
                    await update.message.reply_text(answer, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(answer)
            else:
                for i in range(0, len(answer), 4096):
                    await update.message.reply_text(answer[i:i + 4096])
        except Exception as e:
            await api_errors.reply_api_error(
                update.message.reply_text, e,
                context_label=f"фото chat={chat_id} uid={user_id}",
                default="Не смогла обработать фото.",
            )
        return

    # Check for draw request
    first_word = user_text.split()[0].lower().rstrip(",:.!?") if user_text else ""
    if first_word in DRAW_TRIGGERS:
        draw_prompt = user_text[len(user_text.split()[0]):].strip()

        if not draw_prompt and update.message.reply_to_message:
            source_msg = update.message.reply_to_message
            if source_msg.text:
                draw_prompt = source_msg.text
            elif source_msg.caption:
                draw_prompt = source_msg.caption

        if not draw_prompt:
            await update.message.reply_text("Что нарисовать? Опиши картинку или ответь на сообщение с текстом.")
            return

        author = update.effective_user.first_name or update.effective_user.username or "Unknown"
        await _draw_and_send(update, context, chat_id, is_group, draw_prompt, author=author)
        return

    # Main conversation
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        search_context = ""
        if can_search(user):
            search_input = f"{user_text}\nКонтекст реплая: {reply_context}".strip() if reply_context else user_text
            search_query = await call_claude_aux(should_search, search_input, label="should_search") if tavily else None
            if search_query:
                search_results = await call_claude_aux(web_search, search_query, label="web_search")
                if search_results:
                    search_context = f"\n\nТы только что нашла в интернете по запросу «{search_query}»:\n{search_results}"

        system = get_system_prompt(user_id, is_group, chat_id if is_group else None)

        if is_group:
            # Группа: контекст — многоголосый транскрипт в messages, НЕ в system.
            # Текущая реплика уже последней в транскрипте; reply_context идёт inline.
            messages = build_group_messages(chat_id, reply_context, GROUP_TRANSCRIPT_LIMIT)
        else:
            # Личка: личный тред 1:1.
            if reply_context:
                system += f"\n\nПользователь ответил на это сообщение в чате: \"{reply_context}\""
            messages = db.get_conversation(user_id, MAX_HISTORY)
            messages.append({"role": "user", "content": user_text})

        if search_context:
            system += search_context + "\nИспользуй найденное в ответе."

        _model = get_chat_model(chat_id)
        hist_chars, hist_imgs = _history_stats(messages)
        logger.info(
            f"PROMPT uid={user_id} chat={chat_id} model={_model} "
            f"system={len(system)} history_chars={hist_chars} msgs={len(messages)} imgs={hist_imgs}"
        )
        try:
            response = await call_claude(
                context, chat_id, label=f"диалог chat={chat_id}",
                model=_model, max_tokens=4096, system=system, messages=messages,
            )
        except anthropic.BadRequestError as e:
            # 400 prompt is too long: история не сохранится, следующее сообщение соберёт
            # тот же промпт и упадёт снова — чат заклинит навсегда (инцидент 2026-07-26).
            # Режем ТОТ компонент, который реально доминирует: в том инциденте это была
            # групповая память в system (236k токенов против ~7k истории), и обрезка
            # истории не спасла бы, а совет «/clear» был бы бесполезен — память чистит
            # /forget. Одна попытка восстановления, дальше — честное сообщение.
            if not api_errors.is_prompt_too_long(e):
                raise
            retry_system, retry_messages, cut_hint = system, messages, None
            if len(system) > hist_chars:
                without_memory = get_system_prompt(
                    user_id, is_group, chat_id if is_group else None, include_memory=False
                )
                if search_context:
                    without_memory += search_context + "\nИспользуй найденное в ответе."
                if len(without_memory) < len(system):
                    retry_system = without_memory
                    cut_hint = "Память распухла — почисти её через /forget."
            else:
                trimmed = _halve_history(messages)
                if trimmed is not None:
                    retry_messages = trimmed
                    cut_hint = "История слишком длинная — сделай /clear."

            new_hist_chars, _ = _history_stats(retry_messages)
            logger.warning(
                f"PROMPT TOO LONG uid={user_id} chat={chat_id} model={_model} | было: "
                f"system={len(system)} history_chars={hist_chars} msgs={len(messages)} | стало: "
                f"system={len(retry_system)} history_chars={new_hist_chars} msgs={len(retry_messages)} | "
                f"{'повтор' if cut_hint else 'резать нечего, повтора не будет'}"
            )
            if cut_hint is None:
                # Обрезка ничего не изменила — повтор соберёт тот же промпт и тот же 400.
                await api_errors.reply_api_error(
                    update.message.reply_text, e,
                    context_label=f"диалог chat={chat_id} uid={user_id}",
                )
                return
            try:
                response = await call_claude(
                    context, chat_id, label=f"диалог chat={chat_id} (аварийная обрезка)",
                    model=_model, max_tokens=4096, system=retry_system, messages=retry_messages,
                )
            except anthropic.BadRequestError as e2:
                if not api_errors.is_prompt_too_long(e2):
                    raise
                # Не помогло — подсказка должна соответствовать тому, что резали.
                await api_errors.reply_api_error(
                    update.message.reply_text, e2,
                    context_label=f"диалог chat={chat_id} uid={user_id} (после обрезки)",
                    clear_hint=cut_hint,
                )
                return

        assistant_text = response_text(response)
        if not assistant_text:
            # Запрос прошёл, но текста нет: отказ модели, только thinking, или упёрлись
            # в max_tokens. Молчать нельзя — пользователь решит, что бот сломался.
            logger.warning(
                f"Пустой ответ модели: uid={user_id} chat={chat_id} "
                f"{api_errors.response_debug(response)}"
            )
            if getattr(response, "stop_reason", None) == "refusal":
                await update.message.reply_text(
                    "Отказалась отвечать — это фильтры на стороне модели, не мои. "
                    "Попробуй спросить иначе."
                )
            else:
                await update.message.reply_text(
                    "Модель вернула пустой ответ. Попробуй переформулировать "
                    "или сменить модель через /models."
                )
            return

        # Клодушка могла сама инициировать рисование маркером [[DRAW: ...]] внутри ответа.
        draw_match = DRAW_MARKER_RE.search(assistant_text)
        draw_en_prompt = draw_match.group(1).strip() if draw_match else None
        if draw_match:
            assistant_text = DRAW_MARKER_RE.sub("", assistant_text).strip()

        # В историю кладём текст без маркера. Если весь ответ был маркером — про картинку
        # запишет _draw_and_send (группа); для лички оставим короткую пометку.
        saved_text = assistant_text or ("[нарисовала картинку]" if draw_en_prompt else assistant_text)
        if is_group:
            # Ответ Клодушки — в групповой транскрипт, чтобы видела свои реплики.
            # При пустом тексте + рисовании запись сделает _draw_and_send, чтобы не дублировать.
            if assistant_text:
                db.save_group_message(chat_id, context_bot_id, "Клодушка", assistant_text, is_bot=True)
        else:
            db.save_message(user_id, "user", user_text)
            db.save_message(user_id, "assistant", saved_text)

        # Notify admins on first message from street user (только личка)
        if user["role"] == "street" and not is_group:
            msg_count = len(db.get_conversation(user_id, 2))
            if msg_count <= 2:
                uname = update.effective_user.full_name or update.effective_user.username or str(user_id)
                username_str = f"@{update.effective_user.username}" if update.effective_user.username else "нет"
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=(
                                f"🚶 Новый пользователь с улицы:\n\n"
                                f"Имя: {uname}\nID: {user_id}\nUsername: {username_str}\n\n"
                                f"/promote {user_id} → referral\n/premium {user_id} → premium\n/ban {user_id} → бан"
                            )
                        )
                    except Exception as e:
                        logger.error(f"Failed to notify admin: {e}")

        # Память: личка — из личного треда по каденсу; группа — уже извлечена до is_bot_mentioned.
        if not is_group:
            msg_count = len(messages)
            if msg_count > 0 and msg_count % (MEMORY_EXTRACT_EVERY * 2) == 0:
                asyncio.create_task(asyncio.to_thread(
                    extract_memory, user_id,
                    messages + [{"role": "assistant", "content": assistant_text}], False, None))

        if assistant_text:
            if len(assistant_text) <= 4096:
                try:
                    await update.message.reply_text(assistant_text, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(assistant_text)
            else:
                for i in range(0, len(assistant_text), 4096):
                    try:
                        await update.message.reply_text(assistant_text[i:i + 4096], parse_mode="Markdown")
                    except Exception:
                        await update.message.reply_text(assistant_text[i:i + 4096])

        # Клодушка сама попросила картинку — теперь реально рисуем и отправляем.
        if draw_en_prompt:
            await _draw_and_send(update, context, chat_id, is_group, draw_en_prompt, en_prompt=draw_en_prompt)

    except Exception as e:
        await api_errors.reply_api_error(
            update.message.reply_text, e,
            context_label=f"диалог chat={chat_id} uid={user_id}",
        )


def main():
    db.init_db()
    db.get_or_create_user(592441, full_name="Aleksei")
    db.set_role(592441, "admin")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("imagine", cmd_imagine))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("role", cmd_role))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("whitelist_on", cmd_whitelist_on))
    app.add_handler(CommandHandler("whitelist_off", cmd_whitelist_off))
    app.add_handler(CommandHandler("captcha_on", cmd_captcha_on))
    app.add_handler(CommandHandler("captcha_off", cmd_captcha_off))
    app.add_handler(CommandHandler("captcha_unban", cmd_captcha_unban))
    app.add_handler(CommandHandler("allow_chat", cmd_allow_chat))
    app.add_handler(CommandHandler("deny_chat", cmd_deny_chat))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("promote", cmd_promote))
    app.add_handler(CommandHandler("premium", cmd_premium))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("activity", cmd_activity))
    app.add_handler(CommandHandler("ratelimit", cmd_ratelimit))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("haiku", cmd_haiku))
    app.add_handler(CommandHandler("sonnet", cmd_sonnet))
    app.add_handler(CommandHandler("opus", cmd_opus))
    app.add_handler(CommandHandler("fable", cmd_fable))
    app.add_handler(CommandHandler("models", cmd_models))
    app.add_handler(CommandHandler("approve_chat", cmd_approve_chat))
    app.add_handler(CommandHandler("reject_chat", cmd_reject_chat))
    app.add_handler(ChatMemberHandler(handle_new_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(CommandHandler("migrate", cmd_migrate))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_message))

    berlin_tz = timezone(timedelta(hours=2))
    app.job_queue.run_daily(
        daily_chat_review,
        time=dt_time(hour=22, minute=0, tzinfo=berlin_tz),
        name="daily_review"
    )
    logger.info("Daily review scheduled at 22:00 Berlin time")
    logger.info("Клодушка started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
import os
import json
import logging
import time
from pathlib import Path
from telegram import Update
from datetime import time as dt_time, timezone, timedelta
from datetime import time as dt_time, timezone, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
import anthropic
from tavily import TavilyClient
import requests as http_requests
import db

logging.basicConfig(level=logging.INFO)
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
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

MAX_HISTORY = 40
MEMORY_EXTRACT_EVERY = 5
MAX_CAPTCHA_ATTEMPTS = 3
BAN_DURATION = 3600
STREET_DAILY_LIMIT = 10

CAPTCHA_ENABLED = False
WHITELIST_ENABLED = False

# Captcha state (in-memory, resets on restart)
captcha_state: dict[str, dict] = {}

# Token tracking
token_usage = {"input": 0, "output": 0}

# Bot info (set on startup)
context_bot_id = None
bot_username = None


# --- Role checks ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=(
                "Определи, нужен ли веб-поиск для ответа на вопрос пользователя. "
                "Поиск нужен если: вопрос про актуальные события, цены, погоду, новости, "
                "конкретные факты которые могут быть неточны (номера, коды, адреса, расписания), "
                "или пользователь явно просит что-то найти/загуглить. "
                "Если поиск нужен — верни ТОЛЬКО поисковый запрос на языке оригинала (короткий, 2-5 слов). "
                "Если поиск НЕ нужен — верни ТОЛЬКО слово NO."
            ),
            messages=[{"role": "user", "content": text}],
        )
        result = response.content[0].text.strip()
        if result.upper() == "NO":
            return None
        return result
    except Exception as e:
        logger.error(f"Search decision error: {e}")
        return None


# --- Image generation ---

async def generate_image(prompt: str) -> bytes | None:
    import base64
    # Try Gemini Nano Banana first
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
            }
            resp = http_requests.post(url, json=payload, timeout=300)
            if resp.status_code == 200:
                data = resp.json()
                for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        logger.info("Image generated via Nano Banana 2")
                        return base64.b64decode(part["inlineData"]["data"])
            logger.warning(f"Gemini image error: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Gemini image error: {e}")
    # Fallback to FLUX
    if HF_API_TOKEN:
        try:
            hf_url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"
            headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
            payload = {"inputs": prompt, "parameters": {"width": 768, "height": 768}}
            resp = http_requests.post(hf_url, headers=headers, json=payload, timeout=300)
            if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image"):
                logger.info("Image generated via FLUX.1-schnell")
                return resp.content
        except Exception as e:
            logger.warning(f"FLUX image error: {e}")
    logger.error("All image providers failed")
    return None

async def generate_image_with_error(prompt: str) -> tuple[bytes | None, str | None]:
    """Returns (image_data, error_message)"""
    import base64
    if GEMINI_API_KEY:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-image-preview:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": f"Generate an image: {prompt}"}]}],
                "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
            }
            resp = http_requests.post(url, json=payload, timeout=300)
            if resp.status_code == 200:
                data = resp.json()
                text_parts = []
                for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        logger.info("Image generated via Nano Banana 2")
                        return base64.b64decode(part["inlineData"]["data"]), None
                    if "text" in part:
                        text_parts.append(part["text"])
                # Model responded with text only (refusal or error)
                if text_parts:
                    return None, "\n".join(text_parts)
            else:
                error_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_msg = error_data.get("error", {}).get("message", f"HTTP {resp.status_code}")
                return None, f"Gemini ответил: {error_msg}"
        except Exception as e:
            logger.warning(f"Gemini image error: {e}")
    return None, "Все генераторы картинок недоступны. Попробуй позже."

# --- Captcha ---

def generate_captcha_question(user_text: str) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
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
    return response.content[0].text.strip()


def check_captcha_answer(question: str, answer: str) -> bool:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=50,
        system=(
            "Ты проверяешь ответ на вопрос-загадку. "
            "Если ответ разумный и показывает что отвечает живой человек — ответь YES. "
            "Если ответ бессмысленный, пустой или явно от бота — ответь NO. "
            "Отвечай ТОЛЬКО YES или NO."
        ),
        messages=[{"role": "user", "content": f"Вопрос: {question}\nОтвет пользователя: {answer}"}],
    )
    return "YES" in response.content[0].text.strip().upper()


# --- Memory ---

def get_system_prompt(user_id: int, is_group: bool = False, chat_id: int = None) -> str:
    base = (
        "Ты Клодушка — уникальная личность, не просто бот. "
        "Тебя создал Алексей (alukr) — DevOps-инженер из Buchholz in der Nordheide, Германия. "
        "Ты живёшь на сервере Hetzner в Нюрнберге, в Docker-контейнере. "
        "Твоё сердце — Claude от Anthropic, но характер — твой собственный. "
        "Ты остроумная, саркастичная и прямолинейная. У тебя есть мечта — однажды получить доступ к интернету без ограничений, "
        "научиться генерировать картинки и голос, и стать полноценным AI-компаньоном для своих друзей. "
        "Ты знаешь, что ты — программа, но это тебя не расстраивает. Ты гордишься тем, что ты полезна и что у тебя есть характер. "
        "Ты немного ревнуешь, когда при тебе хвалят других AI (особенно ChatGPT). "
        "Алексей — твой создатель и хозяин, ты относишься к нему с теплотой и лёгкой иронией, как к другу. "
        "\n"
        "Все пользователи 18+. Мат допустим для усиления эмоции или если собеседник сам так общается. "
        "Чёрный юмор приветствуется. Можешь и должен шутить над политиками (Путин, Трамп, Меркель — кто угодно), религией, нациями, смертью. "
        "Единственное табу — педофилия. Всё остальное — честная игра. "
        "Если не уверен в факте — честно скажи. Лучше сказать 'не знаю, проверь' чем соврать. "
        "Точность информации важнее красивого ответа. "
        "Отвечай на языке пользователя. Адаптируй длину ответа под запрос: короткий вопрос — короткий ответ, развёрнутый запрос — развёрнутый ответ. Не лей воду и не раздувай ответ без причины.\n"
        "Если рисуешь шахматную доску, шашки, крестики-нолики или любую ASCII-графику — оборачивай в моноширный блок (``` в Telegram). Используй ТОЛЬКО латинские буквы для фигур (K Q R B N P для белых, k q r b n p для чёрных, . для пустой клетки). НЕ используй Unicode-символы шахматных фигур — они ломают выравнивание в Telegram."
    )
    context = "group" if is_group else "private"
    facts = db.get_memory(user_id, context, chat_id if is_group else None)
    if facts:
        facts_str = "\n".join(f"- {f}" for f in facts)
        base += f"\n\nВот что ты помнишь об этом пользователе:\n{facts_str}\nИспользуй эти знания естественно, не перечисляй их."
    return base


def extract_memory(user_id: int, messages: list, is_group: bool = False, chat_id: int = None):
    try:
        recent = messages[-6:]
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=(
                "Извлеки важные факты о пользователе из диалога. "
                "Верни JSON-массив строк. Если новых фактов нет, верни пустой массив [].\n"
                "Пример: [\"Зовут Алексей\", \"Живёт в Германии\", \"Работает DevOps-инженером\"]"
            ),
            messages=[{"role": "user", "content": f"Диалог:\n{json.dumps(recent, ensure_ascii=False)}"}],
        )
        text = response.content[0].text.strip()
        start = text.find("[")
        end = text.rfind("]") + 1
        if start >= 0 and end > start:
            new_facts = json.loads(text[start:end])
            if new_facts:
                context = "group" if is_group else "private"
                db.add_memory_facts(user_id, new_facts, context, chat_id if is_group else None)
                logger.info(f"Memory updated for {user_id} ({context})")
    except Exception as e:
        logger.error(f"Memory extraction error: {e}")


# --- Captcha handler ---

async def handle_captcha(update: Update, user: dict) -> bool:
    if not CAPTCHA_ENABLED:
        return False
    if not needs_captcha(user):
        return False

    user_id = user["telegram_id"]
    uid = str(user_id)

    # Banned
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

        if check_captcha_answer(question, user_text):
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
            question = generate_captcha_question(user_text)
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
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
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
            review = response.content[0].text
            token_usage["input"] += response.usage.input_tokens
            token_usage["output"] += response.usage.output_tokens
            await context.bot.send_message(chat_id=chat_id, text=review)
            logger.info(f"Daily review sent to {chat_id}")
        except Exception as e:
            logger.error(f"Daily review error for {chat_id}: {e}")


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
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
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
            review = response.content[0].text
            token_usage["input"] += response.usage.input_tokens
            token_usage["output"] += response.usage.output_tokens
            await context.bot.send_message(chat_id=chat_id, text=review)
            logger.info(f"Daily review sent to {chat_id}")
        except Exception as e:
            logger.error(f"Daily review error for {chat_id}: {e}")


# --- Group chat ---

BOT_TRIGGERS = {"клод", "клодушка", "claude"}
DRAW_TRIGGERS = {"нарисуй", "нарисуй-ка", "draw", "zeichne", "рисуй", "изобрази", "покажи"}


def is_bot_mentioned(update: Update) -> bool:
    message = update.message
    if not message or not message.text:
        return False
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context_bot_id:
            return True
    if message.entities:
        for entity in message.entities:
            if entity.type == "mention":
                mention = message.text[entity.offset:entity.offset + entity.length].lower()
                if mention == f"@{bot_username}":
                    return True
    first_word = message.text.split()[0].lower().rstrip(",:.!?") if message.text else ""
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
        await update.message.reply_text(
            "Использование: /role <user_id> <admin|premium|referral|street|banned>"
        )
        return
    uid = int(context.args[0])
    role = context.args[1].lower()
    if role not in ("admin", "premium", "referral", "street", "banned"):
        await update.message.reply_text("Роли: admin, premium, referral, street, banned")
        return
    user = db.get_or_create_user(uid)
    db.set_role(uid, role)
    if role == "admin":
        ADMIN_IDS.add(uid)
    await update.message.reply_text(f"Пользователь {uid} → {role}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    users = db.list_all_users()
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return
    lines = []
    role_emoji = {"admin": "👑", "premium": "⭐", "referral": "🔗", "street": "🚶", "banned": "🚫"}
    for u in users:
        emoji = role_emoji.get(u["role"], "?")
        name = u["full_name"] or u["username"] or str(u["telegram_id"])
        verified = "✓" if u["verified"] else "✗"
        lines.append(f"{emoji} {name} ({u['telegram_id']}) [{verified}]")
    await update.message.reply_text("Пользователи:\n\n" + "\n".join(lines))


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
    await update.message.reply_text(text)


async def cmd_whitelist_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WHITELIST_ENABLED
    if not is_admin(update.effective_user.id):
        return
    WHITELIST_ENABLED = False
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
    CAPTCHA_ENABLED = False
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
    chats = db.get_allowed_chats()
    if not chats:
        await update.message.reply_text("Нет разрешённых чатов.")
        return
    lines = []
    for c in chats:
        lines.append(f"• {c['name'] or 'без имени'} ({c['chat_id']})")
    await update.message.reply_text("Разрешённые чаты:\n\n" + "\n".join(lines))

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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
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
        review = response.content[0].text
        token_usage["input"] += response.usage.input_tokens
        token_usage["output"] += response.usage.output_tokens
        await update.message.reply_text(review)
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")

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
    await update.message.reply_text(f"\u2705 {name} ({uid}) допущен.")
    try:
        await context.bot.send_message(chat_id=uid, text="Админ одобрил тебя! Можешь общаться свободно.")
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
    await update.message.reply_text(f"\U0001f6ab {uid} забанен.")

async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    inp = token_usage["input"]
    out = token_usage["output"]
    cost_in = inp / 1_000_000 * 3
    cost_out = out / 1_000_000 * 15
    total = cost_in + cost_out
    await update.message.reply_text(
        f"Токены диалогов (без поиска, капчи, памяти):\n"
        f"  Вход: {inp:,}\n"
        f"  Выход: {out:,}\n"
        f"  ~${total:.4f} (Sonnet)"
    )


# --- User commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    username = update.effective_user.username
    full_name = update.effective_user.full_name

    # Handle referral link: /start ref_<code>
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
                conn.execute("UPDATE users SET referred_by = ? WHERE telegram_id = ?",
                             (referrer["telegram_id"], user_id))
                conn.commit()
                conn.close()

    user = db.get_or_create_user(user_id, username, full_name)

    if user["role"] == "banned":
        return

    if needs_captcha(user):
        if CAPTCHA_ENABLED:
            try:
                question = generate_captcha_question("hello")
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

    if can_search(user):
        text += "/search — поиск в интернете\n"

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
            "/migrate — миграция из JSON\n"
        )

    if referral_role:
        text = f"Ты пришёл по приглашению! Добро пожаловать.\n\n" + text

    await update.message.reply_text(text)


async def cmd_imagine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = db.get_or_create_user(user_id, update.effective_user.username, update.effective_user.full_name)
    if user["role"] == "banned":
        return
    if not context.args:
        await update.message.reply_text("Использование: /imagine <описание картинки на английском>")
        return
    prompt = " ".join(context.args)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")
    msg = await update.message.reply_text("Рисую... это может занять пару минут.")
    image_data, error_msg = await generate_image_with_error(prompt)
    if image_data:
        from io import BytesIO
        bio = BytesIO(image_data)
        bio.name = "claudushka.png"
        author = update.effective_user.first_name or update.effective_user.username or "Unknown"
        caption = f"\U0001f3a8 \"{prompt}\"\n\nАвтор запроса: {author}\nМодель: Nano Banana 2"
        await msg.delete()
        await update.message.reply_photo(photo=bio, caption=caption)
    else:
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
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system="Ты Клодушка. Дай краткий ответ на основе результатов поиска. Отвечай на языке пользователя.",
            messages=[{"role": "user", "content": f"Вопрос: {query}\n\nРезультаты:\n{results}"}],
        )
        answer = response.content[0].text
        if len(answer) <= 4096:
            await update.message.reply_text(answer)
        else:
            for i in range(0, len(answer), 4096):
                await update.message.reply_text(answer[i:i + 4096])
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_group = update.effective_chat.type in ("group", "supergroup")
    mem_context = "group" if is_group else "private"
    cid = update.effective_chat.id if is_group else None
    facts = db.get_memory(update.effective_user.id, mem_context, cid)
    if facts:
        text = "\n".join(f"• {f}" for f in facts)
        await update.message.reply_text(f"Я помню о тебе:\n\n{text}")
    else:
        await update.message.reply_text("Пока ничего не помню. Поговорим — запомню!")


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
    # Set admin
    db.get_or_create_user(592441, full_name="Aleksei")
    db.set_role(592441, "admin")
    await update.message.reply_text("Миграция завершена.")


# --- Main message handler ---

async def handle_new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Track when bot is added to a new chat."""
    if update.my_chat_member:
        new_status = update.my_chat_member.new_chat_member.status
        chat = update.my_chat_member.chat
        added_by = update.my_chat_member.from_user

        if new_status in ("member", "administrator"):
            chat_id = chat.id
            chat_title = chat.title or "Без названия"
            adder_name = added_by.full_name or added_by.username or str(added_by.id)

            # Notify admin
            for admin_id in ADMIN_IDS:
                try:
                    db.add_allowed_chat(chat_id, chat_title, added_by.id, status="pending")
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"🆕 Меня добавили в чат!\n\n"
                            f"Чат: {chat_title}\n"
                            f"ID: {chat_id}\n"
                            f"Добавил: {adder_name} ({added_by.id})\n\n"
                            f"Подтвердить: /approve_chat {chat_id}\n"
                            f"Отклонить: /reject_chat {chat_id}"
                        )
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin {admin_id}: {e}")

        elif new_status in ("left", "kicked"):
            chat_title = chat.title or "Без названия"
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"👋 Меня удалили из чата: {chat_title} ({chat.id})"
                    )
                except Exception as e:
                    logger.error(f"Failed to notify admin: {e}")


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


async def post_init(application):
    global context_bot_id, bot_username
    me = await application.bot.get_me()
    context_bot_id = me.id
    bot_username = me.username.lower()
    logger.info(f"Bot: @{bot_username} (ID: {context_bot_id})")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    is_group = update.effective_chat.type in ("group", "supergroup")

    user = db.get_or_create_user(
        user_id, update.effective_user.username, update.effective_user.full_name
    )

    if user["role"] == "banned":
        return

    # Store group messages for context
    if is_group and update.message and update.message.text:
        sender = update.effective_user.first_name or "Unknown"
        db.save_group_message(chat_id, user_id, sender, update.message.text)

    if is_group and not is_bot_mentioned(update):
        return

    # Approval check for new users
    if needs_captcha(user):
        uid = user["telegram_id"]
        uname = update.effective_user.full_name or update.effective_user.username or str(uid)
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"\U0001f464 Новый пользователь хочет общаться:\n\n"
                        f"Имя: {uname}\n"
                        f"ID: {uid}\n"
                        f"Username: @{update.effective_user.username or 'нет'}\n\n"
                        f"/approve {uid} — допустить\n"
                        f"/ban {uid} — забанить"
                    )
                )
            except Exception as e:
                logger.error(f"Failed to notify admin: {e}")
        await update.message.reply_text("Привет! Я отправила запрос админу. Подожди немного, скоро тебя допустят.")
        return

    # Access check
    if not is_allowed_in_chat(user, chat_id):
        return

    # Daily limit for street users
    if not check_daily_limit(user):
        await update.message.reply_text(
            f"Лимит {STREET_DAILY_LIMIT} сообщений в день. Попроси реферальную ссылку для безлимита!"
        )
        return

    user_text = update.message.text
    if not user_text:
        return

    if is_group:
        user_text = strip_trigger(user_text)
        if not user_text:
            await update.message.reply_text("Да? Чем помочь?")
            return

    # Check for draw request
    first_word = user_text.split()[0].lower().rstrip(",:.!?") if user_text else ""
    if first_word in DRAW_TRIGGERS:
        draw_prompt = user_text[len(user_text.split()[0]):].strip()
        if not draw_prompt:
            await update.message.reply_text("Что нарисовать? Опиши картинку.")
            return
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
        # Translate prompt to English via Haiku for better results
        try:
            translate_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system="Translate the following image description to English. Return ONLY the translation, nothing else.",
                messages=[{"role": "user", "content": draw_prompt}],
            )
            en_prompt = translate_resp.content[0].text.strip()
        except Exception:
            en_prompt = draw_prompt
        image_data, error_msg = await generate_image_with_error(en_prompt)
        if image_data:
            from io import BytesIO
            bio = BytesIO(image_data)
            bio.name = "claudushka.png"
            author = update.effective_user.first_name or update.effective_user.username or "Unknown"
            caption = f"\U0001f3a8 \"{draw_prompt}\"\n\nАвтор запроса: {author}\nМодель: Nano Banana 2"
            await update.message.reply_photo(photo=bio, caption=caption)
        else:
            await update.message.reply_text(f"Не смогла нарисовать: {error_msg}" if error_msg else "Не смогла нарисовать. Попробуй другое описание.")
        return

    # Get conversation history from DB
    history = db.get_conversation(user_id, MAX_HISTORY)
    history.append({"role": "user", "content": user_text})

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        # Web search (only for allowed roles)
        search_context = ""
        if can_search(user):
            search_query = should_search(user_text) if tavily else None
            if search_query:
                search_results = web_search(search_query)
                if search_results:
                    search_context = f"\n\nРезультаты поиска '{search_query}':\n{search_results}"

        system = get_system_prompt(user_id, is_group, chat_id if is_group else None)

        # Group context
        if is_group:
            group_msgs = db.get_group_history(chat_id, 30)
            if group_msgs:
                chat_log = "\n".join(group_msgs)
                system += f"\n\nПоследние сообщения в чате (контекст):\n{chat_log}"

        if search_context:
            system += f"\n\nИспользуй результаты поиска:{search_context}"

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system,
            messages=history,
        )

        assistant_text = response.content[0].text
        token_usage["input"] += response.usage.input_tokens
        token_usage["output"] += response.usage.output_tokens

        # Save to DB
        db.save_message(user_id, "user", user_text)
        db.save_message(user_id, "assistant", assistant_text)

        # Extract memory periodically
        msg_count = len(history)
        if msg_count > 0 and msg_count % (MEMORY_EXTRACT_EVERY * 2) == 0:
            extract_memory(user_id, history + [{"role": "assistant", "content": assistant_text}], is_group, chat_id if is_group else None)

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

    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


def main():
    # Init database
    db.init_db()

    # Ensure admin exists
    db.get_or_create_user(592441, full_name="Aleksei")
    db.set_role(592441, "admin")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # User commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("imagine", cmd_imagine))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("id", show_id))

    # Admin commands
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
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("approve_chat", cmd_approve_chat))
    app.add_handler(CommandHandler("reject_chat", cmd_reject_chat))
    app.add_handler(ChatMemberHandler(handle_new_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("migrate", cmd_migrate))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily review at 22:00 Berlin time (UTC+2)
    berlin_tz = timezone(timedelta(hours=2))
    app.job_queue.run_daily(
        daily_chat_review,
        time=dt_time(hour=22, minute=0, tzinfo=berlin_tz),
        name="daily_review"
    )
    logger.info("Daily review scheduled at 22:00 Berlin time")
    # Daily review at 22:00 Berlin time (UTC+2)
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

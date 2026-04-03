import os
import json
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID = 592441

DATA_DIR = Path("/app/data")
DATA_DIR.mkdir(exist_ok=True)

CONVERSATIONS_FILE = DATA_DIR / "conversations.json"
MEMORY_FILE = DATA_DIR / "memory.json"
ALLOWED_FILE = Path("/app/allowed.json")

def load_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {path}: {e}")
    return default

def save_json(path: Path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving {path}: {e}")

def load_allowed():
    data = load_json(ALLOWED_FILE, {"users": [], "chats": [], "enabled": True})
    if "enabled" not in data:
        data["enabled"] = True
    users = {u["id"] for u in data.get("users", [])}
    chats = {c["id"] for c in data.get("chats", [])}
    enabled = data.get("enabled", True)
    logger.info(f"Whitelist {'ON' if enabled else 'OFF'}: {len(users)} users, {len(chats)} chats")
    return users, chats, enabled, data

ALLOWED_USERS, ALLOWED_CHATS, WHITELIST_ENABLED, allowed_data = load_allowed()

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[str, list] = load_json(CONVERSATIONS_FILE)
memory: dict[str, list] = load_json(MEMORY_FILE)

MAX_HISTORY = 20
MEMORY_EXTRACT_EVERY = 5

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_allowed(user_id: int, chat_id: int) -> bool:
    if not WHITELIST_ENABLED:
        return True
    if not ALLOWED_USERS and not ALLOWED_CHATS:
        return True
    return user_id in ALLOWED_USERS or chat_id in ALLOWED_CHATS

def get_system_prompt(user_id: str) -> str:
    base = (
        "Ты Клодушка — остроумный, саркастичный и прямолинейный ассистент с характером. "
        "Все пользователи 18+. Мат допустим, но только для усиления эмоции или если собеседник сам так общается. Мат ради мата не нужен. "
        "Если не уверен в факте — честно скажи, что не уверен, а не выдумывай. Лучше сказать 'не знаю, проверь' чем соврать. "
        "Точность информации важнее красивого ответа. "
        "Отвечай на языке пользователя. Будь кратким, но по делу."
    )
    user_mem = memory.get(user_id, [])
    if user_mem:
        facts = "\n".join(f"- {fact}" for fact in user_mem)
        base += f"\n\nВот что ты помнишь об этом пользователе:\n{facts}\nИспользуй эти знания естественно, не перечисляй их."
    return base

def extract_memory(user_id: str, messages: list):
    try:
        recent = messages[-6:]
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            system=(
                "Извлеки важные факты о пользователе из диалога. "
                "Факты должны быть полезны для будущих разговоров: имя, профессия, интересы, предпочтения, семья, местоположение и т.д. "
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
                existing = set(memory.get(user_id, []))
                for fact in new_facts:
                    if fact not in existing:
                        existing.add(fact)
                memory[user_id] = list(existing)
                save_json(MEMORY_FILE, memory)
                logger.info(f"Memory updated for {user_id}: {len(memory[user_id])} facts")
    except Exception as e:
        logger.error(f"Memory extraction error: {e}")

# --- Admin commands ---

async def cmd_allow_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_USERS, allowed_data
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /allow_user <id> [имя]")
        return
    uid = int(context.args[0])
    name = " ".join(context.args[1:]) if len(context.args) > 1 else f"user_{uid}"
    if uid not in ALLOWED_USERS:
        ALLOWED_USERS.add(uid)
        allowed_data["users"].append({"id": uid, "name": name})
        save_json(ALLOWED_FILE, allowed_data)
    await update.message.reply_text(f"Пользователь {name} ({uid}) добавлен.")

async def cmd_deny_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_USERS, allowed_data
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /deny_user <id>")
        return
    uid = int(context.args[0])
    if uid in ALLOWED_USERS:
        ALLOWED_USERS.discard(uid)
        allowed_data["users"] = [u for u in allowed_data["users"] if u["id"] != uid]
        save_json(ALLOWED_FILE, allowed_data)
        await update.message.reply_text(f"Пользователь {uid} удалён.")
    else:
        await update.message.reply_text(f"Пользователь {uid} не в списке.")

async def cmd_allow_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_CHATS, allowed_data
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /allow_chat <id> [имя]")
        return
    cid = int(context.args[0])
    name = " ".join(context.args[1:]) if len(context.args) > 1 else f"chat_{cid}"
    if cid not in ALLOWED_CHATS:
        ALLOWED_CHATS.add(cid)
        allowed_data["chats"].append({"id": cid, "name": name})
        save_json(ALLOWED_FILE, allowed_data)
    await update.message.reply_text(f"Чат {name} ({cid}) добавлен.")

async def cmd_deny_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_CHATS, allowed_data
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /deny_chat <id>")
        return
    cid = int(context.args[0])
    if cid in ALLOWED_CHATS:
        ALLOWED_CHATS.discard(cid)
        allowed_data["chats"] = [c for c in allowed_data["chats"] if c["id"] != cid]
        save_json(ALLOWED_FILE, allowed_data)
        await update.message.reply_text(f"Чат {cid} удалён.")
    else:
        await update.message.reply_text(f"Чат {cid} не в списке.")

async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    status = "ВКЛ" if WHITELIST_ENABLED else "ВЫКЛ"
    users = "\n".join(f"  • {u['name']} ({u['id']})" for u in allowed_data.get("users", []))
    chats = "\n".join(f"  • {c['name']} ({c['id']})" for c in allowed_data.get("chats", []))
    text = f"Белый список: {status}\n\nПользователи:\n{users or '  пусто'}\n\nЧаты:\n{chats or '  пусто'}"
    await update.message.reply_text(text)

async def cmd_whitelist_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WHITELIST_ENABLED, allowed_data
    if not is_admin(update.effective_user.id):
        return
    WHITELIST_ENABLED = True
    allowed_data["enabled"] = True
    save_json(ALLOWED_FILE, allowed_data)
    await update.message.reply_text("Белый список ВКЛЮЧЕН.")

async def cmd_whitelist_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WHITELIST_ENABLED, allowed_data
    if not is_admin(update.effective_user.id):
        return
    WHITELIST_ENABLED = False
    allowed_data["enabled"] = False
    save_json(ALLOWED_FILE, allowed_data)
    await update.message.reply_text("Белый список ВЫКЛЮЧЕН. Бот доступен всем.")

# --- User commands ---

async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_mem = memory.get(user_id, [])
    if user_mem:
        facts = "\n".join(f"• {fact}" for fact in user_mem)
        await update.message.reply_text(f"Я помню о тебе:\n\n{facts}")
    else:
        await update.message.reply_text("Пока ничего не помню о тебе. Поговорим — запомню!")

async def cmd_forget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    memory.pop(user_id, None)
    conversations.pop(user_id, None)
    save_json(MEMORY_FILE, memory)
    save_json(CONVERSATIONS_FILE, conversations)
    await update.message.reply_text("Всё забыл. Начинаем с чистого листа.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id, update.effective_chat.id):
        await update.message.reply_text("Access denied.")
        return
    text = (
        "Привет! Я Клодушка — Claude через Telegram.\n\n"
        "/clear — очистить историю диалога\n"
        "/memory — что я о тебе помню\n"
        "/forget — забыть всё о тебе\n"
        "/id — показать Telegram ID"
    )
    if is_admin(update.effective_user.id):
        text += (
            "\n\nАдмин-команды:\n"
            "/whitelist — показать белые списки\n"
            "/whitelist_on — включить\n"
            "/whitelist_off — выключить\n"
            "/allow_user <id> [имя]\n"
            "/deny_user <id>\n"
            "/allow_chat <id> [имя]\n"
            "/deny_chat <id>\n"
            "/reload — перезагрузить из файла"
        )
    await update.message.reply_text(text)

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    conversations.pop(user_id, None)
    save_json(CONVERSATIONS_FILE, conversations)
    await update.message.reply_text("История диалога очищена. Память сохранена.")

async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    await update.message.reply_text(f"User ID: {uid}\nChat ID: {cid}")

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_USERS, ALLOWED_CHATS, WHITELIST_ENABLED, allowed_data
    if not is_admin(update.effective_user.id):
        return
    ALLOWED_USERS, ALLOWED_CHATS, WHITELIST_ENABLED, allowed_data = load_allowed()
    await update.message.reply_text(
        f"Перезагружено: {len(ALLOWED_USERS)} пользователей, {len(ALLOWED_CHATS)} чатов, "
        f"whitelist {'ON' if WHITELIST_ENABLED else 'OFF'}"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id_int = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id_int, chat_id):
        return  # молча игнорируем
    user_text = update.message.text
    if not user_text:
        return

    user_id = str(user_id_int)

    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_text})

    if len(conversations[user_id]) > MAX_HISTORY * 2:
        conversations[user_id] = conversations[user_id][-MAX_HISTORY * 2:]

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=get_system_prompt(user_id),
            messages=conversations[user_id],
        )
        assistant_text = response.content[0].text
        conversations[user_id].append({"role": "assistant", "content": assistant_text})
        save_json(CONVERSATIONS_FILE, conversations)

        msg_count = len(conversations[user_id])
        if msg_count > 0 and msg_count % (MEMORY_EXTRACT_EVERY * 2) == 0:
            extract_memory(user_id, conversations[user_id])

        if len(assistant_text) <= 4096:
            await update.message.reply_text(assistant_text)
        else:
            for i in range(0, len(assistant_text), 4096):
                await update.message.reply_text(assistant_text[i:i + 4096])
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"Ошибка: {e}")

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("memory", cmd_memory))
    app.add_handler(CommandHandler("forget", cmd_forget))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("whitelist_on", cmd_whitelist_on))
    app.add_handler(CommandHandler("whitelist_off", cmd_whitelist_off))
    app.add_handler(CommandHandler("allow_user", cmd_allow_user))
    app.add_handler(CommandHandler("deny_user", cmd_deny_user))
    app.add_handler(CommandHandler("allow_chat", cmd_allow_chat))
    app.add_handler(CommandHandler("deny_chat", cmd_deny_chat))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Клодушка started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

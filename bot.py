import os
import json
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

def load_allowed():
    try:
        with open("allowed.json", "r") as f:
            data = json.load(f)
        users = {u["id"] for u in data.get("users", [])}
        chats = {c["id"] for c in data.get("chats", [])}
        logger.info(f"Loaded {len(users)} allowed users, {len(chats)} allowed chats")
        return users, chats
    except FileNotFoundError:
        logger.warning("allowed.json not found, allowing everyone")
        return set(), set()

ALLOWED_USERS, ALLOWED_CHATS = load_allowed()

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
conversations: dict[int, list] = {}
MAX_HISTORY = 20

def is_allowed(user_id: int, chat_id: int) -> bool:
    if not ALLOWED_USERS and not ALLOWED_CHATS:
        return True
    if user_id in ALLOWED_USERS:
        return True
    if chat_id in ALLOWED_CHATS:
        return True
    return False

async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global ALLOWED_USERS, ALLOWED_CHATS
    if update.effective_user.id not in ALLOWED_USERS:
        return
    ALLOWED_USERS, ALLOWED_CHATS = load_allowed()
    await update.message.reply_text(
        f"Перезагружено: {len(ALLOWED_USERS)} пользователей, {len(ALLOWED_CHATS)} чатов"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id, update.effective_chat.id):
        await update.message.reply_text("Access denied.")
        return
    await update.message.reply_text(
        "Привет! Я Клодушка — Claude через Telegram.\n\n"
        "/clear — очистить историю диалога\n"
        "/id — показать твой Telegram ID\n"
        "/reload — перезагрузить белые списки"
    )

async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conversations.pop(user_id, None)
    await update.message.reply_text("История очищена.")

async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cid = update.effective_chat.id
    await update.message.reply_text(f"User ID: {uid}\nChat ID: {cid}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    if not is_allowed(user_id, chat_id):
        await update.message.reply_text("Access denied.")
        return
    user_text = update.message.text
    if not user_text:
        return
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
            system="Ты Клодушка — дружелюбный и полезный ассистент. Отвечай на языке пользователя. Будь кратким, но информативным.",
            messages=conversations[user_id],
        )
        assistant_text = response.content[0].text
        conversations[user_id].append({"role": "assistant", "content": assistant_text})
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
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("reload", cmd_reload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Клодушка started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

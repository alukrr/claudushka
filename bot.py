import os
import logging
import requests
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
ALLOWED_IDS_RAW = os.getenv("ALLOWED_TELEGRAM_USER_IDS", "")

if not TELEGRAM_BOT_TOKEN or not CLAUDE_API_KEY:
    raise RuntimeError("TELEGRAM_BOT_TOKEN and CLAUDE_API_KEY must be set in environment")

ALLOWED_TELEGRAM_USER_IDS = set()
for p in [item.strip() for item in ALLOWED_IDS_RAW.split(",") if item.strip()]:
    try:
        ALLOWED_TELEGRAM_USER_IDS.add(int(p))
    except ValueError:
        raise ValueError(f"Invalid user id in ALLOWED_TELEGRAM_USER_IDS: '{p}'")

HISTORY = defaultdict(list)
MAX_HISTORY_ITEMS = 8

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in ALLOWED_TELEGRAM_USER_IDS


async def auth_guard(update: Update) -> bool:
    if update.effective_user is None:
        return False
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("❌ У вас нет доступа к этому боту. Свяжитесь с администратором.")
        return False
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update):
        return
    await update.message.reply_text(
        "Привет! Я бот на Claude API.\n" "Команды: /start, /clear, /id. Отправьте любое сообщение для общения."
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update):
        return
    user_id = update.effective_user.id
    await update.message.reply_text(f"Ваш Telegram user_id: {user_id}")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update):
        return
    user_id = update.effective_user.id
    HISTORY.pop(user_id, None)
    await update.message.reply_text("✅ История чата очищена.")


def claude_complete_with_history(user_history):
    url = "https://api.anthropic.com/v1/complete"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "model": "claude-3.5",  # или другой доступный модельный тег
        "messages": user_history,
        "temperature": 0.7,
        "max_tokens_to_sample": 1200,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "completion" in data:
        return data["completion"]
    if "output" in data and isinstance(data["output"], dict) and "text" in data["output"]:
        return data["output"]["text"]
    if "choices" in data and data["choices"]:
        return data["choices"][0].get("message", {}).get("content", "")
    raise ValueError(f"Unexpected response from Claude: {data}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_guard(update):
        return

    if update.message is None or update.message.text is None:
        return

    user_id = update.effective_user.id
    user_text = update.message.text.strip()

    if user_text.startswith("/"):
        return

    history = HISTORY[user_id]
    history.append({"role": "user", "content": user_text})
    if len(history) > MAX_HISTORY_ITEMS * 2:
        history = history[-MAX_HISTORY_ITEMS * 2 :]

    try:
        claude_resp = claude_complete_with_history(history)
    except Exception as e:
        logger.exception("Claude API error")
        await update.message.reply_text(f"Ошибка Claude API: {e}")
        return

    history.append({"role": "assistant", "content": claude_resp})
    HISTORY[user_id] = history

    await update.message.reply_text(claude_resp)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()

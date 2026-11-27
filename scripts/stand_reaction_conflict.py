"""Стенд, шаг 7 чек-листа реакций: конфликт спонтанной и рабочей реакции на РЕАЛЬНОМ Telegram. Запуск внутри контейнера стенда:

    docker exec -i claudushka-test python3 - <chat_id> < scripts/stand_reaction_conflict.py

Берёт последнее сообщение человека в чате (из group_messages), от имени бота ставит спонтанную 😁 (запись в message_reactions + реальный
set_message_reaction), затем кодом бота react(👀) → пауза → unreact(): спонтанная 😁 должна вернуться. Печатает порядок вызовов; итог смотрит
`tester run reactions-conflict-after` (a see: 1 → 😁). Только стенд."""
import asyncio
import os
import sys
import time

sys.path.insert(0, "/app")
import bot  # noqa: E402
import db  # noqa: E402
from telegram import Bot  # noqa: E402


async def main(chat_id: int) -> None:
    b = Bot(os.environ["TELEGRAM_BOT_TOKEN"])
    async with b:
        me = await b.get_me()
        bot.context_bot_id = me.id
        bot.REACTIONS = True
        conn = db.get_conn()
        row = conn.execute("SELECT tg_message_id FROM group_messages WHERE chat_id = ? AND is_bot = 0 AND tg_message_id IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        conn.close()
        mid = row["tg_message_id"]
        target = bot._ReactTarget(b, chat_id, mid)
        calls = []
        real = target.set_reaction

        async def logged(emoji):
            calls.append(emoji)
            await real(emoji)
        target.set_reaction = logged
        bot._queue_reaction(target, "😁", on_ok=lambda: db.set_user_reactions(chat_id, mid, me.id, "Клодушка", ["😁"], int(time.time())))
        await asyncio.sleep(3)
        bot.react(target, bot.REACTION_SEARCH)          # рабочая 👀 вытесняет спонтанную
        await asyncio.sleep(3)
        bot.unreact(target)                              # конец работы → вернуть спонтанную
        await asyncio.sleep(3)
        print("порядок set_reaction:", calls)


asyncio.run(main(int(sys.argv[1])))

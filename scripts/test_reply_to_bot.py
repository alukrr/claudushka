"""Юнит-тесты цитаты реплики бота в reply_context (fix/reply-to-bot, docs/claude/group-chat.md).

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_reply_to_bot.py -q
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir


def test_short_quote_kept_whole():
    q = bot._bot_reply_quote("Москва — столица России.")
    assert q == "(это твоё собственное сообщение) Москва — столица России."


def test_long_quote_capped_by_size():
    q = bot._bot_reply_quote("а" * 5000)
    body = q.removeprefix("(это твоё собственное сообщение) ")
    assert len(body) == bot.BOT_REPLY_QUOTE_CHARS + 1   # + «…»
    assert body.endswith("…")


def test_whitespace_collapsed():
    assert bot._bot_reply_quote("раз\n\n  два\tтри").endswith(" раз два три")


def test_no_draw_wording():
    # инцидент 2026-09-08: пометки не должны начинаться с «нарисовала» и т.п. — модель копирует формат
    assert "нарисова" not in bot._bot_reply_quote("текст")


def test_service_block_carries_quote():
    sb = bot.service_block(reply_context=bot._bot_reply_quote("вчерашний ответ"))
    assert "твоё собственное сообщение" in sb and "вчерашний ответ" in sb

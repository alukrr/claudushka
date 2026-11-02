"""Тесты: /help режется под лимит Telegram, потолок Retry-After у SDK-клиента. Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_help_and_retry_cap.py -q
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx2  # noqa: E402

import bot  # noqa: E402

LIMIT = bot.TELEGRAM_MSG_LIMIT


def test_split_lines_short_text_is_one_part():
    assert bot.split_lines_for_telegram("a\nb") == ["a\nb"]


def test_split_lines_respects_limit_and_loses_nothing():
    text = "\n".join(f"/cmd{i} — описание команды номер {i}" for i in range(400))
    parts = bot.split_lines_for_telegram(text)
    assert len(parts) > 1 and all(len(p) <= LIMIT for p in parts)
    assert "\n".join(parts) == text            # границы только по переводам строк, ничего не потеряно и не продублировано


def test_split_lines_overlong_line_is_cut():
    parts = bot.split_lines_for_telegram("x" * (LIMIT * 2 + 5))
    assert [len(p) for p in parts] == [LIMIT, LIMIT, 5]


def test_real_help_fits_telegram_for_admin_and_user():
    full = bot.USER_HELP + "\n\nАдмин-команды:\n" + bot.ADMIN_HELP
    parts = bot.split_lines_for_telegram(full)
    assert all(len(p) <= LIMIT for p in parts) and "\n".join(parts) == full
    assert len(bot.split_lines_for_telegram(bot.USER_HELP)) == 1      # обычному пользователю — одно сообщение


def test_cmd_help_sends_every_part_to_admin(monkeypatch):
    sent = []

    async def reply_text(t):
        sent.append(t)
    monkeypatch.setattr(bot, "is_admin", lambda uid: True)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1), message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_help(upd, None))
    assert len(sent) >= 2 and all(len(t) <= LIMIT for t in sent)


def test_sdk_retry_wait_is_capped_even_if_pool_says_866s():
    opts = SimpleNamespace(get_max_retries=lambda default: 3)
    long_wait = httpx2.Headers({"retry-after": "866"})
    assert bot.client._calculate_retry_timeout(3, opts, long_wait) == bot.SDK_RETRY_WAIT_CAP
    short_wait = httpx2.Headers({"retry-after": "2"})
    assert bot.client._calculate_retry_timeout(3, opts, short_wait) == 2.0      # короткое ожидание пула уважаем

"""Фоновый фолбэк без окна и алерта (FALLBACK_QUIET_LABELS) и файлы больше 20 МБ (TG_FILE_MAX_BYTES) — 2026-11-27, v2.24.3.
Без сети."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

LUNA, FLASH_G = bot.MODELS["luna"]["id"], bot.MODELS["flash_g"]["id"]


def rate_limit():
    resp = httpx.Response(429, request=httpx.Request("POST", "http://pool/v1/messages"),
                          json={"error": {"message": "Rate limit reached"}})
    return anthropic.RateLimitError("Rate limit reached", response=resp, body={"error": {"message": "Rate limit reached"}})


@pytest.fixture(autouse=True)
def alerts(monkeypatch):
    bot._fallback_until.clear()
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", True)
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(bot, "_last_fallback_alert", {})
    sent = []
    monkeypatch.setattr(bot, "notify_admins", lambda text: sent.append(text))
    monkeypatch.setattr(bot, "_fire", lambda x: None)
    yield sent
    bot._fallback_until.clear()


def test_quiet_label_no_window_no_alert(alerts):
    bot._enter_fallback(LUNA, FLASH_G, rate_limit(), "activity_check")
    assert LUNA not in bot._fallback_until and alerts == []
    assert bot._fallback_plan(LUNA)[0] == LUNA          # следующий вызов Luna идёт в пул как обычно


def test_user_facing_label_opens_window_and_alerts(alerts):
    bot._enter_fallback(LUNA, FLASH_G, rate_limit(), "should_search")
    assert LUNA in bot._fallback_until and len(alerts) == 1
    assert bot._fallback_plan(LUNA)[0] == FLASH_G


def test_quiet_then_real_failure_still_alerts(alerts):
    bot._enter_fallback(LUNA, FLASH_G, rate_limit(), "activity_check")
    bot._enter_fallback(LUNA, FLASH_G, rate_limit(), "диалог chat=1")
    assert len(alerts) == 1 and LUNA in bot._fallback_until


def test_quiet_label_still_falls_back_this_call():
    run = bot._FallbackRun(LUNA)
    assert run.advance(rate_limit(), "activity_check") and run.model == FLASH_G


def test_tg_file_too_big():
    assert bot.tg_file_too_big(SimpleNamespace(file_size=bot.TG_FILE_MAX_BYTES + 1))
    assert not bot.tg_file_too_big(SimpleNamespace(file_size=bot.TG_FILE_MAX_BYTES))
    assert not bot.tg_file_too_big(SimpleNamespace(file_size=None)) and not bot.tg_file_too_big(SimpleNamespace())


def test_big_video_not_downloaded(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("get_file не должен вызываться")
    monkeypatch.setattr(bot, "_download_telegram_file", boom)
    big = bot.TG_FILE_MAX_BYTES + 1
    assert asyncio.run(bot._recognize_video(None, SimpleNamespace(duration=10, file_size=big, file_id="f"))) is None
    assert asyncio.run(bot._recognize_voice(None, SimpleNamespace(duration=10, file_size=big, file_id="f"))) is None
    assert asyncio.run(bot._recognize_video_note(None, SimpleNamespace(duration=10, file_size=big, file_id="f"))) == (None, None)

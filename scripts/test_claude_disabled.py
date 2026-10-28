"""Хотфикс 2026-10-28: пул закрыл модели Anthropic (403 model_blocked). CLAUDE_DISABLED=1 — Claude не зовём вообще.

Без сети, клиент подменён (хелперы из test_model_fallback). Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_claude_disabled.py -q
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("CLAUDE_DISABLED", "0")   # остальные тесты проверяют «Claude жив»; здесь включаем monkeypatch'ем
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_model_fallback import api_errors, bot, clean, dialog, make_exc, run, use_fake  # noqa: E402,F401


@pytest.fixture(autouse=True)
def disabled(monkeypatch, clean):
    monkeypatch.setattr(bot, "CLAUDE_DISABLED", True)
    return clean


def models(fake):
    return [c["model"] for c in fake.calls]


@pytest.mark.parametrize("key,target", [("sonnet", "FALLBACK_MODEL"), ("opus", "FALLBACK_MODEL"),
                                        ("fable", "FALLBACK_MODEL"), ("haiku", "FALLBACK_MODEL_CHEAP")])
def test_claude_dialog_goes_straight_to_gemini(monkeypatch, disabled, key, target):
    fake = use_fake(monkeypatch, make_exc(403, "Model is blocked", {"error": {"details": {"error_code": "model_blocked"}}}))
    run(dialog(bot.MODELS[key]["id"]))
    assert models(fake) == [getattr(bot, target)]       # ни одной попытки Claude
    assert disabled == [getattr(bot, target)]           # учёт по фактической модели


def test_even_with_fallback_flag_off(monkeypatch, disabled):
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", False)
    fake = use_fake(monkeypatch)
    run(dialog(bot.MODELS["sonnet"]["id"]))
    assert models(fake) == [bot.FALLBACK_MODEL]


def test_sync_create_service_call(monkeypatch, disabled):
    fake = use_fake(monkeypatch)
    bot.sync_create(model=bot.DEFAULT_MODEL_ID, max_tokens=30, label="aux",
                    messages=[{"role": "user", "content": "hi"}], **bot.aux_params())
    assert models(fake) == [bot.FALLBACK_MODEL_CHEAP]
    assert "thinking" not in fake.calls[0] and "output_config" not in fake.calls[0]


def test_no_reverse_fallback_to_claude(monkeypatch, disabled):
    assert bot.fallback_model_for(bot.MODELS["lite"]["id"]) is None
    assert bot.fallback_model_for(bot.MODELS["flash"]["id"]) is None
    fake = use_fake(monkeypatch)
    run(dialog(bot.MODELS["lite"]["id"]))
    assert models(fake) == [bot.MODELS["lite"]["id"]]


def test_native_search_tools_not_sent(monkeypatch, disabled):
    fake = use_fake(monkeypatch)
    with pytest.raises(bot.NativeSearchUnavailable):
        run(dialog(bot.MODELS["sonnet"]["id"], tools=[{"type": "web_search_20250305", "name": "web_search"}]))
    assert fake.calls == []


def test_search_provider_paid_claude_is_gpt(monkeypatch, disabled):
    monkeypatch.setattr(bot, "chat_tier", lambda chat_id: "paid")
    monkeypatch.setattr(bot.db, "get_chat_search_mode_db", lambda chat_id: "auto")
    assert bot.search_provider_for(1, bot.MODELS["sonnet"]["id"]) == "gpt"


def test_model_blocked_403_triggers_fallback_marker():
    exc = make_exc(403, "Model 'claude-haiku-5-5' is blocked for this account.",
                   {"error": {"details": {"error_code": "model_blocked"}, "type": "permission_error"}})
    assert api_errors.should_fallback(exc) is True

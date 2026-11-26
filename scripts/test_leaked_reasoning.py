"""Рассуждения модели обычным текстом в ответе (инцидент 2026-11-26) и изменённые сообщения в handle_message (v2.24.1)."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import bot  # noqa: E402

# Форма настоящего ответа 2026-11-26 (текст заменён нейтральным).
LEAK = ("दिग्गज\nThinking Process:\n\n1.  **Analyze the Request:**\n    *   Message to reply to: …\n\n"
        "2.  **Formulate Response:**\n    *   …\n\n3.  **Refine:**\n    *   …Готовая фраза, приклеенная к плану.")


def test_incident_shape_detected():
    assert bot.looks_like_leaked_reasoning(LEAK)


def test_variants_detected():
    for t in ("Thinking Process: …", "  **Analyze the request:** x", "1. **Draft Response:** x", "<thought>x</thought> ответ",
              "**Formulate the response**\nтекст"):
        assert bot.looks_like_leaked_reasoning(t), t


def test_normal_answers_not_detected():
    for t in ("Привет! Как дела?", "1. **Шаг первый:** сделай бэкап", "Thinking process у тебя хромает, друг",
              "Вот план:\n1. **Analyze** логи\n2. посмотри",  # «Analyze» без «the request» — обычный список
              "", "   "):
        assert not bot.looks_like_leaked_reasoning(t), t


def test_only_head_is_checked():
    t = "Обычный ответ. " * 30 + "\nThinking Process: дальше по тексту"
    assert not bot.looks_like_leaked_reasoning(t)


def test_strip_drops_leaked_reasoning():
    assert bot.strip_leaked_service_note(LEAK) == ("", True)
    assert bot.strip_leaked_service_note("Просто ответ") == ("Просто ответ", False)


def _resp(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


def test_finish_review_drops_leaked_reasoning():
    assert bot._finish_review(_resp(LEAK), -1, "test") == ""
    assert bot._finish_review(_resp("Обзор: всё спокойно."), -1, "test") == "Обзор: всё спокойно."


def test_dialog_call_retries_once_on_leak(monkeypatch):
    calls = []

    async def fake_model_call(context, chat_id, *, label, usage_label, **kw):
        calls.append(label)
        return _resp(LEAK if len(calls) == 1 else "Нормальный ответ")

    monkeypatch.setattr(bot, "_model_call", fake_model_call)
    r = asyncio.run(bot._dialog_call(None, 1, 1, False, retry_messages=lambda: [], model="m", messages=[]))
    assert bot.api_errors.response_text(r) == "Нормальный ответ"
    assert len(calls) == 2 and "рассуждения" in calls[1]


def test_dialog_call_no_retry_on_normal(monkeypatch):
    calls = []

    async def fake_model_call(context, chat_id, *, label, usage_label, **kw):
        calls.append(label)
        return _resp("Ответ")

    monkeypatch.setattr(bot, "_model_call", fake_model_call)
    asyncio.run(bot._dialog_call(None, 1, 1, False, retry_messages=lambda: [], model="m", messages=[]))
    assert len(calls) == 1


def test_handle_message_ignores_edited_message():
    upd = SimpleNamespace(message=None, edited_message=SimpleNamespace(text="правка"),
                          effective_user=None, effective_chat=None)
    assert asyncio.run(bot.handle_message(upd, None)) is None   # раньше — AttributeError

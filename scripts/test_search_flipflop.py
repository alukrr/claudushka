"""Качели «слажала / не слажала» (инцидент 2026-11-27, v2.24.2): should_search получает дату и цитату отдельно,
правило честности не велит пересматривать прошлые реплики по свежей выдаче."""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import bot  # noqa: E402


def test_input_has_date_and_separate_reply():
    s = bot.search_decision_input("Таллин будет в РФ?", "прошлая реплика бота", today="2026-11-27")
    assert s.startswith("<today>2026-11-27</today>")
    assert "<message>Таллин будет в РФ?</message>" in s
    assert "<reply_to>прошлая реплика бота</reply_to>" in s
    assert "Контекст реплая" not in s


def test_input_without_reply_and_default_date():
    s = bot.search_decision_input("курс евро")
    assert "<reply_to>" not in s
    assert f"<today>{bot._berlin_now().strftime('%Y-%m-%d')}</today>" in s


def test_should_search_sends_date_in_message_not_system(monkeypatch):
    seen = {}

    def fake_sync_create(**kw):
        seen.update(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="NO")], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    monkeypatch.setattr(bot, "sync_create", fake_sync_create)
    assert bot.should_search("Таллин будет в РФ?", "цитата") is None
    content = seen["messages"][0]["content"]
    assert "<today>" in content and "<reply_to>цитата</reply_to>" in content
    assert "<today>" in seen["system"] and bot._berlin_now().strftime("%Y-%m-%d") not in seen["system"]  # system статичен


def test_callers_pass_reply_context_separately(monkeypatch):
    calls = []

    async def aux(fn, *a, label=""):
        calls.append((label, a))
        return None
    monkeypatch.setattr(bot, "call_claude_aux", aux)
    monkeypatch.setattr(bot, "_search_allowed", lambda *a: True)
    msg = SimpleNamespace(message=SimpleNamespace())
    asyncio.run(bot.search_with_provider(msg, 1, 1, True, "вопрос", "цитата", "gpt"))
    monkeypatch.setattr(bot, "tavily", object())
    asyncio.run(bot.search_with_provider(msg, 1, 1, True, "вопрос", "цитата", "tavily"))
    assert calls == [("should_search", ("вопрос", "цитата"))] * 2


def test_core_rules_forbid_unprompted_self_revision():
    r = bot._core_rules()
    assert "по свежей выдаче поиска не пересматривай" in r
    assert "Уже признанную ошибку не отменяй" in r
    assert "«перепроверила»" in r

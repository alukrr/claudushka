"""Юнит-тесты Gemini как моделей чата (ТЗ feat/gemini-models): реестр, kwargs под провайдера, обратный фолбэк,
free-выбор. Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_gemini_models.py -q
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("CLAUDE_DISABLED", "0")   # эти тесты — про живой Claude (хотфикс 2026-10-28)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_errors  # noqa: E402
from test_model_fallback import make_exc, ok_response  # noqa: E402  (импортирует и bot)
import bot  # noqa: E402

FLASH, ARGON, LITE = (bot.MODELS[k]["id"] for k in ("flash", "argon", "lite"))
SONNET, HAIKU = bot.MODELS["sonnet"]["id"], bot.DEFAULT_MODEL_ID


def run(coro):
    return asyncio.run(coro)


def test_registry_gemini_entries():
    for key, price in (("argon", (2.0, 10.0)), ("flash", (0.75, 3.75)), ("lite", (0.25, 1.50))):
        m = bot.MODELS[key]
        assert m["provider"] == "gemini" and (m["in"], m["out"]) == price
        assert bot.model_provider(m["id"]) == "gemini"
        assert not m.get("effort") and not m.get("restricted")
        assert bot.dialog_params(m["id"]) == {} and bot.aux_params(m["id"]) == {}
        assert bot.out_tokens(m["id"], 4096) == 4096 + 4000
    assert bot.model_provider(SONNET) == "anthropic" and bot.model_provider("gpt-6-luna") == "openai"
    # одна цена на строку: Gemini-модели чата не дублируются в LEGACY_PRICES
    assert not {FLASH, ARGON, LITE} & bot.LEGACY_PRICES.keys()


def test_kwargs_for_model_gemini_chat_keeps_budget_claude_source_adds():
    sysb = [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    base = dict(system=sysb, messages=[{"role": "user", "content": "hi"}], thinking={"type": "adaptive"},
                output_config={"effort": "low"}, tools=[{"type": "x"}])
    # вызов собран под Gemini (запас уже в max_tokens) — второй раз не добавляем
    out = bot.kwargs_for_model(dict(model=FLASH, max_tokens=4096 + 4000, **base), FLASH)
    assert out["max_tokens"] == 8096 and "cache_control" not in str(out) and not {"thinking", "tools"} & out.keys()
    # собран под Claude → запас добавляем
    out = bot.kwargs_for_model(dict(model=SONNET, max_tokens=100, **base), FLASH)
    assert out["max_tokens"] == 100 + bot.FALLBACK_THINKING_HEADROOM


def test_kwargs_for_model_gemini_to_claude_swaps_headroom():
    kw = dict(model=FLASH, max_tokens=4096 + 4000, system="s", messages=[])
    assert bot.kwargs_for_model(kw, SONNET)["max_tokens"] == 4096 + bot.MODELS["sonnet"]["thinking_headroom"]
    assert bot.kwargs_for_model(kw, HAIKU)["max_tokens"] == 4096 + bot.MODELS["haiku"]["thinking_headroom"]
    assert kw["model"] == FLASH and kw["max_tokens"] == 8096          # оригинал цел


def test_fallback_model_for_reverse():
    assert bot.fallback_model_for(FLASH) == SONNET and bot.fallback_model_for(ARGON) == SONNET
    assert bot.fallback_model_for(LITE) == HAIKU
    assert bot.fallback_model_for(SONNET) == bot.FALLBACK_MODEL


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    bot._fallback_until.clear()
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", True)
    monkeypatch.setattr(bot, "_last_fallback_alert", 0.0)
    monkeypatch.setattr(bot, "_fire", lambda coro: coro.close())

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(api_errors, "_sleep_with_keepalive", no_sleep)
    tracked = []
    monkeypatch.setattr(bot, "_track_response", lambda model, resp, label="aux": tracked.append((model, label)))
    return tracked


class Fake:
    """Gemini-модели падают с gemini_exc, Claude отвечает; запоминает вызовы."""
    def __init__(self, gemini_exc=None, claude_exc=None):
        self.calls, self.gemini_exc, self.claude_exc = [], gemini_exc, claude_exc

    def create(self, **kw):
        self.calls.append(kw)
        exc = self.gemini_exc if kw["model"].startswith("gemini") else self.claude_exc
        if exc:
            raise exc
        return ok_response(kw["model"])


def use_fake(monkeypatch, **kw):
    fake = Fake(**kw)
    c = SimpleNamespace(messages=fake)
    monkeypatch.setattr(bot, "client", c)
    monkeypatch.setattr(bot, "client_noretry", c)
    return fake


def dialog(model, max_tokens, **extra):
    return bot.call_claude(None, None, label="диалог", usage_label="dialog", model=model, max_tokens=max_tokens,
                           system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
                           messages=[{"role": "user", "content": "hi"}], **extra)


def test_gemini_chat_first_call_is_cleaned(monkeypatch, clean):
    fake = use_fake(monkeypatch)
    run(dialog(FLASH, 8096, thinking={"type": "adaptive"}))
    assert len(fake.calls) == 1
    assert "cache_control" not in str(fake.calls[0]) and "thinking" not in fake.calls[0]
    assert fake.calls[0]["max_tokens"] == 8096
    assert clean == [(FLASH, "dialog")]


def test_reverse_fallback_to_sonnet_with_label(monkeypatch, clean):
    fake = use_fake(monkeypatch, gemini_exc=make_exc(529))
    run(dialog(FLASH, 4096 + 4000))
    assert [c["model"] for c in fake.calls] == [FLASH, FLASH, SONNET]
    assert fake.calls[-1]["max_tokens"] == 4096 + bot.MODELS["sonnet"]["thinking_headroom"]
    assert clean == [(SONNET, "dialog_fallback")]                      # модель фактическая, метка с суффиксом
    # липкое окно: следующий запрос сразу к Claude
    fake.calls.clear()
    run(dialog(FLASH, 8096))
    assert [c["model"] for c in fake.calls] == [SONNET]


def test_lite_falls_back_to_haiku(monkeypatch):
    fake = use_fake(monkeypatch, gemini_exc=make_exc(500))
    run(dialog(LITE, 8096))
    assert fake.calls[-1]["model"] == HAIKU


def test_gemini_chat_no_fallback_on_402(monkeypatch):
    fake = use_fake(monkeypatch, gemini_exc=make_exc(402, "insufficient balance"))
    with pytest.raises(Exception):
        run(dialog(FLASH, 8096))
    assert all(c["model"] == FLASH for c in fake.calls)


def test_probe_gemini_gets_headroom(monkeypatch):
    fake = use_fake(monkeypatch)
    run(bot._probe_model(None, 1, FLASH))
    assert fake.calls[0]["max_tokens"] == 4001
    run(bot._probe_model(None, 1, SONNET))
    assert fake.calls[-1]["max_tokens"] == 1


def test_usage_label():
    assert bot._usage_label("dialog", FLASH, FLASH) == "dialog"
    assert bot._usage_label("dialog", SONNET, FLASH) == "dialog_fallback"


def test_get_chat_model_free_choice(monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: ARGON)    # платный выбор не должен влиять
    monkeypatch.setattr(bot.db, "get_chat_free_model_db", lambda c: LITE)
    assert bot.get_chat_model(1) == LITE
    monkeypatch.setattr(bot.db, "get_chat_free_model_db", lambda c: None)
    assert bot.get_chat_model(1) == HAIKU
    monkeypatch.setattr(bot.db, "get_chat_free_model_db", lambda c: SONNET)      # недопустимое в free → Haiku
    assert bot.get_chat_model(1) == HAIKU
    monkeypatch.setattr(bot, "chat_tier", lambda c: "paid")
    assert bot.get_chat_model(1) == ARGON


class Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **k):
        self.sent.append(text)


def make_update(user_id=5):
    m = Msg()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=m,
                           effective_chat=SimpleNamespace(id=user_id, type="private")), m


def test_set_model_free_flow(monkeypatch):
    """free: Lite → chat_free_model (chat_models не трогаем); Flash → отказ; Haiku → тоже free-таблица."""
    writes = []
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot.db, "set_chat_free_model_db", lambda c, m: writes.append(("free", m)))
    monkeypatch.setattr(bot.db, "set_chat_model_db", lambda c, m: writes.append(("paid", m)))
    monkeypatch.setattr(bot, "_chat_info", lambda c: (True, "этот диалог"))

    async def probe(*a, **k):
        return None
    monkeypatch.setattr(bot, "_probe_model", probe)
    ctx = SimpleNamespace(args=[], bot=None)
    upd, m = make_update()
    run(bot._set_chat_model(upd, ctx, "lite"))
    run(bot._set_chat_model(upd, ctx, "haiku"))
    run(bot._set_chat_model(upd, ctx, "flash"))
    assert writes == [("free", LITE), ("free", HAIKU)]
    assert "платном режиме" in m.sent[-1]


def test_set_model_free_probe_failure_does_not_write(monkeypatch):
    writes = []
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot.db, "set_chat_free_model_db", lambda c, m: writes.append(m))
    monkeypatch.setattr(bot, "_chat_info", lambda c: (True, "этот диалог"))

    async def probe(*a, **k):
        raise make_exc(529)
    monkeypatch.setattr(bot, "_probe_model", probe)
    upd, m = make_update()
    run(bot._set_chat_model(upd, SimpleNamespace(args=[], bot=None), "lite"))
    assert writes == [] and "недоступна" in m.sent[-1]

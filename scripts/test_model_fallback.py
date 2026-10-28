"""Юнит-тесты фолбэка Gemini → GPT (ТЗ no-anthropic, docs/claude/model-fallback.md).

Без сети: клиент подменён. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_model_fallback.py -q
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("CLAUDE_DISABLED", "0")   # эти тесты — про живой Claude (хотфикс 2026-10-28)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_errors  # noqa: E402

# bot.py при импорте создаёт /app/data (путь контейнера) — вне контейнера подавляем только это.
_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir


def make_exc(status: int, text: str = "boom", body=None):
    req = httpx.Request("POST", "http://pool/v1/messages")
    resp = httpx.Response(status, request=req, json=body or {"error": {"message": text}})
    cls = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
           404: anthropic.NotFoundError, 429: anthropic.RateLimitError}.get(status)
    if cls is None:
        cls = anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError
    return cls(text, response=resp, body=body)


def ok_response(model: str):
    return SimpleNamespace(model=model, content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.mark.parametrize("exc,expected", [
    (make_exc(500), True), (make_exc(529), True), (make_exc(503), True), (make_exc(429), True),
    (anthropic.APIConnectionError(request=httpx.Request("POST", "http://x")), True),
    (make_exc(404), True),
    (make_exc(403, "no available channel for model"), True),
    (make_exc(401, "upstream subscription expired"), True),
    (make_exc(401, "invalid x-api-key"), False),
    (make_exc(403, "forbidden"), False),
    (make_exc(402, "insufficient balance"), False),
    (make_exc(400, "prompt is too long: 300000 tokens"), False),
    (make_exc(400, "bad param"), False),
    (make_exc(422, "unprocessable"), False),
])
def test_should_fallback(exc, expected):
    assert api_errors.should_fallback(exc) is expected


def test_should_fallback_midstream_error():
    exc = anthropic.APIStatusError("x", response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
                                   body={"error": {"type": "overloaded_error"}})
    assert api_errors.should_fallback(exc) is True


FLASH, LITE, ARGON = (bot.MODELS[k]["id"] for k in ("flash", "lite", "argon"))
TERRA, LUNA, SOL = "gpt-6.1-terra", "gpt-6-luna", "gpt-6.1-sol"


def test_fallback_kwargs_strips_and_copies():
    system = [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
    kw = dict(model=FLASH, system=system, messages=messages, max_tokens=100 + 4000,
              thinking={"type": "adaptive"}, output_config={"effort": "low"}, tools=[{"type": "web_search"}])
    out = bot.kwargs_for_model(kw, TERRA)
    assert out["model"] == TERRA
    assert out["max_tokens"] == 100 + bot._headroom(TERRA)       # запас Gemini заменён запасом GPT
    assert not {"thinking", "output_config", "tools"} & out.keys()
    assert "cache_control" not in str(out["system"]) + str(out["messages"])
    assert "cache_control" in str(system) and "cache_control" in str(messages) and "thinking" in kw  # оригиналы целы


def test_fallback_pairs():
    assert bot.fallback_model_for(LITE) == LUNA and bot.fallback_model_for(FLASH) == TERRA
    assert bot.fallback_model_for(ARGON) == SOL
    assert bot.fallback_model_for(LUNA) is None and bot.fallback_model_for("claude-haiku-5-5") is None
    for gem, gpt in bot.FALLBACK_PAIRS.items():                  # пара — другой провайдер и с ценой
        assert bot.model_provider(gem) == "gemini" and bot.model_provider(gpt) == "openai"
        assert bot.price_meta(gpt) is not None and bot.price_meta(gem) is not None


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
    monkeypatch.setattr(bot, "_track_response", lambda model, resp, label="aux": tracked.append(model))
    return tracked


class FakeMessages:
    """Gemini-модели падают с primary_exc (если задан), GPT отвечает."""
    def __init__(self, primary_exc=None):
        self.calls = []
        self.primary_exc = primary_exc

    def create(self, **kw):
        self.calls.append(kw)
        if kw["model"].startswith("gemini") and self.primary_exc:
            raise self.primary_exc
        return ok_response(kw["model"])


def use_fake(monkeypatch, primary_exc=None):
    fake = FakeMessages(primary_exc)
    c = SimpleNamespace(messages=fake)
    monkeypatch.setattr(bot, "client", c)
    monkeypatch.setattr(bot, "client_noretry", c)
    return fake


def run(coro):
    return asyncio.run(coro)


def dialog(model, **extra):
    return bot.call_claude(None, None, label="t", model=model, max_tokens=50, system="s",
                           messages=[{"role": "user", "content": "hi"}], **extra)


def test_normal_path_no_fallback(monkeypatch, clean):
    fake = use_fake(monkeypatch)
    run(dialog(FLASH))
    assert [c["model"] for c in fake.calls] == [FLASH] and clean == [FLASH]


def test_5xx_falls_back_one_retry_then_gpt(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    assert [c["model"] for c in fake.calls] == [FLASH] * 2 + [TERRA]
    assert clean == [TERRA]                         # учёт по фактической модели


@pytest.mark.parametrize("model,target", [(LITE, LUNA), (FLASH, TERRA), (ARGON, SOL)])
def test_each_model_goes_to_its_pair(monkeypatch, model, target):
    fake = use_fake(monkeypatch, make_exc(500))
    run(dialog(model))
    assert fake.calls[-1]["model"] == target


def test_sticky_window_then_return(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    fake.calls.clear()
    run(dialog(FLASH))                               # окно открыто: основную модель не трогаем вообще
    assert [c["model"] for c in fake.calls] == [TERRA]
    bot._fallback_until[FLASH] = time.monotonic() - 1   # окно истекло
    fake.calls.clear()
    fake.primary_exc = None
    run(dialog(FLASH))
    assert [c["model"] for c in fake.calls] == [FLASH]


def test_sticky_is_per_model(monkeypatch):
    use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    assert bot._sticky_fallback(ARGON) is None


@pytest.mark.parametrize("exc", [make_exc(402, "insufficient balance"), make_exc(400, "prompt is too long: 9"),
                                 make_exc(401, "invalid key")])
def test_no_fallback_for_402_400_key(monkeypatch, exc):
    fake = use_fake(monkeypatch, exc)
    with pytest.raises(anthropic.APIStatusError):
        run(dialog(FLASH))
    assert all(c["model"] == FLASH for c in fake.calls)
    assert not bot._fallback_until


def test_disabled(monkeypatch):
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", False)
    fake = use_fake(monkeypatch, make_exc(529))
    with pytest.raises(anthropic.APIStatusError):
        run(dialog(FLASH))
    assert len(fake.calls) == len(api_errors.RETRY_DELAYS) + 1     # старая лестница ретраев


def test_probe_never_falls_back(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(404))
    with pytest.raises(anthropic.NotFoundError):
        run(bot._probe_model(None, 1, FLASH))
    assert all(c["model"] == FLASH for c in fake.calls)


def test_sync_create_fallback(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(500))
    r = bot.sync_create(label="x", model=bot.DEFAULT_MODEL_ID, max_tokens=30 + 4000, system="s",
                        messages=[{"role": "user", "content": "x"}], thinking={"type": "disabled"},
                        output_config={"effort": "low"})
    assert api_errors.response_text(r) == "ok"
    assert fake.calls[-1]["model"] == LUNA and "thinking" not in fake.calls[-1]
    assert fake.calls[-1]["max_tokens"] == 30 + bot._headroom(LUNA)
    assert clean == [LUNA]


def test_prices_present():
    for m in list(bot.FALLBACK_PAIRS) + list(bot.FALLBACK_PAIRS.values()):
        assert bot.price_meta(m) is not None
    assert bot._billed_model(TERRA, SimpleNamespace(model=TERRA)) == TERRA
    # старые Claude-строки usage_log по-прежнему имеют подпись
    assert bot.price_meta("claude-haiku-5-5")["label"] == "Haiku 5.5"
    assert bot.price_meta("claude-fable-5-1")["label"] == "Fable 5.1"


def test_dedup_pricing_matches_bot():
    """dedup_memory.PRICING — ручная копия MODELS из bot.py (общего модуля нет): расхождение цен/запаса — падение."""
    import dedup_memory as dm
    assert set(dm.MODEL_ALIASES.values()) == {m["id"] for m in bot.MODELS.values()}
    for m in bot.MODELS.values():
        assert dm.PRICING[m["id"]] == (m["in"], m["out"], m["cache_read_mult"], m["cache_write_5m_mult"],
                                       m["cache_write_1h_mult"]), m["id"]
        assert dm.THINKING_HEADROOM[m["id"]] == m["thinking_headroom"]


def test_catalog_ids_strips_any_provider_prefix():
    payload = {"data": [{"id": "openai/gpt-6.1-terra"}, {"id": "google/gemini-3.8-flash"},
                        {"id": "gemini-4-argon"}, {"id": "openai/gpt-6-luna"}]}
    ids = bot.catalog_ids(payload)
    assert ids == {"gpt-6.1-terra", "gemini-3.8-flash", "gemini-4-argon", "gpt-6-luna"}
    assert set(bot.FALLBACK_PAIRS.values()) - {"gpt-6.1-sol"} <= ids
    assert bot.catalog_ids({}) == set()

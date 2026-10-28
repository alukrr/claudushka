"""Юнит-тесты фолбэка Claude → Gemini (ТЗ feat/model-fallback, docs/claude/model-fallback.md).

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


def test_fallback_kwargs_strips_and_copies():
    system = [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
    kw = dict(model=bot.DEFAULT_MODEL_ID, system=system, messages=messages, max_tokens=100,
              thinking={"type": "adaptive"}, output_config={"effort": "low"}, tools=[{"type": "web_search"}])
    out = bot.kwargs_for_model(kw, "gemini-x")
    assert out["model"] == "gemini-x"
    assert out["max_tokens"] == 100 + bot.FALLBACK_THINKING_HEADROOM
    assert not {"thinking", "output_config", "tools"} & out.keys()
    assert "cache_control" not in str(out["system"]) + str(out["messages"])
    assert "cache_control" in str(system) and "cache_control" in str(messages) and "thinking" in kw  # оригиналы целы


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
    def __init__(self, claude_exc=None):
        self.calls = []
        self.claude_exc = claude_exc

    def create(self, **kw):
        self.calls.append(kw)
        if kw["model"].startswith("gemini"):
            return ok_response(kw["model"])
        if self.claude_exc:
            raise self.claude_exc
        return ok_response(kw["model"])


def use_fake(monkeypatch, claude_exc=None):
    fake = FakeMessages(claude_exc)
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
    run(dialog(bot.MODELS["sonnet"]["id"]))
    assert [c["model"] for c in fake.calls] == [bot.MODELS["sonnet"]["id"]] and clean == [bot.MODELS["sonnet"]["id"]]


def test_5xx_falls_back_one_claude_retry_then_gemini(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(bot.MODELS["sonnet"]["id"]))
    assert [c["model"] for c in fake.calls] == [bot.MODELS["sonnet"]["id"]] * 2 + [bot.FALLBACK_MODEL]
    assert clean == [bot.FALLBACK_MODEL]            # учёт по Gemini


def test_haiku_goes_to_cheap(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(500))
    run(dialog(bot.DEFAULT_MODEL_ID))
    assert fake.calls[-1]["model"] == bot.FALLBACK_MODEL_CHEAP


def test_sticky_window_then_return(monkeypatch):
    sonnet = bot.MODELS["sonnet"]["id"]
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(sonnet))
    fake.calls.clear()
    run(dialog(sonnet))                              # окно открыто: Claude не трогаем вообще
    assert [c["model"] for c in fake.calls] == [bot.FALLBACK_MODEL]
    bot._fallback_until[sonnet] = time.monotonic() - 1   # окно истекло
    fake.calls.clear()
    fake.claude_exc = None
    run(dialog(sonnet))
    assert [c["model"] for c in fake.calls] == [sonnet]


def test_sticky_is_per_model(monkeypatch):
    use_fake(monkeypatch, make_exc(529))
    run(dialog(bot.MODELS["sonnet"]["id"]))
    assert bot._sticky_fallback(bot.MODELS["opus"]["id"]) is None


@pytest.mark.parametrize("exc", [make_exc(402, "insufficient balance"), make_exc(400, "prompt is too long: 9"),
                                 make_exc(401, "invalid key")])
def test_no_fallback_for_402_400_key(monkeypatch, exc):
    fake = use_fake(monkeypatch, exc)
    with pytest.raises(anthropic.APIStatusError):
        run(dialog(bot.MODELS["sonnet"]["id"]))
    assert all(not c["model"].startswith("gemini") for c in fake.calls)
    assert not bot._fallback_until


def test_disabled(monkeypatch):
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", False)
    fake = use_fake(monkeypatch, make_exc(529))
    with pytest.raises(anthropic.APIStatusError):
        run(dialog(bot.MODELS["sonnet"]["id"]))
    assert len(fake.calls) == len(api_errors.RETRY_DELAYS) + 1     # старая лестница ретраев


def test_probe_never_falls_back(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(404))
    with pytest.raises(anthropic.NotFoundError):
        run(bot._probe_model(None, 1, "claude-nonexistent"))
    assert all(not c["model"].startswith("gemini") for c in fake.calls)


def test_native_tools_raise_instead_of_gemini(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(529))
    with pytest.raises(bot.NativeSearchUnavailable):
        run(bot.call_claude(None, None, label="t", model=bot.MODELS["sonnet"]["id"], max_tokens=5, system="s",
                            messages=[{"role": "user", "content": "x"}], tools=[{"type": "web_search_20250305"}]))
    assert all(not c["model"].startswith("gemini") for c in fake.calls)
    with pytest.raises(bot.NativeSearchUnavailable):   # липкое окно уже открыто
        run(bot.call_claude(None, None, label="t", model=bot.MODELS["sonnet"]["id"], max_tokens=5, system="s",
                            messages=[{"role": "user", "content": "x"}], tools=[{"type": "web_search_20250305"}]))
    run(dialog(bot.MODELS["sonnet"]["id"]))             # а обычный запрос в окне идёт на Gemini
    assert fake.calls[-1]["model"] == bot.FALLBACK_MODEL


def test_sync_create_fallback(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(500))
    r = bot.sync_create(label="x", model=bot.DEFAULT_MODEL_ID, max_tokens=30, system="s",
                        messages=[{"role": "user", "content": "x"}], thinking={"type": "disabled"},
                        output_config={"effort": "low"})
    assert api_errors.response_text(r) == "ok"
    assert fake.calls[-1]["model"] == bot.FALLBACK_MODEL_CHEAP and "thinking" not in fake.calls[-1]
    assert clean == [bot.FALLBACK_MODEL_CHEAP]


def test_gemini_prices_present():
    for m in (bot.FALLBACK_MODEL, bot.FALLBACK_MODEL_CHEAP):
        assert bot.price_meta(m) is not None
    assert bot._billed_model("gemini-3.8-flash", SimpleNamespace(model="gemini-3.8-flash")) == "gemini-3.8-flash"


@pytest.mark.parametrize("second_exc", [make_exc(529), None])
def test_native_searches_recorded_when_later_iteration_fails(monkeypatch, second_exc):
    """Поиски первой итерации (pause_turn) попадают в usage_log, даже если вторая упала."""
    recorded = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: recorded.append(a))
    block = SimpleNamespace(type="text", text="ищу", model_dump=lambda **k: {"type": "text", "text": "ищу"})
    first = SimpleNamespace(
        content=[block], stop_reason="pause_turn",
        usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=2)))
    exc = second_exc or bot.NativeSearchUnavailable("m")
    calls = []

    async def fake_stream(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            return first
        raise exc

    monkeypatch.setattr(bot, "call_claude_stream", fake_stream)
    with pytest.raises(type(exc)):
        run(bot.call_dialog_native(None, 1, None, label="t", model=bot.MODELS["sonnet"]["id"], max_tokens=50,
                                   system="s", messages=[{"role": "user", "content": "x"}]))
    assert len(calls) == 2
    assert [r[:3] for r in recorded] == [("search", "anthropic", "native_search")] * 2

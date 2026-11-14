"""Запасной путь расшифровки голоса: пул не ответил 200 → прямой Google API (gemini-3.1-flash-lite), docs/claude/media.md."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Resp:
    def __init__(self, status, text="", meta=None):
        self.status_code = status
        self._text, self._meta = text, meta or {}

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": self._text}] if self._text else []}}], "usageMetadata": self._meta}


@pytest.fixture
def env(monkeypatch):
    calls, rec = [], []
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "gkey")
    monkeypatch.setattr(bot, "ANTHROPIC_API_KEY", "poolkey")
    monkeypatch.setattr(bot, "_audio_pool_down_until", 0.0)
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rec.append((a, k)))
    answers = {"pool": Resp(429), "google": Resp(200, "Привет!")}

    def post(url, **k):
        who = "google" if url.startswith(bot.GOOGLE_API_BASE_URL) else "pool"
        calls.append((who, url, k["headers"]["x-goog-api-key"]))
        r = answers[who]
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(bot.http_requests, "post", post)
    return calls, rec, answers


def test_pool_ok_no_direct_call(env):
    calls, rec, answers = env
    answers["pool"] = Resp(200, "Из пула")
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) == "Из пула"
    assert [c[0] for c in calls] == ["pool"]
    assert rec[0][0][2] == "voice"


def test_pool_empty_200_is_not_a_failure(env):
    calls, _, answers = env
    answers["pool"] = Resp(200, "")
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) is None
    assert [c[0] for c in calls] == ["pool"]


@pytest.mark.parametrize("pool", [Resp(429), Resp(503), Resp(404), RuntimeError("сеть")])
def test_pool_fail_goes_to_google(env, pool):
    calls, rec, answers = env
    answers["pool"] = pool
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) == "Привет!"
    assert [c[0] for c in calls] == ["pool", "google"]
    assert bot.GEMINI_AUDIO_DIRECT_MODEL in calls[1][1] and calls[1][2] == "gkey" and calls[0][2] == "poolkey"
    assert rec[-1][0][1:3] == (bot.GEMINI_AUDIO_DIRECT_MODEL, "voice_fallback")


def test_sticky_skips_pool_then_returns(env, monkeypatch):
    calls, _, answers = env
    run(bot._transcribe_audio_gemini(b"x", "audio/ogg"))
    run(bot._transcribe_audio_gemini(b"x", "audio/ogg"))
    assert [c[0] for c in calls] == ["pool", "google", "google"]
    monkeypatch.setattr(bot, "_audio_pool_down_until", 0.0)          # окно истекло, пул ожил
    answers["pool"] = Resp(200, "Снова пул")
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) == "Снова пул"
    assert calls[-1][0] == "pool"


def test_no_google_key_behaves_as_before(env, monkeypatch):
    calls, _, _ = env
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "")
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) is None
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) is None
    assert [c[0] for c in calls] == ["pool", "pool"]


def test_both_fail_returns_none(env):
    _, rec, answers = env
    answers["google"] = Resp(500)
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) is None
    assert rec == []


def test_direct_cost_audio_tokens_priced_higher(env):
    _, rec, _ = env
    meta = {"promptTokenCount": 1030, "candidatesTokenCount": 100, "thoughtsTokenCount": 20,
            "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 30}, {"modality": "AUDIO", "tokenCount": 1000}]}
    bot._record_gemini_direct_audio_usage(meta, "gemini-3.1-flash-lite")
    args, kw = rec[0]
    assert args[:3] == ("llm", "gemini-3.1-flash-lite", "voice_fallback") and args[4:6] == (1030, 120) and kw["thinking"] == 20
    assert args[3] == pytest.approx((30 * 0.25 + 1000 * 0.50 + 120 * 1.50) / 1e6)

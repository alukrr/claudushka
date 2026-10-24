"""Юнит-тесты поиска через Gemini + Google Search (ТЗ feat/gemini-search, docs/claude/native-search.md).

Без сети: http_requests и _record_usage подменены. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_gemini_search.py -q
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body


def good_body():
    return {"candidates": [{
        "content": {"parts": [{"text": "думаю...", "thought": True}, {"text": "EUR/USD 1.09 на 24.10.2026"}]},
        "finishReason": "STOP",
        "groundingMetadata": {"webSearchQueries": ["EUR to USD"],
                              "groundingChunks": [{"web": {"title": "xe.com", "uri": REDIRECT}},
                                                  {"web": {"title": "europa.eu", "uri": REDIRECT + "2"}}]},
    }], "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 215, "thoughtsTokenCount": 482}}


@pytest.fixture
def rec(monkeypatch):
    rows = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rows.append((a, k)))
    monkeypatch.setattr(bot, "GEMINI_RETRY_DELAY", 0)
    return rows


def run(coro):
    return asyncio.run(coro)


def test_success_skips_thoughts_resolves_redirect_and_bills_thoughts(monkeypatch, rec):
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, good_body()))
    monkeypatch.setattr(bot.http_requests, "head", lambda url, **k: Resp(
        302, headers={"Location": "https://www.xe.com/currencyconverter/"}) if url == REDIRECT else Resp(200))
    text, sources = run(bot._gemini_grounded_search("курс евро"))
    assert text == "EUR/USD 1.09 на 24.10.2026"
    assert sources[0] == ("xe.com", "https://www.xe.com/currencyconverter/")
    assert sources[1] == ("europa.eu", REDIRECT + "2")          # не развернулось → redirect с подписью из title
    llm = next(a for a, _ in rec if a[0] == "llm")
    assert llm[1] == bot.SEARCH_GEMINI_MODEL and llm[2] == "gemini_search"
    assert llm[5] == 215 + 482                                   # выход = ответ + рассуждения
    assert any(a[0] == "search" and a[1] == "gemini" for a, _ in rec)


def test_text_is_capped(monkeypatch, rec):
    body = good_body()
    body["candidates"][0]["content"]["parts"] = [{"text": "x" * 10000}]
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    monkeypatch.setattr(bot.http_requests, "head", lambda *a, **k: Resp(200))
    text, _ = run(bot._gemini_grounded_search("q"))
    assert len(text) == bot.SEARCH_GEMINI_MAX_CHARS


def test_retry_once_on_5xx_then_none(monkeypatch, rec):
    calls = []
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: calls.append(1) or Resp(503))
    assert run(bot._gemini_grounded_search("q")) is None
    assert len(calls) == 2


def test_no_retry_on_400(monkeypatch, rec):
    calls = []
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: calls.append(1) or Resp(400))
    assert run(bot._gemini_grounded_search("q")) is None
    assert len(calls) == 1


def test_network_error_returns_none(monkeypatch, rec):
    def boom(*a, **k):
        raise ConnectionError("x")
    monkeypatch.setattr(bot.http_requests, "post", boom)
    assert run(bot._gemini_grounded_search("q")) is None


def test_empty_text_returns_none_but_bills(monkeypatch, rec):
    body = good_body()
    body["candidates"][0]["content"]["parts"] = [{"text": "t", "thought": True}]
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    assert run(bot._gemini_grounded_search("q")) is None
    assert any(a[0] == "llm" for a, _ in rec)                    # токены уже потрачены


def test_sources_line_accepts_labelled_pairs():
    md, plain = bot.sources_line([("xe.com", "https://www.xe.com/a"), ("europa.eu", REDIRECT),
                                  ("xe.com", "https://xe.com/b"), "https://reuters.com/x"])
    assert plain == "🔗 xe.com · europa.eu · reuters.com"
    assert "(https://www.xe.com/a)" in md


def test_provider_fallback_to_tavily_when_gemini_none(monkeypatch):
    async def none_search(q):
        return None
    async def aux(fn, *a, label=""):
        return "q" if label == "should_search" else "tavily-result"
    monkeypatch.setattr(bot, "_gemini_grounded_search", none_search)
    monkeypatch.setattr(bot, "call_claude_aux", aux)
    monkeypatch.setattr(bot, "_search_allowed", lambda *a: True)
    monkeypatch.setattr(bot, "react", lambda *a, **k: None)
    monkeypatch.setattr(bot, "tavily", object())
    msg = SimpleNamespace(message=SimpleNamespace())
    assert run(bot.search_with_provider(msg, 1, 1, False, "курс", "", "gemini")) == ("q", "tavily-result", [])
    monkeypatch.setattr(bot, "tavily", None)
    assert run(bot.search_with_provider(msg, 1, 1, False, "курс", "", "gemini")) == ("", "", [])

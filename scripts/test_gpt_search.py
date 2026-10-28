"""Юнит-тесты поиска через GPT (/v1/responses) и выбора провайдера поиска (ТЗ feat/gemini-models,
docs/claude/native-search.md). Без сети: http_requests и _record_usage подменены. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_gpt_search.py -q
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

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        return self._body


def good_body(searches=1, with_tool_use=True):
    usage = {"input_tokens": 8769, "output_tokens": 88, "total_tokens": 8857,
             "input_tokens_details": {"cached_tokens": 4096}, "output_tokens_details": {"reasoning_tokens": 34}}
    if with_tool_use:
        usage["server_tool_use"] = {"web_search_requests": searches}
    return {"status": "completed", "usage": usage, "output": [
        {"type": "web_search_call", "action": {"queries": ["EUR USD"]}},
        {"type": "message", "content": [{"type": "output_text", "text": "EUR/USD 1.09 на 24.10.2026", "annotations": [
            {"type": "url_citation", "title": "XE", "url": "https://www.xe.com/c/?utm_source=openai"},
            {"type": "url_citation", "title": "ECB", "url": "https://ecb.europa.eu/rates?a=1&utm_source=openai"},
            {"type": "url_citation", "title": "XE again", "url": "https://www.xe.com/c/?utm_source=openai"},
        ]}]},
    ]}


@pytest.fixture
def rec(monkeypatch):
    rows = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rows.append((a, k)))
    monkeypatch.setattr(bot, "GEMINI_RETRY_DELAY", 0)
    return rows


def run(coro):
    return asyncio.run(coro)


def test_success_sources_clean_and_billing(monkeypatch, rec):
    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return Resp(200, good_body())
    monkeypatch.setattr(bot.http_requests, "post", post)
    text, sources = run(bot._gpt_web_search("курс евро"))
    assert text == "EUR/USD 1.09 на 24.10.2026"
    assert sources == [("xe.com", "https://www.xe.com/c/"), ("ecb.europa.eu", "https://ecb.europa.eu/rates?a=1")]
    assert sent["url"].endswith("/v1/responses") and sent["headers"]["Authorization"].startswith("Bearer ")
    assert "max_output_tokens" not in sent["json"] and "store" not in sent["json"]
    assert sent["json"]["tools"] == [{"type": "web_search"}]
    llm = next(a for a, _ in rec if a[0] == "llm")
    assert llm[1] == "gpt-6-luna" and llm[2] == "gpt_search"
    assert llm[4] == 8769 - 4096 and llm[5] == 88 and llm[7] == 4096     # вход без кэша, выход, кэш-чтение
    assert llm[3] == pytest.approx(bot.calc_llm_cost("gpt-6-luna", 8769 - 4096, 88, cache_read=4096))
    assert len([a for a, _ in rec if a[0] == "search" and a[1] == "gpt"]) == 1


def test_search_rows_follow_web_search_requests_else_calls(monkeypatch, rec):
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, good_body(searches=3)))
    run(bot._gpt_web_search("q"))
    assert len([a for a, _ in rec if a[0] == "search"]) == 3
    rec.clear()
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, good_body(with_tool_use=False)))
    run(bot._gpt_web_search("q"))
    assert len([a for a, _ in rec if a[0] == "search"]) == 1       # по числу web_search_call


def test_text_is_capped(monkeypatch, rec):
    body = good_body()
    body["output"][1]["content"][0]["text"] = "x" * 10000
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    text, _ = run(bot._gpt_web_search("q"))
    assert len(text) == bot.SEARCH_GPT_MAX_CHARS


def test_sources_capped(monkeypatch, rec):
    body = good_body()
    body["output"][1]["content"][0]["annotations"] = [
        {"type": "url_citation", "title": "t", "url": f"https://s{i}.com/x"} for i in range(9)]
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    _, sources = run(bot._gpt_web_search("q"))
    assert len(sources) == bot.SEARCH_GPT_MAX_SOURCES


def test_retry_once_on_5xx_then_none(monkeypatch, rec):
    calls = []
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: calls.append(1) or Resp(503))
    assert run(bot._gpt_web_search("q")) is None
    assert len(calls) == 2


def test_no_retry_on_other_4xx(monkeypatch, rec):
    calls = []
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: calls.append(1) or Resp(401))
    monkeypatch.setattr(bot, "SEARCH_GPT_EFFORT", "")
    assert run(bot._gpt_web_search("q")) is None
    assert len(calls) == 1


def test_400_on_reasoning_retries_without_it(monkeypatch, rec):
    bodies = []

    def post(url, json=None, **k):
        bodies.append(dict(json))
        return Resp(400, text="unsupported reasoning") if "reasoning" in json else Resp(200, good_body())
    monkeypatch.setattr(bot.http_requests, "post", post)
    monkeypatch.setattr(bot, "SEARCH_GPT_EFFORT", "low")
    assert run(bot._gpt_web_search("q")) is not None
    assert "reasoning" in bodies[0] and "reasoning" not in bodies[1]


def test_network_error_returns_none(monkeypatch, rec):
    def boom(*a, **k):
        raise ConnectionError("x")
    monkeypatch.setattr(bot.http_requests, "post", boom)
    assert run(bot._gpt_web_search("q")) is None


def test_empty_text_returns_none_but_bills(monkeypatch, rec):
    body = good_body()
    body["output"] = [{"type": "web_search_call"}]
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    assert run(bot._gpt_web_search("q")) is None
    assert any(a[0] == "llm" for a, _ in rec)                      # токены уже потрачены


def test_sources_line_accepts_labelled_pairs():
    md, plain = bot.sources_line([("xe.com", "https://www.xe.com/a"), ("ecb.europa.eu", "https://ecb.europa.eu/r"),
                                  ("xe.com", "https://xe.com/b"), "https://reuters.com/x"])
    assert plain == "🔗 xe.com · ecb.europa.eu · reuters.com"
    assert "(https://www.xe.com/a)" in md


def test_provider_fallback_to_tavily_when_gpt_none(monkeypatch):
    async def none_search(q):
        return None

    async def aux(fn, *a, label=""):
        return "q" if label == "should_search" else "tavily-result"
    monkeypatch.setattr(bot, "_gpt_web_search", none_search)
    monkeypatch.setattr(bot, "call_claude_aux", aux)
    monkeypatch.setattr(bot, "_search_allowed", lambda *a: True)
    monkeypatch.setattr(bot, "react", lambda *a, **k: None)
    monkeypatch.setattr(bot, "tavily", object())
    msg = SimpleNamespace(message=SimpleNamespace())
    assert run(bot.search_with_provider(msg, 1, 1, False, "курс", "", "gpt")) == ("q", "tavily-result", [])
    monkeypatch.setattr(bot, "tavily", None)
    assert run(bot.search_with_provider(msg, 1, 1, False, "курс", "", "gpt")) == ("", "", [])


@pytest.mark.parametrize("tier,mode,model,expected", [
    ("paid", "auto", "flash", "gpt"), ("paid", "auto", "argon", "gpt"), ("paid", "gpt", "flash", "gpt"),
    ("paid", "tavily", "flash", "tavily"), ("paid", "claude", "flash", "gpt"),   # «claude» из старой БД → auto
    ("free", "gpt", "lite", "tavily"), ("free", "auto", "lite", "tavily"),
])
def test_search_provider_for(monkeypatch, tier, mode, model, expected):
    monkeypatch.setattr(bot, "chat_tier", lambda c: tier)
    monkeypatch.setattr(bot.db, "get_chat_search_mode_db", lambda c: mode)
    monkeypatch.setattr(bot, "SEARCH_PROVIDER_FREE", "tavily")
    assert bot.search_provider_for(1, bot.MODELS[model]["id"]) == expected


def test_search_provider_free_env_gpt(monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot, "SEARCH_PROVIDER_FREE", "gpt")
    assert bot.search_provider_for(1, bot.DEFAULT_MODEL_ID) == "gpt"


def test_utm_stripped_in_text_and_sources(monkeypatch, rec):
    body = good_body()
    body["output"][1]["content"][0]["text"] = "курс ([xe.com](https://www.xe.com/c/?a=1&utm_source=openai)) 1.09"
    body["output"][1]["content"][0]["annotations"] = [
        {"type": "url_citation", "title": "XE", "url": "https://www.xe.com/c/?GL_Campaign_ID=7&utm_source=openai"}]
    monkeypatch.setattr(bot.http_requests, "post", lambda *a, **k: Resp(200, body))
    text, sources = run(bot._gpt_web_search("q"))
    assert "utm_source" not in text and "(https://www.xe.com/c/?a=1)" in text
    assert sources == [("xe.com", "https://www.xe.com/c/?GL_Campaign_ID=7")]

"""Фолбэк дедупликации на ту же модель у Google (2026-11-19, пул выключил Gemini). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts/test_dedup_fallback.py -q
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dedup_memory as dm  # noqa: E402

UNAVAILABLE = ("Gemini models are currently unavailable on this service. Retrying will not help; do not retry "
               "this request and use another model.")


def make_exc(status, text):
    req = httpx.Request("POST", "http://pool/v1/messages")
    resp = httpx.Response(status, request=req, json={"error": {"message": text}})
    cls = {400: anthropic.BadRequestError}.get(status, anthropic.InternalServerError)
    return cls(text, response=resp, body={"error": {"message": text}})


def ok_resp(model, facts):
    import json
    return SimpleNamespace(model=model, stop_reason="end_turn",
                           content=[SimpleNamespace(type="text", text=json.dumps({"facts": facts}, ensure_ascii=False))],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0, cache_creation=None))


class FakeClient:
    def __init__(self, exc=None):
        self.calls, self.exc = [], exc
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw["model"])
        if self.exc:
            raise self.exc
        return ok_resp(kw["model"], ["Любит котов"])


@pytest.fixture
def native(monkeypatch):
    calls = []
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(dm, "_native_create", lambda **kw: calls.append(kw["model"]) or ok_resp(kw["model"], ["Любит котов"]))
    return calls


def groups(n=2):
    return [dm.Group(dm.GroupKey(i, "private", None, "long"), [10 * i, 10 * i + 1],
                     ["Любит котов", "Обожает котов"], None) for i in range(1, n + 1)]


def test_aliases_and_prices():
    assert dm.NATIVE_ALIASES == {"flash_g": "google-direct/gemini-3.8-flash", "argon_g": "google-direct/gemini-4-argon",
                                 "neon_g": "google-direct/gemini-4-neon"}
    for pool, nat in dm.NATIVE_FALLBACK.items():
        assert nat in dm.PRICING and dm.THINKING_HEADROOM[nat] == dm.THINKING_HEADROOM[pool]
        assert dm.is_native(nat) and not dm.is_native(pool)
    assert "gpt-6-luna" not in dm.NATIVE_FALLBACK          # Luna не подменяем другой моделью
    assert set(dm.MODEL_ALIASES.values()).isdisjoint(dm.NATIVE_ALIASES.values())   # TUI видит только пул


def test_pool_400_unavailable_goes_to_google_and_sticks(native):
    client = FakeClient(make_exc(400, UNAVAILABLE))
    state = {}
    res = list(dm.run_llm(groups(2), "gemini-3.8-flash", client, state=state))
    assert [r.status for r in res] == ["ok", "ok"]
    assert all(r.model_used == "google-direct/gemini-3.8-flash" for r in res)
    assert client.calls == ["gemini-3.8-flash"]             # пул спросили один раз, дальше сразу Google
    assert native == ["google-direct/gemini-3.8-flash"] * 2
    assert state["fallback"] == "google-direct/gemini-3.8-flash"
    expected = dm.request_cost("google-direct/gemini-3.8-flash", 100, 50, 0, 0, 0)
    assert res[0].cost == pytest.approx(expected)          # цена — по фактической модели


def test_5xx_also_falls_back(native):
    res = list(dm.run_llm(groups(1), "gemini-4-argon", FakeClient(make_exc(503, "overloaded")), state={}))
    assert res[0].status == "ok" and res[0].model_used == "google-direct/gemini-4-argon"


def test_ordinary_400_no_fallback(native):
    res = list(dm.run_llm(groups(1), "gemini-3.8-flash", FakeClient(make_exc(400, "bad param")), state={}))
    assert res[0].status == "error" and native == []


def test_luna_has_no_fallback(native):
    res = list(dm.run_llm(groups(1), "gpt-6-luna", FakeClient(make_exc(503, "down")), state={}))
    assert res[0].status == "error" and native == []


def test_no_google_key_no_fallback(native, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    res = list(dm.run_llm(groups(1), "gemini-3.8-flash", FakeClient(make_exc(400, UNAVAILABLE)), state={}))
    assert res[0].status == "error" and native == []


def test_pool_ok_no_fallback(native):
    client = FakeClient()
    res = list(dm.run_llm(groups(1), "gemini-3.8-flash", client))
    assert res[0].status == "ok" and res[0].model_used == "gemini-3.8-flash" and native == []


def test_native_model_chosen_directly(native):
    client = FakeClient()
    res = list(dm.run_llm(groups(1), "google-direct/gemini-4-neon", client))
    assert res[0].status == "ok" and client.calls == [] and native == ["google-direct/gemini-4-neon"]

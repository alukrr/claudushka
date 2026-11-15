"""Нативная полоса Google (feat/google-native): модели argon_g / flash_g / lite напрямую через generateContent.
Без сети. docs/claude/models-and-costs.md → «Нативная полоса Google»."""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_errors  # noqa: E402
import bot  # noqa: E402

ARGON_G, FLASH_G, LITE = "google-direct/gemini-4-argon", "google-direct/gemini-3.8-flash", "google-direct/gemini-3.1-flash-lite"
HI = [{"role": "user", "content": "hi"}]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Resp:
    def __init__(self, status, data=None, text=""):
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


def ok(text="Привет!", **meta):
    return Resp(200, {"candidates": [{"content": {"parts": [{"text": "думаю", "thought": True}, {"text": text}]},
                                      "finishReason": "STOP"}],
                      "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 50, "thoughtsTokenCount": 200,
                                        "cachedContentTokenCount": 400, **meta}})


def err(status):
    return Resp(status, {"error": {"message": "nope"}})


@pytest.fixture
def net(monkeypatch):
    calls, answers = [], []
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "gkey")
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)

    def post(url, **k):
        calls.append((url, k))
        r = answers.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(bot.http_requests, "post", post)
    return calls, answers


def test_registry_and_prices():
    assert [bot.MODELS[k]["id"] for k in ("argon_g", "flash_g", "lite")] == [ARGON_G, FLASH_G, LITE]
    assert all(bot.is_native_model(m) for m in (ARGON_G, FLASH_G, LITE))
    assert not bot.is_native_model("gemini-4-argon") and not bot.is_native_model("gpt-6-luna") and not bot.is_native_model(None)
    a = bot.MODELS["argon_g"]
    assert (a["in"], a["out"]) == pytest.approx((2.2, 11.0))                      # прайс Google +10%
    assert (bot.MODELS["flash_g"]["in"], bot.MODELS["flash_g"]["out"]) == pytest.approx((0.825, 4.125))
    assert (bot.MODELS["lite"]["in"], bot.MODELS["lite"]["out"]) == pytest.approx((0.275, 1.65))
    assert bot.model_provider(ARGON_G) == "gemini"
    assert not any(k in bot.FREE_MODEL_KEYS for k in ("argon_g", "flash_g", "lite"))


def test_price_steps_by_date():
    a, f = bot.MODELS["argon_g"], bot.MODELS["flash_g"]
    tz = bot.BERLIN_TZ
    assert bot.effective_price(a, datetime(2026, 11, 30, 23, 59, tzinfo=tz)) == pytest.approx((2.2, 11.0))
    assert bot.effective_price(a, datetime(2026, 12, 1, 0, 0, tzinfo=tz)) == pytest.approx((4.4, 22.0))
    assert bot.effective_price(f, datetime(2026, 12, 31, tzinfo=tz)) == pytest.approx((0.825, 4.125))
    assert bot.effective_price(f, datetime(2027, 1, 1, tzinfo=tz)) == pytest.approx((1.65, 8.25))
    assert bot.effective_price(bot.MODELS["argon"]) == (2.0, 10.0)               # у моделей пула ступеней нет


def test_fallback_pairs_native_to_pool():
    assert bot.FALLBACK_PAIRS[ARGON_G] == "gemini-4-argon"
    assert bot.FALLBACK_PAIRS[FLASH_G] == "gemini-3.8-flash"
    assert bot.FALLBACK_PAIRS[LITE] == "gemini-3.8-flash"
    assert not any(v.startswith("google-direct/") for v in bot.FALLBACK_PAIRS.values())   # фолбэка НА нативные нет


def test_payload_conversion():
    kw = {"model": ARGON_G, "max_tokens": 5000, "temperature": 0.7,
          "system": [{"type": "text", "text": "Ты Клодушка."}, {"type": "text", "text": "Правила."}],
          "messages": [
              {"role": "user", "content": "Привет"},
              {"role": "user", "content": [{"type": "text", "text": "ещё"}]},
              {"role": "assistant", "content": "Ну привет."},
              {"role": "assistant", "content": ""},
              {"role": "user", "content": [
                  {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
                  {"type": "text", "text": "Что тут?"}, {"type": "tool_result", "content": "x"}]}]}
    p = bot.google_native_payload(kw)
    assert p["systemInstruction"] == {"parts": [{"text": "Ты Клодушка.\n\nПравила."}]}
    assert [c["role"] for c in p["contents"]] == ["user", "model", "user"]    # соседние одной роли склеены, пустая выброшена
    assert p["contents"][0]["parts"] == [{"text": "Привет"}, {"text": "ещё"}]
    assert p["contents"][2]["parts"] == [{"inline_data": {"mime_type": "image/png", "data": "QUJD"}}, {"text": "Что тут?"}]
    assert p["generationConfig"] == {"maxOutputTokens": 5000, "temperature": 0.7}
    assert "systemInstruction" not in bot.google_native_payload({"messages": HI})


def test_create_ok_response_shape_and_usage(net):
    calls, answers = net
    answers.append(ok())
    r = bot.google_native_create(model=ARGON_G, max_tokens=100, messages=HI)
    url, k = calls[0]
    assert url.endswith("/v1beta/models/gemini-4-argon:generateContent") and url.startswith(bot.GOOGLE_API_BASE_URL)
    assert k["headers"] == {"x-goog-api-key": "gkey"} and k["timeout"] == bot.GOOGLE_NATIVE_TIMEOUT
    assert api_errors.response_text(r) == "Привет!" and r.model == ARGON_G and r.stop_reason == "end_turn"
    u = r.usage
    assert (u.input_tokens, u.output_tokens, u.cache_read_input_tokens, u.output_tokens_details.thinking_tokens) == (600, 250, 400, 200)
    assert not api_errors.was_truncated(r)


def test_track_response_bills_native_price(net, monkeypatch):
    _, answers = net
    answers.append(ok())
    rec = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rec.append((a, k)))
    monkeypatch.setattr(bot, "effective_price", lambda meta, now=None: (meta["in"], meta["out"]))
    r = bot.google_native_create(model=ARGON_G, messages=HI)
    bot._track_response(ARGON_G, r, "dialog")
    args, kw = rec[0]
    assert args[:3] == ("llm", ARGON_G, "dialog") and args[4:8] == (600, 250, 0, 400) and kw["thinking"] == 200
    assert args[3] == pytest.approx((600 * 2.2 + 400 * 2.2 * 0.05 + 250 * 11.0) / 1e6)


def test_max_tokens_marks_truncated(net):
    _, answers = net
    answers.append(Resp(200, {"candidates": [{"content": {"parts": [{"text": "обрыв"}]}, "finishReason": "MAX_TOKENS"}]}))
    r = bot.google_native_create(model=LITE, messages=HI)
    assert r.stop_reason == "max_tokens" and api_errors.was_truncated(r)


@pytest.mark.parametrize("data", [
    {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
    {"candidates": [{"finishReason": "SAFETY"}]},
])
def test_safety_block_maps_to_existing_flow(net, data):
    _, answers = net
    answers.append(Resp(200, data))
    with pytest.raises(anthropic.BadRequestError) as e:
        bot.google_native_create(model=ARGON_G, messages=HI)
    assert api_errors.is_safety_block(e.value) and not api_errors.should_fallback(e.value)


@pytest.mark.parametrize("status,cls,fallback", [
    (429, anthropic.RateLimitError, True), (503, anthropic.APIStatusError, True), (404, anthropic.NotFoundError, True),
    (400, anthropic.BadRequestError, False), (403, anthropic.PermissionDeniedError, False),
])
def test_http_errors_are_sdk_errors(net, status, cls, fallback):
    _, answers = net
    answers.append(err(status))
    with pytest.raises(cls) as e:
        bot.google_native_create(model=FLASH_G, messages=HI)
    assert e.value.status_code == status and api_errors.should_fallback(e.value) is fallback


def test_prompt_too_long_and_network_and_no_key(net, monkeypatch):
    calls, answers = net
    answers.append(Resp(400, {"error": {"message": "The input token count (1200000) exceeds the maximum number of tokens allowed"}}))
    with pytest.raises(anthropic.BadRequestError) as e:
        bot.google_native_create(model=ARGON_G, messages=HI)
    assert api_errors.is_prompt_too_long(e.value)
    answers.append(bot.http_requests.Timeout("t"))
    with pytest.raises(anthropic.APITimeoutError) as e:
        bot.google_native_create(model=ARGON_G, messages=HI)
    assert api_errors.should_fallback(e.value)
    answers.append(RuntimeError("dns"))
    with pytest.raises(anthropic.APIConnectionError):
        bot.google_native_create(model=ARGON_G, messages=HI)
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "")
    n = len(calls)
    with pytest.raises(anthropic.AuthenticationError):
        bot.google_native_create(model=ARGON_G, messages=HI)
    assert len(calls) == n                                   # без ключа в сеть не ходим


def test_retries_inside_call(net):
    calls, answers = net
    answers.extend([err(429), ok("со второй")])
    r = bot.google_native_create(retries=1, model=LITE, messages=HI)
    assert api_errors.response_text(r) == "со второй" and len(calls) == 2
    answers.append(err(429))
    with pytest.raises(anthropic.RateLimitError):
        bot.google_native_create(retries=0, model=LITE, messages=HI)


def test_dispatch_native_vs_pool(net):
    calls, answers = net
    pool = []
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: pool.append(kw) or "pool"))
    answers.append(ok())
    r = bot._messages_create(fake, {"model": ARGON_G, "messages": HI})
    assert r.model == ARGON_G and pool == [] and len(calls) == 1
    assert bot._messages_create(fake, {"model": "gemini-4-argon", "messages": []}) == "pool" and len(calls) == 1


def test_call_claude_native_then_fallback_to_pool(net, monkeypatch):
    """Google не отвечает (429) → та же модель через пул, метка dialog_fallback, модель в учёте — пуловая."""
    calls, answers = net
    answers.extend([err(429), err(429)])
    pool_resp = SimpleNamespace(model="gemini-4-argon", content=[SimpleNamespace(type="text", text="из пула")],
                                stop_reason="end_turn", usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    pool = []
    monkeypatch.setattr(bot, "client_noretry",
                        SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: pool.append(kw) or pool_resp)))
    monkeypatch.setattr(bot, "_fallback_until", {})
    monkeypatch.setattr(bot, "_fire", lambda coro: coro.close())
    monkeypatch.setattr(bot, "FALLBACK_PRIMARY_DELAYS", (0,))
    rec = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rec.append(a))
    r = run(bot.call_claude(None, None, label="диалог", usage_label="dialog", model=ARGON_G,
                            max_tokens=bot.out_tokens(ARGON_G, 1000), messages=HI))
    assert api_errors.response_text(r) == "из пула" and len(calls) == 2
    assert pool[0]["model"] == "gemini-4-argon" and pool[0]["max_tokens"] == bot.out_tokens("gemini-4-argon", 1000)
    assert rec[0][1:3] == ("gemini-4-argon", "dialog_fallback")

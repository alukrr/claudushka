"""Цепочка фолбэка до двух переходов (issue #5, docs/claude/tz-fallback-chain.md). Без сети: `_messages_create` подменён.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_fallback_chain.py -q
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_errors  # noqa: E402

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

FLASH, LUNA, ARGON = (bot.MODELS[k]["id"] for k in ("flash", "luna", "argon"))
FLASH_G, ARGON_G = (bot.MODELS[k]["id"] for k in ("flash_g", "argon_g"))
NEON, NEON_G = bot.MODELS["neon"]["id"], bot.MODELS["neon_g"]["id"]
TERRA, SOL = "gpt-6.1-terra", "gpt-6.1-sol"
LITE = "google-direct/gemini-3.1-flash-lite"
URL = "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent"


def pool_exc(status: int, text: str = "boom"):
    resp = httpx.Response(status, request=httpx.Request("POST", "http://pool/v1/messages"), json={"error": {"message": text}})
    cls = {400: anthropic.BadRequestError, 401: anthropic.AuthenticationError, 403: anthropic.PermissionDeniedError,
           404: anthropic.NotFoundError, 429: anthropic.RateLimitError}.get(status)
    cls = cls or (anthropic.InternalServerError if status >= 500 else anthropic.APIStatusError)
    return cls(text, response=resp, body={"error": {"message": text}})


def goog_exc(status: int, text: str = "boom"):
    return bot._google_status_error(status, text, URL)


def ok_response(model: str):
    return SimpleNamespace(model=model, content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


# ---------------------------------------------------------------- fallback_chain (чистая)
TABLE = {
    LUNA: [FLASH_G, FLASH], FLASH: [FLASH_G, TERRA], ARGON: [ARGON_G, SOL], NEON: [NEON_G, SOL],
    FLASH_G: [FLASH, TERRA], ARGON_G: [ARGON, SOL], NEON_G: [NEON, SOL], LITE: [FLASH, TERRA],
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    bot._fallback_until.clear()
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", True)
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(bot, "_last_fallback_alert", {})
    alerts = []
    monkeypatch.setattr(bot, "notify_admins", lambda text: alerts.append(text))
    monkeypatch.setattr(bot, "_fire", lambda x: None)

    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(api_errors, "_sleep_with_keepalive", no_sleep)
    monkeypatch.setattr(bot, "_track_response", lambda model, resp, label="aux": None)
    yield alerts
    bot._fallback_until.clear()      # липкие окна — глобальное состояние: не оставлять соседним тестам (Luna → Flash у Google)


def test_chain_table():
    for model, chain in TABLE.items():
        assert bot.fallback_chain(model) == chain, model
        assert len(chain) <= 2 and bot.FALLBACK_CHAINS[model] == tuple(chain)
    assert bot.fallback_chain(TERRA) == [] and bot.fallback_chain(SOL) == [] and bot.fallback_chain("") == []
    assert bot.fallback_chain("claude-haiku-5-5") == [] and bot.fallback_chain("gemini-9-nonexistent") == []


def test_chain_gpt_is_always_last_and_never_for_luna():
    for model, chain in bot.FALLBACK_CHAINS.items():
        gpt = [i for i, m in enumerate(chain) if bot.model_provider(m) == "openai"]
        assert gpt in ([], [len(chain) - 1]), model       # GPT — только последним звеном
    assert not any(bot.model_provider(m) == "openai" for m in bot.FALLBACK_CHAINS[LUNA])   # у Luna GPT-фолбэка нет


def test_chain_without_google_key_skips_native(monkeypatch):
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "")
    assert bot.fallback_chain(FLASH) == [TERRA] and bot.fallback_chain(ARGON) == [SOL] and bot.fallback_chain(LUNA) == [FLASH]
    assert bot.fallback_chain(ARGON_G) == [ARGON, SOL]
    assert not any(bot.is_native_model(m) for chain in map(bot.fallback_chain, bot.FALLBACK_CHAINS) for m in chain)


def test_chain_disabled_and_max_hops(monkeypatch):
    assert bot.fallback_chain(ARGON_G, max_hops=1) == [ARGON] and bot.fallback_chain(ARGON_G, max_hops=0) == []
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", False)
    assert all(bot.fallback_chain(m) == [] for m in bot.FALLBACK_CHAINS)


# ---------------------------------------------------------------- should_fallback по поставщику
@pytest.mark.parametrize("exc,next_model,expected", [
    # Google напрямую: 401/403/429 — всегда
    (goog_exc(401, "GEMINI_API_KEY не задан"), None, True),
    (goog_exc(403, "Permission denied on the service."), None, True),
    (goog_exc(429, "prepayment credits are depleted"), None, True),
    (goog_exc(503, "overloaded"), None, True),
    # Google 400: только с признаком ключа/биллинга (живой ответ 2026-11-25 на неверный ключ — 400)
    (goog_exc(400, "API key not valid. Please pass a valid API key."), None, True),
    (goog_exc(400, "Please enable billing for this project"), None, True),
    (goog_exc(400, "Invalid JSON payload received. Unknown name"), None, False),
    (goog_exc(400, f"{api_errors.PROMPT_TOO_LONG_MARKER}: The input token count exceeds the maximum"), None, False),
    (goog_exc(400, f"{api_errors.SAFETY_BLOCK_MARKER} (Google: SAFETY)"), None, False),
    # пул: 401/403 без признаков — ключ, не повод (как раньше); с признаками апстрима — повод
    (pool_exc(403, "forbidden"), None, False),
    (pool_exc(401, "invalid x-api-key"), None, False),
    (pool_exc(403, "no available channel"), None, True),
    # 402 пула — повод, только если следующее звено у Google (свой кошелёк)
    (pool_exc(402, "insufficient balance"), FLASH_G, True),
    (pool_exc(402, "insufficient balance"), ARGON_G, True),
    (pool_exc(402, "insufficient balance"), TERRA, False),
    (pool_exc(402, "insufficient balance"), None, False),
    # остальное — как было
    (pool_exc(500), None, True), (pool_exc(404), None, True), (pool_exc(429), None, True),
    (pool_exc(400, "bad param"), None, False), (pool_exc(400, "prompt is too long: 9"), None, False),
    (pool_exc(400, "Gemini models are currently unavailable on this service. use another model."), None, True),
])
def test_should_fallback_by_provider(exc, next_model, expected):
    assert api_errors.should_fallback(exc, next_model=next_model) is expected


def test_should_fallback_old_signature_still_works():
    assert api_errors.should_fallback(pool_exc(529)) is True       # один аргумент (dedup_memory.create_with_fallback)


def test_fallback_reason_keys():
    r = api_errors.fallback_reason
    assert r(goog_exc(429, "x"))[0] == "google_quota" and "кончились деньги или квота (HTTP 429)" in r(goog_exc(429, "x"))[1]
    assert r(goog_exc(403, "x"))[0] == "google_key" and r(goog_exc(400, "API key not valid"))[0] == "google_key"
    assert r(goog_exc(503, "x"))[0] == "google_down"
    assert r(pool_exc(402, "insufficient balance")) == ("pool_balance", "пул: кончился баланс (402)")
    assert r(pool_exc(400, "Gemini models are currently unavailable. use another model"))[0] == "pool_model_off"
    assert r(pool_exc(404))[0] == "pool_model_off"
    assert r(pool_exc(502))[0] == "pool_down" and "502" in r(pool_exc(502))[1]
    timeout = anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))
    assert r(timeout, google=True)[0] == "google_down" and r(timeout, google=False)[0] == "pool_down"


# ---------------------------------------------------------------- обход цепочки (async и sync — один сценарий)
class Script:
    """script: {model: [exc | None, ...]} — по одному элементу на вызов этой модели; кончился список — повторять последний.
    Нет модели в script — отвечает."""
    def __init__(self, script):
        self.script = {m: list(v) for m, v in script.items()}
        self.calls = []

    def create(self, **kw):
        m = kw["model"]
        self.calls.append(m)
        steps = self.script.get(m)
        step = None
        if steps:
            step = steps.pop(0) if len(steps) > 1 else steps[0]
        if step is not None:
            raise step
        return ok_response(m)


def install(monkeypatch, script):
    fake = Script(script)
    monkeypatch.setattr(bot, "_messages_create", lambda sdk, kw: fake.create(**kw))
    return fake


def squash(models):
    out = []
    for m in models:
        if not out or out[-1] != m:
            out.append(m)
    return out


def call_async(model, **extra):
    return asyncio.run(bot.call_claude(None, None, label="t", model=model, max_tokens=50, system="s",
                                       messages=[{"role": "user", "content": "hi"}], **extra))


def call_sync(model, **extra):
    return bot.sync_create(label="t", model=model, max_tokens=50, system="s",
                           messages=[{"role": "user", "content": "hi"}], **extra)


BOTH = pytest.mark.parametrize("call", [call_async, call_sync], ids=["async", "sync"])


def outcome(monkeypatch, call, model, script, **extra):
    bot._fallback_until.clear()
    fake = install(monkeypatch, script)
    try:
        r = call(model, **extra)
        res = ("ok", r.model)
    except anthropic.APIStatusError as e:
        res = ("err", e.status_code)
    return res, squash(fake.calls), set(bot._fallback_until)


SCENARIOS = {
    "primary down → link1": (FLASH, {FLASH: [pool_exc(529)]}),
    "primary and link1 down → link2": (FLASH, {FLASH: [pool_exc(529)], FLASH_G: [goog_exc(429, "quota")]}),
    "all three down → last error": (ARGON, {ARGON: [pool_exc(500)], ARGON_G: [goog_exc(503)], SOL: [pool_exc(502)]}),
    "402 pool → Google answers": (FLASH, {FLASH: [pool_exc(402, "insufficient balance")]}),
    "402 pool, Google 403 → GPT": (FLASH, {FLASH: [pool_exc(402, "insufficient balance")], FLASH_G: [goog_exc(403, "x")]}),
    "native: Google key → pool answers": (ARGON_G, {ARGON_G: [goog_exc(400, "API key not valid")]}),
    "400 request: no fallback": (FLASH, {FLASH: [pool_exc(400, "bad param")]}),
    "402 pool GPT-link: no fallback": (TERRA, {TERRA: [pool_exc(402, "insufficient balance")]}),
    "neon chain": (NEON, {NEON: [pool_exc(404)], NEON_G: [goog_exc(429, "x")]}),
    "luna chain without GPT": (LUNA, {LUNA: [pool_exc(500)], FLASH_G: [goog_exc(500)], FLASH: [pool_exc(503)]}),
}


# (результат, звенья по порядку без повторов) — ключ Google задан (autouse-фикстура)
EXPECT = {
    "primary down → link1": (("ok", FLASH_G), [FLASH, FLASH_G]),
    "primary and link1 down → link2": (("ok", TERRA), [FLASH, FLASH_G, TERRA]),
    "all three down → last error": (("err", 502), [ARGON, ARGON_G, SOL]),
    "402 pool → Google answers": (("ok", FLASH_G), [FLASH, FLASH_G]),
    "402 pool, Google 403 → GPT": (("ok", TERRA), [FLASH, FLASH_G, TERRA]),
    "native: Google key → pool answers": (("ok", ARGON), [ARGON_G, ARGON]),
    "400 request: no fallback": (("err", 400), [FLASH]),
    "402 pool GPT-link: no fallback": (("err", 402), [TERRA]),
    "neon chain": (("ok", SOL), [NEON, NEON_G, SOL]),
    "luna chain without GPT": (("err", 503), [LUNA, FLASH_G, FLASH]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_async_and_sync_walk_the_chain_identically(monkeypatch, name):
    model, script = SCENARIOS[name]
    a = outcome(monkeypatch, call_async, model, script)
    s = outcome(monkeypatch, call_sync, model, script)
    assert a == s, (name, a, s)
    assert (a[0], a[1]) == EXPECT[name], (name, a)


@BOTH
def test_primary_down_first_link_answers(monkeypatch, call):
    fake = install(monkeypatch, {FLASH: [pool_exc(529)]})
    assert call(FLASH).model == FLASH_G
    assert squash(fake.calls) == [FLASH, FLASH_G] and FLASH in bot._fallback_until


@BOTH
def test_two_hops_end_on_gpt(monkeypatch, call, clean):
    fake = install(monkeypatch, {FLASH: [pool_exc(529)], FLASH_G: [goog_exc(429, "quota")]})
    assert call(FLASH).model == TERRA
    assert squash(fake.calls) == [FLASH, FLASH_G, TERRA]
    assert {FLASH, FLASH_G} <= set(bot._fallback_until)
    # алерты: причина в тексте, второй переход — отдельной строкой
    assert any("пул не отвечает" in t and FLASH_G in t for t in clean), clean
    assert any(t.startswith("⚠️ " + FLASH + " и " + FLASH_G + " не отвечают") and "Google: кончились деньги или квота (HTTP 429)" in t
               and TERRA in t for t in clean), clean


@BOTH
def test_all_down_raises_last_links_error(monkeypatch, call):
    install(monkeypatch, {ARGON: [pool_exc(500)], ARGON_G: [goog_exc(503)], SOL: [pool_exc(502)]})
    with pytest.raises(anthropic.APIStatusError) as e:
        call(ARGON)
    assert e.value.status_code == 502                 # ошибка ПОСЛЕДНЕГО звена → reply_api_error


@BOTH
def test_sticky_window_of_link_is_skipped_without_call(monkeypatch, call):
    fake = install(monkeypatch, {FLASH: [pool_exc(529)]})
    bot._fallback_until[FLASH_G] = bot.time.monotonic() + 100        # первое звено недавно падало
    assert call(FLASH).model == TERRA
    assert FLASH_G not in fake.calls and squash(fake.calls) == [FLASH, TERRA]


@BOTH
def test_primary_in_window_goes_straight_to_first_live_link(monkeypatch, call):
    fake = install(monkeypatch, {})
    bot._fallback_until[FLASH] = bot.time.monotonic() + 100
    assert call(FLASH).model == FLASH_G and fake.calls == [FLASH_G]
    fake.calls.clear()
    bot._fallback_until[FLASH_G] = bot.time.monotonic() + 100
    assert call(FLASH).model == TERRA and fake.calls == [TERRA]


@BOTH
def test_all_windows_open_still_tries_last_link(monkeypatch, call):
    fake = install(monkeypatch, {})
    for m in (FLASH, FLASH_G, TERRA):
        bot._fallback_until[m] = bot.time.monotonic() + 100
    assert call(FLASH).model == TERRA and fake.calls == [TERRA]


@BOTH
def test_expired_window_returns_to_primary(monkeypatch, call):
    fake = install(monkeypatch, {})
    bot._fallback_until[FLASH] = bot.time.monotonic() - 1
    assert call(FLASH).model == FLASH and fake.calls == [FLASH]


@BOTH
def test_max_hops_one_keeps_gpt_out(monkeypatch, call):
    """Дневной обзор: Argon у Google → Argon пула, GPT в цепочку не входит. Лежат оба — ошибка, не GPT."""
    fake = install(monkeypatch, {ARGON_G: [goog_exc(429, "x")], ARGON: [pool_exc(500)]})
    with pytest.raises(anthropic.APIStatusError):
        call(ARGON_G, max_hops=1)
    assert SOL not in fake.calls
    bot._fallback_until.clear()
    fake = install(monkeypatch, {ARGON_G: [goog_exc(429, "x")]})
    assert call(ARGON_G, max_hops=1).model == ARGON


def test_daily_review_and_dedup_call_with_one_hop():
    assert "max_hops=1" in inspect.getsource(bot.daily_chat_review)
    assert "max_hops=1" in inspect.getsource(bot._DedupClientShim)


def test_fallback_false_means_no_chain(monkeypatch):
    fake = install(monkeypatch, {FLASH: [pool_exc(500)]})
    with pytest.raises(anthropic.APIStatusError):
        call_async(FLASH, fallback=False)
    assert set(fake.calls) == {FLASH} and len(fake.calls) == len(api_errors.RETRY_DELAYS) + 1


def test_disabled_flag_keeps_old_ladder(monkeypatch):
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", False)
    fake = install(monkeypatch, {FLASH: [pool_exc(529)]})
    with pytest.raises(anthropic.APIStatusError):
        call_async(FLASH)
    assert set(fake.calls) == {FLASH} and len(fake.calls) == len(api_errors.RETRY_DELAYS) + 1


# ---------------------------------------------------------------- ретраи и число запросов
def test_retry_ladder_primary_and_middle_one_retry_last_full(monkeypatch):
    fake = install(monkeypatch, {FLASH: [pool_exc(529)], FLASH_G: [goog_exc(503)], TERRA: [pool_exc(502)]})
    with pytest.raises(anthropic.APIStatusError):
        call_async(FLASH)
    n = lambda m: fake.calls.count(m)  # noqa: E731
    assert (n(FLASH), n(FLASH_G), n(TERRA)) == (2, 2, len(api_errors.RETRY_DELAYS) + 1) == (2, 2, 4)
    assert len(fake.calls) == 8                       # потолок HTTP-запросов на один вызов без safety-повтора


def test_single_live_link_gets_full_ladder(monkeypatch):
    fake = install(monkeypatch, {TERRA: [pool_exc(502)]})
    with pytest.raises(anthropic.APIStatusError):
        call_async(TERRA)
    assert len(fake.calls) == 4                       # без цепочки — как раньше, полная лестница


def test_dialog_call_safety_retry_worst_case_is_nine_requests(monkeypatch):
    """_dialog_call: первый запрос и один повтор без истории при safety-блоке. Худший случай — 9 HTTP-запросов."""
    # 1-й вызов: Flash пула 500 ×2 → Flash у Google 500 ×2 → Terra отвечает safety-блоком (1) = 5 запросов;
    # повтор без истории: Flash и Google в липких окнах → остаётся Terra, она всегда 500: полная лестница 4 = 9.
    fake = install(monkeypatch, {FLASH: [pool_exc(500)], FLASH_G: [goog_exc(503)],
                                 TERRA: [pool_exc(400, api_errors.SAFETY_BLOCK_MARKER), pool_exc(502)]})
    msgs = [{"role": "user", "content": "hi"}]
    with pytest.raises(anthropic.APIStatusError):
        asyncio.run(bot._dialog_call(None, 1, 1, False, retry_messages=lambda: msgs,
                                     model=FLASH, max_tokens=50, system="s", messages=msgs))
    assert len(fake.calls) == 9, fake.calls
    assert squash(fake.calls) == [FLASH, FLASH_G, TERRA]


# ---------------------------------------------------------------- алерты: причина и кулдаун по причине
def test_alert_has_reason_and_cooldown_is_per_reason(monkeypatch, clean):
    install(monkeypatch, {ARGON_G: [goog_exc(429, "x")]})
    call_async(ARGON_G)
    assert len(clean) == 1 and "Google: кончились деньги или квота (HTTP 429)" in clean[0] and ARGON in clean[0]
    # та же причина, окно открыто снова, но кулдаун не прошёл → второго алерта нет
    bot._fallback_until.clear()
    call_async(ARGON_G)
    assert len(clean) == 1
    # другая причина (пул лёг) → алерт приходит, «Google без денег» её не глушит
    bot._fallback_until.clear()
    install(monkeypatch, {ARGON: [pool_exc(402, "insufficient balance")]})
    call_async(ARGON)
    assert len(clean) == 2 and "пул: кончился баланс (402)" in clean[1]
    # и ещё одна: пул выключил модель
    bot._fallback_until.clear()
    install(monkeypatch, {FLASH: [pool_exc(400, "Gemini models are currently unavailable. use another model")]})
    call_async(FLASH)
    assert len(clean) == 3 and "пул выключил модель" in clean[2]


def test_alert_repeats_after_cooldown(monkeypatch, clean):
    install(monkeypatch, {ARGON_G: [goog_exc(429, "x")]})
    call_async(ARGON_G)
    bot._fallback_until.clear()
    for k in bot._last_fallback_alert:
        bot._last_fallback_alert[k] -= bot.FALLBACK_ALERT_COOLDOWN + 1
    call_async(ARGON_G)
    assert len(clean) == 2


# ---------------------------------------------------------------- параметры звена
def test_kwargs_for_every_link_derive_from_primary(monkeypatch):
    """max_tokens под звено считается от ОСНОВНОЙ модели: Luna (запас 2000) → Flash у Google (4000) → Flash пула (4000)."""
    seen = {}
    monkeypatch.setattr(bot, "_messages_create", lambda sdk, kw: (seen.setdefault(kw["model"], kw), (_ for _ in ()).throw(pool_exc(500)))[1]
                        if kw["model"] != FLASH else ok_response(FLASH))
    r = bot.sync_create(label="t", model=LUNA, max_tokens=bot.out_tokens(LUNA, 30), system="s",
                        messages=[], thinking={"type": "disabled"})
    assert r.model == FLASH
    assert seen[LUNA]["max_tokens"] == 30 + 2000 and seen[FLASH_G]["max_tokens"] == 30 + 4000

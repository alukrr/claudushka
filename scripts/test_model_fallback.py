"""Юнит-тесты фолбэка между провайдерами пула (ТЗ no-anthropic и lite-gone, docs/claude/model-fallback.md).

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
    (make_exc(400, "Gemini models are currently unavailable on this service. Retrying will not help; do not retry "
                   "this request and use another model."), True),     # пул выключил Gemini, 2026-11-19
])
def test_should_fallback(exc, expected):
    assert api_errors.should_fallback(exc) is expected


def test_should_fallback_midstream_error():
    exc = anthropic.APIStatusError("x", response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
                                   body={"error": {"type": "overloaded_error"}})
    assert api_errors.should_fallback(exc) is True


FLASH, LUNA, ARGON = (bot.MODELS[k]["id"] for k in ("flash", "luna", "argon"))
FLASH_G, ARGON_G = (bot.MODELS[k]["id"] for k in ("flash_g", "argon_g"))
TERRA, SOL = "gpt-6.1-terra", "gpt-6.1-sol"
UNAVAILABLE = "Gemini models are currently unavailable on this service. Retrying will not help; use another model."


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


def test_fallback_first_hop():
    """Первое звено: с 2026-11-19 Gemini пула и Luna → Gemini напрямую у Google (решение Алексея); GPT — только вторым."""
    first = lambda m: bot.fallback_chain(m)[0]  # noqa: E731
    assert first(LUNA) == FLASH_G
    assert first(FLASH) == FLASH_G and first(ARGON) == ARGON_G
    assert first(FLASH_G) == FLASH and first(ARGON_G) == ARGON
    assert bot.fallback_chain(TERRA) == [] and bot.fallback_chain(SOL) == []
    assert bot.fallback_chain("gemini-3.1-flash-lite") == []   # убрана пулом 2026-11-14
    assert bot.fallback_chain("claude-haiku-5-5") == []
    NEON, NEON_G = bot.MODELS["neon"]["id"], bot.MODELS["neon_g"]["id"]
    assert first(NEON) == NEON_G and first(NEON_G) == NEON
    for src, chain in bot.FALLBACK_CHAINS.items():              # первое звено — ДРУГОЙ поставщик (пул <-> Google), у всех есть цена
        assert bot.is_native_model(src) != bot.is_native_model(chain[0])
        assert all(bot.price_meta(m) is not None for m in (src, *chain))


def test_no_google_key_chains_skip_native(monkeypatch):
    """Без GEMINI_API_KEY нативные звенья выкинуты: Luna → Flash пула, Flash → Terra, Argon → Sol (как до 2026-11-19)."""
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "")
    assert bot.fallback_chain(LUNA) == [FLASH]
    assert bot.fallback_chain(FLASH) == [TERRA] and bot.fallback_chain(ARGON) == [SOL]
    assert bot.fallback_chain(ARGON_G) == [ARGON, SOL]             # нативная → пул: ключ не нужен для звеньев пула
    assert bot.fallback_chain(bot.MODELS["neon"]["id"]) == [SOL]
    assert bot.fallback_chain(bot.MODELS["neon_g"]["id"]) == [bot.MODELS["neon"]["id"], SOL]
    for chain in bot.FALLBACK_CHAINS.values():
        assert all(bot.price_meta(m) is not None for m in chain)


def test_luna_to_flash_headroom_is_4000():
    """Переход Luna → Flash (Google): запас на рассуждения заменяется запасом Flash (4000 вместо 2000)."""
    assert bot._headroom(LUNA) == 2000 and bot._headroom(FLASH) == 4000 and bot._headroom(FLASH_G) == 4000
    kw = dict(model=LUNA, max_tokens=bot.out_tokens(LUNA, 100), system="s", messages=[])
    assert kw["max_tokens"] == 2100
    assert bot.kwargs_for_model(kw, FLASH_G)["max_tokens"] == 100 + 4000
    assert bot.kwargs_for_model(kw, FLASH_G)["model"] == FLASH_G


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    bot._fallback_until.clear()
    monkeypatch.setattr(bot, "FALLBACK_ENABLED", True)
    monkeypatch.setattr(bot, "GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(bot, "_last_fallback_alert", {})
    monkeypatch.setattr(bot, "_fire", lambda coro: coro.close())
    async def no_sleep(*a, **k):
        return None
    monkeypatch.setattr(api_errors, "_sleep_with_keepalive", no_sleep)
    tracked = []
    monkeypatch.setattr(bot, "_track_response", lambda model, resp, label="aux": tracked.append(model))
    yield tracked
    bot._fallback_until.clear()      # окна — глобальное состояние: не оставлять соседним тестам


class FakeMessages:
    """Модели из `fail` (по умолчанию — все Gemini пула) падают с primary_exc (если задан), остальные отвечают."""
    def __init__(self, primary_exc=None, fail=None):
        self.calls = []
        self.primary_exc = primary_exc
        self.fail = fail

    def create(self, **kw):
        self.calls.append(kw)
        failing = kw["model"] in self.fail if self.fail is not None else kw["model"].startswith("gemini")
        if failing and self.primary_exc:
            raise self.primary_exc
        return ok_response(kw["model"])


def use_fake(monkeypatch, primary_exc=None, fail=None):
    fake = FakeMessages(primary_exc, fail)
    c = SimpleNamespace(messages=fake)
    monkeypatch.setattr(bot, "client", c)
    monkeypatch.setattr(bot, "client_noretry", c)
    monkeypatch.setattr(bot, "google_native_create", lambda retries=0, **kw: fake.create(**kw))   # нативная полоса — тот же фейк
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


def test_5xx_falls_back_one_retry_then_google(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    assert [c["model"] for c in fake.calls] == [FLASH] * 2 + [FLASH_G]
    assert clean == [FLASH_G]                       # учёт по фактической модели


@pytest.mark.parametrize("model", [FLASH, ARGON])
def test_pool_gemini_off_400_goes_to_google_without_retry(monkeypatch, clean, model):
    """2026-11-19: пул отвечает 400 «Gemini currently unavailable» — без повтора сразу Google, окно липнет."""
    fake = use_fake(monkeypatch, make_exc(400, UNAVAILABLE), fail={FLASH, ARGON})
    r = run(dialog(model))
    assert api_errors.response_text(r) == "ok"
    target = {FLASH: FLASH_G, ARGON: ARGON_G}[model]
    assert [c["model"] for c in fake.calls] == [model, target]
    assert clean == [target] and bot._fallback_plan(model)[0] == target


def test_pool_gemini_off_400_sync_create(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(400, UNAVAILABLE), fail={FLASH})
    r = bot.sync_create(label="x", model=FLASH, max_tokens=4030, system="s", messages=[{"role": "user", "content": "x"}])
    assert api_errors.response_text(r) == "ok" and fake.calls[-1]["model"] == FLASH_G and clean == [FLASH_G]


@pytest.mark.parametrize("model,target", [(LUNA, FLASH_G), (FLASH, FLASH_G), (ARGON, ARGON_G),
                                          (FLASH_G, FLASH), (ARGON_G, ARGON)])
def test_each_model_goes_to_its_pair(monkeypatch, model, target):
    fake = use_fake(monkeypatch, make_exc(500), fail={model})
    run(dialog(model))
    assert fake.calls[-1]["model"] == target


def test_luna_down_answers_via_flash_with_flash_headroom(monkeypatch, clean):
    fake = use_fake(monkeypatch, make_exc(404), fail={LUNA})
    r = run(bot.call_claude(None, None, label="t", model=LUNA, max_tokens=bot.out_tokens(LUNA, 50), system="s",
                            messages=[{"role": "user", "content": "hi"}]))
    assert api_errors.response_text(r) == "ok"
    assert fake.calls[-1]["model"] == FLASH_G and fake.calls[-1]["max_tokens"] == 50 + 4000
    assert clean == [FLASH_G]
    assert bot._fallback_plan(LUNA)[0] == FLASH_G and bot._fallback_plan(FLASH)[0] == FLASH


def test_luna_chain_ends_on_flash_pool(monkeypatch):
    """Luna и Flash у Google лежат вместе: второе звено цепочки — Flash пула (Luna → Flash_g → Flash), GPT там нет."""
    fake = use_fake(monkeypatch, make_exc(500), fail={LUNA, FLASH_G})
    r = run(dialog(LUNA))
    assert api_errors.response_text(r) == "ok" and fake.calls[-1]["model"] == FLASH
    fake = use_fake(monkeypatch, make_exc(500), fail={LUNA, FLASH_G, FLASH})   # лежат все три — ошибка последнего звена
    bot._fallback_until.clear()
    with pytest.raises(anthropic.APIStatusError):
        run(dialog(LUNA))
    assert {c["model"] for c in fake.calls} == {LUNA, FLASH_G, FLASH}


def test_sticky_window_then_return(monkeypatch):
    fake = use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    fake.calls.clear()
    run(dialog(FLASH))                               # окно открыто: основную модель не трогаем вообще
    assert [c["model"] for c in fake.calls] == [FLASH_G]
    bot._fallback_until[FLASH] = time.monotonic() - 1   # окно истекло
    fake.calls.clear()
    fake.primary_exc = None
    run(dialog(FLASH))
    assert [c["model"] for c in fake.calls] == [FLASH]


def test_sticky_is_per_model(monkeypatch):
    use_fake(monkeypatch, make_exc(529))
    run(dialog(FLASH))
    assert bot._fallback_plan(ARGON)[0] == ARGON


@pytest.mark.parametrize("exc", [make_exc(400, "prompt is too long: 9"), make_exc(401, "invalid key")])
def test_no_fallback_for_400_key(monkeypatch, exc):
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
    assert bot.DEFAULT_MODEL_ID == LUNA            # служебные вызовы — Luna; её пара — Flash
    fake = use_fake(monkeypatch, make_exc(500), fail={LUNA})
    r = bot.sync_create(label="x", model=bot.DEFAULT_MODEL_ID, max_tokens=30 + 2000, system="s",
                        messages=[{"role": "user", "content": "x"}], thinking={"type": "disabled"},
                        output_config={"effort": "low"})
    assert api_errors.response_text(r) == "ok"
    assert fake.calls[-1]["model"] == FLASH_G and "thinking" not in fake.calls[-1]
    assert fake.calls[-1]["max_tokens"] == 30 + bot._headroom(FLASH_G) == 4030
    assert clean == [FLASH_G]


def test_prices_present():
    for m in list(bot.FALLBACK_CHAINS) + [x for chain in bot.FALLBACK_CHAINS.values() for x in chain]:
        assert bot.price_meta(m) is not None
    assert bot._billed_model(TERRA, SimpleNamespace(model=TERRA)) == TERRA
    # старые Claude-строки usage_log по-прежнему имеют подпись
    assert bot.price_meta("claude-haiku-5-5")["label"] == "Haiku 5.5"
    assert bot.price_meta("claude-fable-5-1")["label"] == "Fable 5.1"


def test_dedup_pricing_matches_bot():
    """dedup_memory.PRICING — ручная копия MODELS из bot.py (общего модуля нет): расхождение цен/запаса — падение."""
    import dedup_memory as dm
    pool_models = [m for m in bot.MODELS.values() if not m.get("native")]   # MODEL_ALIASES (и TUI) — только пул
    assert set(dm.MODEL_ALIASES.values()) == {m["id"] for m in pool_models}
    # Нативные — NATIVE_ALIASES / NATIVE_FALLBACK (фолбэк дедупа на ту же модель у Google), цена — базовая из MODELS
    native = [bot.MODELS[k] for k in dm.NATIVE_ALIASES]
    assert set(dm.NATIVE_ALIASES.values()) == {m["id"] for m in native} == set(dm.NATIVE_FALLBACK.values())
    for pool_id, nat_id in dm.NATIVE_FALLBACK.items():
        assert bot.FALLBACK_CHAINS[pool_id][0] == nat_id                  # то же первое звено, что у бота
    for m in pool_models + native:
        assert dm.PRICING[m["id"]] == pytest.approx((m["in"], m["out"], m["cache_read_mult"], m["cache_write_5m_mult"],
                                                     m["cache_write_1h_mult"])), m["id"]
        assert dm.THINKING_HEADROOM[m["id"]] == m["thinking_headroom"]


def test_catalog_ids_strips_any_provider_prefix():
    payload = {"data": [{"id": "openai/gpt-6.1-terra"}, {"id": "google/gemini-3.8-flash"},
                        {"id": "gemini-4-argon"}, {"id": "google/gemini-4-neon"}, {"id": "openai/gpt-6-luna"},
                        {"id": "openai/gpt-6.1-sol"}]}
    ids = bot.catalog_ids(payload)
    assert ids == {"gpt-6.1-terra", "gemini-3.8-flash", "gemini-4-argon", "gemini-4-neon", "gpt-6-luna", "gpt-6.1-sol"}
    assert {m for c in bot.FALLBACK_CHAINS.values() for m in c if not bot.is_native_model(m)} <= ids
    assert bot.catalog_ids({}) == set()

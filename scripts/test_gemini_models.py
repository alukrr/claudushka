"""Юнит-тесты реестра моделей после ухода с Anthropic (ТЗ no-anthropic): Gemini-модели, free без выбора, заглушки
/haiku /sonnet /opus /fable, миграция chat_models. Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_gemini_models.py -q
"""
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_model_fallback import make_exc  # noqa: E402  (импортирует и bot)
import bot  # noqa: E402
import db  # noqa: E402

FLASH, ARGON, LITE = (bot.MODELS[k]["id"] for k in ("flash", "argon", "lite"))


def run(coro):
    return asyncio.run(coro)


def test_registry_has_no_claude():
    assert set(bot.MODELS) == {"argon", "flash", "lite"}
    for key, price in (("argon", (2.0, 10.0)), ("flash", (0.75, 3.75)), ("lite", (0.25, 1.50))):
        m = bot.MODELS[key]
        assert m["provider"] == "gemini" and (m["in"], m["out"]) == price
        assert bot.dialog_params(m["id"]) == {} and bot.aux_params(m["id"]) == {}
        assert bot.out_tokens(m["id"], 4096) == 4096 + 4000
        assert not {"effort", "tier_threshold"} & m.keys()
    assert bot.DEFAULT_MODEL_ID == LITE and bot.MODELS[bot.PAID_DEFAULT_MODEL_KEY]["id"] == FLASH
    assert bot.FREE_MODEL_KEYS == ("lite",)
    assert bot.DAILY_REVIEW_MODEL_ID == ARGON
    assert bot.model_provider("claude-opus-5-5") == "anthropic" and bot.model_provider("gpt-6-luna") == "openai"
    assert not {FLASH, ARGON, LITE} & bot.LEGACY_PRICES.keys()   # одна цена на строку


def test_calc_cost_gpt_cache_read():
    cost = bot.calc_llm_cost("gpt-6.1-terra", 1_000_000, 0, cache_read=1_000_000)
    assert cost == pytest.approx(1.0 + 0.05)


def test_get_chat_model(monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: ARGON)    # платный выбор в free не влияет
    assert bot.get_chat_model(1) == LITE
    monkeypatch.setattr(bot, "chat_tier", lambda c: "paid")
    assert bot.get_chat_model(1) == ARGON
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: "claude-sonnet-5-5")   # строка не из реестра
    assert bot.get_chat_model(1) == FLASH
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: d)
    assert bot.get_chat_model(1) == FLASH


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
    """free (не админ): Lite — «и так на ней», Flash — отказ; в chat_models ничего не пишем."""
    writes = []
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot, "is_admin", lambda c: False)
    monkeypatch.setattr(bot.db, "set_chat_model_db", lambda c, m: writes.append(m))
    ctx = SimpleNamespace(args=[], bot=None)
    upd, m = make_update()
    run(bot._set_chat_model(upd, ctx, "lite"))
    assert "и так" in m.sent[-1]
    run(bot._set_chat_model(upd, ctx, "flash"))
    assert "платном режиме" in m.sent[-1] and writes == []


def _paid_setup(monkeypatch, writes, probe):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "paid")
    monkeypatch.setattr(bot, "is_admin", lambda c: False)
    monkeypatch.setattr(bot.db, "set_chat_model_db", lambda c, m: writes.append(m))
    monkeypatch.setattr(bot, "_chat_display", lambda u, c: (True, "этот диалог"))
    monkeypatch.setattr(bot, "_probe_model", probe)


def test_set_model_paid_probe_failure_does_not_write(monkeypatch):
    writes = []

    async def probe(*a, **k):
        raise make_exc(529)
    _paid_setup(monkeypatch, writes, probe)
    upd, m = make_update()
    run(bot._set_chat_model(upd, SimpleNamespace(args=[], bot=None), "argon"))
    assert writes == [] and "недоступна" in m.sent[-1]


def test_set_model_paid_writes(monkeypatch):
    writes = []

    async def probe(*a, **k):
        return None
    _paid_setup(monkeypatch, writes, probe)
    upd, m = make_update()
    run(bot._set_chat_model(upd, SimpleNamespace(args=[], bot=None), "argon"))
    assert writes == [ARGON]


def test_claude_commands_are_stubs():
    upd, m = make_update()
    run(bot.cmd_claude_gone(upd, None))
    assert "/argon" in m.sent[0] and "Claude" in m.sent[0]


def test_migration_chat_models_and_search_mode():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER);
        CREATE TABLE chat_models (chat_id INTEGER PRIMARY KEY, model TEXT NOT NULL);
        CREATE TABLE chat_search_mode (chat_id INTEGER PRIMARY KEY, mode TEXT NOT NULL);
    """)
    rows = [(1, "claude-haiku-5-5"), (2, "claude-sonnet-5-5"), (3, "claude-opus-5-5"), (4, "claude-fable-5-1"),
            (5, "gemini-4-argon")]
    conn.executemany("INSERT INTO chat_models VALUES (?, ?)", rows)
    conn.executemany("INSERT INTO chat_search_mode VALUES (?, ?)", [(1, "claude"), (2, "gpt"), (3, "auto")])
    conn.commit()
    conn.isolation_level = None   # _run_once сам делает BEGIN IMMEDIATE / commit
    db._run_once(conn, "no_anthropic_models", db._mig_no_anthropic)
    db._run_once(conn, "no_anthropic_models", db._mig_no_anthropic)    # идемпотентно
    got = dict(conn.execute("SELECT chat_id, model FROM chat_models").fetchall())
    assert got == {1: LITE, 2: FLASH, 3: ARGON, 4: ARGON, 5: ARGON}
    modes = dict(conn.execute("SELECT chat_id, mode FROM chat_search_mode").fetchall())
    assert modes == {1: "auto", 2: "gpt", 3: "auto"}


def test_default_photo_question_follows_user_language(monkeypatch):
    rows = [{"role": "user", "content": "اريد ترجمه صوره"}, {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "[Фото] Что на этом изображении? Опиши подробно."}]
    monkeypatch.setattr(bot.db, "get_conversation", lambda u, n: rows)
    q = bot._default_photo_question(1, False)
    assert "اريد ترجمه صوره" in q and "не по-русски" in q
    assert bot._default_photo_question(1, True) == bot.PHOTO_DEFAULT_QUESTION          # группа — без изменений
    rows[0]["content"] = "Привет, как дела"
    assert bot._default_photo_question(1, False) == bot.PHOTO_DEFAULT_QUESTION          # русский — как раньше
    monkeypatch.setattr(bot.db, "get_conversation", lambda u, n: [])
    assert bot._default_photo_question(1, False) == bot.PHOTO_DEFAULT_QUESTION

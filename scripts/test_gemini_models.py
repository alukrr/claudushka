"""Юнит-тесты реестра моделей после ухода с Anthropic (ТЗ no-anthropic) и пропажи Flash-Lite (ТЗ lite-gone): Gemini Flash/Argon
и GPT-6 Luna, free без выбора, заглушки /haiku /sonnet /opus /fable и /lite, миграции chat_models, голос. Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_gemini_models.py -q
"""
import asyncio
import os
import re
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

FLASH, ARGON, LUNA = (bot.MODELS[k]["id"] for k in ("flash", "argon", "luna"))
LITE_OLD = "gemini-3.1-flash-lite"   # убрана пулом 2026-11-14


def run(coro):
    return asyncio.run(coro)


def test_registry_has_no_claude():
    assert set(bot.MODELS) == {"argon", "flash", "luna"}
    for key, provider, price, headroom in (("argon", "gemini", (2.0, 10.0), 4000), ("flash", "gemini", (0.75, 3.75), 4000),
                                           ("luna", "openai", (0.10, 0.50), 2000)):
        m = bot.MODELS[key]
        assert m["provider"] == provider and (m["in"], m["out"]) == price
        assert bot.dialog_params(m["id"]) == {} and bot.aux_params(m["id"]) == {}
        assert bot.out_tokens(m["id"], 4096) == 4096 + headroom
        assert not {"effort", "tier_threshold"} & m.keys()
    luna = bot.MODELS["luna"]
    assert luna["id"] == "gpt-6-luna" and luna["label"] == "GPT-6 Luna" and luna["context"] == 872_000
    assert luna["cache_read_mult"] == 0.1 and luna["cache_write_5m_mult"] == 1.25 and luna["cache_write_1h_mult"] == 1.25
    assert bot.DEFAULT_MODEL_KEY == "luna" and bot.DEFAULT_MODEL_ID == LUNA
    assert bot.MODELS[bot.PAID_DEFAULT_MODEL_KEY]["id"] == FLASH
    assert bot.FREE_MODEL_KEYS == ("luna",)
    assert bot.DAILY_REVIEW_MODEL_ID == ARGON
    assert bot.model_provider("claude-opus-5-5") == "anthropic" and bot.model_provider(LITE_OLD) == "gemini"
    assert not {FLASH, ARGON, LUNA} & bot.LEGACY_PRICES.keys()   # одна цена на строку


def test_old_lite_rows_keep_label_and_price():
    """ Старые строки usage_log на Flash-Lite: подпись и цена из LEGACY_PRICES (в /cost не «нет в реестре»)."""
    meta = bot.price_meta(LITE_OLD)
    assert meta["label"] == "Gemini 3.1 Flash-Lite" and (meta["in"], meta["out"]) == (0.25, 1.50)
    assert meta["cache_read_mult"] == 0.1 and meta["cache_write_5m_mult"] == 1.0 and meta["provider"] == "gemini"
    assert bot.calc_llm_cost(LITE_OLD, 1_000_000, 1_000_000) == pytest.approx(1.75)
    assert LITE_OLD not in bot._MODELS_BY_ID                      # выбрать её нельзя
    assert bot.price_meta("gpt-6-luna")["label"] == "GPT-6 Luna"


def test_luna_price_and_cache():
    assert bot.calc_llm_cost(LUNA, 1_000_000, 1_000_000, cache_read=1_000_000) == pytest.approx(0.10 + 0.50 + 0.01)


def test_calc_cost_gpt_cache_read():
    cost = bot.calc_llm_cost("gpt-6.1-terra", 1_000_000, 0, cache_read=1_000_000)
    assert cost == pytest.approx(1.0 + 0.05)


def test_get_chat_model(monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: ARGON)    # платный выбор в free не влияет
    assert bot.get_chat_model(1) == LUNA
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
    """free (не админ): Luna — «и так на ней», Flash — отказ; в chat_models ничего не пишем."""
    writes = []
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot, "is_admin", lambda c: False)
    monkeypatch.setattr(bot.db, "set_chat_model_db", lambda c, m: writes.append(m))
    ctx = SimpleNamespace(args=[], bot=None)
    upd, m = make_update()
    run(bot._set_chat_model(upd, ctx, "luna"))
    assert "и так" in m.sent[-1] and "GPT-6 Luna" in m.sent[-1]
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
    assert "/argon" in m.sent[0] and "Claude" in m.sent[0] and "/luna" in m.sent[0] and "/lite" not in m.sent[0]


def test_lite_command_is_stub_and_luna_command_switches(monkeypatch):
    upd, m = make_update()
    run(bot.cmd_lite_gone(upd, None))
    assert "Flash-Lite убрал поставщик" in m.sent[0] and "/luna" in m.sent[0] and "/models" in m.sent[0]
    assert not hasattr(bot, "cmd_lite")
    seen = []

    async def fake_set(update, context, key):
        seen.append(key)
    monkeypatch.setattr(bot, "_set_chat_model", fake_set)
    run(bot.cmd_luna(upd, None))
    assert seen == ["luna"]


def test_luna_needs_no_probe_in_paid_chat(monkeypatch):
    """Luna — дефолт: пробного запроса перед записью нет (условие по DEFAULT_MODEL_ID, а не по строке ключа)."""
    writes, probes = [], []

    async def probe(*a, **k):
        probes.append(a)
    _paid_setup(monkeypatch, writes, probe)
    upd, m = make_update()
    run(bot._set_chat_model(upd, SimpleNamespace(args=[], bot=None), "luna"))
    assert writes == [LUNA] and probes == []
    run(bot._set_chat_model(upd, SimpleNamespace(args=[], bot=None), "argon"))
    assert writes == [LUNA, ARGON] and len(probes) == 1


def test_models_listing_groups_by_provider(monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda c: "free")
    monkeypatch.setattr(bot, "is_admin", lambda c: False)
    monkeypatch.setattr(bot, "_chat_display", lambda u, c: (True, "этот диалог"))
    monkeypatch.setattr(bot.db, "get_chat_model_db", lambda c, d=None: None)
    upd, m = make_update()
    run(bot.cmd_models(upd, SimpleNamespace(args=[], bot=None)))
    text = m.sent[0]
    assert text.index("Gemini (") < text.index("/argon") < text.index("/flash") < text.index("GPT (") < text.index("/luna")
    assert "▸ /luna" in text and "$0.1/$0.5" in text and "872k" in text
    assert "только Luna" in text and "Lite" not in text
    assert "/lite" not in text


def test_help_mentions_luna_not_lite():
    for text in (bot.USER_HELP, bot.ADMIN_HELP):
        assert "/luna" in text and "GPT-6 Luna" in text
        assert not re.search(r"^\s*/lite", text, re.M)          # в списках команд /lite нет (в ADMIN_HELP — только упоминание заглушки)
    assert "luna → Gemini 3.8 Flash" in bot.ADMIN_HELP
    assert "Luna" in bot.TIER_FREE_MSG and "Lite" not in bot.TIER_FREE_MSG


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
    assert got == {1: LITE_OLD, 2: FLASH, 3: ARGON, 4: ARGON, 5: ARGON}   # id Flash-Lite в миграции заморожен
    modes = dict(conn.execute("SELECT chat_id, mode FROM chat_search_mode").fetchall())
    assert modes == {1: "auto", 2: "gpt", 3: "auto"}


def test_migration_lite_gone_idempotent_on_temp_db(tmp_path):
    """lite_gone: точное сравнение, чужие строки не трогает, повторный запуск ничего не меняет; цепочка с no_anthropic."""
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.executescript("""
        CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER);
        CREATE TABLE chat_models (chat_id INTEGER PRIMARY KEY, model TEXT NOT NULL);
        CREATE TABLE chat_search_mode (chat_id INTEGER PRIMARY KEY, mode TEXT NOT NULL);
    """)
    conn.executemany("INSERT INTO chat_models VALUES (?, ?)", [
        (1, LITE_OLD), (2, "gemini-3.8-flash"), (3, "gemini-4-argon"), (4, "gemini-3.1-flash-lite-x"),
        (5, "claude-haiku-5-5"), (6, "gpt-6-luna")])
    conn.commit()
    conn.isolation_level = None
    db._run_once(conn, "no_anthropic_models", db._mig_no_anthropic)      # бывшие Haiku → id Flash-Lite…
    db._run_once(conn, "lite_gone", db._mig_lite_gone)                    # …и сразу дальше → Luna
    first = dict(conn.execute("SELECT chat_id, model FROM chat_models").fetchall())
    assert first == {1: "gpt-6-luna", 2: FLASH, 3: ARGON, 4: "gemini-3.1-flash-lite-x", 5: "gpt-6-luna", 6: "gpt-6-luna"}
    conn.execute("INSERT INTO chat_models VALUES (7, ?)", (LITE_OLD,))
    db._run_once(conn, "lite_gone", db._mig_lite_gone)                    # маркер есть — второй раз не выполняется
    assert conn.execute("SELECT model FROM chat_models WHERE chat_id = 7").fetchone()[0] == LITE_OLD
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE name = 'lite_gone'").fetchone()[0] == 1
    db._mig_lite_gone(conn)                                               # и сама функция идемпотентна
    assert dict(conn.execute("SELECT chat_id, model FROM chat_models").fetchall())[1] == "gpt-6-luna"
    conn.close()


def test_init_db_creates_luna_default_and_runs_lite_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fresh.db")
    db.init_db()
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = 'lite_gone'").fetchone()
    assert "DEFAULT 'gpt-6-luna'" in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'chat_models'").fetchone()[0]
    conn.close()
    db.init_db()                                                           # повторный старт — без ошибок


def test_audio_transcript_drops_thought_parts():
    parts = [{"text": "Сначала подумаю, что тут сказано.", "thought": True},
             {"text": " Привет, "}, {"text": "как дела?"}, {"text": "ещё размышление", "thought": True}]
    assert bot._audio_text_from_parts(parts) == "Привет, как дела?"
    assert bot._audio_text_from_parts([{"text": "рассуждение", "thought": True}]) == ""
    assert bot._audio_text_from_parts([]) == ""
    assert bot._audio_text_from_parts([{"text": "без флага", "thought": False}]) == "без флага"


def test_audio_model_default_and_usage_counts_thoughts(monkeypatch):
    assert bot.GEMINI_AUDIO_MODEL_NAME == "gemini-3.8-flash" or os.environ.get("GEMINI_AUDIO_MODEL")
    rec = []
    monkeypatch.setattr(bot, "_record_usage", lambda *a, **k: rec.append((a, k)))
    meta = {"promptTokenCount": 1000, "candidatesTokenCount": 100, "thoughtsTokenCount": 300}
    bot._record_gemini_usage(meta, "gemini-3.8-flash", "voice")
    args, kw = rec[0]
    kind, model, label, cost, inp, out = args[:6]
    assert (kind, model, label, inp, out, kw["thinking"]) == ("llm", "gemini-3.8-flash", "voice", 1000, 400, 300)
    assert cost == pytest.approx(1000 / 1e6 * 0.75 + 400 / 1e6 * 3.75)    # рассуждения — как выход, по цене Flash


def test_transcribe_skips_thoughts_end_to_end(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "думаю", "thought": True}, {"text": "Привет!"}]}}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "thoughtsTokenCount": 5}}
    urls = []
    monkeypatch.setattr(bot.http_requests, "post", lambda url, **k: urls.append(url) or Resp())
    monkeypatch.setattr(bot, "_record_gemini_usage", lambda *a, **k: None)
    assert run(bot._transcribe_audio_gemini(b"x", "audio/ogg")) == "Привет!"
    assert bot.GEMINI_AUDIO_MODEL_NAME in urls[0]


def test_dedup_and_wa_use_luna():
    import dedup_memory as dm
    assert dm.MODEL_ALIASES["luna"] == "gpt-6-luna" and "lite" not in dm.MODEL_ALIASES
    # whatsapp.py при импорте требует META_*-переменные — читаем константу из исходника
    src = (Path(__file__).resolve().parent.parent / "whatsapp.py").read_text(encoding="utf-8")
    assert re.search(r'^WA_AUX_MODEL = "gpt-6-luna"', src, re.M)


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

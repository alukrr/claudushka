"""Юнит-тесты реестра настроек чата (feat/settings-registry, docs/claude/tz-settings-registry.md, docs/claude/settings.md).

Дополняют характеризационные `scripts/test_settings_registry.py` (те не трогаем — они доказывают «поведение не изменилось»).
Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_settings_units.py -q
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

GROUP = -1001234567890
OTHER = -1009999999999


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status) VALUES (?, 'T', 'approved')", (GROUP,))
    c.commit()
    c.close()
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    return tmp_path / "t.db"


def cc(key, value=None, *, bot_admin=False, chat_admin=False, target=GROUP, in_target=True, tier="paid"):
    return bot.can_change(key, actor_id=1, is_bot_admin=bot_admin, is_chat_admin=chat_admin, target_chat_id=target,
                          in_target_chat=in_target, tier=tier, value=value)


# ---------- реестр ----------
def test_eight_keys():
    assert set(bot.SETTINGS) == {"model", "image_provider", "persona", "review_persona", "review_enabled",
                                 "search_mode", "activity", "autoignore"}
    for k, s in bot.SETTINGS.items():
        assert s.key == k and s.label and s.scope in ("group", "both") and s.kind in ("choice", "int", "bool")
        assert s.who in ("anyone", "chat_admin") and s.free_policy in ("deny", "admin_saves", "allow")


def test_choices_match_source_registries():
    S = bot.SETTINGS
    assert [v for v, _ in S["model"].choices(0)] == list(bot.MODELS)
    assert [v for v, _ in S["image_provider"].choices(0)] == list(bot.IMAGE_PROVIDERS)
    assert [v for v, _ in S["persona"].choices(0)] == list(bot.PERSONAS)
    assert [v for v, _ in S["review_persona"].choices(0)] == ["off", *bot.PERSONAS]
    assert [v for v, _ in S["search_mode"].choices(0)] == list(bot.SEARCH_MODES)
    assert dict(S["model"].choices(0))["argon"] == bot.MODELS["argon"]["label"]
    assert dict(S["persona"].choices(0))["toxic"] == bot.PERSONAS["toxic"]["label"]
    assert S["activity"].int_range == (0, 100) and S["activity"].choices is None


def test_restricted_values_from_registries():
    assert bot.SETTINGS["image_provider"].restricted_values() == {
        k for k, m in bot.IMAGE_PROVIDERS.items() if m.get("restricted")} == {"bananapro"}
    assert bot.SETTINGS["model"].restricted_values() == {k for k, m in bot.MODELS.items() if m.get("restricted")}


# ---------- can_change: таблица ----------
def test_can_change_foreign_chat_only_bot_admin():
    for key, val in (("model", "argon"), ("persona", "toxic"), ("search_mode", "gpt"), ("activity", "10"),
                     ("review_persona", "toxic"), ("image_provider", "gpt")):
        ok, why = cc(key, val, in_target=False, target=OTHER, chat_admin=True)
        assert not ok and "другого чата" in why, key
        assert cc(key, val, in_target=False, target=OTHER, bot_admin=True)[0], key
    # тексты отказа разные у разных команд (как было)
    assert "Без аргумента команда переключит" in cc("model", "argon", in_target=False, target=OTHER)[1]
    assert "Без аргумента" not in cc("activity", "10", in_target=False, target=OTHER)[1]


def test_can_change_group_only():
    for key, val in (("review_persona", "toxic"), ("review_enabled", True), ("activity", "10"), ("autoignore", "on")):
        ok, why = cc(key, val, target=555, bot_admin=True)       # личка (chat_id > 0), даже админ бота
        assert not ok and why, key
    assert cc("review_persona", None, target=555)[1] == "Обзор бывает только в групповых чатах."
    # админ бота действует на чужую группу из лички
    assert cc("review_persona", "toxic", target=OTHER, in_target=False, bot_admin=True)[0]


def test_can_change_model_free_and_restricted():
    assert cc("model", "argon", tier="free")[1].startswith("Gemini 4 Argon — доступно в платном режиме")
    assert "я и так на" in cc("model", "luna", tier="free")[1]
    assert cc("model", "argon", tier="free", bot_admin=True)[0]            # админ бота сохраняет (с пометкой в команде)
    assert cc("model", "argon", tier="free", chat_admin=True)[0] is False  # админ чата — нет
    ok, why = cc("image_provider", "bananapro")
    assert not ok and "дорогая" in why and "только админы чата и админы бота" in why
    assert cc("image_provider", "bananapro", chat_admin=True)[0] and cc("image_provider", "bananapro", bot_admin=True)[0]
    assert cc("image_provider", "bananapro", target=555)[0]                # личка — сам себе хозяин
    assert cc("image_provider", "gpt")[0]                                  # не restricted


def test_can_change_chat_admin_rules():
    assert not cc("persona", "toxic")[0] and cc("persona", "toxic", chat_admin=True)[0]
    assert cc("persona", "toxic", target=555)[0]                           # личка — сам
    assert not cc("review_persona", "toxic")[0] and cc("review_persona", "toxic", chat_admin=True)[0]
    assert not cc("activity", "10")[0] and cc("activity", "10", chat_admin=True)[0]
    assert cc("activity", None)[0]                                         # смотреть — все
    assert cc("review_persona", None)[0]
    assert cc("search_mode", "gpt")[0] and cc("search_mode", "gpt", target=555)[0]   # любой
    assert not cc("review_enabled", True)[0] and cc("review_enabled", False, chat_admin=True)[0]
    assert not cc("autoignore", None)[0] and cc("autoignore", None, chat_admin=True)[0]   # смотреть — тоже админы


def test_can_change_free_policies():
    assert cc("review_persona", "toxic", tier="free")[1] == "Обзор — платная функция, баланс — /cost"
    assert cc("review_persona", "toxic", tier="free", bot_admin=True)[0]
    assert cc("search_mode", "gpt", tier="free")[1].startswith("В бесплатном режиме поиск")
    # allow: без тарифной проверки
    assert cc("activity", "10", tier="free", chat_admin=True)[0]
    assert cc("review_enabled", True, tier="free", chat_admin=True)[0]
    assert cc("autoignore", "on", tier="free", chat_admin=True)[0]


def test_can_change_value_validation_order():
    assert cc("review_persona", "zzz", chat_admin=True)[1] == bot.REVIEW_PERSONA_USAGE
    assert cc("search_mode", "zzz")[1] == bot.SEARCHMODE_USAGE
    assert cc("activity", "abc", chat_admin=True)[1] == bot.ACTIVITY_NOT_NUMBER
    assert cc("activity", "101", chat_admin=True)[1] == bot.ACTIVITY_OUT_OF_RANGE
    # значение проверяется ДО прав (как в командах) …
    assert cc("review_persona", "zzz")[1] == bot.REVIEW_PERSONA_USAGE
    # … кроме /autoignore: там сначала права
    assert "только для админов" in cc("autoignore", "bogus")[1]
    assert cc("autoignore", "bogus", chat_admin=True)[1] == bot.AUTOIGNORE_USAGE


def test_can_change_is_pure(tmpdb, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("can_change не должна трогать БД")
    monkeypatch.setattr(db, "get_conn", boom)
    for key, val in (("model", "argon"), ("search_mode", "gpt"), ("activity", "5"), ("review_enabled", True),
                     ("image_provider", "bananapro"), ("autoignore", "off")):
        cc(key, val, chat_admin=True)
        cc(key, val, tier="free")


# ---------- apply_setting ----------
def test_apply_invalid_values_refused_without_write(tmpdb):
    cases = [("model", "nope"), ("image_provider", "nope"), ("persona", "nope"), ("review_persona", "nope"),
             ("search_mode", "nope"), ("activity", 101), ("activity", -1), ("activity", "abc"), ("autoignore", "bogus")]
    for key, val in cases:
        ok, why = run(bot.apply_setting(key, GROUP, val))
        assert not ok and why, (key, val)
    c = db.get_conn()
    assert c.execute("SELECT COUNT(*) FROM chat_models").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM chat_image_provider").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM chat_persona").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM chat_review_persona").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM chat_search_mode").fetchone()[0] == 0
    row = c.execute("SELECT activity_percent, autoignore_enabled FROM allowed_chats WHERE chat_id=?", (GROUP,)).fetchone()
    c.close()
    assert tuple(row) == (0, 1)


def test_apply_writes(tmpdb):
    async def go():
        assert (await bot.apply_setting("persona", GROUP, "toxic"))[0]
        assert (await bot.apply_setting("review_persona", GROUP, "granny"))[0]
        assert (await bot.apply_setting("search_mode", GROUP, "tavily"))[0]
        assert (await bot.apply_setting("image_provider", GROUP, "flare"))[0]
        assert (await bot.apply_setting("activity", GROUP, 40))[0]
        assert (await bot.apply_setting("autoignore", GROUP, False))[0]
        assert (await bot.apply_setting("review_enabled", GROUP, False))[0]
        assert (await bot.apply_setting("review_persona", GROUP, "off"))[0]
    run(go())
    assert db.get_chat_persona_db(GROUP) == "toxic" and db.get_review_persona_db(GROUP) is None
    assert db.get_chat_search_mode_db(GROUP) == "tavily" and db.get_chat_image_provider_db(GROUP) == "flare"
    assert db.get_activity_percent(GROUP) == 40 and db.get_autoignore_enabled(GROUP) is False
    assert db.get_chat_row(GROUP)["daily_review_enabled"] == 0


def test_apply_unknown_chat_refused(tmpdb):
    ok, why = run(bot.apply_setting("activity", OTHER, 10))
    assert not ok and "Такого чата нет" in why
    ok, why = run(bot.apply_setting("review_enabled", OTHER, True))
    assert not ok and "ещё не известен боту" in why


def test_apply_model_probe(tmpdb, monkeypatch):
    probes = []

    async def fake_probe(context, chat_id, model_id):
        probes.append((chat_id, model_id))

    monkeypatch.setattr(bot, "_probe_model", fake_probe)
    ok, _ = run(bot.apply_setting("model", GROUP, "argon", context=None, probe_chat_id=777))
    assert ok and probes == [(777, "gemini-4-argon")] and db.get_chat_model_db(GROUP) == "gemini-4-argon"
    # Luna (дефолт) — без пробы
    probes.clear()
    ok, _ = run(bot.apply_setting("model", GROUP, "luna", context=None, probe_chat_id=777))
    assert ok and probes == []


def test_apply_model_probe_failure_writes_nothing(tmpdb, monkeypatch):
    async def bad_probe(context, chat_id, model_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "_probe_model", bad_probe)
    ok, why = run(bot.apply_setting("model", GROUP, "argon", context=None, probe_chat_id=GROUP))
    assert not ok and "сейчас недоступна — модель не меняю" in why
    assert db.get_chat_model_db(GROUP) is None


def test_apply_model_free_skips_probe(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "free")

    async def bad_probe(*a, **k):
        raise AssertionError("в free пробы нет")

    monkeypatch.setattr(bot, "_probe_model", bad_probe)
    ok, _ = run(bot.apply_setting("model", GROUP, "argon"))
    assert ok and db.get_chat_model_db(GROUP) == "gemini-4-argon"


# ---------- get ----------
def test_get_shapes(tmpdb):
    for key, s in bot.SETTINGS.items():
        g = s.get(GROUP)
        assert set(g) == {"value", "stored", "note"}, key
    assert bot.SETTINGS["persona"].get(GROUP) == {"value": "classic", "stored": None, "note": ""}
    db.set_chat_persona_db(GROUP, "toxic")
    assert bot.SETTINGS["persona"].get(GROUP)["value"] == "toxic"
    assert bot.SETTINGS["review_persona"].get(GROUP)["note"] == "как у чата"
    db.set_review_persona_db(GROUP, "granny")
    g = bot.SETTINGS["review_persona"].get(GROUP)
    assert g["value"] == "granny" and g["stored"] == "granny" and g["note"] == ""
    assert bot.SETTINGS["activity"].get(GROUP)["value"] == 0
    assert bot.SETTINGS["autoignore"].get(GROUP)["value"] is True


def test_get_free_notes(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "free")
    assert bot.SETTINGS["model"].get(GROUP)["note"] == "бесплатный режим — всегда Luna"
    assert bot.SETTINGS["model"].get(GROUP)["value"] == "luna"
    assert "активность не работает" in bot.SETTINGS["activity"].get(GROUP)["note"]

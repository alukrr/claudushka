"""Профиль v2 (v2.20.0): многозначные поля дополняются, карточка автора, решения по id (docs/claude/tz-profile-v2.md).

Без сети: БД временная. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_profile_v2.py -q
"""
import os
import sqlite3
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

CHAT = -1001234567890
U = 111


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.get_or_create_user(U, "u111", "User 111")
    return tmp_path / "t.db"


def _prop(field, value, op=None, ev="цитата"):
    d = {"field": field, "value": value, "evidence": ev}
    if op:
        d["op"] = op
    return d


def _pending(field):
    return [dict(r) for r in db.get_conn().execute(
        "SELECT * FROM profile_proposals WHERE field=? AND status='pending' ORDER BY id", (field,))]


def _set(field, value):
    assert db.apply_profile_ops([("set", U, "group", CHAT, field, value)])["set"] == 1


# ---- profile_merge
def test_merge_empty_current():
    assert db.profile_merge("", "дочь") == "дочь"


def test_merge_duplicate_case_insensitive():
    assert db.profile_merge("Муж; дочь", "  ДОЧЬ ") == "Муж; дочь"


def test_merge_full_list_from_model():
    assert db.profile_merge("A, B", "a; B; C") == "a; B; C"


def test_merge_plain_add():
    assert db.profile_merge("муж", "дочь") == "муж; дочь"


def test_merge_mixed_separators():
    assert db.profile_merge("шахматы, лыжи; плавание", "Лыжи, сноуборд") == "шахматы, лыжи; плавание; сноуборд"


# ---- add_profile_proposals
def test_add_already_present_is_same(tmpdb):
    _set("family", "муж; дочь")
    r = db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь")])
    assert r["same"] == 1 and r["new"] == 0 and not _pending("family")


def test_two_adds_merge_into_one_pending(tmpdb):
    _set("family", "муж")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь")])
    r = db.add_profile_proposals(U, "group", CHAT, [_prop("family", "сын")])
    p = _pending("family")
    assert len(p) == 1 and p[0]["new_value"] == "дочь; сын" and p[0]["op"] == "add" and r["superseded"] == 1
    # то же ещё раз — ничего не меняется
    assert db.add_profile_proposals(U, "group", CHAT, [_prop("family", "сын")])["same"] == 1


def test_replace_supersedes(tmpdb):
    _set("family", "муж; дочь")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "сын")])
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь", op="replace")])
    p = _pending("family")
    assert len(p) == 1 and p[0]["new_value"] == "дочь" and p[0]["op"] == "replace"


def test_single_value_field_ignores_op(tmpdb):
    db.add_profile_proposals(U, "group", CHAT, [_prop("city", "Гамбург", op="add")])
    assert _pending("city")[0]["op"] == "replace"


# ---- apply_profile_ops
def _approve_one(field):
    pid = _pending(field)[0]["id"]
    return db.apply_profile_ops([("approve", pid)], by=1), pid


def test_apply_add_over_changed_value(tmpdb):
    _set("family", "муж")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь")])
    _set("family", "муж; сын")          # профиль поправили после предложения
    rep, _ = _approve_one("family")
    assert rep["approved"] == 1 and not rep["failed"]
    assert db.get_profile(U, "group", CHAT)["family"] == "муж; сын; дочь"


def test_apply_overflow_fails_and_stays_pending(tmpdb):
    long = "; ".join(f"родственник номер {i}" for i in range(9))   # < 200
    assert len(long) < db.PROFILE_VALUE_MAX
    _set("family", long)
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "ещё один очень дальний родственник")])
    rep, pid = _approve_one("family")
    assert rep["approved"] == 0 and "переполнено" in rep["failed"][0][1]
    assert db.get_profile(U, "group", CHAT)["family"] == long          # ничего не обрезано и не записано
    assert _pending("family")[0]["id"] == pid


def test_apply_legacy_null_op_adds(tmpdb):
    _set("family", "муж")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь")])
    c = db.get_conn()
    c.execute("UPDATE profile_proposals SET op = NULL")                 # строка дайджеста #5 до миграции
    c.commit()
    c.close()
    rep, _ = _approve_one("family")
    assert rep["approved"] == 1
    assert db.get_profile(U, "group", CHAT)["family"] == "муж; дочь"


def test_apply_replace_still_checks_changed_profile(tmpdb):
    _set("city", "Казань")
    db.add_profile_proposals(U, "group", CHAT, [_prop("city", "Москва")])
    _set("city", "Берлин")
    rep, _ = _approve_one("city")
    assert rep["approved"] == 0 and "изменился" in rep["failed"][0][1]


def test_list_proposals_shows_result(tmpdb):
    _set("family", "муж")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь")])
    r = db.list_proposals()[0]
    assert r["result_value"] == "муж; дочь"
    assert db.profile_after_marked(r["current_value"], r["result_value"]) == "муж; ＋дочь"


# ---- decide_digest(ids)
def _three():
    _set("family", "муж")
    db.add_profile_proposals(U, "group", CHAT, [_prop("family", "дочь"), _prop("city", "Гамбург"),
                                                _prop("profession", "врач")])
    did, rows = db.create_profile_digest()
    return did, {r["field"]: r["id"] for r in rows}


def test_decide_digest_ids(tmpdb):
    did, ids = _three()
    rep = db.decide_digest(did, False, 1, [ids["city"], 99999])
    assert rep["rejected"] == 1 and rep["unknown"] == [99999] and rep["total"] == 3
    rep = db.decide_digest(did, True, 1)                      # остальное — без id
    assert rep["approved"] == 2 and rep["already"] == 1
    prof = db.get_profile(U, "group", CHAT)
    assert prof["family"] == "муж; дочь" and prof["profession"] == "врач" and "city" not in prof
    rep = db.decide_digest(did, True, 1, [ids["city"]])       # уже решённый — в отчёт, не ошибка
    assert rep["approved"] == 0 and rep["already"] == 1 and not rep["failed"]


# ---- миграция
def test_migration_adds_op_column_idempotently(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    db.init_db()
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("ALTER TABLE profile_proposals DROP COLUMN op")
    c.commit()
    c.close()
    db.init_db()
    db.init_db()
    cols = [r[1] for r in db.get_conn().execute("PRAGMA table_info(profile_proposals)")]
    assert "op" in cols


# ---- карточка автора
def test_service_block_speaker_profile():
    t = bot.service_block(speaker_profile="Сейчас пишет Аня.")
    assert "Сейчас пишет Аня." in t
    assert "Сейчас пишет" not in bot.service_block()
    assert "Сейчас пишет" not in bot.service_block(speaker_profile="Сейчас пишет Аня.", spontaneous=True)


def test_speaker_card_limit_and_empty(tmpdb):
    assert bot.speaker_profile_card(U, CHAT, "Аня") == ""
    for f, v in (("name", "Аня"), ("communication", "на «ты», без мата"), ("hobbies", "х" * 190),
                 ("family", "у" * 190), ("profession", "ф" * 190)):
        _set(f, v)
    card = bot.speaker_profile_card(U, CHAT, "Аня")
    assert card.startswith("Сейчас пишет Аня.") and len(card) <= bot.PROFILE_SPEAKER_MAX_CHARS
    assert "Как общаться: на «ты», без мата" in card
    assert bot.service_block(speaker_profile=card).count("Сейчас пишет") == 1

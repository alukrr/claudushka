"""Метки участников P1… не попадают в тексты фактов и профиля (docs/claude/tz-memory-uid.md, ревью Opus).

Без сети. Запуск: TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_memory_uid_tags.py -q
"""
import json
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
import db  # noqa: E402
Path.mkdir = _orig_mkdir

CHAT = -1001234567891
A, B = 111, 222


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (A, B):
        db.get_or_create_user(uid, f"u{uid}", f"User {uid}")
    return tmp_path / "t.db"


def fake_model(monkeypatch, answer):
    calls = []

    def _create(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(answer, ensure_ascii=False))],
                               stop_reason="end_turn")

    monkeypatch.setattr(bot, "sync_create", _create)
    return calls


def facts(uid):
    conn = db.get_conn()
    rows = conn.execute("SELECT fact FROM memory WHERE user_id=? AND context='group' AND chat_id=?", (uid, CHAT)).fetchall()
    conn.close()
    return sorted(r["fact"] for r in rows)


def proposals(uid):
    conn = db.get_conn()
    rows = conn.execute("SELECT field, new_value, evidence FROM profile_proposals WHERE user_id=? AND chat_id=?",
                        (uid, CHAT)).fetchall()
    conn.close()
    return sorted((r["field"], r["new_value"], r["evidence"]) for r in rows)


INFO = {"primary": {"P1": "Аня", "P2": "Борис"}}


def test_replace_tags_plain_and_brackets():
    assert bot.replace_memory_tags("P1 живёт в Бремене, дружит с [P2]", INFO) == ("Аня живёт в Бремене, дружит с Борис", 2)
    assert bot.replace_memory_tags("дружит с [ P2 ]", INFO) == ("дружит с Борис", 1)


def test_text_without_tags_unchanged():
    t = "Живёт в Бремене, работает над проектом APP1 и P-образным профилем"
    assert bot.replace_memory_tags(t, INFO) == (t, 0)


def test_unknown_tag_untouched():
    assert bot.replace_memory_tags("P900 и [P901] не из окна, а P1 из окна", INFO) == ("P900 и [P901] не из окна, а Аня из окна", 1)


def test_system_prompt_has_rule(tmpdb, monkeypatch):
    db.save_group_message(CHAT, A, "Аня", "привет")
    calls = fake_model(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    system = calls[-1]["system"]
    assert "только для поля" in system and "меток не пиши" in system and "по нику" in system


def test_facts_saved_without_tags(tmpdb, monkeypatch):
    db.save_group_message(CHAT, A, "Аня", "я живу в Бремене")
    db.save_group_message(CHAT, B, "Борис", "привет")
    fake_model(monkeypatch, {"participants": [{
        "id": "P1", "long_term": ["P1 живёт в Бремене, дружит с [P2]"], "medium_term": ["Звонил P2", "P900 не из окна"],
        "profile_updates": [{"field": "family", "value": "брат — P2", "evidence": "P1: «мой брат Борис»"}]}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == sorted(["Аня живёт в Бремене, дружит с Борис", "P900 не из окна", "Звонил Борис"])
    assert facts(B) == []
    assert proposals(A) == [("family", "брат — Борис", "Аня: «мой брат Борис»")]
    assert not any("P1" in f or "P2" in f for f in facts(A))


def test_latest_nick_used(tmpdb, monkeypatch):
    db.save_group_message(CHAT, A, "Леша", "привет")
    db.save_group_message(CHAT, A, "Алексей", "я программист")
    fake_model(monkeypatch, {"participants": [{"id": "P1", "long_term": ["P1 — программист"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == ["Алексей — программист"]


def test_replacement_count_logged(tmpdb, monkeypatch, caplog):
    db.save_group_message(CHAT, A, "Аня", "привет")
    fake_model(monkeypatch, {"participants": [{"id": "P1", "long_term": ["P1 — любит чай"], "medium_term": []}]})
    with caplog.at_level("INFO"):
        bot.extract_all_participants_memory(CHAT)
    assert "меток в текстах заменено: 1" in caplog.text

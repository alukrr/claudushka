"""Групповая память и профили — атрибуция по user_id (метки участников P1…), а не по нику (docs/claude/tz-memory-uid.md).

Без сети: модель подменена, БД временная. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_memory_uid.py -q
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

# bot.py при импорте создаёт /app/data (путь контейнера) — вне контейнера подавляем только это.
_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

CHAT = -1001234567890
A, B, C = 111, 222, 333


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (A, B, C):
        db.get_or_create_user(uid, f"u{uid}", f"User {uid}")
    return tmp_path / "t.db"


class FakeModel:
    """Подмена sync_create: запоминает запрос, отдаёт заготовленный JSON."""

    def __init__(self, monkeypatch, answer):
        self.answer = answer
        self.calls = []
        monkeypatch.setattr(bot, "sync_create", self)

    def __call__(self, **kw):
        self.calls.append(kw)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer, ensure_ascii=False)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")

    @property
    def request(self) -> str:
        return self.calls[-1]["messages"][0]["content"]


def say(uid, nick, text, is_bot=False):
    db.save_group_message(CHAT, uid, nick, text, is_bot=is_bot)


def facts(uid, tier=None):
    conn = db.get_conn()
    rows = conn.execute("SELECT fact, tier FROM memory WHERE user_id=? AND context='group' AND chat_id=?",
                        (uid, CHAT)).fetchall()
    conn.close()
    return sorted(r["fact"] for r in rows if tier is None or r["tier"] == tier)


def proposals(uid):
    conn = db.get_conn()
    rows = conn.execute("SELECT field, new_value FROM profile_proposals WHERE user_id=? AND chat_id=?",
                        (uid, CHAT)).fetchall()
    conn.close()
    return sorted((r["field"], r["new_value"]) for r in rows)


def test_nick_taken_over_facts_stay_with_owner(tmpdb, monkeypatch):
    """A пишет как «Мурзик», потом B меняет ник на «Мурзик»; факт для метки A -> у A, у B ничего (раньше — у B)."""
    say(A, "Мурзик", "я живу в Бремене")
    say(B, "Мурзик", "купил велосипед")
    m = FakeModel(monkeypatch, {"participants": [{"id": "P1", "long_term": ["Живёт в Бремене"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == ["Живёт в Бремене"]
    assert facts(B) == []
    assert "РАЗНЫЕ люди" in m.request


def test_two_people_same_nick_two_tags(tmpdb, monkeypatch):
    say(A, "Саша", "я живу в Бремене")
    say(B, "Саша", "я живу в Кёльне")
    m = FakeModel(monkeypatch, {"participants": [
        {"id": "P1", "long_term": ["Живёт в Бремене"], "medium_term": ["Болеет"]},
        {"id": "P2", "long_term": ["Живёт в Кёльне"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == ["Болеет", "Живёт в Бремене"]
    assert facts(B) == ["Живёт в Кёльне"]
    assert facts(A, "medium") == ["Болеет"]
    assert "[P1] Саша: я живу в Бремене" in m.request and "[P2] Саша: я живу в Кёльне" in m.request
    assert "P1 и P2 — РАЗНЫЕ люди с одинаковым ником «Саша»" in m.request


def test_one_person_two_nicks_one_tag(tmpdb, monkeypatch):
    say(A, "Леша", "привет")
    say(A, "Алексей", "я программист")
    m = FakeModel(monkeypatch, {"participants": [{"id": "P1", "long_term": ["Программист"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert "P1 — Алексей (также писал как: Леша)" in m.request
    assert "P2" not in m.request
    assert "РАЗНЫЕ" not in m.request
    assert facts(A) == ["Программист"]


def test_unknown_tag_skipped(tmpdb, monkeypatch, caplog):
    say(A, "Аня", "привет")
    FakeModel(monkeypatch, {"participants": [{"id": "P9", "long_term": ["Чужой факт"], "medium_term": []}]})
    with caplog.at_level("WARNING"):
        bot.extract_all_participants_memory(CHAT)
    assert facts(A) == [] and facts(B) == []
    assert "неизвестная метка" in caplog.text


def test_tag_format_tolerated(tmpdb, monkeypatch):
    say(A, "Аня", "привет")
    FakeModel(monkeypatch, {"participants": [{"id": "[p1]", "long_term": ["Факт"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == ["Факт"]


def test_name_only_unambiguous_used_ambiguous_skipped(tmpdb, monkeypatch):
    say(A, "Аня", "привет")
    say(B, "Саша", "привет")
    say(C, "Саша", "и я")
    FakeModel(monkeypatch, {"participants": [
        {"name": "Аня", "long_term": ["Про Аню"], "medium_term": []},
        {"name": "Саша", "long_term": ["Про какого-то Сашу"], "medium_term": []}]})
    bot.extract_all_participants_memory(CHAT)
    assert facts(A) == ["Про Аню"]
    assert facts(B) == [] and facts(C) == []


def test_rows_without_user_id_not_in_extraction(tmpdb, monkeypatch):
    say(A, "Аня", "я из Бонна")
    say(None, "Аноним", "секретная реплика без uid")
    say(A, "Клодушка", "ответ бота", is_bot=True)
    m = FakeModel(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    assert "секретная реплика" not in m.request and "ответ бота" not in m.request
    assert "[P1] Аня: я из Бонна" in m.request


def test_only_rows_without_uid_no_call(tmpdb, monkeypatch):
    say(None, "Аноним", "привет")
    m = FakeModel(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    assert m.calls == []


def test_profile_proposals_go_to_uid_by_tag(tmpdb, monkeypatch):
    say(A, "Мурзик", "я живу в Бремене")
    say(B, "Мурзик", "привет")
    FakeModel(monkeypatch, {"participants": [
        {"id": "P1", "long_term": [], "medium_term": [],
         "profile_updates": [{"field": "city", "value": "Бремен", "evidence": "я живу в Бремене"}]}]})
    bot.extract_all_participants_memory(CHAT)
    assert proposals(A) == [("city", "Бремен")]
    assert proposals(B) == []


def test_profile_prompt_labels(tmpdb, monkeypatch):
    """Текущий профиль в запросе подписан меткой «P1 (Ник)» — профиль того же человека, что и в транскрипте."""
    say(A, "Мурзик", "привет")
    say(B, "Мурзик", "привет")
    conn = db.get_conn()
    conn.execute("INSERT INTO user_profile (user_id, context, chat_id, field, value, source, updated_at) "
                 "VALUES (?, 'group', ?, 'city', 'Бремен', 'admin', 0)", (A, CHAT))
    conn.commit()
    conn.close()
    m = FakeModel(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    assert "P1 (Мурзик): city=Бремен" in m.request
    assert "P2 (Мурзик): (пусто)" in m.request


def test_system_prompt_mentions_tags(tmpdb, monkeypatch):
    say(A, "Аня", "привет")
    m = FakeModel(monkeypatch, {"participants": []})
    bot.extract_all_participants_memory(CHAT)
    system = m.calls[-1]["system"]
    assert "[P1], [P2]" in system and '"id"' in system


def test_name_lookup_function_removed():
    assert not hasattr(db, "get_user_id_by_name_in_chat")


def test_get_group_transcript_has_user_id_and_dialog_unchanged(tmpdb, monkeypatch):
    say(A, "Аня", "привет")
    say(None, "Аноним", "без uid")
    say(A, "Клодушка", "ответ", is_bot=True)
    tr = db.get_group_transcript(CHAT, 10)
    assert [e["user_id"] for e in tr] == [A, None, A]
    # build_group_messages ключ user_id не читает: выход тот же с ключом и без него
    out_with = bot.build_group_messages(CHAT, "", 10)
    stripped = [{k: v for k, v in e.items() if k != "user_id"} for e in tr]
    monkeypatch.setattr(db, "get_group_transcript", lambda *a, **k: stripped)
    assert bot.build_group_messages(CHAT, "", 10) == out_with

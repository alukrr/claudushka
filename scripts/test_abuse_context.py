"""Проверка на травлю судит ТЕКУЩЕЕ обращение, прошлое — контекст (инцидент 2026-11-02, Москвичи; решение Opus). Без сети.
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_abuse_context.py -q
"""
import asyncio
import os
import sys
import time
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

CHAT = -1001079751651
UID = 359830490
OTHER = 7242378
BOT = 1


@pytest.fixture
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(bot, "context_bot_id", BOT)


def at(user_id, text, ts, is_bot=False):
    c = db.get_conn()
    c.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, is_bot, timestamp) VALUES (?,?,?,?,?,?)",
              (CHAT, user_id, "x", text, 1 if is_bot else 0, ts))
    c.commit()
    c.close()


class Msg:
    def __init__(self, text=""):
        self.text = text
        self.sent = []

    async def reply_text(self, text, **k):
        self.sent.append(text)


def run_check(monkeypatch, verdict, text):
    calls = []

    async def fake_aux(**kw):
        calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=verdict)], stop_reason="end_turn")
    monkeypatch.setattr(bot, "aux_create", fake_aux)
    m = Msg(text)
    asyncio.run(bot._abuse_check_task(m, CHAT, UID, "Вася"))
    return calls, m


def test_rudeness_to_other_person_not_in_input(tmpdb, monkeypatch):
    """Инцидент: «ты нам замкадышную залупу шлёшь» (к человеку, бот не отвечал) + безобидное обращение к боту."""
    now = int(time.time())
    at(OTHER, "В соседнем чате пришло", now - 600)
    at(UID, "А ты нам замкадышную залупу шлёшь", now - 590)
    at(OTHER, "и мята лаунж", now - 580)
    at(UID, "Клодушка, Марина права или это уже что-то на бабушкином", now - 5)
    calls, m = run_check(monkeypatch, '{"abuse": false}', "Клодушка, Марина права или это уже что-то на бабушкином")
    payload = calls[0]["messages"][0]["content"]
    assert "замкадышную" not in payload and "мята" not in payload
    assert f"{bot.ABUSE_MARK}: Клодушка, Марина права" in payload
    assert not m.sent and db.get_ignore(CHAT, UID) is None


def test_payload_marks_current_message(tmpdb, monkeypatch):
    now = int(time.time())
    at(UID, "ты тупой бот", now - 100)
    at(BOT, "сам такой", now - 99, is_bot=True)
    at(UID, "заткнись, железка", now - 5)
    calls, _ = run_check(monkeypatch, '{"abuse": false}', "заткнись, железка")
    payload = calls[0]["messages"][0]["content"]
    assert payload.startswith("КОНТЕКСТ:") and "ты тупой бот" in payload and "сам такой" in payload
    assert payload.rstrip().endswith(f"{bot.ABUSE_MARK}: заткнись, железка")
    assert payload.count("заткнись, железка") == 1          # оцениваемое не дублируется в контексте


def test_old_and_before_strike_not_in_input(tmpdb, monkeypatch):
    now = int(time.time())
    old = now - (bot.ABUSE_CONTEXT_WINDOW_MIN + 5) * 60
    at(UID, "древняя грубость", old)
    at(BOT, "ответ на древнюю", old + 1, is_bot=True)
    at(UID, "до наказания", now - 400)
    at(BOT, "ответ до наказания", now - 399, is_bot=True)
    db.set_ignore(CHAT, UID, 1, None, now - 300)
    at(UID, "после наказания", now - 100)
    at(BOT, "ответ после", now - 99, is_bot=True)
    at(UID, "текущее", now - 5)
    calls, _ = run_check(monkeypatch, '{"abuse": false}', "текущее")
    payload = calls[0]["messages"][0]["content"]
    assert "древняя" not in payload and "до наказания" not in payload
    assert "после наказания" in payload and "ответ после" in payload


def test_repeated_explicit_abuse_context_passed(tmpdb, monkeypatch):
    now = int(time.time())
    for i, t in enumerate(["ты тупая железка", "бесполезный кусок кода", "заткнись уже, дура"]):
        at(UID, t, now - 300 + i * 10)
        at(BOT, f"ответ {i}", now - 299 + i * 10, is_bot=True)
    at(UID, "ну ты и ничтожество", now - 5)
    calls, m = run_check(monkeypatch, '{"abuse": true, "reason": "повторная травля"}', "ну ты и ничтожество")
    payload = calls[0]["messages"][0]["content"]
    for t in ("ты тупая железка", "бесполезный кусок кода", "заткнись уже, дура", "ответ 2"):
        assert t in payload
    assert db.get_ignore(CHAT, UID)["level"] == 1
    assert "за оскорбления в адрес бота" in m.sent[-1]       # предупреждение — ответом на оцениваемое сообщение


def test_unanswered_user_lines_dropped_answered_kept(tmpdb):
    now = int(time.time())
    at(UID, "болтовня без ответа бота", now - 300)
    at(UID, "обращение с ответом", now - 200)
    at(BOT, "ответ бота", now - 199, is_bot=True)
    at(UID, "текущее", now - 5)
    rows, cur = db.get_abuse_context(CHAT, UID, window_start_ts=now - 3600, current_text="текущее")
    assert cur == "текущее"
    assert [r["text"] for r in rows] == ["обращение с ответом", "ответ бота"]


def test_service_notes_do_not_count_as_bot_answer(tmpdb):
    now = int(time.time())
    at(UID, "реплика", now - 200)
    at(BOT, "(служебное: предупреждение бота для Вася о повторах)", now - 199, is_bot=True)
    at(UID, "текущее", now - 5)
    rows, cur = db.get_abuse_context(CHAT, UID, window_start_ts=now - 3600, skip_bot_prefixes=bot._abuse_skip_prefixes(),
                                     current_text="текущее")
    assert rows == [] and cur == "текущее"


def test_current_text_not_in_db_is_kept_as_is(tmpdb):
    """Подпись/голос: текущего нет в group_messages дословно — оцениваем переданный текст, строки не теряем."""
    now = int(time.time())
    at(UID, "обращение", now - 100)
    at(BOT, "ответ", now - 99, is_bot=True)
    rows, cur = db.get_abuse_context(CHAT, UID, window_start_ts=now - 3600, current_text="другой текст")
    assert cur == "другой текст" and [r["text"] for r in rows] == ["обращение", "ответ"]


def test_own_limit(tmpdb):
    now = int(time.time())
    for i in range(10):
        at(UID, f"реплика {i}", now - 500 + i * 10)
        at(BOT, f"ответ {i}", now - 499 + i * 10, is_bot=True)
    at(UID, "текущее", now - 5)
    rows, _ = db.get_abuse_context(CHAT, UID, window_start_ts=now - 3600, current_text="текущее", own_limit=3)
    assert [r["text"] for r in rows if not r["is_bot"]] == ["реплика 7", "реплика 8", "реплика 9"]


def test_prompt_phrases():
    s = bot._ABUSE_SYSTEM
    assert "Оценивай только помеченное сообщение" in s
    assert "Грубость в адрес других людей — не травля бота" in s
    assert "Одна резкая реплика сама по себе — не травля; травля — когда это целенаправленно и повторяется" in s
    assert bot.ABUSE_MARK in s and bot.ABUSE_CONTEXT_WINDOW_MIN == 60

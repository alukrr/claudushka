"""Юнит-тесты отдельного характера обзора и записи обзора в историю (feat/review-persona, docs/claude/tz-review-persona.md).

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_review_persona.py -q
"""
import os
import sys
import time
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


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    return tmp_path / "t.db"


def test_db_set_get_clear(tmpdb):
    assert db.get_review_persona_db(CHAT) is None
    db.set_review_persona_db(CHAT, "toxic")
    assert db.get_review_persona_db(CHAT) == "toxic"
    db.set_review_persona_db(CHAT, "granny")
    assert db.get_review_persona_db(CHAT) == "granny"
    db.clear_review_persona_db(CHAT)
    assert db.get_review_persona_db(CHAT) is None


def test_no_row_follows_chat(tmpdb):
    db.set_chat_persona_db(CHAT, "business")
    assert bot.get_review_persona(CHAT) == "business"


def test_separate_review_persona(tmpdb):
    db.set_chat_persona_db(CHAT, "classic")
    db.set_review_persona_db(CHAT, "toxic")
    assert bot.get_review_persona(CHAT) == "toxic"
    assert bot.get_chat_persona(CHAT) == "classic"


def test_unknown_key_falls_back_db_untouched(tmpdb):
    db.set_chat_persona_db(CHAT, "granny")
    db.set_review_persona_db(CHAT, "nope")
    assert bot.get_review_persona(CHAT) == "granny"
    assert db.get_review_persona_db(CHAT) == "nope"


def test_review_style_from_review_persona(tmpdb):
    db.set_chat_persona_db(CHAT, "classic")
    db.set_review_persona_db(CHAT, "toxic")
    assert bot.persona_review_style(CHAT) == bot.PERSONAS["toxic"]["review_style"]
    db.set_review_persona_db(CHAT, "business")
    meta = bot.PERSONAS["business"]
    assert bot.persona_review_style(CHAT) == (meta.get("review_style") or meta["tone"])


def test_review_system_label(tmpdb):
    assert "режим характера обзора" in bot._review_system(CHAT)


REVIEW = (
    "Сегодня чат жил бурной жизнью.\n\n"
    "🔥 Битва за пиццу\nУтром Вася и Петя спорили про ананасы и никто не победил.\n\n"
    "— Ночной марафон\nК полуночи все делали вид, что спят.\n\n"
    "Итог дня: пицца всегда права."
)


def test_history_text_prefix_and_titles():
    t = bot._review_history_text(7, REVIEW)
    assert t.startswith("(обзор дня по 7 ")
    assert "Битва за пиццу" in t and "Ночной марафон" in t
    assert "ананасы" not in t and "делали вид" not in t
    assert "пицца всегда права" not in t   # итоговая фраза
    assert "🔥" not in t and "— Ночной" not in t


def test_history_text_two_paragraphs_keeps_last():
    t = bot._review_history_text(3, "Первая тема\nтекст\n\nВторая тема\nтекст")
    assert "Первая тема" in t and "Вторая тема" in t


def test_history_text_long_review_capped():
    review = "\n\n".join(f"Тема номер {i} " + "очень длинный заголовок " * 10 + "\nтело" for i in range(50))
    t = bot._review_history_text(40, review)
    assert len(t) <= bot.REVIEW_HISTORY_MAX_CHARS
    assert t.startswith("(обзор дня по 40 ")


def test_history_title_clipped():
    t = bot._review_history_text(5, "а" * 300 + "\nтело\n\nвторая\n\nитог")
    assert "а" * 81 not in t and "…" in t


def test_build_review_log_drops_history_note():
    note = bot._review_history_text(7, REVIEW)
    rows = [
        {"ts": int(time.time()), "is_bot": 1, "sender": "Клодушка", "text": note},
        {"ts": int(time.time()), "is_bot": 0, "sender": "Вася", "text": "привет"},
    ]
    log = bot.build_review_log(rows)
    assert len(log) == 1 and "Вася" in log[0]["line"]


# ---- команда /review_persona ----
import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402


def _call(monkeypatch, args, *, admin=False, chat_admin=False, chat_type="supergroup", tier="paid", chat_id=CHAT):
    sent = []

    async def reply(text, **k):
        sent.append(text)

    async def fake_chat_admin(*a, **k):
        return chat_admin

    monkeypatch.setattr(bot, "is_admin", lambda uid: admin)
    monkeypatch.setattr(bot, "is_chat_admin", fake_chat_admin)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: tier)
    monkeypatch.setattr(bot, "_chat_display", lambda u, cid: (True, f"Чат {cid}"))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=1),
                          effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                          effective_message=SimpleNamespace(reply_text=reply))
    asyncio.run(bot.cmd_review_persona(upd, SimpleNamespace(args=args)))
    return sent[-1]


def test_cmd_show_for_anyone(tmpdb, monkeypatch):
    out = _call(monkeypatch, [])
    assert "как у чата" in out and "▸" in out


def test_cmd_set_by_chat_admin_and_off(tmpdb, monkeypatch):
    out = _call(monkeypatch, ["toxic"], chat_admin=True)
    assert db.get_review_persona_db(CHAT) == "toxic" and "характер обзора" in out
    assert "отдельный режим" in _call(monkeypatch, [])
    _call(monkeypatch, ["off"], chat_admin=True)
    assert db.get_review_persona_db(CHAT) is None


def test_cmd_plain_member_denied(tmpdb, monkeypatch):
    _call(monkeypatch, ["toxic"])
    assert db.get_review_persona_db(CHAT) is None


def test_cmd_unknown_key_usage(tmpdb, monkeypatch):
    out = _call(monkeypatch, ["zzz"], chat_admin=True)
    assert "off" in out and db.get_review_persona_db(CHAT) is None


def test_cmd_private_denied_without_chat_id(tmpdb, monkeypatch):
    out = _call(monkeypatch, ["toxic"], admin=True, chat_type="private", chat_id=5)
    assert "только в групповых" in out


def test_cmd_foreign_chat_admin_only(tmpdb, monkeypatch):
    _call(monkeypatch, ["toxic", "-100777"], admin=False, chat_admin=True)
    assert db.get_review_persona_db(-100777) is None
    _call(monkeypatch, ["toxic", "-100777"], admin=True)
    assert db.get_review_persona_db(-100777) == "toxic"


def test_cmd_free_denied_for_non_admin_allowed_for_admin(tmpdb, monkeypatch):
    out = _call(monkeypatch, ["toxic"], chat_admin=True, tier="free")
    assert "платная функция" in out and db.get_review_persona_db(CHAT) is None
    out = _call(monkeypatch, ["toxic"], admin=True, tier="free")
    assert db.get_review_persona_db(CHAT) == "toxic" and "после пополнения" in out

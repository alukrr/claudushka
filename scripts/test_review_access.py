"""Юнит-тесты доступа к /review и кулдауна (issue #7, docs/claude/tz-review-chat-admins.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_review_access.py -q
"""
import asyncio
import os
import sys
import time
from datetime import datetime
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

CHAT = -1001234567890
PFX = f"({bot.REVIEW_SENT_LABEL}"


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path / "t.db"


def _put(ts, content, is_bot=True, chat=CHAT):
    conn = db.get_conn()
    conn.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, is_bot, timestamp) VALUES (?,?,?,?,?,?)",
                 (chat, 1, "x", content, 1 if is_bot else 0, ts))
    conn.commit()
    conn.close()


def test_last_review_ts_none_is_zero(tmpdb):
    assert db.get_last_review_ts(CHAT, PFX) == 0


def test_last_review_ts_ignores_human_and_other_chat(tmpdb):
    _put(1000, bot._review_sent_note(9), is_bot=False)             # человек написал тот же текст
    _put(2000, bot._review_sent_note(9), chat=CHAT - 1)            # чужой чат
    _put(3000, "обычная реплика бота")
    assert db.get_last_review_ts(CHAT, PFX) == 0


def test_last_review_ts_takes_latest(tmpdb):
    _put(1000, bot._review_sent_note(9))
    _put(5000, bot._review_sent_note(12) + " Темы обзора: …")
    _put(3000, bot._review_sent_note(7))
    _put(9000, "реплика бота после обзора")
    assert db.get_last_review_ts(CHAT, PFX) == 5000


def test_last_review_ts_prefix_not_a_pattern(tmpdb):
    _put(1000, bot._review_sent_note(9))
    assert db.get_last_review_ts(CHAT, "(%") == 0
    assert db.get_last_review_ts(CHAT, "(обзор _н") == 0
    assert db.get_last_review_ts(CHAT, "%") == 0


# --- порядок проверок cmd_review ---

class _Msg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _upd(user_id, chat_type="supergroup"):
    msg = _Msg()
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=CHAT, type=chat_type),
                           message=msg, effective_message=msg), msg


def _run(monkeypatch, *, admin=False, chat_admin=False, tier="paid", last=0, human=0, chat_type="supergroup", now=None):
    monkeypatch.setattr(bot, "is_admin", lambda uid: admin)

    async def _ica(ctx, cid, uid):
        return chat_admin
    monkeypatch.setattr(bot, "is_chat_admin", _ica)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: tier)
    monkeypatch.setattr(db, "get_last_review_ts", lambda cid, p: last)
    monkeypatch.setattr(bot, "collect_review_entries", lambda cid: ([], human))
    if now is not None:
        monkeypatch.setattr(bot.time, "time", lambda: now)
    upd, msg = _upd(42, chat_type)
    asyncio.run(bot.cmd_review(upd, SimpleNamespace(bot=SimpleNamespace())))
    return msg.replies


def test_bot_admin_passes_in_free_and_cooldown(monkeypatch):
    now = 1_800_000_000
    r = _run(monkeypatch, admin=True, tier="free", last=now - 60, now=now)
    assert r == ["Маловато сообщений для обзора."]


def test_member_denied_by_rights(monkeypatch):
    r = _run(monkeypatch, chat_admin=False)
    assert len(r) == 1 and "только для админов чата" in r[0]


def test_private_chat_denied(monkeypatch):
    r = _run(monkeypatch, chat_admin=True, chat_type="private")
    assert r == ["/review работает только в групповых чатах."]


def test_chat_admin_in_free_denied_by_tier(monkeypatch):
    r = _run(monkeypatch, chat_admin=True, tier="free")
    assert r == ["Обзор — в платном режиме: пополнить — /pay."]


def test_manager_in_cooldown_denied_with_time(monkeypatch):
    now = 1_800_000_000
    last = now - 59 * 60
    r = _run(monkeypatch, chat_admin=True, last=last, now=now)
    t0 = datetime.fromtimestamp(last, bot.BERLIN_TZ)
    t1 = datetime.fromtimestamp(last + 3600, bot.BERLIN_TZ)
    assert r == [f"Обзор уже был в {t0:%H:%M}, следующий — после {t1:%H:%M}."]


def test_manager_after_cooldown_reaches_next_check(monkeypatch):
    now = 1_800_000_000
    r = _run(monkeypatch, chat_admin=True, last=now - 61 * 60, now=now)
    assert r == ["Маловато сообщений для обзора."]


def test_no_previous_review_reaches_next_check(monkeypatch):
    assert _run(monkeypatch, chat_admin=True, last=0) == ["Маловато сообщений для обзора."]


def test_cooldown_constant_and_prefix_passed(monkeypatch):
    assert bot.REVIEW_CMD_COOLDOWN_MIN == 60
    seen = []
    monkeypatch.setattr(bot, "is_admin", lambda uid: False)

    async def _ica(ctx, cid, uid):
        return True
    monkeypatch.setattr(bot, "is_chat_admin", _ica)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    monkeypatch.setattr(db, "get_last_review_ts", lambda cid, p: seen.append(p) or 0)
    monkeypatch.setattr(bot, "collect_review_entries", lambda cid: ([], 0))
    upd, _ = _upd(42)
    asyncio.run(bot.cmd_review(upd, SimpleNamespace(bot=SimpleNamespace())))
    assert seen == [PFX]

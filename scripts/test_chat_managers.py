"""Тесты управляющих чата (feat/chat-managers, docs/claude/tz-chat-managers.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_chat_managers.py -q
"""
import asyncio
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

CHAT = -1001234567890
BOT_ADMIN, TG_ADMIN, MANAGER, PLAIN, OTHER = 1, 2, 3, 4, 5
BOT_ID = 999


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status) VALUES (?, 'T', 'approved')", (CHAT,))
    c.commit()
    c.close()
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == BOT_ADMIN)
    return tmp_path / "t.db"


def make_ctx(statuses, args=None, fail=False):
    """statuses: {user_id: status}; для остальных — member. fail — get_chat_member падает."""
    async def get_member(chat_id, uid):
        if fail:
            raise RuntimeError("telegram down")
        st = statuses.get(uid, "member")
        return SimpleNamespace(status=st, is_member=True,
                               user=SimpleNamespace(id=uid, full_name=f"U{uid}", username=None, is_bot=uid == BOT_ID))
    return SimpleNamespace(args=list(args or []), bot=SimpleNamespace(get_chat_member=get_member, id=BOT_ID))


def run(coro):
    return asyncio.run(coro)


# ---- is_chat_admin / is_real_chat_admin ----

@pytest.mark.parametrize("uid,status,manager,chat_admin,real_admin", [
    (BOT_ADMIN, "member", False, True, True),            # админ бота
    (TG_ADMIN, "administrator", False, True, True),      # админ в Telegram
    (TG_ADMIN, "creator", False, True, True),
    (MANAGER, "member", True, True, False),              # управляющий
    (MANAGER, "restricted", True, True, False),
    (PLAIN, "member", False, False, False),              # участник без строки
    (MANAGER, "left", True, False, False),               # вышел — права не действуют, строка остаётся
    (MANAGER, "kicked", True, False, False),
])
def test_is_chat_admin_matrix(tmpdb, uid, status, manager, chat_admin, real_admin):
    if manager:
        db.add_chat_manager(CHAT, uid, BOT_ADMIN)
    ctx = make_ctx({uid: status})
    assert run(bot.is_chat_admin(ctx, CHAT, uid)) is chat_admin
    assert run(bot.is_real_chat_admin(ctx, CHAT, uid)) is real_admin
    if manager:
        assert db.is_chat_manager_db(CHAT, uid)   # строка не удаляется автоматически


def test_restricted_not_member_is_left(tmpdb):
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)

    async def get_member(chat_id, uid):
        return SimpleNamespace(status="restricted", is_member=False)
    ctx = SimpleNamespace(bot=SimpleNamespace(get_chat_member=get_member))
    assert run(bot.is_chat_admin(ctx, CHAT, MANAGER)) is False


def test_return_to_group_restores_rights(tmpdb):
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)
    assert run(bot.is_chat_admin(make_ctx({MANAGER: "left"}), CHAT, MANAGER)) is False
    assert run(bot.is_chat_admin(make_ctx({MANAGER: "member"}), CHAT, MANAGER)) is True


def test_query_error_is_false_but_bot_admin_stays(tmpdb):
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)
    ctx = make_ctx({}, fail=True)
    assert run(bot.is_chat_admin(ctx, CHAT, MANAGER)) is False
    assert run(bot.is_real_chat_admin(ctx, CHAT, TG_ADMIN)) is False
    assert run(bot.is_chat_admin(ctx, CHAT, BOT_ADMIN)) is True


def test_manager_in_other_chat_is_not_manager_here(tmpdb):
    db.add_chat_manager(-100999, MANAGER, BOT_ADMIN)
    assert run(bot.is_chat_admin(make_ctx({}), CHAT, MANAGER)) is False


def test_db_functions(tmpdb):
    assert db.list_chat_managers(CHAT) == []
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)
    db.add_chat_manager(CHAT, MANAGER, TG_ADMIN)         # повторная выдача — одна строка
    rows = db.list_chat_managers(CHAT)
    assert len(rows) == 1 and rows[0]["added_by"] == TG_ADMIN and rows[0]["added_at"]
    assert db.remove_chat_manager(CHAT, MANAGER) is True
    assert db.remove_chat_manager(CHAT, MANAGER) is False


# ---- команды ----

def call(cmd, actor, args=None, *, statuses=None, reply_to=None, chat_type="supergroup"):
    sent = []

    async def reply(text, **k):
        sent.append(text)

    reply_msg = None
    if reply_to:
        reply_msg = SimpleNamespace(from_user=SimpleNamespace(
            id=reply_to, full_name=f"U{reply_to}", username=None, is_bot=reply_to == BOT_ID))
    msg = SimpleNamespace(reply_text=reply, reply_to_message=reply_msg)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=actor), effective_message=msg,
                          effective_chat=SimpleNamespace(id=CHAT, type=chat_type))
    run(cmd(upd, make_ctx(statuses or {TG_ADMIN: "administrator", MANAGER: "member"}, args)))
    return sent[-1] if sent else None


def test_add_by_reply_and_by_id_and_rm(tmpdb):
    out = call(bot.cmd_manager, TG_ADMIN, ["add"], reply_to=MANAGER)
    assert out == f"U{MANAGER} теперь управляет ботом в этом чате (как админ чата)."
    assert db.is_chat_manager_db(CHAT, MANAGER)
    assert db.list_chat_managers(CHAT)[0]["added_by"] == TG_ADMIN
    out = call(bot.cmd_manager, BOT_ADMIN, ["add", str(OTHER)])
    assert "теперь управляет" in out and db.is_chat_manager_db(CHAT, OTHER)
    out = call(bot.cmd_manager, TG_ADMIN, ["rm"], reply_to=MANAGER)
    assert out == f"U{MANAGER} больше не управляет ботом в этом чате." and not db.is_chat_manager_db(CHAT, MANAGER)
    assert "и так не управляет" in call(bot.cmd_manager, TG_ADMIN, ["rm", str(MANAGER)])


def test_manager_cannot_grant_or_revoke(tmpdb):
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)
    st = {MANAGER: "member"}
    assert "только админы" in call(bot.cmd_manager, MANAGER, ["add", str(PLAIN)], statuses=st)
    assert not db.is_chat_manager_db(CHAT, PLAIN)
    assert "только админы" in call(bot.cmd_manager, MANAGER, ["rm", str(MANAGER)], statuses=st)
    assert db.is_chat_manager_db(CHAT, MANAGER)
    assert "только админы" in call(bot.cmd_manager, PLAIN, ["add", str(PLAIN)], statuses=st)


def test_already_admin_bot_and_errors(tmpdb):
    assert call(bot.cmd_manager, BOT_ADMIN, ["add"], reply_to=TG_ADMIN) == f"U{TG_ADMIN} и так админ."
    assert call(bot.cmd_manager, TG_ADMIN, ["add", str(BOT_ADMIN)]).endswith("и так админ.")   # админ бота
    assert db.list_chat_managers(CHAT) == []
    assert call(bot.cmd_manager, TG_ADMIN, ["add"], reply_to=BOT_ID) == "Бота управляющим сделать нельзя."
    assert call(bot.cmd_manager, TG_ADMIN, ["add", str(BOT_ID)]) == "Бота управляющим сделать нельзя."
    assert "user_id — число" in call(bot.cmd_manager, TG_ADMIN, ["add", "abc"])
    assert call(bot.cmd_manager, TG_ADMIN, ["add"]).startswith("Формат:")
    assert call(bot.cmd_manager, TG_ADMIN, []).startswith("Формат:")
    assert call(bot.cmd_manager, TG_ADMIN, ["zzz"]).startswith("Формат:")
    assert db.list_chat_managers(CHAT) == []


def test_unknown_uid_not_written(tmpdb):
    sent = []

    async def reply(text, **k):
        sent.append(text)

    async def get_member(chat_id, uid):
        if uid == TG_ADMIN:
            return SimpleNamespace(status="administrator")
        raise RuntimeError("user not found")
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=TG_ADMIN),
                          effective_message=SimpleNamespace(reply_text=reply, reply_to_message=None),
                          effective_chat=SimpleNamespace(id=CHAT, type="supergroup"))
    ctx = SimpleNamespace(args=["add", "777"], bot=SimpleNamespace(get_chat_member=get_member, id=BOT_ID))
    run(bot.cmd_manager(upd, ctx))
    assert "Не нашла" in sent[-1] and db.list_chat_managers(CHAT) == []


def test_private_chat(tmpdb):
    assert call(bot.cmd_manager, TG_ADMIN, ["add", "5"], chat_type="private") == "/manager работает только в групповых чатах."
    assert call(bot.cmd_managers, TG_ADMIN, chat_type="private") == "/managers работает только в групповых чатах."


def test_managers_list(tmpdb):
    assert "Управляющих нет" in call(bot.cmd_managers, TG_ADMIN)
    db.save_group_message(CHAT, MANAGER, "Вася", "привет")
    db.add_chat_manager(CHAT, MANAGER, TG_ADMIN)
    out = call(bot.cmd_managers, TG_ADMIN)
    assert f"Вася ({MANAGER})" in out and "Берлин" in out
    # смотреть может и управляющий; обычный участник — нет
    assert f"Вася ({MANAGER})" in call(bot.cmd_managers, MANAGER, statuses={MANAGER: "member"})
    assert "только для админов" in call(bot.cmd_managers, PLAIN, statuses={})


# ---- сквозной: права управляющего через реестр и команды ----

def settings_call(cmd, actor, args, statuses):
    sent = []

    async def reply(text, **k):
        sent.append(text)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=actor), effective_chat=SimpleNamespace(id=CHAT, type="supergroup"),
                          effective_message=SimpleNamespace(reply_text=reply, reply_to_message=None))
    run(cmd(upd, make_ctx(statuses, args)))
    return sent[-1]


def _review_enabled():
    c = db.get_conn()
    v = c.execute("SELECT daily_review_enabled FROM allowed_chats WHERE chat_id = ?", (CHAT,)).fetchone()[0]
    c.close()
    return v


def _activity():
    c = db.get_conn()
    v = c.execute("SELECT activity_percent FROM allowed_chats WHERE chat_id = ?", (CHAT,)).fetchone()[0]
    c.close()
    return v


def test_manager_passes_settings_then_loses_them(tmpdb):
    c = db.get_conn()
    c.execute("UPDATE allowed_chats SET daily_review_enabled = 0 WHERE chat_id = ?", (CHAT,))
    c.commit()
    c.close()
    st = {PLAIN: "member"}
    # до выдачи — отказ
    settings_call(bot.cmd_activity, PLAIN, ["20"], st)
    settings_call(bot.cmd_review_on, PLAIN, [], st)
    assert _activity() == 0 and _review_enabled() == 0
    # выдали — проходит /activity и /review_on (через реестр)
    call(bot.cmd_manager, TG_ADMIN, ["add"], reply_to=PLAIN)
    settings_call(bot.cmd_activity, PLAIN, ["20"], st)
    settings_call(bot.cmd_review_on, PLAIN, [], st)
    assert _activity() == 20 and _review_enabled() == 1
    # сняли — снова отказ
    call(bot.cmd_manager, TG_ADMIN, ["rm"], reply_to=PLAIN)
    settings_call(bot.cmd_activity, PLAIN, ["50"], st)
    settings_call(bot.cmd_review_off, PLAIN, [], st)
    assert _activity() == 20 and _review_enabled() == 1


def test_manager_cannot_use_bot_admin_features(tmpdb):
    """Управляющий не получает админку бота: чужой чат в /activity остаётся только админу бота."""
    db.add_chat_manager(CHAT, MANAGER, BOT_ADMIN)
    out = settings_call(bot.cmd_activity, MANAGER, ["20", "-100777"], {MANAGER: "member"})
    assert "только админ" in out.lower() or "чужой" in out.lower() or "админ" in out.lower()
    assert _activity() == 0

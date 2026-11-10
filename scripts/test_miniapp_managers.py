"""Тесты «Управляющие» в мини-приложении (feat/miniapp-managers, ТЗ docs/claude/tz-miniapp-managers.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_miniapp_managers.py -q
"""
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
Path.mkdir = _orig_mkdir

TOKEN = "123456:TEST-FAKE-TOKEN"
BOT_ADMIN, TG_ADMIN, MANAGER, PLAIN, NEWBIE, LEFT, TWIN_A, TWIN_B, OUTSIDER, BOT_USER = 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
G1, G_BANNED, G_UNKNOWN = -1001000000001, -1001000000004, -1001999999999
BOT_ID = 555
# статусы в G1; кого нет в словаре — get_chat_member падает (человека нет в группе)
STATUS = {TG_ADMIN: "administrator", MANAGER: "member", PLAIN: "member", NEWBIE: "member", LEFT: "left",
          TWIN_A: "member", TWIN_B: "member", BOT_USER: "member", BOT_ADMIN: "member"}


def auth(user_id):
    fields = {"auth_date": str(int(time.time())), "query_id": "AAH",
              "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False)}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    return {"Authorization": "tma " + urlencode({**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()})}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, 'Группа 1', 'approved', 0)", (G1,))
    c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, 'Бан', 'approved', 1)", (G_BANNED,))
    c.commit()
    c.close()
    for uid, uname in ((TG_ADMIN, "tgadmin"), (MANAGER, "mgr"), (PLAIN, "plain"), (NEWBIE, "NewBie_X"), (LEFT, "leaver"),
                       (TWIN_A, "twin"), (TWIN_B, "Twin"), (OUTSIDER, "outsider"), (BOT_USER, "somebot")):
        db.get_or_create_user(uid, uname, f"Имя{uid}")
    db.add_chat_manager(G1, MANAGER, TG_ADMIN)
    names = {}

    class FakeBot:
        id = BOT_ID

        async def get_chat_member(self, chat_id, uid):
            st = STATUS.get(uid) if chat_id == G1 else ("member" if chat_id == G_BANNED and uid == MANAGER else None)
            if st is None:
                raise RuntimeError("not in chat")
            return SimpleNamespace(status=st, is_member=True,
                                   user=SimpleNamespace(id=uid, full_name=names.get(uid, f"Имя{uid}"), username=f"u{uid}", is_bot=uid == BOT_USER))
    monkeypatch.setattr(bot, "_admin_bot", FakeBot())
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == BOT_ADMIN)
    bot._ma_cache.clear()
    return TestClient(bot.build_miniapp())


def url(chat=G1, uid=None):
    return f"/api/chats/{chat}/managers" + (f"/{uid}" if uid is not None else "")


def managers_ids():
    return [m["user_id"] for m in db.list_chat_managers(G1)]


# ---------- просмотр ----------

def test_list_for_tg_admin_bot_admin_and_manager(env):
    d = env.get(url(), headers=auth(TG_ADMIN)).json()
    assert d["can_edit"] is True and [m["id"] for m in d["managers"]] == [MANAGER] and d["chat"]["title"] == "Группа 1"
    assert d["managers"][0]["added_by"] and d["managers"][0]["added_at"]
    assert env.get(url(), headers=auth(BOT_ADMIN)).json()["can_edit"] is True
    dm = env.get(url(), headers=auth(MANAGER)).json()
    assert dm["can_edit"] is False and [m["id"] for m in dm["managers"]] == [MANAGER]      # видит список, но не правит


def test_list_denied(env):
    assert env.get(url(), headers=auth(PLAIN)).status_code == 403          # обычный участник — раздела нет
    assert env.get(url(), headers=auth(OUTSIDER)).status_code == 403       # не в группе
    assert env.get(url(G_BANNED), headers=auth(MANAGER)).status_code == 403
    assert env.get(url(), ).status_code == 401
    assert env.get(url(G1 * 0 + 5), headers=auth(TG_ADMIN)).status_code == 400          # личка — только группы
    assert env.get(url(G_UNKNOWN), headers=auth(BOT_ADMIN)).status_code in (403, 404)


def test_chat_view_flag(env):
    assert env.get(f"/api/chats/{G1}/settings", headers=auth(TG_ADMIN)).json()["chat"]["can_view_managers"] is True
    assert env.get(f"/api/chats/{G1}/settings", headers=auth(MANAGER)).json()["chat"]["can_view_managers"] is True
    assert env.get(f"/api/chats/{G1}/settings", headers=auth(PLAIN)).json()["chat"]["can_view_managers"] is False


# ---------- права на запись ----------

def test_manager_cannot_assign_or_remove(env):
    r = env.post(url(), json={"user": str(PLAIN)}, headers=auth(MANAGER))
    assert r.status_code == 403 and managers_ids() == [MANAGER]
    assert env.delete(url(uid=MANAGER), headers=auth(MANAGER)).status_code == 403 and managers_ids() == [MANAGER]


def test_plain_member_cannot_write(env):
    assert env.post(url(), json={"user": str(NEWBIE)}, headers=auth(PLAIN)).status_code == 403
    assert env.delete(url(uid=MANAGER), headers=auth(PLAIN)).status_code == 403
    assert managers_ids() == [MANAGER]


def test_tg_admin_and_bot_admin_can_assign_by_id_and_remove(env):
    r = env.post(url(), json={"user": str(NEWBIE)}, headers=auth(TG_ADMIN))
    assert r.status_code == 200 and managers_ids() == [MANAGER, NEWBIE]
    assert db.list_chat_managers(G1)[1]["added_by"] == TG_ADMIN
    assert env.delete(url(uid=NEWBIE), headers=auth(BOT_ADMIN)).status_code == 200
    assert managers_ids() == [MANAGER]
    r = env.post(url(), json={"user": NEWBIE}, headers=auth(BOT_ADMIN))          # числом, не строкой
    assert r.status_code == 200 and NEWBIE in managers_ids()


def test_write_rights_are_fresh_not_cached(env):
    assert env.get(url(), headers=auth(TG_ADMIN)).status_code == 200            # кэш прав «админ» прогрет
    STATUS[TG_ADMIN] = "member"                                                 # разжалован в Telegram
    try:
        assert env.post(url(), json={"user": str(NEWBIE)}, headers=auth(TG_ADMIN)).status_code == 403
    finally:
        STATUS[TG_ADMIN] = "administrator"
    assert NEWBIE not in managers_ids()


# ---------- @username ----------

def test_username_case_insensitive_needs_confirm(env):
    r = env.post(url(), json={"user": "@newbie_x", "confirm": False}, headers=auth(TG_ADMIN))
    assert r.status_code == 200 and r.json()["found"] == {"id": NEWBIE, "name": f"Имя{NEWBIE}"}
    assert NEWBIE not in managers_ids()                                          # без confirm записи нет
    r = env.post(url(), json={"user": "@newbie_x"}, headers=auth(TG_ADMIN))      # confirm по умолчанию false
    assert "found" in r.json() and NEWBIE not in managers_ids()
    r = env.post(url(), json={"user": "NEWBIE_x", "confirm": True}, headers=auth(TG_ADMIN))   # без @ тоже
    assert r.status_code == 200 and NEWBIE in managers_ids()                     # в БД — user_id, не ник


def test_unknown_username_404_with_hint(env):
    r = env.post(url(), json={"user": "@nobody_here", "confirm": True}, headers=auth(TG_ADMIN))
    assert r.status_code == 404 and "ещё не писал боту" in r.json()["message"] and "/id" in r.json()["message"]
    assert managers_ids() == [MANAGER]


def test_username_must_be_in_group(env):
    r = env.post(url(), json={"user": "@outsider", "confirm": True}, headers=auth(TG_ADMIN))
    assert r.status_code == 409 and "не состоит" in r.json()["message"]
    r = env.post(url(), json={"user": "@leaver", "confirm": True}, headers=auth(TG_ADMIN))     # вышел из группы
    assert r.status_code == 409
    assert env.post(url(), json={"user": str(OUTSIDER)}, headers=auth(TG_ADMIN)).status_code == 409
    assert managers_ids() == [MANAGER]


def test_ambiguous_username(env):
    r = env.post(url(), json={"user": "@twin", "confirm": True}, headers=auth(TG_ADMIN))
    assert r.status_code == 409 and "несколько" in r.json()["message"] and managers_ids() == [MANAGER]


# ---------- кого нельзя ----------

def test_cannot_add_bot_admin_or_existing_manager(env):
    r = env.post(url(), json={"user": str(BOT_USER)}, headers=auth(TG_ADMIN))
    assert r.status_code == 400 and "Бота" in r.json()["message"]
    r = env.post(url(), json={"user": str(TG_ADMIN)}, headers=auth(BOT_ADMIN))
    assert r.status_code == 409 and "и так админ" in r.json()["message"]
    r = env.post(url(), json={"user": str(MANAGER)}, headers=auth(TG_ADMIN))
    assert r.status_code == 409 and "уже управляет" in r.json()["message"]
    assert managers_ids() == [MANAGER]


def test_remove_not_a_manager_404(env):
    assert env.delete(url(uid=PLAIN), headers=auth(TG_ADMIN)).status_code == 404


def test_bad_requests(env):
    h = auth(TG_ADMIN)
    assert env.post(url(), json={}, headers=h).status_code == 400
    assert env.post(url(), json={"user": True}, headers=h).status_code == 400
    assert env.post(url(), json={"user": "ab"}, headers=h).status_code == 400            # не id и не ник
    assert env.post(url(), json={"user": "<b>x</b>"}, headers=h).status_code == 400
    assert env.post(url(), json={"user": "123", "confirm": "yes"}, headers=h).status_code == 400
    assert env.post(url(), content=b"not json", headers=h).status_code == 400
    assert managers_ids() == [MANAGER]


# ---------- общая логика с командой /manager ----------

def test_command_and_miniapp_share_assign_logic():
    assert bot._manager_assign.__name__ == "_manager_assign" and bot._manager_unassign.__name__ == "_manager_unassign"
    import inspect
    assert "_manager_assign" in inspect.getsource(bot.cmd_manager) and "_manager_assign" in inspect.getsource(bot._ma_managers_add)
    assert "_manager_unassign" in inspect.getsource(bot.cmd_manager) and "_manager_unassign" in inspect.getsource(bot._ma_managers_remove)

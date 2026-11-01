"""Тесты мини-приложения настроек (feat/miniapp, docs/claude/miniapp.md, ТЗ docs/claude/tz-miniapp.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_miniapp.py -q
Нужны fastapi/httpx из requirements.txt (TestClient).
"""
import hashlib
import hmac
import json
import logging
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
from miniapp import server as ma_server  # noqa: E402
Path.mkdir = _orig_mkdir

TOKEN = "123456:TEST-FAKE-TOKEN"
BOT_ADMIN, TG_ADMIN, MEMBER, MANAGER, OUTSIDER = 1, 2, 3, 4, 5
G1, G2, G_OTHER, G_BANNED = -1001000000001, -1001000000002, -1001000000003, -1001000000004


def make_init_data(user_id, *, token=TOKEN, auth_date=None, with_signature=True, tamper=None):
    fields = {"auth_date": str(int(time.time()) if auth_date is None else auth_date), "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
              "user": json.dumps({"id": user_id, "first_name": "Тест", "username": f"u{user_id}"}, ensure_ascii=False)}
    if with_signature:
        fields["signature"] = "c2lnbmF0dXJlLWZha2U"   # поле signature входит в data_check_string (hash исключается один)
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields[tamper] = fields[tamper] + "x"
    return urlencode({**fields, "hash": h})


def auth(user_id, **kw):
    return {"Authorization": "tma " + make_init_data(user_id, **kw)}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    for cid, name, banned in ((G1, "Группа 1", 0), (G2, "Группа 2", 0), (G_OTHER, "Чужая", 0), (G_BANNED, "Забаненная", 1)):
        c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, ?, 'approved', ?)", (cid, name, banned))
    c.commit()
    c.close()
    for uid in (TG_ADMIN, MEMBER, MANAGER):
        db.save_group_message(G1, uid, f"U{uid}", "привет")
    db.save_group_message(G2, MEMBER, "U3", "привет")
    db.save_group_message(G_OTHER, OUTSIDER, "U5", "привет")
    db.save_group_message(G_BANNED, MEMBER, "U3", "привет")
    statuses = {(G1, TG_ADMIN): "administrator", (G1, MEMBER): "member", (G1, MANAGER): "member", (G2, MEMBER): "member",
                (G_OTHER, OUTSIDER): "member", (G_BANNED, MEMBER): "member"}

    async def get_member(chat_id, uid):
        st = statuses.get((chat_id, uid))
        if st is None:
            raise RuntimeError("not in chat")
        return SimpleNamespace(status=st, is_member=True)
    monkeypatch.setattr(bot, "_admin_bot", SimpleNamespace(get_chat_member=get_member))
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == BOT_ADMIN)
    tiers = {}
    monkeypatch.setattr(bot, "chat_tier", lambda cid: tiers.get(cid, "paid"))
    bot._ma_cache.clear()
    client = TestClient(bot.build_miniapp())
    return SimpleNamespace(client=client, statuses=statuses, tiers=tiers)


# ---- проверка initData ----

def test_validate_ok_and_user_id():
    d = ma_server.validate_init_data(make_init_data(77), TOKEN)
    assert d["user_id"] == 77 and d["user"]["username"] == "u77"


def test_validate_works_without_signature_field_too():
    assert ma_server.validate_init_data(make_init_data(77, with_signature=False), TOKEN)["user_id"] == 77


@pytest.mark.parametrize("kw", [{"tamper": "user"}, {"tamper": "auth_date"}, {"tamper": "query_id"}])
def test_validate_tampered_field(kw):
    with pytest.raises(ma_server.InitDataError):
        ma_server.validate_init_data(make_init_data(77, **kw), TOKEN)


def test_validate_foreign_token_stale_and_garbage():
    with pytest.raises(ma_server.InitDataError):
        ma_server.validate_init_data(make_init_data(77, token="999:OTHER"), TOKEN)
    with pytest.raises(ma_server.InitDataError):
        ma_server.validate_init_data(make_init_data(77, auth_date=int(time.time()) - 7200), TOKEN, max_age=3600)
    with pytest.raises(ma_server.InitDataError):
        ma_server.validate_init_data(make_init_data(77, auth_date=int(time.time()) + 3600), TOKEN)
    for bad in ("", "user=1", "hash=abc", "a=1&a=2&hash=x"):
        with pytest.raises(ma_server.InitDataError):
            ma_server.validate_init_data(bad, TOKEN)


def test_api_without_or_with_bad_authorization_is_401(env):
    c = env.client
    assert c.get("/api/chats").status_code == 401
    assert c.get("/api/chats", headers={"Authorization": "tma garbage"}).status_code == 401
    assert c.get("/api/chats", headers={"Authorization": "Bearer " + make_init_data(MEMBER)}).status_code == 401
    assert c.get("/api/chats", headers=auth(MEMBER, token="999:OTHER")).status_code == 401
    assert c.get("/api/chats", headers=auth(MEMBER, auth_date=1)).status_code == 401
    assert c.post(f"/api/chats/{MEMBER}/settings/search_mode", json={"value": "gpt"}).status_code == 401
    assert c.get(f"/api/chats/{G1}/settings").status_code == 401


def test_index_page_and_headers_without_telegram(env):
    r = env.client.get("/")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text
    assert "https://telegram.org" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert env.client.get("/api/chats").headers["x-content-type-options"] == "nosniff"
    assert env.client.get("/docs").status_code == 404 and env.client.get("/openapi.json").status_code == 404


def test_init_data_not_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    h = auth(MEMBER)
    env.client.get("/api/chats", headers=h)
    assert h["Authorization"].split(" ", 1)[1] not in caplog.text and TOKEN not in caplog.text


# ---- /api/chats ----

def test_chats_member_sees_own_private_and_own_groups_only(env):
    r = env.client.get("/api/chats", headers=auth(MEMBER))
    assert r.status_code == 200
    ids = [c["id"] for c in r.json()["chats"]]
    assert ids[0] == MEMBER and set(ids[1:]) == {G1, G2}          # без чужой и забаненной группы
    byid = {c["id"]: c for c in r.json()["chats"]}
    assert byid[G1]["is_admin"] is False and byid[G1]["tier"] == "paid" and byid[G1]["is_group"] is True


def test_chats_admin_marks_and_left_member_hidden(env):
    byid = {c["id"]: c for c in env.client.get("/api/chats", headers=auth(TG_ADMIN)).json()["chats"]}
    assert set(byid) == {TG_ADMIN, G1} and byid[G1]["is_admin"] is True
    env.statuses[(G1, MEMBER)] = "left"
    bot._ma_cache.clear()
    ids = [c["id"] for c in env.client.get("/api/chats", headers=auth(MEMBER)).json()["chats"]]
    assert G1 not in ids and G2 in ids


def test_chats_bot_admin_sees_all(env):
    ids = {c["id"] for c in env.client.get("/api/chats", headers=auth(BOT_ADMIN)).json()["chats"]}
    assert ids == {BOT_ADMIN, G1, G2, G_OTHER, G_BANNED}


# ---- GET settings ----

def test_settings_foreign_chat_403_and_banned_403(env):
    h = auth(MEMBER)
    assert env.client.get(f"/api/chats/{G_OTHER}/settings", headers=h).status_code == 403
    assert env.client.get(f"/api/chats/{G_BANNED}/settings", headers=h).status_code == 403
    assert env.client.get(f"/api/chats/{OUTSIDER}/settings", headers=h).status_code == 403      # чужая личка
    assert env.client.get(f"/api/chats/{G1}/settings", headers=h).status_code == 200
    assert env.client.get(f"/api/chats/{G_OTHER}/settings", headers=auth(BOT_ADMIN)).status_code == 200


def test_settings_keys_by_scope(env):
    priv = env.client.get(f"/api/chats/{MEMBER}/settings", headers=auth(MEMBER)).json()
    group = env.client.get(f"/api/chats/{G1}/settings", headers=auth(MEMBER)).json()
    pk = {s["key"] for s in priv["settings"]}
    gk = {s["key"] for s in group["settings"]}
    assert pk == {k for k, s in bot.SETTINGS.items() if s.scope == "both"}
    assert gk == set(bot.SETTINGS) and pk < gk                 # в личке нет групповых
    assert priv["chat"] == {"id": MEMBER, "title": "Личка с ботом", "tier": "paid", "is_group": False, "is_admin": False}


def test_settings_describe_rights_and_choices(env):
    s = {x["key"]: x for x in env.client.get(f"/api/chats/{G1}/settings", headers=auth(MEMBER)).json()["settings"]}
    assert s["activity"]["can_change"] is False and "админы" in s["activity"]["reason"]        # участник — не меняет
    assert s["autoignore"]["can_change"] is False and s["autoignore"]["value"] is None         # даже смотреть нельзя
    assert s["search_mode"]["can_change"] is True and s["activity"]["int_range"] == [0, 100]
    bp = {c["value"]: c for c in s["image_provider"]["choices"]}["bananapro"]
    assert bp["allowed"] is False and bp["reason"]                                              # restricted в группе — не участнику
    adm = {x["key"]: x for x in env.client.get(f"/api/chats/{G1}/settings", headers=auth(TG_ADMIN)).json()["settings"]}
    assert adm["activity"]["can_change"] is True and adm["autoignore"]["value"] is True
    assert all(c["allowed"] for c in adm["image_provider"]["choices"])


def test_settings_free_note_and_denial(env):
    env.tiers[MEMBER] = "free"
    s = {x["key"]: x for x in env.client.get(f"/api/chats/{MEMBER}/settings", headers=auth(MEMBER)).json()["settings"]}
    assert "Lite" in s["model"]["note"] and s["model"]["can_change"] is False and s["model"]["reason"]


# ---- POST settings ----

def post(env, uid, chat, key, value):
    return env.client.post(f"/api/chats/{chat}/settings/{key}", headers=auth(uid), json={"value": value})


def test_post_allowed_writes_through_registry_and_rereads(env):
    r = post(env, MEMBER, MEMBER, "search_mode", "tavily")        # своя личка (in_target_chat=True)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["setting"]["value"] == "tavily"
    assert db.get_chat_search_mode_db(MEMBER) == "tavily"
    r = post(env, TG_ADMIN, G1, "activity", 30)
    assert r.status_code == 200 and r.json()["setting"]["value"] == 30 and db.get_activity_percent(G1) == 30
    assert post(env, MEMBER, G1, "search_mode", "gpt").status_code == 200   # любой участник группы своей же — in_target_chat=True


def test_post_forbidden_403_with_reason_and_no_write(env):
    r = post(env, MEMBER, G1, "activity", 50)
    assert r.status_code == 403 and "админы" in r.json()["message"] and db.get_activity_percent(G1) == 0
    r = post(env, MEMBER, G1, "image_provider", "bananapro")       # restricted
    assert r.status_code == 403 and db.get_chat_image_provider_db(G1) != "bananapro"
    r = post(env, MEMBER, G1, "autoignore", False)
    assert r.status_code == 403
    r = post(env, MEMBER, MEMBER, "activity", 10)                  # групповая настройка в личке
    assert r.status_code == 403 and "группов" in r.json()["message"]
    assert post(env, MEMBER, G_OTHER, "search_mode", "gpt").status_code == 403    # чужая группа


def test_post_invalid_value_400_and_unknown_key_404(env):
    assert post(env, TG_ADMIN, G1, "activity", 101).status_code == 400
    assert post(env, TG_ADMIN, G1, "activity", "20").status_code == 400
    assert post(env, TG_ADMIN, G1, "activity", True).status_code == 400
    assert post(env, TG_ADMIN, G1, "autoignore", "on").status_code == 400
    assert post(env, MEMBER, MEMBER, "search_mode", "nope").status_code == 400
    assert post(env, MEMBER, MEMBER, "persona", 5).status_code == 400
    assert post(env, MEMBER, MEMBER, "nokey", 1).status_code == 404
    assert db.get_activity_percent(G1) == 0
    r = env.client.post(f"/api/chats/{MEMBER}/settings/search_mode", headers=auth(MEMBER), content=b"not json")
    assert r.status_code == 400
    r = env.client.post(f"/api/chats/{MEMBER}/settings/search_mode", headers=auth(MEMBER), json={"nope": 1})
    assert r.status_code == 400


def test_post_free_denied_for_plain_user_admin_saves(env):
    env.tiers[MEMBER] = "free"
    r = post(env, MEMBER, MEMBER, "search_mode", "gpt")
    assert r.status_code == 403 and db.get_chat_search_mode_db(MEMBER) != "gpt"
    env.tiers[G1] = "free"
    r = post(env, BOT_ADMIN, G1, "search_mode", "gpt")                 # админ бота в free сохраняет с пометкой
    assert r.status_code == 200 and "бесплатном" in r.json()["message"]


def test_manager_gets_chat_admin_rights_and_loses_them(env):
    assert post(env, MANAGER, G1, "activity", 20).status_code == 403
    db.add_chat_manager(G1, MANAGER, TG_ADMIN)
    bot._ma_cache.clear()
    assert post(env, MANAGER, G1, "activity", 20).status_code == 200 and db.get_activity_percent(G1) == 20
    db.remove_chat_manager(G1, MANAGER)
    # чтение идёт из кэша 5 минут, а запись проверяет права заново
    assert post(env, MANAGER, G1, "activity", 40).status_code == 403 and db.get_activity_percent(G1) == 20


def test_write_rechecks_membership_without_cache(env):
    assert env.client.get("/api/chats", headers=auth(MEMBER)).status_code == 200   # кэшируем «состоит»
    env.statuses[(G1, MEMBER)] = "kicked"
    assert post(env, MEMBER, G1, "search_mode", "gpt").status_code == 403


def test_banned_user_403(env):
    c = db.get_conn()
    c.execute("INSERT OR IGNORE INTO users (telegram_id, username, full_name) VALUES (?, 'b', 'b')", (MEMBER,))
    c.execute("UPDATE users SET banned = 1 WHERE telegram_id = ?", (MEMBER,))
    c.commit()
    c.close()
    assert env.client.get("/api/chats", headers=auth(MEMBER)).status_code == 403


def test_usage_ctx_set_for_write(env, monkeypatch):
    seen = []
    real = bot.apply_setting

    async def spy(*a, **k):
        seen.append(bot.usage_ctx.get())
        return await real(*a, **k)
    monkeypatch.setattr(bot, "apply_setting", spy)
    post(env, TG_ADMIN, G1, "activity", 10)
    assert seen == [(G1, "group", TG_ADMIN)]


# ---- запуск и лимиты ----

def test_rate_limit_429(env):
    app = bot._miniapp_create_app(bot_token=TOKEN, deps=bot._MiniappDeps(), rate_limit=3)
    c = TestClient(app)
    codes = [c.get("/api/chats", headers=auth(MEMBER)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    assert c.get("/api/chats", headers=auth(TG_ADMIN)).status_code == 200      # лимит — на user.id


def test_disabled_without_public_url(monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "")
    assert bot.miniapp_enabled() is False
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "https://x.example")
    assert bot.miniapp_enabled() is True
    monkeypatch.setattr(bot, "_miniapp_create_app", None)           # нет каталога ./miniapp
    assert bot.miniapp_enabled() is False


# ---- /settings и диплинк ----

def _cmd_update(chat_type, chat_id=-100500, args=None):
    sent = []

    async def reply(text, **k):
        sent.append((text, k))
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id, type=chat_type), effective_user=SimpleNamespace(id=7),
                          effective_message=SimpleNamespace(reply_text=reply))
    return upd, SimpleNamespace(args=args or []), sent


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_cmd_settings_private_group_and_unconfigured(monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "https://x.example")
    monkeypatch.setattr(bot, "bot_username", "claudushka_bot")
    upd, ctx, sent = _cmd_update("private")
    run(bot.cmd_settings(upd, ctx))
    btn = sent[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert btn.web_app.url == "https://x.example/"
    upd, ctx, sent = _cmd_update("supergroup", chat_id=-100500)
    run(bot.cmd_settings(upd, ctx))
    btn = sent[-1][1]["reply_markup"].inline_keyboard[0][0]
    assert btn.web_app is None and btn.url == "https://t.me/claudushka_bot?start=settings_-100500"
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "")
    upd, ctx, sent = _cmd_update("private")
    run(bot.cmd_settings(upd, ctx))
    assert "не настроено" in sent[-1][0]


def test_start_deeplink(monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "https://x.example")
    upd, ctx, sent = _cmd_update("private")
    assert run(bot._start_settings_deeplink(upd, "settings_-100500")) is True
    assert sent[-1][1]["reply_markup"].inline_keyboard[0][0].web_app.url == "https://x.example/?chat=-100500"
    assert run(bot._start_settings_deeplink(upd, "settings_abc")) is False       # не число — обычный /start
    assert run(bot._start_settings_deeplink(upd, "other")) is False
    upd, ctx, sent = _cmd_update("supergroup")
    assert run(bot._start_settings_deeplink(upd, "settings_-100500")) is False   # только в личке


def test_html_has_no_innerhtml_and_uses_theme():
    html = (Path(__file__).resolve().parent.parent / "miniapp" / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html and "--tg-theme-bg-color" in html and "Telegram.WebApp" in html or "window.Telegram" in html

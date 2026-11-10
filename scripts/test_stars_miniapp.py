"""Тесты мини-приложения для пополнения Stars (feat/stars-topup, ТЗ docs/claude/tz-stars-topup.md, раздел «Мини-приложение»). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_stars_miniapp.py -q
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
BOT_ADMIN, MEMBER, OUTSIDER = 1, 3, 5
G1, G_BANNED = -1001000000001, -1001000000004


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
    for uid in (MEMBER, OUTSIDER):
        db.get_or_create_user(uid)
    links = []

    class FakeBot:
        async def get_chat_member(self, chat_id, uid):
            if (chat_id, uid) in {(G1, MEMBER), (G_BANNED, MEMBER)}:
                return SimpleNamespace(status="member", is_member=True)
            raise RuntimeError("not in chat")

        async def create_invoice_link(self, **kw):
            links.append(kw)
            return "https://t.me/$FAKELINK"
    monkeypatch.setattr(bot, "_admin_bot", FakeBot())
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == BOT_ADMIN)
    bot._ma_cache.clear()
    return SimpleNamespace(client=TestClient(bot.build_miniapp()), links=links)


def test_topup_info_member_and_private(env):
    r = env.client.get(f"/api/chats/{G1}/topup", headers=auth(MEMBER))
    assert r.status_code == 200
    d = r.json()
    assert d["title"] == "Группа 1" and [p["stars"] for p in d["packs"]] == list(bot.STARS_PACKS)
    assert d["packs"][0]["usd"] == pytest.approx(bot.STARS_PACKS[0] * bot.STARS_USD_RATE)
    assert "Как купить звёзды" in d["help"] and d["balance"] == 0
    assert env.client.get(f"/api/chats/{MEMBER}/topup", headers=auth(MEMBER)).status_code == 200


def test_topup_link_created_with_correct_invoice(env):
    r = env.client.post(f"/api/chats/{G1}/topup", json={"stars": 200}, headers=auth(MEMBER))
    assert r.status_code == 200 and r.json() == {"invoice_link": "https://t.me/$FAKELINK"}
    kw = env.links[0]
    assert kw["currency"] == "XTR" and kw["payload"] == f"topup:{G1}:200" and kw["prices"][0].amount == 200
    assert db.get_balance(G1) == 0          # ссылка денег не зачисляет — только successful_payment в боте


def test_topup_denied_for_outsider_banned_and_foreign_private(env):
    assert env.client.get(f"/api/chats/{G1}/topup", headers=auth(OUTSIDER)).status_code == 403
    assert env.client.post(f"/api/chats/{G1}/topup", json={"stars": 100}, headers=auth(OUTSIDER)).status_code == 403
    assert env.client.post(f"/api/chats/{G_BANNED}/topup", json={"stars": 100}, headers=auth(MEMBER)).status_code in (400, 403)
    assert env.client.post(f"/api/chats/{OUTSIDER}/topup", json={"stars": 100}, headers=auth(MEMBER)).status_code == 403
    # админ бота: чужая личка и группа вне списка — нельзя
    assert env.client.post(f"/api/chats/{MEMBER}/topup", json={"stars": 100}, headers=auth(BOT_ADMIN)).status_code == 403
    assert env.client.post("/api/chats/-1009999/topup", json={"stars": 100}, headers=auth(BOT_ADMIN)).status_code == 403
    assert env.links == []


def test_topup_bad_requests(env):
    h = auth(MEMBER)
    assert env.client.post(f"/api/chats/{G1}/topup", json={"stars": 77}, headers=h).status_code == 400      # нет такого пакета
    assert env.client.post(f"/api/chats/{G1}/topup", json={"stars": "100"}, headers=h).status_code == 400
    assert env.client.post(f"/api/chats/{G1}/topup", json={"stars": True}, headers=h).status_code == 400
    assert env.client.post(f"/api/chats/{G1}/topup", json={}, headers=h).status_code == 400
    assert env.client.post(f"/api/chats/{G1}/topup", content=b"not json", headers=h).status_code == 400
    assert env.client.get(f"/api/chats/{G1}/topup").status_code == 401                                   # без подписи
    assert env.links == []


def test_chat_view_has_can_topup(env):
    d = env.client.get(f"/api/chats/{G1}/settings", headers=auth(MEMBER)).json()
    assert d["chat"]["can_topup"] is True
    d = env.client.get(f"/api/chats/{MEMBER}/settings", headers=auth(MEMBER)).json()
    assert d["chat"]["can_topup"] is True


def test_frontend_has_topup_screen_and_uses_text_nodes():
    html = (Path(__file__).resolve().parent.parent / "miniapp" / "index.html").read_text(encoding="utf-8")
    assert "openInvoice" in html and "Как купить звёзды из России" in html and "/topup" in html
    assert ".innerHTML" not in html          # справка и пакеты — только textContent (слово в комментарии — не использование)

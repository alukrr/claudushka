"""Тесты экрана «Расходы» и открытия приложения из группы (feat/miniapp-v2, ТЗ docs/claude/tz-miniapp-v2.md). Без сети, во временной SQLite.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_miniapp_costs.py -q
(в PowerShell: $env:TELEGRAM_BOT_TOKEN="x"; $env:ANTHROPIC_API_KEY="x"; python -m pytest scripts/test_miniapp_costs.py -q)
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
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
BOT_ADMIN, TG_ADMIN, MEMBER, MANAGER, OUTSIDER = 1, 2, 3, 4, 5
G1, G_BANNED, G_UNKNOWN = -1001000000001, -1001000000004, -1001000000009
BERLIN = bot.BERLIN_TZ
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN)       # фиксированное «сейчас» для _cost_data


def make_init_data(user_id):
    fields = {"auth_date": str(int(time.time())), "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
              "user": json.dumps({"id": user_id, "first_name": "Тест"}, ensure_ascii=False)}
    dcs = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()})


def auth(user_id):
    return {"Authorization": "tma " + make_init_data(user_id)}


def put(ts, chat_id, cost, billed=True, kind="llm", model="gemini-3-flash-preview", label="dialog", inp=10, out=5):
    c = db.get_conn()
    c.execute("INSERT INTO usage_log (ts, chat_id, chat_type, user_id, kind, model, label, input, output, cache_write, cache_read, "
              "thinking, cost_usd, billed) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
              (int(ts), chat_id, "group" if chat_id < 0 else "private", 0, kind, model, label, inp, out, cost, 1 if billed else 0))
    c.commit()
    c.close()


def at(y, m, d, h=12, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=BERLIN).timestamp()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")        # временная база, не data/
    db.init_db()
    c = db.get_conn()
    for cid, name, banned in ((G1, "Группа 1", 0), (G_BANNED, "Забаненная", 1)):
        c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, ?, 'approved', ?)", (cid, name, banned))
    c.commit()
    c.close()
    for uid in (TG_ADMIN, MEMBER, MANAGER):
        db.save_group_message(G1, uid, f"U{uid}", "привет")
    db.save_group_message(G_BANNED, MEMBER, "U3", "привет")
    db.add_chat_manager(G1, MANAGER, TG_ADMIN)
    statuses = {(G1, TG_ADMIN): "administrator", (G1, MEMBER): "member", (G1, MANAGER): "member",
                (G_BANNED, MEMBER): "member", (G1, OUTSIDER): None}

    async def get_member(chat_id, uid):
        st = statuses.get((chat_id, uid))
        if st is None:
            raise RuntimeError("not in chat")
        return SimpleNamespace(status=st, is_member=True)
    monkeypatch.setattr(bot, "_admin_bot", SimpleNamespace(get_chat_member=get_member))
    monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == BOT_ADMIN)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    bot._ma_cache.clear()
    return SimpleNamespace(client=TestClient(bot.build_miniapp()))


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


# ---- границы месяцев (чистая логика) ----

def test_month_windows_basic_and_year_wrap():
    w = bot._cost_month_windows(NOW)
    assert len(w) == 12 and w[0][0] == "2026-11" and w[0][3] is True and w[-1][0] == "2025-12"
    assert all(not x[3] for x in w[1:])
    for older, newer in zip(w[1:], w[:-1]):                        # месяцы стыкуются без дыр и перекрытий
        assert older[2] == newer[1]
    jan = bot._cost_month_windows(datetime(2026, 1, 15, tzinfo=BERLIN), 3)
    assert [x[0] for x in jan] == ["2026-01", "2025-12", "2025-11"]


def test_month_windows_dst_boundaries_are_calendar_midnights():
    by = {x[0]: x for x in bot._cost_month_windows(NOW)}
    mar, octo = by["2026-03"], by["2026-10"]
    assert mar[1].utcoffset() == timedelta(hours=1) and mar[2].utcoffset() == timedelta(hours=2)    # CET → CEST
    assert mar[2].timestamp() - mar[1].timestamp() == (31 * 24 - 1) * 3600                          # в марте 743 часа
    assert octo[1].utcoffset() == timedelta(hours=2) and octo[2].utcoffset() == timedelta(hours=1)  # CEST → CET
    assert octo[2].timestamp() - octo[1].timestamp() == (31 * 24 + 1) * 3600                        # в октябре 745 часов
    for x in by.values():
        assert x[1].hour == 0 and x[1].day == 1 and x[2].hour == 0 and x[2].day == 1


# ---- _cost_data на временной базе ----

def test_cost_data_months_by_berlin_not_utc(env):
    utc = lambda y, m, d, h, mi: datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp()
    put(utc(2026, 10, 31, 23, 30), G1, 1.0)     # 23:30 UTC 31 окт = 00:30 CET 1 ноя → ноябрь Берлина
    put(utc(2026, 10, 31, 22, 30), G1, 2.0)     # 22:30 UTC = 23:30 CET 31 окт → октябрь
    put(utc(2026, 6, 30, 22, 30), G1, 4.0)      # 22:30 UTC = 00:30 CEST 1 июля → июль (лето, UTC+2)
    put(utc(2026, 6, 30, 21, 30), G1, 8.0)      # 23:30 CEST 30 июня → июнь
    m = {r["month"]: r for r in bot._cost_data(G1, True, NOW)["months"]}
    assert m["2026-11"]["paid"] == 1.0 and m["2026-10"]["paid"] == 2.0
    assert m["2026-07"]["paid"] == 4.0 and m["2026-06"]["paid"] == 8.0


def test_cost_data_partial_current_month_and_ordering(env):
    put(at(2026, 11, 10, 9), G1, 0.5)
    put(at(2026, 10, 15), G1, 3.0)
    put(at(2024, 1, 1), G1, 99.0)               # старше 12 месяцев — в «по месяцам» не попадает
    put(at(2026, 11, 11), G1, 7.0)              # «будущее» относительно NOW — не считается
    d = bot._cost_data(G1, True, NOW)
    months = d["months"]
    assert [x["month"] for x in months][:2] == ["2026-11", "2026-10"] and len(months) == 12
    assert months[0]["partial"] is True and all(not x["partial"] for x in months[1:])
    assert months[0]["paid"] == 0.5 and months[1]["paid"] == 3.0
    assert sum(x["paid"] for x in months) == 3.5


def test_cost_data_show_free_false_has_no_free_anywhere(env):
    put(at(2026, 11, 9), G1, 1.0, billed=True)
    put(at(2026, 11, 9), G1, 5.0, billed=False, model="free-only-model", label="freelabel")
    open_ = bot._cost_data(G1, False, NOW)
    assert "free" not in set(walk_keys(open_)) and open_["show_free"] is False
    blob = json.dumps(open_, ensure_ascii=False)
    assert "free-only-model" not in blob and "freelabel" not in blob and '"billed": false' not in blob
    assert all(r["billed"] is True for r in open_["by_model"] + open_["by_label"])
    full = bot._cost_data(G1, True, NOW)
    assert full["show_free"] is True and "free" in set(walk_keys(full))
    assert sum(x["free"] for x in full["months"]) == 5.0 and sum(x["paid"] for x in full["months"]) == 1.0
    assert any(not r["billed"] for r in full["by_model"]) and any(not r["billed"] for r in full["by_label"])


def test_cost_data_consistency_days_week_kinds_and_balance(env):
    # NOW = вторник 10 ноября 2026; неделя с понедельника 9-го
    put(at(2026, 11, 10, 8), G1, 1.0, kind="llm")
    put(at(2026, 11, 9, 8), G1, 2.0, kind="image", model="banana")
    put(at(2026, 11, 8, 8), G1, 4.0, kind="search", model="gpt")       # воскресенье: в днях (7), но не в «за неделю»
    put(at(2026, 11, 3, 8), G1, 8.0)                                   # 7 дней назад: вне окна дней
    d = bot._cost_data(G1, False, NOW)
    assert [x["date"] for x in d["days"]][0] == "2026-11-04" and d["days"][-1]["date"] == "2026-11-10"
    assert sum(x["paid"] for x in d["days"]) == 7.0
    assert sum(r["cost"] for r in d["by_model"]) == 7.0                # те же 7 дней
    assert d["by_kind_week"] == {"llm": 1.0, "image": 2.0, "search": 0.0}
    assert sum(d["by_kind_week"].values()) == 3.0                      # = «Эта неделя» в /cost (с понедельника)
    assert d["rate_7d_per_day"] == pytest.approx(7.0 / 7)              # ts<=NOW+1, окно 7*86400 назад от NOW: 4,2,1 ✓, 8.0 (7 дней назад в 08:00) — вне
    assert d["balance"] == db.get_balance(G1) and d["days_left"] is None   # баланс <= 0 → «на N дней» нет
    assert d["chat"] == {"id": G1, "title": "Группа 1", "tier": "paid", "verified": bot.is_chat_verified(G1)}


def test_cost_detail_text_has_months_and_fits_telegram(env, monkeypatch):
    monkeypatch.setattr(bot, "_berlin_now", lambda: NOW)
    put(at(2026, 11, 9), G1, 1.0)
    put(at(2026, 11, 9), G1, 2.0, billed=False, label="free")
    text = bot._cost_detail(G1)
    lines = text.split("\n")
    i = lines.index("По месяцам (платное / бесплатное):")
    assert len(lines[i + 1:]) == 12 and lines[i + 1].startswith("  2026-11:") and lines[i + 1].endswith("(по сегодня)")
    assert "Платное (billed=1): $1.00" in text and "Бесплатное (billed=0): $2.00" in text and "По дням" in text
    assert len(text) < bot.TELEGRAM_MSG_LIMIT
    assert all(len(p) <= bot.TELEGRAM_MSG_LIMIT for p in bot.split_lines_for_telegram(text * 3))   # режется по строкам


# ---- эндпоинт ----

def costs(env, uid, chat_id):
    return env.client.get(f"/api/chats/{chat_id}/costs", headers=auth(uid))


def test_costs_401_without_signature(env):
    assert env.client.get(f"/api/chats/{G1}/costs").status_code == 401
    assert env.client.get(f"/api/chats/{G1}/costs", headers={"Authorization": "tma garbage"}).status_code == 401


def test_costs_403_for_plain_member_and_outsiders(env):
    r = costs(env, MEMBER, G1)
    assert r.status_code == 403 and "админы чата" in r.json()["message"]
    assert costs(env, OUTSIDER, G1).status_code == 403              # не участник группы
    assert costs(env, MEMBER, G_BANNED).status_code == 403          # забаненная группа
    assert costs(env, BOT_ADMIN, G_UNKNOWN).status_code == 403      # админ бота: только чаты из allowed_chats


def test_costs_200_chat_admin_and_manager_without_free(env):
    put(at(2026, 11, 9), G1, 1.0)
    put(at(2026, 11, 9), G1, 5.0, billed=False, model="free-only-model", label="freelabel")
    for uid in (TG_ADMIN, MANAGER):
        r = costs(env, uid, G1)
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["show_free"] is False and "free" not in set(walk_keys(j))
        assert "free-only-model" not in r.text and "freelabel" not in r.text
        assert len(j["months"]) == 12 and len(j["days"]) == 7 and set(j["by_kind_week"]) >= {"llm", "image", "search"}


def test_costs_200_bot_admin_sees_free(env):
    put(at(2026, 11, 9), G1, 1.0)
    put(at(2026, 11, 9), G1, 5.0, billed=False, model="free-only-model")
    j = costs(env, BOT_ADMIN, G1).json()
    assert j["show_free"] is True and "free" in set(walk_keys(j))
    assert any(not r["billed"] for r in j["by_model"])


def test_costs_private_own_foreign_and_bot_admin(env):
    put(at(2026, 11, 9), MEMBER, 1.0)
    put(at(2026, 11, 9), MEMBER, 5.0, billed=False)
    own = costs(env, MEMBER, MEMBER)                                # своя личка — самому человеку, без бесплатного
    assert own.status_code == 200 and own.json()["show_free"] is False and "free" not in set(walk_keys(own.json()))
    assert costs(env, MEMBER, TG_ADMIN).status_code == 403          # чужая личка
    assert costs(env, TG_ADMIN, MEMBER).status_code == 403
    r = costs(env, BOT_ADMIN, MEMBER)                               # чужая личка — админу бота, с бесплатным
    assert r.status_code == 200 and r.json()["show_free"] is True


def test_costs_internal_error_is_not_leaked(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret-internal-detail")
    monkeypatch.setattr(bot, "_cost_data", boom)
    r = costs(env, BOT_ADMIN, G1)
    assert r.status_code == 500 and "secret-internal-detail" not in r.text and r.json()["message"]


def test_can_view_costs_flags_in_list_and_chat_view(env):
    def flags(uid):
        return {c["id"]: c["can_view_costs"] for c in env.client.get("/api/chats", headers=auth(uid)).json()["chats"]}
    assert flags(MEMBER) == {MEMBER: True, G1: False}
    assert flags(TG_ADMIN)[G1] is True and flags(MANAGER)[G1] is True and flags(BOT_ADMIN)[G1] is True
    chat = lambda uid, cid: env.client.get(f"/api/chats/{cid}/settings", headers=auth(uid)).json()["chat"]
    assert chat(MEMBER, G1)["can_view_costs"] is False and chat(TG_ADMIN, G1)["can_view_costs"] is True
    assert chat(MEMBER, MEMBER)["can_view_costs"] is True


# ---- Часть B: /settings в группе и startapp ----

class FakeMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))


def run_settings(chat_type, chat_id):
    msg = FakeMsg()
    upd = SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(type=chat_type, id=chat_id))
    asyncio.run(bot.cmd_settings(upd, None))
    return msg.sent[0]


@pytest.fixture()
def miniapp_on(monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "https://example.test")
    monkeypatch.setattr(bot, "bot_username", "testbot")


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_startapp_param_charset_and_encoding():
    p = bot._settings_startapp_param(G1)
    assert p == "chat_n1001000000001" and re.fullmatch(r"[A-Za-z0-9_]{1,64}", p)
    assert bot._settings_startapp_param(-5) == "chat_n5"


def test_settings_in_group_without_main_app_is_v1(miniapp_on, monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_MAIN_APP", False)
    text, markup = run_settings("supergroup", G1)
    b = buttons(markup)
    assert text == "Настройки открываются в личке." and len(b) == 1
    assert b[0].url == f"https://t.me/testbot?start=settings_{G1}"


def test_settings_in_group_with_main_app_has_startapp_and_fallback(miniapp_on, monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_MAIN_APP", True)
    _, markup = run_settings("supergroup", G1)
    b = buttons(markup)
    assert [x.text for x in b] == ["Открыть настройки", "Открыть в личке"]
    assert b[0].url == "https://t.me/testbot?startapp=chat_n1001000000001" and b[0].web_app is None
    assert b[1].url == f"https://t.me/testbot?start=settings_{G1}"


def test_settings_in_private_unchanged_with_main_app(miniapp_on, monkeypatch):
    monkeypatch.setattr(bot, "MINIAPP_MAIN_APP", True)
    _, markup = run_settings("private", 7)
    b = buttons(markup)
    assert len(b) == 1 and b[0].web_app.url == "https://example.test/"

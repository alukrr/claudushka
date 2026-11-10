"""Тесты feat/cost-weeks-subscription: недели KW (ISO 8601) и оценка цены подписки по восстановленному балансу
(ТЗ docs/claude/tz-cost-weeks-subscription.md). Без сети, во временной SQLite.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_subscription_estimate.py -q
(в PowerShell: $env:TELEGRAM_BOT_TOKEN="x"; $env:ANTHROPIC_API_KEY="x"; python -m pytest scripts/test_subscription_estimate.py -q)
"""
import json
import os
import sys
import time as real_time
from datetime import datetime, timedelta
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

BERLIN = bot.BERLIN_TZ
G1, G2 = -1001000000001, -1001000000002
ADMIN_CHAT = 777
DAY = 86400
T0 = 1_000_000_000          # произвольная точка отсчёта для чистых тестов (UTC-секунды)


def d(n):
    """n суток от T0."""
    return T0 + int(n * DAY)


def credit(amount, ts):
    return (ts, float(amount), False)


def spend(cost, ts):
    return (ts, -float(cost), True)


def at(y, m, dd, h=12, mi=0):
    return datetime(y, m, dd, h, mi, tzinfo=BERLIN).timestamp()


# ---- чистая логика: db.paid_seconds_from_events / db.billed_outside_paid ----

def test_paid_topup_spend_to_zero_silence_topup_again():
    ev = [credit(1.0, d(1)), spend(1.0, d(3)), credit(2.0, d(6)), spend(0.5, d(8))]
    # paid: сутки 1→3 (2 дня), тишина free 3→6, снова paid 6→10 (баланс 1.5 после траты)
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(10)) == 6 * DAY
    assert db.billed_outside_paid(0.0, ev) == 0


def test_paid_writeoff_same_ts_as_overdraft():
    # трата 1.5 при балансе 1.0 → баланс −0.5 → writeoff +0.5 в тот же ts (db.record_usage); дальше free
    ev = [credit(1.0, d(1)), spend(1.5, d(2)), credit(0.5, d(2))]
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(5)) == 1 * DAY
    assert db.billed_outside_paid(0.0, ev) == 0


def test_paid_bonus_and_negative_adjust_are_just_credits():
    # 'bonus' даёт платный режим, отрицательная корректировка ('adjust', amount<0) его снимает без единой траты
    ev = [credit(5.0, d(0)), credit(-5.0, d(4))]
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(10)) == 4 * DAY


def test_paid_equal_ts_credit_before_spend_and_state_after_group():
    ev = [spend(1.0, d(2)), credit(1.0, d(2))]                    # в списке — трата первой; при равном ts кредиты идут раньше
    assert db.billed_outside_paid(0.0, ev) == 0                   # пополнение и трата в одну секунду: трата внутри платного
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(5)) == 0.0   # итог группы = 0 → free; интервал нулевой длины не платный
    assert db.billed_outside_paid(0.0, [spend(1.0, d(2))]) == 1   # трата без баланса — нарушение


def test_paid_admin_private_chat_whole_window_even_with_negative_balance():
    ev = [spend(3.0, d(1)), spend(2.0, d(2))]                     # у админа бота billed=1 без writeoff, баланс уходит в минус
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(10), always_paid=True) == 10 * DAY
    assert db.billed_outside_paid(0.0, ev, always_paid=True) == 0
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(10)) == 0.0


def test_paid_window_starts_mid_paid_interval_and_clips_end():
    # пополнение за окном (opening = 3.0), окно [d2, d5): трата до нуля на d3 → paid 1 день
    ev = [spend(3.0, d(3))]
    assert db.paid_seconds_from_events(3.0, ev, d(2), d(5)) == 1 * DAY
    # конец окна посреди платного интервала обрезает его: события после until не влияют
    ev2 = [credit(9.0, d(1)), spend(9.0, d(9))]
    assert db.paid_seconds_from_events(0.0, ev2, d(0), d(4)) == 3 * DAY
    assert db.paid_seconds_from_events(0.0, [], d(0), d(4)) == 0.0
    assert db.paid_seconds_from_events(5.0, [], d(0), d(4)) == 4 * DAY
    assert db.paid_seconds_from_events(5.0, [], d(4), d(4)) == 0.0


def test_paid_threshold_is_min_balance_not_zero():
    ev = [credit(1.0, d(1)), spend(1.0 - db.PAID_MIN_BALANCE / 2, d(2))]   # остаток ниже порога = free (пыль float)
    assert db.paid_seconds_from_events(0.0, ev, d(0), d(5)) == 1 * DAY


# ---- на временной БД ----

@pytest.fixture()
def tdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    for cid, name in ((G1, "Группа 1"), (G2, "Группа 2")):
        c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, ?, 'approved', 0)", (cid, name))
    c.commit()
    c.close()
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == ADMIN_CHAT)
    return SimpleNamespace()


def put_credit(chat_id, amount, ts, kind="topup"):
    c = db.get_conn()
    c.execute("INSERT INTO chat_credits (ts, chat_id, amount, kind) VALUES (?, ?, ?, ?)", (int(ts), chat_id, amount, kind))
    c.commit()
    c.close()


def put(ts, chat_id, cost, billed=True, kind="llm", label="dialog"):
    c = db.get_conn()
    c.execute("INSERT INTO usage_log (ts, chat_id, chat_type, user_id, kind, model, label, input, output, cache_write, cache_read, "
              "thinking, cost_usd, billed) VALUES (?,?,?,?,?,?,?,?,?,0,0,0,?,?)",
              (int(ts), chat_id, "group" if chat_id < 0 else "private", 0, kind, "gemini-3-flash-preview", label, 10, 5, cost, 1 if billed else 0))
    c.commit()
    c.close()


def test_db_paid_seconds_reads_all_credit_kinds_and_only_billed_rows(tdb):
    put_credit(G1, 5.0, d(0), "bonus")
    put_credit(G1, 2.0, d(1), "topup")
    put_credit(G1, -1.0, d(2), "adjust")
    put_credit(G1, 0.5, d(3), "weird_kind")                 # любой kind суммируется как есть — так же, как _BALANCE_SQL
    put(d(1.5), G1, 100.0, billed=False)                    # бесплатное баланс не двигает
    put(d(4), G1, 6.5)                                      # 5+2−1+0.5 = 6.5 → ровно ноль на d4
    assert db.paid_seconds(G1, d(0), d(10)) == 4 * DAY
    assert db.paid_seconds(G1, d(2), d(10)) == 2 * DAY      # окно начинается посреди платного интервала: opening = 7.0
    assert db.paid_seconds(G2, d(0), d(10)) == 0.0          # чужие записи не влияют
    assert bot._paid_seconds(ADMIN_CHAT, d(0), d(10)) == 10 * DAY    # личка админа бота — всё окно


def test_real_record_usage_flow_billed_rows_inside_paid_and_balance_matches(tdb, monkeypatch):
    clock = [d(0)]

    class FakeTime:
        time = staticmethod(lambda: clock[0])

        def __getattr__(self, name):
            return getattr(real_time, name)
    monkeypatch.setattr(db, "time", FakeTime())

    def at_day(n):
        clock[0] = d(n)

    def use(cost):
        billed = db.get_balance(G1) > db.PAID_MIN_BALANCE   # ровно как bot.chat_tier + _record_usage
        db.record_usage(G1, "group", 1, "llm", "m", "dialog", cost, billed)
        return billed

    at_day(0)
    assert db.grant_starter_bonus(G1, 5.0)
    at_day(1); assert use(1.0)
    at_day(2); assert use(1.0)
    at_day(3); assert use(4.0)                              # баланс −1 → writeoff +1 в тот же ts → free
    at_day(4); assert use(0.5) is False                     # free: billed=0
    at_day(6); db.add_credit(G1, 2.0, "topup")
    at_day(8); assert use(1.0)
    at_day(9); assert use(3.0)                              # перерасход → writeoff → free
    until = d(12)
    c = db.get_conn()
    opening, events = db.subscription_events(c, G1, d(0), until)
    assert db.paid_seconds_from_events(opening, events, d(0), until) == 6 * DAY    # 0→3 и 6→9
    assert db.billed_outside_paid(opening, events) == 0
    o_all, ev_all = db.subscription_events(c, G1, 0, 2 ** 62)
    restored = o_all + sum(x[1] for x in ev_all)
    assert restored == pytest.approx(db.get_balance(G1), abs=1e-9) and restored == pytest.approx(0.0, abs=1e-9)
    c.close()
    assert db.paid_seconds(G1, d(0), until) == 6 * DAY


# ---- формула оценки (bot._subscription_estimate) ----

def test_formula_15_paid_days_of_30_spent_10(tdb):
    now = datetime(2026, 11, 27, 0, 0, tzinfo=BERLIN)       # окно = с 28.10 00:00 (пол 2026-10-28), 30 суток
    since = datetime(2026, 10, 28, 0, 0, tzinfo=BERLIN)
    put_credit(G1, 10.0, since.timestamp())
    put(datetime(2026, 11, 12, 0, 0, tzinfo=BERLIN).timestamp(), G1, 10.0)   # баланс → 0 ровно через 15 суток
    s = bot._subscription_estimate(G1, now)
    assert s["enough"] is True and s["paid_days"] == 15.0 and s["spent"] == 10.0
    assert s["month"] == pytest.approx(20.0) and s["week"] == pytest.approx(4.9)             # 10/15 × 7 × 1.05
    assert s["since"] == "2026-10-28" and s["window_days"] == pytest.approx(30.0, abs=0.01)
    assert bot._subscription_line(s).startswith("В режиме подписки: месяц ≈ $20.00, неделя ≈ $4.90")


def test_formula_free_spend_not_in_estimate_and_quiet_paid_days_count(tdb):
    now = datetime(2026, 11, 27, 0, 0, tzinfo=BERLIN)
    since = datetime(2026, 10, 28, tzinfo=BERLIN).timestamp()
    put_credit(G1, 100.0, since)                            # платный весь месяц, тратит один раз: тихие дни — в знаменателе
    put(since + 5 * DAY, G1, 3.0)
    put(since + 6 * DAY, G1, 50.0, billed=False)            # бесплатное в оценку не входит
    s = bot._subscription_estimate(G1, now)
    assert s["spent"] == 3.0 and s["paid_days"] == pytest.approx(30.0, abs=0.01)
    assert s["month"] == pytest.approx(3.0, abs=0.01)       # 3/30 × 30, а не 3/«1 день с тратами» × 30


def test_formula_min_days_boundary_and_not_enough(tdb):
    now = datetime(2026, 11, 20, 0, 0, tzinfo=BERLIN)
    since = datetime(2026, 10, 28, tzinfo=BERLIN).timestamp()
    put_credit(G1, 1.0, since)
    put(since + 6 * DAY, G1, 1.0)                           # 6 платных суток
    s = bot._subscription_estimate(G1, now)
    assert s["enough"] is False and s["month"] is None and s["week"] is None and s["paid_days"] == 6.0
    assert bot._subscription_line(s).startswith("Цена подписки: мало данных (6.0 дн. в платном режиме")
    put_credit(G2, 1.0, since)
    put(since + 7 * DAY, G2, 1.0)                           # ровно 7 — уже хватает
    s7 = bot._subscription_estimate(G2, now)
    assert s7["enough"] is True and s7["paid_days"] == 7.0 and s7["month"] == pytest.approx(30 / 7)
    assert bot._subscription_estimate(-5, now)["enough"] is False   # чат без записей


def test_window_floor_and_90_days_cap(tdb):
    early = bot._subscription_estimate(G1, datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN))
    assert early["since"] == "2026-10-28" and early["window_days"] == pytest.approx(13.5, abs=0.01)
    late = bot._subscription_estimate(G1, datetime(2027, 3, 1, 12, 0, tzinfo=BERLIN))
    assert late["since"] == "2026-12-01" and late["window_days"] == pytest.approx(90.0, abs=0.01)   # 90 суток назад от 1 марта 12:00
    before = bot._subscription_estimate(G1, datetime(2026, 10, 20, 12, 0, tzinfo=BERLIN))             # «сейчас» раньше пола
    assert before["enough"] is False and before["paid_days"] == 0.0


def test_admin_private_chat_is_always_paid_in_estimate(tdb):
    now = datetime(2026, 11, 27, 0, 0, tzinfo=BERLIN)
    since = datetime(2026, 10, 28, tzinfo=BERLIN).timestamp()
    put(since + 10 * DAY, ADMIN_CHAT, 30.0)                 # без пополнений и writeoff — баланс отрицательный
    s = bot._subscription_estimate(ADMIN_CHAT, now)
    assert s["enough"] is True and s["paid_days"] == pytest.approx(30.0, abs=0.01) and s["month"] == pytest.approx(30.0, abs=0.01)


# ---- недели ----

def test_week_windows_iso_year_boundaries():
    w = bot._cost_week_windows(datetime(2026, 1, 2, 12, 0, tzinfo=BERLIN))
    assert w[0][0] == "2026-W01" and w[0][1].strftime("%Y-%m-%d") == "2025-12-29" and w[0][3] is True    # 29.12.2025 — KW 1 2026
    assert w[1][0] == "2025-W52" and len(w) == 12 and all(not x[3] for x in w[1:])
    w2 = bot._cost_week_windows(datetime(2024, 12, 31, 9, 0, tzinfo=BERLIN))
    assert w2[0][0] == "2025-W01" and w2[0][1].strftime("%Y-%m-%d") == "2024-12-30" and w2[1][0] == "2024-W52"
    w3 = bot._cost_week_windows(datetime(2021, 1, 3, 12, 0, tzinfo=BERLIN), 2)    # воскресенье 3.1.2021 — ещё KW 53 2020
    assert [x[0] for x in w3] == ["2020-W53", "2020-W52"]


def test_week_windows_monday_midnight_contiguous_and_dst_lengths():
    w = bot._cost_week_windows(datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN), 40)
    assert w[0][0] == "2026-W46" and w[0][1].strftime("%Y-%m-%d") == "2026-11-09"
    for older, newer in zip(w[1:], w[:-1]):
        assert older[2] == newer[1]
    assert all(x[1].weekday() == 0 and x[1].hour == 0 and x[1].minute == 0 for x in w)
    by = {x[1].strftime("%Y-%m-%d"): x for x in w}
    hours = lambda x: (x[2].timestamp() - x[1].timestamp()) / 3600
    assert hours(by["2026-03-23"]) == 167 and hours(by["2026-10-19"]) == 169 and hours(by["2026-11-02"]) == 168
    assert by["2026-03-23"][1].utcoffset() == timedelta(hours=1) and by["2026-03-23"][2].utcoffset() == timedelta(hours=2)


NOW = datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN)


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def test_cost_data_weeks_fields_partial_and_sums(tdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    put(at(2026, 11, 9, 0, 0), G1, 1.0)           # понедельник 00:00 — уже KW 46
    put(at(2026, 11, 8, 23, 59), G1, 2.0)         # воскресенье 23:59 — KW 45
    put(at(2026, 11, 10, 9), G1, 4.0, billed=False)
    put(at(2026, 11, 11), G1, 8.0)                # «будущее» относительно NOW — не считается
    d_ = bot._cost_data(G1, True, NOW)
    wk = d_["weeks"]
    assert len(wk) == 12 and wk[0]["week"] == "2026-W46" and wk[0]["kw"] == 46 and wk[0]["iso_year"] == 2026
    assert wk[0]["start"] == "2026-11-09" and wk[0]["end"] == "2026-11-15" and wk[0]["partial"] is True
    assert wk[0]["paid"] == 1.0 and wk[0]["free"] == 4.0
    assert wk[1]["week"] == "2026-W45" and wk[1]["paid"] == 2.0 and wk[1]["partial"] is False and wk[1]["end"] == "2026-11-08"
    assert wk[1]["start"] == "2026-11-02"
    assert [x["start"] for x in wk] == sorted((x["start"] for x in wk), reverse=True)


def test_cost_data_weeks_show_free_false_has_no_free(tdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    put(at(2026, 11, 9), G1, 1.0)
    put(at(2026, 11, 9), G1, 5.0, billed=False, label="freelabel")
    j = bot._cost_data(G1, False, NOW)
    assert "free" not in set(walk_keys(j)) and all("free" not in w for w in j["weeks"])
    assert "freelabel" not in json.dumps(j, ensure_ascii=False)
    assert {"enough", "month", "week", "paid_days", "spent", "window_days", "since"} <= set(j["subscription"])
    full = bot._cost_data(G1, True, NOW)
    assert full["weeks"][0]["free"] == 5.0 and full["weeks"][0]["paid"] == 1.0
    json.dumps(j)                                  # сериализуется для мини-приложения


def test_cost_detail_has_weeks_between_days_and_months_and_subscription_line(tdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    monkeypatch.setattr(bot, "_berlin_now", lambda: NOW)
    put(at(2026, 11, 9), G1, 1.0)
    text = bot._cost_detail(G1)
    lines = text.split("\n")
    i_d, i_w, i_m = (lines.index(x) for x in ("По дням (платное / бесплатное):", "По неделям (платное / бесплатное):",
                                              "По месяцам (платное / бесплатное):"))
    assert i_d < i_w < i_m
    assert lines[i_w + 1] == "  KW 46 (09.11–15.11): $1.00 / $0.00 (по сегодня)" and len(lines[i_w + 1:i_m - 1]) == 12
    assert lines[2].startswith("Цена подписки: мало данных")            # третья строка: после заголовка и баланса
    assert len(text) < bot.TELEGRAM_MSG_LIMIT


def test_cost_detail_year_suffix_only_across_iso_year(tdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    monkeypatch.setattr(bot, "_berlin_now", lambda: datetime(2026, 1, 8, 12, 0, tzinfo=BERLIN))
    lines = bot._cost_detail(G1).split("\n")
    kw = [x for x in lines if x.startswith("  KW ")]
    assert kw[0].startswith("  KW 2 (05.01–11.01)") and kw[1].startswith("  KW 1 (29.12–04.01)")
    assert kw[2].startswith("  KW 52/2025 (22.12–28.12)")


def test_cost_report_chat_scope_has_subscription_second_line_and_total_scope_has_none(tdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    monkeypatch.setattr(bot, "_berlin_now", lambda: NOW)
    own = bot._cost_report(G1, 1).split("\n")
    assert own[0].startswith("Баланс:") and own[1].startswith("Цена подписки: мало данных")
    total = bot._cost_report(None, 1)
    assert "подписк" not in total
    put_credit(ADMIN_CHAT, 1.0, at(2026, 10, 28, 0, 0))
    priv = bot._cost_report(ADMIN_CHAT, ADMIN_CHAT).split("\n")             # личка пользователя — scope=user_id — тоже получает строку
    assert priv[1].startswith("В режиме подписки: месяц") or priv[1].startswith("Цена подписки")

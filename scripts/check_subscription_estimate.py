"""Сверка оценки цены подписки на копии боевой БД (feat/cost-weeks-subscription, ТЗ docs/claude/tz-cost-weeks-subscription.md).

ТОЛЬКО ЧТЕНИЕ: база открывается `sqlite3.connect('file:...?mode=ro', uri=True)`. Работает вне бота: без токенов и без импорта bot.py —
берёт чистую логику из db.py (`subscription_events`, `paid_seconds_from_events`, `billed_outside_paid`).

    python scripts/check_subscription_estimate.py /tmp/claudushka-copy.db [--top 5] [--now 2026-11-10T12:00] [--admin-ids 1,2]

Для N крупнейших по тратам (billed=1) за окно чатов печатает: chat_id, название, 12 недель KW (платное), paid_days, spent, цену месяц/неделя.
Проверки (печатаются для каждого чата и итогом; ненулевое — код выхода 1):
  1. «каждая строка usage_log billed=1 в окне лежит внутри восстановленного платного интервала» — число нарушений;
  2. «восстановленный баланс на now = баланс по формуле get_balance» (`db._BALANCE_SQL`), допуск 1e-6;
  3. тариф «сейчас» по восстановленному балансу совпадает с `balance > PAID_MIN_BALANCE`.
Константы окна/формулы продублированы из bot.py (SUBSCRIPTION_*): импортировать bot.py нельзя — при расхождении поправить в обоих местах.
admin-ids по умолчанию читаются текстом из bot.py (`ADMIN_IDS = {...}`), без импорта: личка админа бота — всё окно платное.
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import db  # noqa: E402  (db.py не имеет побочных эффектов при импорте)

BERLIN_TZ = ZoneInfo("Europe/Berlin")
SUBSCRIPTION_EST_DAYS = 90
SUBSCRIPTION_EST_SINCE = datetime(2026, 10, 28, tzinfo=BERLIN_TZ)
SUBSCRIPTION_MIN_DAYS = 7
SUBSCRIPTION_MONTH_DAYS = 30
SUBSCRIPTION_WEEK_MARKUP = 0.05


def week_windows(now, count=12):
    """Копия bot._cost_week_windows: (метка, начало, следующий понедельник, partial), новые сверху."""
    monday = now.astimezone(BERLIN_TZ).date()
    monday -= timedelta(days=monday.weekday())
    out = []
    for i in range(count):
        d0 = monday - timedelta(days=7 * i)
        iso = d0.isocalendar()
        out.append((f"KW {iso[1]:02d}/{iso[0]}", datetime.combine(d0, dt_time(0, 0), tzinfo=BERLIN_TZ),
                    datetime.combine(d0 + timedelta(days=7), dt_time(0, 0), tzinfo=BERLIN_TZ), i == 0))
    return out


def admin_ids_from_bot():
    try:
        m = re.search(r"^ADMIN_IDS\s*=\s*\{([^}]*)\}", (ROOT / "bot.py").read_text(encoding="utf-8"), re.M)
        return {int(x) for x in re.findall(r"-?\d+", m.group(1))} if m else set()
    except OSError:
        return set()


def chat_title(conn, chat_id):
    try:
        if chat_id < 0:
            r = conn.execute("SELECT name FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
            return (r and r[0]) or str(chat_id)
        r = conn.execute("SELECT full_name, username FROM users WHERE telegram_id = ?", (chat_id,)).fetchone()
        return (r and (r[0] or r[1])) or str(chat_id)
    except sqlite3.Error:
        return str(chat_id)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db_path", help="путь к КОПИИ БД (sqlite3.backup), открывается read-only")
    ap.add_argument("--top", type=int, default=5, help="сколько крупнейших по тратам чатов (по умолчанию 5)")
    ap.add_argument("--now", help="«сейчас» в Berlin, ISO (по умолчанию — текущее время)")
    ap.add_argument("--admin-ids", help="telegram_id админов бота через запятую (по умолчанию — из bot.py текстом)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    admins = {int(x) for x in args.admin_ids.split(",") if x.strip()} if args.admin_ids else admin_ids_from_bot()
    now = datetime.fromisoformat(args.now).replace(tzinfo=BERLIN_TZ) if args.now else datetime.now(BERLIN_TZ)
    end = int(now.timestamp()) + 1
    start = max(now - timedelta(days=SUBSCRIPTION_EST_DAYS), SUBSCRIPTION_EST_SINCE)
    since = int(start.timestamp())
    conn = sqlite3.connect(f"file:{Path(args.db_path).resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    print(f"Окно: {start:%Y-%m-%d %H:%M} .. {now:%Y-%m-%d %H:%M} Berlin ({(end - since) / 86400:.1f} дн.); админы бота: {sorted(admins) or 'нет'}")
    top = conn.execute("SELECT chat_id, SUM(cost_usd) AS spent FROM usage_log WHERE billed = 1 AND chat_id IS NOT NULL "
                       "AND ts >= ? AND ts < ? GROUP BY chat_id ORDER BY spent DESC LIMIT ?", (since, end, args.top)).fetchall()
    total_bad = total_diff = 0
    for r in top:
        cid = r["chat_id"]
        always = cid in admins
        opening, events = db.subscription_events(conn, cid, since, end)
        paid_days = db.paid_seconds_from_events(opening, events, since, end, always) / 86400
        spent = sum(-d for _t, d, is_spend in events if is_spend)
        bad = db.billed_outside_paid(opening, events, always)
        # баланс на now: всё до `since` + события окна + (на случай ts >= end) весь хвост — сравниваем с формулой get_balance
        o_all, ev_all = db.subscription_events(conn, cid, 0, 2 ** 62)
        restored = o_all + sum(d for _t, d, _s in ev_all)
        formula = db._balance_conn(conn, cid)
        diff = abs(restored - formula)
        tier_ok = always or ((restored > db.PAID_MIN_BALANCE) == (formula > db.PAID_MIN_BALANCE))
        total_bad += bad
        total_diff += (diff > 1e-6) + (not tier_ok)
        print(f"\nchat_id={cid}  {chat_title(conn, cid)}{'  [админ бота: всё окно платное]' if always else ''}")
        print(f"  paid_days={paid_days:.2f}  spent=${spent:.4f}", end="  ")
        if paid_days >= SUBSCRIPTION_MIN_DAYS:
            per_day = spent / paid_days
            print(f"месяц ≈ ${per_day * SUBSCRIPTION_MONTH_DAYS:.2f}  неделя ≈ ${per_day * 7 * (1 + SUBSCRIPTION_WEEK_MARKUP):.2f}")
        else:
            print(f"мало данных (< {SUBSCRIPTION_MIN_DAYS} дн.)")
        print("  недели (платное): " + "  ".join(
            f"{label}{'*' if partial else ''} ${conn.execute('SELECT COALESCE(SUM(cost_usd),0) FROM usage_log WHERE chat_id=? AND billed=1 AND ts>=? AND ts<?', (cid, int(w0.timestamp()), min(int(w1.timestamp()), end))).fetchone()[0]:.2f}"
            for label, w0, w1, partial in week_windows(now)))
        print(f"  billed=1 вне платного интервала: {bad}")
        print(f"  баланс: восстановлен {restored:.8f} / get_balance {formula:.8f} (разница {diff:.2e}); тариф сейчас совпадает: {tier_ok}")
    conn.close()
    print(f"\nИТОГО: нарушений billed=1 вне платного интервала = {total_bad}; расхождений баланса/тарифа = {total_diff}; чатов проверено = {len(top)}")
    sys.exit(1 if (total_bad or total_diff) else 0)


if __name__ == "__main__":
    main()

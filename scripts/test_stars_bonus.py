"""Тесты П1/П4 ТЗ stars-topup (docs/claude/tz-stars-topup.md → «Правки Алексея 2026-11-10»): раздельные порог и бонус, возврат единственного платежа
снимает проверку и отзывает остаток бонуса, довыдача бонуса до полного. Без сети, во временной SQLite.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_stars_bonus.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

from test_stars_topup import (ADMIN, G1, USER, RefundBot, credits, env, make_update, pay, refund, run_topup)  # noqa: F401  (env — фикстура)
import bot
import db


def bonus_net(chat_id):
    return sum(c["amount"] for c in credits(chat_id) if c["kind"] == "bonus")


def spend(chat_id, amount):
    db.add_credit(chat_id, -amount, "adjust", "потратили", ADMIN)


def test_constants_and_threshold_function():
    assert bot.STARTER_BONUS_PRIVATE == 2.0 and bot.STARTER_BONUS_GROUP == 5.0
    assert bot._verify_min_topup(USER) == 2.0 and bot._verify_min_topup(G1) == 5.0
    assert not hasattr(bot, "VERIFY_MIN_TOPUP")


def test_private_threshold_199_no_200_yes(env):
    run_topup([str(USER), "1.99"])
    assert not bot.is_chat_verified(USER) and bonus_net(USER) == 0
    run_topup([str(USER), "0.01"])
    assert bot.is_chat_verified(USER) and bonus_net(USER) == pytest.approx(2.0)


def test_group_threshold_499_no_500_yes(env):
    run_topup([str(G1), "4.99"])
    assert not bot.is_chat_verified(G1) and bonus_net(G1) == 0
    run_topup([str(G1), "0.01"])
    assert bot.is_chat_verified(G1) and bonus_net(G1) == pytest.approx(5.0)


def test_two_100_star_payments_private_bonus_on_second(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 100, "A1")
    assert not bot.is_chat_verified(USER) and bonus_net(USER) == 0
    pay(USER, 100, "A2")
    assert bot.is_chat_verified(USER) and bonus_net(USER) == pytest.approx(2.0)


def test_stars_200_private_and_500_group(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "B1")
    assert bot.is_chat_verified(USER) and bonus_net(USER) == pytest.approx(2.0)
    pay(G1, 200, "B2", user_id=USER)
    assert not bot.is_chat_verified(G1)
    pay(G1, 500, "B3", user_id=USER)
    assert bot.is_chat_verified(G1) and bonus_net(G1) == pytest.approx(5.0)


def test_verify_gives_same_bonus_amounts(env):
    bot.verify_target(USER, ADMIN)
    bot.verify_target(G1, ADMIN)
    assert bonus_net(USER) == pytest.approx(2.0) and bonus_net(G1) == pytest.approx(5.0)
    bot.verify_target(USER, ADMIN)                                  # повтор не дарит второй раз
    assert bonus_net(USER) == pytest.approx(2.0)


def test_old_bigger_bonus_not_touched(env):
    db.add_credit(G1, 10.0, "bonus", "starter", None)               # бонус до 2026-11-10 ($10 группе)
    bot.verify_target(G1, ADMIN)
    assert bonus_net(G1) == pytest.approx(10.0)


def test_refund_single_payment_unverifies_and_revokes_bonus(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "C1")                                            # topup 2 + bonus 2, баланс 4
    assert bot.is_chat_verified(USER) and db.get_balance(USER) == pytest.approx(4.0)
    b = RefundBot()
    replies = refund("C1", b)
    assert b.calls == [(USER, "C1")]
    assert not bot.is_chat_verified(USER)
    assert bonus_net(USER) == pytest.approx(0.0) and db.get_balance(USER) == pytest.approx(0.0)
    assert "Проверка снята" in replies[0] and "бонус отозван на $2.00" in replies[0]
    assert any(c["note"] == "revoke:C1" and c["kind"] == "bonus" and c["amount"] == pytest.approx(-2.0) for c in credits(USER))


def test_refund_partially_spent_bonus_revokes_only_remainder(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "D1")                                            # баланс 4
    spend(USER, 1.5)                                                # баланс 2.5 ≥ 2 → возврат разрешён
    refund("D1", RefundBot())
    assert db.get_balance(USER) == pytest.approx(0.0) and db.get_balance(USER) >= 0
    assert bonus_net(USER) == pytest.approx(1.5)                    # отозвано только неистраченное 0.5
    assert not bot.is_chat_verified(USER)


def test_repay_tops_bonus_up_to_full_not_more(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "E1")
    spend(USER, 1.5)
    refund("E1", RefundBot())                                       # бонус нетто 1.5
    pay(USER, 200, "E2")                                            # topup_total ≥ $2 → проверка, довыдача 0.5
    assert bot.is_chat_verified(USER)
    assert bonus_net(USER) == pytest.approx(2.0)                    # ровно до полного, не больше
    pay(USER, 200, "E3")
    assert bonus_net(USER) == pytest.approx(2.0)


def test_refund_not_single_keeps_verification_and_bonus(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "F1")
    pay(USER, 200, "F2")
    refund("F1", RefundBot())
    assert bot.is_chat_verified(USER) and bonus_net(USER) == pytest.approx(2.0)
    assert not any(c["note"] and c["note"].startswith("revoke:") for c in credits(USER))


def test_refund_not_single_when_manual_topup_exists(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    run_topup([str(USER), "1", "руками"])
    pay(USER, 200, "G1")                                            # проверка и бонус — на втором пополнении
    refund("G1", RefundBot())
    assert bot.is_chat_verified(USER) and bonus_net(USER) == pytest.approx(2.0)


def test_refund_group_unverifies(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(G1, 500, "H1", user_id=USER)
    assert bot.is_chat_verified(G1)
    refund("H1", RefundBot())
    assert not bot.is_chat_verified(G1) and bonus_net(G1) == pytest.approx(0.0) and db.get_balance(G1) == pytest.approx(0.0)


def test_refund_admin_private_chat_not_unverified(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    db.get_or_create_user(ADMIN)
    db.set_role(ADMIN, "admin")
    pay(ADMIN, 200, "I1", user_id=ADMIN)
    refund("I1", RefundBot())
    assert db.get_user(ADMIN)["role"] == "admin"                    # админов бота не трогаем


def test_refund_telegram_failure_restores_everything(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "J1")
    before = (credits(USER), db.get_balance(USER), bot.is_chat_verified(USER), db.get_user(USER)["role"])
    replies = refund("J1", RefundBot(fail=True))
    assert "Telegram отказал" in replies[0]
    assert (credits(USER), db.get_balance(USER), bot.is_chat_verified(USER), db.get_user(USER)["role"]) == before
    assert db.star_payment_get("J1")["status"] == "paid"


def test_refund_telegram_failure_restores_group_status(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(G1, 500, "K1", user_id=USER)
    c = db.get_conn()
    prev = dict(c.execute("SELECT status, approved_by, approved_at FROM allowed_chats WHERE chat_id=?", (G1,)).fetchone())
    c.close()
    refund("K1", RefundBot(fail=True))
    c = db.get_conn()
    now = dict(c.execute("SELECT status, approved_by, approved_at FROM allowed_chats WHERE chat_id=?", (G1,)).fetchone())
    c.close()
    assert now == prev and bot.is_chat_verified(G1) and bonus_net(G1) == pytest.approx(5.0)


def test_reconstructed_balance_equals_get_balance_after_bonus_revoke(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 200, "L1")
    spend(USER, 1.5)
    refund("L1", RefundBot())
    conn = db.get_conn()
    opening, events = db.subscription_events(conn, USER, 0, 2 ** 31)
    conn.close()
    assert opening + sum(e[1] for e in events) == pytest.approx(db.get_balance(USER))


def test_terms_mention_thresholds(env):
    assert "$2" in bot.STARS_TERMS_TEXT and "$5" in bot.STARS_TERMS_TEXT and "бонус" in bot.STARS_TERMS_TEXT

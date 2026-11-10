"""Тесты пополнения через Telegram Stars (feat/stars-topup, ТЗ docs/claude/tz-stars-topup.md). Без сети, во временной SQLite.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_stars_topup.py -q
(в PowerShell: $env:TELEGRAM_BOT_TOKEN="x"; $env:ANTHROPIC_API_KEY="x"; python -m pytest scripts/test_stars_topup.py -q)
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

ADMIN, USER, OTHER = 1, 7, 8
G1 = -1001000000001
G_BANNED = -1001000000004


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def make_update(user_id, chat_id, msg=None):
    msg = msg or FakeMsg()
    chat = SimpleNamespace(id=chat_id, type="supergroup" if chat_id < 0 else "private")
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=chat,
                           effective_message=msg, message=msg), msg


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, 'G1', 'pending', 0)", (G1,))
    c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, 'GB', 'approved', 1)", (G_BANNED,))
    c.commit()
    c.close()
    db.get_or_create_user(USER)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == ADMIN)
    notices = []

    async def fake_notice(chat_id, text):
        notices.append((chat_id, text))
    monkeypatch.setattr(bot, "_send_chat_notice", fake_notice)
    admin_msgs = []

    async def fake_admins(text):
        admin_msgs.append(text)
    monkeypatch.setattr(bot, "notify_admins", fake_admins)
    return SimpleNamespace(notices=notices, admin_msgs=admin_msgs)


def credits(chat_id):
    c = db.get_conn()
    rows = [dict(r) for r in c.execute("SELECT amount, kind, note, by_user FROM chat_credits WHERE chat_id=? ORDER BY id", (chat_id,))]
    c.close()
    return rows


def run_topup(args, chat_id=ADMIN):
    upd, msg = make_update(ADMIN, chat_id)
    asyncio.run(bot.cmd_topup(upd, SimpleNamespace(args=args)))
    return msg.replies


# ---- характеризация /topup (одинаково до и после выноса apply_topup) ----

def test_topup_characterization_small_no_verify(env):
    replies = run_topup([str(G1), "2.5", "тест"])
    assert credits(G1) == [{"amount": 2.5, "kind": "topup", "note": "тест", "by_user": ADMIN}]
    assert len(replies) == 1
    assert "💰" in replies[0] and "+2.50$ (topup)" in replies[0] and "Баланс:" in replies[0] and "не проверен" in replies[0]
    assert "Авто-проверка" not in replies[0]
    # тариф сменился free→paid: одно сообщение о смене тарифа, не «Ура»
    assert len(env.notices) == 1 and env.notices[0][0] == G1 and "Ура, баланс пополнен" not in env.notices[0][1]


def test_topup_characterization_autoverify_and_bonus(env):
    replies = run_topup([str(G1), "6"])
    assert "Авто-проверка" in replies[0] and "стартовый бонус" in replies[0]
    assert bot.is_chat_verified(G1)
    kinds = [c["kind"] for c in credits(G1)]
    assert kinds == ["topup", "bonus"]
    # повторное пополнение: тариф не меняется → «Ура, баланс пополнен»
    env.notices.clear()
    run_topup([str(G1), "1"])
    assert len(env.notices) == 1 and "Ура, баланс пополнен на $1.00" in env.notices[0][1]


def test_topup_characterization_adjust_negative_writeoff(env):
    run_topup([str(G1), "1"])
    replies = run_topup([str(G1), "-5"])
    assert "(adjust)" in replies[0] and "writeoff" in replies[0]
    assert db.get_balance(G1) == 0
    assert [c["kind"] for c in credits(G1)] == ["topup", "adjust", "writeoff"]


def test_topup_characterization_unknown_id_needs_force(env):
    replies = run_topup(["-1009999", "5"])
    assert "нет в базе" in replies[0] and credits(-1009999) == []


# ---- Stars: конфиг ----

def test_rate_and_packs_parsing():
    assert bot._parse_stars_rate("") == 0.010 and bot._parse_stars_rate("0,013") == 0.013
    assert bot._parse_stars_rate("abc") == 0.010 and bot._parse_stars_rate("-1") == 0.010 and bot._parse_stars_rate("5") == 0.010
    assert bot._parse_stars_packs("") == (100, 200, 500, 1000, 2500)
    assert bot._parse_stars_packs("500, 100;100,x,0,-5,99999") == (100, 500)


# ---- Stars: зачисление ----

def payment_update(user_id, payload, total, charge, currency="XTR", in_chat=None):
    sp = SimpleNamespace(currency=currency, total_amount=total, invoice_payload=payload, telegram_payment_charge_id=charge)
    msg = FakeMsg()
    msg.successful_payment = sp
    user = SimpleNamespace(id=user_id, first_name="Вася", username=None)
    chat = SimpleNamespace(id=in_chat if in_chat is not None else user_id, type="private")
    return SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=msg), msg


def pay(chat_id, stars, charge, user_id=USER, **kw):
    upd, msg = payment_update(user_id, f"topup:{chat_id}:{stars}", stars, charge, **kw)
    asyncio.run(bot.on_successful_payment(upd, SimpleNamespace(bot=None)))
    return msg.replies


def test_successful_payment_credits_once_idempotent(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    replies = pay(USER, 100, "CH1")                  # $1.00 — ниже порога авто-проверки
    assert db.get_balance(USER) == pytest.approx(1.0)
    assert credits(USER) == [{"amount": pytest.approx(1.0), "kind": "topup", "note": "stars:CH1", "by_user": USER}]
    assert "Зачислено $1.00 (100 ⭐)" in replies[0]
    row = db.star_payment_get("CH1")
    assert row["status"] == "paid" and row["rate"] == 0.010 and row["stars"] == 100 and row["payer_id"] == USER
    assert len(env.admin_msgs) == 1 and "CH1" in env.admin_msgs[0]
    # повтор того же апдейта: ничего не зачисляется и не отвечается
    assert pay(USER, 100, "CH1") == []
    assert db.get_balance(USER) == pytest.approx(1.0) and len(credits(USER)) == 1
    c = db.get_conn()
    assert c.execute("SELECT COUNT(*) FROM star_payments").fetchone()[0] == 1
    c.close()


def test_rate_frozen_in_payment_row(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.013)
    pay(USER, 100, "CHR")
    assert db.star_payment_get("CHR")["usd"] == pytest.approx(1.3) and db.get_balance(USER) == pytest.approx(1.3)
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)       # смена курса не трогает записанное
    assert db.star_payment_get("CHR")["usd"] == pytest.approx(1.3)


def test_payment_triggers_autoverify_like_topup(env, monkeypatch):
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    pay(USER, 500, "CH2")                       # $5 = VERIFY_MIN_TOPUP → проверен + стартовый бонус, как у /topup
    assert bot.is_chat_verified(USER)
    assert [c["kind"] for c in credits(USER)] == ["topup", "bonus"]


def test_tier_change_notice_without_double_thanks(env):
    pay(USER, 100, "CHT")                       # free → paid: одно сообщение о смене тарифа, «Ура» не дублирует подтверждение
    assert len(env.notices) == 1 and "Ура, баланс пополнен" not in env.notices[0][1]
    env.notices.clear()
    pay(USER, 100, "CHT2")                      # тариф тот же: уведомлений чату нет (плательщик уже получил «Зачислено»)
    assert env.notices == []


def test_payment_into_group_from_miniapp_notifies_group(env):
    pay(G1, 100, "CH3", in_chat=USER)           # платил в личке с ботом (мини-приложение), пополнял группу
    assert db.get_balance(G1) > 0
    assert any(cid == G1 and "пополнен" in t and "Вася" in t for cid, t in env.notices)


def test_payment_with_bad_payload_is_not_credited(env):
    upd, msg = payment_update(USER, "garbage", 100, "CHX")
    asyncio.run(bot.on_successful_payment(upd, SimpleNamespace(bot=None)))
    assert db.get_balance(USER) == 0 and db.star_payment_get("CHX") is None
    assert env.admin_msgs and "не распознан" in env.admin_msgs[0]
    upd, msg = payment_update(USER, f"topup:{USER}:500", 100, "CHY")     # сумма не совпала с payload
    asyncio.run(bot.on_successful_payment(upd, SimpleNamespace(bot=None)))
    assert db.get_balance(USER) == 0


def test_non_xtr_payment_ignored(env):
    upd, msg = payment_update(USER, f"topup:{USER}:100", 100, "CHW", currency="USD")
    asyncio.run(bot.on_successful_payment(upd, SimpleNamespace(bot=None)))
    assert db.star_payment_get("CHW") is None and db.get_balance(USER) == 0


def test_no_credit_without_successful_payment(env):
    # кнопка и pre_checkout денег не трогают; единственный путь зачисления — on_successful_payment
    sent = []

    class B:
        async def send_invoice(self, chat_id, **kw):
            sent.append(kw)

    class Q:
        data = "pay:100"
        message = SimpleNamespace(chat_id=USER)

        async def answer(self, *a, **k):
            pass
    asyncio.run(bot.on_pay_button(SimpleNamespace(callback_query=Q()), SimpleNamespace(bot=B())))
    assert sent == [] and db.get_balance(USER) == 0 and credits(USER) == []         # с v2.12.1 callback-кнопка счёт не выставляет
    c = db.get_conn()
    assert c.execute("SELECT COUNT(*) FROM star_payments").fetchone()[0] == 0
    c.close()


# ---- Stars: pre_checkout ----

def test_check_star_payment_rules(env):
    chk = bot.check_star_payment
    assert chk(f"topup:{USER}:100", USER, "XTR", 100)[0] is None
    assert chk(f"topup:{G1}:200", USER, "XTR", 200)[0] is None                    # любой участник платит группе
    assert chk(f"topup:{G1}:100", USER, "XTR", 200)[0]                            # сумма ≠ payload
    assert chk(f"topup:{G1}:100", USER, "USD", 100)[0]                            # не XTR
    assert chk(f"topup:{G1}:77", USER, "XTR", 77)[0]                              # нет такого пакета
    assert chk("topup:abc", USER, "XTR", 100)[0] and chk("", USER, "XTR", 100)[0]  # подделанный payload
    assert chk(f"topup:{G_BANNED}:100", USER, "XTR", 100)[0]                      # забаненный чат
    assert chk("topup:-1009999:100", USER, "XTR", 100)[0]                         # неизвестный чат
    assert chk(f"topup:{OTHER}:100", USER, "XTR", 100)[0]                         # чужая личка
    assert chk(f"topup:{USER}:100", 555, "XTR", 100)[0]                           # личка не своя


def test_check_star_payment_banned_payer(env):
    c = db.get_conn()
    c.execute("UPDATE users SET banned = 1 WHERE telegram_id = ?", (USER,))
    c.commit()
    c.close()
    assert bot.check_star_payment(f"topup:{G1}:100", USER, "XTR", 100)[0]


def test_pre_checkout_answers(env):
    answers = []

    class Q:
        invoice_payload, currency, total_amount = f"topup:{G1}:100", "XTR", 100
        from_user = SimpleNamespace(id=USER)

        async def answer(self, ok, error_message=None):
            answers.append((ok, error_message))
    asyncio.run(bot.on_pre_checkout(SimpleNamespace(pre_checkout_query=Q()), None))
    assert answers == [(True, None)]
    Q.invoice_payload = "topup:-1009999:100"
    asyncio.run(bot.on_pre_checkout(SimpleNamespace(pre_checkout_query=Q()), None))
    assert answers[1][0] is False and answers[1][1]


def test_processor_does_not_queue_pre_checkout():
    async def scenario():
        proc = bot.PerChatUpdateProcessor(1)
        gate = asyncio.Event()

        async def long_private_chat():
            await gate.wait()
        upd1 = SimpleNamespace(effective_chat=SimpleNamespace(id=USER, type="private"), effective_user=SimpleNamespace(id=USER))
        t = asyncio.create_task(proc.process_update(upd1, long_private_chat()))
        await asyncio.sleep(0.05)
        done = []

        async def quick():
            done.append(1)
        pre = SimpleNamespace(pre_checkout_query=object(), effective_chat=None, effective_user=SimpleNamespace(id=USER))
        await asyncio.wait_for(proc.process_update(pre, quick()), timeout=1)    # слот один и занят, замок лички занят — не ждёт
        assert done == [1]
        gate.set()
        await t
    asyncio.run(scenario())


def test_gate_lets_successful_payment_through_even_if_chat_banned(env):
    upd, msg = payment_update(USER, f"topup:{G_BANNED}:100", 100, "CHB")
    upd.effective_chat = SimpleNamespace(id=G_BANNED, type="supergroup")
    asyncio.run(bot.gate_update(upd, SimpleNamespace()))         # не бросает ApplicationHandlerStop


# ---- возврат ----

class RefundBot:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def refund_star_payment(self, user_id, telegram_payment_charge_id):
        if self.fail:
            raise RuntimeError("CHARGE_ALREADY_REFUNDED")
        self.calls.append((user_id, telegram_payment_charge_id))
        return True


def refund(charge, bot_obj):
    upd, msg = make_update(ADMIN, ADMIN)
    asyncio.run(bot.cmd_refund(upd, SimpleNamespace(args=[charge] if charge else [], bot=bot_obj)))
    return msg.replies


def test_refund_ok_removes_credit_and_marks(env):
    pay(USER, 100, "R1")
    b = RefundBot()
    replies = refund("R1", b)
    assert b.calls == [(USER, "R1")] and "Возвращено 100" in replies[0]
    assert db.get_balance(USER) == pytest.approx(0.0)
    assert db.star_payment_get("R1")["status"] == "refunded"
    assert credits(USER)[-1]["note"] == "stars-refund:R1" and credits(USER)[-1]["amount"] == pytest.approx(-1.0)
    assert "уже возвращён" in refund("R1", b)[0] and len(b.calls) == 1        # повторно нельзя


def test_refund_rejected_when_balance_lower(env):
    pay(USER, 100, "R2")
    db.add_credit(USER, -0.6, "adjust", "потратили", ADMIN)                 # баланс 0.4 < 1.0
    b = RefundBot()
    replies = refund("R2", b)
    assert "Не возвращаю" in replies[0] and b.calls == []
    assert db.star_payment_get("R2")["status"] == "paid" and db.get_balance(USER) == pytest.approx(0.4)


def test_refund_telegram_failure_rolls_back(env):
    pay(USER, 100, "R3")
    replies = refund("R3", RefundBot(fail=True))
    assert "Telegram отказал" in replies[0]
    assert db.star_payment_get("R3")["status"] == "paid" and db.get_balance(USER) == pytest.approx(1.0)
    assert all(c["note"] != "stars-refund:R3" for c in credits(USER))


def test_refund_admin_only_and_unknown(env):
    upd, msg = make_update(USER, USER)
    asyncio.run(bot.cmd_refund(upd, SimpleNamespace(args=["R1"], bot=RefundBot())))
    assert msg.replies == []
    assert "нет в star_payments" in refund("NOPE", RefundBot())[0]
    assert "Платежей Stars пока нет" in refund(None, RefundBot())[0]


def test_stars_sum_excludes_refunded(env):
    pay(USER, 100, "S1")
    pay(USER, 200, "S2")
    refund("S1", RefundBot())
    stars, usd = db.star_payments_sum(0, 2 ** 31, USER)
    assert stars == 200 and usd == pytest.approx(bot._stars_usd(200))
    assert "200 ⭐" in bot._stars_week_line(0, 2 ** 31, None)


# ---- /pay, /topup не-админа, счёт, тексты ----

def test_pay_buttons_help_and_topup_for_non_admin(env):
    upd, msg = make_update(USER, USER)
    sent = {}

    async def reply(text, **kw):
        sent.update(text=text, **kw)
    msg.reply_text = reply
    bot._pay_link_cache.clear()
    bot._pay_menu.clear()

    class LinkBot:
        async def create_invoice_link(self, **kw):
            return f"https://t.me/$L{kw['prices'][0].amount}"
    asyncio.run(bot.cmd_pay(upd, SimpleNamespace(args=[], bot=LinkBot())))
    rows = sent["reply_markup"].inline_keyboard
    assert [r[0].url for r in rows[:len(bot.STARS_PACKS)]] == [f"https://t.me/$L{n}" for n in bot.STARS_PACKS]
    assert str(bot.STARS_PACKS[0]) + " ⭐" in rows[0][0].text
    upd, msg = make_update(USER, USER)
    asyncio.run(bot.cmd_pay(upd, SimpleNamespace(args=["help"])))
    assert "Как купить звёзды" in msg.replies[0] and "**" not in msg.replies[0] and "@PremiumBot" in msg.replies[0]
    upd, msg = make_update(USER, USER)                                          # /topup не-админа ведёт на /pay
    asyncio.run(bot.cmd_topup(upd, SimpleNamespace(args=[], bot=LinkBot())))
    assert msg.replies and "Пополнение баланса" in msg.replies[0]


def test_invoice_kwargs_limits(env):
    kw = bot._stars_invoice_kwargs(USER, 2500)
    assert kw["currency"] == "XTR" and len(kw["prices"]) == 1 and kw["prices"][0].amount == 2500
    assert 1 <= len(kw["title"]) <= 32 and 1 <= len(kw["description"]) <= 255 and len(kw["payload"].encode()) <= 128
    assert bot.STARS_PAYLOAD_RE.match(kw["payload"])


def test_old_pay_callback_buttons_do_not_send_invoice(env):
    sent, answers = [], []

    class B:
        async def send_invoice(self, chat_id, **kw):
            sent.append((chat_id, kw))

    class Q:
        data = "pay:200"
        message = SimpleNamespace(chat_id=USER)

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))
    for data in ("pay:200", "pay:77"):
        Q.data = data
        asyncio.run(bot.on_pay_button(SimpleNamespace(callback_query=Q()), SimpleNamespace(bot=B())))
    assert sent == []
    assert answers == [("Это меню устарело — отправьте /pay.", True)] * 2


def test_terms_paysupport_and_help_texts(env):
    upd, msg = make_update(USER, USER)
    asyncio.run(bot.cmd_terms(upd, None))
    asyncio.run(bot.cmd_paysupport(upd, None))
    assert "Условия" in msg.replies[0] and "Поддержка самого Telegram" in msg.replies[1]
    assert "/pay" in bot.USER_HELP and "/terms" in bot.USER_HELP and "/paysupport" in bot.USER_HELP and "/refund" in bot.ADMIN_HELP

"""Тесты fix/pay-invoice-links (v2.12.1): /pay даёт URL-кнопки со ссылкой на счёт, а не callback + sendInvoice. Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_pay_links.py -q
"""
import asyncio
from types import SimpleNamespace

import pytest

from test_stars_topup import ADMIN, G1, USER, credits, env, make_update, pay  # noqa: F401  (env — фикстура)
import bot
import db


class MsgBot:
    """Подмена бота: создание ссылок, удаление сообщений, любой send_invoice = нарушение."""
    def __init__(self, fail_for=()):
        self.links, self.deleted, self.invoices, self.fail_for = [], [], [], set(fail_for)

    async def create_invoice_link(self, **kw):
        stars = kw["prices"][0].amount
        self.links.append(kw)
        if stars in self.fail_for:
            raise RuntimeError("boom")
        return f"https://t.me/$LINK{len(self.links)}_{stars}"

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_invoice(self, *a, **kw):
        self.invoices.append(kw)


class Msg:
    _n = 100

    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        Msg._n += 1
        self.sent.append((text, kw))
        return SimpleNamespace(message_id=Msg._n)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    bot._pay_link_cache.clear()
    bot._pay_menu.clear()
    monkeypatch.setattr(bot, "STARS_PACKS", (100, 200, 500))
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    monkeypatch.setattr(bot, "bot_username", "claudushka_test_bot")
    monkeypatch.setattr(bot, "MINIAPP_PUBLIC_URL", "https://example.test")
    monkeypatch.setattr(bot, "_miniapp_create_app", object())     # miniapp_enabled() = True
    monkeypatch.setattr(bot, "MINIAPP_MAIN_APP", True)


def run_pay(chat_id, tg_bot, user=USER):
    upd, _ = make_update(user, chat_id)
    msg = Msg()
    upd.effective_message = upd.message = msg
    asyncio.run(bot.cmd_pay(upd, SimpleNamespace(args=[], bot=tg_bot)))
    return msg.sent


def test_pay_makes_url_buttons_and_sends_no_invoice(env):
    b = MsgBot()
    sent = run_pay(USER, b)
    kb = sent[0][1]["reply_markup"].inline_keyboard
    pack_rows = kb[:3]
    assert [r[0].url.split("_")[-1] for r in pack_rows] == ["100", "200", "500"]
    assert all(r[0].callback_data is None for r in pack_rows)             # не callback: нажатие ничего не шлёт в чат
    assert b.invoices == []                                               # sendInvoice не вызывался
    assert [k["payload"] for k in b.links] == [f"topup:{USER}:{n}" for n in (100, 200, 500)]   # те же kwargs, что у мини-приложения
    assert all(k["currency"] == "XTR" for k in b.links)
    assert "окно оплаты" in sent[0][0]


def test_app_button_private_web_app_group_startapp_or_deeplink(env, monkeypatch):
    priv = run_pay(USER, MsgBot())[0][1]["reply_markup"].inline_keyboard[-1][0]
    assert priv.text == "Открыть в приложении" and priv.web_app.url == f"https://example.test/?chat={USER}"
    grp = run_pay(G1, MsgBot())[0][1]["reply_markup"].inline_keyboard[-1][0]
    assert grp.url == f"https://t.me/claudushka_test_bot?startapp={bot._settings_startapp_param(G1)}" and grp.web_app is None
    monkeypatch.setattr(bot, "MINIAPP_MAIN_APP", False)
    grp2 = run_pay(G1, MsgBot())[0][1]["reply_markup"].inline_keyboard[-1][0]
    assert grp2.url == f"https://t.me/claudushka_test_bot?start=settings_{G1}"
    monkeypatch.setattr(bot, "_miniapp_create_app", None)                 # приложение выключено — кнопки нет, пакеты есть
    kb = run_pay(USER, MsgBot())[0][1]["reply_markup"].inline_keyboard
    assert len(kb) == 3 and all(r[0].url for r in kb)


def test_links_cached_by_chat_stars_rate(env, monkeypatch):
    b = MsgBot()
    run_pay(USER, b)
    run_pay(USER, b)
    assert len(b.links) == 3                                              # второй /pay — из кэша, пять (здесь три) запросов не повторяются
    run_pay(G1, b)
    assert len(b.links) == 6                                              # другой чат — свой ключ
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.020)
    run_pay(USER, b)
    assert len(b.links) == 9                                              # курс сменился — новые ссылки (описание с другой суммой)


def test_failed_link_skips_pack_and_does_not_break_pay(env):
    b = MsgBot(fail_for={200})
    sent = run_pay(USER, b)
    kb = sent[0][1]["reply_markup"].inline_keyboard
    assert [r[0].url.split("_")[-1] for r in kb[:2]] == ["100", "500"]    # 200 пропущен
    assert (USER, 200, bot.STARS_USD_RATE) not in bot._pay_link_cache     # неудача не кэшируется: следующий /pay попробует снова
    b2 = MsgBot()
    run_pay(USER, b2)
    assert [k["prices"][0].amount for k in b2.links] == [200]            # запрошен только недостающий


def test_all_links_failed_says_so(env):
    sent = run_pay(USER, MsgBot(fail_for={100, 200, 500}))
    assert len(sent) == 1 and "Не удалось подготовить счета" in sent[0][0] and "reply_markup" not in sent[0][1]


def test_new_pay_deletes_previous_menu_one_per_chat(env):
    b = MsgBot()
    run_pay(USER, b)
    first = bot._pay_menu[USER]
    assert b.deleted == []
    run_pay(USER, b)
    assert b.deleted == [(USER, first)] and bot._pay_menu[USER] != first
    run_pay(G1, b)                                                        # другой чат — меню первого не трогаем
    assert b.deleted == [(USER, first)] and set(bot._pay_menu) == {USER, G1}


def test_delete_error_is_ignored(env):
    class B(MsgBot):
        async def delete_message(self, chat_id, message_id):
            raise RuntimeError("message to delete not found")
    b = B()
    run_pay(USER, b)
    sent = run_pay(USER, b)
    assert sent and "reply_markup" in sent[0][1]


def test_old_callback_button_answers_popup_without_invoice(env):
    answers, b = [], MsgBot()

    class Q:
        data = "pay:200"
        message = SimpleNamespace(chat_id=USER)

        async def answer(self, text=None, show_alert=False):
            answers.append((text, show_alert))
    asyncio.run(bot.on_pay_button(SimpleNamespace(callback_query=Q()), SimpleNamespace(bot=b)))
    assert answers == [("Это меню устарело — отправьте /pay.", True)] and b.invoices == [] and b.links == []


def test_group_payment_by_link_lands_in_payer_private_and_group_gets_notice(env, monkeypatch):
    """Путь после оплаты по ссылке из группы: successful_payment приходит в ЛИЧКУ плательщика (payload — группа):
    плательщик получает «Зачислено», группа — уведомление о пополнении. Новых правил нет (как у мини-приложения)."""
    monkeypatch.setattr(bot, "STARS_USD_RATE", 0.010)
    replies = pay(G1, 200, "LINKPAY1", user_id=USER)                     # payment_update: чат апдейта — личка USER
    assert "Зачислено $2.00 (200 ⭐)" in replies[0]
    assert db.get_balance(G1) == pytest.approx(2.0) and db.star_payment_get("LINKPAY1")["chat_id"] == G1
    assert any(cid == G1 and "Баланс чата пополнен на $2.00" in text for cid, text in env.notices)
    assert any("LINKPAY1" in t for t in env.admin_msgs)

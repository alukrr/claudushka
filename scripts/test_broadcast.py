"""Тесты рассылки админа бота (feat/broadcast, ТЗ docs/claude/tz-broadcast.md). Без сети, во временной SQLite, Bot API подменён.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_broadcast.py -q
"""
import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, ChatMigrated, Forbidden, NetworkError, RetryAfter

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

ADMIN, ADMIN2, USER, STRANGER = 1, 2, 7, 99
G_ACTIVE, G_OLD, G_BANNED, G_REJECTED = -1001, -1002, -1003, -1004
DAY = 86400


class FakeBot:
    id = 555

    def __init__(self, errors=None):
        self.copies, self.sent, self.errors = [], [], dict(errors or {})

    async def copy_message(self, chat_id, from_chat_id, message_id):
        err = self.errors.get(chat_id)
        if isinstance(err, list):                      # последовательность: первые ответы — ошибки, потом успех
            err = err.pop(0) if err else None
        if err is not None:
            raise err
        self.copies.append((chat_id, from_chat_id, message_id))

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeMsg:
    def __init__(self, reply_to=None):
        self.replies, self.reply_to_message = [], reply_to

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))


def src_message(text="Привет!", **kw):
    base = dict(chat_id=ADMIN, message_id=10, text=text, caption=None, sticker=None, photo=None, video=None,
                animation=None, document=None, voice=None)
    base.update(kw)
    return SimpleNamespace(**base)


def put(chat_id, user_id, ts, is_bot=0):
    c = db.get_conn()
    c.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, is_bot, timestamp) VALUES (?, ?, 'x', 'hi', ?, ?)",
              (chat_id, user_id, is_bot, ts))
    c.commit()
    c.close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    now = int(time.time())
    c = db.get_conn()
    for cid, banned, status in ((G_ACTIVE, 0, "approved"), (G_OLD, 0, "approved"), (G_BANNED, 1, "approved"), (G_REJECTED, 0, "rejected")):
        c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (?, 'g', ?, ?)", (cid, status, banned))
    c.commit()
    c.close()
    put(G_ACTIVE, USER, now - DAY)
    put(G_OLD, USER, now - 90 * DAY)
    put(G_BANNED, USER, now - DAY)
    put(G_REJECTED, USER, now - DAY)
    # только реплики бота не считаются активностью людей
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status, banned) VALUES (-1005, 'botonly', 'approved', 0)")
    c.commit()
    c.close()
    put(-1005, 555, now - DAY, is_bot=1)
    for uid in (ADMIN, ADMIN2, USER, 20, 21, 22):
        db.get_or_create_user(uid)
    c = db.get_conn()
    for uid in (USER, 20, 21, 22):
        c.execute("INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, 'user', 'q', ?)", (uid, now))
    c.execute("UPDATE users SET last_active = ? WHERE telegram_id = 20", (now - 90 * DAY,))       # давно не писал
    c.execute("UPDATE users SET banned = 1 WHERE telegram_id = 21")                                # забанен
    c.execute("UPDATE users SET role = 'banned' WHERE telegram_id = 22")                           # старая роль banned
    c.commit()
    c.close()
    db.get_or_create_user(STRANGER)                                                                # есть в users, но не писал (нет conversations)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid in (ADMIN, ADMIN2))
    monkeypatch.setattr(bot, "ADMIN_IDS", {ADMIN, ADMIN2})
    monkeypatch.setattr(bot, "BROADCAST_RATE_PER_SEC", 30.0)
    bot._broadcast_pending.clear()
    bot._broadcast_run.clear()
    return None


def cmd(args, reply=True, chat_type="private", user=ADMIN, fake_bot=None):
    msg = FakeMsg(src_message() if reply else None)
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(type=chat_type, id=user),
                          effective_message=msg)
    ctx = SimpleNamespace(args=args, bot=fake_bot or FakeBot())
    asyncio.run(bot.cmd_broadcast(upd, ctx))
    return msg


class Q:
    def __init__(self, data, user):
        self.data, self.from_user, self.answers, self.edits = data, SimpleNamespace(id=user), [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


def press(data, user, fake_bot):
    q = Q(data, user)
    asyncio.run(bot.on_broadcast_button(SimpleNamespace(callback_query=q), SimpleNamespace(bot=fake_bot)))
    return q


async def _run_and_wait(fake_bot, bid, ids):
    bot._broadcast_start(fake_bot, bid, ids)
    await bot._broadcast_run["task"]


def run_broadcast(fake_bot, bid, ids):
    asyncio.run(_run_and_wait(fake_bot, bid, ids))


def token_of(msg):
    markup = msg.replies[-1][1]["reply_markup"]
    return markup.inline_keyboard[0][0].callback_data.split(":")[2]


# ---------- отбор получателей ----------

def test_recipients_active(env):
    r = db.broadcast_recipients(30, [ADMIN, ADMIN2])
    assert r["groups"] == [G_ACTIVE]                       # старая, забаненная, rejected и «одни реплики бота» — не получают
    assert set(r["private"]) == {USER, ADMIN, ADMIN2}      # давний 20, забаненные 21/22 и не писавший STRANGER — нет; админы всегда


def test_recipients_all_skips_activity_but_not_bans(env):
    r = db.broadcast_recipients(None, [ADMIN, ADMIN2])
    assert set(r["groups"]) == {G_ACTIVE, G_OLD, -1005}
    assert 20 in r["private"] and 21 not in r["private"] and 22 not in r["private"] and STRANGER not in r["private"]


# ---------- команда и кнопки ----------

def test_only_admin_and_only_private(env):
    assert cmd([], user=USER).replies == []
    assert "только из личного чата" in cmd([], chat_type="supergroup").replies[0][0]


def test_requires_reply(env):
    assert "ответь на него командой" in cmd([], reply=False).replies[0][0]


def test_preview_counts_and_no_send_before_button(env):
    fb = FakeBot()
    m = cmd([], fake_bot=fb)
    text = m.replies[0][0]
    assert "Уйдёт в 1 групп и 3 личек" in text and not fb.copies
    assert db.broadcast_last_unfinished() is None          # без кнопки в БД ничего не создано


def test_confirm_sends_copies_and_report(env):
    fb = FakeBot()
    m = cmd([], fake_bot=fb)
    q = press(f"bc:go:{token_of(m)}", ADMIN, fb)
    assert "запущена" in q.edits[0]
    asyncio.run(asyncio.sleep(0))                           # фоновая задача — свой event loop уже закрыт: гоняем явно ниже
    b = db.broadcast_last_unfinished()
    assert b and b["src_message_id"] == 10


def test_full_run_report_and_group_note(env):
    fb = FakeBot(errors={USER: Forbidden("bot was blocked by the user"), -1005: BadRequest("Chat not found")})
    ids = [G_ACTIVE, USER, ADMIN, -1005]
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", ids, bot._broadcast_note(src_message("Новое: звёзды")))
    run_broadcast(fb, bid, ids)
    assert sorted(c[0] for c in fb.copies) == sorted([G_ACTIVE, ADMIN])
    assert db.broadcast_counts(bid) == {"sent": 2, "blocked": 1, "gone": 1}
    assert "отправлено 2" in fb.sent[-1][1] and "заблокирован/выгнан 1" in fb.sent[-1][1] and "чат недоступен 1" in fb.sent[-1][1]
    assert fb.sent[-1][0] == ADMIN
    assert db.broadcast_get(bid)["status"] == "done"
    rows = db.get_group_history(G_ACTIVE, 5)
    assert any("служебное объявление" in r and "Новое: звёзды" in r for r in rows)     # группе записано, личке — нет


def test_blocked_does_not_stop_run(env):
    fb = FakeBot(errors={5: Forbidden("kicked"), 6: ChatMigrated(-100)})
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", [5, 6, 7], "n")
    run_broadcast(fb, bid, [5, 6, 7])
    assert [c[0] for c in fb.copies] == [7]


def test_retry_after_waits_and_continues(env, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)
    monkeypatch.setattr(bot.asyncio, "sleep", fake_sleep)
    fb = FakeBot(errors={5: [RetryAfter(3)]})
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", [5, 6], "n")
    run_broadcast(fb, bid, [5, 6])
    assert [c[0] for c in fb.copies] == [5, 6]
    assert any(s >= 3 for s in sleeps)


def test_button_only_author_and_once(env):
    fb = FakeBot()
    m = cmd([], fake_bot=fb)
    t = token_of(m)
    assert press(f"bc:go:{t}", USER, fb).edits == []                        # не админ — ничего
    assert "другого админа" in press(f"bc:go:{t}", ADMIN2, fb).answers[0]   # другой админ — отказ, токен цел
    assert t in bot._broadcast_pending
    q1 = press(f"bc:go:{t}", ADMIN, fb)
    assert q1.edits and t not in bot._broadcast_pending
    bot._broadcast_run.get("task") and bot._broadcast_run["task"].cancel()
    q2 = press(f"bc:go:{t}", ADMIN, fb)                                       # второй раз — уже нельзя
    assert q2.edits == [] and "устарел или уже обработан" in q2.answers[0]


def test_cancel_button(env):
    fb = FakeBot()
    m = cmd([], fake_bot=fb)
    t = token_of(m)
    q = press(f"bc:no:{t}", ADMIN, fb)
    assert "отменена" in q.edits[0] and t not in bot._broadcast_pending
    assert db.broadcast_last_unfinished() is None


def test_expired_preview(env):
    fb = FakeBot()
    t = token_of(cmd([], fake_bot=fb))
    bot._broadcast_pending[t]["ts"] -= bot.BROADCAST_CONFIRM_TTL + 1
    q = press(f"bc:go:{t}", ADMIN, fb)
    assert q.edits == [] and "устарел" in q.answers[0]


def test_test_mode_sends_only_to_admins_without_button(env):
    fb = FakeBot()

    async def go():
        msg = FakeMsg(src_message())
        upd = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private", id=ADMIN),
                              effective_message=msg)
        await bot.cmd_broadcast(upd, SimpleNamespace(args=["test"], bot=fb))
        await bot._broadcast_run["task"]
        return msg
    msg = asyncio.run(go())
    assert sorted(c[0] for c in fb.copies) == [ADMIN, ADMIN2]
    assert "Тест" in msg.replies[0][0] and "reply_markup" not in msg.replies[0][1]


def test_one_at_a_time_and_stop(env):
    fb = FakeBot()

    async def go():
        msg = FakeMsg(src_message())
        mk = lambda a: (SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private", id=ADMIN),
                                        effective_message=msg), SimpleNamespace(args=a, bot=fb))
        bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", list(range(100, 140)), "n")
        bot._broadcast_start(fb, bid, list(range(100, 140)))
        await asyncio.sleep(0)
        await bot.cmd_broadcast(*mk([]))
        assert "уже идёт" in msg.replies[-1][0]
        await bot.cmd_broadcast(*mk(["stop"]))
        assert "Останавливаю" in msg.replies[-1][0]
        await bot._broadcast_run["task"]
        return bid
    bid = asyncio.run(go())
    assert len(fb.copies) < 40 and db.broadcast_get(bid)["status"] == "stopped"
    assert "остановлена" in fb.sent[-1][1]


def test_resume_does_not_duplicate(env):
    fb = FakeBot(errors={103: NetworkError("timeout")})
    ids = [101, 102, 103, 104]
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", ids, "n")
    db.broadcast_mark(bid, 101, "sent")
    db.broadcast_mark(bid, 102, "blocked")
    db.broadcast_set_status(bid, "running")                  # как после рестарта посреди рассылки
    assert db.broadcast_pending(bid) == [103, 104]

    async def go():
        msg = FakeMsg()
        upd = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private", id=ADMIN),
                              effective_message=msg)
        await bot.cmd_broadcast(upd, SimpleNamespace(args=["resume"], bot=fb))
        await bot._broadcast_run["task"]
        return msg
    msg = asyncio.run(go())
    assert [c[0] for c in fb.copies] == [104]                # 101 и 102 не трогали; 103 — ошибка сети
    assert db.broadcast_counts(bid) == {"sent": 2, "blocked": 1, "error": 1}
    assert "Дослать оставшимся" in fb.sent[-1][1]
    fb.errors.clear()
    run_broadcast(fb, bid, db.broadcast_pending(bid))        # повтор error-строки
    assert [c[0] for c in fb.copies] == [104, 103] and db.broadcast_counts(bid) == {"sent": 3, "blocked": 1}


def test_resume_nothing(env):
    assert "Нечего досылать" in cmd(["resume"]).replies[0][0]


def test_note_placeholders():
    assert "[стикер]" in bot._broadcast_note(src_message(text=None, sticker=object()))
    assert "[фото] подпись" in bot._broadcast_note(src_message(text=None, caption="подпись", photo=[1]))
    assert len(bot._broadcast_note(src_message("я" * 5000))) <= bot.BROADCAST_NOTE_MAX


def cmd_resume(fb):
    async def go():
        msg = FakeMsg()
        upd = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private", id=ADMIN),
                              effective_message=msg)
        await bot.cmd_broadcast(upd, SimpleNamespace(args=["resume"], bot=fb))
        task = bot._broadcast_run.get("task")
        if task is not None and not task.done():
            await task
        return msg
    return asyncio.run(go())


def test_finished_run_with_error_is_resumable(env):
    fb = FakeBot(errors={102: NetworkError("timeout")})
    ids = [101, 102, 103]
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", ids, "n")
    run_broadcast(fb, bid, ids)                                          # дошла до конца, но у 102 — error
    assert db.broadcast_counts(bid) == {"sent": 2, "error": 1}
    assert db.broadcast_get(bid)["status"] == "stopped"                  # не «done», пока есть error
    assert db.broadcast_last_unfinished()["id"] == bid
    assert "/broadcast resume" in fb.sent[-1][1]                         # совет есть, потому что resume сработает
    fb.errors.clear()
    fb.copies.clear()
    msg = cmd_resume(fb)
    assert "осталось 1" in msg.replies[0][0]
    assert [c[0] for c in fb.copies] == [102]                            # только строке с ошибкой, без дублей
    assert db.broadcast_counts(bid) == {"sent": 3}
    assert db.broadcast_get(bid)["status"] == "done"
    assert "/broadcast resume" not in fb.sent[-1][1]                     # всё дошло — совета нет
    assert "Нечего досылать" in cmd_resume(fb).replies[0][0]
    assert len(fb.copies) == 1


def test_clean_run_report_has_no_resume_advice(env):
    fb = FakeBot()
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "active", [101], "n")
    run_broadcast(fb, bid, [101])
    assert db.broadcast_get(bid)["status"] == "done" and "resume" not in fb.sent[-1][1]


def test_test_mode_error_does_not_advise_resume(env):
    fb = FakeBot(errors={101: NetworkError("timeout")})
    bid = db.broadcast_create(ADMIN, ADMIN, 10, "test", [101], "n")
    run_broadcast(fb, bid, [101])
    assert "resume" not in fb.sent[-1][1] and db.broadcast_last_unfinished() is None

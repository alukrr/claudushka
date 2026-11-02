"""Тесты параллельной обработки апдейтов (feat/concurrent-updates, docs/claude/tz-concurrent-updates.md): PerChatUpdateProcessor —
порядок внутри чата, параллельность между чатами, общий лимит, замок раньше слота, исключения, usage_ctx. Без сети. Запуск из корня:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_concurrent_updates.py -q
"""
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# bot.py при импорте создаёт /app/data (путь контейнера) — вне контейнера подавляем только это.
_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

DELAY = 0.15


def upd(chat=None, user=None, ctype="private"):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat, type=ctype) if chat is not None else None,
                           effective_user=SimpleNamespace(id=user) if user is not None else None,
                           effective_message=None)


class Harness:
    """Имитирует Application.__update_fetcher: на каждый апдейт — своя задача, process_update(update, coroutine)."""

    def __init__(self, limit):
        self.proc = bot.PerChatUpdateProcessor(limit)
        self.events: list[tuple] = []
        self.tasks: list[asyncio.Task] = []

    def feed(self, update, name, delay=DELAY, fail=False):
        async def work():
            self.events.append(("start", name, time.monotonic()))
            await asyncio.sleep(delay)
            self.events.append(("end", name, time.monotonic()))
            if fail:
                raise RuntimeError("boom")

        async def wrapper():
            try:
                await self.proc.process_update(update, work())
            except RuntimeError:
                pass
        self.tasks.append(asyncio.create_task(wrapper()))

    async def done(self):
        await asyncio.gather(*self.tasks)

    def t(self, kind, name):
        return next(ts for k, n, ts in self.events if k == kind and n == name)

    def order(self):
        return [(k, n) for k, n, _ in self.events]


def run(coro):
    return asyncio.run(coro)


def test_same_chat_is_sequential_in_arrival_order():
    async def go():
        h = Harness(16)
        for i in range(4):
            h.feed(upd(1), f"m{i}", delay=DELAY if i == 0 else 0.01)
        await h.done()
        return h
    h = run(go())
    assert h.order() == [x for i in range(4) for x in (("start", f"m{i}"), ("end", f"m{i}"))]   # строго по очереди, порядок прихода


def test_different_chats_run_in_parallel():
    async def go():
        h = Harness(16)
        t0 = time.monotonic()
        for c in range(5):
            h.feed(upd(c + 1), f"c{c}")
        await h.done()
        return time.monotonic() - t0
    assert run(go()) < DELAY * 2.5      # пять чатов за ~одну задержку, не за пять


def test_global_limit_third_chat_waits():
    async def go():
        h = Harness(2)
        for c in range(3):
            h.feed(upd(c + 1), f"c{c}")
        await h.done()
        return h
    h = run(go())
    assert h.t("start", "c2") >= h.t("end", "c0") - 0.01      # третий стартовал только после освобождения слота
    assert h.t("start", "c1") < h.t("end", "c0")              # а второй — сразу


def test_flood_in_one_chat_does_not_take_slots():
    """10 апдейтов чата A + апдейт чата B при лимите 2: B стартует сразу (слот не занят ожидающими A)."""
    async def go():
        h = Harness(2)
        for i in range(10):
            h.feed(upd(1), f"a{i}", delay=0.05)
        h.feed(upd(2), "b")
        t0 = time.monotonic()
        await h.done()
        return h, t0
    h, t0 = run(go())
    assert h.t("start", "b") - t0 < 0.03
    assert h.t("end", "b") < h.t("end", "a9")


def test_exception_does_not_hold_chat_lock_and_dict_empties():
    async def go():
        h = Harness(16)
        h.feed(upd(1), "bad", delay=0.01, fail=True)
        h.feed(upd(1), "good", delay=0.01)
        await h.done()
        return h
    h = run(go())
    assert ("end", "good") in h.order()
    assert h.proc._chat_locks == {}         # записи удалены, память не растёт
    assert h.proc.current_concurrent_updates == 0


def test_update_without_chat_and_user_is_processed():
    async def go():
        h = Harness(16)
        h.feed(upd(), "anon")
        h.feed(object(), "not-an-update")
        await h.done()
        return h
    h = run(go())
    assert ("end", "anon") in h.order() and ("end", "not-an-update") in h.order()
    assert bot.PerChatUpdateProcessor.queue_key(upd(user=7)) == 7
    assert bot.PerChatUpdateProcessor.queue_key(upd(chat=-100, user=7)) == -100


def test_limit_one_is_strictly_sequential():
    async def go():
        h = Harness(1)
        for c in range(3):
            h.feed(upd(c + 1), f"c{c}", delay=0.05)
        await h.done()
        return h
    h = run(go())
    assert h.order() == [x for i in range(3) for x in (("start", f"c{i}"), ("end", f"c{i}"))]


def test_cancel_while_waiting_releases_lock_entry():
    async def go():
        h = Harness(16)
        h.feed(upd(1), "first", delay=0.1)
        h.feed(upd(1), "waiter", delay=0.1)
        await asyncio.sleep(0.02)
        h.tasks[1].cancel()
        await asyncio.gather(*h.tasks, return_exceptions=True)
        return h
    h = run(go())
    assert h.proc._chat_locks == {}
    assert ("start", "waiter") not in h.order()


# --- Как PTB вызывает процессор: контракт, на который опирается класс (исходник PTB 22.8) ---

def test_ptb_contract_application_uses_our_process_update_and_tasks_per_update():
    from telegram.ext import Application
    app = Application.builder().token("1:x").concurrent_updates(bot.PerChatUpdateProcessor(4)).build()
    assert isinstance(app.update_processor, bot.PerChatUpdateProcessor)
    assert app.update_processor.max_concurrent_updates == 4          # >1 → Application создаёт задачу на каждый апдейт
    assert bot.PerChatUpdateProcessor.process_update is not bot.BaseUpdateProcessor.process_update


# --- usage_ctx изолирован между параллельными апдейтами ---

def test_usage_ctx_isolated_between_parallel_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()

    async def go():
        h = Harness(16)

        def handler(chat, ctype, delay):
            u = upd(chat, user=chat, ctype=ctype)

            async def work():
                await bot.gate_update(u, None)          # выставляет usage_ctx (effective_message=None → дальше не идёт)
                await asyncio.sleep(delay)              # второй апдейт успевает выставить СВОЙ контекст
                bot._record_usage("llm", "gemini-3.1-flash-lite", f"t{chat}", 0.001, 10, 5)
            return u, work()
        for chat, ctype, delay in ((-1001, "supergroup", 0.15), (42, "private", 0.05), (-1002, "supergroup", 0.1)):
            u, w = handler(chat, ctype, delay)
            h.tasks.append(asyncio.create_task(h.proc.process_update(u, w)))
        await h.done()
    run(go())
    c = sqlite3.connect(db.DB_PATH)
    rows = {r[0]: (r[1], r[2]) for r in c.execute("SELECT label, chat_id, chat_type FROM usage_log")}
    assert rows == {"t-1001": (-1001, "group"), "t42": (42, "private"), "t-1002": (-1002, "group")}


def test_chat_log_filter_appends_chat(monkeypatch):
    import logging
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "ответ %s", ("ок",), None)
    tok = bot.usage_ctx.set((-555, "group", 1))
    try:
        bot._ChatLogFilter().filter(rec)
        assert rec.getMessage() == "ответ ок [chat=-555]"
        rec2 = logging.LogRecord("x", logging.INFO, __file__, 1, "уже chat=7", (), None)
        bot._ChatLogFilter().filter(rec2)
        assert rec2.getMessage() == "уже chat=7"           # не дублируем
    finally:
        bot.usage_ctx.reset(tok)
    rec3 = logging.LogRecord("x", logging.INFO, __file__, 1, "без контекста", (), None)
    bot._ChatLogFilter().filter(rec3)
    assert rec3.getMessage() == "без контекста"


def test_env_positive_int(monkeypatch):
    monkeypatch.setenv("X_N", "0")
    assert bot._env_positive_int("X_N", 16) == 1
    monkeypatch.setenv("X_N", "abc")
    assert bot._env_positive_int("X_N", 16) == 16
    monkeypatch.delenv("X_N")
    assert bot._env_positive_int("X_N", 16) == 16

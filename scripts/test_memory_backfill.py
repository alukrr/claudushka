"""Тесты /memory_backfill (feat/memory-backfill, docs/claude/tz-private-memory.md часть 2). Без сети, модель подменена.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_memory_backfill.py -q
"""
import asyncio
import json
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

ADMIN, A, B, C = 900, 111, 222, 333
CH = bot.MEMORY_BACKFILL_CHUNK


def fill(uid, n):
    """n строк треда (пары user/assistant; нечётное n — последняя одинокая user)."""
    for i in range(n):
        db.save_message(uid, "user" if i % 2 == 0 else "assistant", f"u{uid} реплика {i}")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (ADMIN, A, B, C):
        db.get_or_create_user(uid, full_name=f"U{uid}")
    monkeypatch.setattr(bot, "MEMORY_BACKFILL_FILE", tmp_path / "memory_backfill.json")
    monkeypatch.setattr(bot, "MEMORY_BACKFILL_PAUSE", 0)
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    e = SimpleNamespace(calls=[], sent=[], answer=lambda uid, msgs: (["факт"], []), fail=set(), hook=None)

    e.real_raw = bot._extract_private_raw

    def fake_raw(user_id, messages, label="extract_memory"):
        e.calls.append((user_id, [m["content"] for m in messages], label))
        if e.hook:
            e.hook(user_id, messages)
        if user_id in e.fail:
            raise RuntimeError("модель недоступна: секретный-текст-ошибки")
        return e.answer(user_id, messages)
    monkeypatch.setattr(bot, "_extract_private_raw", fake_raw)

    async def send_message(chat_id, text):
        e.sent.append((chat_id, text))
    e.bot = SimpleNamespace(send_message=send_message)
    bot._backfill_run.clear()
    return e


def run_worker(e, with_profile=True):
    bot._backfill_run.clear()
    bot._backfill_run["stop"] = False
    asyncio.run(bot._backfill_worker(e.bot, ADMIN, with_profile))


def marks():
    return json.loads(bot.MEMORY_BACKFILL_FILE.read_text())["users"]


def private_facts(uid):
    c = db.get_conn()
    try:
        return [tuple(r) for r in c.execute("SELECT fact, context, chat_id, tier FROM memory WHERE user_id=? ORDER BY id", (uid,))]
    finally:
        c.close()


def test_chunks_in_ascending_order_20_each(env):
    fill(A, 45)
    run_worker(env)
    assert [len(m) for _, m, _ in env.calls] == [20, 20, 5]
    flat = [x for _, m, _ in env.calls for x in m]
    assert flat == [f"u{A} реплика {i}" for i in range(45)]      # от старых к новым, ничего не потеряно
    assert {lbl for _, _, lbl in env.calls} == {"memory_backfill"}


def test_dry_calls_nothing_and_reports(env):
    fill(A, 45)
    fill(B, 10)
    fill(C, 1)   # тред из одной строки пропускается
    replies = []
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private"),
                             effective_message=SimpleNamespace(reply_text=lambda t, **k: replies.append(t) or asyncio.sleep(0)))
    ctx = SimpleNamespace(args=["dry"], bot=env.bot)
    asyncio.run(bot.cmd_memory_backfill(update, ctx))
    assert env.calls == [] and not bot.MEMORY_BACKFILL_FILE.exists()
    assert "людей 2" in replies[0] and "реплик 55" in replies[0] and "вызовов 4" in replies[0]


def test_marks_persist_and_repeat_run_does_not_touch_processed(env):
    fill(A, 30)
    run_worker(env)
    assert len(env.calls) == 2 and marks()[str(A)]["done"] == marks()[str(A)]["to"]
    env.calls.clear()
    run_worker(env)       # «после перезапуска»: состояние читается из файла
    assert env.calls == []


def test_resume_after_restart_from_marks(env):
    fill(A, 60)   # 3 куска; останавливаемся после первого

    def hook(uid, msgs):
        bot._backfill_run["stop"] = True
    env.hook = hook
    run_worker(env)
    assert len(env.calls) == 1
    first_done = marks()[str(A)]["done"]
    env.hook = None
    env.calls.clear()
    run_worker(env)       # продолжение с отметки
    assert [len(m) for _, m, _ in env.calls] == [20, 20]
    assert env.calls[0][1][0] == f"u{A} реплика 20" and first_done < marks()[str(A)]["done"]


def test_stop_finishes_current_call_and_reports(env):
    fill(A, 40)
    fill(B, 40)

    def hook(uid, msgs):
        bot._backfill_run["stop"] = True
    env.hook = hook
    run_worker(env)
    assert len(env.calls) == 1 and "остановлено" in env.sent[-1][1]


def test_chunk_error_keeps_mark_and_moves_to_next_person(env):
    fill(A, 20)
    fill(B, 20)
    env.fail = {A}
    run_worker(env)
    assert [u for u, _, _ in env.calls] == [A, B]
    assert marks()[str(A)]["done"] == 0 and marks()[str(B)]["done"] == marks()[str(B)]["to"]
    assert "ошибок: 1" in env.sent[-1][1] and "секретный-текст-ошибки" not in " ".join(t for _, t in env.sent)
    env.fail = set(); env.calls.clear()
    run_worker(env)       # кусок с ошибкой повторяется, обработанное — нет
    assert [u for u, _, _ in env.calls] == [A]


def test_three_errors_in_a_row_stop_everything(env):
    for uid in (A, B, C):
        fill(uid, 20)
    db.get_or_create_user(444, full_name="U4"); fill(444, 20)
    env.fail = {A, B, C, 444}
    run_worker(env)
    assert len(env.calls) == bot.MEMORY_BACKFILL_MAX_ERRORS and "ошибки модели подряд" in env.sent[-1][1]


def test_profile_proposals_only_with_run_not_with_facts_flag(env):
    fill(A, 10)
    fill(B, 10)
    env.answer = lambda uid, msgs: (["Живёт в Трире"], [{"field": "city", "value": "Трир", "evidence": "я из Трира"}])
    run_worker(env, with_profile=True)
    c = db.get_conn()
    try:
        assert [tuple(r) for r in c.execute("SELECT user_id, context, chat_id, field FROM profile_proposals ORDER BY user_id")] == \
               [(A, "private", 0, "city"), (B, "private", 0, "city")]
    finally:
        c.close()
    # режим «только факты»
    bot.MEMORY_BACKFILL_FILE.unlink()
    c = db.get_conn(); c.execute("DELETE FROM profile_proposals"); c.execute("DELETE FROM memory"); c.commit(); c.close()
    run_worker(env, with_profile=False)
    c = db.get_conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM profile_proposals").fetchone()[0] == 0
    finally:
        c.close()
    assert private_facts(A) == [("Живёт в Трире", "private", None, "long")]


def test_facts_written_private_null_chat_and_exact_duplicates_skipped_beyond_read_cap(env):
    fill(A, 10)
    old = [f"старый факт {i}" for i in range(30)]   # больше потолка чтения 12 long
    db.add_memory_facts(A, old, "private", None)
    env.answer = lambda uid, msgs: (["старый факт 0", "старый факт 0", "новый факт"], [])
    run_worker(env)
    facts = [f[0] for f in private_facts(A)]
    assert facts.count("старый факт 0") == 1 and facts.count("новый факт") == 1
    assert all(f[1:] == ("private", None, "long") for f in private_facts(A))


def test_does_not_touch_group_facts_profile_or_conversations(env):
    fill(A, 20)
    db.add_memory_facts(A, ["групповой"], "group", -100500)
    db.add_profile_proposals(A, "private", None, [{"field": "city", "value": "Гамбург", "evidence": "x"}])
    c = db.get_conn()
    before = (c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], c.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0],
              [tuple(r) for r in c.execute("SELECT * FROM memory WHERE context='group'")])
    c.close()
    run_worker(env)
    c = db.get_conn()
    after = (c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], c.execute("SELECT COUNT(*) FROM user_profile").fetchone()[0],
             [tuple(r) for r in c.execute("SELECT * FROM memory WHERE context='group'")])
    c.close()
    assert before == after


def test_range_fixed_at_first_sight_new_rows_not_backfilled(env):
    fill(A, 20)
    run_worker(env)
    fill(A, 20)           # живой тред растёт — это дело живого каденса, а не первичной обработки
    env.calls.clear()
    run_worker(env)
    assert env.calls == []


def test_usage_goes_to_admin_private_chat_with_label(env, monkeypatch):
    """Настоящий _extract_private_raw с подменённым клиентом: строка usage_log — на личный чат админа, метка memory_backfill."""
    monkeypatch.setattr(bot, "_extract_private_raw", env.real_raw)   # настоящий вызов модели (клиент подменён ниже)
    seen = []

    def create(**kw):
        seen.append(kw)
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text='{"facts": ["Любит чай"], "profile_updates": []}')],
                               stop_reason="end_turn", usage=SimpleNamespace(input_tokens=2000, output_tokens=50, cache_creation_input_tokens=0,
                                                                              cache_read_input_tokens=0))
    monkeypatch.setattr(bot, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    fill(A, 20)
    sent = []

    async def send_message(chat_id, text):
        sent.append(text)
    bot._backfill_run.clear(); bot._backfill_run["stop"] = False
    asyncio.run(bot._backfill_worker(SimpleNamespace(send_message=send_message), ADMIN, True))
    c = db.get_conn()
    try:
        rows = [tuple(r) for r in c.execute("SELECT chat_id, label, model FROM usage_log")]
    finally:
        c.close()
    assert rows == [(ADMIN, "memory_backfill", bot.DEFAULT_MODEL_ID)]            # не на чат пользователя A
    assert not ({"thinking", "tools", "cache_control", "output_config"} & set(seen[0]))
    assert private_facts(A)[0][0] == "Любит чай" and "Расход:" in sent[-1]


def _cmd(args, user=ADMIN, chat_type="private"):
    replies = []

    async def reply_text(t, **k):
        replies.append(t)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_chat=SimpleNamespace(type=chat_type),
                             effective_message=SimpleNamespace(reply_text=reply_text))
    return update, SimpleNamespace(args=args, bot=None), replies


def test_command_refused_in_group_and_for_non_admin(env):
    fill(A, 20)
    for user, ctype, expect_reply in ((ADMIN, "supergroup", True), (A, "private", False)):
        upd, ctx, replies = _cmd(["run"], user, ctype)
        asyncio.run(bot.cmd_memory_backfill(upd, ctx))
        assert bool(replies) == expect_reply and not bot._backfill_active() and env.calls == []


def test_second_run_refused_while_running(env):
    fill(A, 20)
    out = {}

    async def go():
        gate = asyncio.Event()

        async def slow_worker(*a, **k):
            await gate.wait()
        orig = bot._backfill_worker
        bot._backfill_worker = slow_worker
        try:
            upd, ctx, r1 = _cmd(["run"])
            ctx.bot = env.bot
            await bot.cmd_memory_backfill(upd, ctx)
            upd2, ctx2, r2 = _cmd(["run"])
            ctx2.bot = env.bot
            await bot.cmd_memory_backfill(upd2, ctx2)
            out["r1"], out["r2"] = r1, r2
            # stop
            upd3, ctx3, r3 = _cmd(["stop"])
            await bot.cmd_memory_backfill(upd3, ctx3)
            out["stop_flag"] = bot._backfill_run["stop"]
            gate.set()
            await bot._backfill_run["task"]
        finally:
            bot._backfill_worker = orig
    asyncio.run(go())
    assert "Запускаю" in out["r1"][0] and "уже идёт" in out["r2"][0] and out["stop_flag"] is True


def test_state_file_written_atomically_via_replace(env, monkeypatch):
    replaced = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (replaced.append((str(a), str(b))), real(a, b))[1])
    fill(A, 20)
    run_worker(env)
    assert replaced and replaced[0][0].endswith(".tmp") and replaced[0][1].endswith("memory_backfill.json")
    assert not Path(replaced[0][0]).exists()


def test_help_and_registration():
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("memory_backfill", cmd_memory_backfill)' in src
    assert "/memory_backfill" in bot.ADMIN_HELP and "/memory_backfill" not in bot.USER_HELP

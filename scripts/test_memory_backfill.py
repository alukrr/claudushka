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
    e = SimpleNamespace(calls=[], sent=[], answer=lambda uid, msgs: (["факт"], []), fail=set(), hook=None, bad=False)

    e.real_raw = bot._extract_private_raw

    def fake_raw(user_id, messages, label="extract_memory", diag=None):
        e.calls.append((user_id, [m["content"] for m in messages], label))
        if e.hook:
            e.hook(user_id, messages)
        if e.bad and diag is not None:
            diag["bad"] = True
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


def fill_long(uid, n, size):
    """n строк по size знаков, у каждой уникальный префикс «#i» — чтобы видеть, что дошло."""
    for i in range(n):
        db.save_message(uid, "user" if i % 2 == 0 else "assistant", f"#{i} " + "я" * (size - len(f"#{i} ")))


def test_long_chunk_no_row_skipped_mark_on_last_sent(env):
    fill_long(A, 20, 3000)           # каждая реплика режется до 1500; в 12000 влезает 8 → вызовы 8 + 8 + 4
    run_worker(env)
    sizes = [len(m) for _, m, _ in env.calls]
    assert sizes == [8, 8, 4]
    seen = [x.split(" ")[0] for _, m, _ in env.calls for x in m]
    assert seen == [f"#{i}" for i in range(20)]                     # ни одна строка не пропущена, порядок цел
    for _, m, _ in env.calls:         # то, что реально уйдёт в запрос (обрезку делает _extract_private_raw): ни одна из отправленных строк не теряется
        clipped = bot.clip_extract_messages([{"role": "user", "content": x} for x in m])
        assert len(clipped) == len(m)
        assert all(len(c["content"]) <= bot.MEMORY_EXTRACT_MSG_CHARS for c in clipped)
        assert sum(len(c["content"]) for c in clipped) <= bot.MEMORY_EXTRACT_TOTAL_CHARS
    assert marks()[str(A)]["done"] == marks()[str(A)]["to"]


def test_mark_after_first_long_call_is_on_last_sent_row(env):
    fill_long(A, 20, 3000)

    def hook(uid, msgs):
        bot._backfill_run["stop"] = True
    env.hook = hook
    run_worker(env)
    assert len(env.calls) == 1 and len(env.calls[0][1]) == 8
    c = db.get_conn()
    try:
        ids = [r[0] for r in c.execute("SELECT id FROM conversations WHERE user_id=? ORDER BY id", (A,))]
    finally:
        c.close()
    assert marks()[str(A)]["done"] == ids[7]                         # 8-я строка — последняя отправленная; 9-я пойдёт следующим вызовом
    env.hook = None; env.calls.clear()
    run_worker(env)
    assert env.calls[0][1][0].startswith("#8 ")                      # продолжение — ровно с неотправленной строки


def test_single_row_longer_than_limit_does_not_loop(env):
    db.save_message(A, "user", "я" * 50000)
    db.save_message(A, "assistant", "ок")
    run_worker(env)
    assert [len(m) for _, m, _ in env.calls] == [2]
    assert len(bot.clip_extract_messages([{"role": "user", "content": env.calls[0][1][0]}])[0]["content"]) == bot.MEMORY_EXTRACT_MSG_CHARS
    assert marks()[str(A)]["done"] == marks()[str(A)]["to"]
    env.calls.clear()
    run_worker(env)
    assert env.calls == []


def test_take_minimum_one_row_and_exact_limit(monkeypatch):
    assert bot._backfill_take([100000, 5]) == 2                      # каждая строка режется до 1500, потому обе влезают
    assert bot._backfill_take([1500] * 20) == 8                      # 8 × 1500 = 12000 влезает ровно
    assert bot._backfill_take([100] * 20) == 20
    assert bot._backfill_take([]) == 0
    monkeypatch.setattr(bot, "MEMORY_EXTRACT_TOTAL_CHARS", 1000)     # общий лимит меньше одной строки: минимум одна всё равно уходит
    assert bot._backfill_take([2000, 5]) == 1


def test_plan_calls_equal_real_calls_on_mixed_lengths(env):
    sizes = [50, 3000, 4000, 200, 9000, 100, 100, 2500, 700, 12000, 40] * 7     # 77 строк разной длины
    for i, n in enumerate(sizes):
        db.save_message(A, "user" if i % 2 == 0 else "assistant", "я" * n)
    fill(B, 45)
    planned = bot.backfill_plan({"users": {}})
    run_worker(env)
    assert planned["calls"] == len(env.calls) and planned["rows"] == 77 + 45
    assert planned["calls"] > -(-77 // bot.MEMORY_BACKFILL_CHUNK) + -(-45 // bot.MEMORY_BACKFILL_CHUNK)   # дробление длинных кусков видно в оценке


def test_dry_reports_exact_calls_for_long_rows(env):
    fill_long(A, 20, 3000)           # 3 вызова, а не 1
    replies = []

    async def reply_text(t, **k):
        replies.append(t)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=ADMIN), effective_chat=SimpleNamespace(type="private"),
                             effective_message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_memory_backfill(update, SimpleNamespace(args=["dry"], bot=env.bot)))
    assert "вызовов 3" in replies[0] and env.calls == []


def test_empty_answers_counted_in_report_and_mark_moves(env):
    fill(A, 40)                      # 2 куска
    env.bad = True
    run_worker(env)
    assert "пустых ответов: 2" in env.sent[-1][1]
    assert marks()[str(A)]["done"] == marks()[str(A)]["to"]          # отметку двигаем, как раньше


def _raw_env(monkeypatch, text, stop="end_turn"):
    def create(**kw):
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0, cache_read_input_tokens=0))
    monkeypatch.setattr(bot, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))


@pytest.mark.parametrize("text,stop,bad", [
    ('{"facts": ["a"], "profile_updates": []}', "end_turn", False),
    ('{"facts": [], "profile_updates": []}', "end_turn", False),      # честное «ничего нового» — не пустой ответ
    ("извини, не могу", "end_turn", True),                            # не JSON
    ("", "end_turn", True),                                           # пусто
    ('{"facts": ["a", "b"', "max_tokens", True),                      # обрезан
])
def test_raw_marks_bad_answers(env, monkeypatch, text, stop, bad):
    monkeypatch.setattr(bot, "_extract_private_raw", env.real_raw)
    _raw_env(monkeypatch, text, stop)
    bot.usage_ctx.set((ADMIN, "private", ADMIN))
    diag = {}
    bot._extract_private_raw(A, [{"role": "user", "content": "привет"}], "memory_backfill", diag)
    assert bool(diag.get("bad")) is bad


def test_live_extraction_still_keeps_last_messages_when_too_long():
    # живое извлечение не менялось: при переполнении отбрасываются самые СТАРЫЕ (последние реплики важнее)
    many = [{"role": "user", "content": f"#{i} " + "я" * 1495} for i in range(10)]
    out = bot.clip_extract_messages(many)
    assert out[-1]["content"].startswith("#9 ") and out[0]["content"].startswith("#2 ")


def test_help_and_registration():
    src = (Path(__file__).resolve().parent.parent / "bot.py").read_text(encoding="utf-8")
    assert 'CommandHandler("memory_backfill", cmd_memory_backfill)' in src
    assert "/memory_backfill" in bot.ADMIN_HELP and "/memory_backfill" not in bot.USER_HELP
    lines = bot.ADMIN_HELP.split("\n")
    i = next(n for n, l in enumerate(lines) if l.lstrip().startswith("/broadcast "))
    j = next(n for n, l in enumerate(lines) if l.lstrip().startswith("/memory_backfill"))
    assert j > i + 1 and lines[i + 1].lstrip().startswith("/broadcast test")        # строка не разрывает описание /broadcast

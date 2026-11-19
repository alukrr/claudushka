"""Тесты автоматической дедупликации памяти (feat/dedup-auto, docs/claude/tz-dedup-auto.md). Без сети, модель — фейк.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts/test_dedup_auto.py -q
"""
import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
import dedup_memory as dm  # noqa: E402
Path.mkdir = _orig_mkdir

ADMIN, U1, U2, U3 = 900, 111, 222, 333
NOW = int(time.time())
OLD = NOW - 10 * 86400


def run(coro):
    return asyncio.run(coro)


def ins(uid, n, created=OLD, ctx="private", chat_id=None, tier="long", expires=None, prefix="факт"):
    conn = db.get_conn()
    for i in range(n):
        conn.execute("INSERT INTO memory (user_id, fact, context, chat_id, created_at, tier, expires_at) VALUES (?,?,?,?,?,?,?)",
                     (uid, f"{prefix} {uid}-{ctx}-{chat_id}-{tier}-{i}", ctx, chat_id, created, tier, expires))
    conn.commit()
    conn.close()


def count(uid=None):
    conn = db.get_conn()
    n = conn.execute("SELECT COUNT(*) FROM memory" + (" WHERE user_id=?" if uid else ""), (uid,) if uid else ()).fetchone()[0]
    conn.close()
    return n


def states():
    conn = db.get_conn()
    rows = {(r["user_id"], r["context"], r["chat_key"], r["tier"]): dict(r) for r in conn.execute("SELECT * FROM memory_dedup_state")}
    conn.close()
    return rows


def due_keys(**kw):
    return [k for k, _n, _f in db.dedup_due_groups(bot.DEDUP_AUTO_MIN_FACTS, bot.DEDUP_AUTO_MIN_NEW, bot.DEDUP_AUTO_NEW_RATIO, **kw)]


UNAVAILABLE = "Gemini models are currently unavailable on this service. Retrying will not help; use another model."


def pool_400():
    """Ошибка пула, на которой фолбэк дедупа (create_with_fallback → _native_create) РЕАЛЬНО сработал бы без выключателя."""
    r = httpx.Response(400, request=httpx.Request("POST", "http://pool/v1/messages"), json={"error": {"message": UNAVAILABLE}})
    return anthropic.BadRequestError(UNAVAILABLE, response=r, body={"error": {"message": UNAVAILABLE}})


def resp(facts, out_tok=50, stop="end_turn"):
    return SimpleNamespace(model="gemini-3.8-flash", stop_reason=stop,
                           content=[SimpleNamespace(type="text", text=json.dumps({"facts": facts}, ensure_ascii=False))],
                           usage=SimpleNamespace(input_tokens=100, output_tokens=out_tok, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0, cache_creation=None))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (ADMIN, U1, U2, U3):
        db.get_or_create_user(uid, full_name=f"U{uid}")
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(dm, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(dm, "DATA_DIR", d)
    monkeypatch.setattr(dm, "BACKUPS_DIR", d / "backups")
    monkeypatch.setattr(dm, "RUNS_DIR", d / "dedup_runs")
    monkeypatch.setattr(dm, "CALIBRATION_PATH", d / "dedup_calibration.json")
    monkeypatch.setattr(bot, "DEDUP_AUTO_FILE", d / "dedup_auto.json")
    monkeypatch.setattr(bot, "is_admin", lambda uid: uid == ADMIN)
    monkeypatch.setattr(bot, "ADMIN_IDS", {ADMIN})
    e = SimpleNamespace(calls=[], sent=[], answer=lambda facts: facts[: max(1, len(facts) // 2)], out_tok=50, stop="end_turn", raise_for=None)

    def fake_sync_create(**kw):
        facts = json.loads(kw["messages"][0]["content"])
        e.calls.append((kw.get("label"), kw["model"], facts))
        if e.raise_for and e.raise_for(facts):
            err = e.raise_for(facts)
            raise err if isinstance(err, Exception) else RuntimeError("провайдер упал: секретный-текст")
        return resp(e.answer(facts), e.out_tok, e.stop)
    monkeypatch.setattr(bot, "sync_create", fake_sync_create)
    monkeypatch.setattr(dm, "_native_create", lambda **kw: pytest.fail("_native_create вызван в процессе бота"))

    class Bot:
        async def send_message(self, chat_id, text, **k):
            e.sent.append((chat_id, text))
    e.bot = Bot()
    e.ev = SimpleNamespace(is_set=lambda: False)
    return e


def go(e, budget=2.0):
    return bot._dedup_auto_run_sync(budget, e.ev)


# ---------------------------------------------------------------- отбор due

def test_due_size_threshold(env):
    ins(U1, 9)
    ins(U2, 10)
    assert due_keys() == [(U2, "private", None, "long")]


def test_due_ignores_expired_facts(env):
    ins(U1, 6)
    ins(U1, 6, expires=NOW - 5, tier="long")     # просроченные не считаются живыми
    assert due_keys() == []


def test_due_new_threshold_and_ratio(env):
    ins(U1, 10)
    db.dedup_state_put(U1, "private", None, "long", NOW - 100, 10, 10, "unchanged")
    assert due_keys() == []
    ins(U1, 4, created=NOW)                      # 4 < max(5, 0.2×10=2)
    assert due_keys() == []
    ins(U1, 1, created=NOW, prefix="ещё")        # 5 новых
    assert [k[0] for k in due_keys()] == [U1]
    # большая группа: порог — 20% от facts_after
    ins(U2, 60)
    db.dedup_state_put(U2, "private", None, "long", NOW - 100, 60, 60, "unchanged")
    ins(U2, 11, created=NOW)                     # 11 < ceil(0.2×60)=12
    assert U2 not in [k[0] for k in due_keys()]
    ins(U2, 1, created=NOW, prefix="доп")
    assert U2 in [k[0] for k in due_keys()]


def test_due_order_by_new_desc_and_separate_groups(env):
    ins(U1, 12)
    ins(U1, 12, ctx="group", chat_id=-100, tier="medium")
    db.dedup_state_put(U1, "group", -100, "medium", NOW - 100, 12, 12, "unchanged")
    ins(U1, 6, created=NOW, ctx="group", chat_id=-100, tier="medium", prefix="нов")
    got = db.dedup_due_groups(10, 5, 0.2)
    assert [(k, n) for k, n, _ in got] == [((U1, "private", None, "long"), 12), ((U1, "group", -100, "medium"), 6)]


# ---------------------------------------------------------------- прогон и отметки

def test_merged_facts_are_not_new_after_apply(env):
    ins(U1, 20)
    st = go(env)
    assert st["applied"] == 1 and count(U1) == 10 and st["facts_before"] == 20 and st["facts_after"] == 10
    assert due_keys() == []                      # слитые факты (created_at = now) — не «новые»
    assert states()[(U1, "private", 0, "long")]["status"] == "applied"
    assert states()[(U1, "private", 0, "long")]["facts_after"] == 10
    assert go(env)["due"] == 0                   # второй прогон ничего не платит
    assert len(env.calls) == 1


def test_state_written_for_every_status_except_error(env):
    ins(U1, 20)                                  # applied
    ins(U2, 12)                                  # unchanged
    ins(U3, 12)                                  # truncated
    ins(U3, 12, ctx="group", chat_id=-5)         # empty
    ins(U1, 12, ctx="group", chat_id=-7)         # error
    env.answer = lambda facts: ([] if facts[0].startswith("факт 333-group") else facts if facts[0].startswith("факт 222")
                                else facts[:3] if not facts[0].startswith("факт 333-private") else facts[:2])

    def fake(**kw):
        facts = json.loads(kw["messages"][0]["content"])
        if facts[0].startswith("факт 111-group"):
            raise RuntimeError("упал")
        stop = "max_tokens" if facts[0].startswith("факт 333-private") else "end_turn"
        return resp(env.answer(facts), 50, stop)
    bot.sync_create = fake
    st = go(env)
    s = states()
    assert s[(U1, "private", 0, "long")]["status"] == "applied"
    assert s[(U2, "private", 0, "long")]["status"] == "unchanged"
    assert s[(U3, "private", 0, "long")]["status"] == "truncated"
    assert s[(U3, "group", -5, "long")]["status"] == "empty"
    assert (U1, "group", -7, "long") not in s    # error — без отметки, повторит в следующий раз
    assert st["errors"] == 1 and st["unchanged"] == 1 and st["truncated"] == 1 and st["empty"] == 1 and st["applied"] == 1
    assert count(U2) == 12 and count(U3) == 24   # не применённое не тронуто
    assert (U1, "group", -7, "long") in [k for k, _n, _f in db.dedup_due_groups(10, 5, 0.2)]


def test_guard_not_applied_and_reported(env):
    ins(U1, 20)
    env.answer = lambda facts: facts[:1]         # 1 из 20 = 5% < 10%
    st = go(env)
    assert st["applied"] == 0 and count(U1) == 20
    assert [(k.user_id, b, a) for k, b, a in st["guard"]] == [(U1, 20, 1)]
    assert states()[(U1, "private", 0, "long")]["status"] == "guard"
    assert "Страховка" in bot._dedup_auto_report_text(st, "Итог", 0.1, 1)


def test_budget_stops_and_leaves_rest_untouched(env):
    ins(U1, 30)
    ins(U2, 20)
    ins(U3, 12)
    env.out_tok = 1_000_000                      # один вызов ≈ $3.75 > бюджета
    st = go(env, budget=2.0)
    assert st["processed"] == 1 and st["left"] == 2 and len(env.calls) == 1
    assert len(states()) == 1                    # у ждущих отметки не тронуты — они снова due
    assert {k[0] for k in due_keys()} == {U2, U3}


def test_three_errors_in_a_row_abort(env):
    for u in (U1, U2, U3):
        ins(u, 12)
    env.raise_for = lambda facts: True
    st = go(env)
    assert st["errors"] == 3 and "ошибки модели подряд" in st["abort"] and states() == {}
    assert "секретный-текст" not in bot._dedup_auto_report_text(st, "x", 0, 0)   # сырой текст исключения в отчёт не идёт


def test_model_called_via_sync_create_with_label_and_no_native(env):
    ins(U1, 20)
    go(env)
    assert [c[0] for c in env.calls] == ["memory_dedup"] and env.calls[0][1] == bot.DEDUP_AUTO_MODEL_ID == "gemini-3.8-flash"


def test_native_create_never_called_even_on_pool_error(env, monkeypatch):
    ins(U1, 12)
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    env.raise_for = lambda facts: pool_400()
    go(env)                                       # _native_create в фикстуре роняет тест, если его вызвали
    res = list(dm.run_llm([dm.Group(dm.GroupKey(1, "private", None, "long"), [1, 2], ["a", "b"], None)], "gemini-3.8-flash",
                          bot._DedupClientShim(), None, None, {"no_fallback": True}))
    assert res[0].status == "error" and "BadRequestError" in res[0].error


def test_no_fallback_switch_in_dedup_core(monkeypatch):
    called = []
    monkeypatch.setattr(dm, "_native_create", lambda **kw: called.append(1))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(pool_400())))
    with pytest.raises(anthropic.BadRequestError):
        dm.create_with_fallback(client, "gemini-3.8-flash", {"no_fallback": True}, max_tokens=1)
    assert called == []


def test_backup_before_run_and_purge_pattern(env):
    ins(U1, 20)
    st = go(env)
    files = list((dm.BACKUPS_DIR).glob("*.db"))
    assert [f.name for f in files] == [st["backup"]] and st["backup"].startswith("claudushka-dedup-")
    assert files[0].parent == dm.BACKUPS_DIR       # в bot.DB_BACKUP_DIR (= DATA_DIR/backups) — шаблон purge_old_db_backups `*.db`
    assert bot.DB_BACKUP_DIR.name == dm.BACKUPS_DIR.name == "backups"
    assert st["run_file"] and (dm.RUNS_DIR / st["run_file"]).exists()


def test_backup_failure_cancels_run(env, monkeypatch):
    ins(U1, 20)

    def boom(*a, **k):
        raise dm.BackupError("бэкап не создался (диск)")
    monkeypatch.setattr(dm, "backup_db", boom)
    st = go(env)
    assert "бэкап" in st["abort"] and env.calls == [] and count(U1) == 20 and states() == {}


def test_rows_changed_during_run_skipped_without_state(env):
    ins(U1, 20)
    real = bot.sync_create

    def racing(**kw):
        conn = db.get_conn()
        conn.execute("DELETE FROM memory WHERE id = (SELECT MIN(id) FROM memory)")   # бот/просрочка убрали строку снимка
        conn.commit()
        conn.close()
        return real(**kw)
    bot.sync_create = racing
    st = go(env)
    assert st["applied"] == 0 and len(st["apply_skipped"]) == 1 and states() == {}


# ---------------------------------------------------------------- режимы, команда, задача

def test_mode_file_default_and_roundtrip(env):
    assert bot._dedup_auto_load() == {"mode": "estimate", "last": None}
    bot._dedup_auto_save({"mode": "apply", "last": {"x": 1}})
    assert bot._dedup_auto_load()["mode"] == "apply"
    bot.DEDUP_AUTO_FILE.write_text("{мусор", encoding="utf-8")
    assert bot._dedup_auto_load()["mode"] == "estimate"
    bot.DEDUP_AUTO_FILE.write_text(json.dumps({"mode": "boom"}), encoding="utf-8")
    assert bot._dedup_auto_load()["mode"] == "estimate"


def test_estimate_mode_job_no_model_no_state(env):
    ins(U1, 20)
    job_ctx = SimpleNamespace(bot=env.bot)
    run(bot.dedup_auto_job(job_ctx))             # файла нет → estimate
    assert env.calls == [] and states() == {} and count(U1) == 20
    assert len(env.sent) == 1 and env.sent[0][0] == ADMIN and "estimate" in env.sent[0][1] and "Групп к проверке: 1" in env.sent[0][1]
    assert not dm.BACKUPS_DIR.exists() or not list(dm.BACKUPS_DIR.glob("*.db"))   # estimate бэкапов не делает


def test_off_mode_job_does_nothing(env):
    ins(U1, 20)
    bot._dedup_auto_save({"mode": "off", "last": None})
    run(bot.dedup_auto_job(SimpleNamespace(bot=env.bot)))
    assert env.sent == [] and env.calls == []


def test_report_cost_counts_fallback_label(env, monkeypatch):
    """Найдено на стенде 2026-11-19: при фолбэке бота метка — memory_dedup_fallback, отчёт показывал $0.0000 (0 вызовов)."""
    rows = {"memory_dedup": (1, 0.01), "memory_dedup_fallback": (2, 0.03)}
    monkeypatch.setattr(db, "usage_label_sum", lambda label, chat_id, since=0: rows[label])
    assert bot._dedup_auto_spent(ADMIN, 0) == (3, pytest.approx(0.04))


def test_apply_mode_job_runs_and_reports(env):
    ins(U1, 20)
    bot._dedup_auto_save({"mode": "apply", "last": None})

    async def main():
        await bot.dedup_auto_job(SimpleNamespace(bot=env.bot))
        await bot._dedup_auto_run["task"]
    run(main())
    assert count(U1) == 10 and len(env.sent) == 1
    text = env.sent[0][1]
    assert "готово" in text and "Применено: 1" in text and "20 → 10" in text and "Бэкап:" in text
    assert bot._dedup_auto_load()["last"]["applied"] == 1


def test_second_run_while_active_is_refused(env):
    ins(U1, 20)

    async def main():
        a = bot._dedup_auto_start(env.bot, ADMIN, [ADMIN], "t")
        b = bot._dedup_auto_start(env.bot, ADMIN, [ADMIN], "t")
        await bot._dedup_auto_run["task"]
        return a, b
    assert run(main()) == (True, False)


def test_usage_ctx_set_before_thread(env):
    ins(U1, 20)
    seen = []
    real = bot.sync_create

    def spy(**kw):
        seen.append(bot.usage_ctx.get())
        return real(**kw)
    bot.sync_create = spy

    async def main():
        bot._dedup_auto_start(env.bot, ADMIN, [ADMIN], "t")
        await bot._dedup_auto_run["task"]
    run(main())
    assert seen == [(ADMIN, "private", ADMIN)]


def _upd(uid, chat_type="private", args=()):
    sent = []

    class M:
        async def reply_text(self, t, **k):
            sent.append((t, k))
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_message=M(),
                           effective_chat=SimpleNamespace(type=chat_type)), SimpleNamespace(args=list(args), job_queue=None), sent


def test_command_admin_private_only_and_modes(env):
    u, c, sent = _upd(U1)
    run(bot.cmd_dedup_auto(u, c))
    assert sent == []                            # не админ — молчим
    u, c, sent = _upd(ADMIN, "supergroup")
    run(bot.cmd_dedup_auto(u, c))
    assert "только в личном" in sent[0][0]
    for arg, mode in (("on", "apply"), ("off", "off"), ("estimate-only", "estimate")):
        u, c, sent = _upd(ADMIN, args=[arg])
        run(bot.cmd_dedup_auto(u, c))
        assert bot._dedup_auto_load()["mode"] == mode
    u, c, sent = _upd(ADMIN)
    run(bot.cmd_dedup_auto(u, c))
    assert "Режим: estimate" in sent[0][0] and "ещё не было" in sent[0][0]


def test_command_estimate_and_run_confirm_do_not_call_model(env):
    ins(U1, 20)
    u, c, sent = _upd(ADMIN, args=["estimate"])
    run(bot.cmd_dedup_auto(u, c))
    assert "Групп к проверке: 1" in sent[0][0] and env.calls == [] and states() == {}
    u, c, sent = _upd(ADMIN, args=["run"])
    run(bot.cmd_dedup_auto(u, c))
    assert "reply_markup" in sent[0][1] and env.calls == [] and count(U1) == 20
    kb = sent[0][1]["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == ["da:go", "da:no"]


def test_button_only_admin_private(env):
    ins(U1, 20)
    edits = []

    class Q:
        def __init__(self, uid, data, ctype="private"):
            self.from_user, self.data = SimpleNamespace(id=uid), data
            self.message = SimpleNamespace(chat=SimpleNamespace(type=ctype))
        async def answer(self, *a, **k): pass
        async def edit_message_text(self, t, **k): edits.append(t)
    ctx = SimpleNamespace(bot=env.bot)
    run(bot.on_dedup_auto_button(SimpleNamespace(callback_query=Q(U1, "da:go")), ctx))
    assert edits == [] and not bot._dedup_auto_active()
    run(bot.on_dedup_auto_button(SimpleNamespace(callback_query=Q(ADMIN, "da:no")), ctx))
    assert edits == ["Автодедуп: запуск отменён."]

    async def main():
        await bot.on_dedup_auto_button(SimpleNamespace(callback_query=Q(ADMIN, "da:go")), ctx)
        await bot._dedup_auto_run["task"]
    run(main())
    assert count(U1) == 10


def test_job_schedule_constants():
    assert bot.DEDUP_AUTO_WEEKDAY == 1 and (bot.DEDUP_AUTO_TIME.hour, bot.DEDUP_AUTO_TIME.minute) == (5, 7)
    assert bot.DEDUP_AUTO_MODEL_ID == bot.MODELS["flash"]["id"]
    assert "/dedup_auto" in bot.ADMIN_HELP and "/dedup_auto" not in bot.USER_HELP
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert 'name="dedup_auto"' in src and 'CommandHandler("dedup_auto"' in src and "on_dedup_auto_button" in src


# ---------------------------------------------------------------- чистка state вместе с памятью

def _put_states():
    for u in (U1, U2):
        db.dedup_state_put(u, "private", None, "long", NOW, 10, 10, "applied")
        db.dedup_state_put(u, "group", -1, "long", NOW, 10, 10, "applied")
        db.dedup_state_put(u, "group", -2, "medium", NOW, 10, 10, "applied")


def test_clear_memory_clears_state(env):
    _put_states()
    db.clear_memory(U1, "private")
    assert {k for k in states() if k[0] == U1} == {(U1, "group", -1, "long"), (U1, "group", -2, "medium")}
    db.clear_memory(U1)
    assert {k for k in states() if k[0] == U1} == set() and len([k for k in states() if k[0] == U2]) == 3


def test_forget_apply_clears_state_of_scope(env):
    _put_states()
    db.forget_apply(U1, "group", -1)
    assert {k[2] for k in states() if k[0] == U1} == {0, -2}
    db.forget_apply(U1, "private")
    assert {k[2] for k in states() if k[0] == U1} == {-2}
    db.forget_apply(U2, None)
    assert [k for k in states() if k[0] == U2] == []
    assert any(k[0] == U1 for k in states())

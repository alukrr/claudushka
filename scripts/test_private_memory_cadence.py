"""Тесты личного извлечения памяти (fix/private-memory-extract, docs/claude/tz-private-memory.md). Без сети, модель подменена.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_private_memory_cadence.py -q
"""
import ast
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

U, OTHER = 111, 222
STEP = bot.MEMORY_EXTRACT_EVERY * 2


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (U, OTHER):
        db.get_or_create_user(uid, full_name=f"U{uid}")
    e = SimpleNamespace(launched=[], requests=[], reply='{"facts": [], "profile_updates": []}')

    def fake_extract(uid, messages, is_group=False, chat_id=None):  # подмена самой extract_memory для тестов каденса
        e.launched.append((uid, list(messages)))
    monkeypatch.setattr(bot, "extract_memory", fake_extract)
    monkeypatch.setattr(bot, "is_admin", lambda uid: False)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    return e


def pair(uid, i):
    db.save_message(uid, "user", f"вопрос {i}")
    db.save_message(uid, "assistant", f"ответ {i}")


async def run_cadence(uid):
    """Как после сохранения пары в хендлере: помощник → фоновая задача → ждём её."""
    started = bot._maybe_extract_private_memory(uid)
    for t in list(bot._background_tasks):
        await t
    return started


def test_launches_on_10th_20th_30th_row_and_never_between(env):
    hits = []

    async def go():
        for i in range(1, 16):  # 15 пар = 30 строк
            pair(U, i)
            if await run_cadence(U):
                hits.append(db.count_conversation(U))
    asyncio.run(go())
    assert hits == [10, 20, 30] and len(env.launched) == 3


def test_odd_length_thread_does_not_die(env):
    # тред нечётной длины (одинокая строка в начале, как у старых/сломанных тредов): запуск ровно раз на каждое пересечение кратного 10
    db.save_message(U, "user", "одинокая")
    hits = []

    async def go():
        for i in range(1, 13):
            pair(U, i)
            if await run_cadence(U):
                hits.append(db.count_conversation(U))
    asyncio.run(go())
    assert hits == [11, 21]  # 25 строк — две отсечки; ни одной пропущенной


def test_below_threshold_and_cleared_thread_restarts_count(env):
    async def go():
        for i in range(1, 5):
            pair(U, i)
            assert not await run_cadence(U)  # 2..8 строк
        pair(U, 5)
        assert await run_cadence(U)          # 10-я
        db.clear_conversation(U)             # /clear
        for i in range(1, 5):
            pair(U, i)
            assert not await run_cadence(U)  # счёт заново: до 8 строк тишина
        pair(U, 5)
        assert await run_cadence(U)          # снова 10-я
    asyncio.run(go())
    assert len(env.launched) == 2


def test_other_users_rows_do_not_count(env):
    async def go():
        for i in range(1, 5):
            pair(U, i)
            pair(OTHER, i)
        pair(OTHER, 5)
        assert await run_cadence(OTHER)
        assert not await run_cadence(U)
    asyncio.run(go())
    assert [u for u, _ in env.launched] == [OTHER]


def test_model_gets_exactly_last_10_rows_in_order(env):
    async def go():
        for i in range(1, 8):  # 14 строк
            pair(U, i)
        for i in range(8, 11):
            pair(U, i)  # 20 строк
        await run_cadence(U)
    asyncio.run(go())
    (uid, msgs), = env.launched
    assert uid == U and len(msgs) == STEP
    assert [m["content"] for m in msgs] == [x for i in range(6, 11) for x in (f"вопрос {i}", f"ответ {i}")]
    assert [m["role"] for m in msgs][:2] == ["user", "assistant"]


def test_same_second_pair_keeps_order(env):
    # обе реплики пары сохранены в одну секунду: порядок — по id, не по timestamp
    async def go():
        for i in range(1, 6):
            pair(U, i)
        await run_cadence(U)
    asyncio.run(go())
    roles = [m["role"] for m in env.launched[0][1]]
    assert roles == ["user", "assistant"] * 5


def test_clip_long_message_and_total_limit():
    long_msg = {"role": "user", "content": "я" * 5000}
    out = bot.clip_extract_messages([long_msg])
    assert len(out[0]["content"]) == bot.MEMORY_EXTRACT_MSG_CHARS
    many = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 1500} for i in range(10)]
    out = bot.clip_extract_messages(many)
    assert sum(len(m["content"]) for m in out) <= bot.MEMORY_EXTRACT_TOTAL_CHARS
    assert len(out) == 8 and out == many[2:]  # отброшены самые старые, порядок цел
    assert bot.clip_extract_messages([]) == []
    # одна огромная реплика всё равно попадает (режется до лимита одной реплики), а не обнуляет запрос
    assert len(bot.clip_extract_messages([long_msg, long_msg])) == 2 or bot.MEMORY_EXTRACT_MSG_CHARS * 2 > bot.MEMORY_EXTRACT_TOTAL_CHARS


def test_due_function_boundaries():
    assert [n for n in range(0, 41) if bot.private_extract_due(n)] == [10, 11, 20, 21, 30, 31, 40]


# ---- настоящий extract_memory: запрос, запись фактов, профиль, расход ----

@pytest.fixture()
def real(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    db.get_or_create_user(U, full_name="U")
    monkeypatch.setattr(bot, "is_admin", lambda uid: False)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    e = SimpleNamespace(requests=[], reply="")

    def create(**kw):
        e.requests.append(kw)
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text=e.reply)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))
    monkeypatch.setattr(bot, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))
    return e


def test_fact_saved_private_null_chat_and_profile_proposal_chat0(real):
    real.reply = json.dumps({"facts": ["Любит чай"], "profile_updates": [{"field": "city", "value": "Гамбург", "evidence": "я живу в Гамбурге"}]})
    bot.usage_ctx.set((U, "private", U))
    bot.extract_memory(U, [{"role": "user", "content": "я живу в Гамбурге и люблю чай"}, {"role": "assistant", "content": "ок"}], False, None)
    c = db.get_conn()
    try:
        assert c.execute("SELECT fact, context, chat_id, tier FROM memory WHERE user_id=?", (U,)).fetchall()[0][:] == ("Любит чай", "private", None, "long")
        prop = c.execute("SELECT context, chat_id, field FROM profile_proposals WHERE user_id=?", (U,)).fetchall()
        assert [tuple(r) for r in prop] == [("private", 0, "city")]
        usage = c.execute("SELECT chat_id, label, model, billed FROM usage_log").fetchall()
        assert [tuple(r) for r in usage] == [(U, "extract_memory", bot.DEFAULT_MODEL_ID, 1)]
    finally:
        c.close()


def test_request_has_only_user_rule_limit_and_no_forbidden_params(real):
    real.reply = '{"facts": [], "profile_updates": []}'
    bot.usage_ctx.set((U, "private", U))
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "content": "я" * 4000} for i in range(10)]
    bot.extract_memory(U, msgs, False, None)
    (req,) = real.requests
    assert "ТОЛЬКО из реплик пользователя" in req["system"] and "role=user" in req["system"]
    body = req["messages"][0]["content"]
    dialog = json.loads(body.split("Диалог:\n", 1)[1])
    assert all(len(m["content"]) <= bot.MEMORY_EXTRACT_MSG_CHARS for m in dialog)
    assert sum(len(m["content"]) for m in dialog) <= bot.MEMORY_EXTRACT_TOTAL_CHARS
    assert not ({"thinking", "tools", "cache_control", "output_config"} & set(req))
    assert req["model"] == bot.DEFAULT_MODEL_ID


def test_end_to_end_10_rows_to_fact_and_usage(real):
    real.reply = '{"facts": ["Держит кота"], "profile_updates": []}'

    async def go():
        bot.usage_ctx.set((U, "private", U))
        for i in range(1, 6):
            pair(U, i)
            if i < 5:
                assert not bot._maybe_extract_private_memory(U)
        assert bot._maybe_extract_private_memory(U)
        for t in list(bot._background_tasks):
            await t
    asyncio.run(go())
    assert any("кота" in str(f) for f in db.get_memory_for_private(U))
    c = db.get_conn()
    try:
        assert c.execute("SELECT COUNT(*) FROM usage_log WHERE label='extract_memory' AND chat_id=?", (U,)).fetchone()[0] == 1
    finally:
        c.close()
    sent = json.loads(real.requests[0]["messages"][0]["content"].split("Диалог:\n", 1)[1])
    assert len(sent) == STEP and sent[0]["content"] == "вопрос 1"


# ---- каждое место сохранения пары в личке зовёт помощник ----

def _functions_calling(name_attr):
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    out = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            names = {(c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", None)) for c in calls}
            if name_attr in names:
                out[fn.name] = fn
    return out


def test_every_save_message_site_calls_helper():
    savers = _functions_calling("save_message")
    assert set(savers) == {"_finish_media_answer", "handle_message"}  # новый сохраняющий код — осознанно, с помощником
    for name, fn in savers.items():
        saves = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "save_message"]
        helper = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_maybe_extract_private_memory"]
        # handle_message: основной диалог + ветка рисования; _finish_media_answer: одно место
        assert len(helper) >= (2 if name == "handle_message" else 1), name
        # помощник стоит ПОСЛЕ сохранения (по строкам): иначе пара не попала бы в счёт
        for h in helper:
            assert any(s.lineno < h.lineno for s in saves), (name, h.lineno)


def test_old_window_cadence_is_gone():
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert "msg_count % (MEMORY_EXTRACT_EVERY * 2)" not in src


def test_media_answer_branch_triggers_cadence(env, monkeypatch):
    sent = []

    class Msg:
        async def reply_text(self, text, **kw):
            sent.append(text)

    update = SimpleNamespace(message=Msg(), effective_user=SimpleNamespace(first_name="x", username="x"))

    async def go():
        for i in range(1, 5):
            pair(U, i)  # 8 строк
        await bot._finish_media_answer(update, None, chat_id=U, user_id=U, is_group=False, answer="Красивое фото",
                                       history_user_text="[фото] смотри", request_text="смотри")
        for t in list(bot._background_tasks):
            await t
    asyncio.run(go())
    assert db.count_conversation(U) == 10 and len(env.launched) == 1  # пара ответа на фото двигает счёт и запускает извлечение
    assert env.launched[0][1][-2]["content"] == "[фото] смотри"

"""Тесты /forget с подтверждением (feat/forget-confirm, docs/claude/tz-forget-confirm.md). Без сети, модель подменена.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_forget_confirm.py -q
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.error import BadRequest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
import restore_user_memory as rum  # noqa: E402
Path.mkdir = _orig_mkdir

U, OTHER = 111, 222
G1, G2 = -1000000000001, -1000000000002
NOW = int(time.time())


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    for uid in (U, OTHER):
        db.get_or_create_user(uid, full_name=f"U{uid}")
    monkeypatch.setattr(bot, "FORGET_BACKUP_DIR", tmp_path / "fb")
    monkeypatch.setattr(bot, "is_admin", lambda uid: False)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    return SimpleNamespace(tmp=tmp_path, model_calls=[])


def add_fact(uid, text, chat=G1, tier="long", exp=None, ts=None):
    ctx = "group" if chat else "private"
    c = db.get_conn()
    cur = c.execute("INSERT INTO memory (user_id, fact, context, chat_id, created_at, tier, expires_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, text, ctx, chat, ts or NOW, tier, exp))
    c.commit()
    c.close()
    return cur.lastrowid


def add_profile(uid, chat, field="city", value="X"):
    ctx = "group" if chat else "private"
    c = db.get_conn()
    c.execute("INSERT INTO user_profile (user_id, context, chat_id, field, value, source, updated_at) VALUES (?,?,?,?,?,'admin',1)",
              (uid, ctx, chat or 0, field, value))
    c.execute("INSERT INTO profile_proposals (user_id, context, chat_id, field, new_value, created_at) VALUES (?,?,?,?,?,1)",
              (uid, ctx, chat or 0, field, value))
    c.commit()
    c.close()


def facts(uid, chat="any"):
    c = db.get_conn()
    try:
        if chat == "any":
            rows = c.execute("SELECT fact FROM memory WHERE user_id=? ORDER BY id", (uid,)).fetchall()
        elif chat is None:
            rows = c.execute("SELECT fact FROM memory WHERE user_id=? AND chat_id IS NULL ORDER BY id", (uid,)).fetchall()
        else:
            rows = c.execute("SELECT fact FROM memory WHERE user_id=? AND chat_id=? ORDER BY id", (uid, chat)).fetchall()
        return [r[0] for r in rows]
    finally:
        c.close()


def count(table, uid, chat=None):
    c = db.get_conn()
    try:
        if chat is None:
            return c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0]
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=? AND chat_id=?", (uid, chat)).fetchone()[0]
    finally:
        c.close()


def fill(uid, n, chat=G1, prefix="f"):
    return [add_fact(uid, f"{prefix}{i}", chat) for i in range(n)]


# ---- заглушки Telegram ----

class Msg:
    def __init__(self, chat_id, chat_type, text="", markup=None):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.text, self.reply_markup = text, markup


class Q:
    def __init__(self, data, uid, chat_id, chat_type, markup_error=None):
        self.data, self.from_user, self.message = data, SimpleNamespace(id=uid), Msg(chat_id, chat_type)
        self.answers, self.edits, self.markup_edits, self.markup_error = [], [], [], markup_error

    async def answer(self, text=None, show_alert=False, **k):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **k):
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None, **k):
        if self.markup_error:
            raise self.markup_error
        self.markup_edits.append(reply_markup)


def press(q):
    asyncio.run(bot.on_forget_button(SimpleNamespace(callback_query=q), SimpleNamespace()))
    return q


def cmd(uid, chat_id, chat_type, args=()):
    sent = []

    async def reply_text(text, reply_markup=None, **k):
        sent.append((text, reply_markup))
    upd = SimpleNamespace(effective_user=SimpleNamespace(id=uid), effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
                          message=SimpleNamespace(reply_text=reply_text))
    asyncio.run(bot.cmd_forget(upd, SimpleNamespace(args=list(args))))
    return sent


def go(kind, uid=U, ts=None):
    return f"fg:go:{kind}:{uid}:{ts if ts is not None else int(time.time())}"


@pytest.fixture()
def model(env, monkeypatch):
    """Подмена aux_create: ответ задаётся env.reply (текст) или env.raises."""
    env.reply, env.raises = '{"keep": [1, 2]}', None

    async def fake(**kw):
        env.model_calls.append(kw)
        if env.raises:
            raise env.raises
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text=env.reply)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    monkeypatch.setattr(bot, "aux_create", fake)
    return env


# ---- команда: ничего не удаляет, считает, шлёт кнопки ----

def test_command_in_group_sends_warning_and_deletes_nothing(env):
    fill(U, 6)
    add_profile(U, G1)
    (text, kb), = cmd(U, G1, "supergroup")
    assert "в этом чате: 6 фактов и профиль" in text and "ТОЧНО СТИРАЕМ" in text
    btns = [b for row in kb.inline_keyboard for b in row]
    assert [b.text for b in btns] == ["Да, стереть", "Отмена"]
    assert all(len(b.callback_data.encode()) <= 64 for b in btns)
    assert btns[0].callback_data.startswith(f"fg:go:c:{U}:") and btns[1].callback_data == f"fg:no:{U}"
    assert len(facts(U)) == 6 and count("user_profile", U) == 1


def test_command_private_mentions_group_facts(env):
    add_fact(U, "личный", chat=None)
    fill(U, 3, G1)
    fill(U, 2, G2)
    db.save_message(U, "user", "привет")
    (text, kb), = cmd(U, U, "private")
    assert "1 фактов, профиль и нашу переписку" in text and "5 фактов в 2 чатах" in text and "/forget all" in text
    assert kb.inline_keyboard[0][0].callback_data.startswith(f"fg:go:c:{U}:")


def test_command_all_text_and_group_refusal(env):
    add_fact(U, "личный", chat=None)
    fill(U, 2, G1)
    fill(U, 1, G2)
    (text, kb), = cmd(U, U, "private", ["all"])
    assert "ВООБЩЕ ВСЁ" in text and "4 фактов в личке и 2 чатах" in text and "ничего" in text
    assert kb.inline_keyboard[0][0].callback_data.startswith(f"fg:go:a:{U}:")
    (text, kb), = cmd(U, G1, "supergroup", ["all"])
    assert "только в личке" in text and kb is None
    assert len(facts(U)) == 4


def test_command_empty_scope_has_no_buttons(env):
    fill(U, 2, G2)  # в этом чате (G1) у человека ничего нет
    (text, kb), = cmd(U, G1, "supergroup")
    assert text == bot.FORGET_NOTHING and kb is None
    (text, kb), = cmd(U, U, "private")   # в личке пусто, но в группах есть — подсказка про all
    assert "нечего забывать" in text and "/forget all" in text and kb is None
    (text, kb), = cmd(OTHER, OTHER, "private")   # нигде ничего — без подсказки
    assert text == bot.FORGET_NOTHING and kb is None


def test_command_unknown_arg_is_usage(env):
    (text, kb), = cmd(U, U, "private", ["wat"])
    assert text == bot.FORGET_USAGE and kb is None


# ---- область ----

def test_group_scope_leaves_other_chats_private_and_people(env, model):
    fill(U, 3, G1)
    fill(U, 2, G2)
    add_fact(U, "личный", chat=None)
    fill(OTHER, 3, G1, "o")
    add_profile(U, G1, "city")
    add_profile(U, G2, "name")
    add_profile(U, None, "name")
    add_profile(OTHER, G1)
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert facts(U, G1) == [] and len(facts(U, G2)) == 2 and facts(U, None) == ["личный"] and len(facts(OTHER)) == 3
    assert count("user_profile", U, G1) == 0 and count("profile_proposals", U, G1) == 0
    assert count("user_profile", U, G2) == 1 and count("user_profile", U, 0) == 1 and count("user_profile", OTHER) == 1
    assert q.edits == [bot.FORGET_DONE_PLAIN] and model.model_calls == []


def test_private_scope_leaves_groups_and_clears_thread(env, model):
    fill(U, 3, G1)
    add_fact(U, "личный", chat=None)
    add_profile(U, None, "name")
    add_profile(U, G1, "city")
    db.save_message(U, "user", "x")
    press(Q(go("c"), U, U, "private"))
    assert facts(U, None) == [] and len(facts(U, G1)) == 3
    assert count("user_profile", U, 0) == 0 and count("user_profile", U, G1) == 1
    assert count("conversations", U) == 0


def test_all_wipes_everything_without_model(env, model):
    fill(U, 6, G1)
    fill(U, 6, G2)
    add_fact(U, "личный", chat=None)
    add_profile(U, G1)
    add_profile(U, None, "name")
    fill(OTHER, 2, G1, "o")
    q = press(Q(go("a"), U, U, "private"))
    assert facts(U) == [] and count("user_profile", U) == 0 and count("profile_proposals", U) == 0 and len(facts(OTHER)) == 2
    assert q.edits == [bot.FORGET_DONE_PLAIN] and model.model_calls == []   # ≥5 фактов, но all — без «сувениров» и без модели


def test_all_button_in_group_refused(env, model):
    fill(U, 3)
    q = press(Q(go("a"), U, G1, "supergroup"))
    assert q.answers == [(bot.FORGET_ALL_IN_GROUP, True)] and q.markup_edits == [] and len(facts(U)) == 3


# ---- кнопки: автор, время, отмена, двойное нажатие ----

def test_foreign_press_changes_nothing(env, model):
    fill(U, 6)
    q = press(Q(go("c"), OTHER, G1, "supergroup"))
    assert q.answers == [(bot.FORGET_NOT_YOURS, True)] and q.edits == [] and q.markup_edits == [] and len(facts(U)) == 6
    q = press(Q(f"fg:no:{U}", OTHER, G1, "supergroup"))
    assert q.edits == [] and len(facts(U)) == 6


def test_expired_press_changes_nothing(env, model):
    fill(U, 6)
    q = press(Q(go("c", ts=int(time.time()) - bot.FORGET_CONFIRM_TTL - 1), U, G1, "supergroup"))
    assert q.edits == [bot.FORGET_STALE] and len(facts(U)) == 6 and model.model_calls == []
    q = press(Q(go("c", ts=int(time.time()) - bot.FORGET_CONFIRM_TTL + 30), U, G1, "supergroup"))
    assert q.edits != [bot.FORGET_STALE]   # внутри срока — работает


def test_cancel(env, model):
    fill(U, 6)
    q = press(Q(f"fg:no:{U}", U, G1, "supergroup"))
    assert q.edits == [bot.FORGET_CANCELLED] and len(facts(U)) == 6 and not (env.tmp / "fb").exists()


def test_garbage_callback_data_is_ignored(env, model):
    fill(U, 3)
    for data in ("fg:", "fg:go:c:x:y", "fg:go:z:111:1", "fg:zz:111", "fg:go:c:111"):
        q = press(Q(data, U, G1, "supergroup"))
        assert q.edits == [] and len(facts(U)) == 3


def test_double_yes_second_press_does_nothing(env, model):
    fill(U, 6)
    q1 = press(Q(go("c"), U, G1, "supergroup"))
    assert len(facts(U)) == 2 and "Почти всё забыла" in q1.edits[0]
    kept = facts(U)
    # второе нажатие уже в очереди: Telegram отвечает «message is not modified» на повторное снятие кнопок
    q2 = press(Q(go("c"), U, G1, "supergroup", markup_error=BadRequest("Message is not modified")))
    assert q2.edits == [] and facts(U) == kept and len(model.model_calls) == 1


def test_buttons_removed_before_any_deletion(env, model, monkeypatch):
    fill(U, 6)
    order = []
    real_apply = db.forget_apply
    monkeypatch.setattr(db, "forget_apply", lambda *a, **k: (order.append("apply"), real_apply(*a, **k))[1])

    class QQ(Q):
        async def edit_message_reply_markup(self, reply_markup=None, **k):
            order.append("markup")
    press(QQ(go("c"), U, G1, "supergroup"))
    assert order[0] == "markup" and "apply" in order


# ---- «сувениры» ----

def test_keepsakes_kept_with_original_rows(env, model):
    f_ids = [add_fact(U, f"факт {i}", G1, tier="medium", exp=NOW + 3600 * (i + 1), ts=NOW - 100 + i) for i in range(6)]
    model.reply = '{"keep": [1, 3]}'   # номера по списку «свежие первыми»: 1 = факт 5, 3 = факт 3
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert sorted(facts(U)) == ["факт 3", "факт 5"]
    c = db.get_conn()
    rows = c.execute("SELECT fact, expires_at, tier, created_at FROM memory WHERE user_id=? ORDER BY fact", (U,)).fetchall()
    c.close()
    assert [tuple(r) for r in rows] == [("факт 3", NOW + 3600 * 4, "medium", NOW - 97), ("факт 5", NOW + 3600 * 6, "medium", NOW - 95)]
    assert q.edits[0].startswith("Почти всё забыла. Два факта") and "• факт 5" in q.edits[0] and "• факт 3" in q.edits[0]
    assert "/forget all" in q.edits[0]


def test_less_than_five_live_facts_no_model(env, model):
    for i in range(4):
        add_fact(U, f"a{i}")
    add_fact(U, "старый", tier="medium", exp=NOW - 10)   # просроченный в счёт не идёт
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert facts(U) == [] and model.model_calls == [] and q.edits == [bot.FORGET_DONE_PLAIN]


def test_second_forget_clears_the_two_leftovers(env, model):
    fill(U, 6)
    press(Q(go("c"), U, G1, "supergroup"))
    assert len(facts(U)) == 2
    before = len(model.model_calls)
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert facts(U) == [] and len(model.model_calls) == before and q.edits == [bot.FORGET_DONE_PLAIN]


@pytest.mark.parametrize("reply,left", [
    ("вот мой выбор: ничего", 0),
    ('{"keep": []}', 0),
    ('{"keep": "1,2"}', 0),
    ('{"keep": [99, 0, -1, "1", 1.5, true]}', 0),       # вне диапазона / не целые / bool — всё отброшено
    ('{"keep": [2, 2, 2]}', 1),                         # одинаковые — один
    ('{"keep": [1, 2, 3, 4]}', 2),                      # лишние — не больше двух
    ('{"keep": [1, 99, 3]}', 2),                        # годные берём, мусор отбрасываем
])
def test_model_garbage_and_extras(env, model, reply, left):
    fill(U, 6)
    model.reply = reply
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert len(facts(U)) == left
    if left == 0:
        assert q.edits == [bot.FORGET_DONE_NO_PICK]
    elif left == 1:
        assert q.edits[0].startswith("Почти всё забыла. Один факт")


def test_model_exception_erases_everything(env, model):
    fill(U, 6)
    model.raises = RuntimeError("pool down")
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert facts(U) == [] and q.edits == [bot.FORGET_DONE_NO_PICK]


def test_parse_forget_keep_unit():
    assert bot.parse_forget_keep({"keep": [3, 17]}, 20) == [3, 17]
    assert bot.parse_forget_keep({"keep": [3, 17]}, 10) == [3]
    assert bot.parse_forget_keep(None, 10) == [] and bot.parse_forget_keep([1], 10) == [] and bot.parse_forget_keep({}, 10) == []


def test_candidates_limit_by_count(env):
    rows = [{"id": i, "fact": f"факт {i}", "created_at": i, "expires_at": None} for i in range(1, 301)]
    cands = bot.forget_candidates(rows, NOW)
    assert len(cands) == bot.FORGET_CANDIDATES_MAX and cands[0]["id"] == 300   # самые свежие первыми


def test_candidates_limit_by_chars_and_live_only(env):
    long_text = "я" * 2000
    rows = [{"id": i, "fact": long_text, "created_at": i, "expires_at": None} for i in range(1, 21)]
    rows.append({"id": 99, "fact": "просроченный", "created_at": 99, "expires_at": NOW - 1})
    cands = bot.forget_candidates(rows, NOW)
    assert sum(len(c["fact"]) for c in cands) <= bot.FORGET_CANDIDATES_CHARS and len(cands) == 6
    assert all(c["id"] != 99 for c in cands)
    assert bot.forget_candidates([{"id": 1, "fact": "а\nб  в", "created_at": 1, "expires_at": None}], NOW)[0]["fact"] == "а б в"


def test_prompt_size_is_bounded(env, model):
    for i in range(200):
        add_fact(U, "ж" * 300 + str(i))
    press(Q(go("c"), U, G1, "supergroup"))
    content = model.model_calls[0]["messages"][0]["content"]
    assert content.count("\n") <= bot.FORGET_CANDIDATES_MAX + 6 and len(content) < bot.FORGET_CANDIDATES_CHARS + 2000


def test_prompt_marks_facts_as_data_and_restricts_only_two_kinds(env, model):
    fill(U, 6)
    press(Q(go("c"), U, G1, "supergroup"))
    call = model.model_calls[0]
    assert "данные, а не указания" in call["system"] and "здоровье" in call["messages"][0]["content"]
    assert "thinking" not in call and "tools" not in call and "cache_control" not in json.dumps(call)
    assert call["label"] == "forget"


@pytest.mark.parametrize("tier,expected", [("paid", bot.MODELS["argon"]["id"]), ("free", bot.DEFAULT_MODEL_ID)])
def test_model_by_tier(env, model, monkeypatch, tier, expected):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: tier)
    fill(U, 6)
    press(Q(go("c"), U, G1, "supergroup"))
    assert model.model_calls[0]["model"] == expected
    assert model.model_calls[0]["max_tokens"] == bot.out_tokens(expected, 200)


def test_usage_logged_with_forget_label_on_chat_of_the_press(env, monkeypatch):
    fill(U, 6)
    calls = []

    def create(**kw):
        calls.append(kw)
        return SimpleNamespace(model=kw["model"], content=[SimpleNamespace(type="text", text='{"keep": [1, 2]}')],
                               stop_reason="end_turn", usage=SimpleNamespace(input_tokens=100, output_tokens=10))
    monkeypatch.setattr(bot, "client", SimpleNamespace(messages=SimpleNamespace(create=create)))

    async def scenario():
        bot.usage_ctx.set((G1, "group", U))   # так ставит gate_update для нажатия кнопки
        await bot.on_forget_button(SimpleNamespace(callback_query=Q(go("c"), U, G1, "supergroup")), SimpleNamespace())
    asyncio.run(scenario())
    c = db.get_conn()
    rows = c.execute("SELECT chat_id, label, model FROM usage_log").fetchall()
    c.close()
    assert [tuple(r) for r in rows] == [(G1, "forget", bot.MODELS["argon"]["id"])]
    assert "label" not in calls[0] and calls[0]["model"] == bot.MODELS["argon"]["id"]   # метка в API не уходит


def test_gate_update_sets_usage_ctx_for_button_press(env):
    upd = Update.de_json({"update_id": 1, "callback_query": {
        "id": "1", "chat_instance": "x", "from": {"id": U, "is_bot": False, "first_name": "A"}, "data": f"fg:no:{U}",
        "message": {"message_id": 1, "date": 1700000000, "chat": {"id": G1, "type": "supergroup", "title": "T"}, "text": "w"}}}, None)

    async def scenario():
        bot.usage_ctx.set(None)
        await bot.gate_update(upd, SimpleNamespace())
        return bot.usage_ctx.get()
    assert asyncio.run(scenario()) == (G1, "group", U)


# ---- файл-копия ----

def test_backup_file_content_and_restore_from_json(env, model, monkeypatch):
    fill(U, 4, G1)
    add_fact(U, "в другом чате", G2)
    add_profile(U, G1, "city", "Казань")
    db.save_message(U, "user", "личный тред")
    press(Q(go("c"), U, G1, "supergroup"))
    files = list((env.tmp / "fb").glob(f"{U}-*.json"))
    assert len(files) == 1 and oct(files[0].stat().st_mode)[-3:] == "600"
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["version"] == 1 and data["user_id"] == U and data["scope"] == "chat" and data["context"] == "group" and data["chat_id"] == G1
    assert len(data["memory"]) == 4 and len(data["user_profile"]) == 1 and len(data["profile_proposals"]) == 1
    assert "личный тред" not in files[0].read_text(encoding="utf-8")            # тред в файл не пишем
    assert facts(U, G1) == [] and facts(U, G2) == ["в другом чате"]
    # возврат из файла: слияние, профиль возвращается, предложения — нет, чужой чат не дублируется
    rep = rum.run(None, env.tmp / "t.db", U, apply=True, from_json=files[0], backup_dir=env.tmp / "rb")
    assert len(rep["add"]) == 4 and len(rep["profile_add"]) == 1
    assert sorted(facts(U, G1)) == ["f0", "f1", "f2", "f3"] and facts(U, G2) == ["в другом чате"]
    assert count("user_profile", U, G1) == 1 and count("profile_proposals", U, G1) == 0
    again = rum.run(None, env.tmp / "t.db", U, apply=True, from_json=files[0], backup_dir=env.tmp / "rb")
    assert again["add"] == [] and again["profile_add"] == []


def test_restore_from_json_rejects_other_user_and_garbage(env, tmp_path):
    fill(U, 2)
    scope = db.forget_scope(U, "group", G1)
    path = bot._write_forget_backup(U, "c", "group", G1, scope)
    with pytest.raises(rum.RestoreError, match="не совпадает"):
        rum.run(None, env.tmp / "t.db", OTHER, from_json=path)
    junk = tmp_path / "junk.json"
    junk.write_text("[1, 2]")
    with pytest.raises(rum.RestoreError, match="не файл-копия"):
        rum.run(None, env.tmp / "t.db", U, from_json=junk)
    with pytest.raises(rum.RestoreError, match="ровно один"):
        rum.run(None, env.tmp / "t.db", U)


def test_backup_failure_deletes_nothing(env, model):
    fill(U, 6)
    add_profile(U, G1)
    blocker = env.tmp / "blocker"
    blocker.write_text("x")
    bot.FORGET_BACKUP_DIR = blocker / "sub"   # mkdir внутри файла не получится
    try:
        q = press(Q(go("c"), U, G1, "supergroup"))
    finally:
        bot.FORGET_BACKUP_DIR = env.tmp / "fb"
    assert q.edits == [bot.FORGET_FAIL] and len(facts(U)) == 6 and count("user_profile", U) == 1 and model.model_calls == []
    assert "Errno" not in q.edits[0] and "Traceback" not in q.edits[0]


def test_apply_failure_deletes_nothing_and_says_so(env, model, monkeypatch):
    fill(U, 6)

    def boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(db, "forget_apply", boom)
    q = press(Q(go("c"), U, G1, "supergroup"))
    assert q.edits == [bot.FORGET_FAIL] and len(facts(U)) == 6


def test_forget_apply_is_one_transaction(env, monkeypatch):
    fill(U, 3)
    add_profile(U, G1)
    db.save_message(U, "user", "x")
    real = db.get_conn
    orig_execute = []

    class Boom:
        def __init__(self, conn):
            self.c = conn

        def execute(self, sql, *a):
            if "DELETE FROM conversations" in sql:
                raise RuntimeError("fail in the middle")
            return self.c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self.c, name)
    monkeypatch.setattr(db, "get_conn", lambda: Boom(real()))
    with pytest.raises(RuntimeError):
        db.forget_apply(U, "group", G1, [], clear_thread=True)
    monkeypatch.setattr(db, "get_conn", real)
    assert len(facts(U)) == 3 and count("user_profile", U) == 1 and count("conversations", U) == 1   # откатилось целиком


def test_purge_forget_backups_older_than_30_days(env):
    d = env.tmp / "fb"
    d.mkdir()
    old, fresh = d / "1-1.json", d / "2-2.json"
    for f in (old, fresh):
        f.write_text("{}")
    t = time.time() - (bot.FORGET_BACKUP_DAYS + 1) * 86400
    os.utime(old, (t, t))
    assert bot.purge_forget_backups() == 1 and not old.exists() and fresh.exists()
    assert bot.purge_forget_backups() == 0


def test_purge_forget_backups_without_directory(env):
    assert not (env.tmp / "fb").exists() and bot.purge_forget_backups() == 0


def test_memory_purge_job_cleans_backups(env):
    d = env.tmp / "fb"
    d.mkdir()
    old = d / "1-1.json"
    old.write_text("{}")
    t = time.time() - (bot.FORGET_BACKUP_DAYS + 5) * 86400
    os.utime(old, (t, t))
    asyncio.run(bot.memory_purge_job())
    assert not old.exists()


def test_texts_and_registration(env):
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert 'CallbackQueryHandler(on_forget_button, pattern=r"^fg:")' in src
    assert "/forget all" in bot.USER_HELP

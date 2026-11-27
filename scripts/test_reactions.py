"""Юнит-тесты эмодзи-реакций (v2.25.0, docs/claude/tz-reactions.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_reactions.py -q
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

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
from telegram import ReactionTypeCustomEmoji, ReactionTypeEmoji, ReactionTypePaid  # noqa: E402
from telegram.constants import ReactionEmoji  # noqa: E402
Path.mkdir = _orig_mkdir

CHAT = -1001234567890
BOT = 999
A, B = 101, 102


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(bot, "context_bot_id", BOT)
    bot._reaction_last_check.clear()
    bot._reaction_new_lines.clear()
    bot._reaction_allowed_cache.clear()
    bot._reacted.clear()
    return tmp_path / "t.db"


# ------------------------------------------------------------------ БД

def test_migration_adds_column_old_rows_null(tmp_path, monkeypatch):
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE group_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER, "
              "sender_name TEXT, content TEXT NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0, timestamp INTEGER NOT NULL, hidden INTEGER NOT NULL DEFAULT 0)")
    c.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, timestamp) VALUES (1, 2, 'x', 'старая', 5)")
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", p)
    db.init_db()
    db.init_db()   # идемпотентно
    conn = db.get_conn()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(group_messages)")]
    assert "tg_message_id" in cols
    assert conn.execute("SELECT tg_message_id FROM group_messages").fetchone()[0] is None
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='message_reactions'").fetchone()
    conn.close()


def test_save_group_message_returns_row_and_tg_id(tmpdb):
    rid = db.save_group_message(CHAT, A, "Аня", "привет", tg_message_id=55)
    assert isinstance(rid, int)
    rows = db.get_group_transcript(CHAT, 5)
    assert rows[-1]["tg_id"] == 55
    rid2 = db.save_group_message(CHAT, BOT, "Клодушка", "ответ", is_bot=True)
    assert db.get_group_transcript(CHAT, 5)[-1]["tg_id"] is None
    db.set_group_tg_id(rid2, 56)
    assert db.get_group_transcript(CHAT, 5)[-1]["tg_id"] == 56
    db.set_group_tg_id(rid2, 57)            # уже заполнен — не перезаписывается
    assert db.get_group_transcript(CHAT, 5)[-1]["tg_id"] == 56


def test_set_user_reactions_replace_remove_isolation(tmpdb):
    db.set_user_reactions(CHAT, 10, A, "Аня", ["😁", "👍"], 100)
    db.set_user_reactions(CHAT, 10, B, "Боря", ["😁"], 101)
    got = db.get_reactions_for(CHAT, [10])[10]
    assert sorted((r["user_id"], r["emoji"]) for r in got) == [(A, "👍"), (A, "😁"), (B, "😁")]
    db.set_user_reactions(CHAT, 10, A, "Аня", ["🔥"], 102)      # заменяет набор человека
    got = db.get_reactions_for(CHAT, [10])[10]
    assert sorted((r["user_id"], r["emoji"]) for r in got) == [(A, "🔥"), (B, "😁")]
    db.set_user_reactions(CHAT, 10, A, "Аня", [], 103)          # снял всё; Боря не затронут
    assert [(r["user_id"], r["emoji"]) for r in db.get_reactions_for(CHAT, [10])[10]] == [(B, "😁")]
    assert db.get_reactions_for(CHAT, []) == {} and db.get_reactions_for(CHAT, [None]) == {}


def test_reactions_purged_with_group_messages(tmpdb, monkeypatch):
    monkeypatch.setattr(db, "GROUP_MESSAGES_KEEP", 3)
    for i in range(1, 4):
        db.save_group_message(CHAT, A, "Аня", f"м{i}", tg_message_id=i)
        db.set_user_reactions(CHAT, i, B, "Боря", ["👍"], 100 + i)
    db.set_user_reactions(CHAT + 1, 1, B, "Боря", ["👍"], 100)  # чужой чат не трогаем
    assert set(db.get_reactions_for(CHAT, [1, 2, 3])) == {1, 2, 3}
    time.sleep(1.1)                                    # timestamp в секундах: новая строка должна быть новее
    db.save_group_message(CHAT, A, "Аня", "м4", tg_message_id=4)   # срезает самую старую (tg 1)
    assert set(db.get_reactions_for(CHAT, [1, 2, 3, 4])) == {2, 3}
    assert set(db.get_reactions_for(CHAT + 1, [1])) == {1}


def test_count_user_reactions_since(tmpdb):
    db.set_user_reactions(CHAT, 1, BOT, "Клодушка", ["😁"], 1000)
    db.set_user_reactions(CHAT, 2, BOT, "Клодушка", ["👍"], 2000)
    db.set_user_reactions(CHAT, 3, A, "Аня", ["👍"], 2000)
    assert db.count_user_reactions_since(CHAT, BOT, 1500) == 1
    assert db.count_user_reactions_since(CHAT, BOT, 0) == 2


# ------------------------------------------------------------------ on_message_reaction

def _mr(chat_type="supergroup", user=None, new=(), anon=False, mid=7):
    user = None if anon else (user or SimpleNamespace(id=A, is_bot=False, first_name="Аня", username=None))
    mr = SimpleNamespace(chat=SimpleNamespace(id=CHAT, type=chat_type), user=user, message_id=mid, new_reaction=list(new), date=None)
    return SimpleNamespace(message_reaction=mr)


def _run_h(upd):
    asyncio.run(bot.on_message_reaction(upd, SimpleNamespace()))


def test_on_reaction_writes_emoji_only(tmpdb):
    _run_h(_mr(new=[ReactionTypeEmoji("😁"), ReactionTypeCustomEmoji("123"), ReactionTypePaid()]))
    assert [(r["user_id"], r["emoji"], r["name"]) for r in db.get_reactions_for(CHAT, [7])[7]] == [(A, "😁", "Аня")]
    _run_h(_mr(new=[]))                                              # снял
    assert db.get_reactions_for(CHAT, [7]) == {}


@pytest.mark.parametrize("kw", [
    dict(anon=True, new=[ReactionTypeEmoji("😁")]),
    dict(chat_type="private", new=[ReactionTypeEmoji("😁")]),
    dict(user=SimpleNamespace(id=BOT, is_bot=True, first_name="Клодушка", username=None), new=[ReactionTypeEmoji("😁")]),
    dict(user=SimpleNamespace(id=5, is_bot=True, first_name="Бот", username=None), new=[ReactionTypeEmoji("😁")]),
])
def test_on_reaction_skipped(tmpdb, kw):
    _run_h(_mr(**kw))
    assert db.get_reactions_for(CHAT, [7]) == {}


def test_on_reaction_banned_chat_skipped(tmpdb, monkeypatch):
    monkeypatch.setattr(db, "is_chat_banned", lambda cid: True)
    _run_h(_mr(new=[ReactionTypeEmoji("😁")]))
    assert db.get_reactions_for(CHAT, [7]) == {}


def test_reaction_update_gets_chat_queue_key():
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT), effective_user=SimpleNamespace(id=A))
    assert bot.PerChatUpdateProcessor.queue_key(upd) == CHAT


def test_handler_registered_for_message_reaction():
    src = Path(bot.__file__).read_text(encoding="utf-8")
    assert "MessageReactionHandler(on_message_reaction)" in src


# ------------------------------------------------------------------ reactions_note

def _entries():
    return [
        {"id": 1, "sender": "Аня", "text": "Я за кота", "is_bot": False, "ts": 1_800_000_000, "user_id": A, "tg_id": 11},
        {"id": 2, "sender": "Клодушка", "text": "Кот — это хорошо", "is_bot": True, "ts": 1_800_000_060, "user_id": BOT, "tg_id": 12},
        {"id": 3, "sender": "Боря", "text": "А я за собаку", "is_bot": False, "ts": 1_800_000_120, "user_id": B, "tg_id": 13},
        {"id": 4, "sender": "Боря", "text": "без реакций", "is_bot": False, "ts": 1_800_000_180, "user_id": B, "tg_id": 14},
    ]


def test_reactions_note_format_you_and_order():
    ent = _entries()
    labels = bot.group_sender_labels(ent)
    rx = {12: [{"user_id": A, "name": "Аня", "emoji": "😁"}, {"user_id": B, "name": "Боря", "emoji": "😁"}],
          13: [{"user_id": BOT, "name": "Клодушка", "emoji": "👍"}]}
    note = bot.reactions_note(ent, labels, rx, BOT)
    assert note.startswith("Реакции людей")
    assert "😁×2 (Аня, Боря)" in note and "👍 (ты)" in note and "твою реплику" in note
    assert note.index("«А я за собаку»") < note.index("«Кот — это хорошо»")        # новее — первыми
    assert "без реакций" not in note and "Я за кота" not in note


def test_reactions_note_only_window_rows_and_empty():
    ent = _entries()
    labels = bot.group_sender_labels(ent)
    assert bot.reactions_note(ent, labels, {}, BOT) == ""
    assert bot.reactions_note(ent, labels, {999: [{"user_id": A, "name": "Аня", "emoji": "😁"}]}, BOT) == ""   # сообщения нет в окне
    assert bot.reactions_note([{**e, "tg_id": None} for e in ent], labels, {11: [{"user_id": A, "name": "Аня", "emoji": "😁"}]}, BOT) == ""


def test_reactions_note_namesake_labels_and_unknown_reactor():
    ent = [
        {"id": 1, "sender": "Тест", "text": "раз", "is_bot": False, "ts": 1_800_000_000, "user_id": A, "tg_id": 1},
        {"id": 2, "sender": "Тест", "text": "два", "is_bot": False, "ts": 1_800_000_060, "user_id": B, "tg_id": 2},
    ]
    labels = bot.group_sender_labels(ent)
    assert all("#" in v for v in labels.values())
    rx = {1: [{"user_id": B, "name": "Тест", "emoji": "👍"}, {"user_id": 555, "name": "Чужой", "emoji": "👍"}]}
    note = bot.reactions_note(ent, labels, rx, BOT)
    assert labels[2] in note and "Чужой" in note               # подпись как в окне; не из окна — по sender_name


def test_reactions_note_limit_whole_items():
    ent = [{"id": i, "sender": "Аня", "text": "сообщение номер %d " % i + "x" * 60, "is_bot": False, "ts": 1_800_000_000 + i,
            "user_id": A, "tg_id": i} for i in range(1, 30)]
    rx = {i: [{"user_id": B, "name": "Боря", "emoji": "👍"}] for i in range(1, 30)}
    note = bot.reactions_note(ent, bot.group_sender_labels(ent), rx, BOT)
    assert 0 < len(note) <= bot.REACTIONS_NOTE_MAX_CHARS
    assert note.endswith("(Боря).") and "номер 29" in note and "номер 1 " not in note


def test_reactions_note_only_in_service_block_not_system(tmpdb, monkeypatch):
    db.save_group_message(CHAT, A, "Аня", "смешное", tg_message_id=11)
    db.set_user_reactions(CHAT, 11, B, "Боря", ["😁"], 1)
    info: dict = {}
    msgs = bot.build_group_messages(CHAT, "", 40, info=info)
    assert "😁" in info["reactions"]
    assert all("Реакции людей" not in str(m["content"]) for m in msgs)           # не в строках истории
    blk = bot.service_block(reactions_note=info["reactions"])
    assert "Реакции людей" in blk
    system = bot.build_system(A, True, CHAT)
    assert "Реакции людей на сообщения (новые" not in str(system)                 # в system — только статичная фраза аддендума
    assert bot.service_block() .count("Реакции людей") == 0


# ------------------------------------------------------------------ reaction_should_check

def _ok_state():
    bot._reaction_new_lines[CHAT] = bot.REACTION_MIN_NEW


def test_should_check_positive_and_each_ban(tmpdb, monkeypatch):
    _ok_state()
    now = 1_800_000_000
    assert bot.reaction_should_check(CHAT, A, "смешная шутка", now=now) is True
    # личка
    assert bot.reaction_should_check(5, A, "смешная шутка", now=now) is False
    # пустой текст / плейсхолдер медиа
    assert bot.reaction_should_check(CHAT, A, "", now=now) is False
    assert bot.reaction_should_check(CHAT, A, "[Фото]", now=now) is False
    # мало новых строк
    bot._reaction_new_lines[CHAT] = bot.REACTION_MIN_NEW - 1
    assert bot.reaction_should_check(CHAT, A, "текст", now=now) is False
    _ok_state()
    # кулдаун
    bot._reaction_last_check[CHAT] = now - 60
    assert bot.reaction_should_check(CHAT, A, "текст", now=now) is False
    bot._reaction_last_check[CHAT] = now - bot.REACTION_RECHECK_MIN * 60 - 1
    assert bot.reaction_should_check(CHAT, A, "текст", now=now) is True
    # автоигнор
    monkeypatch.setattr(bot, "user_is_ignored", lambda c, u: True)
    assert bot.reaction_should_check(CHAT, A, "текст", now=now) is False
    monkeypatch.setattr(bot, "user_is_ignored", lambda c, u: False)
    # дневной лимит
    for i in range(bot.REACTION_DAILY_CAP):
        db.set_user_reactions(CHAT, 1000 + i, BOT, "Клодушка", ["👍"], int(now) - 100)
    assert bot.reaction_should_check(CHAT, A, "текст", now=now) is False
    assert bot.reaction_should_check(CHAT, A, "текст", now=now + 2 * 86400) is True    # сутки прошли


def test_should_check_free_tier_allowed(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "free")
    _ok_state()
    assert bot.reaction_should_check(CHAT, A, "текст", now=1_800_000_000) is True


def test_should_check_has_no_side_effects(tmpdb):
    _ok_state()
    bot.reaction_should_check(CHAT, A, "текст", now=1_800_000_000)
    assert CHAT not in bot._reaction_last_check and bot._reaction_new_lines[CHAT] == bot.REACTION_MIN_NEW


# ------------------------------------------------------------------ разбор ответа

def _kept():
    return [
        {"id": 1, "sender": "Аня", "text": "шутка", "is_bot": False, "tg_id": 11},
        {"id": 2, "sender": "Клодушка", "text": "ответ", "is_bot": True, "tg_id": 12},
        {"id": 3, "sender": "Боря", "text": "старое", "is_bot": False, "tg_id": None},
        {"id": 4, "sender": "Боря", "text": "ещё шутка", "is_bot": False, "tg_id": 14},
    ]


def _parse(text, allowed=("😁", "👍"), bot_reacted=(), working=()):
    return bot.parse_reaction_choice(text, _kept(), allowed, set(bot_reacted), set(working), CHAT)


def test_parse_ok():
    row, emoji = _parse('{"react": {"n": 1, "emoji": "😁"}}')
    assert row["tg_id"] == 11 and emoji == "😁"
    assert _parse('```json\n{"react": {"n": 4, "emoji": "👍"}}\n```')[0]["tg_id"] == 14


@pytest.mark.parametrize("text", [
    '{"react": null}', "", "мусор", "[]", '{"react": "😁"}', '{"react": {"n": 0, "emoji": "😁"}}', '{"react": {"n": 5, "emoji": "😁"}}',
    '{"react": {"n": "1", "emoji": "😁"}}', '{"react": {"n": true, "emoji": "😁"}}',
    '{"react": {"n": 2, "emoji": "😁"}}',        # строка бота
    '{"react": {"n": 3, "emoji": "😁"}}',        # нет tg_id
    '{"react": {"n": 1, "emoji": "🖕"}}',        # не из списка
    '{"react": {"n": 1}}',
])
def test_parse_rejects(text):
    assert _parse(text) is None


def test_parse_emoji_not_allowed_in_chat_and_already_reacted_and_working():
    assert _parse('{"react": {"n": 1, "emoji": "😁"}}', allowed=("👍",)) is None
    assert _parse('{"react": {"n": 1, "emoji": "😁"}}', bot_reacted={11}) is None
    assert _parse('{"react": {"n": 1, "emoji": "😁"}}', working={(CHAT, 11)}) is None
    assert _parse('{"react": {"n": 1, "emoji": "😁"}}', working={(CHAT, 99)}) is not None


def test_choices_are_standard_and_no_working_emoji():
    std = {m.value for m in ReactionEmoji}
    assert all(e in std for e in bot.REACTION_CHOICES)
    assert bot.REACTION_SEARCH not in bot.REACTION_CHOICES and bot.REACTION_DRAW not in bot.REACTION_CHOICES
    assert len(set(bot.REACTION_CHOICES)) == len(bot.REACTION_CHOICES)


# ------------------------------------------------------------------ _reaction_task целиком

class _FakeBot:
    def __init__(self, available=None, fail=False):
        self.available, self.fail, self.calls = available, fail, []

    async def do_api_request(self, method, api_kwargs=None):
        """Сырой getChat: available_reactions опущено (None) — разрешены все; список (в т.ч. пустой) — только они."""
        assert method == "getChat"
        if self.fail:
            raise RuntimeError("нет сети")
        raw = {"id": (api_kwargs or {}).get("chat_id")}
        if self.available is not None:
            raw["available_reactions"] = [{"type": "emoji", "emoji": r.emoji} for r in self.available]
        return raw

    async def set_message_reaction(self, chat_id, message_id, reaction=None):
        self.calls.append((chat_id, message_id, reaction))


def _seed():
    db.save_group_message(CHAT, A, "Аня", "обычное", tg_message_id=21)
    db.save_group_message(CHAT, B, "Боря", "ха-ха, смешно про кота", tg_message_id=22)


def _run_task(monkeypatch, fake_bot, answer, queued):
    calls = []

    async def fake_aux(**kw):
        calls.append(kw)
        return answer
    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "response_text", lambda r: r)
    monkeypatch.setattr(bot, "_queue_reaction", lambda msg, emoji, on_ok=None: queued.append((msg, emoji, on_ok)))
    asyncio.run(bot._reaction_task(fake_bot, CHAT, B))
    return calls


def test_task_queues_reaction_and_records_on_ok(tmpdb, monkeypatch):
    _seed()
    queued = []
    calls = _run_task(monkeypatch, _FakeBot(), '{"react": {"n": 2, "emoji": "😁"}}', queued)
    assert calls[0]["label"] == "reaction_check" and "[2] Боря" in calls[0]["messages"][0]["content"]
    assert len(queued) == 1
    target, emoji, on_ok = queued[0]
    assert (target.chat_id, target.message_id, emoji) == (CHAT, 22, "😁")
    assert db.get_reactions_for(CHAT, [22]) == {}            # до успеха Telegram — записи нет
    on_ok()
    assert [(r["user_id"], r["emoji"]) for r in db.get_reactions_for(CHAT, [22])[22]] == [(BOT, "😁")]


def test_task_null_and_garbage_do_nothing(tmpdb, monkeypatch):
    _seed()
    for ans in ('{"react": null}', "мусор", '{"react": {"n": 9, "emoji": "😁"}}'):
        queued = []
        _run_task(monkeypatch, _FakeBot(), ans, queued)
        assert queued == []


def test_task_skips_message_with_own_reaction_and_working(tmpdb, monkeypatch):
    _seed()
    db.set_user_reactions(CHAT, 22, BOT, "Клодушка", ["👍"], 1)
    queued = []
    _run_task(monkeypatch, _FakeBot(), '{"react": {"n": 2, "emoji": "😁"}}', queued)
    assert queued == []
    db.set_user_reactions(CHAT, 22, BOT, "Клодушка", [], 2)
    bot._reacted.add((CHAT, 22))                              # там сейчас 👀/✍
    _run_task(monkeypatch, _FakeBot(), '{"react": {"n": 2, "emoji": "😁"}}', queued)
    assert queued == []


def test_task_respects_available_reactions(tmpdb, monkeypatch):
    _seed()
    queued = []
    only_thumb = (ReactionTypeEmoji("👍"),)
    calls = _run_task(monkeypatch, _FakeBot(available=only_thumb), '{"react": {"n": 2, "emoji": "😁"}}', queued)
    assert queued == [] and "Разрешённые эмодзи: 👍" in calls[0]["messages"][0]["content"]
    queued = []
    bot._reaction_allowed_cache.clear()
    _run_task(monkeypatch, _FakeBot(available=()), '{"react": {"n": 2, "emoji": "😁"}}', queued)   # реакции в чате выключены
    assert queued == []


def test_task_getchat_error_means_all_allowed(tmpdb, monkeypatch):
    _seed()
    queued = []
    _run_task(monkeypatch, _FakeBot(fail=True), '{"react": {"n": 2, "emoji": "😁"}}', queued)
    assert len(queued) == 1


def test_allowed_cache_one_hour(tmpdb):
    fb = _FakeBot(available=(ReactionTypeEmoji("👍"),))

    async def go():
        a = await bot._chat_allowed_reactions(fb, CHAT, now=1000)
        fb.available = None
        b = await bot._chat_allowed_reactions(fb, CHAT, now=1000 + 3599)
        c = await bot._chat_allowed_reactions(fb, CHAT, now=1000 + 3601)
        return a, b, c
    a, b, c = asyncio.run(go())
    assert a == frozenset({"👍"}) and b == frozenset({"👍"}) and c is None


def test_reaction_check_is_quiet_label():
    assert "reaction_check" in bot.FALLBACK_QUIET_LABELS


# ------------------------------------------------------------------ конфликт со спонтанной и рабочей реакцией

class _Msg:
    def __init__(self):
        self.chat_id, self.message_id, self.calls = CHAT, 31, []

    async def set_reaction(self, emoji):
        self.calls.append(emoji)


def test_unreact_restores_spontaneous(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "REACTIONS", True)
    db.set_user_reactions(CHAT, 31, BOT, "Клодушка", ["😁"], 1)       # спонтанная уже стоит
    msg = _Msg()

    async def go():
        assert bot.react(msg, bot.REACTION_SEARCH)                      # рабочая вытесняет её
        await asyncio.sleep(0.05)
        bot.unreact(msg)                                                # после работы — вернуть спонтанную
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert msg.calls == [bot.REACTION_SEARCH, "😁"]


def test_unreact_without_spontaneous_clears(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "REACTIONS", True)
    msg = _Msg()

    async def go():
        bot.react(msg, bot.REACTION_SEARCH)
        await asyncio.sleep(0.05)
        bot.unreact(msg)
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert msg.calls == [bot.REACTION_SEARCH, None]


def test_queue_reaction_on_ok_only_after_success(monkeypatch):
    ok, bad = [], []

    class M:
        chat_id, message_id = CHAT, 40

        def __init__(self, fail):
            self.fail = fail

        async def set_reaction(self, e):
            if self.fail:
                raise RuntimeError("REACTION_INVALID")

    async def go():
        bot._queue_reaction(M(False), "😁", on_ok=lambda: ok.append(1))
        await asyncio.sleep(0.05)
        bot._queue_reaction(M(True), "😁", on_ok=lambda: bad.append(1))
        await asyncio.sleep(0.05)
    asyncio.run(go())
    assert ok == [1] and bad == []


# ------------------------------------------------------------------ tg_message_id в ветках ответа бота

def _sent(mid):
    async def reply_text(text, **kw):
        return SimpleNamespace(message_id=mid)
    return reply_text


def test_media_answer_saves_first_part_id(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", BOT)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=_sent(501)), effective_user=SimpleNamespace(first_name="Аня", username=None))
    asyncio.run(bot._finish_media_answer(update, SimpleNamespace(), chat_id=CHAT, user_id=A, is_group=True, answer="Красивое фото",
                                         history_user_text="[фото]", request_text=""))
    last = db.get_group_transcript(CHAT, 3)[-1]
    assert last["is_bot"] and last["text"] == "Красивое фото" and last["tg_id"] == 501


def test_activity_reply_saves_id(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", BOT)
    db.save_group_message(CHAT, A, "Аня", "кто за кота?", tg_message_id=60)

    async def fake_aux(**kw):
        return '{"say": true}'

    async def fake_call(*a, **kw):
        return "Я за кота."
    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "call_claude", fake_call)
    monkeypatch.setattr(bot, "response_text", lambda r: r)
    monkeypatch.setattr(bot, "build_system", lambda *a, **k: "sys")
    msg = SimpleNamespace(reply_text=_sent(61), text="кто за кота?", caption=None, reply_to_message=None,
                          from_user=SimpleNamespace(first_name="Аня"))
    asyncio.run(bot._activity_task(msg, SimpleNamespace(), CHAT, A))
    last = db.get_group_transcript(CHAT, 3)[-1]
    assert last["is_bot"] and last["text"] == "Я за кота." and last["tg_id"] == 61


def test_send_image_returns_sent_message():
    class Msg:
        async def reply_photo(self, photo, caption):
            return SimpleNamespace(message_id=71)

        async def reply_document(self, document, caption):
            return SimpleNamespace(message_id=72)
    assert asyncio.run(bot._send_image(Msg(), b"x", {"size": "1K"}, "cap")).message_id == 71
    assert asyncio.run(bot._send_image(Msg(), b"x", {"size": "2K"}, "cap")).message_id == 72


def test_sent_id_helper():
    assert bot._sent_id(SimpleNamespace(message_id=5)) == 5
    assert bot._sent_id(None) is None and bot._sent_id(SimpleNamespace()) is None


def test_every_bot_save_passes_tg_id():
    """Все сохранения ответов бота в group_messages передают id сообщения (v2.25.0): grep по исходнику."""
    import re
    src = Path(bot.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"db\.save_group_message\((?:[^()]|\([^()]*\))*\)", src)
    bots = [c for c in calls if "is_bot=True" in c]
    assert len(bots) == 9
    # без tg_message_id — только ответы, id которых дописывается set_group_tg_id (основной диалог и ответ на фото/файл: сначала запись, потом отправка)
    without = [c for c in bots if "tg_message_id" not in c]
    assert len(without) == 3 and src.count("db.set_group_tg_id(") == 2

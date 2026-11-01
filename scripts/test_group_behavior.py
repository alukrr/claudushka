"""Юнит-тесты автоигнора и активности (feat/group-behavior, docs/claude/tz-group-behavior.md).

Без сети: модель подменена. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_group_behavior.py -q
"""
import asyncio
import os
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
Path.mkdir = _orig_mkdir

CHAT = -1001234567890
UID = 777


def _dr(chat, uid, text, now):
    """Обращение в момент now, бот ответил прямо перед ним (живой обмен)."""
    return bot.detect_repeat(chat, uid, text, now, last_bot_ts=int(now))


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def default_steps(monkeypatch):
    # на стенде IGNORE_STEPS_MIN=1,1,1,1 в окружении — тесты проверяют лестницу по умолчанию
    monkeypatch.setattr(bot, "IGNORE_STEPS_MIN", [5, 15, 30, 60])


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    c = db.get_conn()
    c.execute("INSERT INTO allowed_chats (chat_id, name, status) VALUES (?, 'T', 'approved')", (CHAT,))
    c.commit()
    c.close()
    bot._recent_addresses.clear()
    bot._activity_last_check.clear()
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    return tmp_path / "t.db"


# ---------- повтор ----------
def test_norm_ignores_punct_emoji_case():
    assert bot._norm_address("Привет!!! 😀") == bot._norm_address("привет") == "привет"


def test_norm_placeholder_empty():
    for t in ("[Фото]", "[Кружок]", "[Видео: кот]", "[GIF]", "[Голосовое]", "[Файл: a.txt]", "", "!!!"):
        assert bot._norm_address(t) == ""


def test_repeat_third_triggers(tmpdb):
    t = 1000.0
    assert not _dr(CHAT, UID, "дай денег", t)
    assert not _dr(CHAT, UID, "Дай денег!", t + 30)
    assert _dr(CHAT, UID, "дай денег", t + 60)


def test_repeat_two_is_not_enough(tmpdb):
    assert not _dr(CHAT, UID, "раз", 1000.0)
    assert not _dr(CHAT, UID, "раз", 1010.0)


def test_repeat_window_10_min(tmpdb):
    _dr(CHAT, UID, "одно и то же", 1000.0)
    _dr(CHAT, UID, "одно и то же", 1010.0)
    assert not _dr(CHAT, UID, "одно и то же", 1010.0 + 11 * 60)


def test_repeat_different_texts_no(tmpdb):
    for i, t in enumerate(("привет как дела", "что делаешь вечером", "расскажи анекдот")):
        assert not _dr(CHAT, UID, t, 1000.0 + i)


def test_repeat_placeholders_not_counted(tmpdb):
    for i in range(5):
        assert not _dr(CHAT, UID, "[Фото]", 1000.0 + i)


def test_repeat_per_user(tmpdb):
    _dr(CHAT, 1, "спам", 1000.0)
    _dr(CHAT, 2, "спам", 1001.0)
    assert not _dr(CHAT, 3, "спам", 1002.0)


# ---------- лестница ----------
def test_ladder_steps():
    assert bot.IGNORE_STEPS_MIN == [5, 15, 30, 60]
    now = 10_000
    prev = None
    got = []
    for _ in range(6):
        level, until = bot.compute_strike(prev, now)
        got.append((level, None if until is None else (until - now) // 60))
        prev = {"level": level, "ignored_until": until, "last_strike_ts": now}
        now += 10
    assert got == [(1, None), (2, 5), (3, 15), (4, 30), (5, 60), (6, 60)]


def test_ladder_reset_after_24h():
    now = 1_000_000
    prev = {"level": 4, "ignored_until": now, "last_strike_ts": now - 25 * 3600}
    assert bot.compute_strike(prev, now) == (1, None)
    prev["last_strike_ts"] = now - 23 * 3600
    assert bot.compute_strike(prev, now)[0] == 5


def test_strike_texts_fixed():
    assert bot.strike_text("Вася", 1) == "Вася, предупреждение за повторы одной и той же фразы (флуд). Продолжишь — помолчу с тобой 5 мин."
    assert bot.strike_text("Вася", 1, "abuse") == "Вася, предупреждение за оскорбления в адрес бота. Продолжишь — помолчу с тобой 5 мин."
    assert bot.strike_text("Вася", 2) == "Вася, беру паузу с тобой на 5 мин за повторы одной и той же фразы (флуд)."
    assert bot.strike_text("Вася", 3, "abuse") == "Вася, беру паузу с тобой на 15 мин за оскорбления в адрес бота."


def test_env_steps():
    assert bot._parse_steps("1,1,1,1") == [1, 1, 1, 1]
    assert bot._parse_steps("bad") == [5, 15, 30, 60]
    assert bot._parse_steps(None) == [5, 15, 30, 60]


class FakeMsg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **k):
        self.sent.append(text)


def test_apply_strike_flow_and_unignore(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", 1)
    m = FakeMsg()
    run(bot._apply_strike(m, CHAT, UID, "Вася", "repeat"))
    assert "предупреждение за повторы" in m.sent[-1] and not bot.user_is_ignored(CHAT, UID)
    run(bot._apply_strike(m, CHAT, UID, "Вася", "repeat"))
    assert "беру паузу" in m.sent[-1] and bot.user_is_ignored(CHAT, UID)
    # оба сообщения бота — в group_messages с is_bot (инвариант)
    assert db.get_last_bot_ts(CHAT) > 0
    assert db.delete_ignore(CHAT, UID) and not bot.user_is_ignored(CHAT, UID)
    assert db.get_ignore(CHAT, UID) is None


# ---------- Lite: разбор ----------
def test_json_flag_parsing():
    f = bot._json_flag
    assert f('{"abuse": true, "reason": "x"}', "abuse", "t") is True
    assert f('Вот ответ: {"abuse": false}', "abuse", "t") is False
    assert f('{"abuse": "true"}', "abuse", "t") is False   # не bool
    for junk in ("", "мусор", "{", "[]", "null"):
        assert f(junk, "abuse", "t") is False
    assert f('{"say": true}', "say", "t") is True


def _fake_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _abuse_run(monkeypatch, behaviour, name="Вася"):
    calls = []

    async def fake_aux(**kw):
        calls.append(kw)
        if isinstance(behaviour, Exception):
            raise behaviour
        return _fake_response(behaviour)

    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "context_bot_id", 1)
    m = FakeMsg()
    run(bot._abuse_check_task(m, CHAT, UID, name))
    return calls, m


def _seed_exchange():
    db.save_group_message(CHAT, UID, "Вася", "ты тупой бот")
    db.save_group_message(CHAT, 1, "Клодушка", "сам такой", is_bot=True)
    db.save_group_message(CHAT, UID, "Вася", "ещё раз: тупой")


def test_abuse_true_applies_strike(tmpdb, monkeypatch):
    _seed_exchange()
    calls, m = _abuse_run(monkeypatch, '{"abuse": true, "reason": "травля"}')
    assert calls[0]["label"] == "abuse_check" and calls[0]["model"] == bot.DEFAULT_MODEL_ID
    assert db.get_ignore(CHAT, UID)["level"] == 1 and "за оскорбления в адрес бота" in m.sent[-1]


def test_abuse_false_nothing(tmpdb, monkeypatch):
    _seed_exchange()
    _, m = _abuse_run(monkeypatch, '{"abuse": false}')
    assert db.get_ignore(CHAT, UID) is None and not m.sent


def test_abuse_junk_empty_exception_safety_are_false(tmpdb, monkeypatch):
    _seed_exchange()
    for beh in ("мусор", "", RuntimeError("boom"), bot._synthetic_safety_error()):
        _, m = _abuse_run(monkeypatch, beh)
        assert db.get_ignore(CHAT, UID) is None and not m.sent


def test_abuse_input_capped(tmpdb):
    rows = [{"is_bot": i % 2 == 1, "text": "я" * 1000} for i in range(20)]
    out = bot.build_abuse_input(rows)
    assert len(out) <= bot.ABUSE_TOTAL_CHARS
    assert all(len(line) <= bot.ABUSE_ITEM_CHARS + 12 for line in out.splitlines())


def test_user_bot_exchange_only_user_and_bot(tmpdb):
    db.save_group_message(CHAT, 5, "Петя", "чужое")
    _seed_exchange()
    rows = db.get_user_bot_exchange(CHAT, UID)
    assert [r["text"] for r in rows] == ["ты тупой бот", "сам такой", "ещё раз: тупой"]


# ---------- активность ----------
def test_activity_conditions(tmpdb, monkeypatch):
    now = 100_000.0
    assert not bot.activity_should_check(CHAT, UID, "привет", now, 0.0)   # 0% — выключено
    db.set_activity_percent(CHAT, 100)
    assert bot.activity_should_check(CHAT, UID, "привет", now, 0.5)
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "free")
    assert not bot.activity_should_check(CHAT, UID, "привет", now, 0.0)   # free
    monkeypatch.setattr(bot, "chat_tier", lambda cid: "paid")
    assert not bot.activity_should_check(CHAT, UID, "[Фото]", now, 0.0)   # плейсхолдер
    assert not bot.activity_should_check(CHAT, UID, "", now, 0.0)
    db.set_ignore(CHAT, UID, 2, int(time.time()) + 600, int(time.time()))
    assert not bot.activity_should_check(CHAT, UID, "привет", time.time(), 0.0)   # автор в игноре
    db.delete_ignore(CHAT, UID)


def test_activity_random_threshold(tmpdb):
    db.set_activity_percent(CHAT, 30)
    assert bot.activity_should_check(CHAT, UID, "привет", time.time(), 0.29)
    assert not bot.activity_should_check(CHAT, UID, "привет", time.time(), 0.31)


def test_activity_cooldown_after_bot_reply(tmpdb):
    db.set_activity_percent(CHAT, 100)
    db.save_group_message(CHAT, 1, "Клодушка", "реплика", is_bot=True)
    now = time.time()
    assert not bot.activity_should_check(CHAT, UID, "привет", now + 9 * 60, 0.0)
    assert bot.activity_should_check(CHAT, UID, "привет", now + 11 * 60, 0.0)


def test_activity_recheck_cooldown(tmpdb):
    db.set_activity_percent(CHAT, 100)
    now = time.time()
    bot._activity_last_check[CHAT] = now
    assert not bot.activity_should_check(CHAT, UID, "привет", now + 60, 0.0)
    assert bot.activity_should_check(CHAT, UID, "привет", now + 4 * 60, 0.0)


def test_activity_task_say_true_sends_and_saves(tmpdb, monkeypatch):
    db.save_group_message(CHAT, UID, "Вася", "кто-нибудь знает, как варить борщ?")
    monkeypatch.setattr(bot, "context_bot_id", 1)
    monkeypatch.setattr(bot, "get_chat_model", lambda cid: bot.DEFAULT_MODEL_ID)
    seen = {}

    async def fake_aux(**kw):
        seen["aux"] = kw
        return _fake_response('{"say": true}')

    async def fake_call(ctx, chat_id, **kw):
        seen["call"] = kw
        return _fake_response("Со свёклой, конечно. [[DRAW: borscht]]")

    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "call_claude", fake_call)
    m = FakeMsg()
    run(bot._activity_task(m, None, CHAT, UID))
    assert seen["aux"]["label"] == "activity_check"
    assert seen["call"]["label"].startswith("activity") and seen["call"]["usage_label"] == "activity"
    assert m.sent == ["Со свёклой, конечно."]       # маркер вырезан
    assert db.get_last_bot_ts(CHAT) > 0
    assert "Тебя сейчас не звали" in str(seen["call"]["messages"][-1]["content"])


def test_activity_task_say_false_silent(tmpdb, monkeypatch):
    db.save_group_message(CHAT, UID, "Вася", "привет всем")
    called = []

    async def fake_aux(**kw):
        return _fake_response('{"say": false}')

    async def fake_call(*a, **k):
        called.append(1)

    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "call_claude", fake_call)
    m = FakeMsg()
    run(bot._activity_task(m, None, CHAT, UID))
    assert not m.sent and not called


def test_activity_task_errors_silent(tmpdb, monkeypatch):
    db.save_group_message(CHAT, UID, "Вася", "привет")

    async def boom(**kw):
        raise bot._synthetic_safety_error()

    monkeypatch.setattr(bot, "aux_create", boom)
    m = FakeMsg()
    run(bot._activity_task(m, None, CHAT, UID))   # не падает
    assert not m.sent


def test_activity_task_empty_reply_not_sent(tmpdb, monkeypatch):
    db.save_group_message(CHAT, UID, "Вася", "привет")
    monkeypatch.setattr(bot, "get_chat_model", lambda cid: bot.DEFAULT_MODEL_ID)

    async def fake_aux(**kw):
        return _fake_response('{"say": true}')

    async def fake_call(*a, **k):
        return _fake_response("[[DRAW: cat]]")

    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "call_claude", fake_call)
    m = FakeMsg()
    run(bot._activity_task(m, None, CHAT, UID))
    assert not m.sent


# ---------- service_block / system / db ----------
def test_service_block_spontaneous():
    assert "Тебя сейчас не звали" in bot.service_block(spontaneous=True)
    assert "Тебя сейчас не звали" not in bot.service_block()


def test_system_prompt_pause_wording(tmpdb):
    sysp = bot.build_system(UID, True, CHAT)
    text = sysp if isinstance(sysp, str) else " ".join(b.get("text", "") for b in sysp)
    assert "автоматическая пауза бота" in text
    assert "игнорировать людей сама ты не можешь" in text


def test_db_settings_roundtrip(tmpdb):
    assert db.get_activity_percent(CHAT) == 0 and db.get_autoignore_enabled(CHAT) is True
    assert db.set_activity_percent(CHAT, 40) and db.get_activity_percent(CHAT) == 40
    assert db.set_autoignore_enabled(CHAT, False) and db.get_autoignore_enabled(CHAT) is False
    assert not db.set_activity_percent(-999, 10)
    assert db.get_autoignore_enabled(-999) is True


def test_old_constant_removed():
    assert not hasattr(bot, "CHAT_ACTIVITY_CHANCE")


# ---------- повтор: бот молчал / тормозил ----------
def test_repeat_bot_silent_is_not_repeat(tmpdb):
    t = 1_000_000.0
    for i in range(3):
        assert not bot.detect_repeat(CHAT, UID, "Клодушка, ты тут?", t + 60 * i, last_bot_ts=0)
    # реплика бота была ДО обращений — тоже не в счёт
    assert not bot.detect_repeat(CHAT, UID, "Клодушка, ты тут?", t + 150, last_bot_ts=int(t) - 5)


def test_repeat_slow_bot_queue_is_not_repeat(tmpdb):
    # бот тормозит: сообщения написаны в t, t+20, t+40; к обработке каждого последняя реплика бота — t+60
    t = 1_000_000.0
    lb = int(t) + 60
    for dt in (0, 20, 40):
        assert not bot.detect_repeat(CHAT, UID, "Клодушка, ты тут?", t + dt, last_bot_ts=lb)


def test_repeat_live_exchange_is_repeat(tmpdb):
    # бот отвечал между сообщениями: даты сообщений — после ответов
    t = 1_000_000.0
    assert not bot.detect_repeat(CHAT, UID, "дай денег", t, last_bot_ts=0)
    assert not bot.detect_repeat(CHAT, UID, "дай денег", t + 10, last_bot_ts=int(t) + 3)
    assert bot.detect_repeat(CHAT, UID, "дай денег", t + 20, last_bot_ts=int(t) + 13)


def test_repeat_bot_answered_only_first_is_not_repeat(tmpdb):
    t = 1_000_000.0
    bot.detect_repeat(CHAT, UID, "дай денег", t, last_bot_ts=0)
    bot.detect_repeat(CHAT, UID, "дай денег", t + 60, last_bot_ts=int(t) + 5)
    # после второго обращения бот молчал → второе не считается, повторов только один
    assert not bot.detect_repeat(CHAT, UID, "дай денег", t + 120, last_bot_ts=int(t) + 5)


# ---------- пометка паузы в истории ----------
def test_pause_history_note_format():
    w = bot.pause_history_note("Вася", 1)
    assert w == "(служебное: предупреждение бота для Вася о повторах)"
    p = bot.pause_history_note("Вася", 3)
    assert p == "(служебное: автопауза бота для Вася на 15 мин за повторы)"
    for n in (w, p):
        assert n.startswith("(") and n.endswith(")")
        assert "беру паузу" not in n and "помолчу" not in n


def test_apply_strike_history_is_note_not_text(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", 1)
    m = FakeMsg()
    run(bot._apply_strike(m, CHAT, UID, "Вася", "repeat"))
    run(bot._apply_strike(m, CHAT, UID, "Вася", "repeat"))
    assert "предупреждение за повторы" in m.sent[0] and "беру паузу" in m.sent[1]   # в Telegram — живой текст
    texts = [e["text"] for e in db.get_group_transcript(CHAT, 10) if e["is_bot"]]
    assert texts == ["(служебное: предупреждение бота для Вася о повторах)",
                     "(служебное: автопауза бота для Вася на 5 мин за повторы)"]
    assert not any("беру паузу" in t for t in texts)


def test_pause_note_reason():
    assert bot.pause_history_note("Вася", 1, "repeat").endswith("о повторах)")
    assert bot.pause_history_note("Вася", 1, "abuse") == "(служебное: предупреждение бота для Вася о грубости в адрес бота)"
    assert bot.pause_history_note("Вася", 2, "abuse") == "(служебное: автопауза бота для Вася на 5 мин за грубость в адрес бота)"


def test_abuse_strike_history_note_reason(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", 1)
    _seed_exchange()
    _abuse_run(monkeypatch, '{"abuse": true}')
    texts = [e["text"] for e in db.get_group_transcript(CHAT, 10) if e["is_bot"]]
    assert "(служебное: предупреждение бота для Вася о грубости в адрес бота)" in texts


# ---------- вход Lite: since_ts, служебные строки, промпт, reason ----------
def _save_at(user_id, name, text, ts, is_bot=False):
    c = db.get_conn()
    c.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, is_bot, timestamp) VALUES (?,?,?,?,?,?)",
              (CHAT, user_id, name, text, 1 if is_bot else 0, ts))
    c.commit()
    c.close()


def test_exchange_since_ts_strictly_after(tmpdb):
    _save_at(UID, "Вася", "до наказания", 100)
    _save_at(UID, "Вася", "в ту же секунду", 200)
    _save_at(1, "Клодушка", "(служебное: автопауза бота для Вася на 5 мин за повторы)", 200, is_bot=True)
    _save_at(UID, "Вася", "после", 201)
    rows = db.get_user_bot_exchange(CHAT, UID, since_ts=200)
    assert [r["text"] for r in rows] == ["после"]
    assert len(db.get_user_bot_exchange(CHAT, UID, since_ts=0)) == 4


def test_exchange_skips_service_bot_lines(tmpdb):
    _save_at(UID, "Вася", "привет", 10)
    _save_at(1, "Клодушка", "(служебное: автопауза бота для Вася на 5 мин за повторы)", 11, is_bot=True)
    _save_at(1, "Клодушка", f"({bot.DRAW_SENT_LABEL}: cat)", 12, is_bot=True)
    _save_at(1, "Клодушка", f"({bot.REVIEW_SENT_LABEL} 7 сообщениям)\nТемы", 13, is_bot=True)
    _save_at(1, "Клодушка", "обычный ответ", 14, is_bot=True)
    rows = db.get_user_bot_exchange(CHAT, UID, skip_bot_prefixes=bot._abuse_skip_prefixes())
    assert [r["text"] for r in rows] == ["привет", "обычный ответ"]


def test_abuse_skip_prefixes_from_constants():
    pf = bot._abuse_skip_prefixes()
    assert f"({bot.PAUSE_NOTE_LABEL}:" in pf and f"({bot.DRAW_SENT_LABEL}" in pf and f"({bot.REVIEW_SENT_LABEL}" in pf


def test_abuse_task_input_has_no_notes_and_no_old_lines(tmpdb, monkeypatch):
    now = int(time.time())
    _save_at(UID, "Вася", "старое до наказания", now - 100)
    db.set_ignore(CHAT, UID, 2, now + 60, now - 50)
    _save_at(1, "Клодушка", "(служебное: автопауза бота для Вася на 5 мин за повторы)", now - 50, is_bot=True)
    _save_at(UID, "Вася", "где ключи от танка?", now - 10)
    calls, _ = _abuse_run(monkeypatch, '{"abuse": false, "reason": "обычный вопрос"}')
    payload = calls[0]["messages"][0]["content"]
    assert "где ключи от танка?" in payload
    assert "старое до наказания" not in payload and "служебное" not in payload


def test_abuse_prompt_phrases():
    assert "Повторы одной и той же фразы без оскорблений — не травля, их ловит другой механизм" in bot._ABUSE_SYSTEM
    assert "Один обычный вопрос или просьба (даже странная или шуточная) — никогда не травля" in bot._ABUSE_SYSTEM


def test_json_verdict_reason():
    assert bot._json_verdict('{"abuse": true, "reason": "оскорбления"}', "abuse", "t") == (True, "оскорбления")
    assert bot._json_verdict('{"abuse": false}', "abuse", "t") == (False, "")
    assert bot._json_verdict("мусор", "abuse", "t") == (False, "")
    assert len(bot._json_verdict('{"abuse": true, "reason": "' + "я" * 500 + '"}', "abuse", "t")[1]) == 200


def test_abuse_reason_logged(tmpdb, monkeypatch, caplog):
    import logging
    _seed_exchange()
    with caplog.at_level(logging.INFO):
        _abuse_run(monkeypatch, '{"abuse": true, "reason": "прямые оскорбления бота"}')
    assert any("reason=abuse" in r.message and "прямые оскорбления бота" in r.message for r in caplog.records)
    caplog.clear()
    db.delete_ignore(CHAT, UID)
    with caplog.at_level(logging.INFO):
        _abuse_run(monkeypatch, '{"abuse": false, "reason": "обычный вопрос"}')
    assert any("abuse=false" in r.message and "обычный вопрос" in r.message for r in caplog.records)

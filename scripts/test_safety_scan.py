"""Юнит-тесты safety-восстановления чата (docs/claude/safety-unblock.md, ТЗ tz-safety-unblock.md).

Без сети: модель подменена. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_safety_scan.py -q
"""
import asyncio
import os
import sqlite3
import sys
import time
from pathlib import Path

import anthropic
import httpx
import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api_errors  # noqa: E402

# bot.py при импорте создаёт /app/data (путь контейнера) — вне контейнера подавляем только это.
_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
import db  # noqa: E402
Path.mkdir = _orig_mkdir

CHAT = -1001234567890
BAD = "ПЛОХОЕ-СЛОВО"


def run(coro):
    return asyncio.run(coro)


def safety_exc():
    return bot._synthetic_safety_error()


def other_400():
    body = {"error": {"message": "something else"}}
    resp = httpx.Response(400, request=httpx.Request("POST", "http://pool/v1/messages"), json=body)
    return anthropic.BadRequestError("something else", response=resp, body=body)


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    return tmp_path / "t.db"


def fill(n=10, bad_idx=()):
    """n реплик чата; на позициях bad_idx текст содержит BAD. -> список id по порядку."""
    ids = []
    for i in range(n):
        text = f"реплика номер {i} " + (BAD if i in bad_idx else "норм")
        db.save_group_message(CHAT, 100 + i % 3, f"user{i % 3}", text, is_bot=(i % 4 == 3))
        ids.append(_last_id())
    return ids


def _last_id():
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute("SELECT MAX(id) FROM group_messages").fetchone()[0]
    finally:
        c.close()


# --- safety_find (чистый алгоритм) ---

def make_probe(is_blocked, budget=None):
    calls = {"n": 0}

    async def probe(excluded):
        calls["n"] += 1
        if budget is not None and calls["n"] > budget:
            raise bot._ScanBudgetExceeded()
        return is_blocked(excluded)
    return probe, calls


def blocked_if_any_left(bad):
    """Блок, пока в запросе осталась хоть одна плохая строка."""
    return lambda ex: bool(set(bad) - set(ex))


@pytest.mark.parametrize("bad", [[0], [7], [31], [49], [0, 49], [5, 6], [3, 20, 44]])
def test_find_any_left(bad):
    cands = list(range(50))
    probe, calls = make_probe(blocked_if_any_left(bad))
    assert sorted(run(bot.safety_find(cands, probe))) == sorted(bad)
    assert calls["n"] <= 40


def test_find_combo_only_together():
    """Блок только когда в запросе остались ≥2 из набора (комбинация) — исключение одной не лечит."""
    bad = {4, 33}
    cands = list(range(50))
    probe, _ = make_probe(lambda ex: len(bad - set(ex)) >= 2)
    found = run(bot.safety_find(cands, probe))
    assert len(found) >= 1 and not probe_blocks(bad, found)


def probe_blocks(bad, excluded):
    return len(set(bad) - set(excluded)) >= 2


def test_find_five_copies_in_both_halves():
    """Случай 2026-10-31: 5 одинаковых вставок, блок пока осталась хоть одна."""
    bad = [10, 11, 18, 40, 41]
    probe, calls = make_probe(blocked_if_any_left(bad))
    assert sorted(run(bot.safety_find(list(range(50)), probe))) == bad
    assert calls["n"] <= 40


def test_find_budget_exceeded():
    probe, _ = make_probe(blocked_if_any_left([25]), budget=2)
    with pytest.raises(bot._ScanBudgetExceeded):
        run(bot.safety_find(list(range(50)), probe))


# --- db: hidden ---

def test_migration_adds_hidden_to_old_db(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE group_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
              "user_id INTEGER, sender_name TEXT, content TEXT NOT NULL, is_bot INTEGER NOT NULL DEFAULT 0, "
              "timestamp INTEGER NOT NULL)")
    c.execute("INSERT INTO group_messages (chat_id, user_id, sender_name, content, timestamp) VALUES (?,?,?,?,?)",
              (CHAT, 1, "a", "старая строка", int(time.time())))
    c.commit()
    c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.init_db()  # идемпотентно
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(group_messages)")]
    assert "hidden" in cols
    tr = db.get_group_transcript(CHAT, 10)
    assert [r["text"] for r in tr] == ["старая строка"]


def test_hide_filters_all_prompt_reads(tmpdb):
    ids = fill(6)
    since = int(time.time()) - 3600
    until = int(time.time()) + 3600
    assert db.hide_group_messages(CHAT, [ids[2], ids[3]]) == 2
    assert db.hide_group_messages(CHAT, [ids[2]]) == 0               # повторно — ничего
    assert db.hide_group_messages(-5, [ids[0]]) == 0                  # чужой чат
    assert [r["id"] for r in db.get_group_transcript(CHAT, 10)] == [ids[0], ids[1], ids[4], ids[5]]
    assert ids[2] not in [r["id"] for r in db.get_group_messages_range(CHAT, since, until)]
    assert len(db.get_group_history(CHAT, 10, since)) == 4
    assert db.unhide_group_message(CHAT, ids[2]) is True
    assert db.unhide_group_message(CHAT, ids[2]) is False
    assert ids[2] in [r["id"] for r in db.get_group_transcript(CHAT, 10)]


def test_transcript_exclude_and_min_id(tmpdb):
    ids = fill(8)
    tr = db.get_group_transcript(CHAT, 3, exclude_ids={ids[7], ids[6]})
    assert [r["id"] for r in tr] == ids[3:6]                          # окно сдвинулось на старые строки
    tr = db.get_group_transcript(CHAT, 50, exclude_ids={ids[7]}, min_id=ids[4])
    assert [r["id"] for r in tr] == ids[4:7]                          # без подтягивания старых


def test_list_hidden_has_no_text_and_same_content(tmpdb):
    ids = fill(4)
    db.save_group_message(CHAT, 1, "u", "дубль", False)
    d1 = _last_id()
    db.save_group_message(CHAT, 2, "v", "дубль", False)
    d2 = _last_id()
    assert db.group_message_ids_with_same_content(CHAT, [d1]) == [d1, d2]
    db.hide_group_messages(CHAT, [d1])
    assert db.group_message_ids_with_same_content(CHAT, [d2]) == [d2]
    rows = db.list_hidden_group_messages(CHAT)
    assert rows and "text" not in rows[0] and "content" not in rows[0] and rows[0]["length"] == len("дубль")


# --- build_group_messages с пробами ---

def test_build_group_messages_probe_params(tmpdb):
    ids = fill(12)
    full = bot.build_group_messages(CHAT, "", 50)
    assert BAD not in str(full)
    ids2 = fill(3, bad_idx=(1,))
    msgs = bot.build_group_messages(CHAT, "", 50, exclude_ids=set(ids2[1:2]))
    assert BAD not in str(msgs)
    assert BAD in str(bot.build_group_messages(CHAT, "", 50, exclude_ids=set()))
    only_new = bot.build_group_messages(CHAT, "", 50, min_id=ids2[0])
    assert "реплика номер 0" in str(only_new) and "номер 5 " not in str(only_new)


def test_group_tail_is_only_the_trigger_line(tmpdb):
    fill(6, bad_idx=(1, 4))                                           # плохие строки в хвосте людей подряд
    db.save_group_message(CHAT, 7, "Аня", "последний вопрос", False)
    tail = bot.build_group_tail(CHAT, "процитировано")
    assert len(tail) == 1 and tail[0]["role"] == "user"
    assert "Аня: последний вопрос" in tail[0]["content"] and "процитировано" in tail[0]["content"]
    assert BAD not in tail[0]["content"] and "реплика номер" not in tail[0]["content"]
    assert BAD in str(bot.build_group_messages(CHAT, "", 50)[-1])    # а «последний turn» склеил бы всё
    db.save_group_message(CHAT, 0, "Клодушка", "ответ бота", True)
    assert bot.build_group_tail(CHAT)[0]["content"] == "(…)"


# --- _dialog_call: повтор без истории и счётчик ---

@pytest.fixture()
def calls_log(monkeypatch):
    log = {"scans": [], "calls": []}
    monkeypatch.setattr(bot, "_spawn_background_task", lambda coro: (coro.close(), log["scans"].append(1)))
    bot._safety_strikes.clear()
    bot._safety_scan_running.clear()
    bot._safety_scan_last.clear()
    return log


def script_model(monkeypatch, log, results):
    """results — очередь: исключение (поднять) или любой объект (вернуть)."""
    q = list(results)

    async def fake(context, chat_id, **kw):
        log["calls"].append(kw)
        r = q.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(bot, "_model_call", fake)


def test_dialog_ok_resets_strikes(monkeypatch, calls_log):
    bot._safety_strikes[CHAT] = 1
    script_model(monkeypatch, calls_log, ["resp"])
    r = run(bot._dialog_call(None, CHAT, 1, True, retry_messages=lambda: ["x"], model="m", messages=["full"]))
    assert r == "resp" and bot._safety_strikes[CHAT] == 0 and len(calls_log["calls"]) == 1


def test_dialog_retry_without_history(monkeypatch, calls_log):
    script_model(monkeypatch, calls_log, [safety_exc(), "resp2"])
    r = run(bot._dialog_call(None, CHAT, 1, True, retry_messages=lambda: ["tail"], model="m", messages=["full"]))
    assert r == "resp2"
    assert calls_log["calls"][0]["messages"] == ["full"] and calls_log["calls"][1]["messages"] == ["tail"]
    assert bot._safety_strikes[CHAT] == 1                      # успех повтора счётчик не сбрасывает
    assert calls_log["scans"] == []


def test_dialog_retry_also_blocked_raises(monkeypatch, calls_log):
    script_model(monkeypatch, calls_log, [safety_exc(), safety_exc()])
    with pytest.raises(anthropic.BadRequestError) as ei:
        run(bot._dialog_call(None, CHAT, 1, True, retry_messages=lambda: ["tail"], model="m", messages=["full"]))
    assert api_errors.is_safety_block(ei.value)


def test_dialog_other_400_not_retried(monkeypatch, calls_log):
    script_model(monkeypatch, calls_log, [other_400()])
    with pytest.raises(anthropic.BadRequestError):
        run(bot._dialog_call(None, CHAT, 1, True, retry_messages=lambda: ["tail"], model="m", messages=["full"]))
    assert len(calls_log["calls"]) == 1 and bot._safety_strikes.get(CHAT, 0) == 0


def test_second_strike_spawns_scan_once_then_cooldown(monkeypatch, calls_log):
    script_model(monkeypatch, calls_log, [safety_exc(), "r", safety_exc(), "r", safety_exc(), "r", safety_exc(), "r"])
    args = dict(retry_messages=lambda: ["t"], model="m", messages=["f"])
    run(bot._dialog_call(None, CHAT, 1, True, **args))
    assert calls_log["scans"] == []                            # 1-й блок: ещё не скан
    run(bot._dialog_call(None, CHAT, 1, True, **args))
    assert len(calls_log["scans"]) == 1                        # 2-й: скан, счётчик обнулён
    assert bot._safety_strikes[CHAT] == 0
    bot._safety_scan_last[CHAT] = time.monotonic()             # пауза идёт — новые блоки скан не запускают
    run(bot._dialog_call(None, CHAT, 1, True, **args))
    run(bot._dialog_call(None, CHAT, 1, True, **args))
    assert len(calls_log["scans"]) == 1


def test_private_chat_has_no_scan(monkeypatch, calls_log):
    script_model(monkeypatch, calls_log, [safety_exc(), "r", safety_exc(), "r"])
    args = dict(retry_messages=lambda: ["t"], model="m", messages=["f"])
    run(bot._dialog_call(None, 77, 77, False, **args))
    run(bot._dialog_call(None, 77, 77, False, **args))
    assert calls_log["scans"] == [] and 77 not in bot._safety_strikes


def test_test_marker(monkeypatch):
    monkeypatch.setattr(bot, "SAFETY_TEST_MARKER", "МАРКЕР")

    async def ok(context, chat_id, **kw):
        return "ok"
    monkeypatch.setattr(bot, "call_claude", ok)
    assert run(bot._model_call(None, 1, messages=[{"role": "user", "content": "привет"}])) == "ok"
    with pytest.raises(anthropic.BadRequestError) as ei:
        run(bot._model_call(None, 1, messages=[{"role": "user", "content": [{"type": "text", "text": "а МАРКЕР б"}]}]))
    assert api_errors.is_safety_block(ei.value)


# --- safety_scan целиком (БД + build_group_messages настоящие, модель подменена) ---

@pytest.fixture()
def scan_env(tmpdb, monkeypatch):
    monkeypatch.setattr(bot, "HISTORY_STEP", 0)
    bot._HISTORY_ANCHORS.clear()
    bot._safety_scan_running.clear()
    sent = []

    async def fake_notify(text):
        sent.append(text)
    monkeypatch.setattr(bot, "notify_admins", fake_notify)
    state = {"calls": 0}

    async def fake_model(context, chat_id, **kw):
        state["calls"] += 1
        if BAD in bot._messages_plain_text(kw["messages"]):
            raise safety_exc()
        return "ok"
    monkeypatch.setattr(bot, "_model_call", fake_model)
    return sent, state


def test_scan_hides_bad_rows_and_duplicates(scan_env):
    sent, state = scan_env
    ids = fill(20, bad_idx=(3, 15))
    bad_old = ids[3]
    run(bot.safety_scan(CHAT, 1, manual=True))
    hidden = [r["id"] for r in db.list_hidden_group_messages(CHAT)]
    assert sorted(hidden) == sorted([ids[3], ids[15]]) and bad_old in hidden
    assert BAD not in str(bot.build_group_messages(CHAT, "", 50))
    assert len(sent) == 1 and BAD not in sent[0] and "реплика" not in sent[0]   # текст в отчёт не попадает
    assert f"id {ids[3]}" in sent[0] and state["calls"] <= bot.SAFETY_SCAN_MAX_CALLS
    assert CHAT not in bot._safety_scan_running


def test_scan_not_reproduced_hides_nothing(scan_env):
    sent, _ = scan_env
    fill(10)
    run(bot.safety_scan(CHAT, 1, manual=False))
    assert db.list_hidden_group_messages(CHAT) == [] and sent == []              # авто-скан молчит
    run(bot.safety_scan(CHAT, 1, manual=True))
    assert len(sent) == 1 and "не воспроизвёлся" in sent[0]                      # ручной — отвечает


def test_scan_memory_culprit_reports_without_hiding(scan_env, monkeypatch):
    sent, _ = scan_env
    fill(10)

    async def always_blocked(context, chat_id, **kw):
        raise safety_exc()
    monkeypatch.setattr(bot, "_model_call", always_blocked)
    run(bot.safety_scan(CHAT, 1, manual=True))
    assert db.list_hidden_group_messages(CHAT) == [] and "память" in sent[0]


def test_scan_budget(scan_env, monkeypatch):
    sent, state = scan_env
    fill(40, bad_idx=(10,))
    monkeypatch.setattr(bot, "SAFETY_SCAN_MAX_CALLS", 4)
    run(bot.safety_scan(CHAT, 1, manual=True))
    assert db.list_hidden_group_messages(CHAT) == [] and "бюджет" in sent[0] and state["calls"] <= 4


def test_scan_no_parallel(scan_env):
    sent, state = scan_env
    fill(5, bad_idx=(2,))
    bot._safety_scan_running.add(CHAT)
    run(bot.safety_scan(CHAT, 1, manual=True))
    assert state["calls"] == 0 and sent == []

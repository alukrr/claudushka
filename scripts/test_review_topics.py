"""Юнит-тесты дневного обзора с блоками тем (ТЗ feat/review-topics): окно, лог, нарезка сессий, деление под лимит
Telegram, ветки A/B. Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_review_topics.py -q
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402
import db  # noqa: E402

TZ = bot.BERLIN_TZ


def ts(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


def e(t, text="x", sender="A", is_bot=False):
    return {"id": 0, "ts": t, "sender": sender, "text": text, "is_bot": is_bot}


# --- окно ---

def test_window_regular_day():
    since, until = bot._review_window(datetime(2026, 10, 20, 21, 0, tzinfo=TZ))
    assert since == datetime(2026, 10, 19, bot.DAILY_REVIEW_HOUR, 0, tzinfo=TZ)
    assert (until - since) == timedelta(hours=24)


def test_window_dst_end_is_25h():
    # 25.10.2026 — переход CEST→CET: ночь на 25-е длится 25 часов
    since, until = bot._review_window(datetime(2026, 10, 25, 21, 0, tzinfo=TZ))
    assert (until.timestamp() - since.timestamp()) == 25 * 3600


def test_window_dst_start_is_23h():
    since, until = bot._review_window(datetime(2027, 3, 28, 21, 0, tzinfo=TZ))
    assert (until.timestamp() - since.timestamp()) == 23 * 3600


# --- лог ---

def test_log_drops_previous_review_and_formats():
    prev = f"({bot.REVIEW_SENT_LABEL} 50 сообщениям с прошлого обзора (21:00); ...)\nВчера было весело"
    rows = [e(ts(2026, 10, 20, 9, 5), "привет", "Аня"),
            e(ts(2026, 10, 19, 21, 0), prev, "Клодушка", True),
            e(ts(2026, 10, 20, 9, 6), "ответ бота", "Клодушка", True)]
    log = bot.build_review_log(rows)
    assert [x["line"] for x in log] == ["09:05 Аня: привет", "09:06 Клодушка: ответ бота"]


def test_log_human_text_named_like_review_is_kept():
    rows = [e(ts(2026, 10, 20, 9), f"({bot.REVIEW_SENT_LABEL} шутка)", "Боря")]
    assert len(bot.build_review_log(rows)) == 1


def test_log_clips_bot_stronger_than_humans():
    rows = [e(100, "я" * 1000, "Аня"), e(101, "б" * 1000, "Клодушка", True)]
    a, b = bot.build_review_log(rows)
    assert len(a["line"].split(": ", 1)[1]) == bot.REVIEW_USER_MSG_CHARS + 1
    assert len(b["line"].split(": ", 1)[1]) == bot.REVIEW_BOT_MSG_CHARS + 1
    assert a["line"].endswith("…")


def test_log_newlines_flattened():
    assert "\n" not in bot.build_review_log([e(100, "a\nb")])[0]["line"]


# --- сессии ---

def entries(*mins, size=10):
    return [{"ts": m * 60, "line": "x" * size, "is_bot": False} for m in mins]


def test_sessions_empty():
    assert bot.split_review_sessions([]) == []


def test_sessions_gap_boundary():
    # пауза ровно 45 мин — новая сессия; 44 — та же
    assert len(bot.split_review_sessions(entries(0, 44))) == 1
    assert len(bot.split_review_sessions(entries(0, 45))) == 2


def test_sessions_long_session_cut_on_message_boundary():
    es = entries(*range(0, 30), size=100)        # одна сессия, 30*101 знаков
    parts = bot.split_review_sessions(es, chunk_max=1000)
    assert len(parts) > 1
    assert sum(len(p) for p in parts) == 30
    assert all(sum(len(x["line"]) + 1 for x in p) <= 1000 for p in parts)


def test_sessions_across_dst_use_real_time():
    # 25.10.2026 02:30 CEST и 02:40 CET — между ними 70 минут реального времени (повтор часа)
    a = int(datetime(2026, 10, 25, 2, 30, tzinfo=TZ, fold=0).timestamp())
    b = int(datetime(2026, 10, 25, 2, 40, tzinfo=TZ, fold=1).timestamp())
    assert b - a == 70 * 60
    es = [{"ts": a, "line": "a", "is_bot": False}, {"ts": b, "line": "b", "is_bot": False}]
    assert len(bot.split_review_sessions(es)) == 2


# --- деление под Telegram ---

def test_split_short_is_one_message_with_footer():
    assert bot.split_telegram_text("обзор", "футер") == ["обзор\n\nфутер"]


def test_split_long_by_paragraph_footer_last():
    paras = ["п" * 1500 for _ in range(4)]
    parts = bot.split_telegram_text("\n\n".join(paras), "ФУТЕР")
    assert len(parts) == 2 and all(len(p) <= 4096 for p in parts)
    assert parts[-1].endswith("ФУТЕР")
    assert "\n\n".join(parts).replace("\n\nФУТЕР", "") == "\n\n".join(paras)


def test_split_giant_paragraph_cut_on_space():
    text = " ".join(["слово"] * 2000)
    parts = bot.split_telegram_text(text, "")
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 3
    assert " ".join(parts).split() == text.split()


# --- JSON выжимок ---

def test_cap_topics_fits():
    topics = [{"title": f"t{i}", "gist": "g" * 200, "moments": ["m" * 100], "quotes": ["q" * 100]} for i in range(300)]
    assert len(bot._cap_topics(topics, 5000)) <= 5000


# --- ветки A / B ---

def _patch_aux(monkeypatch, handler):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return handler(kw)
    monkeypatch.setattr(bot, "aux_create", fake)
    monkeypatch.setattr(bot, "response_text", lambda r: r)
    return calls


def _log(n, step_min=1, size=50):
    return [{"ts": i * step_min * 60, "line": f"{i} " + "x" * size, "is_bot": False} for i in range(n)]


def test_branch_a_small_log(monkeypatch):
    calls = _patch_aux(monkeypatch, lambda kw: "")
    branch, content = asyncio.run(bot.prepare_review_input(-1, _log(10)))
    assert branch == "A" and not calls and "x" * 50 in content


def test_branch_b_switch_zero(monkeypatch):
    monkeypatch.setattr(bot, "REVIEW_SINGLE_MAX_CHARS", 0)
    calls = _patch_aux(monkeypatch, lambda kw: '{"topics": [{"title": "Т", "gist": "г"}]}')
    branch, content = asyncio.run(bot.prepare_review_input(-1, _log(10)))
    assert branch == "B" and len(calls) == 1 and calls[0]["label"] == "daily_review_chunk"
    assert calls[0]["model"] == bot.DEFAULT_MODEL_ID
    assert all(calls[0].get(k) == v for k, v in bot.aux_params().items())   # служебный вызов — всегда с aux_params
    assert '"title": "Т"' in content


def test_branch_b_failed_chunk_skipped(monkeypatch):
    monkeypatch.setattr(bot, "REVIEW_SINGLE_MAX_CHARS", 0)
    monkeypatch.setattr(bot, "REVIEW_CHUNK_MAX_CHARS", 300)   # несколько частей
    n = {"i": 0}

    def handler(kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
        return '{"topics": [{"title": "ok"}]}'
    calls = _patch_aux(monkeypatch, handler)
    branch, content = asyncio.run(bot.prepare_review_input(-1, _log(20)))
    assert branch == "B" and len(calls) > 1 and '"ok"' in content


def test_branch_b_all_failed_returns_none(monkeypatch):
    monkeypatch.setattr(bot, "REVIEW_SINGLE_MAX_CHARS", 0)
    _patch_aux(monkeypatch, lambda kw: "не json")
    assert asyncio.run(bot.prepare_review_input(-1, _log(10))) is None


def test_note_keeps_filter_prefix():
    note = bot._review_sent_note(42)
    assert note.startswith(f"({bot.REVIEW_SENT_LABEL}") and "с прошлого обзора" in note and "42" in note


# --- db ---

def test_db_range_and_keep(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    if not hasattr(db, "DB_PATH"):
        return
    db.init_db()
    for i in range(5):
        db.save_group_message(-5, 1, "A", f"m{i}", is_bot=(i == 4))
    rows = db.get_group_messages_range(-5, 0, 2 ** 31)
    assert [r["text"] for r in rows] == [f"m{i}" for i in range(5)] and rows[-1]["is_bot"]
    assert db.get_group_messages_range(-5, 2 ** 31, 2 ** 32) == []
    assert db.GROUP_MESSAGES_KEEP == 3000

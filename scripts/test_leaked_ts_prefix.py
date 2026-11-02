"""Утечка формата транскрипта «[время] Имя:» в ответ модели и привязка реплики активности к сообщению-триггеру
(инцидент 2026-11-02, Москвичи). Без сети. Запуск: TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_leaked_ts_prefix.py -q
"""
import asyncio
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

strip = bot.strip_leaked_service_note


def test_prefix_cut_real_case():
    t = "[16:08] ᴀᴜɢᴇɴʙʟɪᴄᴋ: Просто они ещё не догадались автоматизировать отмазки через ИИ."
    assert strip(t) == ("Просто они ещё не догадались автоматизировать отмазки через ИИ.", True)


def test_prefix_with_date_and_spaces_in_name():
    assert strip("[01.11 09:15] Aleksei alukr: привет") == ("привет", True)


def test_invented_dialog_dropped():
    t = "[09:15] Елена: Клодуша, а что ты не ответила Вале про Афганистан?\n[09:16] Елена: Ну так что?"
    assert strip(t) == ("", True)


def test_time_in_middle_or_normal_brackets_untouched():
    for t in ("Встреча в [16:08] по плану", "[важно] читай внимательно: тут правила", "[16:08]", "Сказал: [16:08] Вася: привет"):
        assert strip(t) == (t, False)


def test_service_block_trigger_line():
    sb = bot.service_block(spontaneous=True, trigger="Miramare: «Как будто из нори сделаны» (в ответ на: «Шляпа Цифра»)")
    assert "Твоя реплика уйдёт ответом на это сообщение" in sb and "Как будто из нори сделаны" in sb and "Шляпа Цифра" in sb
    assert "без «[время] Имя:»" in sb
    assert "ответом на это сообщение" not in bot.service_block(spontaneous=True)


def test_activity_task_passes_trigger_and_strips_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    monkeypatch.setattr(bot, "context_bot_id", 1)
    chat = -1001
    db.save_group_message(chat, 5, "Miramare", "Как будто из нори сделаны")
    seen = {}

    async def fake_aux(**kw):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text='{"say": true}')], stop_reason="end_turn")

    async def fake_call(ctx, cid, **kw):
        seen["messages"] = kw["messages"]
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="[16:19] Miramare: Ну норм")], stop_reason="end_turn")

    sent = []

    class Msg:
        text = "Как будто из нори сделаны"
        caption = None
        from_user = SimpleNamespace(first_name="Miramare")
        reply_to_message = SimpleNamespace(text="Шляпа волшебника Цифра", caption=None)

        async def reply_text(self, t, **k):
            sent.append(t)

    monkeypatch.setattr(bot, "aux_create", fake_aux)
    monkeypatch.setattr(bot, "call_claude", fake_call)
    asyncio.run(bot._activity_task(Msg(), None, chat, 5))
    assert sent == ["Ну норм"]
    last = seen["messages"][-1]["content"]
    blob = last if isinstance(last, str) else " ".join(b.get("text", "") for b in last)
    assert "Miramare: «Как будто из нори сделаны»" in blob and "в ответ на: «Шляпа волшебника Цифра»" in blob

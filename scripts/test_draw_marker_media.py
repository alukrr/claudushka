"""Маркеры [[DRAW]]/[[FILE]] в ответах на фото и файлы + общий обработчик ошибок (fix/draw-marker-media, v2.4.2).

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_draw_marker_media.py -q
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

from telegram.error import BadRequest, Forbidden  # noqa: E402


# --- _extract_draw_marker ---

def test_marker_at_end_and_middle():
    assert bot._extract_draw_marker("Щас будет!\n[[DRAW: a cat on a roof]]") == ("Щас будет!", "a cat on a roof", None)
    text, prompt, size = bot._extract_draw_marker("Держи [[DRAW 2K: a red fox]] вот")
    assert "[[" not in text and prompt == "a red fox" and size == "2K"


def test_stub_prompt_is_dropped():
    assert bot._extract_draw_marker("Ок [[DRAW: ...]]") == ("Ок", None, None)


def test_leaked_prompt_recovered_but_russian_phrase_kept():
    note = bot._draw_sent_note("a woman in a red dress, realistic photo")
    text, prompt, _ = bot._extract_draw_marker(f"Вот {note}")
    assert prompt and "woman" in prompt and "a woman" not in text
    phrase = "не буду рисовать картинку: это тупо"
    assert bot._extract_draw_marker(phrase) == (phrase, None, None)


# --- _finish_media_answer ---

@pytest.fixture
def env(monkeypatch):
    calls = SimpleNamespace(draw=[], group=[], private=[])
    msg = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock())
    upd = SimpleNamespace(message=msg, effective_user=SimpleNamespace(first_name="Вася", username=None))

    async def fake_draw(update, context, chat_id, is_group, draw_prompt, **kw):
        calls.draw.append((draw_prompt, kw))
        return True

    monkeypatch.setattr(bot, "_draw_and_send", fake_draw)
    monkeypatch.setattr(bot.db, "save_group_message", lambda *a, **k: calls.group.append(a[3]))
    monkeypatch.setattr(bot.db, "save_message", lambda uid, role, text: calls.private.append((role, text)))
    return upd, msg, calls


def run(upd, answer, request, is_group=True):
    asyncio.run(bot._finish_media_answer(upd, None, chat_id=-100 if is_group else 7, user_id=7, is_group=is_group,
                                         answer=answer, history_user_text=f"[Фото] {request}", request_text=request))


def sent_texts(msg):
    return [c.args[0] for c in msg.reply_text.call_args_list]


def test_photo_draw_request_draws_and_hides_marker(env):
    upd, msg, calls = env
    run(upd, "Сделаю по референсу!\n[[DRAW: a realistic photo of a woman in the same pose]]", "нарисуй её в стиле аниме")
    assert all("[[DRAW" not in t for t in sent_texts(msg))
    assert sent_texts(msg) == ["Сделаю по референсу!"]
    assert calls.group == ["Сделаю по референсу!"]
    prompt, kw = calls.draw[0]
    assert "woman" in prompt and kw["en_prompt"] == prompt
    assert kw["silent_limit"] is False and kw["author"] == "Вася"   # явная просьба


def test_spontaneous_draw_on_photo_is_silent(env):
    upd, _, calls = env
    run(upd, "Красиво. [[DRAW: a sunset over the sea]]", "что на фото?")
    _, kw = calls.draw[0]
    assert kw["silent_limit"] is True and kw["author"] is None


def test_only_marker_group_saved_by_draw(env):
    upd, msg, calls = env
    run(upd, "[[DRAW: a cat]]", "нарисуй кота как на фото")
    assert sent_texts(msg) == [] and calls.group == []   # запись в транскрипт сделает _draw_and_send
    assert calls.draw


def test_private_history_has_note_not_marker(env):
    upd, _, calls = env
    run(upd, "[[DRAW: a cat]]", "нарисуй кота", is_group=False)
    assert calls.private[0] == ("user", "[Фото] нарисуй кота")
    role, text = calls.private[1]
    assert role == "assistant" and "[[DRAW" not in text and "a cat" in text


def test_file_marker_in_document_answer(env):
    upd, msg, calls = env
    run(upd, "Поправила:\n[[FILE: fix.py]]\nprint(1)\n[[/FILE]]", "исправь")
    assert all("[[FILE" not in t for t in sent_texts(msg))
    assert msg.reply_document.await_count == 1
    assert "fix.py" in calls.group[0] and not calls.draw


def test_plain_answer_unchanged(env):
    upd, msg, calls = env
    run(upd, "На фото кот.", "что тут?")
    assert sent_texts(msg) == ["На фото кот."] and calls.group == ["На фото кот."] and not calls.draw


# --- on_error ---

def _ctx(err):
    return SimpleNamespace(error=err)


def test_on_error_forbidden_is_warning(caplog):
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=-100))
    with caplog.at_level(logging.WARNING, logger=bot.logger.name):
        asyncio.run(bot.on_error(upd, _ctx(BadRequest("Not enough rights to send text messages to the chat"))))
        asyncio.run(bot.on_error(upd, _ctx(Forbidden("bot was kicked from the group chat"))))
    recs = [r for r in caplog.records if r.name == bot.logger.name]
    assert len(recs) == 2 and all(r.levelno == logging.WARNING for r in recs)


def test_on_error_other_is_error(caplog):
    with caplog.at_level(logging.WARNING, logger=bot.logger.name):
        asyncio.run(bot.on_error(None, _ctx(RuntimeError("boom"))))
    recs = [r for r in caplog.records if r.name == bot.logger.name]
    assert recs and recs[-1].levelno == logging.ERROR

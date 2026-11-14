"""Юнит-тесты обращения голосовым/кружочком реплаем на бота в группе (fix/voice-reply, docs/claude/media.md).

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_voice_reply.py -q
"""
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

BOT_ID = 4242
OTHER_ID = 555


@pytest.fixture(autouse=True)
def bot_identity(monkeypatch):
    monkeypatch.setattr(bot, "context_bot_id", BOT_ID)
    monkeypatch.setattr(bot, "bot_username", "claudushka_bot")


def _msg(*, voice=False, video_note=False, photo=False, document=False, video=False, text=None, caption=None,
         reply_to=None, chat_type="supergroup"):
    reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_to)) if reply_to is not None else None
    message = SimpleNamespace(
        text=text, caption=caption, entities=None, caption_entities=None,
        photo=[object()] if photo else None, document=object() if document else None, animation=None,
        voice=object() if voice else None, video_note=object() if video_note else None,
        video=object() if video else None, reply_to_message=reply,
    )
    return SimpleNamespace(message=message, effective_chat=SimpleNamespace(type=chat_type))


@pytest.mark.parametrize("kind", ["voice", "video_note"])
def test_reply_to_bot_is_addressed(kind):
    assert bot.is_bot_mentioned(_msg(**{kind: True}, reply_to=BOT_ID)) is True


@pytest.mark.parametrize("kind", ["voice", "video_note"])
def test_no_reply_not_addressed(kind):
    assert bot.is_bot_mentioned(_msg(**{kind: True})) is False


@pytest.mark.parametrize("kind", ["voice", "video_note"])
def test_reply_to_other_person_not_addressed(kind):
    assert bot.is_bot_mentioned(_msg(**{kind: True}, reply_to=OTHER_ID)) is False


def test_plain_video_without_caption_still_not_addressed():
    assert bot.is_bot_mentioned(_msg(video=True, reply_to=BOT_ID)) is False


def test_photo_and_document_unchanged():
    assert bot.is_bot_mentioned(_msg(photo=True, reply_to=BOT_ID)) is True
    assert bot.is_bot_mentioned(_msg(photo=True)) is False
    assert bot.is_bot_mentioned(_msg(document=True, reply_to=BOT_ID)) is True
    assert bot.is_bot_mentioned(_msg(document=True, reply_to=OTHER_ID)) is False


def test_text_unchanged():
    assert bot.is_bot_mentioned(_msg(text="привет", reply_to=BOT_ID)) is True
    assert bot.is_bot_mentioned(_msg(text="привет")) is False
    assert bot.is_bot_mentioned(_msg(text="@claudushka_bot привет")) is False  # entities нет — не mention
    assert bot.is_bot_mentioned(_msg(text="")) is False


def test_video_note_row_text():
    assert bot._video_note_row_text(None, None) == "[Кружок]"
    assert bot._video_note_row_text("привет", None) == "[Кружок] Сказано: привет"
    assert bot._video_note_row_text("привет", "человек машет") == "[Кружок] Сказано: привет. Видно: человек машет"


def test_update_last_group_message(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    chat, uid = -100500, 7
    db.save_group_message(chat, uid, "А", "[Голосовое]")
    db.save_group_message(chat, uid, "А", "[Голосовое]")
    db.save_group_message(chat, 8, "Б", "[Голосовое]")
    assert db.update_last_group_message(chat, uid, "[Голосовое]", "[Голосовое] текст") is True
    texts = [(e["user_id"], e["text"]) for e in db.get_group_transcript(chat, 10)]
    # меняется только ПОСЛЕДНЯЯ подходящая строка этого автора
    assert texts == [(uid, "[Голосовое]"), (uid, "[Голосовое] текст"), (8, "[Голосовое]")]
    assert db.update_last_group_message(chat, 999, "[Голосовое]", "x") is False

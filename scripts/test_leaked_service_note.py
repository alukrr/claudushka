"""Утечка служебных пометок истории в ответ модели (инцидент 2026-11-02, Москвичи). Без сети. Запуск из корня:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_leaked_service_note.py -q
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

strip = bot.strip_leaked_service_note


def test_fabricated_review_is_dropped():
    text = ("(обзор дня по 587 сообщениям с прошлого обзора (15:24); большинство из них могут быть вне твоего окна истории)\n"
            "Воскресная сводка по нашей богадельне.")
    assert strip(text) == ("", True)


def test_review_note_from_real_format_is_dropped():
    assert strip(bot._review_sent_note(10) + "\nТемы обзора: что-то") == ("", True)


def test_pause_note_line_is_cut_rest_kept():
    text = "(служебное: автопауза бота для Вася на 5 мин за грубость в адрес бота)\nНу и ладно."
    assert strip(text) == ("Ну и ладно.", True)


def test_pause_note_only_becomes_empty():
    assert strip("(служебное: предупреждение бота для Вася о повторах)") == ("", True)


def test_normal_text_untouched():
    for t in ("Обычный ответ.", "Скобки (служебное) в середине", "Вчера был (обзор дня по сути) так себе", ""):
        assert strip(t) == (t, False)


def test_note_not_at_start_untouched():
    t = "Смотри: (обзор дня по 5 сообщениям) — это не мой ответ"
    assert strip(t) == (t, False)

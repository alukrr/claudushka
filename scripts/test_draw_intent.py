"""Юнит-тесты разбора «нарисуй» и пометки draw_intent (feat/draw-intent, docs/claude/tz-draw-intent.md).

Без сети. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_draw_intent.py -q
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

P = bot.parse_draw_request


def test_literal_quotes_variants():
    assert P('нарисуй "кот в шляпе"') == ("literal", "кот в шляпе", None)
    assert P("нарисуй «кот в шляпе»") == ("literal", "кот в шляпе", None)
    assert P("нарисуй 2к “кот”") == ("literal", "кот", "2K")
    assert P("нарисуй 2k \"кот\"") == ("literal", "кот", "2K")
    assert P("нарисуй „кот“") == ("literal", "кот", None)


def test_literal_tail_appended():
    assert P("нарисуй «кот» в шляпе") == ("literal", "кот в шляпе", None)


def test_literal_unclosed_quote():
    assert P('нарисуй "кот в шляпе') == ("literal", "кот в шляпе", None)


def test_literal_case_and_comma():
    assert P('НАРИСУЙ, "кот"') == ("literal", "кот", None)


def test_literal_empty():
    assert P('нарисуй ""') == ("literal", "", None)
    assert P("нарисуй «»") == ("literal", "", None)


def test_intent_no_quotes():
    assert P("нарисуй кота в шляпе") == ("intent", "нарисуй кота в шляпе", None)
    assert P("нарисуй что происходит в чате")[0] == "intent"
    assert P("нарисуй") == ("intent", "нарисуй", None)
    assert P("нарисуй 2к кота") == ("intent", "нарисуй 2к кота", "2K")


def test_intent_trigger_not_first():
    assert P("а нарисуй кота")[0] == "intent"
    assert P("Клодушка, ну нарисуй кота")[0] == "intent"
    assert P('а нарисуй "кот"')[0] == "intent"   # не первым словом → не literal


def test_not_draw():
    assert P("покажи кота")[0] is None
    assert P("покажи курс евро")[0] is None
    assert P("")[0] is None
    assert P("привет как дела сегодня нарисуй")[0] is None   # триггер дальше 3-го слова


def test_draw_triggers_no_pokazhi():
    assert "покажи" not in bot.DRAW_TRIGGERS


def test_service_block_draw_intent():
    sb = bot.service_block(draw_intent=True)
    assert "явно просит нарисовать" in sb and "[[DRAW" in sb and "[[DRAW 2K" not in sb


def test_service_block_draw_2k():
    assert "[[DRAW 2K" in bot.service_block(draw_intent=True, draw_size="2K")


def test_service_block_default_clean():
    sb = bot.service_block()
    assert "нарисовать" not in sb and "[[DRAW" not in sb

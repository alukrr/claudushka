"""Тёзки и смена ника в групповом транскрипте (feat/group-nicks, docs/claude/tz-group-nicks.md, issue #3). Без сети, БД временная.
Все ники и имена здесь вымышленные.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts/test_group_nicks.py -q
"""
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

CHAT = -1001
A, B, C = 1000012345, 2000012345, 3000067890      # A и B — одинаковые последние 4 цифры (2345)
NOW = int(time.time())


def row(i, uid, nick, text="привет", is_bot=False):
    return {"id": i, "sender": nick, "text": text, "is_bot": is_bot, "ts": NOW + i, "user_id": uid}


@pytest.fixture()
def tmpdb(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    bot._HISTORY_ANCHORS.clear()
    yield
    bot._HISTORY_ANCHORS.clear()


def say(uid, nick, text="привет", is_bot=False):
    db.save_group_message(CHAT, uid, nick, text, is_bot=is_bot)


# ------------------------------------------------------------------ group_sender_labels

def test_no_namesakes_labels_are_nicks_byte_for_byte():
    rows = [row(1, A, "Саша"), row(2, C, " Маша "), row(3, 0, "Клодушка", is_bot=True)]
    labels = bot.group_sender_labels(rows)
    assert labels == {1: "Саша", 2: " Маша ", 3: "Клодушка"}                    # ни пробелов, ни регистра не трогаем


def test_namesakes_get_last_four_digits_case_insensitive():
    rows = [row(1, A, "Саша"), row(2, C, "саша "), row(3, A, "Саша")]
    labels = bot.group_sender_labels(rows)
    assert labels[1] == "Саша#2345" and labels[3] == "Саша#2345" and labels[2] == "саша#7890"


def test_colliding_last_four_digits_extend_until_distinct():
    rows = [row(1, A, "Саша"), row(2, B, "Саша")]                                # ...12345 против ...12345 → дальше
    labels = bot.group_sender_labels(rows)
    assert labels[1] != labels[2]
    assert labels[1] == "Саша#" + str(A)[-5:] or labels[1].startswith("Саша#")
    assert len({labels[1], labels[2]}) == 2 and labels[1].endswith(str(A)[-len(labels[1].split('#')[1]):])


def test_null_user_and_bot_untouched():
    rows = [row(1, A, "Саша"), row(2, C, "Саша"), row(3, None, "Саша"), row(4, A, "Саша", is_bot=True)]
    labels = bot.group_sender_labels(rows)
    assert labels[3] == "Саша" and labels[4] == "Саша"
    assert labels[1].startswith("Саша#") and labels[2].startswith("Саша#")


def test_single_person_two_nicks_no_suffix():
    rows = [row(1, A, "Саша"), row(2, A, "Алекс")]
    assert bot.group_sender_labels(rows) == {1: "Саша", 2: "Алекс"}


# ------------------------------------------------------------------ строки транскрипта и кэш

def test_group_line_uses_label():
    e = row(1, A, "Саша", "всем привет")
    assert bot._group_line(e, datetime_now()).endswith("Саша: всем привет")
    assert bot._group_line(e, datetime_now(), "Саша#2345").endswith("Саша#2345: всем привет")


def datetime_now():
    from datetime import datetime
    return datetime.now()


def test_build_group_messages_labels_namesakes_and_is_stable(tmpdb):
    say(A, "Саша", "я за кота")
    say(C, "Саша", "а я за собаку")
    say(0, "Клодушка", "рассуждаю", is_bot=True)
    say(A, "Саша", "ну и?")
    m1 = bot.build_group_messages(CHAT)
    m2 = bot.build_group_messages(CHAT)
    assert m1 == m2                                                              # два вызова на одном окне — одно и то же
    text = "\n".join(m["content"] for m in m1)
    assert "Саша#2345: я за кота" in text and "Саша#7890: а я за собаку" in text and "Саша#2345: ну и?" in text


def test_new_non_namesake_row_keeps_prefix(tmpdb):
    say(A, "Саша", "раз")
    say(C, "Саша", "два")
    say(0, "Клодушка", "ответ", is_bot=True)
    say(A, "Саша", "три")
    before = bot.build_group_messages(CHAT)
    say(0, "Клодушка", "ответ 2", is_bot=True)
    say(A, "Саша", "четыре")                                 # тот же человек, не новый тёзка
    after = bot.build_group_messages(CHAT)
    assert after[:len(before) - 1] == before[:-1]                                # всё, кроме последнего user-блока, не изменилось


def test_new_other_person_does_not_change_earlier_lines(tmpdb):
    say(A, "Саша", "раз")
    say(0, "Клодушка", "ответ", is_bot=True)
    say(A, "Саша", "два")
    before = bot.build_group_messages(CHAT)
    say(0, "Клодушка", "ответ 2", is_bot=True)
    say(B, "Лена", "я новая")                                                    # другой ник — не тёзка
    after = bot.build_group_messages(CHAT)
    assert after[:len(before) - 1] == before[:-1]


def test_new_namesake_rewrites_earlier_labels_once(tmpdb):
    say(A, "Саша", "раз")
    say(0, "Клодушка", "ответ", is_bot=True)
    say(A, "Саша", "два")
    before = bot.build_group_messages(CHAT)
    assert "Саша: раз" in before[0]["content"] and "#" not in before[0]["content"]
    say(0, "Клодушка", "ответ 2", is_bot=True)
    say(C, "Саша", "я другой Саша")
    after = bot.build_group_messages(CHAT)
    assert "Саша#2345: раз" in after[0]["content"]                               # один (допустимый) промах кэша на событие


def test_no_time_or_randomness_in_label():
    rows = [row(1, A, "Саша"), row(2, C, "Саша")]
    assert bot.group_sender_labels(rows) == bot.group_sender_labels(list(reversed(rows)))
    time.sleep(0.01)
    assert bot.group_sender_labels(rows) == bot.group_sender_labels(rows)


def test_build_group_tail_single_row_plain(tmpdb):
    say(A, "Саша", "последняя")
    [m] = bot.build_group_tail(CHAT)
    assert m["content"].endswith("Саша: последняя")


# ------------------------------------------------------------------ participants_note

def test_note_empty_without_nick_change():
    assert bot.participants_note([(A, "Саша"), (C, "Маша"), (A, "Саша")], CHAT) == ""
    assert bot.participants_note([(None, "Саша"), (None, "Саша2")], CHAT) == ""


def test_note_one_person_two_nicks_newest_first():
    note = bot.participants_note([(A, "Саша"), (A, "Алекс"), (C, "Маша")], None)
    assert note == "В истории один человек под разными никами: «Алекс» = «Саша»."


def test_note_with_profile_name(tmpdb):
    db.get_or_create_user(A, full_name="x")
    conn = db.get_conn()
    conn.execute("INSERT INTO user_profile (user_id, context, chat_id, field, value, source, updated_at, updated_by) "
                 "VALUES (?,?,?,?,?,?,?,?)", (A, "group", CHAT, "name", "Дима", "admin", NOW, 1))
    conn.commit()
    conn.close()
    note = bot.participants_note([(A, "Саша"), (A, "Алекс")], CHAT)
    assert "«Алекс» = «Саша» (по профилю — Дима)" in note


def test_note_size_limit_drops_whole_old_entries():
    people = []
    for i in range(30):                                                          # 30 человек со сменой ника, i-й — свежее
        uid = 5000 + i
        people += [(uid, f"Старый-ник-номер-{i}"), (uid, f"Новый-ник-номер-{i}")]
    note = bot.participants_note(people, None)
    assert 0 < len(note) <= bot.PARTICIPANTS_NOTE_MAX_CHARS == 400
    assert note.endswith(".") and "Новый-ник-номер-29" in note                   # свежие остались
    assert "Новый-ник-номер-0»" not in note                                      # старые отброшены целыми записями
    for part in note.split(": ", 1)[1].rstrip(".").split("; "):
        assert part.count("«") == part.count("»") >= 2                           # ни одной разорванной записи


def test_note_uses_window_labels_with_suffix(tmpdb):
    say(A, "Саша", "раз")
    say(C, "Саша", "я тёзка")
    say(A, "Алекс", "теперь я Алекс")
    say(0, "Клодушка", "ок", is_bot=True)
    say(C, "Саша", "и я тут")
    info = {}
    bot.build_group_messages(CHAT, info=info)
    assert info["note"] == "В истории один человек под разными никами: «Алекс» = «Саша#2345»."
    assert info["labels"][C] == "Саша#7890" and info["labels"][A] == "Алекс"


# ------------------------------------------------------------------ service_block, карточка автора

def test_service_block_note_only_in_tail_not_spontaneous():
    n = "В истории один человек под разными никами: «Алекс» = «Саша»."
    assert n in bot.service_block(participants_note=n)
    assert n not in bot.service_block(participants_note=n, spontaneous=True)
    assert "под разными никами" not in bot.service_block()


def test_system_prompt_untouched_by_note(tmpdb):
    s1 = bot.build_system(A, True, CHAT)
    say(A, "Саша")
    say(A, "Алекс")
    assert bot.build_system(A, True, CHAT) == s1                                # system/память от смены ника не зависят


def test_group_addendum_has_hash_rule(tmpdb):
    sysmsg = bot.build_system(A, True, CHAT)
    text = "".join(b["text"] for b in sysmsg) if isinstance(sysmsg, list) else sysmsg
    assert "«#цифры»" in text and "эти цифры в ответах не пиши" in text
    private = bot.build_system(A, False, None)
    ptext = "".join(b["text"] for b in private) if isinstance(private, list) else private
    assert "«#цифры»" not in ptext


def test_speaker_card_uses_window_label(tmpdb):
    db.get_or_create_user(C, full_name="x")
    conn = db.get_conn()
    conn.execute("INSERT INTO user_profile (user_id, context, chat_id, field, value, source, updated_at, updated_by) "
                 "VALUES (?,?,?,?,?,?,?,?)", (C, "group", CHAT, "name", "Дима", "admin", NOW, 1))
    conn.commit()
    conn.close()
    say(A, "Саша", "я первый")
    say(C, "Саша", "я второй")
    info = {}
    bot.build_group_messages(CHAT, info=info)
    upd = SimpleNamespace(effective_user=SimpleNamespace(full_name="Саша"))
    card = bot._speaker_card(upd, C, True, CHAT, info)
    assert card.startswith("Сейчас пишет Саша#7890 (по профилю — Дима).")        # та же подпись, что у его строк в окне
    plain = bot._speaker_card(upd, C, True, CHAT)                                # без info — ник, как раньше
    assert plain.startswith("Сейчас пишет Саша (по профилю — Дима).")


def test_speaker_card_same_name_as_profile_no_brackets(tmpdb):
    conn = db.get_conn()
    conn.execute("INSERT INTO user_profile (user_id, context, chat_id, field, value, source, updated_at, updated_by) "
                 "VALUES (?,?,?,?,?,?,?,?)", (C, "group", CHAT, "name", "Саша", "admin", NOW, 1))
    conn.commit()
    conn.close()
    card = bot.speaker_profile_card(C, CHAT, "Саша#7890")
    assert card.startswith("Сейчас пишет Саша#7890.")                            # имя в профиле = ник → скобок нет


# ------------------------------------------------------------------ обзор дня и активность

def test_review_log_labels_namesakes_and_carries_uid():
    rows = [dict(row(1, A, "Саша", "утро"), ts=NOW), dict(row(2, C, "Саша", "день"), ts=NOW + 60),
            dict(row(3, 0, "Клодушка", "ответ", is_bot=True), ts=NOW + 120)]
    log = bot.build_review_log(rows)
    assert [e["line"].split(" ", 1)[1] for e in log] == ["Саша#2345: утро", "Саша#7890: день", "Клодушка: ответ"]
    assert log[0]["uid"] == A and log[0]["label"] == "Саша#2345"


def test_review_log_without_namesakes_as_before():
    rows = [dict(row(1, A, "Саша", "утро"), ts=NOW)]
    [e] = bot.build_review_log(rows)
    assert e["line"].split(" ", 1)[1] == "Саша: утро"


def test_review_input_has_note_line_before_log():
    import asyncio
    rows = [dict(row(1, A, "Саша", "утро"), ts=NOW), dict(row(2, A, "Алекс", "день"), ts=NOW + 60)]
    entries = bot.build_review_log(rows)
    branch, text = asyncio.run(bot.prepare_review_input(CHAT, entries))
    assert branch == "A"
    head, _, log = text.partition("\n")
    assert log.startswith("В истории один человек под разными никами: «Алекс» = «Саша».\n") and "Алекс: день" in log


def test_review_input_no_note_when_no_change():
    import asyncio
    entries = bot.build_review_log([dict(row(1, A, "Саша", "утро"), ts=NOW)])
    _, text = asyncio.run(bot.prepare_review_input(CHAT, entries))
    assert "под разными никами" not in text


def test_review_range_returns_user_id(tmpdb):
    say(A, "Саша", "раз")
    rows = db.get_group_messages_range(CHAT, 0, NOW + 3600)
    assert rows[0]["user_id"] == A


def test_activity_input_labels_namesakes():
    entries = [row(1, A, "Саша", "а"), row(2, C, "Саша", "б")]
    out = bot.build_activity_input(entries)
    assert out.splitlines() == ["Саша#2345: а", "Саша#7890: б"]


def test_rows_without_ids_keep_own_senders():
    """Старые вызовы и тесты без поля id: подписи по порядку строк, а не по ключу id (иначе все получили бы подпись последней строки)."""
    rows = [{"sender": "Аня", "user_id": A, "is_bot": False}, {"sender": "Боря", "user_id": C, "is_bot": False}]
    assert bot.sender_labels_list(rows) == ["Аня", "Боря"]


def test_strip_nick_suffixes_exact_labels_only():
    text = "Саша#2345 — за кота, Саша#7890 — за собаку; тема #12345 и #2345 не трогаем."
    out = bot.strip_nick_suffixes(text, ["Саша#2345", "Саша#7890"])
    assert out == "Саша — за кота, Саша — за собаку; тема #12345 и #2345 не трогаем."
    assert bot.strip_nick_suffixes(text, None) == text and bot.strip_nick_suffixes("", ["Саша#1"]) == ""


def test_group_info_lists_suffixed_labels(tmpdb):
    say(A, "Саша", "раз")
    say(C, "Саша", "два")
    say(B, "Лена", "три")
    info = {}
    bot.build_group_messages(CHAT, info=info)
    assert sorted(info["suffixed"]) == ["Саша#2345", "Саша#7890"] or len(info["suffixed"]) == 2
    assert all("#" in x for x in info["suffixed"]) and info["tagged"] == 2

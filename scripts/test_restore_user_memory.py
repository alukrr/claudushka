"""Тесты scripts/restore_user_memory.py (docs/claude/tz-forget-restore.md). Временные базы, без сети, bot.py не импортируется.

    python -m pytest scripts/test_restore_user_memory.py -q
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import restore_user_memory as rum  # noqa: E402

NOW = 1_800_000_000
U, OTHER = 111, 222
G1, G2 = -1000000000001, -1000000000002

SCHEMA = """
CREATE TABLE memory (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, fact TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT 'private', chat_id INTEGER, created_at INTEGER NOT NULL,
    tier TEXT NOT NULL DEFAULT 'long', expires_at INTEGER);
CREATE TABLE user_profile (user_id INTEGER NOT NULL, context TEXT NOT NULL, chat_id INTEGER NOT NULL,
    field TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL, updated_at INTEGER NOT NULL, updated_by INTEGER,
    PRIMARY KEY (user_id, context, chat_id, field));
CREATE TABLE profile_proposals (id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE conversations (id INTEGER PRIMARY KEY, user_id INTEGER, content TEXT);
"""


def mk(path: Path, memory=(), profile=()):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany("INSERT INTO memory (user_id, fact, context, chat_id, created_at, tier, expires_at) VALUES (?,?,?,?,?,?,?)", memory)
    c.executemany("INSERT INTO user_profile VALUES (?,?,?,?,?,?,?,?)", profile)
    c.commit()
    c.close()
    return path


def q(path, sql, args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def L(fact, chat=G1, user=U, ts=100):
    return (user, fact, "group" if chat else "private", chat, ts, "long", None)


def M(fact, exp, chat=G1, user=U, ts=200):
    return (user, fact, "group", chat, ts, "medium", exp)


@pytest.fixture()
def paths(tmp_path):
    return tmp_path / "copy.db", tmp_path / "prod.db", tmp_path / "backups"


def go(paths, apply=False, chat_id=None, user=U, backup_dir=None):
    src, prod, bk = paths
    return rum.run(src, prod, user, chat_id, apply, backup_dir=backup_dir or bk, now=NOW)


def test_empty_prod_restores_everything(paths):
    mk(paths[0], [L("a"), L("b"), M("c", NOW + 1000), L("личный", chat=None)],
       [(U, "group", G1, "city", "X", "model", 5, 1), (U, "private", 0, "name", "Y", "admin", 6, 1)])
    mk(paths[1])
    rep = go(paths, apply=True)
    assert len(rep["add"]) == 4 and len(rep["profile_add"]) == 2 and rep["applied"]
    assert q(paths[1], "SELECT tier, COUNT(*) FROM memory GROUP BY tier ORDER BY tier") == [("long", 3), ("medium", 1)]
    # строки перенесены как есть: created_at, expires_at, chat_id (NULL у лички), source/updated_at/updated_by профиля
    assert q(paths[1], "SELECT created_at, expires_at FROM memory WHERE fact='c'") == [(200, NOW + 1000)]
    assert q(paths[1], "SELECT chat_id FROM memory WHERE fact='личный'") == [(None,)]
    assert q(paths[1], "SELECT source, updated_at, updated_by FROM user_profile WHERE field='name'") == [("admin", 6, 1)]
    assert Path(rep["backup"]).is_file() and Path(rep["backup"]).name.startswith("claudushka-restore-")


def test_merge_keeps_new_facts_and_skips_duplicate_text(paths):
    mk(paths[0], [L("a"), L("b")])
    mk(paths[1], [L("a", ts=999), L("новый после стирания", ts=999)])
    rep = go(paths, apply=True)
    assert len(rep["add"]) == 1 and len(rep["present_ids"]) == 1
    facts = sorted(r[0] for r in q(paths[1], "SELECT fact FROM memory"))
    assert facts == sorted(["a", "b", "новый после стирания"])
    assert q(paths[1], "SELECT created_at FROM memory WHERE fact='a'") == [(999,)]  # существующая строка не тронута


def test_expired_medium_skipped_and_boundary(paths):
    mk(paths[0], [M("старый", NOW - 1), M("ровно сейчас", NOW), M("живой", NOW + 1), L("long")])
    mk(paths[1])
    rep = go(paths, apply=True)
    assert rep["expired_ids"] == [1, 2] and len(rep["add"]) == 2
    assert sorted(r[0] for r in q(paths[1], "SELECT fact FROM memory")) == ["long", "живой"]


def test_private_fact_with_null_chat_not_duplicated(paths):
    mk(paths[0], [L("личный", chat=None)])
    mk(paths[1], [L("личный", chat=None)])
    rep = go(paths, apply=True)
    assert rep["add"] == [] and q(paths[1], "SELECT COUNT(*) FROM memory") == [(1,)]


def test_same_text_in_other_chat_is_added(paths):
    mk(paths[0], [L("a", chat=G1), L("a", chat=G2)])
    mk(paths[1], [L("a", chat=G1)])
    rep = go(paths, apply=True)
    assert len(rep["add"]) == 1 and q(paths[1], "SELECT chat_id FROM memory ORDER BY id") == [(G1,), (G2,)]


def test_profile_does_not_overwrite_existing_field(paths):
    mk(paths[0], profile=[(U, "group", G1, "city", "Казань", "admin", 5, 1), (U, "group", G1, "name", "Имя", "model", 5, 1)])
    mk(paths[1], profile=[(U, "group", G1, "city", "Москва", "admin", 900, 7)])
    rep = go(paths, apply=True)
    assert len(rep["profile_add"]) == 1 and rep["profile_present"] == 1 and list(rep["profile_fields"]) == ["name"]
    assert q(paths[1], "SELECT value, updated_at FROM user_profile WHERE field='city'") == [("Москва", 900)]
    assert q(paths[1], "SELECT value FROM user_profile WHERE field='name'") == [("Имя",)]


def test_chat_id_limits_facts_and_profile(paths):
    mk(paths[0], [L("a", chat=G1), L("b", chat=G2), L("личный", chat=None)],
       [(U, "group", G1, "city", "X", "model", 5, 1), (U, "group", G2, "city", "Y", "model", 5, 1)])
    mk(paths[1])
    rep = go(paths, apply=True, chat_id=G2)
    assert len(rep["add"]) == 1 and len(rep["profile_add"]) == 1
    assert q(paths[1], "SELECT fact FROM memory") == [("b",)]
    assert q(paths[1], "SELECT chat_id FROM user_profile") == [(G2,)]


def test_other_users_untouched(paths):
    mk(paths[0], [L("свой"), L("чужой", user=OTHER)], [(OTHER, "group", G1, "city", "Z", "model", 5, 1)])
    mk(paths[1], [L("был у чужого", user=OTHER)])
    go(paths, apply=True)
    assert q(paths[1], "SELECT user_id, fact FROM memory ORDER BY id") == [(OTHER, "был у чужого"), (U, "свой")]
    assert q(paths[1], "SELECT COUNT(*) FROM user_profile") == [(0,)]


def test_repeat_apply_adds_nothing(paths):
    mk(paths[0], [L("a"), L("a"), M("c", NOW + 5)], [(U, "group", G1, "city", "X", "model", 5, 1)])  # повтор текста в самой копии
    mk(paths[1])
    first = go(paths, apply=True)
    assert len(first["add"]) == 2 and len(first["dup_ids"]) == 1
    before = q(paths[1], "SELECT * FROM memory ORDER BY id"), q(paths[1], "SELECT * FROM user_profile")
    second = go(paths, apply=True)
    assert second["add"] == [] and second["profile_add"] == []
    assert (q(paths[1], "SELECT * FROM memory ORDER BY id"), q(paths[1], "SELECT * FROM user_profile")) == before


def test_backup_failure_writes_nothing(paths, tmp_path):
    mk(paths[0], [L("a")], [(U, "group", G1, "city", "X", "model", 5, 1)])
    mk(paths[1])
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")  # mkdir внутри файла не получится
    with pytest.raises(rum.RestoreError, match="не создалась"):
        go(paths, apply=True, backup_dir=blocker / "sub")
    assert q(paths[1], "SELECT COUNT(*) FROM memory") == [(0,)] and q(paths[1], "SELECT COUNT(*) FROM user_profile") == [(0,)]


def test_dry_run_does_not_change_file(paths):
    mk(paths[0], [L("a"), M("b", NOW + 5)], [(U, "group", G1, "city", "X", "model", 5, 1)])
    mk(paths[1], [L("уже", ts=1)])
    before = paths[1].read_bytes()
    rep = go(paths)
    assert len(rep["add"]) == 2 and len(rep["profile_add"]) == 1 and not rep["applied"] and rep["backup"] is None
    assert paths[1].read_bytes() == before and not paths[2].exists()


def test_missing_column_in_copy_is_clear_error_before_write(paths):
    c = sqlite3.connect(paths[0])
    c.executescript(SCHEMA.replace(",\n    tier TEXT NOT NULL DEFAULT 'long', expires_at INTEGER", ""))
    c.commit()
    c.close()
    mk(paths[1])
    with pytest.raises(rum.RestoreError, match="нет колонок.*tier"):
        go(paths, apply=True)
    assert not paths[2].exists() and q(paths[1], "SELECT COUNT(*) FROM memory") == [(0,)]


def test_missing_table_in_copy_is_clear_error(paths):
    c = sqlite3.connect(paths[0])
    c.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY)")
    c.commit()
    c.close()
    mk(paths[1])
    with pytest.raises(rum.RestoreError):
        go(paths, apply=True)
    assert not paths[2].exists()


def test_conversations_and_proposals_not_restored(paths):
    mk(paths[0], [L("a")])
    c = sqlite3.connect(paths[0])
    c.execute("INSERT INTO conversations (user_id, content) VALUES (?, 'x')", (U,))
    c.execute("INSERT INTO profile_proposals (user_id) VALUES (?)", (U,))
    c.commit()
    c.close()
    mk(paths[1])
    go(paths, apply=True)
    assert q(paths[1], "SELECT COUNT(*) FROM conversations") == [(0,)] and q(paths[1], "SELECT COUNT(*) FROM profile_proposals") == [(0,)]


def test_report_has_no_fact_text(paths, capsys):
    mk(paths[0], [L("секретный-текст-факта"), M("просроченный-секрет", NOW - 5)], [(U, "group", G1, "city", "секретное-значение", "model", 5, 1)])
    mk(paths[1])
    rum.print_report(go(paths))
    out = capsys.readouterr().out
    assert "секретн" not in out and "просроченный-секрет" not in out and "city" in out

"""Тесты автоудаления старых копий базы (feat/backup-purge, docs/claude/tz-backup-purge.md). Без сети.

    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_backup_purge.py -q
"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_orig_mkdir = Path.mkdir
Path.mkdir = lambda self, *a, **k: None if str(self).replace("\\", "/") == "/app/data" else _orig_mkdir(self, *a, **k)
import bot  # noqa: E402
Path.mkdir = _orig_mkdir

NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DATA_DIR", tmp_path)
    monkeypatch.setattr(bot, "DB_BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups").mkdir()
    return tmp_path


def mk(path, days_old, size=10):
    path.write_bytes(b"x" * size)
    t = NOW - days_old * DAY
    os.utime(path, (t, t))
    return path


def run(**kw):
    return bot.purge_old_db_backups(now=NOW, **kw)


def test_old_deleted_fresh_kept(data):
    for i in range(3):
        mk(data / "backups" / f"new{i}.db", i)
    old = mk(data / "backups" / "old.db", 20)
    r = run()
    assert r["deleted"] == ["old.db"] and not old.exists() and r["kept"] == 3 and r["freed_bytes"] == 10


def test_three_newest_kept_even_if_year_old(data):
    for i in range(5):
        mk(data / "backups" / f"b{i}.db", 365 + i)
    r = run()
    assert sorted(r["deleted"]) == ["b3.db", "b4.db"] and r["kept"] == 3
    assert all((data / "backups" / f"b{i}.db").exists() for i in range(3))


def test_fewer_than_three_nothing_deleted(data):
    mk(data / "backups" / "a.db", 400)
    mk(data / "claudushka.db.bak-1", 500)
    assert run() == {"deleted": [], "kept": 2, "freed_bytes": 0}


def test_boundary_exactly_14_days(data):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    edge = mk(data / "backups" / "edge.db", 14)
    over = mk(data / "backups" / "over.db", 14 + 1 / DAY * 2)
    r = run()
    assert edge.exists() and not over.exists() and r["deleted"] == ["over.db"]


def test_both_patterns_together(data):
    mk(data / "backups" / "n1.db", 1)
    mk(data / "claudushka.db.bak-n2", 2)
    mk(data / "backups" / "n3.db", 3)
    mk(data / "claudushka.db.bak-old", 30)
    mk(data / "backups" / "old.db", 31)
    r = run()
    assert sorted(r["deleted"]) == ["claudushka.db.bak-old", "old.db"] and r["kept"] == 3


def test_foreign_files_untouched(data):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    keep = [mk(data / "claudushka.db", 900), mk(data / "claudushka.db-wal", 900), mk(data / "claudushka.db-shm", 900),
            mk(data / "backups" / "notes.txt", 900), mk(data / "deleted-1.json", 900), mk(data / "other.db.bak-1", 900)]
    (data / "forget-backups").mkdir()
    keep.append(mk(data / "forget-backups" / "1-1.json", 900))
    (data / "dedup_runs").mkdir()
    keep.append(mk(data / "dedup_runs" / "r.db", 900))
    r = run()
    assert r["deleted"] == [] and all(p.exists() for p in keep)


def test_companions_deleted_with_copy_and_not_candidates(data):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    b = mk(data / "claudushka.db.bak-old", 30, 100)
    w = mk(data / "claudushka.db.bak-old-wal", 30, 5)
    s = mk(data / "claudushka.db.bak-old-shm", 30, 7)
    d = mk(data / "backups" / "x.db", 30, 100)
    dw = mk(data / "backups" / "x.db-wal", 30, 3)
    r = run()
    assert sorted(r["deleted"]) == ["claudushka.db.bak-old", "x.db"]
    assert not any(p.exists() for p in (b, w, s, d, dw)) and r["freed_bytes"] == 215


def test_orphan_companion_not_counted_as_fresh_copy(data):
    # -wal у свежей «копии» не должен занимать одно из трёх мест «свежих»
    mk(data / "claudushka.db.bak-a-wal", 0)
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    old = mk(data / "backups" / "old.db", 30)
    assert run()["deleted"] == ["old.db"] and not old.exists()


def test_symlink_and_dir_untouched(data):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    target = mk(data / "target.bin", 900)
    link = data / "backups" / "link.db"
    link.symlink_to(target)
    dr = data / "backups" / "dir.db"
    dr.mkdir()
    os.utime(dr, (NOW - 900 * DAY,) * 2)
    r = run()
    assert r["deleted"] == [] and link.is_symlink() and dr.is_dir() and target.exists()


def test_error_on_one_file_does_not_stop_others(data, monkeypatch):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    bad = mk(data / "backups" / "bad.db", 40)
    good = mk(data / "backups" / "good.db", 50)
    orig = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "bad.db":
            raise PermissionError("nope")
        return orig(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    r = run()
    assert r["deleted"] == ["good.db"] and bad.exists() and not good.exists() and r["kept"] == 4


def test_dry_run_deletes_nothing_same_list(data):
    for i in range(3):
        mk(data / "backups" / f"n{i}.db", 1)
    old = mk(data / "backups" / "old.db", 30, 100)
    w = mk(data / "backups" / "old.db-wal", 30, 4)
    dry = run(dry_run=True)
    assert old.exists() and w.exists() and dry["deleted"] == ["old.db"] and dry["freed_bytes"] == 104
    assert run() == dry | {"kept": 3}
    assert not old.exists()


def test_no_backups_dir_is_not_an_error(data):
    (data / "backups").rmdir()
    mk(data / "claudushka.db.bak-1", 0)
    assert run() == {"deleted": [], "kept": 1, "freed_bytes": 0}
    assert bot.purge_old_db_backups(now=NOW)["kept"] == 1

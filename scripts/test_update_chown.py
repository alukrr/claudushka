"""Юнит-тесты возврата владельца после /update (fix/update-chown, docs/claude/open-tasks.md → сделано).

Без сети и без root: os.chown подменён. Запуск из корня репозитория:
    TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_update_chown.py -q
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


def test_parent_dirs_include_new_nested_dir():
    # живой случай 2026-10-28: docs/claude/bench создан pull'ом от root
    dirs = bot._repo_parent_dirs(["bot.py", "docs/claude/bench/a.md", "docs/claude/x.md", "scripts/t.py"])
    assert dirs == ["docs", "scripts", "docs/claude", "docs/claude/bench"]


def test_parent_dirs_top_level_files_only():
    assert bot._repo_parent_dirs(["bot.py", "db.py"]) == []


def test_only_walk_paths_are_recursive(tmp_path, monkeypatch):
    git = tmp_path / ".git" / "objects"
    git.mkdir(parents=True)
    (git / "ab").write_text("x")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "untracked.tmp").write_text("x")
    calls = []
    monkeypatch.setattr(bot.os, "chown", lambda p, u, g: calls.append(p))
    # абсолютные пути: os.path.join("/repo", "/abs") == "/abs" — обходим захардкоженный /repo
    failed = bot._chown_repo_paths([str(tmp_path / ".git"), str(docs)], 1, 1, walk=(str(tmp_path / ".git"),))
    assert failed == []
    assert str(git / "ab") in calls                    # .git обходится рекурсивно
    assert str(docs) in calls
    assert str(docs / "untracked.tmp") not in calls    # каталог-родитель — без рекурсии

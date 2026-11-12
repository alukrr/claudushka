#!/usr/bin/env python3
"""Вернуть память и профиль участника из копии базы после случайного /forget.

Только stdlib; bot.py и db.py не импортирует, сеть не нужна. ТЗ — docs/claude/tz-forget-restore.md,
порядок работы — docs/claude/memory.md → «Восстановление после /forget».

  python3 scripts/restore_user_memory.py --from-db data/backups/<копия>.db --user-id N            # отчёт, ничего не пишет
  sudo python3 scripts/restore_user_memory.py --from-db <копия> --user-id N [--chat-id C] --apply  # записать
  sudo python3 scripts/restore_user_memory.py --from-json data/forget-backups/<uid>-<unix>.json --user-id N [--apply]   # из копии /forget

Слияние, не замена: из копии добавляются только факты, которых в боевой базе нет по (user_id, context, chat_id, fact),
и только непросроченные; поля профиля — INSERT OR IGNORE (существующее не перезаписывается). Тексты фактов не печатаются.
"""
import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "claudushka.db"
TABLES = ("memory", "user_profile")


class RestoreError(Exception):
    pass


def _open_source(path: Path) -> sqlite3.Connection:
    # копии в data/backups/ — WAL и root: обычный mode=ro падает на «attempt to write a readonly database»
    c = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _source_from_json(path: Path, user_id: int, dst: sqlite3.Connection) -> sqlite3.Connection:
    """Файл-копия /forget (data/forget-backups/<uid>-<unix>.json, пишет bot.py `_write_forget_backup`) -> временная база в памяти
    с теми же таблицами memory и user_profile, дальше правила слияния те же. profile_proposals из файла не возвращаем."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RestoreError(f"файл-копия не читается: {path} ({type(e).__name__}: {e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("memory"), list) or not isinstance(data.get("user_profile"), list):
        raise RestoreError(f"это не файл-копия /forget: {path}")
    if data.get("user_id") != user_id:
        raise RestoreError("user_id в файле-копии не совпадает с --user-id")
    src = sqlite3.connect(":memory:")
    src.row_factory = sqlite3.Row
    for t in TABLES:
        rows = data[t]
        cols = list(rows[0]) if rows else _cols(dst, t)   # пустая таблица — колонки боевой: схема-проверка не помешает
        src.execute(f"CREATE TABLE {t} ({', '.join(cols)})")
        src.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r.get(c) for c in cols) for r in rows])
    return src


def _open_prod_ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if not cols:
        raise RestoreError(f"в базе нет таблицы {table}")
    return cols


def check_schema(src: sqlite3.Connection, dst: sqlite3.Connection) -> dict[str, list[str]]:
    """Колонки для переноса (боевые без id) — по именам; не хватает в копии → ошибка до любой записи."""
    out = {}
    for t in TABLES:
        s, d = _cols(src, t), _cols(dst, t)
        want = [c for c in d if not (t == "memory" and c == "id")]
        missing = [c for c in want if c not in s]
        if missing:
            raise RestoreError(f"в копии у {t} нет колонок, которые есть в боевой: {', '.join(missing)}")
        out[t] = want
    return out


def plan(src: sqlite3.Connection, dst: sqlite3.Connection, user_id: int, chat_id, now: int, cols: dict) -> dict:
    """Что будет добавлено. Читает обе базы, ничего не пишет."""
    chat_sql, chat_args = ("", ())
    if chat_id is not None:
        chat_sql, chat_args = " AND chat_id = ?", (chat_id,)
    rows = src.execute(f"SELECT * FROM memory WHERE user_id = ?{chat_sql} ORDER BY id", (user_id, *chat_args)).fetchall()
    have = {(r[0], r[1], r[2], r[3]) for r in dst.execute(
        "SELECT user_id, context, chat_id, fact FROM memory WHERE user_id = ?", (user_id,))}
    rep = {"copy_total": len(rows), "expired_ids": [], "present_ids": [], "dup_ids": [], "add": [], "add_ids": []}
    seen = set(have)
    for r in rows:
        exp = r["expires_at"]
        if exp is not None and exp <= now:
            rep["expired_ids"].append(r["id"])
            continue
        key = (r["user_id"], r["context"], r["chat_id"], r["fact"])
        if key in have:
            rep["present_ids"].append(r["id"])
        elif key in seen:  # тот же текст дважды в самой копии — второй раз не добавляем (идемпотентность)
            rep["dup_ids"].append(r["id"])
        else:
            seen.add(key)
            rep["add"].append(tuple(r[c] for c in cols["memory"]))
            rep["add_ids"].append(r["id"])
    rep["by_tier"] = Counter(r[cols["memory"].index("tier")] for r in rep["add"]) if "tier" in cols["memory"] else Counter()
    rep["by_chat"] = Counter(r[cols["memory"].index("chat_id")] for r in rep["add"])

    prof = src.execute(f"SELECT * FROM user_profile WHERE user_id = ?{chat_sql} ORDER BY context, chat_id, field",
                       (user_id, *chat_args)).fetchall()
    have_p = {(r[0], r[1], r[2], r[3]) for r in dst.execute(
        "SELECT user_id, context, chat_id, field FROM user_profile WHERE user_id = ?", (user_id,))}
    rep["profile_total"] = len(prof)
    rep["profile_add"], rep["profile_present"] = [], 0
    for r in prof:
        if (r["user_id"], r["context"], r["chat_id"], r["field"]) in have_p:
            rep["profile_present"] += 1
        else:
            rep["profile_add"].append(tuple(r[c] for c in cols["user_profile"]))
    rep["profile_fields"] = Counter(r[cols["user_profile"].index("field")] for r in rep["profile_add"])
    return rep


def _backup(dst_path: Path, backup_dir: Path) -> Path:
    path = backup_dir / f"claudushka-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
    s = d = None
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        s = sqlite3.connect(str(dst_path), timeout=30)
        d = sqlite3.connect(str(path))
        s.backup(d)
    except Exception as e:
        raise RestoreError(f"копия боевой базы не создалась ({type(e).__name__}: {e}) — ничего не записано") from e
    finally:
        for c in (d, s):
            if c is not None:
                c.close()
    return path


def run(from_db: Path | None, db: Path, user_id: int, chat_id=None, apply: bool = False,
        backup_dir: Path | None = None, now: int | None = None, from_json: Path | None = None) -> dict:
    now = int(time.time()) if now is None else now
    if (from_db is None) == (from_json is None):
        raise RestoreError("нужен ровно один источник: --from-db или --from-json")
    for p, name in ((from_db or from_json, "копия"), (db, "боевая база")):
        if not Path(p).is_file():
            raise RestoreError(f"{name} не найдена: {p}")
    src = None
    try:
        dst = _open_prod_ro(Path(db))
        try:
            src = _open_source(Path(from_db)) if from_db else _source_from_json(Path(from_json), user_id, dst)
            cols = check_schema(src, dst)
            rep = plan(src, dst, user_id, chat_id, now, cols)
        finally:
            dst.close()
        rep["backup"] = None
        rep["applied"] = False
        if not apply:
            return rep

        rep["backup"] = _backup(Path(db), backup_dir or Path(db).parent / "backups")
        conn = sqlite3.connect(str(db), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # план пересчитываем внутри транзакции: бот пишет в ту же базу, пока мы работали
                final = plan(src, conn, user_id, chat_id, now, cols)
                mq = f"INSERT INTO memory ({', '.join(cols['memory'])}) VALUES ({', '.join('?' * len(cols['memory']))})"
                conn.executemany(mq, final["add"])
                pq = (f"INSERT OR IGNORE INTO user_profile ({', '.join(cols['user_profile'])}) "
                      f"VALUES ({', '.join('?' * len(cols['user_profile']))})")
                conn.executemany(pq, final["profile_add"])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        final["backup"], final["applied"] = rep["backup"], True
        return final
    finally:
        if src is not None:
            src.close()


def _ids(ids: list) -> str:
    return ", ".join(map(str, ids)) if ids else "—"


def print_report(rep: dict, file=None) -> None:
    file = file or sys.stdout
    p = lambda s="": print(s, file=file)
    verb = "добавлено" if rep["applied"] else "будет добавлено"
    p(f"Факты: в копии {rep['copy_total']}")
    p(f"  просрочено, пропущено: {len(rep['expired_ids'])} (id копии: {_ids(rep['expired_ids'])})")
    p(f"  уже есть в боевой: {len(rep['present_ids'])}")
    if rep["dup_ids"]:
        p(f"  повтор текста внутри копии, пропущено: {len(rep['dup_ids'])} (id копии: {_ids(rep['dup_ids'])})")
    p(f"  {verb}: {len(rep['add'])}")
    for tier, n in sorted(rep["by_tier"].items()):
        p(f"    tier={tier}: {n}")
    for cid, n in sorted(rep["by_chat"].items(), key=lambda kv: (kv[0] is None, kv[0])):
        p(f"    chat_id={'NULL (личка)' if cid is None else cid}: {n}")
    p(f"Профиль: в копии {rep['profile_total']} полей, уже есть в боевой {rep['profile_present']}, "
      f"{verb} {len(rep['profile_add'])}")
    if rep["profile_fields"]:
        p("    поля: " + ", ".join(sorted(rep["profile_fields"])))
    if rep["applied"]:
        p(f"Копия боевой базы перед записью: {rep['backup']}")
    else:
        p("Пробный прогон: боевая база не менялась. Для записи — --apply.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-db", type=Path, help="копия базы, из которой возвращаем")
    src.add_argument("--from-json", type=Path, help="файл-копия /forget (data/forget-backups/<uid>-<unix>.json)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="боевая база (по умолчанию data/claudushka.db)")
    ap.add_argument("--user-id", required=True, type=int)
    ap.add_argument("--chat-id", type=int, default=None, help="вернуть только этот чат")
    ap.add_argument("--apply", action="store_true", help="записать (без флага — только отчёт)")
    a = ap.parse_args(argv)
    try:
        rep = run(a.from_db, a.db, a.user_id, a.chat_id, a.apply, from_json=a.from_json)
    except RestoreError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    print_report(rep)
    return 0


if __name__ == "__main__":
    sys.exit(main())

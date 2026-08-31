#!/usr/bin/env python3
"""Ручная дедупликация фактов в таблице memory. НЕ часть бота, не импортируется bot.py —
запускать вручную с сервера, когда накопился дублирующий мусор (см. CLAUDE.md,
"Гигиена памяти"). Дефолт — dry-run, ничего не пишет в БД, пока не передан --apply.

Два режима:
  --mode exact  Убирает строго идентичные (user_id, context, chat_id, fact) — дёшево,
                без обращения к API, безопасно. Оставляет самую РАННЮЮ запись из дублей.
                Ловит только буквальные повторы, не варианты формулировок.

  --mode llm    Просит модель объединить факты, похожие по смыслу, но разные по
                формулировке (например "Поделился фото" / "Поделился фото в чате" /
                "Отправил фото в чат" -> один факт). Один вызов модели на группу
                (user_id, context, chat_id, tier) — СТОИТ ДЕНЕГ, задаётся --model
                (дефолт Haiku). Старые записи группы удаляются, вместо них
                вставляется объединённый список с tier и (для medium) максимальным
                expires_at среди объединяемых записей.

Фильтры (по умолчанию — вся таблица, сузьте перед первым запуском):
  --user-id ID        только этот пользователь
  --chat-id ID        только этот чат (для context=group)
  --tier long|medium  только этот tier
  --limit-groups N    (только --mode llm) не более N групп за один запуск —
                       подстраховка от случайного дорогого прогона на всей таблице

Примеры (без --apply — всегда dry-run, только печатает что было бы сделано):
  python3 dedup_memory.py --mode exact
  sudo python3 dedup_memory.py --mode exact --apply
  set -a; source .env; set +a
  python3 dedup_memory.py --mode llm --user-id 592441
  sudo -E python3 dedup_memory.py --mode llm --user-id 592441 --chat-id -1001032770549 --apply

Для --apply нужен sudo — каталог data/ обычно root:root (самообновление бота пишет
в БД изнутри контейнера от root); для --mode llm --apply — sudo -E, чтобы прокинуть
ANTHROPIC_API_KEY из окружения. Обычный dry-run (без --apply) sudo НЕ требует —
читает со снимка во временном файле, см. get_conn().
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "claudushka.db"


def get_conn(writable: bool) -> tuple[sqlite3.Connection, str | None]:
    """(connection, temp_path). temp_path не None — вызывающий обязан его удалить.

    WAL-режим требует прав на запись в -wal/-shm файлы ДАЖЕ ДЛЯ ЧТЕНИЯ (ограничение
    самого SQLite, не наше) — на сервере они root:root. Поэтому dry-run читает со
    снимка во временном файле (та же схема, что использовалась вручную весь этот
    день — cp data/claudushka.db /tmp/... && sqlite3 /tmp/...), а не с боевого файла:
    так --apply=False не требует sudo вообще. --apply=True подключается к настоящему
    файлу напрямую и пишет туда — для этого нужны реальные права (sudo).
    """
    if writable:
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA busy_timeout=5000")  # бот может писать параллельно
        conn.row_factory = sqlite3.Row
        return conn, None
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    shutil.copyfile(str(DB_PATH), tmp_path)
    conn = sqlite3.connect(tmp_path)
    conn.row_factory = sqlite3.Row
    return conn, tmp_path


def _filters(user_id, chat_id, tier):
    where, params = [], []
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if tier is not None:
        where.append("COALESCE(tier, 'long') = ?")
        params.append(tier)
    return where, params


def dedup_exact(conn, user_id=None, chat_id=None, tier=None, apply=False):
    where, params = _filters(user_id, chat_id, tier)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id, user_id, context, chat_id, fact FROM memory {where_sql} "
        f"ORDER BY user_id, context, chat_id, fact, created_at",
        params,
    ).fetchall()

    seen, to_delete = set(), []
    for r in rows:
        key = (r["user_id"], r["context"], r["chat_id"], r["fact"])
        if key in seen:
            to_delete.append(r["id"])
        else:
            seen.add(key)

    print(f"[exact] строк в области фильтра: {len(rows)}, точных дублей: {len(to_delete)}")
    if not apply:
        print("[exact] dry-run — ничего не удалено, добавь --apply")
        return
    if to_delete:
        conn.executemany("DELETE FROM memory WHERE id = ?", [(i,) for i in to_delete])
        conn.commit()
    print(f"[exact] удалено {len(to_delete)} строк")


def _groups_for_llm(conn, user_id, chat_id, tier):
    where, params = _filters(user_id, chat_id, tier)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id, user_id, context, chat_id, fact, COALESCE(tier, 'long') AS tier, expires_at "
        f"FROM memory {where_sql} ORDER BY user_id, context, chat_id, tier",
        params,
    ).fetchall()
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["user_id"], r["context"], r["chat_id"], r["tier"])
        groups.setdefault(key, []).append(r)
    return groups


def dedup_llm(conn, user_id=None, chat_id=None, tier=None, apply=False,
              model="claude-haiku-4-5-20251001", limit_groups=None):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY не задан в окружении — source .env (и sudo -E, если apply)")
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    groups = _groups_for_llm(conn, user_id, chat_id, tier)
    candidates = {k: v for k, v in groups.items() if len(v) >= 2}
    print(f"[llm] групп (user,context,chat,tier) в фильтре: {len(groups)}, с 2+ фактами: {len(candidates)}")
    if limit_groups:
        candidates = dict(list(candidates.items())[:limit_groups])
        print(f"[llm] ограничено --limit-groups до {len(candidates)} групп")

    total_before = total_after = 0
    for (uid, ctx, cid, grp_tier), rows in candidates.items():
        facts = [r["fact"] for r in rows]
        total_before += len(facts)
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                system=(
                    "Тебе дан список фактов об одном человеке — часть из них дубли или "
                    "перефразировки одного и того же. Объедини по смыслу, убери повторы, "
                    "оставь МИНИМАЛЬНЫЙ набор уникальных фактов, каждый — короткая фраза "
                    "как в источнике. Не выдумывай ничего нового, не добавляй факты, "
                    "которых не было. Верни ТОЛЬКО JSON: {\"facts\": [\"...\", \"...\"]}"
                ),
                messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
            )
            text = "".join(b.text for b in resp.content if hasattr(b, "text"))
            data = json.loads(text[text.find("{"):text.rfind("}") + 1])
            merged = [f.strip() for f in data.get("facts", []) if isinstance(f, str) and f.strip()]
        except Exception as e:
            print(f"[llm] user={uid} chat={cid} tier={grp_tier}: ошибка ({e}), пропуск")
            total_after += len(facts)
            continue

        print(f"[llm] user={uid} chat={cid} tier={grp_tier}: {len(facts)} -> {len(merged)}")
        total_after += len(merged) or len(facts)
        if not merged:
            continue
        if apply:
            ids = [r["id"] for r in rows]
            conn.executemany("DELETE FROM memory WHERE id = ?", [(i,) for i in ids])
            expires_at = max((r["expires_at"] for r in rows if r["expires_at"]), default=None)
            now = int(time.time())
            conn.executemany(
                "INSERT INTO memory (user_id, context, chat_id, fact, tier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(uid, ctx, cid, f, grp_tier, now, expires_at) for f in merged],
            )
            conn.commit()

    suffix = "" if apply else " (dry-run — ничего не записано, добавь --apply)"
    print(f"[llm] итого по обработанным группам: {total_before} -> {total_after}{suffix}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["exact", "llm"], required=True)
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument("--chat-id", type=int, default=None)
    p.add_argument("--tier", choices=["long", "medium"], default=None)
    p.add_argument("--limit-groups", type=int, default=None, help="только для --mode llm")
    p.add_argument("--model", default="claude-haiku-4-5-20251001", help="только для --mode llm")
    p.add_argument("--apply", action="store_true", help="реально писать в БД (иначе dry-run)")
    args = p.parse_args()

    if not DB_PATH.exists():
        sys.exit(f"Не нашла БД: {DB_PATH}")

    conn, tmp_path = get_conn(writable=args.apply)
    if tmp_path:
        print(f"[dry-run] читаю снимок {tmp_path} (боевой файл не трогаю, sudo не нужен)")
    try:
        if args.mode == "exact":
            dedup_exact(conn, args.user_id, args.chat_id, args.tier, args.apply)
        else:
            dedup_llm(conn, args.user_id, args.chat_id, args.tier, args.apply,
                      args.model, args.limit_groups)
    finally:
        conn.close()
        if tmp_path:
            os.remove(tmp_path)


if __name__ == "__main__":
    main()

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
                (короткое имя haiku/sonnet/opus/fable, как в bot.py, дефолт haiku,
                либо полная API-строка). Старые записи группы удаляются, вместо
                них вставляется объединённый список с tier и (для medium)
                максимальным expires_at среди объединяемых записей. Нужен пакет
                anthropic и ANTHROPIC_API_KEY — на голом хосте их обычно нет
                (см. ниже про docker exec).

Фильтры (по умолчанию — вся таблица, сузьте перед первым запуском):
  --user-id ID        только этот пользователь
  --chat-id ID        только этот чат (для context=group)
  --tier long|medium  только этот tier
  --limit-groups N    (только --mode llm) не более N групп за один запуск —
                       подстраховка от случайного дорогого прогона на всей таблице

Примеры (без --apply — всегда dry-run, только печатает что было бы сделано):
  ./dedup_memory.py --mode exact
  sudo ./dedup_memory.py --mode exact --apply

  # --mode llm на голом хосте: нужны `pip install anthropic==0.43.0` и ключ в окружении
  set -a; source .env; set +a
  ./dedup_memory.py --mode llm --model sonnet --user-id 592441
  sudo -E ./dedup_memory.py --mode llm --model sonnet --user-id 592441 --apply

  # --mode llm через контейнер — ПРОЩЕ: anthropic и ключ там уже есть, sudo не нужен
  # (процесс внутри контейнера и так root), /repo — это ~/claudushka, смонтирован туда
  docker exec claudushka python3 /repo/dedup_memory.py --mode llm --model sonnet --user-id 592441
  docker exec claudushka python3 /repo/dedup_memory.py --mode llm --model sonnet --user-id 592441 --apply

Для --apply на ГОЛОМ ХОСТЕ нужен sudo — каталог data/ обычно root:root (самообновление
бота пишет в БД изнутри контейнера от root); для --mode llm --apply на хосте — sudo -E,
чтобы прокинуть ANTHROPIC_API_KEY. Обычный dry-run (без --apply) sudo НЕ требует —
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

# Короткие имена — как в реестре MODELS в bot.py, чтобы не помнить точные API-строки.
MODEL_ALIASES = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus": "claude-opus-5",
    "fable": "claude-fable-5",
}

# $/MTok in,out — продублировано из реестра MODELS в bot.py (сверять при изменении цен там).
PRICING = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}
# Стандартные множители Anthropic для prompt caching (5-минутный ephemeral, тот тип,
# что реально приходит с этого ключа/пула — см. usage.cache_creation).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


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


def dedup_exact(conn, user_id=None, chat_id=None, tier=None, apply=False, verbose=False):
    where, params = _filters(user_id, chat_id, tier)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id, user_id, context, chat_id, fact FROM memory {where_sql} "
        f"ORDER BY user_id, context, chat_id, fact, created_at",
        params,
    ).fetchall()

    seen, kept_id, to_delete = set(), {}, []
    for r in rows:
        key = (r["user_id"], r["context"], r["chat_id"], r["fact"])
        if key in seen:
            to_delete.append(r["id"])
            if verbose:
                print(f"[exact][dup] user={r['user_id']} chat={r['chat_id']}: "
                      f"{r['fact']!r} (id={r['id']}, оставляем id={kept_id[key]})")
        else:
            seen.add(key)
            kept_id[key] = r["id"]

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
              model="claude-haiku-4-5-20251001", limit_groups=None, verbose=False):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY не задан в окружении — source .env (и sudo -E, если apply), "
                 "либо запусти через docker exec claudushka — там ключ уже есть, см. --help")
    try:
        import anthropic
        import api_errors  # тот же общий модуль, что у bot.py/whatsapp.py — не изобретаем заново
    except ModuleNotFoundError as e:
        sys.exit(
            f"Не хватает модуля ({e}) — он есть только внутри контейнера.\n"
            "Либо: pip install anthropic==0.43.0 (api_errors.py — свой файл рядом, копировать не надо)\n"
            "Либо (проще, не нужен ни pip, ни sudo — контейнер уже root и с ключом в окружении):\n"
            "  docker exec claudushka python3 /repo/dedup_memory.py --mode llm ...\n"
            "  docker exec claudushka python3 /repo/dedup_memory.py --mode llm ... --apply"
        )
    client = anthropic.Anthropic(api_key=api_key)

    groups = _groups_for_llm(conn, user_id, chat_id, tier)
    candidates = {k: v for k, v in groups.items() if len(v) >= 2}
    total_candidate_groups = len(candidates)  # до обрезки --limit-groups — нужно для экстраполяции ниже
    print(f"[llm] групп (user,context,chat,tier) в фильтре: {len(groups)}, с 2+ фактами: {total_candidate_groups}")
    if limit_groups:
        candidates = dict(list(candidates.items())[:limit_groups])
        print(f"[llm] ограничено --limit-groups до {len(candidates)} групп")

    total_before = total_after = 0
    total_in_tok = total_cache_write_tok = total_cache_read_tok = total_out_tok = 0
    started = time.monotonic()
    for (uid, ctx, cid, grp_tier), rows in candidates.items():
        facts = [r["fact"] for r in rows]
        total_before += len(facts)
        try:
            # max_tokens=8192: на группах в сотни-тысячи фактов (живой случай — 1441
            # medium-фактов в одном чате) даже 4096 не хватило и ответ обрывался — тот же
            # класс проблемы, что инцидент 2026-08-03 в extract_all_participants_memory
            # (см. CLAUDE.md). parse_json_lenient ниже переживёт обрыв и на этом лимите —
            # это подстраховка, а не гарантия, что 8192 хватит на ЛЮБую группу; если
            # снова обрежет — сузьте --user-id/--chat-id/--tier и прогоните по частям.
            resp = client.messages.create(
                model=model,
                max_tokens=8192,
                system=(
                    "Тебе дан список фактов об одном человеке — часть из них дубли или "
                    "перефразировки одного и того же. Объедини по смыслу, убери повторы, "
                    "оставь МИНИМАЛЬНЫЙ набор уникальных фактов, каждый — короткая фраза "
                    "как в источнике. Не выдумывай ничего нового, не добавляй факты, "
                    "которых не было. Верни ТОЛЬКО JSON: {\"facts\": [\"...\", \"...\"]}"
                ),
                messages=[{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}],
            )
            # response_text/parse_json_lenient — те же хелперы, что у bot.py/whatsapp.py,
            # не голый content[0].text/find+rfind: у пятого поколения content[0] может
            # быть thinking-блоком без текста, а обрезанный по max_tokens JSON рвёт
            # наивный find/rfind (оба класса багов задокументированы в CLAUDE.md).
            if api_errors.was_truncated(resp):
                print(f"[llm] user={uid} chat={cid} tier={grp_tier}: ответ обрезан по max_tokens")
            text = api_errors.response_text(resp)
            data = api_errors.parse_json_lenient(text, "{", label=f"dedup user={uid} chat={cid}")
            merged = [f.strip() for f in (data or {}).get("facts", []) if isinstance(f, str) and f.strip()]
            # Этот ключ/пул автоматически кеширует большие входы (prompt caching) —
            # для уникального, ни разу не повторяющегося входа (каждая группа фактов
            # своя) это ВСЕГДА запись в кэш, никогда чтение: input_tokens сам по себе
            # почти пустой (несколько токенов), а реальный вес — в
            # cache_creation_input_tokens. Проверено живьём 2026-08-31: на 400 фактах
            # (40690 симв.) input_tokens=2, cache_creation_input_tokens=16186 — без
            # этой строки оценка стоимости занижалась в ~100 раз. Цена записи в кэш —
            # премия ~1.25x к обычному input (5-минутный ephemeral, как здесь), чтение
            # из кэша было бы ~0.1x, но тут читать нечего — каждый вход уникален.
            usage = getattr(resp, "usage", None)
            in_tok = getattr(usage, "input_tokens", 0) or 0
            cache_write_tok = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read_tok = getattr(usage, "cache_read_input_tokens", 0) or 0
            out_tok = getattr(usage, "output_tokens", 0) or 0
            total_in_tok += in_tok
            total_cache_write_tok += cache_write_tok
            total_cache_read_tok += cache_read_tok
            total_out_tok += out_tok
            if verbose:
                print(f"[llm][токены] user={uid} chat={cid} tier={grp_tier}: "
                      f"in={in_tok} cache_write={cache_write_tok} cache_read={cache_read_tok} out={out_tok}")
        except Exception as e:
            print(f"[llm] user={uid} chat={cid} tier={grp_tier}: ошибка ({e}), пропуск")
            total_after += len(facts)
            continue

        print(f"[llm] user={uid} chat={cid} tier={grp_tier}: {len(facts)} -> {len(merged)}")
        if verbose:
            print(f"[llm][было] user={uid} chat={cid} tier={grp_tier}:")
            for f in facts:
                print(f"    - {f}")
            print(f"[llm][стало] user={uid} chat={cid} tier={grp_tier}:")
            for f in merged:
                print(f"    + {f}")
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

    elapsed = time.monotonic() - started
    price_in, price_out = PRICING.get(model, (None, None))
    cost = None
    if price_in is not None:
        # cache_creation ~1.25x обычного input (5-минутный ephemeral, см. CACHE_WRITE_MULTIPLIER),
        # cache_read ~0.1x — но для дедупа он почти всегда 0: каждый вход уникален,
        # читать из кэша нечего, это ВСЕГДА запись, никогда чтение.
        cost = (
            total_in_tok / 1_000_000 * price_in
            + total_cache_write_tok / 1_000_000 * price_in * CACHE_WRITE_MULTIPLIER
            + total_cache_read_tok / 1_000_000 * price_in * CACHE_READ_MULTIPLIER
            + total_out_tok / 1_000_000 * price_out
        )
    cost_str = f"${cost:.4f}" if cost is not None else "? (модель не в PRICING — сверься с /cost в боте)"
    n_groups = len(candidates)
    per_group = elapsed / n_groups if n_groups else 0

    suffix = "" if apply else " (dry-run — ничего не записано, добавь --apply)"
    print(f"[llm] итого по обработанным группам: {total_before} -> {total_after}{suffix}")
    print(f"[llm] время: {elapsed:.1f}с на {n_groups} групп (~{per_group:.1f}с/группу)")
    print(f"[llm] токены: in={total_in_tok} cache_write={total_cache_write_tok} "
          f"cache_read={total_cache_read_tok} out={total_out_tok}, оценка стоимости: {cost_str}")

    # Экстраполяция на ВЕСЬ фильтр (полезно после пробного --limit-groups N, прежде чем
    # снимать лимит и гонять все группы разом): грубая, по СРЕДНЕМУ на группу — реальные
    # группы сильно различаются по размеру (от 2 фактов до 1441), не точная оценка.
    if limit_groups and n_groups < total_candidate_groups and cost is not None:
        factor = total_candidate_groups / n_groups
        print(f"[llm] грубая экстраполяция на все {total_candidate_groups} групп в фильтре "
              f"(± сильно, группы разного размера): ~{elapsed * factor / 60:.1f} мин, "
              f"~${cost * factor:.2f}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["exact", "llm"], required=True)
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument("--chat-id", type=int, default=None)
    p.add_argument("--tier", choices=["long", "medium"], default=None)
    p.add_argument("--limit-groups", type=int, default=None, help="только для --mode llm")
    p.add_argument("--model", default="haiku",
                    help="только для --mode llm — короткое имя (haiku/sonnet/opus/fable, "
                         "как в bot.py) или полная API-строка")
    p.add_argument("--apply", action="store_true", help="реально писать в БД (иначе dry-run)")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="показать сами факты (exact: какой дубль какому id соответствует; "
                         "llm: полный список 'было'/'стало' по каждой группе), не только счётчики")
    args = p.parse_args()
    args.model = MODEL_ALIASES.get(args.model, args.model)

    if not DB_PATH.exists():
        sys.exit(f"Не нашла БД: {DB_PATH}")

    conn, tmp_path = get_conn(writable=args.apply)
    if tmp_path:
        print(f"[dry-run] читаю снимок {tmp_path} (боевой файл не трогаю, sudo не нужен)")
    try:
        if args.mode == "exact":
            dedup_exact(conn, args.user_id, args.chat_id, args.tier, args.apply, args.verbose)
        else:
            dedup_llm(conn, args.user_id, args.chat_id, args.tier, args.apply,
                      args.model, args.limit_groups, args.verbose)
    finally:
        conn.close()
        if tmp_path:
            os.remove(tmp_path)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Ручная дедупликация и точечная правка фактов в таблице memory. НЕ часть бота, не
импортируется bot.py — запускать вручную с сервера (см. docs/claude/open-tasks.md,
"Гигиена памяти").
Дефолт — dry-run, ничего не пишет в БД, пока не передан --apply.

Интерактивный режим (TUI, curses, в стиле htop) — без --mode или с --tui:
  docker exec -it claudushka python3 /repo/dedup_memory.py
Выбор чатов/групп, модели, оценка стоимости по всем моделям БЕЗ обращения к API, пробный
прогон, просмотр «было | стало», применение отрецензированного (без повторного вызова модели).
Клавиши: Пробел — выбрать, F2 модель, F3 поиск, F4 фильтр, F5 пробный прогон, F6 сорт,
F7 цены, F8 применить, F1 полная помощь, F10 выход. Правка фактов вручную: в просмотре группы
«было | стало» — e (режим построчной правки «стало»: Enter правка, a добавить, d удалить, → вернуть
из «было», u отмена, R сброс, b скрыть/показать «было»); на строке группы в главном списке e — та же
правка БЕЗ модели (бесплатно, статус manual). ВАЖНО: пробный прогон тоже вызывает API
и стоит денег (бесплатен только для БД). Применение: бэкап БД в data/backups/ -> ввод APPLY ->
группа пишется, только если все её строки из снимка ещё на месте. Журнал каждого прогона —
data/dedup_runs/, калибровка оценок — data/dedup_calibration.json. Без TTY — код выхода 2.

Режимы CLI:
  --mode exact   Убирает строго идентичные (user_id, context, chat_id, fact) — дёшево,
                 без обращения к API, безопасно. Оставляет самую РАННЮЮ запись из дублей.
                 Ловит только буквальные повторы, не варианты формулировок.

  --mode llm     Просит модель объединить факты, похожие по смыслу, но разные по
                 формулировке (например "Поделился фото" / "Поделился фото в чате" /
                 "Отправил фото в чат" -> один факт). Один вызов модели на группу
                 (user_id, context, chat_id, tier) — СТОИТ ДЕНЕГ, задаётся --model
                 (короткое имя haiku/sonnet/opus/fable, как в bot.py, дефолт haiku,
                 либо полная API-строка). Старые записи группы удаляются, вместо
                 них вставляется объединённый список с tier и (для medium)
                 максимальным expires_at среди объединяемых записей. Нужен пакет
                 anthropic и ANTHROPIC_API_KEY — на голом хосте их обычно нет
                 (см. ниже про docker exec). Печатает прогресс [i/N], ETA и потрачено $.
                 Группа с ОБРЕЗАННЫМ ответом (max_tokens) никогда не применяется.

  --mode purge-expired  Удаляет просроченные строки (expires_at <= now: medium старше 7 дней) — бот
                 их не читает, но раньше их никто не удалял. Без модели, дёшево. dry-run считает,
                 --apply делает бэкап и удаляет. (Бот чистит их и сам: раз в сутки 04:30 Berlin.)

  --mode delete  Точечно удаляет конкретный факт — --fact "точный текст" (--like — как
                 подстрока, LIKE %...%), ОБЯЗАТЕЛЕН --user-id (иначе слишком легко
                 задеть чужие факты, если формулировка не уникальна). Для случаев,
                 когда извлечение памяти намусорило что-то откровенно НЕВЕРНОЕ — не
                 дубль, а галлюцинация — dedup сам такое не ловит, он про повторы, не
                 про достоверность. /forget в самом боте чистит ВСЮ память юзера
                 разом, точечного удаления там нет.

Фильтры (по умолчанию — вся таблица, сузьте перед первым запуском):
  --user-id ID        только этот пользователь (обязателен для --mode delete)
  --chat-id ID        только этот чат (для context=group)
  --tier long|medium  только этот tier
  --include-expired   не прятать просроченные строки (дефолт для exact/delete/llm — только живые,
                       как видит память бот)
  --limit-groups N    (только --mode llm) не более N групп за один запуск —
                       подстраховка от случайного дорогого прогона на всей таблице
  --fact "текст"       (только --mode delete) что искать
  --like               (только --mode delete) --fact как подстрока, не точное совпадение

Примеры (без --apply — всегда dry-run, только печатает что было бы сделано):
  ./dedup_memory.py --mode exact
  sudo ./dedup_memory.py --mode exact --apply

  # --mode llm на голом хосте: нужны `pip install anthropic==1.7.0` и ключ в окружении
  set -a; source .env; set +a
  ./dedup_memory.py --mode llm --model sonnet --user-id 592441
  sudo -E ./dedup_memory.py --mode llm --model sonnet --user-id 592441 --apply

  # --mode llm через контейнер — ПРОЩЕ: anthropic и ключ там уже есть, sudo не нужен
  # (процесс внутри контейнера и так root), /repo — это ~/claudushka, смонтирован туда
  docker exec claudushka python3 /repo/dedup_memory.py --mode llm --model sonnet --user-id 592441
  docker exec claudushka python3 /repo/dedup_memory.py --mode llm --model sonnet --user-id 592441 --apply

  # --mode delete — убрать конкретный неверный факт (dry-run покажет совпадения и id)
  ./dedup_memory.py --mode delete --user-id 592441 --fact "Учит сербский язык"
  sudo ./dedup_memory.py --mode delete --user-id 592441 --fact "Учит сербский язык" --apply

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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Literal

DB_PATH = Path(__file__).resolve().parent / "data" / "claudushka.db"

# Короткие имена — как в реестре MODELS в bot.py, чтобы не помнить точные API-строки.
MODEL_ALIASES = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "fable": "claude-fable-5-1",
}

# $/MTok in,out + множители кэша — дубль MODELS/LEGACY_PRICES из bot.py, менять
# синхронно (сверено 2026-10-04 по https://platform.claude.com/docs/en/about-claude/pricing).
# Кортеж: (in, out, cache_read_mult, cache_write_5m_mult, cache_write_1h_mult).
# cache_read_mult: 0.1 у большинства, Opus 5.5 и Sonnet 5.5 — 0.05, Fable 5.1 — 0.025.
PRICING = {
    "claude-haiku-5-5": (0.10, 0.50, 0.1, 1.25, 2.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.1, 1.25, 2.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.05, 1.25, 2.0),
    "claude-sonnet-5": (2.0, 10.0, 0.1, 1.25, 2.0),
    "claude-opus-5-5": (4.0, 20.0, 0.05, 1.25, 2.0),
    "claude-opus-5": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-opus-4-8": (5.0, 25.0, 0.1, 1.25, 2.0),
    "claude-fable-5-1": (10.0, 50.0, 0.025, 1.25, 2.0),
}

# Ступенчатая цена (Haiku 5.5): промпт > порога токенов тарифицируется по (in, out) верхней ступени, множители
# кэша те же. ДОПУЩЕНИЕ (в доке не уточнено, консервативное — завышает): длина промпта =
# input + запись + чтение кэша вместе. На группах в тысячи фактов вход может перевалить за 100k. Считается ПО ЗАПРОСУ.
TIERS = {"claude-haiku-5-5": (100_000, 0.50, 2.50)}

# Haiku 5.5 по умолчанию думает (adaptive) и съедает max_tokens: thinking выключаем, effort low
# (disabled допустим только на low/medium/high). Как aux_params() в bot.py; другие модели — без.
AUX_PARAMS = {"claude-haiku-5-5": {"thinking": {"type": "disabled"}, "output_config": {"effort": "low"}}}


def request_cost(model, inp, out, cw_5m, cw_1h, cr):
    """$ за один запрос по PRICING/TIERS; None — модели нет в PRICING."""
    if model not in PRICING:
        return None
    price_in, price_out, read_mult, cw5_mult, cw1h_mult = PRICING[model]
    tier = TIERS.get(model)
    if tier and inp + cw_5m + cw_1h + cr > tier[0]:
        price_in, price_out = tier[1], tier[2]
    return (
        inp / 1_000_000 * price_in
        + cw_5m / 1_000_000 * price_in * cw5_mult
        + cw_1h / 1_000_000 * price_in * cw1h_mult
        + cr / 1_000_000 * price_in * read_mult
        + out / 1_000_000 * price_out
    )


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


# Дефолт: просроченные строки не видны (тот же фильтр `expires_at IS NULL OR expires_at > now`, что у
# бота при чтении) — модель не должна платно перерабатывать мусор, который бот и так не читает.
# --include-expired (только CLI) снимает фильтр для exact/delete/llm; purge-expired удаляет их отдельно.
INCLUDE_EXPIRED = False


def _filters(user_id, chat_id, tier):
    where, params = [], []
    if not INCLUDE_EXPIRED:
        where.append("(expires_at IS NULL OR expires_at > ?)")
        params.append(int(time.time()))
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


def dedup_delete(conn, user_id=None, chat_id=None, tier=None, fact=None, like=False, apply=False):
    """Точечное удаление конкретного факта — то, чего не делает /forget (она чистит

    ВСЮ память юзера). Нужно, когда извлечение памяти намусорило что-то откровенно
    неверное (не дубль, просто неправда/галлюцинация) — dedup сам такое не ловит,
    он про повторы, не про достоверность.
    """
    if user_id is None:
        sys.exit("--mode delete требует --user-id — без него слишком легко случайно "
                 "задеть факты не того человека, если формулировка не уникальна")
    if not fact:
        sys.exit("--mode delete требует --fact \"точный текст факта\" (--like — как подстрока)")
    where, params = _filters(user_id, chat_id, tier)
    where.append("fact LIKE ?" if like else "fact = ?")
    params.append(f"%{fact}%" if like else fact)
    where_sql = "WHERE " + " AND ".join(where)
    rows = conn.execute(
        f"SELECT id, user_id, chat_id, COALESCE(tier, 'long') AS tier, fact FROM memory {where_sql}",
        params,
    ).fetchall()

    print(f"[delete] совпадений: {len(rows)}")
    for r in rows:
        print(f"  id={r['id']} chat={r['chat_id']} tier={r['tier']}: {r['fact']!r}")
    if not apply:
        print("[delete] dry-run — ничего не удалено, добавь --apply")
        return
    if rows:
        conn.executemany("DELETE FROM memory WHERE id = ?", [(r["id"],) for r in rows])
        conn.commit()
    print(f"[delete] удалено {len(rows)} строк")


SYSTEM_PROMPT = (
    "Тебе дан список фактов об одном человеке — часть из них дубли или "
    "перефразировки одного и того же. Объедини по смыслу, убери повторы, "
    "оставь МИНИМАЛЬНЫЙ набор уникальных фактов, каждый — короткая фраза "
    "как в источнике. Не выдумывай ничего нового, не добавляй факты, "
    "которых не было. Верни ТОЛЬКО JSON: {\"facts\": [\"...\", \"...\"]}"
)

# max_tokens=8192: на группах в сотни-тысячи фактов (живой случай — 1441 medium-факт в одном
# чате) даже 4096 не хватило и ответ обрывался — тот же класс проблемы, что инцидент 2026-08-03
# в extract_all_participants_memory (docs/claude/incidents.md). Обрезанная группа НЕ применяется
# (status="truncated"); если режет — сузь фильтр и прогони по частям.
MAX_OUT_TOKENS = 8192


class DedupEnvError(Exception):
    """Не хватает окружения для llm-режима (ключ/пакет). Текст — готовая подсказка пользователю."""


@dataclass(frozen=True)
class GroupKey:
    user_id: int
    context: str
    chat_id: int | None
    tier: str


@dataclass
class Group:
    """Снимок группы фактов: что именно (по id) отправили на объединение."""
    key: GroupKey
    row_ids: list[int]
    facts: list[str]
    expires_at: int | None  # максимум среди строк группы (для medium)


@dataclass
class GroupResult:
    key: GroupKey
    row_ids: list[int]
    facts_before: list[str]
    facts_after: list[str]
    expires_at: int | None
    status: Literal["ok", "truncated", "error", "empty", "manual"]
    error: str | None = None
    in_tok: int = 0
    cw_5m: int = 0
    cw_1h: int = 0
    cache_read: int = 0
    out_tok: int = 0
    cost: float | None = None
    seconds: float = 0.0
    # Ответ модели как он пришёл (до правок человека); у manual пусто. facts_after — итог после правок.
    facts_model: list[str] = field(default_factory=list)

    @property
    def edited(self) -> bool:
        """Человек менял итог: у ok — отличается от ответа модели, у manual — от текущих фактов."""
        if self.status == "ok":
            return self.facts_after != self.facts_model
        return self.status == "manual" and self.facts_after != self.facts_before

    @property
    def applicable(self) -> bool:
        # Пустое «стало» неприменимо (удалить все факты группы можно только --mode delete);
        # manual без изменений тоже нечего применять.
        if self.status == "ok":
            return bool(self.facts_after)
        return self.status == "manual" and bool(self.facts_after) and self.facts_after != self.facts_before


@dataclass
class ApplyReport:
    applied: list[GroupKey] = field(default_factory=list)
    skipped: list[tuple[GroupKey, str]] = field(default_factory=list)
    backup_path: Path | None = None
    facts_before: int = 0
    facts_after: int = 0


def make_client():
    """anthropic-клиент или DedupEnvError. sys.exit — только в CLI-обёртке."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise DedupEnvError(
            "ANTHROPIC_API_KEY не задан в окружении — source .env (и sudo -E, если apply), "
            "либо запусти через docker exec claudushka — там ключ уже есть, см. --help")
    try:
        import anthropic
        import api_errors
        del api_errors  # только проверка, что общий модуль на месте
    except ModuleNotFoundError as e:
        raise DedupEnvError(
            f"Не хватает модуля ({e}) — он есть только внутри контейнера.\n"
            "Либо: pip install anthropic==1.7.0 (api_errors.py — свой файл рядом, копировать не надо)\n"
            "Либо (проще, не нужен ни pip, ни sudo — контейнер уже root и с ключом в окружении):\n"
            "  docker exec claudushka python3 /repo/dedup_memory.py --mode llm ...\n"
            "  docker exec claudushka python3 /repo/dedup_memory.py --mode llm ... --apply")
    # base_url явно — та же причина, что в bot.py/whatsapp.py: не полагаемся на неявное
    # чтение ANTHROPIC_BASE_URL внутри SDK.
    return anthropic.Anthropic(api_key=api_key, base_url=os.environ.get("ANTHROPIC_BASE_URL") or None)


def load_groups(conn, user_id=None, chat_id=None, tier=None, min_facts=2) -> list[Group]:
    """Группы (user_id, context, chat_id, tier) с min_facts+ фактами, в порядке таблицы."""
    where, params = _filters(user_id, chat_id, tier)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT id, user_id, context, chat_id, fact, COALESCE(tier, 'long') AS tier, expires_at "
        f"FROM memory {where_sql} ORDER BY user_id, context, chat_id, tier",
        params,
    ).fetchall()
    by_key: dict[GroupKey, list] = {}
    for r in rows:
        by_key.setdefault(GroupKey(r["user_id"], r["context"], r["chat_id"], r["tier"]), []).append(r)
    return [
        Group(k, [r["id"] for r in v], [r["fact"] for r in v],
              min((r["expires_at"] for r in v if r["expires_at"]), default=None))
        for k, v in by_key.items() if len(v) >= min_facts
    ]


def _usage_numbers(resp):
    # Этот ключ/пул автоматически кеширует большие входы (prompt caching) — для уникального,
    # ни разу не повторяющегося входа (каждая группа фактов своя) это ВСЕГДА запись в кэш,
    # никогда чтение: input_tokens почти пустой, реальный вес — в cache_creation_input_tokens.
    # Проверено живьём 2026-08-31: 400 фактов (40690 симв.) -> input_tokens=2,
    # cache_creation_input_tokens=16186; без учёта оценка занижалась в ~100 раз.
    usage = getattr(resp, "usage", None)
    in_tok = getattr(usage, "input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    # Разбивка записи по TTL (как в bot.py _track_response); нет cache_creation — всё 5m.
    cc = getattr(usage, "cache_creation", None)
    if cc is not None:
        cw_1h = getattr(cc, "ephemeral_1h_input_tokens", 0) or 0
        cw_5m = getattr(cc, "ephemeral_5m_input_tokens", 0) or 0
    else:
        cw_5m, cw_1h = cache_write, 0
    out_tok = getattr(usage, "output_tokens", 0) or 0
    return in_tok, cw_5m, cw_1h, cache_read, out_tok


def run_llm(groups, model, client, stop_event=None) -> Iterator[GroupResult]:
    """Прогон: читает снимок (groups), зовёт API, НИЧЕГО не пишет и не печатает.

    stop_event проверяется между группами; идущий HTTP-запрос не прерывается.
    """
    import api_errors  # общий с bot.py/whatsapp.py: response_text / parse_json_lenient / was_truncated
    for g in groups:
        if stop_event is not None and stop_event.is_set():
            return
        k = g.key
        t0 = time.monotonic()
        res = GroupResult(k, g.row_ids, g.facts, list(g.facts), g.expires_at, "error")
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=MAX_OUT_TOKENS,
                **AUX_PARAMS.get(model, {}),
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(g.facts, ensure_ascii=False)}],
            )
            # response_text/parse_json_lenient, не голый content[0].text/find+rfind: у пятого
            # поколения content[0] может быть thinking-блоком без текста, а обрезанный по
            # max_tokens JSON рвёт наивный find/rfind (docs/claude/api-errors.md).
            res.in_tok, res.cw_5m, res.cw_1h, res.cache_read, res.out_tok = _usage_numbers(resp)
            res.cost = request_cost(model, res.in_tok, res.out_tok, res.cw_5m, res.cw_1h, res.cache_read)
            text = api_errors.response_text(resp)
            data = api_errors.parse_json_lenient(text, "{", label=f"dedup user={k.user_id} chat={k.chat_id}")
            merged = [f.strip() for f in (data or {}).get("facts", []) if isinstance(f, str) and f.strip()]
            if api_errors.was_truncated(resp):
                # Обрезанный ответ = частичный список; применить его значило бы удалить ВСЮ
                # группу и вставить огрызок (тихая потеря данных). Не применяется никогда.
                res.status = "truncated"
            elif not merged:
                res.status = "empty"
            else:
                res.status, res.facts_after, res.facts_model = "ok", merged, list(merged)
        except Exception as e:
            res.status, res.error = "error", f"{type(e).__name__}: {e}"
        res.seconds = time.monotonic() - t0
        yield res


def apply_results(conn_rw, results: list[GroupResult]) -> ApplyReport:
    """Применение готовых результатов (без повторного вызова модели).

    Каждая группа — отдельная транзакция. Если хоть одной строки из row_ids уже нет
    (бот удалил/просрочил после снимка) — группа пропускается целиком. Удаляем строго по id,
    факты, добавленные ботом после снимка, не трогаются.
    """
    rep = ApplyReport()
    for r in results:
        if not r.applicable:
            why = ("пусто после правки (удалить все факты группы — только --mode delete)" if r.status == "ok" and r.facts_model
                   else "без изменений" if r.status == "manual" and r.facts_after else f"статус {r.status}")
            rep.skipped.append((r.key, why))
            continue
        k = r.key
        try:
            conn_rw.execute("BEGIN IMMEDIATE")
            ph = ",".join("?" * len(r.row_ids))
            have = conn_rw.execute(f"SELECT COUNT(*) FROM memory WHERE id IN ({ph})", r.row_ids).fetchone()[0]
            if have != len(r.row_ids):
                conn_rw.rollback()
                rep.skipped.append((k, f"в БД осталось {have} из {len(r.row_ids)} строк снимка"))
                continue
            now = int(time.time())
            # Срок слитых фактов = МИНИМУМ живых expires_at строк группы: слитый факт не живёт дольше
            # самого «старого» из живых источников и не воскрешает просроченное (раньше был максимум —
            # один свежий факт возвращал боту слитые остатки просроченных). Не знаем, из каких строк
            # получился каждый слитый факт, поэтому консервативно. Без срока (long) — NULL, как было.
            exps = [e for (e,) in conn_rw.execute(f"SELECT expires_at FROM memory WHERE id IN ({ph})", r.row_ids)]
            timed = [e for e in exps if e is not None]
            if timed and None not in exps and all(e <= now for e in timed):
                conn_rw.rollback()
                rep.skipped.append((k, "всё просрочено"))
                continue
            live = [e for e in timed if e > now]
            expires_at = min(live) if live else None
            conn_rw.execute(f"DELETE FROM memory WHERE id IN ({ph})", r.row_ids)
            conn_rw.executemany(
                "INSERT INTO memory (user_id, context, chat_id, fact, tier, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(k.user_id, k.context, k.chat_id, f, k.tier, now, expires_at) for f in r.facts_after],
            )
            conn_rw.commit()
        except Exception as e:
            conn_rw.rollback()
            rep.skipped.append((k, f"ошибка записи: {type(e).__name__}: {e}"))
            continue
        rep.applied.append(k)
        rep.facts_before += len(r.facts_before)
        rep.facts_after += len(r.facts_after)
    return rep


# ---------------------------------------------------------------- оценка стоимости (без API)

# Единственный живой замер (docs/claude/open-tasks.md): 40690 симв. -> 16186 токенов (~2.51).
# Одна точка — в UI помечается как «грубо». out/in по фактам реального замера нет: 1.0 = верхняя
# граница («ничего не слилось»).
CHARS_PER_TOKEN = 2.5
OUT_RATIO = 1.0
DATA_DIR = DB_PATH.parent
CALIBRATION_PATH = DATA_DIR / "dedup_calibration.json"
RUNS_DIR = DATA_DIR / "dedup_runs"
BACKUPS_DIR = DATA_DIR / "backups"


@dataclass
class Estimate:
    in_tok: float
    out_tok: float
    cost: float | None      # None — модели нет в PRICING
    seconds: float | None   # None — нет калибровки модели
    risk: bool              # ответ, вероятно, обрежется по MAX_OUT_TOKENS


def load_calibration(path: Path | None = None) -> dict:
    try:
        return json.loads((path or CALIBRATION_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def facts_chars(facts) -> int:
    return len(json.dumps(facts, ensure_ascii=False))


def estimate_group(facts, model, calib: dict | None = None) -> Estimate:
    """Оценка одной группы. calib — запись load_calibration()[model] или None (дефолты)."""
    calib = calib or {}
    cpt = calib.get("chars_per_token") or CHARS_PER_TOKEN
    ratio = calib.get("out_ratio") or OUT_RATIO
    fch = facts_chars(facts)
    facts_tok = fch / cpt
    in_tok = (len(SYSTEM_PROMPT) + fch) / cpt
    want_out = facts_tok * ratio
    out_tok = min(MAX_OUT_TOKENS, want_out)
    # Вход всегда уникален -> пул кеширует автоматически -> тарифицируется как запись 5m.
    cost = request_cost(model, 0, out_tok, in_tok, 0, 0)
    sec_per_1k = calib.get("sec_per_1k_out")
    seconds = out_tok / 1000 * sec_per_1k if sec_per_1k else None
    return Estimate(in_tok, out_tok, cost, seconds, want_out > MAX_OUT_TOKENS)


def estimate_total(groups_facts, model, calib: dict | None = None) -> Estimate:
    ests = [estimate_group(f, model, calib) for f in groups_facts]
    costs = [e.cost for e in ests]
    secs = [e.seconds for e in ests]
    return Estimate(
        sum(e.in_tok for e in ests), sum(e.out_tok for e in ests),
        None if any(c is None for c in costs) else sum(costs),
        None if (not secs or any(s is None for s in secs)) else sum(secs),
        any(e.risk for e in ests),
    )


def calibration_source(model, calibs: dict) -> str:
    c = calibs.get(model)
    if not c:
        return "без калибровки — грубо"
    short = next((a for a, m in MODEL_ALIASES.items() if m == model), model)
    return f"калибровка {short} от {c.get('date', '?')} ({c.get('groups', '?')} гр.)"


def _derive_calibration(e: dict) -> dict:
    """Коэффициенты из сумм-аккумуляторов (взвешены по объёму, без выдуманных констант)."""
    if e.get("sum_in_tok"):
        e["chars_per_token"] = round(e["sum_chars"] / e["sum_in_tok"], 3)
    if e.get("sum_out_ok") and e.get("sum_facts_chars_ok") and e.get("chars_per_token"):
        e["out_ratio"] = round(e["sum_out_ok"] / (e["sum_facts_chars_ok"] / e["chars_per_token"]), 3)
        e["sec_per_1k_out"] = round(e["sum_sec_ok"] / (e["sum_out_ok"] / 1000), 2)
    return e


def update_calibration(model, results: list[GroupResult], path: Path | None = None) -> dict | None:
    """Прибавляет реальный прогон к накопленным суммам модели; None — нечего считать.

    Старый формат записи (только готовые коэффициенты, без sum_*) при первом обновлении
    заменяется новым — коэффициенты без сумм взвесить нельзя.
    """
    done = [r for r in results if r.status in ("ok", "truncated") and (r.in_tok + r.cw_5m + r.cw_1h + r.cache_read)]
    if not done:
        return None
    oks = [r for r in done if r.status == "ok" and r.out_tok]
    p = path or CALIBRATION_PATH
    data = load_calibration(p)
    old = data.get(model) or {}
    if "sum_in_tok" not in old:
        old = {}
    e = {
        "sum_chars": old.get("sum_chars", 0) + sum(len(SYSTEM_PROMPT) + facts_chars(r.facts_before) for r in done),
        "sum_in_tok": old.get("sum_in_tok", 0) + sum(r.in_tok + r.cw_5m + r.cw_1h + r.cache_read for r in done),
        "sum_facts_chars_ok": old.get("sum_facts_chars_ok", 0) + sum(facts_chars(r.facts_before) for r in oks),
        "sum_out_ok": old.get("sum_out_ok", 0) + sum(r.out_tok for r in oks),
        "sum_sec_ok": round(old.get("sum_sec_ok", 0) + sum(r.seconds for r in oks), 2),
        "groups": old.get("groups", 0) + len(done),
        "date": time.strftime("%Y-%m-%d"),
    }
    entry = _derive_calibration(e)
    data[model] = entry
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return None
    return entry


def reset_calibration(model, path: Path | None = None) -> bool:
    """Удалить калибровку модели (оценки вернутся к дефолтам)."""
    p = path or CALIBRATION_PATH
    data = load_calibration(p)
    if model not in data:
        return False
    del data[model]
    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- бэкап, журнал прогонов, применение

class BackupError(Exception):
    pass


def backup_db(dest_dir: Path | None = None) -> Path:
    """Консистентная копия боевой БД (sqlite3 backup API — корректно с WAL). Ошибка = не применять."""
    dest_dir = dest_dir or BACKUPS_DIR
    path = dest_dir / f"claudushka-dedup-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = dst = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(DB_PATH))
        dst = sqlite3.connect(str(path))
        src.backup(dst)
    except Exception as e:
        raise BackupError(f"бэкап не создался ({type(e).__name__}: {e})") from e
    finally:
        for c in (dst, src):
            if c is not None:
                c.close()
    return path


def save_run(model, results: list[GroupResult], runs_dir: Path | None = None, suffix: str = "") -> Path | None:
    """Аудит-журнал прогона (в т.ч. непримененного) -> data/dedup_runs/<ts>-<model>[-suffix].json.

    На группу: facts_before, facts_model (ответ модели), facts_after (итог после правок человека),
    edited и status (manual — ручная правка без модели): видно, что правил человек, а что модель.
    """
    runs_dir = runs_dir or RUNS_DIR
    path = runs_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{model}{suffix}.json"
    payload = {
        "model": model, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "groups": [{
            "key": asdict(r.key), "row_ids": r.row_ids, "status": r.status, "error": r.error,
            "facts_before": r.facts_before, "facts_model": r.facts_model, "facts_after": r.facts_after,
            "edited": r.edited,
            "tokens": {"in": r.in_tok, "cw_5m": r.cw_5m, "cw_1h": r.cw_1h,
                       "cache_read": r.cache_read, "out": r.out_tok},
            "cost": r.cost, "seconds": round(r.seconds, 2),
        } for r in results],
    }
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        return None
    return path


def finish_run(model, results: list[GroupResult]):
    """После любого реального прогона: журнал + калибровка. -> (путь журнала, запись калибровки)."""
    if not results:
        return None, None
    return save_run(model, results), update_calibration(model, results)


def apply_reviewed(results: list[GroupResult]) -> ApplyReport:
    """Бэкап -> боевая БД на запись -> apply_results. Бэкап не создался -> BackupError, ничего не пишем."""
    backup = backup_db()
    conn, _ = get_conn(writable=True)
    try:
        rep = apply_results(conn, results)
    finally:
        conn.close()
    rep.backup_path = backup
    return rep


def load_directory(conn) -> tuple[dict, dict]:
    """({chat_id: name}, {user_id: 'Имя (@username)'}) — только для показа в TUI."""
    chats, users = {}, {}
    try:
        for r in conn.execute("SELECT chat_id, name FROM allowed_chats"):
            if r["name"]:
                chats[r["chat_id"]] = r["name"]
        for r in conn.execute("SELECT telegram_id, username, full_name FROM users"):
            name = r["full_name"] or ""
            if r["username"]:
                name = f"{name} (@{r['username']})".strip()
            if name:
                users[r["telegram_id"]] = name
    except sqlite3.Error:
        pass
    return chats, users


def count_expired(conn) -> int:
    """Сколько просроченных строк (бот их не читает, dedup их не видит)."""
    return conn.execute("SELECT COUNT(*) FROM memory WHERE expires_at IS NOT NULL AND expires_at <= ?",
                        (int(time.time()),)).fetchone()[0]


def purge_expired(conn_rw) -> int:
    """Удалить просроченные строки одной транзакцией; -> число удалённых."""
    conn_rw.execute("BEGIN IMMEDIATE")
    try:
        n = conn_rw.execute("DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at <= ?",
                            (int(time.time()),)).rowcount
        conn_rw.commit()
    except Exception:
        conn_rw.rollback()
        raise
    return n


def purge_expired_reviewed() -> tuple[int, Path]:
    """Бэкап -> боевая БД -> purge_expired. Бэкап не создался -> BackupError, ничего не удалено."""
    backup = backup_db()
    conn, _ = get_conn(writable=True)
    try:
        return purge_expired(conn), backup
    finally:
        conn.close()


def make_manual_result(g: Group) -> GroupResult:
    """Ручная правка без модели: «было» = текущие факты снимка, «стало» = их копия. Токены/деньги нулевые."""
    return GroupResult(g.key, list(g.row_ids), list(g.facts), list(g.facts), g.expires_at, "manual",
                       cost=0.0)


def fmt_dur(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def dedup_llm(conn, user_id=None, chat_id=None, tier=None, apply=False,
              model="claude-haiku-5-5", limit_groups=None, verbose=False):
    """CLI-обёртка над run_llm: печатает прогресс, при apply пишет каждую группу сразу."""
    try:
        client = make_client()
    except DedupEnvError as e:
        sys.exit(str(e))

    all_groups = load_groups(conn, user_id, chat_id, tier, min_facts=1)
    candidates = [g for g in all_groups if len(g.facts) >= 2]
    total_candidate_groups = len(candidates)  # до обрезки --limit-groups — нужно для экстраполяции ниже
    print(f"[llm] групп (user,context,chat,tier) в фильтре: {len(all_groups)}, "
          f"с 2+ фактами: {total_candidate_groups}", flush=True)
    if limit_groups:
        candidates = candidates[:limit_groups]
        print(f"[llm] ограничено --limit-groups до {len(candidates)} групп", flush=True)

    n = len(candidates)
    if apply and n:
        # Бэкап ДО первого платного вызова: не создался — не тратим деньги и ничего не пишем.
        try:
            print(f"[llm] бэкап БД: {backup_db()}", flush=True)
        except BackupError as e:
            sys.exit(f"[llm] {e} — прогон отменён")
    results: list[GroupResult] = []
    total_before = total_after = 0
    totals = dict(in_tok=0, cw=0, cr=0, out=0)
    total_cost = 0.0
    truncated_groups = errors = 0
    started = time.monotonic()
    for i, res in enumerate(run_llm(candidates, model, client), 1):
        results.append(res)
        k = res.key
        tag = f"[{i}/{n}] user={k.user_id} chat={k.chat_id} tier={k.tier}"
        total_before += len(res.facts_before)
        total_after += len(res.facts_after) if res.applicable else len(res.facts_before)
        totals["in_tok"] += res.in_tok
        totals["cw"] += res.cw_5m + res.cw_1h
        totals["cr"] += res.cache_read
        totals["out"] += res.out_tok
        total_cost += res.cost or 0.0
        cost_s = f"${res.cost:.4f}" if res.cost is not None else "$?"
        if res.status == "ok":
            print(f"{tag}: {len(res.facts_before)} -> {len(res.facts_after)} ok {cost_s} {res.seconds:.1f}с", flush=True)
        elif res.status == "truncated":
            truncated_groups += 1
            print(f"{tag}: {len(res.facts_before)} -> ОБРЕЗАНО по max_tokens, НЕ применяется {cost_s} "
                  f"{res.seconds:.1f}с", flush=True)
        elif res.status == "empty":
            print(f"{tag}: {len(res.facts_before)} -> пустой/неразобранный ответ, пропуск {cost_s}", flush=True)
        else:
            errors += 1
            print(f"{tag}: ошибка ({res.error}), пропуск", flush=True)
        if verbose:
            print(f"[llm][токены] in={res.in_tok} cache_write={res.cw_5m + res.cw_1h} "
                  f"cache_read={res.cache_read} out={res.out_tok}", flush=True)
            if res.status == "ok":
                print("[llm][было]", flush=True)
                for f in res.facts_before:
                    print(f"    - {f}", flush=True)
                print("[llm][стало]", flush=True)
                for f in res.facts_after:
                    print(f"    + {f}", flush=True)
        if apply:
            rep = apply_results(conn, [res])
            for sk, why in rep.skipped:
                if res.status == "ok":  # прочие статусы уже объяснены выше
                    print(f"[llm]   не применено: {why}", flush=True)
        elapsed = time.monotonic() - started
        eta = elapsed / i * (n - i)
        print(f"[llm]   прошло {fmt_dur(elapsed)} · ETA {fmt_dur(eta)} · потрачено ${total_cost:.4f}", flush=True)

    if truncated_groups:
        print(f"[llm] обрезано, не применено: {truncated_groups} групп — сузь фильтр "
              f"(--user-id/--chat-id/--tier) и прогони по частям", flush=True)
    if errors:
        print(f"[llm] ошибок: {errors} групп (не применены)", flush=True)
    elapsed = time.monotonic() - started
    cost = total_cost if model in PRICING else None
    cost_str = f"${cost:.4f}" if cost is not None else "? (модель не в PRICING — сверься с /cost в боте)"
    per_group = elapsed / n if n else 0

    suffix = "" if apply else " (dry-run — ничего не записано, добавь --apply)"
    print(f"[llm] итого по обработанным группам: {total_before} -> {total_after}{suffix}", flush=True)
    print(f"[llm] время: {elapsed:.1f}с на {n} групп (~{per_group:.1f}с/группу)", flush=True)
    print(f"[llm] токены: in={totals['in_tok']} cache_write={totals['cw']} "
          f"cache_read={totals['cr']} out={totals['out']}, оценка стоимости: {cost_str}", flush=True)

    run_path, calib = finish_run(model, results)
    if run_path:
        print(f"[llm] журнал прогона: {run_path}", flush=True)
    if calib:
        print(f"[llm] калибровка {model} обновлена: {calib}", flush=True)

    # Экстраполяция на ВЕСЬ фильтр (после пробного --limit-groups N, прежде чем снимать лимит):
    # грубая, по СРЕДНЕМУ на группу — реальные группы сильно различаются по размеру (от 2 до 1441).
    if limit_groups and n and n < total_candidate_groups and cost is not None:
        factor = total_candidate_groups / n
        print(f"[llm] грубая экстраполяция на все {total_candidate_groups} групп в фильтре "
              f"(± сильно, группы разного размера): ~{elapsed * factor / 60:.1f} мин, "
              f"~${cost * factor:.2f}", flush=True)
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["exact", "llm", "delete", "purge-expired"], default=None,
                   help="без --mode (или с --tui) открывается интерактивный TUI")
    p.add_argument("--tui", action="store_true", help="интерактивный curses-интерфейс (нужен docker exec -it)")
    p.add_argument("--user-id", type=int, default=None)
    p.add_argument("--chat-id", type=int, default=None)
    p.add_argument("--tier", choices=["long", "medium"], default=None)
    p.add_argument("--limit-groups", type=int, default=None, help="только для --mode llm")
    p.add_argument("--model", default="haiku",
                    help="только для --mode llm — короткое имя (haiku/sonnet/opus/fable, "
                         "как в bot.py) или полная API-строка")
    p.add_argument("--fact", default=None,
                    help="только для --mode delete — текст факта на удаление (точное "
                         "совпадение, если не передан --like)")
    p.add_argument("--like", action="store_true",
                    help="только для --mode delete — --fact ищется как подстрока (LIKE %%...%%), "
                         "не точным совпадением")
    p.add_argument("--include-expired", action="store_true",
                   help="не прятать просроченные строки (по умолчанию exact/delete/llm видят только живые)")
    p.add_argument("--apply", action="store_true", help="реально писать в БД (иначе dry-run)")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="показать сами факты (exact: какой дубль какому id соответствует; "
                         "llm: полный список 'было'/'стало' по каждой группе), не только счётчики")
    args = p.parse_args()
    args.model = MODEL_ALIASES.get(args.model, args.model)
    global INCLUDE_EXPIRED
    INCLUDE_EXPIRED = args.include_expired

    if not DB_PATH.exists():
        sys.exit(f"Не нашла БД: {DB_PATH}")

    if args.tui or args.mode is None:
        import dedup_tui  # только в TUI-режиме: curses не нужен CLI
        sys.exit(dedup_tui.run(args.model))

    conn, tmp_path = get_conn(writable=args.apply)
    if tmp_path:
        print(f"[dry-run] читаю снимок {tmp_path} (боевой файл не трогаю, sudo не нужен)")
    try:
        if args.mode == "purge-expired":
            n = count_expired(conn)
            print(f"[purge-expired] просроченных строк (expires_at <= now): {n} — бот их не читает")
            if not args.apply:
                print("[purge-expired] dry-run — ничего не удалено, добавь --apply (сделает бэкап)")
            elif n:
                try:
                    print(f"[purge-expired] бэкап БД: {backup_db()}")
                except BackupError as e:
                    sys.exit(f"[purge-expired] {e} — ничего не удалено")
                print(f"[purge-expired] удалено {purge_expired(conn)} строк")
        elif args.mode == "exact":
            dedup_exact(conn, args.user_id, args.chat_id, args.tier, args.apply, args.verbose)
        elif args.mode == "delete":
            dedup_delete(conn, args.user_id, args.chat_id, args.tier, args.fact, args.like, args.apply)
        else:
            dedup_llm(conn, args.user_id, args.chat_id, args.tier, args.apply,
                      args.model, args.limit_groups, args.verbose)
    finally:
        conn.close()
        if tmp_path:
            os.remove(tmp_path)


if __name__ == "__main__":
    main()

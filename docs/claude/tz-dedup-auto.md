# ТЗ: автоматическая еженедельная дедупликация памяти (`feat/dedup-auto`)

Ветка: `feat/dedup-auto`. Версия: **v2.19.0** (minor: новая фоновая задача и команда). Стенд — обязателен
(`docs/claude/staging-checklist-dedup-auto.md`): задача сама пишет в `memory`, меняется `docker-compose.yml`.
Работать в WSL: `~/projects/claudushka`, команды Linux (`python3`). Перед началом — `git pull`.

Сначала прочитай: `CLAUDE.md` (Definition of Done, Инварианты), `docs/claude/open-tasks.md` → «Гигиена памяти»
(как устроен `dedup_memory.py`), `docs/claude/memory.md`, `.claude/rules/db.md`, `.claude/rules/docker-compose.md`,
и как сделан образец фоновой задачи с отчётом админу — `/memory_backfill` (`_backfill_worker` в bot.py,
раздел «Первичная обработка личных тредов» в `docs/claude/memory.md`).

## Зачем

Факты памяти копятся перефразировками («Любит котов» / «Обожает котов» / «У него кот»). В промпт идут только
12 long + 8 medium фактов на человека (`MEMORY_LONG_PER_USER` / `MEMORY_MEDIUM_PER_USER`, db.py) — дубли вытесняют
полезное. Сейчас дедуп запускается руками (`dedup_memory.py`, TUI). Алексей: Gemini 3.8 Flash справляется хорошо,
недорого и сжимает сильно — пусть бот делает это сам раз в неделю, но только там, где появилось новое.

## Данные прода (2026-11-19, только чтение)

- Живых фактов 5806 (381k знаков): long 4029, medium 1777. Групп (`user_id, context, chat_id, tier`) — 277, из них
  с 10+ фактами — 151, с 50+ — 32, самая большая — 131 (medium, группа).
- За 7 дней новые факты появились в 151 группе, 5+ новых — в 94; за 28 дней 5+ новых — в 196. Без новых фактов
  за 28 дней — 10 групп.
- Отличить «уже чищено» по `created_at` нельзя: в 134 группах есть пачки по 5+ фактов с одинаковым `created_at` —
  так вставляет и дедуп (`apply_results`), и групповое извлечение памяти. Поэтому нужна своя таблица отметок.
- Цена: весь живой объём ≈ 150k токенов входа; полный проход Flash — порядка $0.5 (оценка по прайсу, не замер).

## Решения (предложены Claude Code; Алексей может поменять до старта — тогда правится этот раздел)

| Что | Значение по умолчанию | Константа |
|---|---|---|
| Когда | раз в неделю, понедельник 05:07 Berlin (после чистки памяти 04:30) | `DEDUP_AUTO_WEEKDAY`, `DEDUP_AUTO_TIME` |
| Модель | Gemini 3.8 Flash пула (`MODELS["flash"]["id"]`), фолбэк бота — Flash у Google | `DEDUP_AUTO_MODEL_ID` |
| Какие группы | обе tier (long и medium), только живые факты (как сейчас `load_groups`) | — |
| Порог размера | группа из 10+ живых фактов | `DEDUP_AUTO_MIN_FACTS = 10` |
| «Есть новое» | новых фактов с прошлой проверки ≥ max(5, 20% от фактов после прошлой проверки) | `DEDUP_AUTO_MIN_NEW = 5`, `DEDUP_AUTO_NEW_RATIO = 0.2` |
| Бюджет | не больше $2 за прогон; остаток — в следующий раз | `DEDUP_AUTO_MAX_USD = 2.0` |
| Страховка от «съела всё» | если после < 10% от «было» — группу НЕ применять, показать в отчёте | `DEDUP_AUTO_MIN_KEEP = 0.1` |
| Кто платит | личный чат админа бота (как `/memory_backfill`), метка `memory_dedup` | — |
| Режим после выкатки | `estimate` (без вызовов модели, только отчёт «что и почём»), включает `apply` Алексей командой | `data/dedup_auto.json` |

## Как отслеживать «есть новое» — таблица `memory_dedup_state`

```sql
CREATE TABLE IF NOT EXISTS memory_dedup_state (
    user_id INTEGER NOT NULL, context TEXT NOT NULL, chat_key INTEGER NOT NULL,  -- chat_key = COALESCE(memory.chat_id, 0)
    tier TEXT NOT NULL,
    checked_at INTEGER NOT NULL,      -- время проверки (после записи результата)
    facts_before INTEGER NOT NULL, facts_after INTEGER NOT NULL,
    status TEXT NOT NULL,             -- applied | unchanged | guard | truncated | empty
    cost_usd REAL,
    PRIMARY KEY (user_id, context, chat_key, tier)
);
```

- Создание — в `init_db` (db.py, правила `.claude/rules/db.md`); функции чтения/записи — в db.py.
- **Группа к проверке** (`due`): живых фактов ≥ `DEDUP_AUTO_MIN_FACTS` И (строки в state нет ИЛИ
  `COUNT(created_at > checked_at)` ≥ max(`DEDUP_AUTO_MIN_NEW`, ceil(`DEDUP_AUTO_NEW_RATIO` × facts_after))).
- После записи слитых фактов (`apply_results` ставит им `created_at = now`) `checked_at` берётся НЕ раньше этого `now`
  — иначе слитые факты сами посчитаются «новыми» и группа будет гоняться каждую неделю. Тест на это обязателен.
- Строку state пишем и тогда, когда применять нечего (`unchanged` — модель не сжала; `guard`; `truncated`; `empty`) —
  иначе такая группа будет платно перепроверяться каждую неделю. НЕ пишем при `error` (сеть/провайдер) — пусть
  попробует в следующий раз.
- Порядок прогона — по числу новых фактов, по убыванию; бюджет кончился — остальные ждут следующей недели
  (state для них не трогаем).
- `/forget` и `clear_memory` удаляют факты человека — строки state этого человека тоже удалить (там же, где чистится
  профиль; инвариант «любое место, удаляющее память человека, чистит и профиль» — сюда же).

## Что сделать

### 1. Ядро — переиспользовать `dedup_memory.py`, не копировать
- Бот импортирует `dedup_memory` (`load_groups`, `run_llm`, `apply_results`, `backup_db`, `estimate_group`, `save_run`).
  **`docker-compose.yml`: добавить `./dedup_memory.py:/app/dedup_memory.py` в сервис `claudushka`** (и в
  `claudushka-wa` — по правилу «новый импортируемый .py — в оба сервиса», хотя WA его не импортирует; см.
  `.claude/rules/docker-compose.md`). Выкатка — только `docker compose up -d --force-recreate claudushka`, не `/update`.
- Вызов модели — через `sync_create` бота (учёт в `usage_log`, фолбэк бота Flash пула → Flash Google). В `run_llm`
  передать «клиента»-прокладку, у которого `messages.create(**kw)` = `sync_create(label="memory_dedup", **kw)`.
- **ВАЖНО: собственный фолбэк дедупа (`create_with_fallback` → `_native_create` → `import bot`) в процессе бота
  вызываться НЕ должен** — `import bot` внутри работающего бота (он запущен как `__main__`) выполнит bot.py второй раз.
  Добавить в `run_llm`/`create_with_fallback` явный выключатель (например `state={"no_fallback": True}`), передавать
  его из бота; тест: при ошибке прокладки `_native_create` не вызывается.
- Всё синхронное (SQLite, `run_llm`) — в `asyncio.to_thread` (инвариант «никаких блокирующих вызовов из event loop»);
  `usage_ctx` (личка админа) поставить ДО `to_thread` — контекст копируется в поток.
- Применение — существующий `apply_results` (своя транзакция на группу, проверка, что строки снимка на месте; факты,
  добавленные ботом во время прогона, не трогаются). Перед первым платным вызовом — `backup_db()`; не создался —
  прогон отменить, отчёт админу. Старые копии удаляет `purge_old_db_backups` (v2.15.0) — проверить, что имя копии дедупа
  подходит под его шаблон.
- Страховка `DEDUP_AUTO_MIN_KEEP`: результат `ok`, но `len(after) < MIN_KEEP × len(before)` → не применять, state =
  `guard`, в отчёт. Обрезанные (`truncated`) — не применяются уже сейчас.
- Журнал прогона — `save_run` (как у CLI), калибровку — `update_calibration`.

### 2. Задача и режимы
- `app.job_queue.run_daily(..., days=(1,))` — понедельник (у PTB 22.8 `0` = воскресенье, см. соседний вызов
  `send_profile_digest`), время по `BERLIN_TZ`.
- Режим — в `data/dedup_auto.json` (`{"mode": "off|estimate|apply", "last": {...отчёт...}}`), как
  `data/memory_backfill.json`. Нет файла → `estimate`.
- `estimate`: список `due`-групп и оценка цены по `estimate_group` (без вызова модели, бесплатно), state НЕ пишется.
- `apply`: прогон как выше.
- Два прогона одновременно не допускать (флаг/замок; ручной запуск во время недельного — «уже идёт»).

### 3. Команда админа бота `/dedup_auto` (только админ бота, только личка)
- `/dedup_auto` — режим, когда следующий прогон, итог прошлого.
- `/dedup_auto estimate` — посчитать сейчас: сколько групп к проверке, фактов, оценка $ (без модели).
- `/dedup_auto run` — прогнать сейчас в режиме `apply` (подтверждение кнопкой: показать оценку и бюджет).
- `/dedup_auto on` / `off` / `estimate-only` — переключить режим недельной задачи.
- `ADMIN_HELP` — одна строка; `USER_HELP` не трогать.

### 4. Отчёт админу (личка, одно сообщение; длинное — `split_lines_for_telegram`)
Режим; групп к проверке / обработано / пропущено по бюджету; фактов было → стало; применено / без изменений /
страховка (список групп: user_id, чат, tier, было→стало) / обрезано / ошибки; потрачено $ (из `usage_log` по
метке за прогон или сумма `cost` результатов); имя бэкапа и журнала прогона.

### 5. Документация и справка (Definition of Done)
- `docs/claude/memory.md` — раздел «Автоматическая дедупликация» (что, когда, пороги, таблица, режимы, команда).
- `docs/claude/open-tasks.md` → «Гигиена памяти» — ссылка на раздел.
- `.claude/rules/db.md` — новая таблица.
- `CLAUDE.md`: индекс («Память» — `/dedup_auto`, `memory_dedup_state`, `DEDUP_AUTO_*`); инвариант про служебные вызовы —
  **третье сознательное исключение «не Luna»: `DEDUP_AUTO_MODEL_ID` = Flash, label `memory_dedup`** (там перечислены два);
  инвариант про удаление памяти — state тоже; «Текущая версия» → v2.19.0.
- README — если есть раздел про память для админа.

### 6. Тесты (`scripts/test_dedup_auto.py`, без сети, модель — фейк)
Отбор `due` (порог размера, порог новых, первый раз, после применения слитые не «новые»); state пишется для
applied/unchanged/guard/truncated/empty и не пишется при error; бюджет останавливает и не трогает state остальных;
страховка `MIN_KEEP`; режим `estimate` не зовёт модель и не пишет; в процессе бота `_native_create` не вызывается;
`/forget`/`clear_memory` чистят state. Плюс весь набор:
`TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts -q` (известное старое падение —
`test_miniapp_costs::test_costs_200_bot_admin_sees_free`, сверить с `main`).

## Чего НЕ делать
- Не писать свой вызов модели и свою логику слияния — только `run_llm`/`apply_results` из `dedup_memory.py`.
- Не трогать TUI и CLI дедупа (кроме выключателя фолбэка), не менять `SYSTEM_PROMPT` дедупа.
- Не дедуплицировать профили (`user_profile`) — только `memory`.
- Не трогать сервер и прод, не ставить тег, не мержить в `main`. Push — только `feat/dedup-auto`.

## Проверки перед сдачей
```bash
for f in bot.py db.py dedup_memory.py; do python3 -c "import ast; ast.parse(open('$f').read())"; done
python3 -c "import ast,collections as c; t=ast.parse(open('bot.py').read()); print([k for k,v in c.Counter(n.name for n in t.body if hasattr(n,'name')).items() if v>1])"   # []
TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts -q
git diff --stat
```

## Отчёт Алексею (на русском)
Что сделано; какие значения порогов в коде; результаты тестов; версия v2.19.0 и команда
`git tag -a v2.19.0 -m "v2.19.0: автоматическая еженедельная дедупликация памяти (/dedup_auto)"`; что не сделано /
не проверено; влияние на контекст — косвенное и в плюс (в промпт уходят те же 12+8 фактов, но разные), лимиты не меняются;
выкатка — `docker compose up -d --force-recreate claudushka` (новый mount), не `/update`.

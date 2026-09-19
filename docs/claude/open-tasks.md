# Открытые задачи (не «известные баги», а запланированное)

- **Fable 5 → Fable 5.1 (`claude-fable-5-1`), ждём пробный запрос Алексея (ТЗ-3, Блок A,
  2026-09-19).** Официальный прайс Anthropic уже перечисляет Fable 5.1 (та же цена
  $10/$50, но `cache_read_mult` 0.025 вместо 0.1 — единственная модель с таким
  множителем), Fable 5 помечена как legacy (retirement не раньше 1.09.2027 — доступна,
  просто не рекомендуется для новых интеграций). Не проверено — принимает ли пул
  `api.apitoken.sale` строку `claude-fable-5-1`. Пробная команда (ключ не попадает в
  историю shell — используется переменная окружения, не литерал):
  ```bash
  cd ~/claudushka && set -a && source .env && set +a
  for m in claude-fable-5-1 claude-sonnet-5; do
    curl -s -o /tmp/probe_$m.json -w "$m: HTTP %{http_code}\n" \
      "${ANTHROPIC_BASE_URL:-https://api.anthropic.com}/v1/messages" \
      -H "x-api-key: $ANTHROPIC_API_KEY" \
      -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" \
      -d "{\"model\": \"$m\", \"max_tokens\": 1, \"messages\": [{\"role\": \"user\", \"content\": \"hi\"}]}"
  done
  cat /tmp/probe_claude-*.json; rm /tmp/probe_claude-*.json
  ```
  `claude-sonnet-5` в этой же команде — сверка для `WA_MODEL` в whatsapp.py (ТЗ-3, Блок A
  п.4): bot.py им уже пользуется в проде для `/sonnet`, ожидаемо 200, код уже переведён
  на него не дожидаясь (риск минимальный) — но если пул вдруг вернёт не 200, дай знать,
  откатим `WA_MODEL` обратно на `claude-sonnet-4-6`.

  `fable-5-1: 200` → пул принял. Дальше (не сделано, ждёт этого ответа):
  1. `bot.py` `MODELS["fable"]`: `"id": "claude-fable-5-1"`, `"label": "Fable 5.1"`,
     `"cache_read_mult": 0.025`.
  2. `db.py`, список идемпотентных миграций (рядом с `UPDATE chat_models SET
     model='claude-sonnet-5' WHERE model LIKE 'claude-sonnet-4-%'`, см.
     `.claude/rules/db.md`): добавить `UPDATE chat_models SET model='claude-fable-5-1'
     WHERE model='claude-fable-5'`. Без миграции чаты на `claude-fable-5` попадут в
     `model_meta()` фолбэк на дефолт (Haiku) — `/cost` посчитает их по ценам Haiku, а
     не Fable, занижая расход в разы.
  3. `dedup_memory.py`: `MODEL_ALIASES["fable"]` и `PRICING["claude-fable-5-1"]`
     синхронно с `MODELS` (см. комментарий «дубль MODELS из bot.py»).

  Если пул НЕ принял (не 200) — ничего не менять, эта запись остаётся как есть до
  следующей проверки.

- **Гигиена памяти**: дедуп фактов (`UNIQUE(user_id, context, chat_id, fact)` +
  `INSERT OR IGNORE`; индекс не создастся, пока в таблице есть дубликаты) и разовая
  чистка просроченных (`DELETE FROM memory WHERE expires_at IS NOT NULL AND expires_at <= now`,
  ~380k символов балласта). На размер промпта не влияет — просроченные и так фильтруются,
  а с v0.11.3–0.11.5 все три функции чтения памяти (`get_memory`, `get_memory_for_private`,
  `get_all_chat_memory`) капаются по `MEMORY_LONG_PER_USER`/`MEMORY_MEDIUM_PER_USER`, так
  что даже необузданный дубликат-мусор в таблице (2026-08-31: "Поделился фото" 13 раз,
  740 фактов на человека в одном чате) больше не раздувает system-prompt. Дедуп теперь
  вопрос КАЧЕСТВА 20 отдаваемых фактов (чтобы это были 20 РАЗНЫХ фактов, а не 13 вариаций
  одного и того же, вытесняющих из-под потолка что-то более информативное), не корректности.
  Подробнее про память — `docs/claude/memory.md`.
  **Инструмент для разовой ручной чистки — `dedup_memory.py`** (v0.11.6+, корень репо,
  НЕ часть бота, никем не импортируется): `--mode exact` убирает буквальные повторы без
  API, `--mode llm` просит модель объединить близкие по смыслу формулировки (стоит
  денег, один вызов на группу `user_id+context+chat_id+tier`, `max_tokens=8192` — на
  группах в сотни-тысячи фактов меньше не хватает, см. живой инцидент ниже). Фильтры
  `--user-id`/`--chat-id`/`--tier`, `--limit-groups N` (только llm — пробный прогон на
  части перед безлимитным), `-v`/`--verbose` (показывает сами факты — exact: кто чей
  дубль, llm: полный список «было»/«стало» на группу, не только счётчики — важно перед
  `--apply`, чтобы свериться, что модель ничего не потеряла/не выдумала). Дефолт —
  dry-run: читает со снимка во временном файле, sudo НЕ требует. `--apply` пишет в
  боевой файл напрямую — `data/` на сервере root:root, нужен `sudo` (для llm — `sudo -E`,
  прокинуть `ANTHROPIC_API_KEY`). `--model` принимает короткие имена
  (`haiku`/`sonnet`/`opus`/`fable`, реестр `MODEL_ALIASES` дублирует `MODELS` из bot.py,
  см. `docs/claude/models-and-costs.md`) или полную API-строку. По завершении llm-режима
  печатает время, токены (in/cache_write/cache_read/out по отдельности) и оценку
  стоимости по локальной табличке `PRICING` (тоже дубль `MODELS`, сверять при смене цен
  там) — а если был `--limit-groups`, ещё и грубую экстраполяцию на весь фильтр. Живой
  масштаб на 2026-08-31: 85k фактов в таблице, 264 группы с 2+ фактами; пробный прогон 5
  групп Sonnet'ом — 312с (~62с/группу), экстраполяция на все 264 группы — грубо ~4.5 часа.

  **У этого ключа/пула включено автоматическое prompt caching** — не только для
  системных промптов, для ЛЮБОГО достаточно большого входа. Для дедупа вход ВСЕГДА
  уникален (разные факты в каждой группе), значит это ВСЕГДА запись в кэш, никогда
  чтение — `usage.input_tokens` при этом почти пустой (единицы токенов), а реальный
  вес в `cache_creation_input_tokens`. Без учёта этого оценка стоимости занижалась
  примерно в 100 раз (проверено: 400 фактов, 40690 символов -> `input_tokens=2`,
  `cache_creation_input_tokens=16186`). `CACHE_WRITE_MULTIPLIER`/`CACHE_READ_MULTIPLIER`
  (1.25x/0.1x, стандартные множители Anthropic для 5-минутного ephemeral-кэша) это
  учитывают. Стоит держать в уме и за пределами этого скрипта — если где-то ещё в
  проекте появится любой достаточно большой одноразовый payload, `/cost` может так же
  недосчитывать, если сам не смотрит на `cache_creation_input_tokens` (см.
  `docs/claude/models-and-costs.md`).

  **Проще всего гонять `--mode llm` через `docker exec claudushka python3 /repo/...`**
  (`/repo` — примонтированный туда же корень репо) — внутри контейнера уже есть пакет
  `anthropic` (на голом хосте его нет, `pip install anthropic==0.43.0` либо так) и
  `ANTHROPIC_API_KEY` в окружении (не надо `source .env`), и процесс и так root
  (не надо `sudo`/`sudo -E`). `--help` в самом скрипте — там же все примеры.

  Живой прогон вскрыл и починил на месте: наивный `"".join(b.text for b in
  resp.content if hasattr(b, "text"))` падал на `NoneType` (thinking-блок пятого
  поколения без текста) — теперь `api_errors.response_text`/`parse_json_lenient`, те
  же хелперы, что у bot.py/whatsapp.py, не переизобретены заново (см.
  `docs/claude/api-errors.md`).
- **`chat_models` постоянна**: `/haiku` `/sonnet` `/opus` `/fable` пишут выбор навсегда,
  отката по таймеру нет. Решение об автооткате не принято; текущая модель видна в `/models`.
- **GPT через прокси**: другой API, нужен отдельный клиент и поле `provider` в реестре
  `MODELS`. Решение не принято.
- **whatsapp.py вне реестра**: свой хардкод `claude-sonnet-4-6`. Синхронизировать —
  только вынося `MODELS` в общий модуль (как сделали с `api_errors.py`).

# Модели per-chat (реестр MODELS, v0.9.0)

Перед правкой `MODELS`, `_set_chat_model`, `/haiku /sonnet /opus /fable`, `/models`,
`_track_response`, `calc_llm_cost`, `/cost` — читай этот файл. С ТЗ v0.10 учёт, тарифы
(free → только Haiku), `/cost` и подтверждение `/fable` описаны в `docs/claude/billing.md`.

**`MODELS` в bot.py — единственный источник правды.** Команды, цены, гейтинг по ролям,
окна контекста, `/models` — всё оттуда. Строки моделей по месту больше не писать.

| ключ | id | label | $/MTok in/out | cache read | запись 5m / 1h | окно | кому |
|---|---|---|---|---|---|---|---|
| `haiku` | `claude-haiku-4-5-20251001` | Haiku 4.5 | 1 / 5 | 0.1× | 1.25× / 2× | 200k | всем (free-режим — только она) |
| `sonnet` | `claude-sonnet-5` | Sonnet 5 | 2 / 10 | 0.1× | 1.25× / 2× | 1M | админ ИЛИ платный чат (дефолт платного) |
| `opus` | `claude-opus-5-5` | Opus 5.5 | 4 / 20 | **0.05×** | 1.25× / 2× | 1M | админ ИЛИ платный чат |
| `fable` | `claude-fable-5-1` | Fable 5.1 | 10 / 50 | **0.025×** | 1.25× / 2× | 1M | админ ИЛИ платный чат, с подтверждением |

**`LEGACY_PRICES`** (bot.py) — модели с ценой, но БЕЗ выбора пользователем: `claude-opus-5`
(прошлый `/opus`, 5 / 25, 0.1× — для подписи старых строк `usage_log` в `/cost`) и
`claude-opus-4-8` (5 / 25, 0.1× — фолбэк safeguards Opus 5.5, см. «Учёт токенов»).
`price_meta(id)` ищет в `MODELS` + `LEGACY_PRICES` (для цены и подписи), `model_meta(id)` — только
в `MODELS` (для выбора/гейтинга). Точная строка Opus 4.8 взята из доков Anthropic; если в логах
всплывёт warning про неизвестную модель — добавить фактическую строку из `response.model`.

Поле `admin_only` из реестра убрано (ТЗ v0.10): гейтинг — «админ ИЛИ paid», кроме Haiku
(`DEFAULT_MODEL_KEY`) — см. `_set_chat_model`. Эффективная модель чата — `get_chat_model(chat_id)`:
free → Haiku, paid → `chat_models` или Sonnet (`PAID_DEFAULT_MODEL_KEY`). `db.get_chat_model_db(chat_id,
default)` теперь отдаёт только сохранённый выбор, без своего дефолта.

Пул `api.apitoken.sale` принимает эти строки — проверено живым запросом 29.07.2026
(плюс старые `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-opus-4-7`). Цены — официальный
прайс Anthropic, прокси даёт скидку сверху; **сверено 2026-09-19** по
[models/overview](https://platform.claude.com/docs/en/about-claude/models/overview) и
[pricing](https://platform.claude.com/docs/en/about-claude/pricing).
**Sonnet 5: цена $2/$10 постоянная** — доки Anthropic прямо говорят, что запланированное
повышение до $3/$15 (1.09.2026) отменено. Старый комментарий в реестре («после 31.08
станет верным само») был ошибкой — цена и так стоит правильно, `/cost` больше не завышает.

`cache_read_mult`, `cache_write_5m_mult`, `cache_write_1h_mult` — поля модели (с 2026-09-23 глобальных
`CACHE_READ_MULTIPLIER`/`CACHE_WRITE_MULTIPLIER` больше нет). Чтение из кэша: 0.1× у Haiku/Sonnet/Opus 5,
0.05× у Opus 5.5, 0.025× у Fable 5.1. Модель без явного поля — `DEFAULT_CACHE_MULTS` (0.1 / 1.25 / 2.0).

**Opus 5 → Opus 5.5 (`claude-opus-5-5`) — 2026-09-23, ТЗ `feat/opus-5-5`.** Стенд пройден 2026-09-23 по
`docs/claude/staging-checklist-opus-5-5.md`: пул принимает `claude-opus-5-5`, `response.model`
совпадает с таблицей цен (warning'ов «нет в таблице цен» не было), миграция и `/cost` — ок. `MODELS["opus"]`,
`/opus`, `DAILY_REVIEW_MODEL_ID` (берётся из `MODELS["opus"]`), `_probe_model` (получает id из
реестра), справка — на 5.5. Миграция `chat_models` — в `init_db()` (точное сравнение
`WHERE model='claude-opus-5'`, не `LIKE`). Особенности 5.5, которые касаются бота:
thinking выключить НЕЛЬЗЯ (`{type: "disabled"}` и `budget_tokens` → 400) — в коде параметра
`thinking` нет нигде, это ок; дефолтный effort у 5.5 — `medium` (у Opus 5 был `high`), бот
effort не задаёт; forced `tool_choice` (`any`/`tool`) → 400 — в коде не используется; thinking
тратит `max_tokens`, текст ответа извлекается только `response_text()` — перебором блоков.

**`max_tokens` на модели чата — только через `out_tokens(model, visible)`** = бюджет на видимый
ответ + `MODELS[...]["thinking_headroom"]` (Haiku 0, Sonnet/Opus/Fable 8000). Найдено на стенде
2026-09-23: `/review` с `max_tokens=500` на Opus 5.5 → `stop_reason=max_tokens blocks=['thinking']`,
весь лимит ушёл на thinking, текста ноль. Касается всех вызовов на `get_chat_model` /
`DAILY_REVIEW_MODEL_ID` (диалог, фото, документ, `/search`, `/review`, дневной обзор). Служебные
вызовы на Haiku — голые числа, Haiku без явного `thinking` не думает. Запас сам не стоит денег,
длину видимого текста держит промпт + `trim_to_last_sentence`.

**Fable 5 → Fable 5.1 (`claude-fable-5-1`) — сделано 2026-09-19.** Пул `api.apitoken.sale`
подтверждён живым запросом (200 на `claude-fable-5-1`, `claude-sonnet-5` в той же
проверке — тоже 200). `MODELS["fable"]` теперь на `claude-fable-5-1`, миграция
`chat_models` — в `init_db()` (idempotent `UPDATE ... WHERE model='claude-fable-5'`),
`dedup_memory.py` (`MODEL_ALIASES`/`PRICING`) синхронизирован.

- `model_meta(model_id)` → метаданные по API-строке; неизвестная модель → дефолт,
  без исключения (в `chat_models` могли остаться строки прошлых поколений).
  В `/cost` такая модель помечается «нет в реестре» — иначе цифра выглядела бы точной.
- `db.get_chat_model_db / set_chat_model_db`, обёртка `get_chat_model(chat_id)` в bot.py.
- **Окна различаются: Haiku 200k, пятое поколение 1M. Лимиты памяти/истории калиброваны
  под МИНИМАЛЬНОЕ (200k) — не поднимать их, ссылаясь на 1M.** Дефолт остаётся Haiku;
  поднять потолок значит вернуть июльский инцидент всем, кто сидит на `/haiku`.
  См. «Потолок на чтении» в `docs/claude/memory.md`.
- **Служебные вызовы — ТОЛЬКО `DEFAULT_MODEL_ID` (Haiku), без исключений** (правило
  Алексея, 2026-09-19): капча, `should_search`, перевод промпта рисования,
  `greet_new_member`, извлечение памяти (и `extract_memory` в личке, и
  `extract_all_participants_memory` в группе — раньше `extract_memory` в личке шёл на
  Sonnet «осознанно, аналитическая задача»; это решение отменено, теперь единообразно
  Haiku, срабатывает каждые 10 сообщений и не должно платить по цене Sonnet/Opus).
  `cmd_review` — это ОТВЕТ пользователю (уходит в чат), не служебный вызов, поэтому модель
  чата (`get_chat_model`). `daily_chat_review` с ТЗ v0.10 — ОСОЗНАННОЕ исключение: всегда Opus
  (`DAILY_REVIEW_MODEL_ID`), платная фича paid-групп (`docs/claude/billing.md`). Не хардкодить
  `MODELS["..."]["id"]` в новых вызовах — служебный вызов берёт `DEFAULT_MODEL_ID`,
  ответ пользователю — `get_chat_model(chat_id)`. Исключение — `_probe_model`: он по
  своей природе обязан тестировать ИМЕННО ту модель, на которую переключаются
  (`/haiku /sonnet /opus /fable`), это не подпадает под классификацию «служебный/ответ».
- whatsapp.py собственного реестра `MODELS` не имеет, но своих моделей теперь две
  константы: `WA_MODEL = "claude-sonnet-5"` (ответы пользователю, было
  `claude-sonnet-4-6` — переведено 2026-09-19, тот же ID, что и `/sonnet` в bot.py) и
  `WA_AUX_MODEL = "claude-haiku-4-5-20251001"` (служебные `should_search`/
  `extract_memory`, тот же принцип «служебное — только Haiku»). Появится третья точка
  с похожей логикой — тогда осмысленно выносить `MODELS` в общий модуль.

## Команды и гейтинг
Все четыре — через общий `_set_chat_model(update, context, key)`, где `key` — ключ
реестра, а не API-строка.
- Гейтинг: не-Haiku — админу или платному чату, иначе отказ «доступно в платном режиме»
  с ценой, а не молчанием (раньше — `admin_only` в реестре). `/fable` в paid просит повтор
  в течение 2 минут (`_fable_pending`), см. `docs/claude/billing.md`.
- **Без аргумента → текущий чат, доступно всем.** С аргументом `/opus -1001109809707` →
  указанный чат, **ТОЛЬКО АДМИНУ**: иначе любой участник менял бы модель в чужом чате,
  зная только ID. Раньше аргумент читался лишь из лички и вся команда была admin-only.
- Аргумент валидируется: не парсится в int → подсказка по формату; чата нет в
  `allowed_chats` → предупреждение, но запись разрешена (чат мог ещё не попасть в таблицу).
- В ответе всегда имя + ID чата, чтобы админ не переключил не тот.
- **Перед записью — пробный запрос** (`_probe_model`, `max_tokens=1`, «hi») через
  `call_claude`. Не прошёл → «модель сейчас недоступна», в БД НЕ пишем. 529 при этом
  ретраится и не выглядит как «модели не существует» (для 404 отдельная фраза).
- `/models [chat_id]` — список с ценами и окном, `▸` = эффективная модель чата, режим
  (платный/бесплатный), пометка «(платный режим)» у недоступных в free.

## Учёт токенов
С ТЗ v0.10 учёт пишется в таблицу `usage_log` (не в память) — подробности схемы, `billed`,
`usage_ctx`, `label` — `docs/claude/billing.md`. Здесь — то, что относится к самой формуле цены.

`_track_response(model, response, label)` вызывается ВНУТРИ обёрток `call_claude()` и
`sync_create()`, а НЕ по месту — новый вызов API попадает в учёт автоматически, ручной
учёт по месту вернёт двойной счёт. Стоимость — `calc_llm_cost(model, inp, out, cache_write_5m,
cache_write_1h, cache_read)` (без наценки; `PRICE_MARKUP` применяет `_record_usage`).

**Цена — по фактической модели (`response.model`), не по запрошенной** (`_billed_model`): Opus 5.5
может прозрачно отдать запрос другой модели при срабатывании safeguards. В `usage_log.model`
пишется та же фактическая. Модели из ответа нет в `price_meta` → цена по запрошенной +
`logger.warning` с обеими строками (один раз на пару за процесс, `_unknown_model_warned`).

**Запись в кэш — раздельно по TTL:** `usage.cache_creation.ephemeral_5m_input_tokens` ×
`cache_write_5m_mult`, `ephemeral_1h_input_tokens` × `cache_write_1h_mult`. Нет `cache_creation` в
ответе — весь `cache_creation_input_tokens` считается как 5m. В `usage_log.cache_write` по-прежнему
пишется сумма (разбивка не хранится — цена уже заморожена в `cost_usd`).

**`usage_log.thinking`** (с 2026-09-23) — `usage.output_tokens_details.thinking_tokens`, ВХОДИТ в
`output` (не прибавлять к нему), только для видимости доли thinking. `/cost <chat_id>` показывает
«(из них thinking N)». Старые строки — 0.

**Кэш обязателен в формуле (найдено и починено в v0.11.7):** пул автоматически кеширует любой
достаточно большой вход. `usage.input_tokens` при этом почти пустой (единицы токенов), а реальный
вес — в `cache_creation_input_tokens`/`cache_read_input_tokens`; порядок занижения без них
подтверждён живьём в `dedup_memory.py`: 40690 символов входа → `input_tokens=2`,
`cache_creation_input_tokens=16186` — порядки, не проценты. Множители — поля модели (см. таблицу
выше), дефолт — `DEFAULT_CACHE_MULTS`.
До ТЗ v0.10 счётчик жил в памяти и сбрасывался рестартом (`token_usage`, `_track_tokens` — удалены);
старые цифры не восстановить, история копится с момента деплоя. Подробнее про кеширование —
`docs/claude/open-tasks.md` (раздел про `dedup_memory.py`).

## Миграция chat_models
В `init_db()`, идемпотентная, вместе с остальными:
`UPDATE chat_models SET model='claude-sonnet-5' WHERE model LIKE 'claude-sonnet-4-%'`
и то же для `claude-opus-4-%` → `claude-opus-5-5` (с 2026-09-23 сразу на 5.5). Плюс
`UPDATE chat_models SET model='claude-opus-5-5' WHERE model='claude-opus-5'` (точное сравнение).
`chat_models` — единственное место выбора модели, оно же хранит «прошлый платный выбор» для
возврата из free (free-режим таблицу не трогает). `usage_log` НЕ мигрируется: это история,
`cost_usd` заморожен при вставке, старые строки Opus 5 остаются по $5/$25. Haiku не трогается.

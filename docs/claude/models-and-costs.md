# Модели per-chat (реестр MODELS, v0.9.0)

Перед правкой `MODELS`, `_set_chat_model`, `/haiku /sonnet /opus /fable`, `/models`,
`_track_response`, `calc_llm_cost`, `/cost` — читай этот файл. С ТЗ v0.10 учёт, тарифы
(free → только Haiku), `/cost` и подтверждение `/fable` описаны в `docs/claude/billing.md`.

**`MODELS` в bot.py — единственный источник правды.** Команды, цены, гейтинг по ролям,
окна контекста, `/models` — всё оттуда. Строки моделей по месту больше не писать.

| ключ | id | label | $/MTok in/out | cache read | запись 5m / 1h | окно | кому |
|---|---|---|---|---|---|---|---|
| `haiku` | `claude-haiku-5-5` | Haiku 5.5 | **0.10 / 0.50** (промпт >100k: 0.50 / 2.50) | 0.1× | 1.25× / 2× | 1M | всем (free-режим — только она) |
| `sonnet` | `claude-sonnet-5-5` | Sonnet 5.5 | 2 / 10 | **0.05×** | 1.25× / 2× | 1M | админ ИЛИ платный чат (дефолт платного) |
| `opus` | `claude-opus-5-5` | Opus 5.5 | 4 / 20 | **0.05×** | 1.25× / 2× | 1M | админ ИЛИ платный чат |
| `fable` | `claude-fable-5-1` | Fable 5.1 | 10 / 50 | **0.025×** | 1.25× / 2× | 1M | админ ИЛИ платный чат, с подтверждением |

**`LEGACY_PRICES`** (bot.py) — модели с ценой, но БЕЗ выбора пользователем: `claude-haiku-4-5-20251001`
(прошлый `/haiku`, 1 / 5, 0.1×), `claude-sonnet-5`
(прошлый `/sonnet`, 2 / 10, 0.1×), `claude-opus-5`
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
**Sonnet 5 / 5.5: цена $2/$10 постоянная** — доки Anthropic прямо говорят, что запланированное
повышение до $3/$15 (1.09.2026) отменено. Старый комментарий в реестре («после 31.08
станет верным само») был ошибкой — цена и так стоит правильно, `/cost` больше не завышает.

`cache_read_mult`, `cache_write_5m_mult`, `cache_write_1h_mult` — поля модели (с 2026-09-23 глобальных
`CACHE_READ_MULTIPLIER`/`CACHE_WRITE_MULTIPLIER` больше нет). Чтение из кэша: 0.1× у Haiku 5.5/Haiku 4.5/Sonnet 5/Opus 5,
0.05× у Opus 5.5 и Sonnet 5.5 (до 2026-10-09 у Sonnet 5.5 по ошибке стояло 0.1× — `/cost` завышал чтение кэша вдвое; старые строки `usage_log` остаются как есть), 0.025× у Fable 5.1. Модель без явного поля — `DEFAULT_CACHE_MULTS` (0.1 / 1.25 / 2.0).

**Prompt caching (v1.1.4)** — как собирается префикс диалога и почему `cache_read` был 0: `docs/claude/prompt-cache.md`.

**Haiku 4.5 → Haiku 5.5 (`claude-haiku-5-5`) — 2026-10-09, ТЗ `feat/haiku-5-5`.** Дефолтная/free-модель и
ВСЕ служебные вызовы. Сверено по докам Anthropic (overview, migration-guide, prompting-claude-haiku-5-5, pricing)
2026-10-09; пул принимает `claude-haiku-5-5` — проверено curl на сервере 2026-10-09 (`end_turn`, `text`, thinking_tokens=0); `anthropic==1.7.0` принимает `thinking=` и `output_config=` именованными аргументами (проверено
mock-транспортом в venv, тело запроса содержит оба). Что важно:
- **Thinking по умолчанию включён** (adaptive, effort `medium`) и тратит `max_tokens`. Служебные вызовы имеют лимиты
  30–512 → без правки ответ был бы пуст. Решение — два хелпера в bot.py, оба смотрят на флаг `"effort": True` в
  записи `MODELS` (у других моделей возвращают `{}`; НЕ через `model_meta` — тот фолбэчит на дефолт и выдал бы чужой
  модели параметры Haiku):
  - `aux_params()` — `thinking={"type":"disabled"}` + `output_config={"effort":"low"}` на ВСЕХ служебных вызовах
    (should_search, rewrite, translate, media_describe, captcha×2, extract_memory, extract_chat_memory, greet_filter,
    greet) и в `_probe_model`. `disabled` допустим только на low/medium/high (на xhigh/max — 400).
  - `dialog_params(model)` — `thinking={"type":"adaptive"}` + `effort=HAIKU_DIALOG_EFFORT` (env, дефолт `low`;
    допустимы low/medium/high, иначе warning и `low`) на ответах пользователю: диалог (+аварийная обрезка), фото, файл,
    `/search`, `/review`. `thinking_headroom` Haiku = 4000 (стартовое значение, уточнить по `usage_log.thinking` на стенде).
  - Риск из гайда: на `low` рассуждения могут протекать в видимый текст → `HAIKU_DIALOG_EFFORT=medium` в `.env` (нужен
    `docker compose up -d --force-recreate`, `restart` env не перечитывает), без правки кода.
- **Ступенчатая цена.** Промпт >100 000 токенов — in 0.50 / out 2.50 (×5), множители кэша те же. По доке pricing
  («Long context pricing») длина промпта = ВЕСЬ вход: `input + cache write + cache read`; каждый запрос считается
  отдельно. **Это ДОПУЩЕНИЕ, не подтверждённый факт:** дока явно говорит только «prompts over 100,000 tokens cost more»;
  фразу про учёт cache read/write содержал WebFetch-вывод страницы pricing, но ручная перепроверка её не нашла.
  Формула консервативна (при ошибке `/cost` списывает больше, не меньше); юнит-тест проверяет только её, не расчёт
  Anthropic. Сверить с реальным счётом при первом входе >100k. В записи
  модели: `tier_threshold/tier_in/tier_out`, учтено в `calc_llm_cost`; `_track_response` пишет `warning`, если вход
  Haiku >100k. Лимиты памяти/истории НЕ поднимали — прежняя причина (инцидент 2026-07-26) + ×5 выше 100k.
- Токенизатор даёт ~+30% токенов на тот же текст; изображения — тарифицируются в более высоком разрешении (до ~2.5× токенов
  на крупном фото; касается `media_describe`). Экономия ожидается ~7–8×, а не 10× (проверить по `usage_log`).
- Отказы: у Haiku 5.5 safety-классификаторы без fallback (`stop_reason="refusal"`), повтор даёт тот же отказ. Диалог
  отвечает пользователю (`bot.py` «Отказалась отвечать…»); служебные вызовы трактуют пустой текст как «нечего делать»
  (should_search → нет поиска, память → ничего не пишем, rewrite → без повтора). Закрыто попутно: `draw_translate` при
  пустом ответе рисует по исходному тексту, `captcha_gen` — запасной вопрос.
- Миграция `chat_models`: `claude-haiku-4-5-20251001` → `claude-haiku-5-5` (точное сравнение, идемпотентна, проверена на
  временной БД). Именно UPDATE, а не DELETE: запись Haiku в платном чате — осознанный `/haiku`, удаление молча перевело бы
  чат на дорогой Sonnet. `_mig_defaults` (`billing_defaults`) — одноразовая, не трогали. SQL-дефолт колонки — 5.5.
- `WA_AUX_MODEL` (whatsapp.py), `MODEL_ALIASES`/`PRICING`/`TIERS`/`AUX_PARAMS` (dedup_memory.py) синхронизированы;
  в dedup цена теперь считается по запросу (ступень зависит от каждого запроса, а не от суммы). Диалог WhatsApp идёт на
  Sonnet (`WA_MODEL`), ему параметры Haiku не нужны.
- Откат: вернуть запись `MODELS["haiku"]` на `claude-haiku-4-5-20251001` (цены 1/5, окно 200k, headroom 0, убрать `effort` и
  `tier_*`) — хелперы вернут `{}`, параметры уйдут сами. Миграцию `chat_models` назад придётся делать руками.

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

**Sonnet 5 → Sonnet 5.5 (`claude-sonnet-5-5`) — 2026-10-04, ТЗ `feat/sonnet-5-5`**, по образцу Opus.
Пул принимает строку (проверено). Цена $2/$10, кэш 0.1× / 1.25× / 2× — как у Sonnet 5 (сверено с pricing на
2026-10-04). `MODELS["sonnet"]`, `WA_MODEL` (whatsapp.py), `MODEL_ALIASES`/`PRICING` в `dedup_memory.py` — на 5.5;
`claude-sonnet-5` остался в `LEGACY_PRICES` и `PRICING` для старых строк `usage_log`. Миграция `chat_models` —
точное `WHERE model='claude-sonnet-5'` (идемпотентна, проверена на временной БД); `claude-sonnet-4-%` теперь
мигрируют сразу в 5.5. Таблицы цен в bot.py и dedup_memory.py дублируются — сверены построчно, в общий
модуль не выносили.

**`max_tokens` на модели чата — только через `out_tokens(model, visible)`** = бюджет на видимый
ответ + `MODELS[...]["thinking_headroom"]` (Haiku 5.5 — 4000 для диалога, Sonnet/Opus/Fable 8000; Sonnet 5.5 — тот же запас, что у 5). Найдено на стенде
2026-09-23: `/review` с `max_tokens=500` на Opus 5.5 → `stop_reason=max_tokens blocks=['thinking']`,
весь лимит ушёл на thinking, текста ноль. Касается всех вызовов на `get_chat_model` /
`DAILY_REVIEW_MODEL_ID` (диалог, фото, документ, `/search`, `/review`, дневной обзор). Служебные
вызовы на Haiku — голые числа, но с `**aux_params()` (thinking выключен). Запас сам не стоит денег,
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
- **Окна: у Haiku 5.5 и пятого поколения 1M, но лимиты памяти/истории калиброваны под 200k —
  не поднимать их, ссылаясь на 1M.** Дефолт остаётся Haiku; поднять потолок значит вернуть июльский
  инцидент всем на `/haiku`, а у Haiku 5.5 вход >100k ещё и в 5 раз дороже.
  См. «Потолок на чтении» в `docs/claude/memory.md`.
- **Служебные вызовы — ТОЛЬКО `DEFAULT_MODEL_ID` (Haiku) и всегда с `**aux_params()`, без исключений** (правило
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
  константы: `WA_MODEL = "claude-sonnet-5-5"` (ответы пользователю, было
  `claude-sonnet-4-6` → `claude-sonnet-5` 2026-09-19 → 5.5 2026-10-04, тот же ID, что и `/sonnet` в bot.py) и
  `WA_AUX_MODEL = "claude-haiku-5-5"` + `WA_AUX_PARAMS` (служебные `should_search`/
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
`UPDATE chat_models SET model='claude-sonnet-5-5' WHERE model LIKE 'claude-sonnet-4-%'`
(с 2026-10-04 сразу на 5.5) и то же для `claude-opus-4-%` → `claude-opus-5-5` (с 2026-09-23 сразу на 5.5). Плюс
`UPDATE chat_models SET model='claude-opus-5-5' WHERE model='claude-opus-5'` (точное сравнение) и `... SET model='claude-sonnet-5-5' WHERE model='claude-sonnet-5'` (с 2026-10-04).
`chat_models` — единственное место выбора модели, оно же хранит «прошлый платный выбор» для
возврата из free (free-режим таблицу не трогает). `usage_log` НЕ мигрируется: это история,
`cost_usd` заморожен при вставке, старые строки Opus 5 остаются по $5/$25. Строки Haiku 4.5 в `usage_log` тоже остаются (цена 1/5 заморожена; подпись — через `LEGACY_PRICES`).
В `chat_models` Haiku 4.5 мигрируется в 5.5 (`UPDATE ... WHERE model='claude-haiku-4-5-20251001'`, с 2026-10-09).

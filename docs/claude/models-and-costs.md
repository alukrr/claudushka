# Модели per-chat (реестр MODELS; с 2026-10-28 — только Gemini, v2.0.0)

Перед правкой `MODELS`, `_set_chat_model`, `/lite /flash /argon`, `/models`, `_track_response`, `calc_llm_cost`, `/cost` —
читай этот файл. Учёт, тарифы, `/cost` — `docs/claude/billing.md`.

**`MODELS` в bot.py — единственный источник правды.** Команды, цены, гейтинг по ролям, окна контекста, `/models` — оттуда.
Строки моделей по месту больше не писать.

**С 2026-10-28 пул не отдаёт модели Anthropic** (403 `model_blocked`), Claude убран из бота (ТЗ `docs/claude/tz-no-anthropic.md`,
v2.0.0). Всё ниже про Claude (таблица Claude-моделей, Haiku 5.5, Opus 5.5, ступенчатая цена, thinking/effort) — **история**,
в коде этого нет; Claude-id живут только в `LEGACY_PRICES` для подписей старых строк `usage_log`.

| ключ | id | label | $/MTok in/out (листовая) | cache read | запись | окно | thinking_headroom | роль |
|---|---|---|---|---|---|---|---|---|
| `lite` | `gemini-3.1-flash-lite` | Gemini 3.1 Flash-Lite | 0.25 / 1.50 | 0.1× | 1.0× | 1M | 4000 | **free (единственная)**, ВСЕ служебные вызовы (`DEFAULT_MODEL_ID`), описание медиа, выжимки обзора, голос |
| `flash` | `gemini-3.8-flash` | Gemini 3.8 Flash | 0.75 / 3.75 | 0.1× | 1.0× | 1M | 4000 | дефолт платного чата (`PAID_DEFAULT_MODEL_KEY`) |
| `argon` | `gemini-4-argon` | Gemini 4 Argon | 2 / 10 | 0.05× | 1.0× | 1M | 4000 | платный выбор, дневной обзор (`DAILY_REVIEW_MODEL_ID`) |

Цены — **листовые (зачёркнутые)** со страницы пула, не со скидкой; кошельки в «виртуальных долларах» по листу. Gemini на этой
полосе кэш промпта не даёт (`cache_control` → 400): вход каждый раз по полной цене (фактически чтение кэша может быть
неявным — `cached_content`; не гарантировано). Gemini 3.8 Flash и Argon **думают всегда**, токены рассуждений входят в
`usage.output_tokens` — `out_tokens(model, visible)` добавляет запас; без него ответ обрывается.

Роли (решения Алексея 2026-10-28, остальное «предложено»): free — Lite без выбора (`FREE_MODEL_KEYS = ("lite",)`; таблица
`chat_free_model` больше не читается и не пишется — выбора нет, строки оставлены на диске); платный дефолт — Flash;
дневной обзор — Argon (нестримовый вывод ≤ 32K, обзор берёт `2200 + 4000`); `restricted` (дорогие, в группе только админам) —
сейчас ни у одной модели нет; режим разработчика — модель чата. GPT-моделей чата нет (Terra — только для картинок, Шаг 2).
`get_chat_model`: free → Lite; paid → `chat_models`, а строка не из реестра (старый Claude до миграции) → Flash.

Параметры вызова: `aux_params()` и `dialog_params()` возвращают `{}` — ни одна модель пула не принимает `thinking` /
`output_config` (Gemini → 400; у GPT влияние на `/v1/messages` не доказано). Функции остались единой точкой, чтобы
не править ~20 вызовов, если управляемая модель вернётся. Служебные вызовы обязаны оборачивать лимит в
`out_tokens(DEFAULT_MODEL_ID, N)` (иначе Lite/Flash могут обрезать ответ на рассуждениях).

Команды `/haiku /sonnet /opus /fable` — заглушки `cmd_claude_gone` (подсказка выбрать модель в `/models`), на один релиз;
подтверждение `/fable` удалено. Миграция БД `no_anthropic_models` (`db._mig_no_anthropic`, `_run_once`, один раз):
`chat_models` haiku → lite, sonnet → flash, opus/fable → argon; `chat_search_mode` claude → auto; счётчики в лог. Без пути назад.

Фолбэк — GPT другого провайдера: `docs/claude/model-fallback.md`. Цены GPT (фолбэк, поиск, Terra для картинок) в
`LEGACY_PRICES`: Luna 0.10/0.50, Terra 1.00/5.00, Sol 2.00/10.00; запись в кэш 1.25× (непроверено на `/v1/messages`).

**Дедуп** (`dedup_memory.py`, `dedup_tui.py`): `MODEL_ALIASES` = lite/flash/argon, дефолт `lite`; `PRICING` — ручная копия `MODELS`
(общий модуль `models.py` не заводили — решение за Алексеем, п. 3.2 ТЗ; расхождение ловит
`scripts/test_model_fallback.py::test_dedup_pricing_matches_bot`). GPT-моделей в дедупе нет (GPT-моделей чата нет).

---- ИСТОРИЯ (до 2026-10-28) ---

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

## Gemini как модели чата (v1.9.0, ТЗ `feat/gemini-models`)
`MODELS` содержит три записи с `"provider": "gemini"`: `/argon` = `gemini-4-argon` ($2/$10), `/flash` =
`gemini-3.8-flash` ($0.75/$3.75), `/lite` = `gemini-3.1-flash-lite` ($0.25/$1.50), окно 1M, `thinking_headroom` 4000,
без `effort`/`restricted`. `model_provider(id)` → `anthropic|gemini|openai` (флаг в записи цены, иначе префикс `gemini*`).
- Вызов идёт через ту же Anthropic-полосу (`/v1/messages`) и те же обёртки; `kwargs_for_model` чистит параметры
  (без `thinking`/`output_config`/`tools`/`cache_control`, запас на рассуждения уже в `out_tokens`). `dialog_params` и
  `aux_params` для них `{}`. Служебные вызовы и дневной обзор от выбора модели чата не зависят.
- **Явного кэша промпта нет** (`cache_control` режется — пул отвечает 400), но неявный кэш Google иногда срабатывает
  (`cache_read_input_tokens` > 0, `prompt-cache.md`): цена входа на 5–10k system — по прайсу, со скидкой на попавшую в кэш
  часть. Множитель записи 1.0 непроверен.
- **Argon прошёл Шаг 0 на стенде 2026-10-24** (`model-fallback.md`): текст, system, картинка, транскрипт, стрим, нестримовый
  `max_tokens=32000`. `check_fallback_models` на старте предупреждает, если Gemini-моделей нет в каталоге пула.
- Обратный фолбэк Gemini → Claude — `model-fallback.md`.
- **Free:** доступны Haiku и Lite (`FREE_MODEL_KEYS`). Выбор free хранится в `chat_free_model` (НЕ в `chat_models`:
  его в free не трогаем, прошлый платный выбор возвращается при пополнении); `get_chat_model` в free = запись
  `chat_free_model` (если это Haiku/Lite), иначе Haiku. Остальным моделям не-админ в free получает «доступно в платном режиме».
  Кто переключает — как в paid. Проба (`_probe_model`) перед записью — для всего, кроме Haiku. Админ, выбирающий в free-чате
  платную модель, пишет `chat_models` как раньше («включится после пополнения»).
- **Деньги free:** Lite ($0.25/$1.50) дороже Haiku 5.5 ($0.10/$0.50) в 2,5–3 раза и без кэша промпта, у Haiku кэш есть —
  реальная разница на чат больше прайсовой. Free-чаты платит Алексей; замер на стенде (10 одинаковых сообщений Haiku vs Lite)
  — в чек-листе `staging-checklist-gemini-models.md`.

## Команды и гейтинг
Все четыре (с v1.9.0 — семь: плюс `/argon /flash /lite`) — через общий `_set_chat_model(update, context, key)`, где `key` — ключ
реестра, а не API-строка.
- **`restricted` (с v1.3.0): `/opus` и `/fable` (и `/bananapro` — `docs/claude/images.md`) в ГРУППЕ переключают только
  админы бота и админы этой группы (`_deny_restricted` → `is_chat_admin`); в личке — сам пользователь платного чата;
  чужой чат по `chat_id` — только админ бота (как раньше). До этого гейтинг был только по тарифу — любой участник платной
  группы мог включить Fable. Чаты, уже стоявшие на Opus/Fable, остаются; обычный участник может уйти с них на
  `/sonnet`/`/haiku`, но вернуться не может.** `/models` показывает «(админы)» у таких строк и секцию «Генерация
  картинок» (`IMAGE_PROVIDERS` с ценами `image_price_text`).
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
  (платный/бесплатный), пометка «(платный режим)» у недоступных в free. С v1.9.0 — два блока (Claude, Gemini),
  внизу строка «Поиск: <режим> (сейчас: <провайдер>)» (`_search_status_line`), цены — «прайс провайдера».

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

**Подстраховка разбивки:** если `cache_creation.ephemeral_5m + ephemeral_1h` меньше `cache_creation_input_tokens`,
остаток считается как 5m (`_track_response`). Нужна для СТРИМА с серверным поиском: там разбивка остаётся нулевой
от `message_start` (см. `docs/claude/native-search.md`, «Грабля учёта»).

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
**После деплоя/рестарта в `/cost` видна волна `cache_read = 0` — это не поломка.** Якоря окна истории
(`_HISTORY_ANCHORS`) живут в памяти процесса: после рестарта у каждого чата один промах кэша на первой
реплике (`cache_write` ≈ вся история), дальше чтение возвращается. Подробности — `prompt-cache.md`.
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

## Замеры пула 2026-10-28 (ТЗ `docs/claude/tz-no-anthropic.md`, Шаг 0)

Прогон 1 — 28.10 ~19:20 Berlin, `scripts/bench_pool.py --images`, 3 прогона на точку, медиана. Сырой вывод —
`docs/claude/bench/2026-10-28-1921.txt`. **Один прогон; скорость пула за сутки менялась в 3–4 раза — решать после
второго прогона в другое время.** Claude: 403 `model_blocked` на все три. `gemini-3.6-flash` (голос) в каталоге НЕТ.

Полоса бота `/v1/messages` (anthropic SDK), вопрос в характере Клодушки:

| Модель | Первый токен, с | Весь ответ, с | Стрим | JSON-служебка (max_tokens=300) | Листовая цена in/out |
|---|---|---|---|---|---|
| gemini-3.1-flash-lite | 0.74 | 1.10 | да | OK, 1.0 с | 0.25 / 1.50 |
| gemini-2.5-flash-lite | 0.91 | 1.33 | да | OK, 0.9 с | 0.10 / 0.40 |
| gemini-2.5-flash | 0.91 | 1.29 | да | OK, 0.8 с | 0.30 / 2.50 |
| gemini-3-flash-preview | 2.97 | 3.13 | нет | **обрыв** (думает ~290 ток.) | 0.50 / 3.00 |
| gemini-3.8-flash | 4.99 | 5.03 | нет | **обрыв** (думает ~290 ток.) | 0.75 / 3.75 |
| gemini-4-argon | 3.43 | 5.33 | да | OK, 2.4 с | 2.00 / 10.00 |
| gpt-6-luna | 2.9 | 3.01 | нет | OK, 6.7 с | 0.10 / 0.50 |
| gpt-5.6-luna | 3.19 | 3.30 | нет | OK, 5.3 с | 0.20 / 1.20 |
| gpt-6.1-terra | 1.43 | 3.31 | да | OK, 2.2 с | 1.00 / 5.00 |
| gpt-6.1-sol | 2.87 | 5.63 | да | OK, 2.6 с | 2.00 / 10.00 |
| gpt-6-sol | 2.78 | 5.38 | да | OK, 2.7 с | 2.00 / 10.00 |
| gpt-6-astra | 2.56 | 7.01 | да | OK, 3.4 с | 10.00 / 50.00 |

- Все GPT и Gemini принимают `/v1/messages` (вариант A ТЗ). Для GPT эта полоса не медленнее `chat/completions` и
  `responses` (Terra: 3.3 / 7.8 / 3.9 с); у Gemini все три полосы ~одинаковы.
- `cache_control`: GPT — 200, Gemini — 400 (как и раньше). Вызов функции (`tools`) — все модели вызвали.
- «Обрыв» у 3.x Flash — это лимит 300 при обязательном thinking, в боте лечится запасом `thinking_headroom` (с
  2026-10-28 бенч проверяет и с запасом). Lite и 2.5 почти не думают.
- Характер (субъективно, по 1 ответу): Gemini заметно ироничнее и ближе к Клодушке, GPT суше, шутка в конце фразы.
- Картинка на входе: в прогоне 1 тестовый PNG был битым (ошибка скрипта) — **не проверено**, исправлено для прогона 2.
- Генерация: GPT Image 2 / Flare / Sunburst — 21–23 с, `usage` = 30 вход + 515 выходных image-токенов (≈ $0.015 по
  листовой $30/1M — приблизительно; в боте плоские $0.02, т.е. не занижено). Terra через `/v1/responses` +
  `image_generation` — 23 с, `quality: low`, `size: 1374x1145`, PNG; в `usage` только текст (2242/62) — цена самой
  картинки по-прежнему неизвестна.

Прогон 2 — 28.10 ~20:44 Berlin (через ~1.5 ч после первого, т.е. тот же вечер — разброс по времени суток
по-прежнему не измерен), `--lanes messages`, сырой вывод `docs/claude/bench/2026-10-28-2044.txt`:
- Время на `/v1/messages` — в пределах разброса прогона 1 (весь ответ): Lite 1.28, 2.5 Flash 1.03, 2.5 Flash-Lite
  1.30, 3-flash-preview 3.02, 3.8 Flash 3.34 (в прогоне 1 — 5.03), Argon 5.41, Terra 3.37, Sol 6.1 5.19, Sol 6 5.04,
  Astra 6.94, Luna 4.19, Luna 5.6 2.90. Стрим — как в прогоне 1.
- **Картинка на входе (64×64 PNG, красная) — все 12 моделей ответили верно.** Фото-хендлер на выбранных моделях работает.
- JSON-служебка с запасом +4000 токенов: 3-flash-preview и 3.8 Flash — OK (659 и 432 выходных токена вместе с
  рассуждениями). Без запаса — обрыв, как и ожидалось.
- Аудио (нативный `generateContent`, 1 с тишины `audio/wav`): `gemini-3.6-flash` — 404 (модели нет), Lite / 2.5
  Flash-Lite / 2.5 Flash — 200, ответ «тишина» (не галлюцинация, в отличие от 3.6-flash в 08.2026). **OGG/Opus из
  Telegram не проверялся** — бенч шлёт WAV.

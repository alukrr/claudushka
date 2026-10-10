# Нативный веб-поиск Anthropic в платном режиме (ТЗ `feat/native-search`, v1.2.0)

## Схема
- **Платный чат** (`chat_tier == "paid"`, личный чат админа — всегда paid): серверный инструмент
  `web_search` в `tools` самого диалогового запроса. Модель сама решает, искать ли. `should_search` и
  Tavily НЕ вызываются.
- **Бесплатный чат** (баланс 0): как раньше — `should_search` (Haiku) → Tavily → результаты в `service_block`.
- Фото и документ, `/search`, служебные вызовы — без изменений (tools не передаются).
- Включение: `_native_search_on` = `NATIVE_SEARCH` и paid и `_search_allowed` (дневной лимит поиска
  непроверенных действует и здесь). `NATIVE_SEARCH=0` — платный режим на Tavily, реакции остаются.

## Env и константы (bot.py, рядом с блоком prompt cache)
| Имя | Дефолт | Смысл |
|---|---|---|
| `NATIVE_SEARCH` | `1` | `0` — откат на Tavily без правки кода (нужен `docker compose up -d --force-recreate`) |
| `NATIVE_SEARCH_TOOL` | `web_search_20250305` | версия инструмента. **`web_search_20260209` пул НЕ пропускает** (см. гейт) |
| `NATIVE_SEARCH_MAX_USES` | `3` | `max_uses` в определении инструмента |
| `NATIVE_SEARCH_PRICE` (const) | `0.01` | $ за один поиск; прайс Anthropic $10/1000, **не сверено с пулом** |
| `NATIVE_SEARCH_MAX_PAUSE` (const) | `3` | сколько раз продолжать при `pause_turn` |

## Гейт: что умеет пул (2026-10-10, из контейнера стенда)
- `web_search_20250305` работает на **Sonnet 5.5, Haiku 5.5, Opus 5.5, Fable 5.1**: в `content` есть
  `server_tool_use` и `web_search_tool_result`, `usage.server_tool_use.web_search_requests = 1`, ответ актуальный.
- `web_search_20260209` (динамическая фильтрация) **не работает**: на Sonnet модель уходит в серверный
  `code_execution`, поиск «недоступен» при `web_search_requests=0`; на Haiku — `invalid_tool_input`. Пул не
  пропускает code execution. Дефолт — `20250305`.
- Пул сам кэширует эти запросы (`cache_write` 9–13k, `cache_read` ~2.8k) — это его автокэш.
- Ответ приходит несколькими text-блоками: цитата рвёт предложение посередине (блок без `citations`, блок
  с одним `url`, блок без). Склеивать **без разделителя**, URL брать со всех блоков.
- Цена поиска у пула: см. раздел «Открыто» ниже.

## Вызов (`call_dialog_native` → `call_claude_stream`)
- **Стриминг.** `client_noretry.messages.stream`, внутри потока (`_stream_final_message`); на событии
  `content_block_start` с блоком `server_tool_use` (`name == "web_search"`) через
  `loop.call_soon_threadsafe(react, ...)` ставится 👀. На стенде первое событие поиска приходит на ~1.6 с, ответ
  целиком — на 5–7 с. Дальше обычное `get_final_message()` — учёт токенов `_track_response` не меняется.
- **Ретраи** — те же `api_errors.call_with_retry` (клиент без SDK-ретраев, «печатает…» между попытками).
  Ошибка ПОСРЕДИ стрима приходит SSE-событием при HTTP 200: SDK 1.7.0 бросает обычный `APIStatusError` с
  `status_code=200` и телом `{"error": {"type": "overloaded_error"}}` (проверено). Поэтому
  `api_errors.is_retryable` и `user_message` смотрят на тип в теле (`stream_error_type`): `overloaded_error`,
  `api_error`, `rate_limit_error` ретраятся.
- **`pause_turn`.** Запрос повторяется, к `messages` добавляется assistant-контент как есть
  (`model_dump(mode="json", exclude_none=True)`, с `encrypted_content`/`signature`; круг проверен — 400 нет),
  не больше `NATIVE_SEARCH_MAX_PAUSE` раз, дальше берём что есть + warning. Каждая итерация — отдельная строка
  `usage_log` (`dialog` для первой, `dialog_pause` для продолжений).
- **Отказ инструмента.** `BadRequestError` с «web search»/«web_search» в тексте (не prompt-too-long) →
  warning и этот же запрос идёт по схеме Tavily (`_tavily_search` + `compose`). Ошибки внутри
  `web_search_tool_result_error` (`too_many_requests`, `unavailable`, `max_uses_exceeded`…) приходят с HTTP 200:
  только warning, модель продолжает сама.
- Аварийная ветка prompt-too-long повторяет запрос БЕЗ tools (обычным `call_claude`): поиск в ней теряется,
  но запрос короче.

## Ответ пользователю (`native_answer`, `sources_line`)
- **Текст** — text-блоки после последнего `web_search_tool_result`, склеенные без разделителя; «сейчас
  поищу» до поиска отбрасывается. Поиска не было — обычный `response_text`.
- **Источники** (требование доки к показу цитат): до 3 разных доменов из `citations`, одной строкой
  `🔗 reuters.com · ecb.europa.eu`, домен — текст ссылки; превью ссылок выключено
  (`LinkPreviewOptions(is_disabled=True)`). Бот шлёт `parse_mode="Markdown"` (legacy): в URL `( ) _ * \` [ ]` и пробел
  кодируются процентами (хост НЕ кодируется — `%5F` в имени хоста ломает ссылку), в тексте домена `_` экранируется.
  Не принял Telegram Markdown — фолбэк в plain: `🔗 домен · домен` без ссылок. Не влезла в 4096 вместе с ответом
  — отдельным сообщением.
- **В БД** (`conversations`/`group_messages`) — только итоговый текст: без источников и без блоков поиска.
  Поэтому `encrypted_content` в следующих репликах не нужен, токены выдачи в историю не тянутся.
- Маркер `[[DRAW: ...]]` обрабатывается по итоговому тексту, как раньше.

## Учёт денег
- `usage.server_tool_use.web_search_requests` → по строке на каждый поиск: `kind='search'`,
  `model='anthropic'` (в `/cost` — «Anthropic web_search», отдельно от «Tavily»), `label='native_search'`,
  цена `NATIVE_SEARCH_PRICE` (наценка `PRICE_MARKUP` — общая, в `_record_usage`).
- Токены выдачи поиска идут во вход и уже в обычном `usage`.
- Дневной лимит поиска непроверенных считает строки `kind='search'` — нативные тоже в счёт.

## Кэш префикса
Определение `tools` стоит в самом начале префикса (tools → system → messages) и одно для всех платных чатов,
без динамических полей. Бесплатные чаты идут без `tools` — их префикс отличается, поэтому точка кэша №1
(персона) между платным и бесплатным режимом НЕ общая; внутри каждого режима — общая.

## Персона
Фраза «Просто скажи что сейчас поищешь…» заменена на нейтральную («Если нужны свежие данные или событие тебе
незнакомо — ищешь и отвечаешь по найденному…»), блок персоны одинаков для обоих режимов. Дата идёт в
`service_block`. Если на стенде Haiku в платном чате ищет слишком редко — добавлять формулировку из гайда
Haiku 5.5 («Search behavior»), а не «ищи всегда» (по гайду это поиск на половине запросов, где он не нужен).

## Открыто
- Цена поиска у пула не сверена с балансом apitoken (константа $0.01 — прайс Anthropic).
- Стоимость токенов выдачи: сравнить среднюю цену реплики с поиском — Tavily против нативного (шаг 9 чек-листа).

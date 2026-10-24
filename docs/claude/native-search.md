# Нативный веб-поиск Anthropic в платном режиме (ТЗ `feat/native-search`, v1.2.0)

## Схема
- **Платный чат** (`chat_tier == "paid"`, личный чат админа — всегда paid): серверный инструмент
  `web_search` в `tools` самого диалогового запроса. Модель сама решает, искать ли. `should_search` и
  Tavily НЕ вызываются.
- **Бесплатный чат** (баланс 0): как раньше — `should_search` (Haiku) → Tavily → результаты в `service_block`.
  Напоминание о пополнении: `_free_search_hint` — при поиске и раз в `FREE_SEARCH_HINT_EVERY`=5 сообщений
  чата (счётчик `_free_msg_counter` в памяти, сброс рестартом/напоминанием) в `service_block` уходит
  `FREE_SEARCH_HINT` («поиск в free работает не очень, обратитесь к Алексею пополнить счёт»). В system и БД не пишется
  (кэш не страдает), +~40 токенов в этих запросах. Paid — никогда.
- Фото и документ, `/search`, служебные вызовы — без изменений (tools не передаются).
- Включение: `_native_search_on` = `NATIVE_SEARCH` и paid и `_search_allowed` (дневной лимит поиска
  непроверенных действует и здесь). `NATIVE_SEARCH=0` — платный режим на Tavily, реакции остаются.

## Env и константы (bot.py, рядом с блоком prompt cache)
| Имя | Дефолт | Смысл |
|---|---|---|
| `NATIVE_SEARCH` | `1` | `0` — откат на Tavily без правки кода (нужен `docker compose up -d --force-recreate`) |
| `NATIVE_SEARCH_TOOL` | `web_search_20250305` | версия инструмента. **`web_search_20260209` пул НЕ пропускает** (см. гейт) |
| `NATIVE_SEARCH_MAX_USES` | `3` | `max_uses` в определении инструмента |
| `NATIVE_SEARCH_PRICE` (const) | `0.01` | $ за один поиск; официальный прайс Anthropic $10/1000 (та же база, что у токенов); у пула стоит $0.004 — см. «Цена у пула» |
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
- Цена поиска у пула: см. раздел «Цена у пула» ниже.

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

## Грабля учёта: usage финального сообщения стрима (найдено на стенде 2026-10-10, починено)
В `get_final_message()` стрима с серверным поиском `usage.cache_creation` (разбивка записи в кэш по TTL) остаётся
нулевой — от `message_start`, а `cache_creation_input_tokens` (итого) обновлён. `_track_response` считал цену записи
по разбивке, и запись кэша выпадала из `cost_usd`: диалоги с поиском выходили в 1.5–2.7× дешевле формулы (ids 315/317
на стенде: 0.0022 и 0.0013 вместо 0.0032 и 0.0035). Теперь `_track_response` докладывает недостающее до итога в 5m.
Заодно: `cache_read`/`cache_write` в таком ответе — СУММА по внутренним итерациям (поиск = несколько проходов модели),
поэтому `cache_read` может быть больше длины самого промпта (25 063 при ~13k) — это не ошибка учёта.
Строки `dialog` с поиском, записанные до фикса, занижены. При сверке с формулой `cost55h` брать только после него.

## Цена у пула (сверено 2026-10-10)
20 запросов Haiku 5.5 с одним поиском каждый (`web_search_requests=20`): токены ≈ $0.029 по нашей таблице, поиски
20 × $0.01. Пул показал по запросу «$0.01 → ≈$0.0046» и списал с баланса $0.09 (27.96 → 27.87). Это 40% от
официальной цены (поиск $0.01 + токены ≈ $0.0115 за запрос): скидка пула 60% распространяется и на поиск. Поиск
у пула ≈ **$0.004**. Мы учитываем по официальному прайсу ($0.01), как и токены — `PRICE_MARKUP = 1.0`, скидка
пула в `usage_log` не заводится, так что в `/cost` поиск виден по официальной цене.

## Провайдеры поиска и режим чата (ТЗ `feat/gemini-models`, v1.9.0)
Схема `should_search` (Haiku) → провайдер → `service_block` с выбором провайдера (`search_with_provider`). Провайдеры:
- `claude` — нативный `web_search` Anthropic (всё, что выше). Только на Claude-моделях чата.
- `gpt` — `_gpt_web_search`: веб-поиск GPT (`gpt-6-luna`) через пул, работает при ЛЮБОЙ модели чата; выжимка идёт в
  `service_block` так же, как результаты Tavily.
- `tavily` — как раньше.

**Режим чата** — `chat_search_mode` (`auto|gpt|claude|tavily`, дефолт `auto`, таблица по образцу `chat_image_provider`),
команда `/searchmode [режим] [chat_id]` (чужой чат — только админ бота; в группе переключает любой, как модели).
`search_provider_for(chat_id, model_id)`:
- free → `SEARCH_PROVIDER_FREE` (env `tavily|gpt`, дефолт `tavily`, пока не замерена цена поиска GPT). Ручного выбора
  в free нет: не-админу отказ; админ может записать режим — включится после пополнения.
- paid, `auto`: Claude-модель → `claude` (при `NATIVE_SEARCH=0` → `tavily`), Gemini-модель → `gpt`;
- `gpt`/`tavily` — принудительно; `claude` при Gemini-модели невозможен → как `auto` (`/models` пишет «Claude →
  недоступен на Gemini, сейчас GPT»).
- **Фолбэк** (`NativeSearchUnavailable` или пул отклонил инструмент) → `gpt`, а если тот вернул `None` — `tavily`
  (с пометкой в логе); без Tavily — ответ без поиска. Тот же провайдер второй раз не зовём: платили бы дважды.
- Решение «искать или нет» для `gpt`/`tavily` — `should_search`; дневной лимит поиска непроверенных
  (`_search_allowed`) — на все провайдеры (строки `kind=search` считаются независимо от `model`).
- `/search` в платном чате с режимом `gpt` идёт через GPT, иначе (в т.ч. `claude`, нативный поиск живёт только в
  диалоге) — Tavily, как раньше; в free — `SEARCH_PROVIDER_FREE`.

**`_gpt_web_search(query)`** (env `SEARCH_GPT_MODEL`=`gpt-6-luna`, `SEARCH_GPT_EFFORT`=`low`):
`POST {POOL_BASE_URL}/v1/responses`, `Authorization: Bearer <ключ бота>`, `tools=[{"type":"web_search"}]`, без
`max_output_tokens` и `store`, `requests` в потоке (без зависимости `openai`). Таймаут `(10, 40)`; одна повторная попытка
на 429/5xx/сеть; 400 при заданном `reasoning` → один повтор без него (принимает ли пул `reasoning.effort` у luna —
проверить на стенде); `None` при любой ошибке. Выжимка ≤ 3000 символов (инвариант про размер).
- **Источники:** `url_citation` из `output[].content[].annotations[]` (`title`, `url`), без дублей, ≤ 5, `utm_source=openai`
  срезан; ссылки настоящие (не редиректы). Строка `🔗 домен · домен` под ответом — всегда кликабельная (Markdown-ссылки,
  фолбэк plain только при отказе Telegram), до 3 доменов (`sources_line`), как у нативного поиска. Условие OpenAI —
  «inline citations must be made clearly visible and clickable»; предложение про ссылки рядом с фактом — в отчёте ТЗ.
- **Деньги:** LLM-строка `gpt_search`, модель `gpt-6-luna` (`LEGACY_PRICES`: $0.10/$0.50, кэш-чтение 10%): вход без
  `cached_tokens`, кэш-чтение отдельно, выход = `output_tokens` (reasoning внутри; при `total_tokens != input+output`
  warning в логе). Поиск — `kind=search`, `model=gpt` (в `/cost` — «GPT web_search»), по строке на
  `usage.server_tool_use.web_search_requests` (нет поля — по числу `web_search_call`), цена `GPT_SEARCH_PRICE = 0.0` —
  **НЕ ИЗВЕСТНО** («Search actions incur a tool call cost» без цифры), замерить по балансу пула.

**Замер стенда 2026-10-24 (`gpt-6-luna`, `/v1/responses`):** пул принимает `reasoning.effort` = `none|low|minimal` (дефолт —
`low`), ответ 200. Время сильно гуляет и от effort почти не зависит: 7–39 с, чаще 15–25 с (запрос «курс евро»: 25 / 19.5 /
39 / 24 с; на повторе 14 и 8 с). Число поисков в одном ответе 1–3 (`server_tool_use.web_search_requests`), от этого и время;
фраза «не более одного веб-поиска» картину не меняет. `usage`: `total_tokens == input + output`, `reasoning_tokens` внутри
`output_tokens`, `cached_tokens` ≈ 4096 внутри `input_tokens` — формула учёта верна (строка `gpt_search` на стенде:
0.000586 $ = формула). Модель сама вставляет в текст markdown-ссылки `([домен](url?...&utm_source=openai))` — `utm_*` чистятся и в
тексте, и в источниках; прочие трекинговые параметры сайтов (`GL_Campaign_ID`…) остаются. Сквозной прогон на `/flash`
(`should_search` 4.8 с + GPT 7.3 с + ответ flash 9.8 с) = 22 с — выше порога 20 с из ТЗ.

**Google Search (grounding Gemini) не используем.** Условия Gemini API (https://ai.google.dev/gemini-api/terms, раздел
Grounding with Google Search, сверено 2026-10-24): результат показывать только автору запроса и вместе с Search
Suggestions (HTML), нельзя изменять/перемежать другим контентом, «analyze… or otherwise learn from». Схема «нашли → модель
чата пересказала в группу» им противоречит. Рабочий вызов нативной полосы с `google_search` остаётся в истории git
(ветка `feat/gemini-search`, коммит `bf4c8d5`), в коде его нет.

## Открыто
- Стоимость токенов выдачи: сравнить среднюю цену реплики с поиском — Tavily против нативного (шаг 9 чек-листа).

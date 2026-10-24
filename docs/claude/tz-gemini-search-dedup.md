# ТЗ: поиск через Gemini (Google Search) вместо Tavily + Gemini в дедупе

Две независимые задачи, две ветки: `feat/gemini-search` (стенд обязателен) и `feat/dedup-gemini`
(мелкая, можно без стенда, если проходит dry-run).

---

## Задача 1. `feat/gemini-search` — Gemini с Google Search как поисковый провайдер

### Зачем
На фолбэке (v1.8.0) нативный `web_search` Anthropic недоступен (`NativeSearchUnavailable`), и поиск уходит
на Tavily, который отвечает плохо. Gemini умеет искать через Google сам.

### Что проверено Алексеем 24.10.2026 (curl с сервера, ключ бота)
- **Anthropic-полоса не подходит:** `POST /v1/messages` с `model=gemini-3.8-flash` и
  `tools=[{"type":"web_search_20250305",...}]` возвращает `400 invalid_request_error: Unsupported parameter:
  only custom tools are supported with this endpoint (tools.0.type)`.
- **Нативная Gemini-полоса работает:** `POST {ANTHROPIC_BASE_URL}/v1beta/models/gemini-3.8-flash:generateContent`,
  заголовок `x-goog-api-key: <ANTHROPIC_API_KEY>`, тело `{"contents":[...],"tools":[{"google_search":{}}]}`
  возвращает HTTP 200 примерно за 10 с (один замер). Ответ с актуальными данными на 24.10.2026.
  - `candidates[0].groundingMetadata.webSearchQueries`, например `['EUR to USD exchange rate today']`;
  - `groundingMetadata.groundingChunks[].web` — `title` (только домен: `europa.eu`, `xe.com`) и `uri`
    в виде РЕДИРЕКТА `https://vertexaisearch.cloud.google.com/grounding-api-redirect/...`, а не настоящий URL;
  - `usageMetadata`: `promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount` (482 при 215 токенах
    ответа). Отдельного счётчика поисков нет.
  - Ключи кандидата: `content`, `finishReason`, `groundingMetadata`.
- Ключ тот же, что у Клодушки (`ANTHROPIC_API_KEY`), прямой вызов этой полосы в боте уже есть:
  `_transcribe_audio_gemini` (`POOL_BASE_URL/v1beta/models/...:generateContent`, `x-goog-api-key`). Делать
  по образцу этой функции, через `http_requests` в потоке. **Новых зависимостей не добавлять** (google-genai не нужен).

### Подход: Gemini — поисковый провайдер, а не модель диалога
Диалог в формат Gemini НЕ конвертируем: группы, фото, кэш и история остаются как есть. Меняется только
источник данных в существующей цепочке Tavily:

`_tavily_search` → `should_search` (Haiku решает, искать ли, и формулирует запрос) → **провайдер поиска** →
текст результатов в `service_block(search_query=..., search_results=...)` → обычный ответ модели чата.

Новая функция `_gemini_grounded_search(query: str) -> tuple[str, list[tuple[str, str]]] | None`:
- запрос: `contents` с одним user-turn. Текст: поисковый запрос плюс короткая инструкция «найди актуальные
  факты, дай сжатую выжимку с цифрами и датами, без вступлений, по-русски, если запрос русский».
  Модель `SEARCH_GEMINI_MODEL` (дефолт `gemini-3.8-flash`), `tools=[{"google_search":{}}]`;
- `thinkingConfig` поставить минимальный, но сначала проверить на стенде, что пул его принимает
  (на Anthropic-полосе `output_config` давал 400). Если не принимает, не передавать;
- ограничение РАЗМЕРА выжимки (инвариант CLAUDE.md про лимит по размеру), например 3000 символов;
- таймаут: 10 с — это норма, поэтому `(10, 40)`. Одна повторная попытка на 429/5xx, как в `_transcribe_audio_gemini`;
- текст брать только из `parts` без `thought: true`;
- `None` при любой ошибке. Тогда вызывающий код уходит на Tavily (если он настроен) или отвечает без поиска.

### Источники
- `groundingChunks[].web.uri` — редирект. Для строки источников (формат как у нативного поиска: `sources_line`,
  короткие ссылки с доменом) разворачивать в реальный URL: `HEAD` (или `GET` с `allow_redirects=False`)
  на редирект, взять `Location`. Таймаут 3–5 с на ссылку, параллельно, не больше 5 ссылок.
  Если не развернулось, использовать редирект-ссылку с подписью из `title`.
- Проверь на стенде, что из контейнера `vertexaisearch.cloud.google.com` доступен и отдаёт 30x с `Location`.
- **Проверить по документации Google Gemini API (Grounding with Google Search):** есть ли требования к показу
  результатов (Search Suggestions / `searchEntryPoint`) и сроку жизни redirect-ссылок. Пришли Алексею, что
  там сказано, со ссылкой на страницу. Не угадывай; если требования есть, обсудим до мержа.

### Где включать (env, дефолты консервативные)
- `SEARCH_PROVIDER_FALLBACK=gemini` — когда нативный поиск недоступен из-за фолбэка (`NativeSearchUnavailable`)
  или когда пул отклонил инструмент (`_native_search_rejected`). Это главный сценарий задачи.
- `SEARCH_PROVIDER_FREE=tavily` — бесплатный режим и `/search` пока остаются на Tavily: цена поиска Gemini
  через пул неизвестна. Значение `gemini` должно работать, переключит Алексей после замера цены.
- Значения: `tavily` | `gemini`. Если провайдер вернул `None`, второй раз не искать, а то и платим дважды.
  Исключение: `gemini` → `None` → Tavily как запасной, но только если в логах это помечено.

### Учёт денег
- Токены поискового вызова идут в `usage_log` как LLM: модель `gemini-3.8-flash`, `label="gemini_search"`,
  цена из `LEGACY_PRICES`. **Выход = `candidatesTokenCount + thoughtsTokenCount`**: рассуждения оплачиваются
  как вывод (так же считает `dnd-mcp`, `app/whisper_providers.py`, класс `_GeminiConversation`).
- Сам поиск — отдельная строка `kind=search`, `model=gemini`, цена `GEMINI_SEARCH_PRICE`. Значение неизвестно:
  поставить 0.0 с комментарием «НЕ ИЗВЕСТНО, замерить по балансу пула» и сказать об этом Алексею в отчёте.
  Не выдумывать цифру.
- Реакция 👀 — как сейчас у Tavily-ветки.

### Попутно (проверить, не обязательно чинить в этой ветке)
В Шаге 0 фолбэка (`docs/claude/model-fallback.md`) не проверено, включает ли `usage.output_tokens` Anthropic-полосы
для Gemini-моделей токены рассуждения. На нативной полосе они отдельные (`thoughtsTokenCount`), и их много
(~480 на короткий ответ). Сравни один и тот же запрос на обеих полосах и запиши в `model-fallback.md`.
Если на Anthropic-полосе рассуждения в `output_tokens` не попадают, `/cost` на фолбэке занижен. Сообщи
Алексею, чинить отдельно.

### Проверка (стенд)
1. Платный чат, Claude подменён на `claude-nonexistent` (как в чек-листе фолбэка), вопрос со свежими данными:
   ответ с актуальными фактами, строка источников с настоящими доменами, в логе `gemini_search`, а не Tavily.
2. В бесплатном чате с дефолтами ищет Tavily, как раньше. С `SEARCH_PROVIDER_FREE=gemini` ищет Gemini.
3. Gemini-поиск недоступен (подменить модель на несуществующую): ответ приходит, без поиска или через Tavily, падений нет.
4. `usage_log`: строка LLM `gemini_search` с выходом, включающим рассуждения, строка `search`/`gemini`.
5. Время ответа на фолбэке с поиском: замерить и записать. Если выйдет больше 20 с, сказать Алексею.

Документация: `docs/claude/native-search.md` (раздел про провайдеры поиска), `model-fallback.md`, `.env.example`,
CLAUDE.md (индекс), README, если меняется поведение `/search`.

---

## Задача 2. `feat/dedup-gemini` — Gemini как выбираемая модель в дедупе

### Зачем
`dedup_memory.py` вызывает `client.messages.create` напрямую, обёртки бота с фолбэком его не касаются.
Если Claude лежит, дедуп не работает.

### Решение Алексея / подход
**Не автоматический фолбэк, а выбор модели.** Дедуп запускается руками и меняет память (удаляет группу фактов
и вставляет слитую). Молчаливая смена модели посреди прогона дала бы смесь результатов двух моделей.

### Что сделать
- `MODEL_ALIASES`: добавить `"gemini": "gemini-3.8-flash"` и `"gemini-lite": "gemini-3.1-flash-lite"`.
  Модели появятся в TUI (`dedup_tui.py` берёт список из `dm.MODEL_ALIASES`) и в CLI `--model`.
- `PRICING`: те же цены, что в `LEGACY_PRICES` bot.py (0.75/3.75 и 0.25/1.50, `cache_read_mult` 0.1, запись 1.0),
  с комментарием «синхронно с bot.py».
- Вызов для Gemini-моделей: без `thinking`/`output_config` (`AUX_PARAMS` для них пустой — проверить),
  без `cache_control` (его там нет — проверить), `max_tokens = MAX_OUT_TOKENS + 4000` на рассуждения
  (как `FALLBACK_THINKING_HEADROOM` в bot.py; лучше импортировать или явно продублировать с комментарием).
  Логика «обрезанный ответ не применяется» (`was_truncated`) остаётся как есть.
- Оценка стоимости в TUI для Gemini — с учётом того, что рассуждения идут в вывод (см. «Попутно» в задаче 1).
  Пока это не проверено, пометить оценку как приблизительную.
- Ответ `response_text` берёт только текстовые блоки — пустой thinking-блок Gemini в начале не мешает.

### Проверка
1. `--mode llm --model gemini --user-id 592441` в dry-run: склейка осмысленная, JSON разбирается, обрезок нет.
2. То же на `sonnet` для сравнения. Разница в склейке — в отчёт Алексею (2–3 примера).
3. TUI: Gemini-модели видны в выборе, оценка стоимости показывается.
4. `--apply` на Gemini — только после того, как Алексей посмотрит dry-run.

Документация: `docs/claude/open-tasks.md` / раздел дедупа, docstring `dedup_memory.py` (список моделей).

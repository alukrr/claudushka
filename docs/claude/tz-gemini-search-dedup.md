# ТЗ: модели Gemini в /models, выбор поиска (GPT/Claude/Tavily), Gemini в дедупе

Две ветки:
- `feat/gemini-models` — Gemini как выбираемые модели чата + поиск через GPT + ручной выбор поиска.
  Крупная правка, **стенд обязателен**.
- `feat/dedup-gemini` — Gemini как выбираемая модель в дедупе. Мелкая, хватит dry-run.

Решения Алексея (24.10.2026):
- три модели Gemini: `gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.1-flash-lite`;
- в бесплатном режиме можно выбрать Haiku или `gemini-3.1-flash-lite`, остальное только в платном;
- поиск по умолчанию идёт за моделью (Claude ищет нативно, Gemini-чат — через GPT `gpt-6-luna`), в чате его можно
  закрепить вручную: GPT / Claude / Tavily. Google Search (grounding) не используем — см. 1.4.

---

## Что уже проверено живыми запросами (Алексей, curl с сервера, ключ бота)

**Anthropic-полоса `POST /v1/messages` с Gemini-моделями (23.10.2026, плюс Шаг 0 фолбэка, `docs/claude/model-fallback.md`):**
- `gemini-3.8-flash`, `gemini-3.1-flash-lite` и `gemini-2.5-flash-lite` отвечают на текст, system-блоки, картинку,
  транскрипт и стрим. `response.model` возвращается голой строкой. Первым в `content` идёт пустой блок `thinking`
  с `signature`, текст идёт следующим.
- `cache_control` и `output_config` дают 400, `thinking: disabled` игнорируется (3.8-flash думает всегда,
  300–480 токенов).
- Серверный `web_search` даёт 400: `only custom tools are supported with this endpoint (tools.0.type)`.
- **`gemini-4-argon` НЕ проверялся.** Его надо проверить так же, как остальные в Шаге 0 фолбэка
  (текст, system, картинка, транскрипт, стрим, `usage`). По докам пула у него вывод «64K streaming · 32K non-streaming».
  Если не проходит, третья ступень отпадает, сказать Алексею. Не подменять другой моделью молча.

**Нативная Gemini-полоса `POST {ANTHROPIC_BASE_URL}/v1beta/models/<m>:generateContent` + `tools:[{"google_search":{}}]` (24.10.2026; работает, но НЕ используем из-за условий Google — см. 1.4):**
- HTTP 200, ответ около 10 с (один замер), с актуальными данными на сегодня.
- `candidates[0].groundingMetadata.webSearchQueries`, `groundingChunks[].web.{title,uri}`. `title` содержит
  только домен, `uri` — РЕДИРЕКТ `https://vertexaisearch.cloud.google.com/grounding-api-redirect/...`.
- `usageMetadata`: `promptTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount` (482 при 215 токенах ответа).
  Счётчика поисков нет, цена поиска через пул НЕИЗВЕСТНА.
- Заголовок `x-goog-api-key: <ANTHROPIC_API_KEY>`. Образец вызова в боте — `_transcribe_audio_gemini`.
  **Новых зависимостей не добавлять.**

---

## Задача 1. `feat/gemini-models`

### 1.1 Модели в реестре
Добавить в `MODELS` три записи с флагом `"provider": "gemini"` (у Claude-моделей `"anthropic"` или флага нет).
Ключи и команды:

| ключ / команда | id | label | цена in/out за MTok (доки пула, 24.10.2026) |
|---|---|---|---|
| `/argon` | `gemini-4-argon` | Gemini 4 Argon | $2 / $10 |
| `/flash` | `gemini-3.8-flash` | Gemini 3.8 Flash | $0.75 / $3.75 |
| `/lite` | `gemini-3.1-flash-lite` | Gemini 3.1 Flash-Lite | $0.25 / $1.50 |

- `cache_read_mult` 0.1 (доки пула: «cached input bills at 10% of input»); запись в кэш 1.0, НЕПРОВЕРЕНО — как в v1.8.0.
- `context`: 1M; `thinking_headroom` 4000 (рассуждения не выключаются). Для argon проверить, что
  `out_tokens(...)` не выходит за его лимит вывода в нестримовом вызове (32K по докам).
- `"effort"` не ставить: `dialog_params`/`aux_params` для Gemini должны давать `{}`.
- `restricted`: не ставить (argon по цене как Sonnet).
- Если команда `/flash` или `/lite` с чем-то конфликтует (проверить существующие хендлеры и алиасы картинок), предложи другие имена.
- Записи этих моделей из `LEGACY_PRICES` (добавлены в v1.8.0 для фолбэка) перенести в `MODELS`, чтобы не было
  двух цен на одну строку. `gemini-2.5-flash-lite` остаётся в `LEGACY_PRICES`, если он где-то используется.
  Подпись «(фолбэк)» в `/cost`: решить, как показывать, что ответ был фолбэком. Например, отдельным `label`
  в `usage_log` (`dialog_fallback`), а не названием модели. Предложи и опиши.

### 1.2 Вызов модели чата на Gemini
- Вызов идёт через ту же Anthropic-полосу и те же обёртки. Параметры чистит то же, что делает `_fallback_kwargs`:
  без `thinking`/`output_config`/`tools`/`cache_control`, с запасом на рассуждения. Вынести это в общую функцию
  «привести kwargs к провайдеру модели», которую зовут и фолбэк, и обычный вызов. Двух копий логики не делать.
- `_probe_model` для Gemini: те же вычищенные параметры. `max_tokens=1` у думающей модели может вернуть
  200 с пустым текстом — это нормально, проба проверяет только, что HTTP не ошибка.
- **Кэш промпта не работает** (`cache_control` режется). На 7–10k system каждое сообщение платится по полной цене входа.
  Проверь на стенде, приходит ли в `usage` Anthropic-полосы что-то в `cache_read_input_tokens` для Gemini
  (неявный кэш на стороне Google). Запиши в `docs/claude/prompt-cache.md`. Не чинить, только зафиксировать.
- **Обратный фолбэк**: если упала Gemini-модель чата, ответ идёт от Claude по тем же правилам `should_fallback`
  и с тем же липким окном. `argon`/`flash` → `claude-sonnet-5-5`, `lite` → `DEFAULT_MODEL_ID` (Haiku).
  Сейчас `fallback_model_for` возвращает `None` для `gemini*` — расширить.
- Маркеры (`[[DRAW]]`, `[[FILE: имя]]`), режимы характера, групповой транскрипт, фото: правила в system-промпте те же.
  На стенде проверить, что Gemini их соблюдает (см. «Проверка»).
- Служебные вызовы (капча, `should_search`, память, перевод, описание медиа) остаются на `DEFAULT_MODEL_ID`
  и от выбора модели чата не зависят. Дневной обзор остаётся на Opus (как сейчас).

### 1.3 Бесплатный режим: Haiku или Lite
Сейчас в free `get_chat_model` всегда отдаёт Haiku, и инвариант CLAUDE.md запрещает менять в free `chat_models`
(прошлый платный выбор восстанавливается при возврате в paid). Инвариант сохранить:
- free-выбор хранить ОТДЕЛЬНО от `chat_models`, например в новой колонке или таблице `chat_free_model`. Схему предложи
  с миграцией в `init_db`, по правилам `.claude/rules/db.md`;
- в free допустимы только `DEFAULT_MODEL_ID` (Haiku) и `gemini-3.1-flash-lite`, остальные модели отвечают как сейчас
  («доступно в платном режиме»);
- кто переключает в free — те же, кто переключает модели в paid (админ бота и админ чата в группе, любой в личке);
- `get_chat_model` в free: выбранная free-модель, иначе Haiku.
- **Деньги:** free-чаты платит Алексей. Lite по прайсу ($0.25/$1.50) дороже Haiku 5.5 ($0.10/$0.50) в 2,5–3 раза,
  плюс у Gemini нет кэша промпта, а у Haiku есть. Значит, реальная разница на чат может быть больше. Посчитай на стенде
  стоимость 10 одинаковых сообщений на Haiku и на Lite в free-чате и приложи к отчёту.
- `/models` в free показывает обе доступные модели и отмечает выбранную.

### 1.4 Поиск: провайдеры и выбор

**Google Search (grounding) НЕ используем.** Условия Gemini API (https://ai.google.dev/gemini-api/terms, раздел
Grounding with Google Search) требуют показывать результат только автору запроса и вместе с Search Suggestions
(HTML, Telegram его не отрисует), запрещают изменять результат, смешивать его с другим контентом и
«analyze… or otherwise learn from» результаты. Схема «нашли → модель чата пересказала в группу» прямо этому
противоречит. Поэтому поиск через GPT (предложение по итогам проверки 24.10.2026; Алексей подтверждает, отдавая ТЗ).

Проверено Алексеем 24.10.2026, `POST {ANTHROPIC_BASE_URL}/v1/responses`, `Authorization: Bearer <ANTHROPIC_API_KEY>`,
`{"model":"gpt-6-luna","tools":[{"type":"web_search"}],"input":"..."}`:
- HTTP 200 за 6–7 с; `gpt-6.1-sol` — 31 с, НЕ подходит;
- `output`: `web_search_call` (`action.queries`) и `message`; в `content[].annotations[]` лежат элементы
  `type: "url_citation"` с `title` и `url`. URL настоящие, без редиректов, с хвостом `utm_source=openai`;
- `usage`: `input_tokens` 8769 (из них `input_tokens_details.cached_tokens` 4096), `output_tokens` 88
  (`output_tokens_details.reasoning_tokens` 34 — уже ВНУТРИ output_tokens по формату OpenAI, проверь),
  `server_tool_use.web_search_requests: 1`;
- условие OpenAI (https://developers.openai.com/api/docs/guides/tools-web-search): «When displaying web results or
  information contained in web results to end users, inline citations must be made clearly visible and clickable
  in your user interface». Строка источников должна быть кликабельной всегда. Если пересказ модели чата ссылается
  на конкретный факт, ссылка рядом лучше, чем только внизу. Предложи, как это сделать в нашем формате, без
  раздувания ответа.

Провайдеры:
- `claude` — нативный `web_search` Anthropic (как сейчас, `call_dialog_native`). Только для Claude-моделей.
- `gpt` — новая `_gpt_web_search(query) -> (text, sources) | None`. Работает при ЛЮБОЙ модели чата: выжимка
  идёт в `service_block` так же, как результаты Tavily.
- `tavily` — как сейчас.

Режим поиска чата (`chat_search_mode`: `auto` | `gpt` | `claude` | `tavily`, дефолт `auto`):
- `auto`: платный чат на Claude → `claude`, платный чат на Gemini → `gpt`; free → `SEARCH_PROVIDER_FREE`
  (env, дефолт `tavily`, пока не замерена цена вызова поиска GPT);
- `gpt`/`tavily` — принудительно этот провайдер;
- `claude` при Gemini-модели чата невозможен — тогда `auto`; в `/models` это видно
  («поиск: Claude → недоступен на Gemini, сейчас GPT»);
- фолбэк (Claude лёг, `NativeSearchUnavailable`, или пул отклонил инструмент) → `gpt`, а если тот вернул
  `None`, то `tavily`. Заменяет нынешний переход сразу на Tavily;
- в free ручного выбора поиска НЕТ (цена неизвестна), действует `SEARCH_PROVIDER_FREE`. Это моё предложение,
  Алексей может переиграть;
- решение «искать или нет» для `gpt`/`tavily` по-прежнему за `should_search`. Лимиты поиска непроверенных
  (`_search_allowed`) — на все провайдеры.

Команда: `/searchmode auto|gpt|claude|tavily` (без аргумента — текущий режим и варианты). Кто переключает —
как с моделями. Хранение — в БД, по образцу `chat_image_provider`.

`_gpt_web_search`:
- модель `SEARCH_GPT_MODEL` (дефолт `gpt-6-luna`), вход: запрос от `should_search` плюс инструкция «актуальные факты,
  сжатая выжимка с цифрами и датами, без вступлений, на языке запроса»; `reasoning.effort` — минимальный из допустимых
  у luna (доки пула: none/low/…), проверить, что пул принимает;
- **не передавать `max_output_tokens`** (доки пула: на этой полосе он не honoured, клиент режет сам; так же сделано в
  `dnd-mcp/app/whisper_providers.py`, класс `_OpenAIConversation`); `store` не передавать или `false`;
- вызов — `http_requests` в потоке, по образцу `_transcribe_audio_gemini`, БЕЗ новой зависимости `openai`;
- лимит РАЗМЕРА выжимки, например 3000 символов (инвариант CLAUDE.md); таймаут `(10, 40)`; одна повторная попытка
  на 429/5xx; `None` при любой ошибке;
- источники — из `url_citation` (`title`, `url`), без дублей, не больше 5, срезать `utm_source=openai`; формат строки —
  как у нативного поиска (`sources_line`). Разворачивать редиректы не нужно — код развёртки vertexaisearch из ветки
  `feat/gemini-search` удалить.

Учёт:
- LLM-строка: модель `gpt-6-luna`, `label="gpt_search"`, цена в `LEGACY_PRICES` ($0.10/$0.50 по докам пула,
  кэш-чтение — 10% входа по докам пула; проверь, что `cached_tokens` вычитаются из входа и считаются по кэш-цене);
  выход = `output_tokens` (reasoning уже внутри — проверить по сумме `total_tokens`);
- строка `kind=search`, `model=gpt`, по одной на `server_tool_use.web_search_requests`, цена `GPT_SEARCH_PRICE`.
  Цена НЕИЗВЕСТНА (OpenAI: «Search actions incur a tool call cost», без цифры на странице) — 0.0 с комментарием
  «замерить по балансу пула», в отчёте сказать Алексею;
- реакция 👀 — как у Tavily-ветки.

Ветка `feat/gemini-search` (уже сделана Claude Code по первой версии ТЗ): взять за основу (`search_with_provider`,
`sources_line` с парами, учёт), заменить Gemini grounding на `_gpt_web_search`, переименовать ветку или продолжить
в `feat/gemini-models` — как удобнее, но одной веткой.

### 1.5 `/models`, справка, документация
- `/models`: блок Claude и блок Gemini (цены, окно, отметка текущей; в free — «Haiku / Lite доступны, остальные в платном»),
  ниже строка «Поиск: <режим> (сейчас: <провайдер>)» с подсказкой `/searchmode`. Строку «Цены — прайс Anthropic…»
  заменить на «прайс провайдера».
- `USER_HELP`/`ADMIN_HELP`, README (команды), CLAUDE.md (индекс, инвариант про free-выбор), `docs/claude/models-and-costs.md`,
  `docs/claude/native-search.md` (провайдеры и режимы), `docs/claude/billing.md` (free: Haiku/Lite), `.env.example`.
- Чек-лист стенда `docs/claude/staging-checklist-gemini-models.md`.

### 1.6 Попутно: учёт рассуждений Gemini на фолбэке (проверить, не чинить молча)
Не проверено, включает ли `usage.output_tokens` Anthropic-полосы рассуждения Gemini. Сравни один и тот же запрос на обеих
полосах. Если не включает, `/cost` занижен уже сейчас (фолбэк v1.8.0 в проде), а с Gemini как основной моделью
расхождение вырастет. Сообщи Алексею, чинить в этой ветке, если просто.

### Проверка (стенд)
1. `gemini-4-argon` проходит Шаг 0. Результат записать в `model-fallback.md`.
2. `/argon`, `/flash`, `/lite` в платном чате: проба, переключение, ответ в характере. Каждый из 5 режимов на `/flash`
   (хотя бы по реплике). Группа: транскрипт и обращение по имени. Фото с вопросом.
3. Маркеры: «нарисуй …» в разговоре → DRAW срабатывает. Просьба «пришли файлом» → `[[FILE: …]]`. Служебные пометки
   в текст не протекают.
4. Поиск: платный Claude-чат `auto` → нативный; платный `/flash` `auto` → GPT, кликабельные источники без `utm_source`;
   `/searchmode tavily` и `/searchmode gpt` на Claude-чате; `/searchmode claude` на Gemini-чате → GPT и честная строка в `/models`.
5. Фолбэк: Claude подменён на `claude-nonexistent` → ответ Gemini, поиск GPT. Обратный: Gemini-модель подменена на
   несуществующую → ответ от Claude.
6. Free: `/lite` в бесплатном чате → ответ Lite; `/flash` в free → отказ «в платном режиме»; после пополнения
   восстанавливается прошлый платный выбор, после обнуления — free-выбор. Стоимость 10 сообщений Haiku и Lite.
7. `usage_log`/`/cost`: Gemini-модели с правильными ценами, рассуждения в выводе, строки `gpt_search` и `search/gpt` (число строк search = `web_search_requests`).
8. Время ответа на `/flash` с поиском — замерить. Если больше 20 с, сказать Алексею.

Версия — minor (новые команды). Расчёт контекста: system не меняется, но у Gemini нет кэша — оценить рост цены входа.

---

## Задача 2. `feat/dedup-gemini` — Gemini как выбираемая модель в дедупе

### Зачем и подход
`dedup_memory.py` вызывает `client.messages.create` напрямую, фолбэк бота его не касается. Делаем не автоматический
фолбэк, а выбор модели: дедуп запускается руками и меняет память (удаляет группу фактов и вставляет слитую). Молчаливая
смена модели посреди прогона дала бы смесь результатов двух моделей.

### Что сделать
- `MODEL_ALIASES`: `"argon": "gemini-4-argon"`, `"flash": "gemini-3.8-flash"`, `"lite": "gemini-3.1-flash-lite"` —
  те же короткие имена, что в боте. TUI берёт список из `dm.MODEL_ALIASES`.
- `PRICING`: те же цены, что в bot.py, с комментарием «синхронно с bot.py».
- Для Gemini-моделей: без `thinking`/`output_config` (`AUX_PARAMS` пустой — проверить), без `cache_control`,
  `max_tokens = MAX_OUT_TOKENS + 4000` на рассуждения. Для argon проверить лимит нестримового вывода (32K).
  `was_truncated` — как есть, обрезанное не применяется.
- Оценка стоимости в TUI: рассуждения считаются выводом. Пока п. 1.6 не проверен, пометить оценку как приблизительную.

### Проверка
1. `--mode llm --model flash --user-id 592441` в dry-run: склейка осмысленная, JSON разбирается, обрезок нет.
2. То же на `sonnet`. 2–3 примера разницы — в отчёт Алексею.
3. TUI: модели видны в выборе, оценка показывается.
4. `--apply` на Gemini — только после того, как Алексей посмотрит dry-run.

Документация: раздел дедупа в `docs/claude/open-tasks.md`, docstring `dedup_memory.py`.

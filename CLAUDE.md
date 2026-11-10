# Клодушка — Telegram-бот с Claude API

## Definition of Done (читать первым)

Задача НЕ считается выполненной, пока не сделано всё из списка. Это не опция
и не «если попросят» — это часть любой задачи, напоминать не будут.

1. **CLAUDE.md или соответствующий файл в `docs/claude/`/`.claude/rules/` обновлён**,
   если изменилось хоть что-то из: архитектура, схема БД, список команд, инварианты,
   известные грабли, workflow, зависимости, модели. Обновлять в том же коммите, что и
   код, — не «потом». Если не уверен, какой файл — см. индекс ниже; когда меняется
   что-то, задевающее сам этот индекс (новая тема, новый файл) — обновлять и его.
2. **README.md обновлён**, если изменился пользовательский интерфейс
   (новые/удалённые команды, изменение поведения).
3. **`USER_HELP` / `ADMIN_HELP` обновлены**, если добавлена или удалена команда.
4. **Синтаксис проверен**: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
   для каждого изменённого .py. Плюс проверка на повторяющиеся имена верхнего уровня (дубль `def` молча перекрывает первую функцию, инцидент 2026-11-10): `python3 -c "import ast,collections as c; t=ast.parse(open('bot.py').read()); print([k for k,v in c.Counter(n.name for n in t.body if hasattr(n,'name')).items() if v>1])"` — должен быть `[]`.
5. **Версия предложена по SemVer** с обоснованием и готовым текстом для тега.
   Тег ставит Алексей вручную — не создавать самостоятельно.
6. **Отчёт о том, что НЕ сделано**: если что-то отложено или не проверено — сказать явно,
   а не умолчать.

Если правка затрагивает system-prompt, память, историю или лимиты — обязательно
оценить влияние на размер контекста и написать об этом в отчёте.

## Тематические файлы — где что искать

Это ядро; подробности вынесены в тематические файлы (не `@`-импорты — грузятся только
когда сам их откроешь). Перед правкой соответствующей области — сначала прочитай файл:

- Рисование (`react`/`REACTIONS`, `_try_gemini_image`, `_try_gpt_image`, `_draw_and_send`, `DRAW_MARKER_RE`,
  `LEAKED_DRAW_RE`, `IMAGE_PROVIDERS`, `GEMINI_IMAGE_PRICING`, `/imagine [2k]` `/banana` `/bananapro` `/gptimage` `/flare`
  `/sunburst` `/imagemodels`, `parse_draw_request`, `draw_intent`) — `docs/claude/images.md`; ТЗ «дословно только в кавычках» — `docs/claude/tz-draw-intent.md`; чек-лист стенда — `docs/claude/staging-checklist-draw-intent.md`
- Режимы характера (`PERSONAS`, `/classic /business /toxic /flirt /granny /personas`, `_persona_text`, `CORE_IDENTITY`/`_core_rules`,
  `get_chat_persona`, `get_review_persona`, `chat_review_persona`, `/review_persona`, `free_persona`, `personas/*.md`, volume `./personas`) — `docs/claude/personas.md`; ТЗ — `docs/claude/tz-personas.md`; ТЗ «характер обзора отдельно» — `docs/claude/tz-review-persona.md`
- Файлы целиком (`[[FILE: имя]]`, `FILE_MARKER_RE`, `extract_files`, `_file_sent_note`, `FILE_*`) — `docs/claude/files.md`
- Настройки чата (`SETTINGS`, `ChatSetting`, `can_change`, `apply_setting`) — `docs/claude/settings.md`; ТЗ — `docs/claude/tz-settings-registry.md`; чек-лист стенда — `docs/claude/staging-checklist-settings-registry.md`
- Групповой чат (`build_group_messages`, `group_messages`, `reply_context`/`_bot_reply_quote`, `detect_repeat`, `compute_strike`, `_apply_strike`, `chat_ignores`, `/autoignore /unignore /activity`, `activity_should_check`, управляющие чата (`chat_managers`, `/manager`, `/managers`, `is_real_chat_admin`), гейтинг доступа,
  `/ratelimit`, `is_chat_admin`) — `docs/claude/group-chat.md`; ТЗ активности и автоигнора — `docs/claude/tz-group-behavior.md`
- Голос/видео/кружочки/GIF (`_transcribe_audio_gemini`, `_extract_video_frames`,
  `_describe_media`, ветки `voice`/`video_note`/`video`/`animation`) —
  `docs/claude/media.md`
- Дневной обзор (`daily_chat_review`, `cmd_review`, `/review_on`, `/review_off`,
  `run_daily`, `BERLIN_TZ`, `_review_window`, `build_review_log`, `split_review_sessions`, `prepare_review_input`, `REVIEW_*`, `_review_history_text`, `GROUP_MESSAGES_KEEP`) — `docs/claude/daily-review.md`; ТЗ «характер обзора отдельно» — `docs/claude/tz-review-persona.md`; чек-лист стенда — `docs/claude/staging-checklist-review-persona.md`; ТЗ «весь день + блоки тем» — `docs/claude/tz-daily-review-topics.md`; чек-лист стенда — `docs/claude/staging-checklist-review-topics.md`
- Модели и цены (`MODELS` = только Gemini, `/argon /flash /lite`, заглушки `/haiku /sonnet /opus /fable`, `/models`, `calc_llm_cost`, миграция `no_anthropic_models`) —
  `docs/claude/models-and-costs.md`
- Prompt caching (`build_system`, `service_block`, `mark_cache_tail`, `pick_history_window`,
  `PROMPT_CACHE`, `HISTORY_STEP`, `CACHE_TTL_PERSONA`) — `docs/claude/prompt-cache.md`
- Поиск (`sources_line`, провайдеры gpt/tavily, `/searchmode`, `chat_search_mode`, `search_provider_for`, `_gpt_web_search`, `SEARCH_PROVIDER_FREE`, `SEARCH_GPT_*`;
  нативный web_search Anthropic удалён 2026-10-28, файл — история) — `docs/claude/native-search.md`
- Деньги и доступ (`usage_log`, `chat_credits`, баланс, тарифы paid/free, `usage_ctx`,
  `_record_usage`, `gate_update`, `/cost`, `/topup`, `/verify /unverify /ban /unban`,
  лимиты непроверенных, стартовый бонус, `/fable`-подтверждение; пополнение звёздами Telegram — `/pay` `/terms` `/paysupport` `/refund`,
  `STARS_USD_RATE`/`STARS_PACKS`, `star_payments`, `apply_topup`/`notify_topup`, `check_star_payment`, `on_pre_checkout`/`on_successful_payment`,
  `star_payment_record`/`star_refund_begin`) — `docs/claude/billing.md` → «Пополнение Stars»; ТЗ — `docs/claude/tz-stars-topup.md`; чек-лист стенда —
  `docs/claude/staging-checklist-stars-topup.md`
- Ошибки API, ретраи, блокирующие вызовы, `response_text`, `parse_json_lenient`, параллельные апдейты (`PerChatUpdateProcessor`, `MAX_CONCURRENT_UPDATES`,
  `THREAD_POOL_WORKERS`, `_ChatLogFilter`) — `docs/claude/api-errors.md`; ТЗ — `docs/claude/tz-concurrent-updates.md`; чек-лист стенда —
  `docs/claude/staging-checklist-concurrent-updates.md`
- Уход с Anthropic (пул закрыл Claude 2026-10-28; Шаги 1 и 3 сделаны в v2.0.0, Шаг 2 — рисование Terra `/terra` — не сделан), замеры всех моделей пула (`scripts/bench_pool.py`) —
  ТЗ `docs/claude/tz-no-anthropic.md`; чек-лист стенда `feat/no-anthropic` — `docs/claude/staging-checklist-no-anthropic.md`
- Фолбэк Gemini → GPT при сбоях пула (`FALLBACK_PAIRS`, `FALLBACK_*`, `should_fallback`, `_call_with_fallback`, `kwargs_for_model`,
  липкое окно, цены GPT в `LEGACY_PRICES`) — `docs/claude/model-fallback.md`
- Safety-блок провайдера (`is_safety_block`, `_dialog_call`, `safety_scan`, `safety_find`, `group_messages.hidden`, `SAFETY_*`,
  `/hidden /unhide /safety_scan`) — `docs/claude/safety-unblock.md`; ТЗ — `docs/claude/tz-safety-unblock.md`; чек-лист стенда
  `feat/safety-unblock` — `docs/claude/staging-checklist-safety-unblock.md`; ручной разбор — инциденты 2026-10-30/31
- Профиль участника (`user_profile`, `profile_proposals`, `_profile_text`, `profile_updates`, `send_profile_digest`,
  `/profile` `/profile_ok` `/profile_no` `/profile_digest`, экран «Профили» в dedup TUI) — `docs/claude/profile.md`
- Память (`memory`-таблица, `extract_memory`, `extract_all_participants_memory`,
  `/memory`, `/memory_full`, `/forget`, метки участников `P1…` в групповом извлечении) — `docs/claude/memory.md`; ТЗ — `docs/claude/tz-memory-uid.md`; чек-лист стенда — `docs/claude/staging-checklist-memory-uid.md`
- Режим разработчика в платной личке (`dev_mode_active`, `handle_dev_message`, `_dev_answer`, `/dev`, `/ws`, `ws_*`, `DEV_*`,
  workspace `data/devspace/`, таблицы `dev_mode`/`dev_messages`) — `docs/claude/dev-mode.md`; ТЗ — `docs/claude/tz-dev-mode.md`;
  чек-лист стенда `feat/dev-mode` — `docs/claude/staging-checklist-dev-mode.md`
- История инцидентов с датами (полные дословные разборы) — `docs/claude/incidents.md`
  — сверься, если баг похож на уже виденный, прежде чем чинить с нуля
- Открытые задачи (незакрытое, не баги) — `docs/claude/open-tasks.md`
- План работ ноябрь 2026 (мини-приложение, активность, характер обзора, рисование по кавычкам, автоигнор + пункты ту-ду):
  решения и порядок веток — `docs/claude/roadmap-2026-11.md`
- Мини-приложение настроек (`miniapp/server.py`, `miniapp/index.html`, `validate_init_data`, `_ma_*`, `/settings`, `MINIAPP_PUBLIC_URL`) — `docs/claude/miniapp.md`; чек-лист стенда — `docs/claude/staging-checklist-miniapp.md`;
  runbook прокси `edge` — `docs/claude/runbook-edge-proxy.md`; ТЗ реестра настроек —
  `docs/claude/tz-settings-registry.md`; ТЗ мини-приложения — `docs/claude/tz-miniapp.md`; ТЗ v2 (экран «Расходы», открытие из группы через Main Mini App `startapp`, «По месяцам» в `/cost <chat_id>`; `_cost_data`, `_ma_chat_costs`, `MINIAPP_MAIN_APP`) — `docs/claude/tz-miniapp-v2.md`; чек-лист стенда v2 — `docs/claude/staging-checklist-miniapp-v2.md`; ТЗ «недели KW и оценка цены подписки» (`_cost_week_windows`, `_subscription_estimate`, `_paid_seconds`, `db.paid_seconds`, поля `weeks`/`subscription` в `/costs`, `scripts/check_subscription_estimate.py`) — `docs/claude/tz-cost-weeks-subscription.md`, раздел «Оценка цены подписки» в `docs/claude/billing.md`; ТЗ управляющих чата — `docs/claude/tz-chat-managers.md`
- Параллельная обработка апдейтов (`PerChatUpdateProcessor`, `MAX_CONCURRENT_UPDATES`): ТЗ — `docs/claude/tz-concurrent-updates.md`
- Атрибуция групповой памяти по uid (метки участников `P1…`): ТЗ — `docs/claude/tz-memory-uid.md`
- `whatsapp.py` — `.claude/rules/whatsapp.md` (грузится автоматически при чтении файла)
- `db.py` — `.claude/rules/db.md` (автоматически)
- `docker-compose.yml` — `.claude/rules/docker-compose.md` (автоматически)
- Тестовый стенд (`docker-compose.test.yml`, обновление зависимостей, рискованные
  правки перед продом) — `docs/claude/staging.md`; пошаговый чек-лист ТЗ v0.10 (с SELECT'ами) —
  `docs/claude/staging-checklist-v0.10.md`; чек-лист ТЗ `feat/opus-5-5` (Opus 5.5, цена по
  `response.model`, миграция `chat_models`) — `docs/claude/staging-checklist-opus-5-5.md`;
  чек-лист ТЗ `feat/haiku-5-5` (Haiku 5.5, thinking/effort, ступенчатая цена >100k) —
  `docs/claude/staging-checklist-haiku-5-5.md`; чек-лист ТЗ `feat/prompt-cache` (кэш префикса, окно
  истории шагами) — `docs/claude/staging-checklist-prompt-cache.md`; чек-лист ТЗ `feat/reactions`
  (реакции 👀/✍ вместо «Рисую…»/«ищу…») — `docs/claude/staging-checklist-reactions.md`; чек-лист ТЗ `feat/native-search` (нативный
  `web_search` в платном режиме) — `docs/claude/staging-checklist-native-search.md`; чек-лист ТЗ `feat/banana-21` (Nano Banana 2.1,
  `/bananapro`, 2K) — `docs/claude/staging-checklist-banana-21.md`; чек-лист ТЗ `feat/personas` (режимы характера, выкатка только `--force-recreate`) — `docs/claude/staging-checklist-personas.md`; чек-лист ТЗ `feat/user-profile` (профиль участника,
  дайджест, экран «Профили») — `docs/claude/staging-checklist-profile.md`; чек-лист ТЗ `feat/model-fallback`
  (фолбэк на Gemini) — `docs/claude/staging-checklist-model-fallback.md`

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 22.8 (мажорное обновление с 21.10 — 2026-09-19, см.
  `docs/claude/staging.md` про прогон на тестовом стенде перед продом)
- anthropic 1.7.0 — только как HTTP-клиент полосы `/v1/messages` пула (модели Anthropic пул не отдаёт с 2026-10-28; HTTP-слой SDK на
  `httpx2`, ОТДЕЛЬНОМ пакете от обычного `httpx`, см. `docs/claude/api-errors.md`) — реестр моделей
  `MODELS` в bot.py (Gemini `/argon /flash /lite`). Подробности — `docs/claude/models-and-costs.md`.
- tavily-python 0.8.4 — веб-поиск в бесплатном режиме и `/search`; платный режим — поиск GPT через пул (`docs/claude/native-search.md`)
- Генерация изображений — `/imagine`, DRAW-маркер, провайдеры banana/GPT Image 2/GPT Image
  2.5 Flare/Sunburst. Подробности — `docs/claude/images.md`.
- fastapi 0.141.1 + uvicorn 0.53.0 — WhatsApp webhook. Подробности — `.claude/rules/whatsapp.md`.
- httpx 0.28.1 (для FastAPI/PTB/наших прямых вызовов — не путать с `httpx2`, отдельной
  зависимостью только anthropic SDK)
- tzdata — гарантирует `zoneinfo.ZoneInfo` независимо от системной базы поясов в
  образе. Единственный часовой пояс проекта — `BERLIN_TZ = ZoneInfo("Europe/Berlin")`
  (bot.py); новое место с датой/временем — брать его, не писать
  `timezone(timedelta(...))`. Подробности — `docs/claude/daily-review.md`.
- SQLite (stdlib `sqlite3`, обёртка в db.py). Подробности — `.claude/rules/db.md`.
- Docker Compose (без Dockerfile). Подробности — `.claude/rules/docker-compose.md`.

## Структура
- bot.py — основной код Telegram-бота (~3750 строк)
- db.py — слой данных SQLite (пользователи, история, память, чаты, учёт расхода, балансы) — `.claude/rules/db.md`
- api_errors.py — классификация ошибок Anthropic API, ретраи, сообщения пользователю
  (общий для bot.py и whatsapp.py) — `docs/claude/api-errors.md`
- whatsapp.py — WhatsApp-бот (FastAPI webhook, отдельный сервис; **сервис на паузе с
  2026-09-19**, код и правила не трогали) — `.claude/rules/whatsapp.md`
- docker-compose.yml — два сервиса: claudushka + claudushka-wa (WhatsApp, `profiles:`,
  на паузе) — `.claude/rules/docker-compose.md`
- .env — секреты (не в git)
- requirements.txt — все зависимости
- dedup_memory.py — ручная дедупликация фактов памяти (CLI + интерактивный TUI `dedup_tui.py` с построчной правкой фактов, оценка стоимости
  без API, бэкап перед apply, обрезанные группы не применяются), см. `docs/claude/open-tasks.md`;
  на сервере запускается функцией `dedup` из `docs/claude/prod-aliases.sh`

## Инварианты (коротко — подробности в тематических файлах)
- Никаких блокирующих вызовов API/HTTP из event loop — `await asyncio.to_thread(...)`
  или выделенные обёртки (`call_claude`/`aux_create`/`sync_create`). `docs/claude/api-errors.md`.
- Фоновые fire-and-forget задачи — только через `_spawn_background_task`, не голый
  `asyncio.create_task` (иначе задачу может собрать GC до завершения). `docs/claude/api-errors.md`.
- Разбор ответа Claude — только `api_errors.response_text()`, никогда `content[0].text`.
- JSON из ответа модели — только `api_errors.parse_json_lenient()`, никогда `find`/`rfind`.
- Мини-приложение — только через `SETTINGS`/`can_change`/`apply_setting` (новых правил в нём нет), авторизация — только подпись `initData` (`validate_init_data`, по документации Telegram); выкатка — `docker compose up -d --force-recreate` (том `./miniapp`, сеть `edge`), не `/update` (`docs/claude/miniapp.md`).
- Права «админ этого чата» — только `is_chat_admin` (включает управляющих `chat_managers`); выдача и снятие прав управляющим — только `is_real_chat_admin` (без управляющих), иначе управляющий начнёт назначать других (`docs/claude/group-chat.md` → «Управляющие чата»).
- Апдейты ОДНОГО чата — по очереди, РАЗНЫХ — параллельно (`PerChatUpdateProcessor`, `MAX_CONCURRENT_UPDATES`, `1` = аварийный откат на «по одному»). Новое глобальное изменяемое состояние — с ключом по чату
  или с явной защитой; проверка-и-запись через `await` по ключу, общему для разных чатов, — гонка; перед взятием замка чата в процессоре `await` не вставлять (`docs/claude/api-errors.md`).
- Автоигнор и активность — только кодом и только в группах; модель паузу не объявляет и не включает (`docs/claude/group-chat.md`, ТЗ `docs/claude/tz-group-behavior.md`, чек-лист `docs/claude/staging-checklist-group-behavior.md`).
- Ответы бота в группе сохраняются в `group_messages` с `is_bot=True` во всех ветках
  (текст/фото/документ/рисование/обзор) — иначе Клодушка не видит собственные реплики.
- Любой источник данных, попадающий в system-prompt или `messages`, обязан иметь лимит
  по РАЗМЕРУ (не только по числу элементов) — см. инцидент 2026-07-26 в `docs/claude/incidents.md`.
- Каждый новый `.py`-файл, который импортируют bot.py/whatsapp.py — добавить в volume
  mounts ОБОИХ сервисов в `docker-compose.yml`. `.claude/rules/docker-compose.md`. Новый volume (как `./personas`) —
  релиз только через `docker compose up -d --force-recreate`, не `/update`.
- Характер: `CORE_IDENTITY` → режим (`personas/*.md`) → `_core_rules()` → групповой аддендум; режим задаёт тон, не правила
  (честность/маркеры/служебный блок/группа). Новое место с зашитым «характером» — брать `tone`/`review_style` режима
  чата. Ключ режима в личке — `user_id`. `docs/claude/personas.md`.
- Новая настройка чата — только через `SETTINGS`; права на неё — только `can_change`, не в хендлере (`docs/claude/settings.md`).
- Модель чата — только `get_chat_model` (free → всегда Lite, `chat_models` в free не трогаем; paid → `chat_models` или Flash).
  Режим поиска чата — `chat_search_mode` через `search_provider_for`, не читать напрямую. Параметры вызова под модель — только
  `kwargs_for_model` (первый вызов и фолбэк). `docs/claude/models-and-costs.md`, `docs/claude/native-search.md`.
- **С 2026-10-28 пул НЕ отдаёт модели Anthropic** (403 `model_blocked`): Claude в `MODELS` нет, новых вызовов на Claude не добавлять.
  `MODELS` — только Gemini через `/v1/messages`; фолбэк — GPT другого провайдера (`FALLBACK_PAIRS`), обратного нет, GPT-моделей
  чата нет. Переезд — `docs/claude/tz-no-anthropic.md`.
- Фолбэк живёт ТОЛЬКО в обёртках (`call_claude`/`sync_create`): новый вызов модели — через них, не `client.messages.create`
  напрямую. Пробам, которым нужна именно запрошенная модель, — `fallback=False`. Ни одна модель не получает `thinking`/
  `output_config`/`tools`/`cache_control` (Gemini → 400, у GPT действие не доказано); 402 и 400 (`prompt is too long`) не
  переключают. `docs/claude/model-fallback.md`.
- Уровень логов httpx не поднимать до INFO — токен бота утекает в URL.
- Пользователь никогда не видит сырой текст исключения — только `api_errors.reply_api_error`.
- Служебные вызовы (капча, поиск, перевод промпта, извлечение памяти, описание медиа) —
  только `DEFAULT_MODEL_ID` (Lite), ВСЕГДА с `**aux_params()` и `max_tokens=out_tokens(DEFAULT_MODEL_ID, N)` (Lite/Flash
  думают, рассуждения съедят малый лимит и ответ придёт пустым); ответы пользователю — `get_chat_model(chat_id)` с
  `**dialog_params(model)` и `out_tokens(...)`.
  `MODELS["..."]["id"]` в новых вызовах не хардкодить (единственное сознательное исключение —
  `daily_chat_review` на Argon, платная фича). `docs/claude/models-and-costs.md`.
- Начало запроса к модели (system) — СТАТИЧНО: изменчивое (время, результаты поиска, reply-контекст)
  только в `service_block` последнего user-turn, не в system, и не в БД. Система собирается через
  `build_system` (блоки с `cache_control`), длина — `system_chars`, не `len`. Окно истории — шагами
  (`pick_history_window`), не скользящее. `docs/claude/prompt-cache.md`.
- Каждый вызов, стоящий денег (LLM/картинка/поиск), обязан попасть в `usage_log` через
  `_record_usage` (LLM — автоматически внутри `call_claude`/`sync_create`/`aux_create`, но
  ВСЕГДА с `label=`). Чат берётся из `usage_ctx` (contextvar, ставит `gate_update`; в джобах —
  явно) — не передавать chat_id руками и не писать в `usage_log` в обход. Цена LLM — по
  `response.model` (фактической), множители кэша — поля модели в `MODELS`/`LEGACY_PRICES`,
  глобальных констант нет. Нативные вызовы `generateContent` (голос) — `_record_gemini_usage` с `label="voice"`. `docs/claude/billing.md`, `docs/claude/models-and-costs.md`.
- Тариф чата — `chat_tier(chat_id)`; модель/провайдер — только через `get_chat_model` /
  `get_chat_image_provider` (учитывают free), не читать `chat_models`/`chat_image_provider`
  напрямую. В free записи этих таблиц не менять. `docs/claude/billing.md`.
- Деньги Stars: баланс пополняется ТОЛЬКО по `successful_payment` (`on_successful_payment` → `db.star_payment_record`, идемпотентно по `charge_id`) и только через
  `apply_topup` (общий хвост с `/topup`); `pre_checkout_query` — только лёгкие проверки (`check_star_payment`), `PerChatUpdateProcessor` пропускает его без замка
  (10 с на ответ); возврат — только `/refund` админа (`star_refund_begin` → `refundStarPayment`, отказ → `star_refund_revert`) (`docs/claude/billing.md`). Порог авто-проверки — только `_verify_min_topup(chat_id)` (личка $2, группа $5), бонус `STARTER_BONUS_*` ($2/$5) — одно правило для Stars, `/topup`, `/verify`.
- Режим разработчика (только личка + paid + флаг) — ТОЛЬКО через `dev_mode_active`; свои таблицы (`dev_messages`), обычный тред
  `conversations`, память, профиль, персону и рисование не читает и не пишет; файлы — состояние в workspace (`data/devspace/<user_id>/`),
  в историю только пометки; вызов — `call_claude` с `usage_label="dev"`, `timeout=DEV_REQUEST_TIMEOUT`. `docs/claude/dev-mode.md`.
- Чтения `group_messages`, идущие в промпт модели, — ТОЛЬКО с `hidden = 0` (`get_group_transcript`, `get_group_messages_range`,
  `get_group_history`); скрытые строки (`hidden=1`, safety-блок) физически остаются, ничего не удаляем. Основной вызов диалога —
  через `_dialog_call` (повтор без истории при safety-блоке), пробы скана — настоящий запрос бота через `_model_call`.
  `docs/claude/safety-unblock.md`.
- Факты и профили участников группы — только по `user_id` (метки `P1…` в запросе извлечения), не по нику: ник меняется и совпадает у разных людей
  (инцидент 2026-09-28); поиск `user_id` по нику не делать (`docs/claude/memory.md`).
- Профиль участника: модель только ПРЕДЛАГАЕТ (`profile_proposals`), в `user_profile` пишет админ через
  `db.apply_profile_ops` (бот и dedup TUI — один путь); личка — `chat_id=0`, не NULL; любое новое место, удаляющее
  память человека, обязано чистить и профиль (`db.clear_memory` уже чистит). `docs/claude/profile.md`.

## Команды (актуальный список)
`/help` показывает всем пользователям базовые команды, adminам — полный список из двух блоков (`USER_HELP` + `ADMIN_HELP` в bot.py). При добавлении новой команды обновлять оба константы.

## Версионность
Текущая версия: v2.9.0 (недели KW по ISO 8601 в расходах — 12 последних, в мини-приложении и `/cost <chat_id>`; оценка цены подписки — месяц ≈ траты/платные дни×30, неделя ×7 +5%, окно 90 дней не раньше 2026-10-28, <7 дней — «мало данных»; платные дни — по восстановленному балансу `db.paid_seconds`; `docs/claude/billing.md`, ТЗ `docs/claude/tz-cost-weeks-subscription.md`; сверка на копии прод-БД 2026-11-10: 0 нарушений); предыдущая: v2.8.0 (мини-приложение v2: экран «Расходы» — админы чата, управляющие и админ бота, бесплатное только админу бота, 12 месяцев; открытие из группы через Main Mini App `?startapp=chat_n…` при `MINIAPP_MAIN_APP=1`; «По месяцам» в `/cost <chat_id>`, общий `_cost_data` — `docs/claude/miniapp.md`, ТЗ `docs/claude/tz-miniapp-v2.md`); предыдущая: v2.7.3 (`/cost <chat_id>`, `/verify`, `/unverify`, `/ban <id>`, `/unban <id>` снова работают: дубль `_parse_chat_arg` переименован в `_parse_safety_chat`, инцидент 2026-11-10 в `docs/claude/incidents.md`); предыдущая: v2.7.2 (групповая память и профили — атрибуция по `user_id` через метки участников `P1…` (`build_memory_participants`, `resolve_memory_participant`), метки в тексты фактов не попадают (`replace_memory_tags`), `get_user_id_by_name_in_chat` удалена — `docs/claude/memory.md`, ТЗ `docs/claude/tz-memory-uid.md`); предыдущая: v2.7.1 (утечки служебных пометок «(обзор дня по», «(служебное:» и формата транскрипта «[время] Имя:» в ответы режутся на выходе — `strip_leaked_service_note`; автоигнор судит только текущее обращение (`ABUSE_MARK`, контекст `get_abuse_context` за 60 мин); активность отвечает на сообщение-триггер; инциденты 2026-11-02 в `docs/claude/incidents.md`); предыдущая: v2.7.0 (параллельная обработка апдейтов: разные чаты — параллельно, один чат — по очереди, `PerChatUpdateProcessor`, `MAX_CONCURRENT_UPDATES` (16; 1 = откат), пул потоков `THREAD_POOL_WORKERS` (32), SQLite timeout 30 с, метка `[chat=…]` в логах — `docs/claude/api-errors.md`); предыдущая: v2.6.0 (мини-приложение настроек: `/settings`, кнопка меню в личке, сервер внутри процесса бота на :8090 за прокси `edge` (`claudushka.alukr.de`), авторизация только `initData`, права только `can_change`; выкатка только `docker compose up -d --force-recreate`; `docs/claude/miniapp.md`, ТЗ `docs/claude/tz-miniapp.md`); предыдущая: v2.5.1 (`/help` админу режется под лимит Telegram — `split_lines_for_telegram`; потолок ожидания `Retry-After` у SDK-клиента `SDK_RETRY_WAIT_CAP` = 10 с — `docs/claude/api-errors.md`); предыдущая: v2.5.0 (управляющие чата — `/manager add|rm`, `/managers`, таблица `chat_managers`; `is_chat_admin` учитывает управляющих, выдача прав — только `is_real_chat_admin`; `docs/claude/group-chat.md`, ТЗ `docs/claude/tz-chat-managers.md`); предыдущая: v2.4.2 (маркеры `[[DRAW]]`/`[[FILE]]` разбираются и в ответах на фото и файлы — `_finish_media_answer`, `_extract_draw_marker`, `docs/claude/images.md`; общий обработчик ошибок хендлеров `on_error` — `docs/claude/api-errors.md`); предыдущая: v2.4.1 (единый реестр настроек чата `SETTINGS` / `can_change` / `apply_setting`, команды переведены на него без изменения поведения — `docs/claude/settings.md`); предыдущая: v2.4.0 (активность в группе `/activity` per-chat — кубик + проверка Lite + реплика модели чата; автоигнор за повторы и травлю — лестница предупреждение → 5/15/30/60 мин, `/autoignore` `/unignore`, таблица `chat_ignores`; `docs/claude/group-chat.md`, ТЗ `docs/claude/tz-group-behavior.md`); предыдущая: v2.3.0 (отдельный характер обзора `/review_persona`, `chat_review_persona`; в историю — пометка + темы обзора вместо полного текста; `docs/claude/personas.md`, `docs/claude/daily-review.md`); предыдущая: v2.2.0 («нарисуй» дословно только в кавычках и `/imagine`, без кавычек — через диалог: модель сама пишет промпт по пометке `draw_intent`; «покажи» больше не триггер; `parse_draw_request`, `docs/claude/images.md`, ТЗ `docs/claude/tz-draw-intent.md`); предыдущая: v2.1.1 (реплай на реплику Клодушки доходит до модели — `_bot_reply_quote`, `docs/claude/group-chat.md`; `/update` возвращает владельца и новым каталогам, и после упавшего pull — `_repo_parent_dirs`); предыдущая: v2.1.0 (самовосстановление чата после safety-блока провайдера: повтор без истории, автоскан и скрытие строки `group_messages.hidden`, `/hidden /unhide /safety_scan`: `docs/claude/safety-unblock.md`); предыдущая: v2.0.0 (уход с Anthropic: Claude убран из бота, модели только Gemini `/argon /flash /lite`, фолбэк на GPT, поиск через GPT, миграция `chat_models`: `docs/claude/tz-no-anthropic.md`, `docs/claude/models-and-costs.md`; Шаг 2 — рисование Terra — не сделан); предыдущая: v1.11.1 (хотфикс `CLAUDE_DISABLED`); v1.11.0 (режим разработчика в платной личке: `/dev`, `/ws`, workspace `data/devspace/`, тред `dev_messages`, альбомы файлов одним запросом: `docs/claude/dev-mode.md`; предыдущая: v1.10.0 — дневной обзор за сутки с блоками тем: окно с прошлого обзора 21:00, ветки A/B (один вызов Opus / выжимки Haiku + итог Opus), `group_messages` 3000 строк, `/review` тем же путём: `docs/claude/daily-review.md`; предыдущая: v1.9.0 (Gemini как модели чата `/argon /flash /lite`, free-выбор Haiku/Lite в `chat_free_model`, поиск через GPT и `/searchmode`, обратный фолбэк Gemini → Claude, Gemini в дедупе: `docs/claude/models-and-costs.md`, `docs/claude/native-search.md`, `docs/claude/model-fallback.md`; предыдущая: v1.8.0 — фолбэк Claude → Gemini при сбоях пула: липкое окно, цены Gemini в /cost, `docs/claude/model-fallback.md`; предыдущая: v1.7.1 — граница признания ошибок и пометка обзора в транскрипте, инцидент 2026-10-22, `docs/claude/incidents.md`; предыдущая: v1.7.0 — режимы характера: 5 режимов per-chat, режим дня в free, `docs/claude/personas.md`; предыдущая: v1.6.0 — файлы целиком документом по `[[FILE: имя]]`, `docs/claude/files.md`; предыдущая: v1.5.1 — dedup: обрезанный кусок большой группы делится пополам, провал слияния не роняет группу, `docs/claude/open-tasks.md`; предыдущая: v1.5.0 — профиль участника: предложения модели, еженедельный дайджест .md вс 19:00 Berlin, подтверждение /profile_ok, экран «Профили» в dedup TUI, `docs/claude/profile.md`; предыдущая: v1.4.4 — промпты извлечения памяти: поездки/отпуск/командировки не пишутся в long, `docs/claude/memory.md`; предыдущая: v1.4.3 — бот удаляет просроченные факты памяти — `memory_purge` 04:30 Berlin, `docs/claude/memory.md`; dedup: только
живые факты, `--mode purge-expired`, большие группы по частям, не платить дважды за обрезанное, срок слияния = минимум;
`docs/claude/open-tasks.md`, инцидент 2026-10-18; предыдущие: v1.4.2 — построчная правка и ручная правка в dedup TUI, v1.4.1 — видимость скрытого выбора, накопительная калибровка, функция `dedup`, v1.4.0 — dedup TUI, v1.3.0 — Nano Banana 2.1, `/bananapro`, 2K по запросу, `/opus` `/fable` `/bananapro` в группе только
админам, 2026-10-10, `docs/claude/images.md`; предыдущие: v1.2.0 — нативный web_search, `docs/claude/native-search.md`, v1.1.5 — реакции 👀/✍, `docs/claude/images.md`, v1.1.4 — prompt cache,
`docs/claude/prompt-cache.md`, v1.1.3 — Haiku 5.5, v1.1.2 — Sonnet 5.5, v1.1.1 — Opus 5.5, v1.1.0 — GPT Image 2.5 Flare/Sunburst, v1.0.0 — учёт стоимости,
тарифы paid/free, ТЗ v0.10, `docs/claude/billing.md`). Теги обычно ставит Алексей вручную:
`git tag -a vX.Y.Z -m "..."`. Клод тоже может создать тег и запушить его — когда Алексей
явно попросил об этом в моменте (например, «поставь тег сам, я в дороге»), а не по
умолчанию на каждую задачу: дефолт по-прежнему «предложить версию и готовую команду,
тег ставит Алексей».

**Не плодить теги.** Тег — на ЗАВЕРШЁННУЮ задачу/фичу, а не на каждый коммит внутри
неё. Если сразу после деплоя нашёлся баг в только что сделанном — это ещё та же задача,
фиксишь и коммитишь без нового тега; тег ставится (или предлагается) один раз, когда
всё действительно закончено и проверено, а не отдельно на каждый хотфикс по дороге.
Инцидент 2026-09: за одну сессию с одной фичей (переключаемый провайдер картинок)
поставил три тега подряд (v0.12.0, v0.12.1, v0.12.2) на фичу и два последовавших
хотфикса — Алексей попросил так больше не делать.

Версия доступна через `/version` в боте. Команды `cmd_update` и `cmd_version` используют хелпер `_git()` — не переписывай их без необходимости.

SemVer:
- **patch** (0.8.x) — багфиксы, поведение для пользователя не меняется
- **minor** (0.x.0) — новые команды/фичи, обратно совместимые
- **major** — несовместимые изменения схемы БД или поведения

`git describe --tags --always --dirty` питает `/version`. Строка «(vX.Y.Z)» в сообщении
коммита БЕЗ реального тега — ошибка: так уже случилось с v0.7.0, тег пришлось ставить
ретроспективно. Предлагая версию, всегда давай готовую команду `git tag -a ... -m "..."`.

## Правила
- Отчёты и ответы Алексею — на русском.
- Новые зависимости добавлять осознанно, минимизировать
- Переменные окружения через .env и docker-compose env_file
- Проверка/бан/балансы — только в БД (`allowed_chats`, `users`, `chat_credits`), НЕ через .env; белых списков и allowed.json больше нет
- Контейнер без Dockerfile, используем image + command
- Данные (SQLite, data/) монтируются в /app/data, не в git
- Не правь файлы напрямую на сервере — это ведёт к `-dirty` версии и расхождению с git

## Пути (не перепутать)
- Windows (PowerShell): `C:\Users\ukrai\projects\claudushka` — рабочая копия, из неё тоже можно править, коммитить и пушить
- WSL: `~/projects/claudushka` — вторая рабочая копия; допустимо работать в обеих средах, главное — не терять правки между ними
  (перед работой `git pull`, после — commit + push)
- Сервер: `~/claudushka` ← только `git pull` и деплой, файлы не править
- Окончания строк: в репозитории только LF — в корне `.gitattributes` с `* text=auto eol=lf`. В Windows-копии файлы на диске могут быть
  CRLF: при правке скриптами сохранять окончания как в файле, не плодить смешанные; перед коммитом смотреть `git diff --stat`
  (раздутый diff = «переехали» окончания строк). Python в PowerShell — `python`, не `python3`.
- `.env` живёт ТОЛЬКО на сервере и внутри контейнера. В WSL и Windows его нет —
  скрипты вида `. ./.env` там молча ничего не подхватят.

## Доступ к серверу (SSH)
- На ПК Алексея (и в Windows, и в WSL) в `~/.ssh/config` есть SSH-алиас прод-сервера. Ниже он
  обозначен как `SSH_HOST`: настоящее имя алиаса задано в глобальных инструкциях Алексея
  (`~/.claude/CLAUDE.md`), подключение — `ssh <значение SSH_HOST>`. Если значение не задано —
  спросить Алексея, не угадывать. IP, пользователь и ключ живут только в локальном
  `~/.ssh/config`. Ни имя алиаса, ни их в репозиторий (он публичный) не переносить.
- **Заходить на сервер только с явного согласия или по просьбе Алексея в текущей сессии**
  («да, заходи на сервер» и т. п.), например когда его нет у компьютера. ТЗ или разрешение
  из прошлой сессии не считаются.
- Сначала только чтение (логи, `docker ps`, `grep` по файлам в контейнере). Перед любым
  изменением на сервере (рестарт, `git pull`, правка `.env`, запись в БД) — спросить ещё раз.
- Правило «не править файлы на сервере» действует и через SSH: код меняется только через git.
- Секреты из `.env` не печатать, не копировать в чат и в git; при проверке выводить длину.
- На том же сервере работает `dnd-mcp` (`~/dnd-mcp`, `~/dnd-mcp-staging`): его контейнеры,
  сети и тома не трогать.

## Workflow
1. Правки в WSL (`~/projects/claudushka`) или в Windows/PowerShell (`C:\Users\ukrai\projects\claudushka`) — см. «Пути»
2. Проверить синтаксис: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
3. Коммит → push в ветку → мерж в main
4. Деплой: `/update` в Telegram (пишет Алексей) или `git pull && docker restart claudushka`
   на сервере — **но это перезапускает только Telegram-сервис (`claudushka`)**.
   `claudushka-wa` (WhatsApp) **на паузе с 2026-09-19** (`profiles: ["whatsapp"]` в
   `docker-compose.yml`, токен Meta протух, каналом никто не пользуется) — обычный
   `docker compose up -d` его больше не поднимает и не перезапускает. Правки в
   `whatsapp.py` сейчас никуда не выкатываются, пока сервис не поднят явно
   (`docker compose --profile whatsapp up -d`) — см. `.claude/rules/whatsapp.md`.
   - Менялись `db.py` или `api_errors.py` (общие для обоих сервисов) или
     `requirements.txt` — `docker restart claudushka` достаточно: `command:`
     перевыполняет `pip install` при каждом старте процесса, пересоздавать
     контейнер не нужно.
   - Менялся сам `docker-compose.yml` (volumes, command, env) — `restart` и `/update` его НЕ
     применяют, нужен `docker compose up -d --force-recreate claudushka`. Живой случай:
     ТЗ v0.10 убрало mount `allowed.json` вместе с файлом — со старым контейнером `restart`
     мог не поднять сервис.
   - Менялся `.env` — `restart` его НЕ подхватит (переменные окружения читаются один
     раз при создании контейнера), нужен `docker compose up -d --force-recreate`.
     Подробности — `.claude/rules/docker-compose.md`.

## Мелкий фикс vs стенд

Не каждая правка требует прогона на тестовом стенде (`docs/claude/staging.md`) — стенд
обязателен для рискованных изменений (мажорные апдейты зависимостей, изменения схемы БД,
крупные рефакторинги хендлеров). Мелкий фикс — сразу в прод, без стенда, если ВСЕ условия
выполняются:
- TLS для dnd и Клодушки — общий стек `edge` (`infra/edge/`, контейнер `edge-caddy`, сеть `edge`; запускается только из
  `~/claudushka/infra/edge/`), переезд выполнен 2026-11-01 по `docs/claude/runbook-edge-proxy.md`. Его правка затрагивает dnd —
  только с согласия Алексея.
- Правка локальна: пара мест в существующей функции/регексе, не новый handler и не
  изменение схемы БД.
- Логику можно проверить локально (юнит-тест на функции/регексе вне бота вживую,
  `ast.parse`), не полагаясь только на ручной прогон в реальном чате.
- Откат тривиален (`git revert`/новый патч-коммит), без миграции данных назад.

Если хоть одно условие не выполняется — стенд. При сомнении — спросить Алексея, не
решать самостоятельно.

Стиль коммитов: `feat:`, `fix:`, `docs:`, `chore:`. Коротко и осмысленно — сообщения видны пользователям в `/update`.

## Открытые задачи
Вынесено в `docs/claude/open-tasks.md` — незакрытые TODO, не «известные баги» (открытых
известных багов нет, см. `docs/claude/incidents.md` для закрытых).

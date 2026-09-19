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
   для каждого изменённого .py.
5. **Версия предложена по SemVer** с обоснованием и готовым текстом для тега.
   Тег ставит Алексей вручную — не создавать самостоятельно.
6. **Отчёт о том, что НЕ сделано**: если что-то отложено или не проверено — сказать явно,
   а не умолчать.

Если правка затрагивает system-prompt, память, историю или лимиты — обязательно
оценить влияние на размер контекста и написать об этом в отчёте.

## Тематические файлы — где что искать

Это ядро; подробности вынесены в тематические файлы (не `@`-импорты — грузятся только
когда сам их откроешь). Перед правкой соответствующей области — сначала прочитай файл:

- Рисование (`_try_gemini_image`, `_try_gpt_image`, `_draw_and_send`, `DRAW_MARKER_RE`,
  `LEAKED_DRAW_RE`, `IMAGE_PROVIDERS`, `/imagine` `/banana` `/gptimage` `/imagemodels`) —
  `docs/claude/images.md`
- Групповой чат (`build_group_messages`, `group_messages`, гейтинг доступа,
  `/ratelimit`, `is_chat_admin`) — `docs/claude/group-chat.md`
- Голос/видео/кружочки/GIF (`_transcribe_audio_gemini`, `_extract_video_frames`,
  `_describe_media_haiku`, ветки `voice`/`video_note`/`video`/`animation`) —
  `docs/claude/media.md`
- Дневной обзор (`daily_chat_review`, `cmd_review`, `/review_on`, `/review_off`,
  `run_daily`, `BERLIN_TZ`) — `docs/claude/daily-review.md`
- Модели и стоимость (`MODELS`, `/haiku /sonnet /opus /fable`, `/models`, `/cost`,
  `_track_response`) — `docs/claude/models-and-costs.md`
- Ошибки API, ретраи, блокирующие вызовы, `response_text`, `parse_json_lenient` —
  `docs/claude/api-errors.md`
- Память (`memory`-таблица, `extract_memory`, `extract_all_participants_memory`,
  `/memory`, `/memory_full`, `/forget`) — `docs/claude/memory.md`
- История инцидентов с датами (полные дословные разборы) — `docs/claude/incidents.md`
  — сверься, если баг похож на уже виденный, прежде чем чинить с нуля
- Открытые задачи (незакрытое, не баги) — `docs/claude/open-tasks.md`
- `whatsapp.py` — `.claude/rules/whatsapp.md` (грузится автоматически при чтении файла)
- `db.py` — `.claude/rules/db.md` (автоматически)
- `docker-compose.yml` — `.claude/rules/docker-compose.md` (автоматически)
- Тестовый стенд (`docker-compose.test.yml`, обновление зависимостей, рискованные
  правки перед продом) — `docs/claude/staging.md`

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 22.8 (мажорное обновление с 21.10 — 2026-09-19, см.
  `docs/claude/staging.md` про прогон на тестовом стенде перед продом)
- anthropic 1.7.0 (мажорное обновление с 0.43.0 — та же правка; HTTP-слой SDK теперь на
  `httpx2`, ОТДЕЛЬНОМ пакете от обычного `httpx`, конфликта версий нет, см.
  `docs/claude/api-errors.md` про смену иерархии исключений 5xx) — реестр моделей
  `MODELS` в bot.py, `/haiku /sonnet /opus /fable`. Подробности —
  `docs/claude/models-and-costs.md`.
- tavily-python 0.8.4 — веб-поиск (`/search`)
- Генерация изображений — `/imagine`, DRAW-маркер, провайдеры banana/GPT Image 2.
  Подробности — `docs/claude/images.md`.
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
- bot.py — основной код Telegram-бота (~3200 строк)
- db.py — слой данных SQLite (пользователи, история, память, чаты) — `.claude/rules/db.md`
- api_errors.py — классификация ошибок Anthropic API, ретраи, сообщения пользователю
  (общий для bot.py и whatsapp.py) — `docs/claude/api-errors.md`
- whatsapp.py — WhatsApp-бот (FastAPI webhook, отдельный сервис; **сервис на паузе с
  2026-09-19**, код и правила не трогали) — `.claude/rules/whatsapp.md`
- allowed.json — белые списки пользователей и чатов (legacy, основной источник — SQLite)
- docker-compose.yml — два сервиса: claudushka + claudushka-wa (WhatsApp, `profiles:`,
  на паузе) — `.claude/rules/docker-compose.md`
- .env — секреты (не в git)
- requirements.txt — все зависимости
- dedup_memory.py — ручная дедупликация фактов памяти, см. `docs/claude/open-tasks.md`

## Инварианты (коротко — подробности в тематических файлах)
- Никаких блокирующих вызовов API/HTTP из event loop — `await asyncio.to_thread(...)`
  или выделенные обёртки (`call_claude`/`aux_create`/`sync_create`). `docs/claude/api-errors.md`.
- Фоновые fire-and-forget задачи — только через `_spawn_background_task`, не голый
  `asyncio.create_task` (иначе задачу может собрать GC до завершения). `docs/claude/api-errors.md`.
- Разбор ответа Claude — только `api_errors.response_text()`, никогда `content[0].text`.
- JSON из ответа модели — только `api_errors.parse_json_lenient()`, никогда `find`/`rfind`.
- Ответы бота в группе сохраняются в `group_messages` с `is_bot=True` во всех ветках
  (текст/фото/документ/рисование/обзор) — иначе Клодушка не видит собственные реплики.
- Любой источник данных, попадающий в system-prompt или `messages`, обязан иметь лимит
  по РАЗМЕРУ (не только по числу элементов) — см. инцидент 2026-07-26 в `docs/claude/incidents.md`.
- Каждый новый `.py`-файл, который импортируют bot.py/whatsapp.py — добавить в volume
  mounts ОБОИХ сервисов в `docker-compose.yml`. `.claude/rules/docker-compose.md`.
- Уровень логов httpx не поднимать до INFO — токен бота утекает в URL.
- Пользователь никогда не видит сырой текст исключения — только `api_errors.reply_api_error`.
- Служебные вызовы (капча, поиск, перевод промпта, извлечение памяти, описание медиа) —
  только `DEFAULT_MODEL_ID`; ответы пользователю — `get_chat_model(chat_id)`.
  `MODELS["..."]["id"]` в новых вызовах не хардкодить. `docs/claude/models-and-costs.md`.

## Команды (актуальный список)
`/help` показывает всем пользователям базовые команды, adminам — полный список из двух блоков (`USER_HELP` + `ADMIN_HELP` в bot.py). При добавлении новой команды обновлять оба константы.

## Версионность
Текущая версия: v0.13.0 (последний тег; security/tz-фиксы от 2026-09-19 — `d71d7c9`,
`a9912bc`, `b72e96e` — уже смёржены в main, но тег на них сознательно не ставили — ждём
завершения остальной работы дня и один общий тег). Теги обычно ставит Алексей вручную:
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
- Новые зависимости добавлять осознанно, минимизировать
- Переменные окружения через .env и docker-compose env_file
- Белые списки через allowed.json и БД, НЕ через .env
- Контейнер без Dockerfile, используем image + command
- Данные (SQLite, data/) монтируются в /app/data, не в git
- Не правь файлы напрямую на сервере — это ведёт к `-dirty` версии и расхождению с git

## Пути (не перепутать)
- Windows: `C:\Claude\claudushka`
- WSL: `~/projects/claudushka` ← отсюда коммиты и push
- Сервер: `~/claudushka` ← только `git pull` и деплой, файлы не править
- `.env` живёт ТОЛЬКО на сервере и внутри контейнера. В WSL и Windows его нет —
  скрипты вида `. ./.env` там молча ничего не подхватят.

## Workflow
1. Правки в WSL (`~/projects/claudushka`)
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
   - Менялся `.env` — `restart` его НЕ подхватит (переменные окружения читаются один
     раз при создании контейнера), нужен `docker compose up -d --force-recreate`.
     Подробности — `.claude/rules/docker-compose.md`.

Стиль коммитов: `feat:`, `fix:`, `docs:`, `chore:`. Коротко и осмысленно — сообщения видны пользователям в `/update`.

## Открытые задачи
Вынесено в `docs/claude/open-tasks.md` — незакрытые TODO, не «известные баги» (открытых
известных багов нет, см. `docs/claude/incidents.md` для закрытых).

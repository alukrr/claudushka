# Клодушка — Telegram-бот с Claude API

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 21.10
- anthropic 0.43.0 (модели: claude-haiku-4-5-20251001 по умолчанию для всех диалогов; per-chat переключение через /haiku /sonnet /opus; вспомогательные вызовы — всегда Haiku)
- tavily-python 0.5.0 — веб-поиск
- генерация изображений: Gemini «Nano Banana 2» (`gemini-3.1-flash-image-preview`) через `requests`, ЕДИНСТВЕННЫЙ провайдер. При отказе банана промпт переписывается через Haiku и банан пробуется снова; фоллбека на FLUX больше нет (выпилен: качество + FLUX.1-schnell стал gated, а годные модели 2026 ушли с бесплатного hf-inference на платные провайдеры). При неудаче — честная ошибка пользователю.

### Рисование: два пути входа
Вся генерация+отправка картинки инкапсулирована в `_draw_and_send(update, context, chat_id, is_group, draw_prompt, en_prompt=None, author=None)`. Если `en_prompt` не передан — `draw_prompt` переводится на английский через Haiku. Два способа запустить рисование:
1. **Команда «нарисуй …»** (`DRAW_TRIGGERS`) — явный запрос пользователя. Вызывает `_draw_and_send` с русским `draw_prompt`, переводом и автором.
2. **Маркер `[[DRAW: english prompt]]`** — Клодушка сама решает нарисовать внутри текстового ответа. В system-prompt ей сказано: НЕ писать «нарисовала» просто так (иначе картинка не появится), а добавлять маркер в конец ответа. `DRAW_MARKER_RE` парсит ответ в main conversation: маркер вырезается из текста (пользователь его не видит), остаток отправляется как реплика, затем `_draw_and_send` реально рисует. Если весь ответ был маркером — текст не шлётся, в группу пишет `_draw_and_send`. ЭТО ФИКС бага «Клодушка верит что нарисовала, но никуда не отправляет». Маркер обрабатывается только в текстовой ветке main conversation, не в фото/документах.
- fastapi 0.115.0 + uvicorn 0.30.0 — WhatsApp webhook
- httpx 0.27.0
- SQLite (через stdlib sqlite3, обёртка в db.py)
- Docker Compose (без Dockerfile)

## Структура
- bot.py — основной код Telegram-бота (~1960 строк)
- db.py — слой данных SQLite (пользователи, история, память, чаты)
- whatsapp.py — WhatsApp-бот (FastAPI webhook, отдельный сервис)
- allowed.json — белые списки пользователей и чатов (legacy, основной источник — SQLite)
- docker-compose.yml — два сервиса: claudushka + claudushka-wa (WhatsApp)
- .env — секреты (не в git)
- requirements.txt — все зависимости
- fix_premium.py — одноразовый скрипт миграции ролей

## Групповой чат (мультиперсональный контекст)
В группах бот — участник беседы нескольких людей, а не личный диалог 1:1.
- Контекст для модели строит `build_group_messages()` (bot.py) из `db.get_group_transcript(chat_id, GROUP_TRANSCRIPT_LIMIT)` (лимит = 50): реплики людей → user-блоки с подписью «Имя: текст» (подряд идущие склеиваются), ответы Клодушки → assistant-блоки. Гарантируется чередование ролей и user первым. Текущее сообщение-триггер уже лежит в `group_messages` (сохранено в начале `handle_message`) и идёт последним user-turn — повторно НЕ добавляется (иначе вернётся баг «ты уже говорила»).
- Групповой контекст идёт в `messages`, НЕ в system-prompt. Старый дамп `get_group_history` в system удалён — не возвращай его обратно, иначе модель снова начнёт сливать собеседников в одного.
- Контекст реплая привязывается inline к текущей реплике (`[в ответ на сообщение Имя: «…»]`), а не в system.
- Ответы Клодушки во всех ветках (текст / фото / документ / рисование) сохраняются в `group_messages` с `is_bot=True` — чтобы она видела собственные реплики.
- Инструкции про групповой режим — в `get_system_prompt(..., is_group=True)`.
- Личка (ветка `else` в `handle_message`) и WhatsApp работают по-старому (личный тред) — их не трогать.
- ГЕЙТИНГ ДОСТУПА разный для группы и лички (намеренно):
  - Группа — на уровне ЧАТА: если `WHITELIST_ENABLED` и чат не `approved` → молчим; иначе отвечаем ВСЕМ участникам. Никакой персональной капчи/допуска/дневного лимита по `user_id` в группе. Бан (`role='banned'`) — глобальный, проверяется в начале `handle_message` и действует везде.
  - Личка — персональный гейтинг: `needs_captcha` (admin-approval), `is_allowed_in_chat`, `check_daily_limit` (лимит для street).
  - Веб-поиск (`can_search`) остаётся привилегией роли и в группе тоже (стоит денег). Если захочется «в разрешённом чате ищут все» — снимать гейт отдельно.
  - `handle_captcha()` (интерактивная капча) — мёртвый код, нигде не вызывается; активен только admin-approval путь.
- Схема: `group_messages.is_bot INTEGER NOT NULL DEFAULT 0`, миграция идемпотентная в `init_db()`. Сигнатура `save_group_message(chat_id, user_id, sender_name, content, is_bot=False)`.
- `db.get_group_history()` (строки «Имя: текст») оставлена специально — её используют `daily_chat_review` и `cmd_review`. Не удалять и не путать с `get_group_transcript()`.

## Модели per-chat
Дефолт всех чатов — `claude-haiku-4-5-20251001`. Выбор хранится в таблице `chat_models` (SQLite), переживает рестарты.
- `db.get_chat_model_db(chat_id)` → возвращает модель (дефолт haiku если записи нет)
- `db.set_chat_model_db(chat_id, model)` → сохраняет выбор
- `get_chat_model(chat_id)` в bot.py — обёртка над db
- Команды `/haiku [chat_id]`, `/sonnet [chat_id]`, `/opus [chat_id]` — в чате без аргумента, из лички с ID
- Вспомогательные вызовы (капча, should_search, translate, greet, extract_memory группы) — всегда Haiku, не зависят от chat_model
- Token tracking: `_track_tokens(model, inp, out)` + словарь `token_usage: dict[str, dict]`. Цены: Haiku $0.80/$4, Sonnet $3/$15, Opus $15/$75 за MTok (in/out)
- `daily_chat_review` и `cmd_review` используют модель чата (не хардкод)

## Команды (актуальный список)
`/help` показывает всем пользователям базовые команды, adminам — полный список из двух блоков (`USER_HELP` + `ADMIN_HELP` в bot.py). При добавлении новой команды обновлять оба константы.

## Версионность
Текущая версия: v0.7.0. Теги ставит Алексей вручную: `git tag -a vX.Y.Z`. Не создавай теги самостоятельно.
Версия доступна через `/version` в боте. Команды `cmd_update` и `cmd_version` используют хелпер `_git()` — не переписывай их без необходимости.

## Правила
- Новые зависимости добавлять осознанно, минимизировать
- Переменные окружения через .env и docker-compose env_file
- Белые списки через allowed.json и БД, НЕ через .env
- Контейнер без Dockerfile, используем image + command
- Данные (SQLite, data/) монтируются в /app/data, не в git
- Не правь файлы напрямую на сервере — это ведёт к `-dirty` версии и расхождению с git

## Критичные блоки docker-compose.yml
Не удалять, не упрощать:
- `environment: GIT_CONFIG_COUNT=1` + `safe.directory=/repo` — без этого git внутри контейнера не работает
- Монтирование `.:/repo` и `/var/run/docker.sock` — нужно для self-update
- Установка `git` и `docker.io` в `command:` — нужна там же

Remote на сервере переключён на HTTPS (`https://github.com/alukrr/claudushka.git`), в WSL остался SSH. Это сделано осознанно.

## Известные баги
Все ранее перечисленные баги устранены: дубль `daily_chat_review`, дубль `run_daily` в `main()`, инвертированные `cmd_whitelist_on` / `cmd_captcha_on` (теперь ставят `True`). Открытых известных багов нет — не «чини» эти места повторно.

## Трёхуровневая память (секретность личка/группа)

### Схема
Факты хранятся в таблице `memory` (`context` + `chat_id` + `tier` + `expires_at`):
- `context='private', chat_id=NULL` — сказано боту в личке.
- `context='group', chat_id=<id>` — узнано в конкретной группе.
- `tier='long'`, `expires_at=NULL` — долгосрочные факты (не протухают).
- `tier='medium'`, `expires_at=<unix+7d>` — среднесрочные, TTL 7 дней. Автоматически исключаются из выборок после истечения.

### Три уровня (группа)
1. **Краткосрочная** — `group_messages` (последние 50 реплик) → `build_group_messages()` → `messages` в API. Не персонализирована, весь чат.
2. **Среднесрочная** (`tier='medium'`) — события и временные состояния этой недели (попала под дождь, поругалась с Валерой, купила клубнику). TTL 7 дней.
3. **Долгосрочная** (`tier='long'`) — устойчивые факты о людях: кто они, где живут, чем занимаются, интересы, возраст, взгляды. Без TTL.

### Извлечение
- **Личка** — `extract_memory()` по личному треду (каденс: `len(messages) % (MEMORY_EXTRACT_EVERY*2) == 0`). Модель Sonnet (hardcoded — осознанно, это аналитическая задача).
- **Группа** — `extract_all_participants_memory(chat_id)`: один вызов Haiku на весь 50-реплик транскрипт, извлекает **оба уровня** сразу для **ВСЕХ видимых участников**. Атрибуция: по `sender_name` → `user_id` через `db.get_user_id_by_name_in_chat()`. Каденс: `db.count_chat_messages(chat_id) % MEMORY_EXTRACT_EVERY_CHAT == 0` (каждые 10 сообщений в чате). Вызывается **до** проверки `is_bot_mentioned` — работает даже когда бота не упоминают.

### Асимметрия чтения (намеренная, не баг)
- В личке `get_system_prompt(is_group=False)` зовёт `db.get_memory_for_private(user_id)` — личные факты ПЛЮС все групповые факты про этого человека из всех чатов (с дедупом). Группа течёт вверх.
- В группе `get_system_prompt(is_group=True)` зовёт `db.get_all_chat_memory(chat_id)` — факты **всех участников** ЭТОГО чата (оба tier, без просроченных). Личное в группу НЕ течёт, другие группы — тоже (изоляция по `chat_id`).

### Команды
- `/memory` — показывает факты текущего пользователя (в группе — только его факты этого чата; в личке — объединённое).
- `/forget` — чистит ВСЮ память юзера (и личное, и групповое во всех чатах).

### Важно
- `db.get_group_history()` (строки «Имя: текст») оставлена специально — её используют `daily_chat_review` и `cmd_review`. Не путать с `get_group_transcript()` и не удалять.
- Старый `extract_group_memory` удалён, заменён на `extract_all_participants_memory`. Не возвращать старый.

## Workflow
1. Правки в WSL (`~/claudushka`)
2. Проверить синтаксис: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
3. Коммит → push в ветку → мерж в main
4. Деплой: `/update` в Telegram (пишет Алексей) или `git pull && docker restart claudushka` на сервере

Стиль коммитов: `feat:`, `fix:`, `docs:`, `chore:`. Коротко и осмысленно — сообщения видны пользователям в `/update`.
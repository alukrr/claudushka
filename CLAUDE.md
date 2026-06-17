# Клодушка — Telegram-бот с Claude API

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 21.10
- anthropic 0.43.0 (модели: claude-sonnet-4-6 для диалогов, claude-haiku-4-5-20251001 для капчи/вспомогательных задач)
- tavily-python 0.5.0 — веб-поиск
- генерация изображений: Gemini «Nano Banana 2» (`gemini-3.1-flash-image-preview`) через `requests`, ЕДИНСТВЕННЫЙ провайдер. При отказе банана промпт переписывается через Haiku и банан пробуется снова; фоллбека на FLUX больше нет (выпилен: качество + FLUX.1-schnell стал gated, а годные модели 2026 ушли с бесплатного hf-inference на платные провайдеры). При неудаче — честная ошибка пользователю.
- fastapi 0.115.0 + uvicorn 0.30.0 — WhatsApp webhook
- httpx 0.27.0
- SQLite (через stdlib sqlite3, обёртка в db.py)
- Docker Compose (без Dockerfile)

## Структура
- bot.py — основной код Telegram-бота (~1600 строк)
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
- Схема: `group_messages.is_bot INTEGER NOT NULL DEFAULT 0`, миграция идемпотентная в `init_db()`. Сигнатура `save_group_message(chat_id, user_id, sender_name, content, is_bot=False)`.
- `db.get_group_history()` (строки «Имя: текст») оставлена специально — её используют `daily_chat_review` и `cmd_review`. Не удалять и не путать с `get_group_transcript()`.

## Версионность
Текущая версия: v0.4.0. Теги ставит Алексей вручную: `git tag -a vX.Y.Z`. Не создавай теги самостоятельно.
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

## Память по слоям (секретность личка/группа)
Факты про человека хранятся слоями в таблице `memory` (`context` + `chat_id`):
- `context='private', chat_id=NULL` — сказано боту в личке.
- `context='group', chat_id=<id>` — узнано в конкретной группе.

Асимметрия чтения (намеренная, не баг):
- В личке `get_system_prompt(is_group=False)` зовёт `db.get_memory_for_private(user_id)` — личные факты ПЛЮС все групповые факты про этого человека из всех чатов (с дедупом). Группа течёт вверх.
- В группе `get_system_prompt(is_group=True)` зовёт `db.get_memory(user_id, 'group', chat_id)` — только факты ЭТОГО чата. Личное в группу НЕ течёт, другие группы — тоже (изоляция по `chat_id`).

Извлечение:
- Личка — `extract_memory()` по личному треду (каденс: `len(messages) % (MEMORY_EXTRACT_EVERY*2) == 0`).
- Группа — `extract_group_memory(user_id, sender_name, chat_id)`: извлекает факты ТОЛЬКО про текущего автора (атрибуция по известному `user_id`, чужие факты не приписываются), хранит как групповые с `chat_id`. Каденс: `db.count_user_messages_in_chat() % MEMORY_EXTRACT_EVERY == 0`. Модель — Sonnet (корректность атрибуции важнее цены).
- `/memory` показывает то, что бот реально использует: в личке — объединённое, в группе — факты этого чата.
- `/forget` чистит ВСЮ память юзера (и личное, и групповое во всех чатах) — это согласуется с тем, что личка и так видит всё.

Дальше (не сделано, при желании): chat-level память об отношениях участников (кто с кем, динамика), а не только пофактовая по людям.

## Workflow
1. Правки в WSL (`~/claudushka`)
2. Проверить синтаксис: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
3. Коммит → push в ветку → мерж в main
4. Деплой: `/update` в Telegram (пишет Алексей) или `git pull && docker restart claudushka` на сервере

Стиль коммитов: `feat:`, `fix:`, `docs:`, `chore:`. Коротко и осмысленно — сообщения видны пользователям в `/update`.
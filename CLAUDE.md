# Клодушка — Telegram-бот с Claude API

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 21.10
- anthropic 0.43.0 (модели: claude-sonnet-4-6 для диалогов, claude-haiku-4-5-20251001 для капчи/вспомогательных задач)
- tavily-python 0.5.0 — веб-поиск
- gradio_client 1.5.0 — FLUX (HuggingFace) для генерации изображений
- fastapi 0.115.0 + uvicorn 0.30.0 — WhatsApp webhook
- httpx 0.27.0
- requests — HTTP для Gemini image API
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

## Известные баги (фиксить по одному)
- `daily_chat_review` определена дважды в bot.py — удалить одну
- В `main()` блок `app.job_queue.run_daily(daily_chat_review, ...)` дублируется — убрать дубликат
- `cmd_whitelist_on` ставит `WHITELIST_ENABLED = False` вместо `True`
- `cmd_captcha_on` ставит `CAPTCHA_ENABLED = False` вместо `True`

## Workflow
1. Правки в WSL (`~/claudushka`)
2. Проверить синтаксис: `python3 -c "import ast; ast.parse(open('bot.py').read())"`
3. Коммит → push в ветку → мерж в main
4. Деплой: `/update` в Telegram (пишет Алексей) или `git pull && docker restart claudushka` на сервере

Стиль коммитов: `feat:`, `fix:`, `docs:`, `chore:`. Коротко и осмысленно — сообщения видны пользователям в `/update`.

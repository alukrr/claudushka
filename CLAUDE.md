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
- bot.py — основной код Telegram-бота (~1500 строк)
- db.py — слой данных SQLite (пользователи, история, память, чаты)
- whatsapp.py — WhatsApp-бот (FastAPI webhook, отдельный сервис)
- allowed.json — белые списки пользователей и чатов (legacy, основной источник — SQLite)
- docker-compose.yml — два сервиса: claudushka + claudushka-wa (WhatsApp)
- .env — секреты (не в git)
- requirements.txt — все зависимости
- fix_premium.py — одноразовый скрипт миграции ролей

## Правила
- Новые зависимости добавлять осознанно, минимизировать
- Переменные окружения через .env и docker-compose env_file
- Белые списки через allowed.json и БД, НЕ через .env
- Контейнер без Dockerfile, используем image + command
- Данные (SQLite, data/) монтируются в /app/data, не в git


Брат, читай прежде чем что-то трогать.
В репе появилась версионность. Текущая версия: v0.4.0. Теги ставит Алексей вручную через git tag -a vX.Y.Z, не выдумывай свои.
Что НЕ надо ломать:

cmd_update и cmd_version в bot.py — работают, протестированы. Используют helper _git() для вызовов git в /repo. Не переписывай "по красоте", не оборачивай в try/except ради try/except — там логика возврата кодов нужна как есть.
docker-compose.yml — там критичные блоки:

environment: GIT_CONFIG_COUNT=1 + safe.directory=/repo — без этого git внутри контейнера не работает
Монтирование .:/repo и /var/run/docker.sock — нужно для self-update
Установка git и docker.io в command: — тоже нужно

На сервере remote переключён на HTTPS (https://github.com/alukrr/claudushka.git), в WSL остался SSH. Это сделано осознанно — не "унифицируй".
Файл 26.0.1 в корне репы на GitHub — мусор, надо удалить отдельным коммитом chore: remove stray file. Но никаких "заодно почищу всё" — только этот файл.

Известные баги, которые можно фиксить (по одному, не пачкой):

daily_chat_review определена дважды в bot.py — оставить одну
В main() блок app.job_queue.run_daily(daily_chat_review, ...) дублируется — убрать дубликат
cmd_whitelist_on ставит WHITELIST_ENABLED = False вместо True — починить
cmd_captcha_on ставит CAPTCHA_ENABLED = False вместо True — починить

Workflow:

Правки делаешь в WSL (~/claudushka)
Коммит → push в свою ветку → мерж в main через PR или локально → push main
На сервере деплой через /update в Telegram (пишет Алексей) или вручную: git pull && docker restart claudushka
Перед коммитом проверяй синтаксис: python3 -c "import ast; ast.parse(open('bot.py').read())"
Не правь на сервере — это уже один раз привело к -dirty версии и расхождению с git

Стиль коммитов: feat:, fix:, docs:, chore:. Пиши коротко и осмысленно — эти сообщения теперь видны в /update пользователям.
Если сомневаешься — спроси Алексея, не догадывайся.

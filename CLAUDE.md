# Клодушка — Telegram-бот с Claude API

## Стек
- Python 3.12 (python:3.12-slim Docker image)
- python-telegram-bot 21.10
- anthropic 0.43.0
- Docker Compose (без Dockerfile)

## Структура
- bot.py — основной код бота
- allowed.json — белые списки пользователей и чатов
- docker-compose.yml — запуск через compose
- .env — секреты (не в git)
- requirements.txt — зависимости (только python-telegram-bot и anthropic)

## Правила
- НЕ добавлять лишних зависимостей (requests, dotenv и т.д.)
- Переменные окружения через .env и docker-compose env_file
- Белые списки через allowed.json, НЕ через .env
- Контейнер без Dockerfile, используем image + command

# Клодушка 🤖

Персональный Telegram-бот на базе Claude API (Anthropic). Создан для себя и друзей.

## Что умеет

- Общение на русском, немецком и английском языках
- Запоминает факты о собеседнике между сессиями
- Хранит историю диалогов (переживает рестарты)
- AI-капча для новых пользователей (загадки на языке собеседника)
- Белые списки пользователей и чатов с управлением через Telegram
- Саркастичный характер, без лишних фильтров (18+)

## Статус

- ✅ Личные чаты — работает
- 🚧 Групповые чаты — в разработке

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Начало работы |
| `/clear` | Очистить историю диалога |
| `/memory` | Что бот помнит о тебе |
| `/forget` | Стереть всё о тебе |
| `/id` | Показать Telegram ID |

Админ-команды: `/whitelist`, `/whitelist_on`, `/whitelist_off`, `/captcha_on`, `/captcha_off`, `/allow_user`, `/deny_user`, `/allow_chat`, `/deny_chat`, `/captcha_unban`, `/reload`

## Стек

- Python 3.12
- python-telegram-bot 21.10
- Anthropic Claude API (Sonnet для диалогов, Haiku для капчи)
- Docker Compose
- Hetzner Cloud (CX23, Nürnberg)

## Запуск
```bash
cp .env.example .env
# заполни .env своими ключами
docker compose up -d
```

## Автор

[@alukr](https://t.me/alukr) — DevOps-инженер, Buchholz in der Nordheide, Германия

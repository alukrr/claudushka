---
paths: ["docker-compose.yml"]
---

# docker-compose.yml

Два сервиса: `claudushka` (Telegram-бот) + `claudushka-wa` (WhatsApp, FastAPI). Оба —
`image: python:3.12-slim` без Dockerfile, зависимости ставятся в `command:` при старте.

## Критичные блоки — не удалять, не упрощать
- `environment: GIT_CONFIG_COUNT=1` + `safe.directory=/repo` — без этого git внутри
  контейнера не работает.
- Монтирование `.:/repo` и `/var/run/docker.sock` — нужно для self-update (`/update`
  в Telegram запускает `git pull` и рестарт контейнера изнутри).
- Установка `git` и `docker.io` в `command:` — нужна там же, для self-update.
- Установка `ffmpeg` в той же строке (с v0.11.0) — нужна для извлечения кадров из
  видео/кружочков, см. `docs/claude/media.md`.

Remote на сервере переключён на HTTPS (`https://github.com/alukrr/claudushka.git`), в
WSL остался SSH. Это сделано осознанно.

## Правило: каждый новый `.py`-файл — в volume mounts ОБОИХ сервисов
Оба сервиса монтируют только явно перечисленные файлы (`./bot.py:/app/bot.py` и т.п.),
не всю директорию целиком. Добавляешь новый `.py`-модуль, который импортируется
bot.py и/или whatsapp.py (по образцу `api_errors.py`, `db.py`) — обязательно добавь
его volume mount в `docker-compose.yml` для КАЖДОГО сервиса, который его использует.
Забытый mount — контейнер стартует со старой (или отсутствующей) версией файла молча,
без явной ошибки при деплое.

## `requirements.txt` меняется → нужен `--force-recreate`
Оба сервиса ставят зависимости через `pip install` в самом `command:` при каждом старте
контейнера. `docker restart` НЕ пересоздаёт контейнер и переиспользует старый layer —
если менялся `requirements.txt`, нужен `docker compose up -d --force-recreate`, иначе
новые пакеты не подтянутся.

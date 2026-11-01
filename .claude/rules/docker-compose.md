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
  в Telegram запускает `git pull` и рестарт контейнера изнутри). У `claudushka` нет
  `user:` в compose — процесс в контейнере root, значит `git pull` изнутри пишет новые
  файлы и git-объекты на bind-mount root-owned'ыми — `cmd_update` (bot.py) после
  успешного pull сама возвращает владельца (`_fix_repo_ownership`, только `.git` и
  файлы из `git ls-files`, `data/` не трогает) — не убирать этот шаг, живой инцидент
  2026-09-19 в `docs/claude/incidents.md`.
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

## Новый volume (`./personas:/app/personas`) — только `--force-recreate`, не `/update`
`restart` и `/update` состав volumes не применяют. Папка `personas/` (тексты режимов характера) читается ботом при старте
(`check_personas`, fail-fast): релиз, добавивший mount, выкатывать `docker compose up -d --force-recreate`
(`docs/claude/personas.md`, `staging-checklist-personas.md`). Mount добавлен в оба сервиса и в `docker-compose.test.yml`.

## `requirements.txt` меняется → хватает обычного `restart`
Оба сервиса ставят зависимости через `pip install` в самом `command:`, а `command:`
выполняется заново при КАЖДОМ старте процесса в контейнере — `docker restart` тоже
запускает `command:` с нуля, значит `pip install` перечитывает уже смонтированный
свежий `requirements.txt` и подтягивает новые пакеты. Пересоздавать контейнер
(`--force-recreate`) для этого не нужно.

## `.env` меняется → нужен `docker compose up -d --force-recreate`
А вот переменные окружения (`environment`/`env_file` в `docker-compose.yml`) читаются
Docker ОДИН РАЗ — при создании контейнера, не при каждом старте процесса внутри него.
`docker restart` перезапускает процесс в уже существующем контейнере со старым
окружением, поэтому правки `.env` он не подхватывает. Нужно пересоздать контейнер:
`docker compose up -d --force-recreate` (или `up -d`, если состав `docker-compose.yml`
тоже менялся — тогда пересоздание триггерится и без явного флага).

## `infra/edge/` — общий прокси (отдельный compose-проект `edge`)
`infra/edge/docker-compose.yml` — НЕ часть compose Клодушки: отдельный проект `name: edge` (контейнер `edge-caddy`, порты 80/443, внешняя сеть
`edge` и внешние тома `edge_caddy_data`/`edge_caddy_config`, создаются вручную). Запускается только из `~/claudushka/infra/edge/`. Он общий для
dnd-mcp и Клодушки, поэтому его правка затрагивает dnd — только с согласия Алексея. Для доступа через прокси сервису Клодушки понадобится сеть
`edge` (`external: true`) — это делает `feat/miniapp`. Подробности и порядок перехода — `docs/claude/runbook-edge-proxy.md`, `docs/claude/staging.md`.

## Мини-приложение: том `./miniapp` и внешняя сеть `edge` — только `--force-recreate`
В `docker-compose.yml` (`claudushka`) и `docker-compose.test.yml` (`claudushka-test`) добавлены том `./miniapp:/app/miniapp` (`miniapp/server.py` + `index.html`;
каталог целиком, отдельный mount для `server.py` не нужен), сети `default` + `edge` и верхнеуровневая `networks: edge: external: true` (сеть создана при
развёртывании прокси, `docs/claude/runbook-edge-proxy.md`; без неё `docker compose up` не стартует). Порт 8090 НЕ публикуется — только `edge-caddy` →
`claudushka:8090` / `claudushka-test:8090`. Релиз с новым томом/сетью и переменными `MINIAPP_*` — только `docker compose up -d --force-recreate claudushka`,
`restart` и `/update` их не применяют (как с `personas`). Включается переменной `MINIAPP_PUBLIC_URL` в `.env`; `claudushka-wa` не затронут. `docs/claude/miniapp.md`.

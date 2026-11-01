# Runbook: общий прокси `edge` для dnd и Клодушки

Решение Алексея 2026-11-01 (`docs/claude/roadmap-2026-11.md`, п.1): Caddy выносится из стека `dnd-mcp` в свой маленький
стек `edge`, к которому по общей docker-сети `edge` подключаются оба проекта. Нужно для мини-приложения
(`docs/claude/tz-miniapp.md`): Telegram открывает мини-приложение только по HTTPS, а 80/443 сейчас держит `dnd-caddy`.

**Это разовое исключение из правила CLAUDE.md «контейнеры, сети и тома dnd-mcp не трогать».** Каждый шаг на сервере
выполняет Алексей или исполнитель с явным разрешением Алексея в текущей сессии; перед шагом с простоем — ещё раз спросить.

## Что есть сейчас (прочитано на сервере 2026-11-01, только чтение)
- `dnd-caddy` (`caddy:2`) из `~/dnd-mcp/docker-compose.yml`: порты `80:80`, `443:443`, тома `dnd-mcp_caddy_data` (сертификаты),
  `dnd-mcp_caddy_config`, конфиг `~/dnd-mcp/Caddyfile` (в git dnd-mcp, чистый): один сайт `{$DND_PUBLIC_HOST}` =
  `dnd.alukr.de` → `reverse_proxy dnd-mcp:8000`. Сеть `dnd-mcp_default`.
- `dnd-mcp` — без опубликованных портов, только через Caddy.
- `~/dnd-mcp-staging` — свой compose с сервисом `caddy` и тем же `container_name: dnd-caddy` и портами 80/443; сейчас НЕ запущен.
  Если его запустить при работающем `edge` — конфликт портов. Его compose правится так же (шаг 5) или стенд dnd поднимается без caddy.
- `claudushka`, `claudushka-test` — сети `claudushka_default` / `claudushka-test_default`, портов нет.
- `ufw` неактивен. Есть ли Hetzner Cloud Firewall — не проверялось (порты 80/443 и так открыты для dnd).

## Целевая схема
```
Интернет :80/:443 → edge-caddy (стек edge, сеть edge)
    dnd.alukr.de               → dnd-mcp:8000            (dnd-mcp подключён к сети edge)
    claudushka.alukr.de        → claudushka:8090         (появится с feat/miniapp)
    claudushka-test.alukr.de   → claudushka-test:8090    (стенд)
```
Файлы стека — в репозитории Клодушки, `infra/edge/` (`docker-compose.yml`, `Caddyfile`, `.env.example`); на сервере
запускается ТОЛЬКО из `~/claudushka/infra/edge/` (не из `~/claudushka-test`). `infra/edge/.env` с именами хостов — не в git
(`.gitignore`). Имя compose-проекта зафиксировано `name: edge`.

## Шаги
0. **Подготовить файлы** (исполнитель, коммит в репо Клодушки, ветка `infra/edge`): 
   - `infra/edge/docker-compose.yml`: сервис `caddy`, `image: caddy:2` (тот же, что сейчас), `container_name: edge-caddy`,
     `restart: unless-stopped`, порты `80:80`, `443:443`, `443:443/udp`, `env_file: .env`, тома `./Caddyfile:/etc/caddy/Caddyfile:ro`,
     `edge_caddy_data:/data`, `edge_caddy_config:/config` (тома `external: true` — создаются в шаге 3), сеть `edge` (`external: true`).
   - `infra/edge/Caddyfile`: три сайта через переменные `{$DND_PUBLIC_HOST}`, `{$CLAUDUSHKA_PUBLIC_HOST}`,
     `{$CLAUDUSHKA_TEST_PUBLIC_HOST}`; бэкенды `dnd-mcp:8000`, `claudushka:8090`, `claudushka-test:8090`. Комментарий: пока у
     Клодушки нет порта, её сайты отдают 502 — это нормально.
   - `infra/edge/.env.example` с тремя переменными; строка `infra/edge/.env` в `.gitignore`.
   - `docs/claude/staging.md` / `.claude/rules/docker-compose.md` — абзац про `edge` и ссылка сюда.
1. **DNS** (Алексей): A-записи `claudushka.alukr.de` и `claudushka-test.alukr.de` на IP сервера (тот же, что у `dnd.alukr.de`).
   Проверка: `getent hosts claudushka.alukr.de` на сервере возвращает его IP. До этого шаг 6 не начинать — иначе Caddy будет
   бесконечно пытаться выпустить сертификат и упрётся в лимиты Let's Encrypt.
2. **На сервере:** `cd ~/claudushka && git pull` (ветка с `infra/edge` уже в `main`), `cp infra/edge/.env.example infra/edge/.env`,
   вписать хосты. Без простоя.
3. **Сеть и тома** (без простоя): `docker network create edge`; `docker volume create edge_caddy_data`;
   `docker volume create edge_caddy_config`; копия сертификатов, чтобы не выпускать заново:
   `docker run --rm -v dnd-mcp_caddy_data:/from:ro -v edge_caddy_data:/to alpine sh -c 'cp -a /from/. /to/'`.
   Старый том `dnd-mcp_caddy_data` НЕ удалять — это откат.
4. **Правка dnd-mcp** (коммит в репозитории dnd-mcp, Алексей или исполнитель с его разрешением): в `~/dnd-mcp/docker-compose.yml`
   убрать сервис `caddy` и его тома из `volumes:`; сервису `dnd-mcp` добавить сети `default` и `edge` (`edge` — `external: true`),
   алиас не нужен (имя сервиса `dnd-mcp` резолвится в сети edge). `Caddyfile` в dnd-mcp оставить как есть (больше не используется) или
   удалить отдельным коммитом. **Пока не применять.**
5. **Переключение (простой dnd — секунды; спросить Алексея перед шагом):**
   ```
   cd ~/dnd-mcp && docker compose up -d --remove-orphans     # убирает dnd-caddy, dnd-mcp получает сеть edge
   cd ~/claudushka/infra/edge && docker compose up -d        # edge-caddy занимает 80/443
   ```
   Затем `~/dnd-mcp-staging/docker-compose.yml` — та же правка (или пометка в его CLAUDE.md, что стенд dnd без caddy).
6. **Проверка:** `curl -sI https://dnd.alukr.de` → 200/ожидаемый ответ, сертификат тот же (`openssl s_client -connect dnd.alukr.de:443
   -servername dnd.alukr.de </dev/null 2>/dev/null | openssl x509 -noout -dates`); `docker logs edge-caddy` — сертификаты для двух
   новых хостов выпущены (до запуска мини-приложения их сайты дают 502); dnd-клиент (MCP) работает как раньше.
7. **Откат:** `cd ~/claudushka/infra/edge && docker compose down`; в `~/dnd-mcp` — `git revert` правки шага 4 и
   `docker compose up -d` (поднимет `dnd-caddy` со старым томом сертификатов).

## После
- `CLAUDE.md` раздел «Доступ к серверу»: строку про dnd дополнить «прокси — стек `edge` (`infra/edge/`), общий для dnd и Клодушки;
  его правка затрагивает dnd — только с согласия Алексея».
- В репозитории dnd-mcp — короткая заметка в его CLAUDE.md, что TLS теперь в стеке `edge` репозитория Клодушки.

# ТЗ: мини-приложение настроек (`feat/miniapp`)

Постановка Алексея 2026-11-01 (п.1 `docs/claude/roadmap-2026-11.md`): настраивать Клодушку не слэш-командами, а формой
в Telegram Mini App. **Зависимости:** ветки `feat/settings-registry` и `feat/chat-managers` уже в `main` (реестр `SETTINGS`, `can_change`,
`apply_setting` — `docs/claude/settings.md`); прокси `edge` развёрнут по `docs/claude/runbook-edge-proxy.md`
(`claudushka.alukr.de` и `claudushka-test.alukr.de` → порт 8090 контейнера). Если чего-то нет — остановиться и сказать.

ТЗ для отдельной сессии исполнителя: **сначала `CLAUDE.md` целиком**, затем `docs/claude/settings.md`,
`.claude/rules/docker-compose.md`, `docs/claude/staging.md`, `docs/claude/billing.md`.

## Что получает пользователь
- В личке с ботом — кнопка меню «Настройки» (menu button типа web_app) и команда `/settings`, отвечающая кнопкой web_app.
- `/settings` в группе — ответ «настройки открываются в личке» с URL-кнопкой на диплинк `https://t.me/<bot_username>?start=settings_<chat_id>`;
  `/start settings_<chat_id>` в личке отвечает кнопкой web_app, открывающей приложение сразу на этом чате (`?chat=<chat_id>` в URL;
  параметр URL не подписан и ничего не разрешает — права проверяет сервер).
  Перед реализацией сверить с актуальной документацией Telegram Bot API, в каких чатах работают web_app-кнопки (inline/keyboard/menu);
  если в группе есть способ открыть Mini App напрямую без BotFather-настройки — описать в отчёте, но в v1 не делать.
- Экран 1: список чатов, которые пользователь может смотреть: своя личка + группы, где он писал (`group_messages.user_id`) и бот там
  есть (`allowed_chats`, не забанены). Админ бота видит все чаты из `allowed_chats` + свою личку. Пометки: «админ», «платный/бесплатный».
- Экран 2: настройки выбранного чата — по одному элементу на ключ `SETTINGS` подходящего `scope`: select для `choice`, ползунок/число
  для `int`, переключатель для `bool`. Подпись `note` из `get` (например, «бесплатный режим — всегда Lite»). Если менять нельзя —
  элемент выключен, рядом причина из `can_change`. Изменение сохраняется сразу; ответ сервера (ок или текст отказа) — коротко под
  элементом; после записи состояние перечитывается с сервера.
- Деньги, баны, проверка, `/topup`, `/ratelimit`, `/unignore` — НЕ в v1.

## Бэкенд — внутри процесса бота
- Отдельного сервиса нет: проверка прав опирается на `is_chat_admin` (запрос к Telegram через живого бота) и на реестр в `bot.py`.
  В `post_init` запустить `uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=MINIAPP_PORT, log_level="warning"))` как фоновую
  задачу на том же event loop (`_spawn_background_task`); `fastapi`/`uvicorn` уже в `requirements.txt` (версии не менять). Падение
  сервера настроек не должно ронять бота — лог + `notify_admins`.
- Включается переменными окружения: `MINIAPP_PORT` (по умолчанию 8090), `MINIAPP_PUBLIC_URL` (`https://claudushka.alukr.de`; стенд —
  `https://claudushka-test.alukr.de`). Нет `MINIAPP_PUBLIC_URL` → веб-сервер не стартует, меню-кнопка не ставится, `/settings`
  отвечает «мини-приложение не настроено» (так бот работает и без прокси).
- Эндпоинты (JSON, все под `/api/`, кроме статики):
  - `GET /` — `miniapp/index.html`; `GET /static/...` не нужен, всё в одном файле.
  - `GET /api/chats` → список чатов экрана 1.
  - `GET /api/chats/{chat_id}/settings` → `{chat: {id, title, tier, is_group}, settings: [{key, label, kind, choices, int_range, value,
    stored, note, can_change, reason}]}`.
  - `POST /api/chats/{chat_id}/settings/{key}` с `{"value": ...}` → `can_change` → `apply_setting` → `{ok, message, setting}`.
- **Авторизация — только `initData` Telegram** в заголовке `Authorization: tma <initData>`. Проверка подписи — по разделу
  «Validating data received via the Mini App» официальной документации Telegram (HMAC-SHA-256, ключ выводится из токена бота;
  алгоритм взять из документации, не из памяти, и сослаться на неё в коде), `hmac.compare_digest`, `auth_date` не старше
  `MINIAPP_INITDATA_MAX_AGE` (по умолчанию 3600 с). Из `initData` берём только `user.id` (и имя для логов). Нет/неверная подпись → 401.
  `initData` и токен НЕ логировать.
- Права на запись — через `can_change` (флаг `is_chat_admin` считать через `is_chat_admin(context-эквивалент, chat_id, user_id)`, кэш
  результата на 5 минут в памяти; управляющие чата из `feat/chat-managers` учитываются там же); на чтение чужого чата — пользователь
  должен быть в списке экрана 1 (админ бота — любой). **`in_target_chat=True` для чатов из списка экрана 1** (своя личка и группы, где
  человек участник): иначе `can_change` откажет ему как постороннему, хотя командой из самой группы он бы смог; `False` — только
  для админа бота, открывшего чужой чат вне своего списка.
- `usage_ctx` для записи с побочными расходами (проба модели) выставлять явно `(chat_id, "group"|"private", user_id)`, как в джобах.
- Ограничение частоты: не больше ~30 запросов в минуту на `user.id` (в памяти) → 429.
- Ответы с заголовками: `Content-Security-Policy` (скрипты только свои inline + `https://telegram.org`), `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`. CORS не нужен (страница и API на одном хосте).
- Каждая успешная запись — `logger.info("miniapp set chat=… key=… value=… by uid=…")`.

## Фронтенд — `miniapp/index.html`, один файл, без сборки и без фреймворков
- Подключает `https://telegram.org/js/telegram-web-app.js` (официальный скрипт), берёт `Telegram.WebApp.initData`, цвета — из
  `themeParams` (светлая/тёмная тема Telegram), `Telegram.WebApp.ready()` / `expand()`, кнопка «назад» — `BackButton`.
- Вся логика прав — на сервере; клиент только показывает `can_change`/`reason`.
- Тексты по-русски, коротко. Работает на ширине телефона.

## Инфраструктура в репозитории Клодушки
- `docker-compose.yml`, сервис `claudushka`: том `./miniapp:/app/miniapp`, сеть `edge` (`external: true`) плюс `default`, env
  `MINIAPP_PUBLIC_URL`/`MINIAPP_PORT` через `.env`; портов НЕ публиковать (доступ только через `edge-caddy`). Новый каталог в
  томах → выкатка только `docker compose up -d --force-recreate claudushka` (как с `personas`), не `/update` — записать в доках и в отчёте.
  `claudushka-wa` не трогать.
- `docker-compose.test.yml` — то же для `claudushka-test` (хост стенда в `.env.test`).
- `.env.example` / `.env.test.example` — новые переменные с комментарием.

## Не делать
- Не менять правила настроек (они в реестре), не добавлять настройки, которых нет в `SETTINGS`.
- Не публиковать порт наружу, не трогать стек `edge` и dnd (это runbook), не править `.env` на сервере без Алексея.
- Не мержить, не ставить тег.

## Тесты
`scripts/test_miniapp.py` (FastAPI `TestClient` — проверить, что он доступен с установленной версией, без новых зависимостей; если
нужен `httpx` — он уже в requirements):
- проверка `initData`: правильная подпись (сгенерировать в тесте тем же алгоритмом с фиктивным токеном) → ок; изменённое поле,
  чужой токен, просроченный `auth_date`, нет заголовка → 401;
- `/api/chats`: участник видит свою личку и свои группы, не видит чужие; админ бота видит все;
- `GET settings` чужого чата не-участником → 403; набор ключей по `scope` (в личке нет групповых);
- `POST`: разрешённое → запись через `apply_setting`; запрещённое → 403 с причиной из `can_change`, без записи; неверное значение → 400;
- без `MINIAPP_PUBLIC_URL` сервер не стартует; лимит частоты → 429;
- все `scripts/test_*.py` без сети — зелёные.

## Документация
- Новый `docs/claude/miniapp.md`: схема (Telegram → edge-caddy → claudushka:8090), авторизация, эндпоинты, выкатка `--force-recreate`,
  переменные, как добавить настройку (только в `SETTINGS` — форма подхватит сама).
- `.claude/rules/docker-compose.md` — том `miniapp`, сеть `edge`, правило выкатки.
- `README.md`, `USER_HELP` (`/settings`), `CLAUDE.md` (индекс, инвариант «мини-приложение — только через `SETTINGS`/`can_change`,
  авторизация — только `initData`»). Версию не менять. `docs/claude/roadmap-2026-11.md` — статус.
- Новый `docs/claude/staging-checklist-miniapp.md`: открыть из меню лички и через `/settings` в группе; участник/админ группы/админ
  бота; free; смена каждой настройки и сверка той же командой (`/models`, `/personas`, `/activity` …); попытка `curl` к API без
  `initData` → 401; `curl https://claudushka-test.alukr.de/` без Telegram — страница открывается, но данных нет.

## Стенд и версия
Стенд обязателен (новый сервис внутри процесса, compose, сеть). Версия **minor: v2.6.0** (v2.5.0 — управляющие чата):
`git tag -a v2.6.0 -m "v2.6.0: мини-приложение настроек (/settings, меню лички)"`. Тег не ставить.

## Порядок и отчёт
1. Убедиться, что реестр в `main` и прокси развёрнут (`curl -sI https://claudushka-test.alukr.de` с сервера даёт 502 — сайт есть, бэкенда
   нет). 2. Ветка `feat/miniapp` от `main`. 3. Бэкенд → тесты → фронтенд → compose → документы → `ast.parse`. 4. Push.
5. Отчёт по-русски: что сделано, отступления, что не проверено (в частности, поведение в разных клиентах Telegram), версия.
Противоречие ТЗ и кода/документации Telegram — остановиться и спросить.

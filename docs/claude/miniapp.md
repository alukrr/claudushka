# Мини-приложение настроек (`feat/miniapp`, v2.6.0)

ТЗ — `docs/claude/tz-miniapp.md`; права — `docs/claude/settings.md` (реестр `SETTINGS`, `can_change`, `apply_setting`) и
`docs/claude/group-chat.md` («Управляющие чата»); прокси — `docs/claude/runbook-edge-proxy.md`; чек-лист стенда —
`docs/claude/staging-checklist-miniapp.md`.

## Схема
```
Telegram (Mini App, HTTPS) → edge-caddy (стек edge, :443) → claudushka:8090 (сеть edge) → uvicorn внутри процесса бота
```
- Сервер — **внутри процесса бота** (`post_init` → `_run_miniapp_server`, фоновая задача на том же event loop). Отдельного сервиса нет: права
  опираются на `is_chat_admin` (запрос к Telegram живым ботом) и на реестр в `bot.py`. Падение сервера бота не роняет: лог + `notify_admins`.
  Сигналами (SIGINT/SIGTERM) управляет PTB — у `uvicorn.Server` переопределён `capture_signals`; `post_shutdown` ставит `should_exit`.
- Код: `miniapp/server.py` (HTTP-слой: проверка `initData`, лимит частоты, заголовки, маршруты; `bot.py` НЕ импортирует — всё через объект
  `deps`, поэтому нет циклического импорта и можно тестировать подменой), `miniapp/index.html` (фронтенд, один файл, без сборки и фреймворков),
  `bot.py` секция «Мини-приложение настроек» (`_ma_*`, `_MiniappDeps`, `build_miniapp`, `cmd_settings`, `_start_settings_deeplink`).
- Каталог `miniapp/` монтируется целиком (`./miniapp:/app/miniapp`), поэтому `miniapp/server.py` отдельного volume не требует.
  Если каталога нет (выкатили `/update` без пересоздания), `bot.py` ловит `ImportError` и мини-приложение молча выключено (warning при старте).

## Включение и переменные (`.env` / `.env.test`)
- `MINIAPP_PUBLIC_URL` — публичный URL (`https://claudushka.alukr.de`, стенд — `https://claudushka-test.alukr.de`). Не секрет. **Нет переменной →
  веб-сервер не стартует, кнопка меню не ставится, `/settings` отвечает «мини-приложение не настроено»** (бот работает как раньше, без прокси).
- `MINIAPP_PORT` (по умолчанию 8090; Caddyfile прокси смотрит на `claudushka:8090` / `claudushka-test:8090`), `MINIAPP_INITDATA_MAX_AGE` (по умолчанию 3600 с).
- Порт наружу НЕ публикуется — доступ только через `edge-caddy` по внешней сети `edge`.

## Выкатка — только `docker compose up -d --force-recreate`, не `/update`
Новый том `./miniapp:/app/miniapp`, сеть `edge` и переменные `.env` применяются только при пересоздании контейнера (`restart`/`/update` — нет):
`docker compose up -d --force-recreate claudushka` (стенд: `docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test`).
Внешняя сеть `edge` должна существовать (создана при развёртывании прокси): иначе `docker compose up` откажется стартовать.
`claudushka-wa` не затронут.

## Авторизация — только `initData`
`Authorization: tma <initData>`. Подпись проверяется **строго по официальной документации** Telegram Mini Apps, раздел «Validating data received via
the Mini App» (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app), `miniapp/server.py → validate_init_data`:
`data_check_string` = все поля КРОМЕ `hash`, по алфавиту, `key=<value>` через `\n`; `secret_key = HMAC_SHA256(key="WebAppData", msg=bot_token)`;
`hash = hex(HMAC_SHA256(key=secret_key, msg=data_check_string))`; сравнение — `hmac.compare_digest`; `auth_date` не старше `MINIAPP_INITDATA_MAX_AGE`.
Поле `signature` (Ed25519, для третьей стороны) входит в `data_check_string` как обычное поле: исключать его документация велит только в схеме
для третьей стороны. Берём из `initData` только `user.id`. `initData` и токен не логируются. Нет/неверная подпись → 401.

## Эндпоинты (JSON, под `/api/`; `GET /` — `index.html` без данных)
- `GET /api/chats` → `{chats: [{id, title, tier, is_group, is_admin, banned?}]}`: своя личка + группы, которые бот знает (`allowed_chats`, не забанены) и где человек
  **писал** (`group_messages`) **или он управляющий** (`chat_managers`), и **сейчас состоит** в них (`getChatMember`, не `left`/`kicked`). Админ бота — все группы из `allowed_chats` + своя личка.
- `GET /api/chats/{chat_id}/settings` → `{chat, settings: [{key, label, kind, choices, int_range, value, stored, note, can_change, reason}]}`; у `choice`
  каждый вариант — `{value, label, allowed, reason}` (в группе `restricted` не-админу недоступны); настройка, которую даже смотреть нельзя (автоигнор не-админу) —
  `value: null`, `can_change: false`, `reason`. В личке нет групповых ключей (`scope`).
- `POST /api/chats/{chat_id}/settings/{key}` с `{"value": …}` → `{ok, message, setting}`. Коды: 400 — неверный тип/значение; 401 — подпись; 403 — нет прав
  или чужой чат (текст из `can_change`); 404 — нет ключа; 409 — `apply_setting` отказал (проба модели, чат не известен); 429 — больше 30 запросов/мин на `user.id`.
- Заголовки на всех ответах: `Content-Security-Policy` (скрипты — свои inline + `https://telegram.org`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## Права
- Доступ к группе (чтение и запись) = строка в `allowed_chats` и не `banned` + человек **состоит** в группе (`getChatMember`, при записи — без кэша). «Писал в чате» НЕ требуется:
  команды в `group_messages` не пишутся, и управляющий/админ, ни разу не писавший в группе, иначе получил бы 403 даже по диплинку из `/settings` в самой группе. Админ бота — любой чат. Запись: `can_change(key, …, in_target_chat=…)` → `apply_setting`. **`in_target_chat=True`
  для чатов из списка экрана 1** (своя личка, группа, где человек участник): иначе `can_change` отказал бы как постороннему; `False` — только админ бота вне своего списка.
- `is_chat_admin` (в нём же управляющие чата) читается с кэшем 5 минут; **запись проверяет права и членство заново, без кэша**.
- `usage_ctx` на запись = `(chat_id, "group"|"private", user_id)`: проба модели при смене модели уходит на счёт этого чата, `context=None` (без «печатает…» в чате).
- Не в v1: деньги, баны, проверка, `/topup`, `/ratelimit`, `/unignore`.

## Открытие приложения
- Личка: кнопка меню «Настройки» (`set_chat_menu_button`, `MenuButtonWebApp`, ставится в `post_init`) и `/settings` (кнопка `web_app`).
- Группа: `/settings` отвечает «настройки открываются в личке» + URL-кнопка `https://t.me/<bot>?start=settings_<chat_id>`; `/start settings_<chat_id>` в личке
  отвечает `web_app`-кнопкой на `…/?chat=<chat_id>` (параметр не подписан и ничего не разрешает — права проверяет сервер).
- Почему так: Bot API для `InlineKeyboardButton.web_app` и `KeyboardButton.web_app` — «Available only in private chats between a user and the bot».
  Прямые ссылки (`t.me/<bot>/<app>`, `?startapp` — Main Mini App) открывают приложение в текущем чате, в т.ч. группе, но требуют настройки в BotFather — в v1 не делали.

## Как добавить настройку
Только в `SETTINGS` (`docs/claude/settings.md`) — форма подхватит сама: список ключей, тип элемента (`choice`/`int`/`bool`), варианты, права и подписи идут из реестра.
Менять `index.html` и `miniapp/server.py` для этого не нужно.

## Известные тонкости
- **Ограничение v1 (список экрана 1):** админ Telegram-группы, который ни разу в ней не писал и не управляющий, в списке «Чаты» может не появиться — группа попадает в список по
  «писал или управляющий». Такой человек открывает её через `/settings` в самой группе (диплинк `?chat=`): доступ определяется членством, не списком.
- Подпись проверена по документации и тестами с самодельной подписью; на настоящих данных клиентов Telegram — на стенде (чек-лист).
- Права на чтение кэшируются 5 минут: после `/manager rm` или выхода из группы список/настройки могут показываться до пяти минут, запись при этом уже отказывает.
- Лимит частоты и кэш — в памяти процесса, после рестарта сбрасываются.

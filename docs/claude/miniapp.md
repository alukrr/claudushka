# Мини-приложение настроек (`feat/miniapp`, v2.6.0; экран «Расходы» и открытие из группы — `feat/miniapp-v2`, код готов, стенд не пройден)

ТЗ — `docs/claude/tz-miniapp.md` (v2 — `docs/claude/tz-miniapp-v2.md`); права — `docs/claude/settings.md` (реестр `SETTINGS`, `can_change`, `apply_setting`) и
`docs/claude/group-chat.md` («Управляющие чата»); прокси — `docs/claude/runbook-edge-proxy.md`; чек-лист стенда —
`docs/claude/staging-checklist-miniapp.md` (v2 — `docs/claude/staging-checklist-miniapp-v2.md`).

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
- `MINIAPP_MAIN_APP` (`1`/`true`/`yes`/`on`; по умолчанию выключено) — в BotFather настроен Main Mini App с URL `MINIAPP_PUBLIC_URL`, `/settings` в группе открывает приложение прямо в группе (см. «Открытие приложения»). Пока выключено — `/settings` в группе ведёт в личку, как в v1. Меняется `.env` → `docker compose up -d --force-recreate` (не `restart`).
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
- `GET /api/chats` → `{chats: [{id, title, tier, is_group, is_admin, can_view_costs, banned?}]}` (`can_view_costs` — показывать ли кнопку «Расходы»; сервер всё равно проверяет сам): своя личка + группы, которые бот знает (`allowed_chats`, не забанены) и где человек
  **писал** (`group_messages`) **или он управляющий** (`chat_managers`), и **сейчас состоит** в них (`getChatMember`, не `left`/`kicked`). Админ бота — все группы из `allowed_chats` + своя личка.
- `GET /api/chats/{chat_id}/costs` → экран «Расходы» (v2, раздел «Расходы» ниже): `{chat, balance, rate_7d_per_day, days_left, months[12], weeks[12], days[7], by_model, by_label, by_kind_week, show_free, subscription}`. 401 — подпись; 403 — не участник / не админ чата («Расходы видят админы чата.») / чужая личка; 500 — «Не удалось загрузить расходы.» (текст исключения не отдаётся).
- `GET /api/chats/{chat_id}/settings` → `{chat, settings: [{key, label, kind, choices, int_range, value, stored, note, can_change, reason}]}`; у `choice`
  каждый вариант — `{value, label, allowed, reason}` (в группе `restricted` не-админу недоступны); настройка, которую даже смотреть нельзя (автоигнор не-админу) —
  `value: null`, `can_change: false`, `reason`. В личке нет групповых ключей (`scope`).
- `GET /api/chats/{chat_id}/topup` → `{title, balance, rate, packs: [{stars, usd}], help}` и `POST /api/chats/{chat_id}/topup` с `{"stars": N}` → `{invoice_link}` (экран «Пополнить», Stars — `docs/claude/billing.md` → «Пополнение Stars»). Права: `_ma_topup_access` = `_ma_access(fresh=True)` и `in_target` — своя личка или группа, где человек состоит (платит любой участник); админ бота вне списка и чужая личка — 403. POST перед `createInvoiceLink` гоняет те же `check_star_payment`, что и `pre_checkout` (400 — нет пакета/чат не принимает). **Баланс здесь не зачисляется** — только бот по `successful_payment`; фронтенд по статусу `paid` лишь перечитывает баланс через ~2,5 с. `chat.can_topup` в `/settings` — флаг кнопки «Пополнить баланс ⭐» (экран чата и «Расходы»). Справка — `miniapp/stars_help_ru.md` (текст идёт как `textContent`, без HTML).
- `POST /api/chats/{chat_id}/settings/{key}` с `{"value": …}` → `{ok, message, setting}`. Коды: 400 — неверный тип/значение; 401 — подпись; 403 — нет прав
  или чужой чат (текст из `can_change`); 404 — нет ключа; 409 — `apply_setting` отказал (проба модели, чат не известен); 429 — больше 30 запросов/мин на `user.id`.
- Заголовки на всех ответах: `Content-Security-Policy` (скрипты — свои inline + `https://telegram.org`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## Права
- Доступ к группе (чтение и запись) = строка в `allowed_chats` и не `banned` + человек **состоит** в группе (`getChatMember`, при записи — без кэша). «Писал в чате» НЕ требуется:
  команды в `group_messages` не пишутся, и управляющий/админ, ни разу не писавший в группе, иначе получил бы 403 даже по диплинку из `/settings` в самой группе. Админ бота — любой чат. Запись: `can_change(key, …, in_target_chat=…)` → `apply_setting`. **`in_target_chat=True`
  для чатов из списка экрана 1** (своя личка, группа, где человек участник): иначе `can_change` отказал бы как постороннему; `False` — только админ бота вне своего списка.
- `is_chat_admin` (в нём же управляющие чата) читается с кэшем 5 минут; **запись проверяет права и членство заново, без кэша**.
- `usage_ctx` на запись = `(chat_id, "group"|"private", user_id)`: проба модели при смене модели уходит на счёт этого чата, `context=None` (без «печатает…» в чате).
- Не в приложении: баны, проверка, `/topup`, `/ratelimit`, `/unignore`. Деньги — только ПРОСМОТР расходов (раздел ниже), не изменение.

## Расходы (v2, `GET /api/chats/{chat_id}/costs`)
- Права — только существующие функции, новых правил нет. Группа: `_ma_access` (членство, `allowed_chats`, не `banned`) + `is_chat_admin` (админы чата и управляющие; кэш 5 мин, как у настроек — это чтение) или админ бота (чат из `allowed_chats`). Своя личка — самому человеку; чужая личка — только админ бота. Остальным 403.
- **Бесплатное (`billed=0`) — расходы владельца: его видит только админ бота.** Остальным сервер вообще не кладёт эти поля в ответ (`_cost_data(show_free=False)`: ни `free` в `months`/`days`, ни free-строк в `by_model`/`by_label`) — не «прячем во фронтенде». `show_free` в ответе говорит, что включено.
- Данные собирает `_cost_data(chat_id, show_free)` в `bot.py` (в `asyncio.to_thread`), её же использует текстовый `/cost <chat_id>` (`_cost_detail`, блок «По месяцам»). Источник — `usage_log` (`db.usage_grouped`, `cost_usd` заморожен в момент вызова) и баланс; схема БД не менялась. Деньги в API — float USD без `$`, форматирует фронтенд.
- `months` — 12 календарных месяцев по `BERLIN_TZ`, новые сверху, текущий `partial: true` («по сегодня»); границы — первое число 00:00 Берлина (`_cost_month_windows`), не «+30 дней», поэтому переход CET/CEST месяц не сдвигает. 12 месяцев × (1 или 2) запросов по индексу `(chat_id, ts)`; отдельная агрегирующая функция в `db.py` не заведена. `weeks` — 12 ISO-недель (DIN 1355 / ISO 8601: понедельник — начало, KW 1 — неделя с первым четвергом года), новые сверху, формы как у `months`: `{week: "2026-W46", kw, iso_year, start: "2026-11-09", end: "2026-11-15", partial, paid, free?}` (`free` — только при `show_free`; `iso_year` может отличаться от календарного: 29.12.2025 — KW 1 2026); границы — понедельник 00:00 Берлина через `datetime.combine` (`_cost_week_windows`), текущая неделя `partial: true`. Фронтенд: блок «По неделям» перед «По месяцам», подпись `KW 46 2026 · 09.11–15.11`. `subscription` — оценка цены подписки `{enough, month, week, paid_days, spent, window_days, since}` (`month`/`week` = `null` при `enough: false`, т.е. платных дней < 7); считается только по платному (`billed=1`), поэтому в ответе её видят все, кто видит расходы, и она не зависит от `show_free`. Фронтенд кладёт её под «Баланс»: «В режиме подписки: месяц ≈ $X, неделя ≈ $Y» и мелко «по N дн. платного режима с DD.MM», либо «Цена подписки: мало данных». Формула и окно — `docs/claude/billing.md` → «Оценка цены подписки». `days` — 7 календарных дней, `by_model` / `by_label` (топ-8 на каждый `billed`) — те же 7 дней, `by_kind_week` — с понедельника (как «По видам за неделю» в `/cost`), только `billed=1`; `rate_7d_per_day` и `days_left` — как «≈ на N дн.» в `/cost`.
- Фронтенд: кнопка «Расходы» на экране 1 и на экране чата (`can_view_costs`; в `GET …/settings` он тоже есть, т.к. при открытии по `startapp` списка чатов нет), отдельный экран с `BackButton` (назад → экран чата). Полоски по месяцам — CSS, без библиотек; CSP и внешние скрипты не менялись.

## Открытие приложения
- Личка: кнопка меню «Настройки» (`set_chat_menu_button`, `MenuButtonWebApp`, ставится в `post_init`) и `/settings` (кнопка `web_app`).
- Группа, `MINIAPP_MAIN_APP=1` (v2): `/settings` отвечает «Настройки Клодушки:» + две URL-кнопки: «Открыть настройки» `https://t.me/<bot>?startapp=chat_n<abs(chat_id)>` (Main Mini App, открывается в текущем чате) и «Открыть в личке» (запасной путь, ниже). `n` — минус группы (`_settings_startapp_param`; в `startapp` только `A-Za-z0-9_`). Фронтенд берёт чат из `Telegram.WebApp.initDataUnsafe.start_param` (`chat_n<id>` / `chat_p<id>`), затем из `?chat=`, иначе экран 1. `start_param`, как `?chat=`, ничего не разрешает: права — только `_ma_access`/`can_change`. Из группы к приложению приходят `chat_type`/`chat_instance` (не `chat_id`) — на них не опираемся.
- **Ручной шаг:** в @BotFather включить Main Mini App для бота с URL `MINIAPP_PUBLIC_URL` (прод и отдельно тестовый бот стенда), затем `MINIAPP_MAIN_APP=1` в `.env` и `--force-recreate`. Точный путь в меню BotFather в документации Telegram не описан — сверить при настройке.
- Группа, без `MINIAPP_MAIN_APP` (v1): `/settings` отвечает «настройки открываются в личке» + URL-кнопка `https://t.me/<bot>?start=settings_<chat_id>`; `/start settings_<chat_id>` в личке
  отвечает `web_app`-кнопкой на `…/?chat=<chat_id>` (параметр не подписан и ничего не разрешает — права проверяет сервер).
- Почему так: Bot API для `InlineKeyboardButton.web_app` и `KeyboardButton.web_app` — «Available only in private chats between a user and the bot».
  Прямые ссылки (`t.me/<bot>/<app>`, `?startapp` — Main Mini App) открывают приложение в текущем чате, в т.ч. группе, но требуют настройки в BotFather — в v1 не делали, в v2 — по флагу `MINIAPP_MAIN_APP`. Какие символы и какой длины допустимы в `startapp`, документация Telegram (bots/webapps, api/links) не говорит; у `/start`-параметра — `A-Z a-z 0-9 _ -`, до 64 символов (bots/features). Наш параметр укладывается в оба набора.

## Как добавить настройку
Только в `SETTINGS` (`docs/claude/settings.md`) — форма подхватит сама: список ключей, тип элемента (`choice`/`int`/`bool`), варианты, права и подписи идут из реестра.
Менять `index.html` и `miniapp/server.py` для этого не нужно.

## Известные тонкости
- **Ограничение v1 (список экрана 1):** админ Telegram-группы, который ни разу в ней не писал и не управляющий, в списке «Чаты» может не появиться — группа попадает в список по
  «писал или управляющий». Такой человек открывает её через `/settings` в самой группе (диплинк `?chat=`): доступ определяется членством, не списком.
- Подпись проверена по документации и тестами с самодельной подписью; на настоящих данных клиентов Telegram — на стенде (чек-лист).
- Права на чтение кэшируются 5 минут: после `/manager rm` или выхода из группы список/настройки могут показываться до пяти минут, запись при этом уже отказывает.
- Лимит частоты и кэш — в памяти процесса, после рестарта сбрасываются.

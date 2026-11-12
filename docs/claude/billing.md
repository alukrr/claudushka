> **С 2026-10-28 (v2.0.0)**: free-режим — только Gemini 3.1 Flash-Lite (выбора нет, `chat_free_model` не используется);
> подтверждение `/fable` удалено, «дорогих» (`restricted`) моделей сейчас нет; нативный поиск Anthropic (`native_search`) не
> считается — остался `gpt_search` (токены) и `kind=search/gpt` (цена вызова пока 0.0). Голос пишется в `usage_log` с
> `label="voice"` (`_record_gemini_usage`). Старые Claude-строки `usage_log` читаются по `LEGACY_PRICES`. Текст ниже про Haiku/
> Sonnet/Opus/Fable — история.

# Стоимость, кошельки, тарифы, доступ (ТЗ v0.10)

Перед правкой `_record_usage`, `chat_tier`, `usage_ctx`, `gate_update`, `/cost`, `/topup`,
`/verify /unverify /ban /unban`, лимитов непроверенных/free-режима, `verify_target`,
стартового бонуса — читай этот файл. «v0.10» — номер ТЗ, НЕ тег репозитория (тег v0.10.0
уже занят под `/ratelimit`; версия этой работы — см. CLAUDE.md, «Версионность»).

## Модель: два независимых измерения
| измерение | значения | что определяет |
|---|---|---|
| **доверие** | banned / непроверенный / проверенный | лимиты сообщений и поиска |
| **тариф** (по балансу) | paid (баланс > `db.PAID_MIN_BALANCE`=0.0001) / free | модели, картинки, обзор |

Бот отвечает всем, кроме banned. Баланс не уходит в минус (writeoff). `ADMIN_IDS` — всегда
проверенные, личный чат админа — всегда paid (учитывается, `billed=1`, но **без writeoff**:
баланс админа может быть отрицательным, это чистый учёт).

- **Проверенный**: группа — `allowed_chats.status='approved'`; пользователь — `users.role`
  `admin`/`premium` (или `ADMIN_IDS`). Непроверенный: `pending` / `street`.
  `users.verified` — это КАПЧА и к проверке отношения не имеет.
- **Бан**: колонки `users.banned` / `allowed_chats.banned`; НЕ трогает ни баланс, ни
  проверку. Роль `banned` и статус `rejected` — валидные значения CHECK, но больше никем
  не выставляются (миграция очистила).
- Личный чат: `chat_id = telegram_id` пользователя (всё, что keyed по chat_id, — балансы,
  `chat_models`, `chat_image_provider` — работает для лички и групп одинаково).

## Схема (db.py, `init_db`)
- `usage_log` — по строке на каждый успешный вызов (LLM / картинка / поиск). `chat_id NULL`
  = служебное вне чата (`billed=0`). `cost_usd` заморожена при записи, уже с `PRICE_MARKUP`.
  `model`: для LLM — API-строка ФАКТИЧЕСКОЙ модели из `response.model` (с 2026-09-23, см.
  `docs/claude/models-and-costs.md` → «Учёт токенов»), для картинок — ключ провайдера (`gpt`/`banana`), для
  поиска — `tavily` (бесплатный режим) или `anthropic` (нативный `web_search` в платном, `label=native_search`, `docs/claude/native-search.md`). `label`: `dialog` (ответ на реплику: диалог/фото/файл), `should_search`,
  `search`, `image`, `draw_translate`, `extract_memory`, `extract_chat_memory`,
  `media_describe`, `abuse_check` (Lite: проверка на травлю при каждом обращении к боту в группе, платит чат), `activity_check` (Lite: «вставлять ли реплику», только при выпавшем шансе), `activity` (спонтанная реплика модели чата), `daily_review`, `forget` (выбор «сувениров» `/forget`: Argon в paid, Lite в free; платит чат, где нажали кнопку), `greet`, `greet_filter`, `captcha_*`, `/review`, `/search`,
  `проверка` (пробный запрос `_probe_model`) и т.д. `thinking` — токены thinking, входят в
  `output` (с 2026-09-23, старые строки 0). Индексы `(chat_id, ts)`, `(ts)` и покрывающие `usage_log(chat_id, billed, cost_usd)`, `chat_credits(chat_id, amount)` — баланс считается полным SUM по чату (не хранится), индекс не даёт читать таблицу.
- `chat_credits` — `bonus` / `topup` / `adjust` / `writeoff`.
- **Баланс не хранится**: `SUM(chat_credits.amount) − SUM(usage_log.cost_usd WHERE billed=1)`
  (`db.get_balance`, округляется до 8 знаков). Retention `usage_log` не делали.
- `daily_usage(day, chat_id, user_id, msgs, limit_notified)` — **добавлена сверх ТЗ**:
  дневной счётчик ответов непроверенным + флаг «уведомление о лимите уже было сегодня»
  (нужен атомарный «один раз в сутки»). Строки старше 7 дней чистятся в `init_db`.
  Заменяет `STREET_DAILY_LIMIT`/`check_daily_limit`/`users.daily_messages` (мёртвые колонки
  `daily_messages`/`daily_reset`/`referral_code`/`referred_by` оставлены — таблицу `users`
  не пересобирали из-за `CHECK` на role и `UNIQUE` на referral_code).
- `schema_migrations(name, applied_at)` — маркеры однократных миграций данных.

### Миграции (однократные, идемпотентные, `db._run_once`)
1. `billing_access`: `rejected → pending`; `users.role banned → street` (очистка бан-листа);
   `referral → premium, verified=1`.
2. `billing_defaults`: удалить из `chat_models` строки с Haiku и из `chat_image_provider` — с
   `gpt` (это были старые глобальные дефолты; без удаления платные чаты застряли бы на
   Haiku/GPT). **Побочный эффект**: проверенные чаты после миграции — paid с дефолтами
   Sonnet/banana вместо прежних Haiku/GPT, без специального уведомления.
3. `billing_starter_bonus`: approved-группам и admin/premium с активностью за 30 дней
   (`group_messages` / `conversations`) — $10 / $5 (`kind='bonus'`, `note='starter'`; суммы миграции заморожены в `db._MIG_BONUS_*`, живые — `STARTER_BONUS_*`).
Ошибка миграции → откат, маркер не ставится, повтор при следующем старте (лог `Migration FAILED`).

## Конфиг (bot.py, рядом с MODELS)
Цены сверены 2026-09-20 (banana переведена на Nano Banana 2.1 2026-10-10: $0.0336 за 1K / $0.0504 за 2K + thinking/вход по `usageMetadata`, Pro $0.134 — `docs/claude/images.md`), gpt $0.02 (себестоимость пула $0.01, решение Алексея — ×2), поиск $0.008 (Tavily, 1 кредит pay-as-you-go).
GPT Image 2.5 Flare/Sunburst (добавлены 2026-09-22, `docs/claude/images.md`) — тот же пул,
та же цена $0.02, что у gpt.
`PRICE_MARKUP`, `IMAGE_PRICES`, `SEARCH_PRICE`, `STARTER_BONUS_*`, `FREE_IMAGES_*`,
`UNVERIFIED_*`, `VERIFY_MIN_TOPUP_GROUP`/`VERIFY_MIN_TOPUP_PRIVATE` (читать только через `_verify_min_topup(chat_id)`), `ADMIN_CONTACT`, `FABLE_CONFIRM_WINDOW`. Сутки/неделя —
`BERLIN_TZ`, неделя с понедельника (`_day_start`, `_week_start`).
`PRICE_MARKUP` умножается в ОДНОМ месте — `_record_usage`; `calc_llm_cost()` возвращает
цену без наценки. Замороженные строки задним числом не пересчитываются.

## Привязка вызова к чату: `usage_ctx`
`contextvars.ContextVar` = `(chat_id, chat_type, user_id)`. Выставляет `gate_update`
(`TypeHandler(Update, ...)`, `group=-1`) на КАЖДЫЙ апдейт, для апдейтов без чата — `None`
(при `MAX_CONCURRENT_UPDATES > 1` каждый апдейт идёт в своей задаче asyncio — контекст изолирован; при `1` апдейты идут в одной задаче по очереди, иначе контекст протёк бы; `docs/claude/api-errors.md` → «Параллельная обработка апдейтов»).
`asyncio.to_thread` и `create_task` копируют контекст, поэтому `sync_create`, `extract_*`,
`web_search`, фоновые задачи наследуют чат сами — **проверено** локально на живом
`Application.process_update` (group -1 → group 0 → to_thread). `daily_chat_review` (джоб,
апдейта нет) выставляет `usage_ctx` явно на каждый чат цикла.
**Стенд обязателен**: после сообщения в группе в `usage_log` должны быть строки с этим
`chat_id`, включая `should_search`/`extract_*`/`draw_translate`; если `chat_id` NULL — доложить,
не обходить костылём.

## Запись и списание (`_record_usage`, единственная точка)
- LLM: `_track_response(model, response, label)` вызывается ВНУТРИ `call_claude` (label =
  `usage_label` или первое слово `label`) и `sync_create` (`label=` kwarg, не уходит в API).
  Новый вызов — всегда с `label=`. Ответы на реплики (диалог/фото/файл) — `usage_label="dialog"`.
- Картинки — после УСПЕШНОЙ генерации (`generate_image_with_error`), поиск — после успешного
  `tavily.search` (в `web_search`, Tavily берёт деньги и за пустую выдачу). Нативный поиск — по строке на каждый
  `usage.server_tool_use.web_search_requests` (`NATIVE_SEARCH_PRICE`, `call_dialog_native`).
- `billed = (chat_tier(chat_id) == 'paid')`, `chat_id NULL → 0`.
- `db.record_usage` в одной транзакции (`BEGIN IMMEDIATE`) пишет строку и, если баланс ушёл
  ниже нуля, — `writeoff` на дефицит (`note='auto'`); возвращает `True`, только если ЭТА
  запись перевела чат paid→free → ровно одно сообщение о переходе (`_fire` — из потока
  через `run_coroutine_threadsafe` на `_main_loop`).
- Ошибка записи не роняет ответ (`try/except` + `logger.exception`).
- Дневной счётчик ответов (`daily_usage.msgs`) увеличивает `_count_reply` в хендлере ОДИН раз на реплику
  после успешного ответа (диалог/фото/файл; ответ картинкой на «нарисуй» — в ветке рисования), а не
  `_record_usage`: на реплику может приходиться несколько LLM-вызовов (аварийная обрезка) — иначе
  непроверенный терял бы лишнее сообщение из лимита.
- Не учитывается: Gemini-транскрипция аудио (`_transcribe_audio_gemini`, POOL) и картинки banana
  как внешний расход — ТЗ описывает только kind `llm|image|search`; цена картинки — `IMAGE_PRICES` (pool) или `gemini_image_cost` (banana/bananapro).

## Тарифы
`chat_tier(chat_id)` — `paid`/`free` по балансу (`is_admin(chat_id)` → всегда paid).
- **free**: `get_chat_model` → выбор free (`chat_free_model`: Haiku или Gemini Lite, дефолт Haiku),
  `get_chat_image_provider` → `gpt`, БЕЗ изменения строк `chat_models`/`chat_image_provider` — при возврате в paid прошлый
  выбор восстанавливается. `/sonnet /opus /fable /argon /flash /banana /bananapro /flare /sunburst` → «доступно в платном
  режиме»; `/haiku` и `/lite` пишут `chat_free_model`, `/gptimage` — «уже так», без записи. Поиск в free — `SEARCH_PROVIDER_FREE`,
  `/searchmode` не-админу отказ. Админ бота может менять настройки чужих free-чатов (запись без применения).
  Дневной лимит картинок (для ВСЕХ, включая проверенных): личка 10, группа 5 на `user_id`;
  считается по `usage_log kind='image' AND billed=0` с полуночи Берлина (`image_quota`).
  Исчерпан: на «нарисуй»/`/imagine` — ответ с лимитом и временем сброса, спонтанный `[[DRAW]]`
  — молча (`silent_limit=True`, маркер уже вырезан). Ежедневный обзор не шлётся.
- **paid**: модель из `chat_models`, нет строки → Sonnet (`PAID_DEFAULT_MODEL_KEY`); картинки из
  `chat_image_provider`, нет строки → banana (`db.DEFAULT_IMAGE_PROVIDER`); без дневного лимита
  картинок, `/ratelimit` действует; обзор.
- Реестр `MODELS` больше не содержит `admin_only`: не-Haiku модели — «админ ИЛИ paid».
- Сообщения о смене тарифа — один раз на переход (`TIER_FREE_MSG` из `_record_usage`,
  `_notice_tier_change` из `/topup`, `/verify`, первого стартового бонуса).

### `/fable` — подтверждение
В paid первая команда не переключает, а шлёт жирным (HTML) предупреждение с `N = MODELS["fable"]["out"] /
out текущей модели чата`; повтор в течение `FABLE_CONFIRM_WINDOW` (120 с) переключает. Ожидание —
`_fable_pending: dict[chat_id, ts]` в памяти (рестарт сбрасывает — повторить). «Уже на Fable» — просто
сообщение. Для админа с `chat_id`-аргументом подсказка повтора включает аргумент.

## Доступ и проверка
- **Неизвестная группа** регистрируется как `pending` сразу (в `gate_update` при первом сообщении и в
  `handle_new_chat`), бот отвечает; админам — «меня добавили» с `/verify` и `/ban`. `ensure_group` — `INSERT
  OR IGNORE`: повторное добавление НЕ сбрасывает `approved`/`banned` (раньше был `INSERT OR REPLACE`).
- **Лимиты непроверенных** (в ЛЮБОМ тарифе; проверенные — без лимитов сообщений и поиска):
  личка — 50 сообщений/сутки; группа — 20/сутки на юзера, но ТОЛЬКО если не проверены ни чат, ни автор
  (`_limits_apply`, `_check_message_limit`). Сверх лимита: одно сообщение в сутки (атомарно через
  `db.daily_limit_notice_claim`), дальше тишина. Поиск — 20/сутки (группа — на чат, личка — на юзера),
  сверх лимита автопоиск молча пропускается; явный `/search` получает сообщение (отклонение от ТЗ:
  иначе команда выглядела бы сломанной).
- **Как проверяют**: авто — `SUM(topup) >= _verify_min_topup(chat_id)` (личка $2, группа $5; в `apply_topup` — `/topup` и Stars) → группа `approved` / юзер
  `premium` + стартовый бонус; вручную — `/verify <id>`, `/unverify <id>` (id < 0 → группа).
  `verify_target` идемпотентно выдаёт бонус (с 2026-11-10: **$5 группе / $2 личке**, раньше $10/$5; ранее выданные не пересчитываются и не отзываются), если не выдавался — точнее, **довыдаёт до полного**: `db._grant_bonus_conn` пишет разницу между положенным бонусом и ЧИСТОЙ суммой строк `kind='bonus'` (отзыв — отрицательная строка), чат с полным или большим бонусом не получает ничего; остальные
  проверенные получают его при первом сообщении (`_ensure_starter_bonus`, кэш `_bonus_checked`).
  Непроверенные — только в момент проверки; новая группа без проверки бонуса не получает.
- **Бан** (`/ban`, `/unban`; реплаем в группе без аргумента — автор сообщения): барьер — `gate_update`
  (`ApplicationHandlerStop`). Забаненная группа молчит, в т.ч. на команды, кроме КОМАНД админов бота; из
  чата бот НЕ выходит. Забаненный юзер игнорируется везде. Админов банить нельзя.
- **Капча** — `needs_captcha` остаётся (`verified=0`, не админ, не banned, не premium), но `handle_captcha`
  как и раньше нигде не вызывается (мёртвый код; блокирующая «заявка админу» в личке удалена — теперь бот
  отвечает всем).
- **Пассивные траты в группах** (не запрошены явно): распознавание медиа (`media_gated`) и приветствие
  новичков — только в проверенных или платных группах. Это решение не из ТЗ — сохранено намерение старого
  `WHITELIST_ENABLED`-гейта. Извлечение памяти (`extract_all_participants_memory`, Haiku) работает во всех
  группах, в free-чатах — с `billed=0` (расход админа; см. `docs/claude/open-tasks.md`).

## Команды
- `/topup <chat_id> <сумма> [коммент] [force]` — только админ. ID, которого нет в базе, отклоняется (опечатка иначе создала бы проверенный «фантом» с бонусом); последний аргумент `force` заводит запись сознательно. `>0` — `topup`, `<0` — `adjust` (сверх баланса →
  дополнительный writeoff в ноль). Ответ: баланс, режим, проверка; после записи — авто-проверка
  и уведомление в чат: при смене тарифа — «Платный режим включён…», иначе (обычное пополнение) —
  «Ура, баланс пополнен на $X…».
- `/cost` — права: админ бота в личке — все чаты (+ «бесплатный режим», «служебное», «writeoff» за
  неделю); чат-админ в группе — этот чат; обычный юзер в личке — его личка; остальным — молчание
  (в т.ч. если не-админ передал аргумент). Работает в любом тарифе. Основные суммы — только
  `billed=1`. «Прошлая неделя (±%)» — изменение прошлой недели к позапрошлой (обе полные).
  `/cost <chat_id>` (админ) — детализация за 7 дней: модели, метки, дни, платное/бесплатное, блок «По неделям» (12 ISO-недель KW, строка `KW 46 (09.11–15.11): $x / $y (по сегодня)`, между «По дням» и «По месяцам»; на стыке ISO-годов — `KW 52/2025`) и блок «По месяцам» (12 календарных месяцев по `BERLIN_TZ`, платное / бесплатное, текущий — «по сегодня»); третьей строкой — оценка цены подписки (см. «Оценка цены подписки»). Строка оценки есть и в `/cost` группы и личке (`scope` = чат / user_id; второй строкой, после баланса); общий `/cost` админа (все чаты) её не показывает. Данные собирает `_cost_data(chat_id, show_free)` (границы месяцев — `_cost_month_windows`, по дате, не «+30 дней»), та же функция питает экран «Расходы» мини-приложения (`docs/claude/miniapp.md` → «Расходы»; бесплатное там только админу бота). Текст `/cost <chat_id>` режется по строкам под лимит Telegram (`split_lines_for_telegram`).
- `/pay` (и `/topup` не-админа), `/terms`, `/paysupport` — всем; `/refund` — только админ бота (раздел «Пополнение Stars»).
- `/verify /unverify /ban /unban /topup` — только `is_admin`. Удалены: `/whitelist(_on/_off)`,
  `/approve`, `/promote`, `/premium`, `/approve_chat`, `/reject_chat`, `/allow_chat`, `/deny_chat`,
  `/role`, `/pending` (покрыт `/chats`), `/migrate` (+ `db.migrate_from_json`, `allowed.json` и его
  volume-mount), реферальные ссылки (`ref_code` в `/start`, `get_referral_code`, `can_invite`…).

## Оценка цены подписки (`feat/cost-weeks-subscription`, ТЗ `docs/claude/tz-cost-weeks-subscription.md`)
Только ОЦЕНКА «сколько стоила бы подписка по прошлому» (сама подписка, оплата и тариф — не здесь; `chat_tier` не меняется). Показывается всем, кто видит расходы
(экран «Расходы», `/cost` группы/лички, `/cost <chat_id>`): бесплатное (`billed=0`) в оценку не входит никогда. Обязательно со знаком «≈».
- **Окно:** `max(now − 90 дней, 2026-10-28 00:00 Berlin)` … now (`SUBSCRIPTION_EST_DAYS`, `SUBSCRIPTION_EST_SINCE`: с 28.10 модели только Gemini, траты на Claude нерепрезентативны). Константы — в bot.py рядом с `PRICE_MARKUP`.
- **`paid_days` — по восстановленному балансу, не по «дням с тратами»** (тихий платный день иначе выпал бы из знаменателя и цена завысилась бы). Тариф не хранится, но баланс = `SUM(chat_credits.amount) − SUM(usage_log.cost_usd WHERE billed=1)` (`db._BALANCE_SQL`), у каждого события есть `ts`.
  `db.subscription_events` берёт баланс на начало окна (одним запросом) и события окна; `db.paid_seconds_from_events` (чистая функция, без БД) идёт по моментам событий: интервал платный, если баланс в его начале — после ВСЕХ событий этого `ts` — `> PAID_MIN_BALANCE`.
  Обёртки: `db.paid_seconds(chat_id, since, until, always_paid, conn=None)` и `bot._paid_seconds` (`always_paid=is_admin(chat_id)`: личка админа бота — всё окно платное, у неё `billed=1` без writeoff и баланс может быть отрицательным).
- **Как в коде, а не «как кажется»:** в баланс идут ВСЕ `kind` из `chat_credits` (`topup`, `bonus`, `adjust` — отрицательная корректировка `/topup` с минусом, `writeoff`, любой другой) — `kind` ничего не фильтрует, как и в `_BALANCE_SQL`; writeoff — обычная положительная строка (доводит баланс до нуля, пишется в ту же или следующую секунду после перерасходной траты, поэтому при равном `ts` кредиты идут раньше трат, а состояние берётся после всей группы событий). Отрицательная `adjust` может перевести чат в free без единой траты — восстановление это учитывает. Строки `billed=0` и `chat_id IS NULL` баланс не двигают.
- **Инвариант проверки:** `_record_usage` ставит `billed=1` только когда `chat_tier == paid` на момент записи, значит каждая `billed=1` должна лежать внутри восстановленного платного интервала (`db.billed_outside_paid` = 0). Ненулевое — гонка параллельных вызовов одного чата в секунду перехода paid→free или дыра в восстановлении. Проверяется тестами и `scripts/check_subscription_estimate.py` на копии прод-БД (только чтение, без бота).
- **Формула:** `spent = Σ cost_usd billed=1` за окно; `paid_days < SUBSCRIPTION_MIN_DAYS` (7) → `{enough: false}`, цену не показываем («мало данных: N дн. в платном режиме»); иначе `per_day = spent / paid_days`, **месяц = per_day × 30** (`SUBSCRIPTION_MONTH_DAYS`), **неделя = per_day × 7 × 1.05** (`SUBSCRIPTION_WEEK_MARKUP`). `PRICE_MARKUP` уже внутри `cost_usd` — второй раз не умножать.
- Блокирующий SQLite-код (`_subscription_estimate`, `_paid_seconds`) вызывается из `_cost_data`/`_cost_report`/`_cost_detail`, которые сами запускаются через `asyncio.to_thread`. Схема БД не менялась. На размер контекста модели не влияет.

## Пополнение Stars (`feat/stars-topup`, ТЗ `docs/claude/tz-stars-topup.md`)
Человек пополняет баланс чата сам, звёздами Telegram (XTR): `/pay` (и `/topup` не-админа) в личке или группе → кнопки-пакеты `STARS_PACKS` (дефолт 100/200/500/1000/2500 ⭐; 200 ⭐ = $2 — ровно порог лички, 500 ⭐ = $5 — порог группы) → **URL-кнопки со ссылкой на счёт** (`createInvoiceLink`, v2.12.1).
В группе платит ЛЮБОЙ участник, зачисляется группе (решение Алексея). Возврат — только админ бота (`/refund`).
- **Курс** `STARS_USD_RATE` (`.env`, дефолт **0.010** — решение Алексея 2026-11-10): сколько $ баланса даёт одна звезда. В `star_payments.rate`/`usd` пишется зафиксированное на момент оплаты — смена курса прошлое не трогает. Фактический курс вывода через Fragment/TON не подтверждён (в обзорах $0.013 или $0.0087–0.0092) — после первого реального вывода поправить.
- **Единственный путь зачисления — `successful_payment`** (`on_successful_payment`). Кнопки пакетов, `pre_checkout_query` и колбэк `openInvoice` мини-приложения денег не трогают.
- **`/pay` без засорения чата (`fix/pay-invoice-links`, v2.12.1).** Раньше кнопка пакета была callback и каждое нажатие слало новый `sendInvoice` в конец чата: на старом меню человек не видел счёта внизу и жал снова. Теперь:
  кнопки пакетов — **URL-кнопки** на ссылку `createInvoiceLink` (те же `_stars_invoice_kwargs`, что у мини-приложения): нажатие открывает оплату у нажавшего, в чат ничего не уходит.
  Ссылки кэшируются в памяти по `(chat_id, stars, STARS_USD_RATE)` (`_pay_link_cache`, `_pay_invoice_link`): повторный `/pay` не делает N запросов; смена курса — новые ссылки; ошибка создания ссылки не кэшируется,
  пакет пропускается (лог), `/pay` не падает; если не вышло ни одной — «Не удалось подготовить счета». **Одно меню на чат:** новый `/pay` удаляет предыдущее меню бота (`_pay_menu[chat_id]`, только память; ошибка удаления игнорируется).
  Под пакетами — «Открыть в приложении» (`_pay_app_button`): личка — `web_app` (`/?chat=<id>`), группа — `startapp` при `MINIAPP_MAIN_APP` или диплинк в личку, как в `/settings`; без мини-приложения кнопки нет.
  `on_pay_button` и `^pay:\d+$` оставлены только для СТАРЫХ сообщений с callback-кнопками: счёт не выставляют, всплывающее «Это меню устарело — отправьте /pay.».
  Оплата по ссылке из группы: `successful_payment` приходит в ЛИЧКУ плательщика (payload несёт группу), группа получает «Баланс чата пополнен на …» — путь тот же, что у мини-приложения (`on_successful_payment`: `update.effective_chat.id != chat_id`), новых правил нет.
- **Схема:** `star_payments(charge_id PK, ts, payer_id, chat_id, stars, usd, rate, status paid|refunded, refunded_ts)`; зачисление — `db.star_payment_record`: `INSERT OR IGNORE` в `star_payments` + строка `chat_credits` (`kind='topup'`, `note='stars:<charge_id>'`, `by_user=payer`) в ОДНОЙ транзакции (`BEGIN IMMEDIATE`). `charge_id` = `telegram_payment_charge_id`; повтор апдейта → `False`, ничего не пишется и не отвечается.
- **`apply_topup(chat_id, amount, note, by_user, *, before, credit_written)`** — общий хвост для `/topup` и Stars: запись кредита (если не `credit_written`), writeoff отрицательной корректировки, авто-проверка при `topup_total ≥ VERIFY_MIN_TOPUP` (`verify_target` + стартовый бонус). Сообщения чату — `notify_topup` (смена тарифа; «Ура, баланс пополнен» — только у `/topup`, у Stars `thanks=False`: плательщик уже получил «✅ Зачислено…»). Поведение `/topup` зафиксировано характеризационными тестами (`scripts/test_stars_topup.py`).
- **`pre_checkout_query`** (`on_pre_checkout` → `check_star_payment`, чистая функция): отвечать надо за 10 с. Проверяет формат payload `topup:<chat_id>:<stars>` (`STARS_PAYLOAD_RE`), `currency == XTR`, сумму = payload, пакет из `STARS_PACKS`, бан плательщика, чат (группа — известна и не забанена; личка — только своя). **`PerChatUpdateProcessor` пропускает такие апдейты без замка и слота** (у `pre_checkout_query` нет чата, ключ очереди = `user_id` = очередь лички, а в ней может идти долгий ответ модели — 10 с не хватило бы).
- **`gate_update` пропускает сообщения с `successful_payment`** мимо проверок бана: деньги уже взяты (бан между `pre_checkout` и оплатой иначе потерял бы зачисление).
- **Сбои:** payload не распознан / сумма не совпала / запись в БД упала после оплаты → `notify_admins` с `charge_id` («деньги взяты, баланс не пополнен — разобрать вручную») и сообщение плательщику «админ уже знает».
- **Возврат** `/refund <charge_id>` (только админ бота; без аргумента — последние 10 платежей): `db.star_refund_begin` атомарно проверяет, что платёж `paid` и на балансе чата лежит не меньше `usd`, пишет отрицательный `adjust` (`note='stars-refund:<charge_id>'`) и ставит `status='refunded'`; затем `refundStarPayment`; отказ Telegram → `db.star_refund_revert(charge_id, pay["undo"])` откатывает ВСЁ (adjust, отзыв бонуса, снятие проверки). Недостаточно баланса → отказ (часть уже потрачена; админ сам корректирует `/topup <id> +сумма` и повторяет).
- **Возврат единственного платежа (П4 ТЗ)**: если нет других `star_payments paid` и других строк `chat_credits kind='topup'`, в той же транзакции (а) снимается проверка (группа `approved → pending`, личка `premium → street`; для личек админов бота не трогаем — `unverify_ok=False`), (б) отзывается неистраченный остаток бонуса = `min(чистый бонус, баланс после возврата)` строкой `kind='bonus'`, `note='revoke:<charge_id>'` — баланс не уходит в минус. При новой оплате бонус довыдаётся до полного (см. «Как проверяют»), истраченная часть повторно не дарится. Известное ограничение: если чат был проверен вручную `/verify` до платежа, проверка тоже снимется (причину проверки БД не хранит) — сообщение `/refund` об этом говорит, админ возвращает `/verify`. `topup_total` возврат не уменьшает (строка `topup` остаётся). Оценка подписки (`paid_seconds`) читает все строки `chat_credits`, отрицательный бонус учитывается сама (тест `test_reconstructed_balance_equals_get_balance_after_bonus_revoke`).
- **Обязательные команды Telegram для платных ботов:** `/terms` (`STARS_TERMS_TEXT`) и `/paysupport` (`STARS_PAYSUPPORT_TEXT`; пометка, что поддержка Telegram по покупкам в ботах не помогает). Тексты — черновики, утверждает Алексей. `/pay help` и экран мини-приложения показывают справку «Как купить звёзды из России» из `miniapp/stars_help_ru.md` (правится без кода; **не проверена на живом аккаунте из РФ** — см. ТЗ).
- **`/cost`:** строка «Пополнения Stars за неделю: N ⭐ / $X» в общем `/cost` админа и в `/cost <chat_id>` (`_stars_week_line`, `db.star_payments_sum`; возвращённые не входят).
- Мини-приложение: кнопка «Пополнить» (`can_topup`) → `GET/POST /api/chats/{id}/topup` — `docs/claude/miniapp.md`.
- Деньги: Stars идут Алексею (вывод в TON вручную через Fragment) — это НЕ расход `usage_log`, в «расходах» не учитывается. `chat_tier`, `PRICE_MARKUP`, подписки не затронуты. На размер контекста модели не влияет.

## Ежедневный обзор
Только paid, только группы, не banned, `daily_review_enabled=1`. Модель — `MODELS["opus"]["id"]`
независимо от модели чата (**осознанное исключение** из правила «служебное — только Haiku»: платная
фича, `billed=1`, `label='daily_review'`). Берутся ТОЛЬКО сообщения с полуночи по Берлину (`get_group_history(..., since_ts=)`), меньше 5 — чат пропускается (иначе молчащий чат каждую ночь получал бы Opus-пересказ старого за свой счёт). Промпт «фельетон» ~1000–1200 знаков, `max_tokens=1000`,
`trim_to_last_sentence` сохранён. Строку «Отключить ежедневный обзор: /review_off (админ чата)»
добавляет КОД; в `group_messages` обзор пишется БЕЗ неё (чтобы модель не копировала хвост в ответы).

## Влияние на размер контекста
Изменений system-prompt/истории/памяти/лимитов размера нет. Косвенно: чужие free-группы и
непроверенные личные чаты теперь активны (раньше блок «заявка админу»), но потолки истории/памяти
не тронуты. Опус-обзор — 1 вызов на paid-группу в сутки, вход — те же 100 сообщений.

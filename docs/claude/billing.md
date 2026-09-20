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
  `model`: для LLM — API-строка, для картинок — ключ провайдера (`gpt`/`banana`), для
  поиска — `tavily`. `label`: `dialog` (ответ на реплику: диалог/фото/файл), `should_search`,
  `search`, `image`, `draw_translate`, `extract_memory`, `extract_chat_memory`,
  `media_describe`, `daily_review`, `greet`, `greet_filter`, `captcha_*`, `/review`, `/search`,
  `проверка` (пробный запрос `_probe_model`) и т.д. Индексы `(chat_id, ts)`, `(ts)`.
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
   (`group_messages` / `conversations`) — $10 / $5 (`kind='bonus'`, `note='starter'`).
Ошибка миграции → откат, маркер не ставится, повтор при следующем старте (лог `Migration FAILED`).

## Конфиг (bot.py, рядом с MODELS)
`PRICE_MARKUP`, `IMAGE_PRICES`, `SEARCH_PRICE`, `STARTER_BONUS_*`, `FREE_IMAGES_*`,
`UNVERIFIED_*`, `VERIFY_MIN_TOPUP`, `ADMIN_CONTACT`, `FABLE_CONFIRM_WINDOW`. Сутки/неделя —
`BERLIN_TZ`, неделя с понедельника (`_day_start`, `_week_start`).
`PRICE_MARKUP` умножается в ОДНОМ месте — `_record_usage`; `calc_llm_cost()` возвращает
цену без наценки. Замороженные строки задним числом не пересчитываются.

## Привязка вызова к чату: `usage_ctx`
`contextvars.ContextVar` = `(chat_id, chat_type, user_id)`. Выставляет `gate_update`
(`TypeHandler(Update, ...)`, `group=-1`) на КАЖДЫЙ апдейт, для апдейтов без чата — `None`
(PTB обрабатывает апдейты последовательно в одном task'е, иначе контекст протёк бы).
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
  `tavily.search` (в `web_search`, Tavily берёт деньги и за пустую выдачу).
- `billed = (chat_tier(chat_id) == 'paid')`, `chat_id NULL → 0`.
- `db.record_usage` в одной транзакции (`BEGIN IMMEDIATE`) пишет строку и, если баланс ушёл
  ниже нуля, — `writeoff` на дефицит (`note='auto'`); возвращает `True`, только если ЭТА
  запись перевела чат paid→free → ровно одно сообщение о переходе (`_fire` — из потока
  через `run_coroutine_threadsafe` на `_main_loop`).
- Ошибка записи не роняет ответ (`try/except` + `logger.exception`).
- `label == 'dialog'` заодно увеличивает `daily_usage.msgs` — так считаются «сообщения, на
  которые бот реально ответил». Ответ картинкой на «нарисуй» (нет LLM-вызова) увеличивает счётчик
  явно в ветке рисования.
- Не учитывается: Gemini-транскрипция аудио (`_transcribe_audio_gemini`, POOL) и картинки banana
  как внешний расход — ТЗ описывает только kind `llm|image|search`; цена картинки — из `IMAGE_PRICES`.

## Тарифы
`chat_tier(chat_id)` — `paid`/`free` по балансу (`is_admin(chat_id)` → всегда paid).
- **free**: `get_chat_model` → Haiku, `get_chat_image_provider` → `gpt`, БЕЗ изменения строк
  `chat_models`/`chat_image_provider` — при возврате в paid прошлый выбор восстанавливается.
  `/sonnet /opus /fable /banana` → «доступно в платном режиме»; `/haiku`, `/gptimage` — «уже так»,
  без записи. Админ бота может менять настройки чужих free-чатов (запись без применения).
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
- **Как проверяют**: авто — `SUM(topup) >= VERIFY_MIN_TOPUP` (в `/topup`) → группа `approved` / юзер
  `premium` + стартовый бонус; вручную — `/verify <id>`, `/unverify <id>` (id < 0 → группа).
  `verify_target` идемпотентно выдаёт бонус ($10 группе / $5 личке), если не выдавался; остальные
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
- `/topup <chat_id> <сумма> [коммент]` — только админ. `>0` — `topup`, `<0` — `adjust` (сверх баланса →
  дополнительный writeoff в ноль). Ответ: баланс, режим, проверка; после записи — авто-проверка
  и уведомление в чат при смене тарифа.
- `/cost` — права: админ бота в личке — все чаты (+ «бесплатный режим», «служебное», «writeoff» за
  неделю); чат-админ в группе — этот чат; обычный юзер в личке — его личка; остальным — молчание
  (в т.ч. если не-админ передал аргумент). Работает в любом тарифе. Основные суммы — только
  `billed=1`. «Прошлая неделя (±%)» — изменение прошлой недели к позапрошлой (обе полные).
  `/cost <chat_id>` (админ) — детализация за 7 дней: модели, метки, дни, платное/бесплатное.
- `/verify /unverify /ban /unban /topup` — только `is_admin`. Удалены: `/whitelist(_on/_off)`,
  `/approve`, `/promote`, `/premium`, `/approve_chat`, `/reject_chat`, `/allow_chat`, `/deny_chat`,
  `/role`, `/pending` (покрыт `/chats`), `/migrate` (+ `db.migrate_from_json`, `allowed.json` и его
  volume-mount), реферальные ссылки (`ref_code` в `/start`, `get_referral_code`, `can_invite`…).

## Ежедневный обзор
Только paid, только группы, не banned, `daily_review_enabled=1`. Модель — `MODELS["opus"]["id"]`
независимо от модели чата (**осознанное исключение** из правила «служебное — только Haiku»: платная
фича, `billed=1`, `label='daily_review'`). Промпт «фельетон» ~1000–1200 знаков, `max_tokens=1000`,
`trim_to_last_sentence` сохранён. Строку «Отключить ежедневный обзор: /review_off (админ чата)»
добавляет КОД; в `group_messages` обзор пишется БЕЗ неё (чтобы модель не копировала хвост в ответы).

## Влияние на размер контекста
Изменений system-prompt/истории/памяти/лимитов размера нет. Косвенно: чужие free-группы и
непроверенные личные чаты теперь активны (раньше блок «заявка админу»), но потолки истории/памяти
не тронуты. Опус-обзор — 1 вызов на paid-группу в сутки, вход — те же 100 сообщений.

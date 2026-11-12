---
paths: ["db.py"]
---

# db.py

Слой данных SQLite (stdlib `sqlite3`, `db.py`). Схема живёт только в коде
(`init_db()`), миграции — идемпотентные `ALTER TABLE`/`UPDATE` в том же месте.

## Таблицы и где искать их семантику
db.py используется отовсюду, поэтому полная семантика каждой таблицы описана в
тематическом файле её фичи, а не здесь:
- `memory` — `docs/claude/memory.md`; `/forget` (v2.13.0): `forget_scope(user_id, context, chat_id)` — строки области только на чтение, `forget_apply(user_id, context, chat_id, keep_ids, clear_thread, snapshot)` — ОДНА транзакция `BEGIN IMMEDIATE`, удаляет РОВНО строки снимка `forget_scope` (факты по `id` кроме `keep_ids`, поля профиля по ключу+`updated_at`, предложения по `id`; позднее добавленное остаётся), по флагу личный тред; `snapshot=None` — снимок берётся внутри транзакции; `context=None` — всё везде, `forget_group_summary` (счёт треда — уже существовавшая `count_conversation`); `clear_memory` остаётся (профиль чистит тоже)
- `user_profile`, `profile_proposals` (профиль участника, `chat_id=0` для лички) — `docs/claude/profile.md`
- `group_messages` (в т.ч. `is_bot`, `hidden`), `chat_rate_limits` — `docs/claude/group-chat.md`; `hidden=1` и функции
  `hide_group_messages`/`unhide_group_message`/`list_hidden_group_messages` (без текста)/`group_message_ids_with_same_content` —
  `docs/claude/safety-unblock.md`. Любое новое чтение `group_messages` в промпт — с `hidden = 0`; `get_group_transcript` принимает
  `exclude_ids`/`min_id` (только пробы скана)
- `chat_models` — `docs/claude/models-and-costs.md`
- `chat_image_provider` — `docs/claude/images.md`
- `chat_search_mode` (`auto|gpt|tavily`; `claude` мигрирован в `auto`) — `docs/claude/models-and-costs.md`, `docs/claude/native-search.md`.
  `chat_free_model` с v2.0.0 не читается и не создаётся (в free единственная модель — Lite); в старых БД таблица осталась, не трогать
- Миграция `no_anthropic_models` (`_mig_no_anthropic`, `_run_once`): `chat_models` Claude → Gemini, id заморожены в db.py — `docs/claude/models-and-costs.md`
- `chat_persona` (режим характера чата, личка — `user_id`) — `docs/claude/personas.md`
- `chat_ignores` (автоигнор: `chat_id, user_id, level, ignored_until, last_strike_ts`) и колонки `allowed_chats.activity_percent` (дефолт 0), `allowed_chats.autoignore_enabled` (дефолт 1) — `docs/claude/group-chat.md`
- `chat_managers` (управляющие чата: `chat_id, user_id, added_by, added_at`, PK `(chat_id, user_id)`; права админа ЭТОГО чата без админки в Telegram) — `docs/claude/group-chat.md` → «Управляющие чата»
- `chat_review_persona` (отдельный режим характера ОБЗОРА; нет строки = «как у чата») — `docs/claude/personas.md`
- `dev_mode` (флаг режима разработчика), `dev_messages` (его тред, отдельно от `conversations`; индекс `(user_id, id)`) — `docs/claude/dev-mode.md`
- `allowed_chats` (в т.ч. `daily_review_enabled`, `status`) — `docs/claude/daily-review.md`;
  колонки `banned` (и `users.banned`), смысл `status` как «проверена/нет» — `docs/claude/billing.md`
- `broadcasts` / `broadcast_log` (рассылка админа бота: что слали и кому/чем кончилось; `pending` создаются сразу на всех получателей — основа `/broadcast resume`; `broadcast_recipients` — отбор активных чатов) — `docs/claude/broadcast.md`
- `star_payments` (платежи Telegram Stars, `charge_id` PK = идемпотентность; `star_payment_record` пишет платёж и `chat_credits` одной транзакцией, `star_refund_begin`/`star_refund_revert` — при возврате единственного платежа снимают проверку и отзывают остаток бонуса отрицательной строкой `kind='bonus'`, `star_payments_sum`). `grant_starter_bonus`/`_grant_bonus_conn` довыдают бонус до полного по ЧИСТОЙ сумме строк `kind='bonus'`, а не «если строка есть» — `docs/claude/billing.md` → «Пополнение Stars»
- `usage_log`, `chat_credits`, `daily_usage`, `schema_migrations` (баланс, тарифы, лимиты,
  однократные миграции) — `docs/claude/billing.md`
- `chat_extract_state` (`last_extract_id`) — ниже, каденс извлечения памяти

Здесь — только готчи уровня самих db.py-функций, не привязанные к одной фиче.

## `count_chat_messages()` немонотонна — не использовать для каденса
`save_group_message` подрезает `group_messages` до `GROUP_MESSAGES_KEEP` (3000, было 1000) строк на чат, поэтому
`COUNT(*)` перестаёт расти и любая проверка `% N == 0` от него залипает навсегда.
Именно так однажды перестала обновляться групповая память (инцидент 2026-07-26,
`docs/claude/incidents.md`). Для каденса использовать `should_extract_chat_memory()`,
а не счётчик. Сама `count_chat_messages` больше нигде не вызывается — оставлена как
справочная, не удалять и не возвращать в каденс.

## `should_extract_chat_memory`: дельту брать по `id` чата, не `MAX(id)` глобально
`group_messages.id` — глобальный AUTOINCREMENT на все чаты, и болтовня в соседней
группе продвигала бы счётчик тихому чату (извлечение = вызов Haiku на 50 реплик —
запускалось бы почти на каждом сообщении). Считаются строки **этого** чата новее
`last_extract_id` (таблица `chat_extract_state`).

## `db.get_group_history()` vs `get_group_transcript()` — не путать
`get_group_history()` (строки «Имя: текст») оставлена специально — её используют
`daily_chat_review` и `cmd_review` (`docs/claude/daily-review.md`). `get_group_transcript()`
кормит `build_group_messages()` для основного диалога (`docs/claude/group-chat.md`).
Разные форматы для разных потребителей — не сливать в одну функцию и не удалять первую.
Исключение: дневной обзор и `/review` с v1.10.0 берут `get_group_messages_range(chat_id, since_ts, until_ts)` — dict'ы
`{id, ts, sender, text, is_bot}` по возрастанию времени, БЕЗ LIMIT по числу (размер ограничивает вызывающий:
`build_review_log` режет реплики, ветки A/B держат общий лимит; `docs/claude/daily-review.md`). `get_group_history`
остаётся для остальных потребителей.
`get_group_transcript()` отдаёт `user_id` каждой строки (может быть None; читает его только групповое извлечение памяти — метки участников, `docs/claude/memory.md`; `build_group_messages`, обзор и скан ключ не используют). `get_user_id_by_name_in_chat` удалена (v2.7.2) — факты и профили группы только по `user_id`, не по нику. `get_group_transcript()` отдаёт и `id` строки, а `get_conversation_rows()` (личка, порядок по `id`) — строки с `id`:
по ним якорь окна истории шагами (`pick_history_window`, `docs/claude/prompt-cache.md`). `get_conversation()` (без id)
остался для счётчика сообщений.

## Паттерн: `UPDATE`-функция обязана возвращать `bool` (нашлась ли строка)
`db.set_chat_status()` раньше делала голый `UPDATE allowed_chats SET status=... WHERE
chat_id=?` без проверки `rowcount` — вызывающий код (тогда `cmd_approve_chat`, удалён в ТЗ v0.10) не мог отличить
«обновили» от «строки не было вообще», и рапортовал успех, когда `UPDATE` молча не менял
ноль строк. Починено: `set_chat_status` теперь возвращает `bool` (`rowcount > 0`), вызывающий
код обязан его проверить. Живой случай — инцидент 2026-09-16/17 в `docs/claude/incidents.md`.
Тот же `rowcount`-паттерн у `set_chat_review_enabled` (`docs/claude/daily-review.md`).
Пиши новые `UPDATE`-функции по этому же контракту, если вызывающий код должен знать,
сработало ли обновление, — не полагайся на «ноль строк = ошибка будет видна и так».

## `allowed_chats.chat_id > 0` — личные чаты, ошибочно попавшие в таблицу
Telegram ID групп/супергрупп/каналов отрицательные, личных чатов — положительные (это
сам `telegram_id` пользователя). `allowed_chats` по смыслу — только группы; до фикса
2026-09-19 `handle_new_chat` не проверял `chat.type` и писал туда личку тоже (см.
инцидент 2026-09-19 в `docs/claude/incidents.md`) — `my_chat_member` Telegram шлёт и на
`/start`/разблокировку бота в личке, не только на добавление в группу. Функционального
влияния такие строки не оказывают, пока остаются `status='pending'` (`is_group_verified`
читает только `status='approved'`, личный гейтинг `allowed_chats` вообще не трогает) — но если
такую строку по ошибке проверить (`/verify`), в неё начнёт слать `daily_chat_review`
(`get_review_chats` берёт только `chat_id < 0`, так что и тут защищено). Посмотреть, сколько таких строк накопилось (read-only):
```sql
SELECT chat_id, name, status, created_at FROM allowed_chats WHERE chat_id > 0;
```
Удалять или нет — решает Алексей, автоматической чистки/миграции нет и не планируется:
```sql
DELETE FROM allowed_chats WHERE chat_id > 0;
```

## Паттерн потолка на чтение: `ROW_NUMBER() OVER (PARTITION BY ...)`
`get_memory`, `get_memory_for_private`, `get_all_chat_memory` (`docs/claude/memory.md`)
все используют один и тот же паттерн для потолка «N самых свежих строк на группу»:
`ROW_NUMBER() OVER (PARTITION BY <ключ группировки>, tier ORDER BY created_at DESC)`,
затем `WHERE rn <= N`. Копируй этот паттерн для новых источников данных с похожим
требованием «топ-N на партицию», а не изобретай новый способ пагинации.

## Общий инвариант (не только про db.py)
Любой источник данных из db.py, попадающий в system-prompt или в `messages`, ОБЯЗАН
иметь лимит по РАЗМЕРУ на чтении, а не только по количеству элементов — таблицы растут,
и без лимита на чтении промпт рано или поздно перестаёт помещаться в окно модели.
Добавляешь новую читающую функцию для system-prompt/messages — сразу с лимитом.

## Транзакции с балансом: `BEGIN IMMEDIATE` и округление
`db.record_usage` и `db.settle_negative_balance` читают баланс и пишут writeoff в ОДНОЙ
транзакции с `BEGIN IMMEDIATE` — иначе два параллельных вызова оба увидели бы «баланс ещё
плюс» и оба ушли бы в минус. `get_balance` округляет до 8 знаков, а тариф paid — это баланс >
`PAID_MIN_BALANCE` (0.0001), а не «> 0»: пыль от float-сложений не должна оставлять чат в
платном режиме. Новые функции, меняющие баланс, — тем же паттерном. Однократные миграции данных
— только через `_run_once` (маркер в `schema_migrations`), не голыми `UPDATE` в списке `ALTER`.

## Платные интервалы по журналам (только чтение)
`subscription_events` / `paid_seconds_from_events` / `billed_outside_paid` / `paid_seconds` восстанавливают, сколько времени чат был в paid, из
`chat_credits` (все `kind`) и `usage_log billed=1` — те же слагаемые, что `_BALANCE_SQL`. Схему не трогают; чистая часть не ходит в БД (её берёт
`scripts/check_subscription_estimate.py` без импорта bot.py). Изменишь формулу баланса — поправь и их. `docs/claude/billing.md` → «Оценка цены подписки».

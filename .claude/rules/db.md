---
paths: ["db.py"]
---

# db.py

Слой данных SQLite (stdlib `sqlite3`, `db.py`). Схема живёт только в коде
(`init_db()`), миграции — идемпотентные `ALTER TABLE`/`UPDATE` в том же месте.

## Таблицы и где искать их семантику
db.py используется отовсюду, поэтому полная семантика каждой таблицы описана в
тематическом файле её фичи, а не здесь:
- `memory` — `docs/claude/memory.md`
- `group_messages` (в т.ч. `is_bot`), `chat_rate_limits` — `docs/claude/group-chat.md`
- `chat_models` — `docs/claude/models-and-costs.md`
- `chat_image_provider` — `docs/claude/images.md`
- `allowed_chats` (в т.ч. `daily_review_enabled`, `status`) — `docs/claude/daily-review.md`
- `chat_extract_state` (`last_extract_id`) — ниже, каденс извлечения памяти

Здесь — только готчи уровня самих db.py-функций, не привязанные к одной фиче.

## `count_chat_messages()` немонотонна — не использовать для каденса
`save_group_message` подрезает `group_messages` до 1000 строк на чат, поэтому
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

## Паттерн: `UPDATE`-функция обязана возвращать `bool` (нашлась ли строка)
`db.set_chat_status()` раньше делала голый `UPDATE allowed_chats SET status=... WHERE
chat_id=?` без проверки `rowcount` — вызывающий код (`cmd_approve_chat`) не мог отличить
«обновили» от «строки не было вообще», и рапортовал успех, когда `UPDATE` молча не менял
ноль строк. Починено: `set_chat_status` теперь возвращает `bool` (`rowcount > 0`), вызывающий
код обязан его проверить. Живой случай — инцидент 2026-09-16/17 в `docs/claude/incidents.md`.
Тот же `rowcount`-паттерн у `set_chat_review_enabled` (`docs/claude/daily-review.md`).
Пиши новые `UPDATE`-функции по этому же контракту, если вызывающий код должен знать,
сработало ли обновление, — не полагайся на «ноль строк = ошибка будет видна и так».

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

# Чек-лист стенда: ТЗ `fix/memory-uid` (групповая память и профили по user_id, v2.7.2)

ТЗ — `docs/claude/tz-memory-uid.md`, описание — `docs/claude/memory.md` («Извлечение»). Места **TG-админ / TG-юзер / Сервер** и функции `q` / `logs` —
`docs/claude/staging-aliases.sh`. Схема БД не меняется, новых файлов и volume нет — выкатка `/update`. Юнит-тесты:
`python -m pytest scripts/test_memory_uid.py -q`. Нужны ДВА тестовых аккаунта (A и B) в группе стенда. Каденс извлечения —
`MEMORY_EXTRACT_EVERY_CHAT` = 15 реплик в чате (болтовню бота тоже считает).

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | `q "SELECT user_id FROM group_messages WHERE chat_id=<id> ORDER BY id DESC LIMIT 3"` | `user_id` заполнен (иначе строки не попадут в извлечение). Узнать user_id аккаунтов A и B |
| 2 | TG-юзер A | В группе: «я живу в Бремене» | Сообщение сохранено |
| 3 | TG-юзер B | Сменить имя профиля на ТОЧНО такое же, как у A (имя в Telegram), написать в группу: «я живу в Кёльне» | Сообщение сохранено под тем же ником, что у A |
| 4 | TG-юзер A | Написать о себе другое: «работаю инженером» | |
| 5 | Оба | Добавить болтовни до 15+ реплик с последнего извлечения | В `logs`: «Group memory extracted for chat …» БЕЗ warning «неизвестная метка» / «нет метки и ник … не однозначен» |
| 6 | Сервер | `q "SELECT user_id, fact, tier FROM memory WHERE context='group' AND chat_id=<id> ORDER BY id DESC LIMIT 20"` | У A — своё (Бремен, инженер), у B — своё (Кёльн); факты A не лежат у B и наоборот, хотя ник один |
| 7 | Сервер | `q "SELECT user_id, field, new_value FROM profile_proposals WHERE chat_id=<id> ORDER BY id DESC LIMIT 10"` | Предложения (город) — у каждого по своему user_id |
| 8 | TG-юзер A и B | `/memory` у каждого | Каждый видит свои факты |
| 9 | Сервер | `logs 200 \| grep -i "group memory"` | Нет Traceback; нет «Group memory extraction error» |
| 10 | TG-юзер B | Вернуть своё имя профиля | Новые факты B — по-прежнему на B (метки по user_id, ник не важен) |

# Чек-лист стенда: `feat/reactions-read-set` (v2.25.0)

ТЗ — `docs/claude/tz-reactions.md`, описание — `docs/claude/group-chat.md` → «Реакции». Схема БД расширена (колонка `group_messages.tg_message_id`, таблица `message_reactions`), новый handler `message_reaction` — стенд обязателен.
Прогон — тестерами (`docs/claude/staging.md` → «Тестер стенда», «Практика»). В отчёт и репозиторий — только `a`/`b`, без ников, названий групп и `user_id`.

**Кто есть кто.** Группа — `TESTER_GROUP_1`: `a` — админ в Telegram, `b` — участник; оба НЕ админы бота. **Бот в группе должен быть админом** — иначе `message_reaction` не приходит
(проверка: `getChatMember` бота из контейнера, статус `administrator`).

## Подготовка
1. `~/claudushka-test`: `git fetch && git checkout --detach origin/feat/reactions-read-set`, `docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test`. В логе «Application started», нет `Traceback`.
2. Миграция: `PRAGMA table_info(group_messages)` — есть `tg_message_id`; таблица `message_reactions` создана (на копии прод-БД и на пустой).
3. Локально: `python -m pytest scripts/test_reactions.py scripts/test_stand_tester.py -q`; `tester check` — все сценарии `reactions-*` разбираются.
4. Группа платная (баланс > 0) — для основного диалога; сама проверка реакций тариф не смотрит.

## Шаги
| # | Действие | Ожидание |
|---|---|---|
| 1 | Выкатка ветки, миграция | см. «Подготовка» 1–2 |
| 2 | `tester run reactions-read` (начало): `a!:`/`b!:`, затем `a: @бот …` | в `group_messages` у новых строк людей и у ответа бота `tg_message_id` заполнен (`SELECT id, is_bot, tg_message_id FROM group_messages ORDER BY id DESC LIMIT 8`) |
| 3 | там же: `b react: bot 1 😁`, `a see: bot 1` (в Telegram реакция видна), затем `a:` обращение | строка в `message_reactions` (id `b`, 😁); в логе `PROMPT … reactions_note=N`, N > 0 — строка «Реакции людей…» ушла в хвост запроса; бот в ответе может на неё сослаться |
| 4 | там же: `b react: bot 2 -` | строка из `message_reactions` исчезла сразу; в Telegram счётчик у других зрителей может обновиться с задержкой до минуты — в сценарии `sleep 20` |
| 5 | `tester run reactions-auto` (≈ 15 мин, через `nohup`): 4 раунда явно смешных/тёплых реплик без обращения к боту | `usage_log` `label=reaction_check` на чат группы; в `message_reactions` строки с id бота — НЕ на каждый раунд (по умолчанию «не ставить»); бот не отвечал текстом |
| 6 | Кулдаун и дневной лимит | **выбор: юнит-тест** (`test_should_check_positive_and_each_ban`: кулдаун 3 мин, ≥3 новых строк, лимит 15 в сутки, автоигнор, личка) — на стенде не гоняем, чтобы не менять константы и не слать десятки сообщений; косвенно: раунды шага 5 идут с паузой `REACTION_RECHECK_MIN` |
| 7 | Конфликт со спонтанной: `tester run reactions-conflict` → `docker exec -i claudushka-test python3 - <chat_id> < scripts/stand_reaction_conflict.py` → `tester run reactions-conflict-after` | порядок `set_reaction`: `['😁', '👀', '😁']`, на сообщении в итоге 😁 (а не пусто и не 👀). Спонтанную ставит скрипт (реальный Telegram, код бота `_queue_reaction`/`react`/`unreact`), потому что заставить модель реагировать именно на сообщение с поиском нельзя |
| 8 | Группа с урезанными реакциями (в Telegram: настройки группы → Реакции → только часть эмодзи) | бот не ставит запрещённое: в `reaction_check` в списке разрешённых только допустимые; **нужен Алексей** (настройка группы) |
| 9 | `usage_log` за прогон | все вызовы `reaction_check` на чате группы, с меткой, `billed=1`; сумма — в отчёт |

## Сверка в БД после прогона
- `SELECT * FROM message_reactions` — только реакции людей из шагов 3–4 и бота из шагов 5/7; на сообщения без `group_messages`-строк реакций нет.
- Реакции срезанных `GROUP_MESSAGES_KEEP` строк удаляются в том же `save_group_message` — на стенде не проверить (нужно 3000 строк): юнит-тест `test_reactions_purged_with_group_messages`.

## Не проверено этим чек-листом
- Личка (в задаче не делается), анонимные реакции (`message_reaction_count`), стикеры (в транскрипт не пишутся).
- Шаг 8 без Алексея; поведение на группах прода, где бот не админ — список снимается отдельно (см. отчёт).

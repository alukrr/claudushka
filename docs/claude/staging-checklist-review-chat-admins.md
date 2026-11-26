# Чек-лист стенда: `feat/review-chat-admins` (v2.24.0)

ТЗ — `docs/claude/tz-review-chat-admins.md` (issue #7), описание — `docs/claude/daily-review.md` → «`/review` для админов чата». Схема БД не меняется.
Прогон — тестерами (`docs/claude/staging.md` → «Тестер стенда», «Практика»). В отчёт и репозиторий — только `a`/`b`, без ников, названий групп и `user_id`.

**Кто есть кто.** Группа — `TESTER_GROUP_1` (во всех тестовых группах `a` — админ в Telegram, `b` — обычный участник); оба НЕ админы бота. Админа бота (шаг 9) изображает Алексей руками.

## Подготовка
1. `~/claudushka-test`: `git fetch && git checkout --detach origin/feat/review-chat-admins`, `docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test`. В логе «Application started», нет `Traceback`.
2. Локально до стенда: `python -m pytest scripts/test_review_access.py scripts/test_review_topics.py scripts/test_review_persona.py -q` — зелёные; `tester check` — оба сценария разбираются.
3. **Баланс группы > 0** (платный режим): `SELECT chat_id, balance_usd FROM chat_credits` по `chat_id` группы (схема — `.claude/rules/db.md`). Пополнение — Алексей: `/topup <id> 5 стенд force`.
4. В `data-tester/tester.env` есть `TESTER_GROUP_1` и `TESTER_B_ID` (user_id `b` из `tester whoami`) — добавляет Алексей или тот, у кого доступ; не печатать.
5. В группе за последний час не было обзора (ни ежедневного 21:00, ни `/review`): `SELECT MAX(timestamp) FROM group_messages WHERE chat_id=<id> AND is_bot=1 AND content LIKE '(обзор дня по%'`.
   Обзор в 21:00 Берлина на стенде сработает для группы, если дневной обзор у неё включён — прогон либо до 21:00, либо после 22:00, либо `/review_off` в группе.

## Шаги (сценарии — `scripts/stand_scenarios/review-access.txt`, `review-access-2.txt`)
| # | Действие | Ожидание |
|---|---|---|
| 1 | `tester run review-access` — шаг «наполнить окно» | 8 реплик в группе |
| 2 | `b: /review` (обычный участник) | отказ правами «только для админов чата…» (не кулдауном) |
| 3 | `a: /review` (админ чата, платный) | приходит обзор (формат обзора, без «Маловато…»); расход чата — шаг 8 |
| 4 | `a: /review` сразу ещё раз | «Обзор уже был в HH:MM, следующий — после HH:MM» (время Берлина, +60 мин) |
| 5 | **Рестарт стенда** (`docker compose -f docker-compose.test.yml restart claudushka-test`, только с согласия Алексея), затем `tester run review-access-2` | кулдаун на месте; `b` без прав — отказ правами; `a: /manager add b` → `b: /review` — КУЛДАУН (значит, права управляющего прошли), не отказ правами; `/manager rm` → `b` снова отказ правами |
| 6 | Free-чат: отказ «Обзор — в платном режиме: пополнить — /pay.» | **выбор:** юнит-тестом `test_chat_admin_in_free_denied_by_tier` (порядок проверок на заглушках); на стенде — только с согласия Алексея (`/topup <id> -<остаток> стенд force` и обратно) |
| 7 | Ежедневный обзор считается: после обзора в 21:00 `/review` админа чата до 22:00 — отказ кулдауном | по желанию, в рамках дня; в БД та же строка `(обзор дня по …` |
| 8 | Расход: `SELECT ts, label, chat_id, model, cost_usd FROM usage_log ORDER BY id DESC LIMIT 10` (колонки — по схеме) | все вызовы обзора с `label` (`/review chat=…`, `daily_review_chunk` если ветка B), `chat_id` — группа тестера, не лишних строк |
| 9 | Алексей (админ бота) в группе: `/review` в кулдауне | проходит (обзор приходит) — без кулдауна и тарифа |

## Сверка в БД после прогона
- `SELECT timestamp, substr(content,1,40) FROM group_messages WHERE chat_id=<id> AND is_bot=1 AND content LIKE '(обзор дня по%' ORDER BY id DESC LIMIT 3` — строка на каждый успешный обзор; у отказов (шаги 2, 4, 5) строки нет.
- После шага 5 `SELECT * FROM chat_managers WHERE chat_id=<id>` — пусто (`rm` отработал).

## Не проверено этим чек-листом
- Free-режим живьём (по умолчанию — юнит-тест, см. шаг 6); поведение при ошибке API (кулдаун не начинается — по коду, строка не пишется).

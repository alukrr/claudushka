# Чек-лист стенда: ТЗ `feat/opus-5-5` (Opus 5 → Opus 5.5, учёт стоимости)

Как поднять стенд — `docs/claude/staging.md`, как устроен учёт — `docs/claude/models-and-costs.md`
(«Учёт токенов», «Миграция chat_models»). Места **TG-админ / TG-юзер / Сервер** и функции `q` / `qw` / `logs` —
как в `docs/claude/staging-checklist-v0.10.md`, раздел 0.2 (вставить в сеанс SSH заново).

Переменные, которые понадобятся (на сервере, в том же сеансе):

```bash
GID=-100...     # тестовая группа (/id в группе)
TUID=...        # (не UID — в bash он readonly) второй, НЕ админский аккаунт (/id у него в личке с ботом)
ADM=592441      # ваш админский аккаунт = id вашей лички с ботом
```

Формула ожидаемой цены Opus 5.5 для сверки (`PRICE_MARKUP=1.0`, значит `cost_usd` = базовая цена).
Запись в кэш хранится одной суммой `cache_write`, поэтому считаем две границы: всё как 5m (×5.00) и всё как 1h (×8.00).
`cost_usd` должен попасть между ними (если 1h-записей нет — ровно в `min`):

```bash
cost55() { q "SELECT id, label, model, input, output, thinking, cache_write, cache_read, cost_usd,
  ROUND((input*4.0 + output*20.0 + cache_read*0.20 + cache_write*5.0)/1e6, 8) AS min,
  ROUND((input*4.0 + output*20.0 + cache_read*0.20 + cache_write*8.0)/1e6, 8) AS max
  FROM usage_log WHERE kind='llm' ORDER BY id DESC LIMIT ${1:-5}"; }
```

---

## 0. Подготовка: засеять состояние «до миграции» (на СТАРОМ коде)

Миграция `chat_models` срабатывает при старте бота. Строки со старым `claude-opus-5` надо положить в БД
**до** переключения стенда на ветку, иначе проверять будет нечего.

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | Стенд поднят на `main` (или любой ветке до `feat/opus-5-5`): `logs 20` | `Клодушка started`, контейнер живой |
| 2 | Сервер | Группа — платная и на старом Opus: `qw "INSERT OR REPLACE INTO chat_models VALUES ($GID, 'claude-opus-5')"`. Баланс: `/cost` в группе → `Режим: платный`; если нет — TG-админ `/topup <GID> 5` | `изменено строк: 1` |
| 3 | Сервер | Личка TUID — старый Opus, но **бесплатный** режим: `qw "INSERT OR REPLACE INTO chat_models VALUES ($TUID, 'claude-opus-5')"` | `изменено строк: 1` |
| 4 | TG-юзер | `/cost` в личке с ботом | `бесплатный`. Если платный — обнулить баланс (шаг 5) |
| 5 | Сервер | (только если в шаге 4 платный) `qw "INSERT INTO chat_credits (ts, chat_id, amount, kind, note) SELECT strftime('%s','now'), $TUID, -(COALESCE((SELECT SUM(amount) FROM chat_credits WHERE chat_id=$TUID),0) - COALESCE((SELECT SUM(cost_usd) FROM usage_log WHERE chat_id=$TUID AND billed=1),0)), 'adjust', 'стенд opus-5-5'"` | `/cost` у TG-юзера → `бесплатный` |
| 6 | Сервер | Легаси-строка истории Opus 5 (billed=0, баланс не трогает): `qw "INSERT INTO usage_log (ts, chat_id, chat_type, kind, model, label, input, output, cost_usd, billed) VALUES (strftime('%s','now'), $GID, 'group', 'llm', 'claude-opus-5', 'стенд_легаси', 1000000, 0, 5.0, 0)"` | `изменено строк: 1` |
| 7 | Сервер | Зафиксировать «до»: `q "SELECT * FROM chat_models WHERE chat_id IN ($GID, $TUID)"` | Обе строки `claude-opus-5` |

## 1. Переключение и миграция

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | `cd ~/claudushka-test && git fetch origin && git checkout origin/feat/opus-5-5 && docker compose -f docker-compose.test.yml up -d --force-recreate` | — |
| 2 | Сервер | `logs 80` (подождать pip install) | `Database initialized`, `Клодушка started`, нет `Traceback` / `Migration FAILED` |
| 3 | Сервер | `q "SELECT * FROM chat_models WHERE chat_id IN ($GID, $TUID)"` | Обе строки `claude-opus-5-5` |
| 4 | Сервер | `q "SELECT model, COUNT(*) AS n FROM chat_models GROUP BY model"` | Нет `claude-opus-5` ровно; `claude-sonnet-5`, `claude-fable-5-1` на месте, как были |
| 5 | Сервер | История не тронута: `q "SELECT model, label, cost_usd FROM usage_log WHERE label='стенд_легаси'"` | `claude-opus-5`, `5.0` |
| 6 | Сервер | Новая колонка: `q "SELECT name, dflt_value FROM pragma_table_info('usage_log') WHERE name='thinking'"` | Одна строка, `dflt_value='0'` |
| 7 | Сервер | Идемпотентность: `docker compose -f docker-compose.test.yml restart`, `logs 40`, повторить шаги 3–6 | Всё то же, ошибок нет |

## 2. `/opus` в личке (ТЗ, проверка 1)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-админ | В личке `/sonnet`, потом `/opus` | `… → Opus 5.5.` **Если** `Opus 5.5 сейчас недоступна` — стоп, прислать `logs 60`: проба `max_tokens=1` не прошла с включённым thinking (известный риск) |
| 2 | Сервер | `q "SELECT model, label, output, thinking FROM usage_log WHERE label LIKE 'проверка%' ORDER BY id DESC LIMIT 1"` | `model='claude-opus-5-5'` |
| 3 | TG-админ | Спросить что-нибудь с рассуждением («Сколько будет 17×23 и почему?») | Нормальный ответ, не пустой и не обрезанный |
| 4 | Сервер | `cost55 3` | Строка `dialog`: `model='claude-opus-5-5'`, `min ≤ cost_usd ≤ max`, `thinking ≤ output` |
| 5 | TG-админ | Ещё 2–3 сообщения подряд в том же диалоге | Ответы идут. В `cost55` у последующих строк `cache_read > 0` (кэш подхватился) и цена по-прежнему в границах |
| 6 | TG-админ | `/models` | `▸ Opus 5.5`, цена $4/$20 |
| 7 | TG-админ | `/help` | `/opus — Opus 5.5`, `Opus 5.5 — $4/$20` |

## 3. `/opus` в группе с длинной историей (ТЗ, проверка 2 + проверка 5)

Группа `GID` после миграции уже на Opus 5.5 — отдельно `/opus` в ней **не** писать, так заодно проверяется
«старая настройка → отвечает 5.5».

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-админ | `/models` в группе | `▸ Opus 5.5` (не Opus 5 и не «нет в реестре») |
| 2 | Группа | Накидать историю: 20–30 реплик с обоих аккаунтов (можно без обращения к боту) | — |
| 3 | Группа | Обратиться к боту (упоминание `@бот` или реплай на его сообщение) 3–4 раза подряд, в т.ч. реплаем на его же ответ | Каждый раз ответ по делу, без «Что-то сломалось» |
| 4 | Сервер | `logs 100 \| grep -iE "400\|invalid_request\|thinking\|Traceback"` | Пусто |
| 5 | Сервер | `cost55 5` | Строки `dialog` с `model='claude-opus-5-5'`, цена в границах |
| 6 | Группа | `/review` | Обзор пришёл целиком |
| 7 | Сервер | `logs 60 \| grep "обрезан по max_tokens"` | Пусто. Если есть — thinking съедает лимит `/review` (500), прислать строку лога |

## 4. Дневной обзор (ТЗ, проверка 3)

Без ручного запуска — только в 22:00 по Берлину. Условия и предупреждения — как B11 в
`docs/claude/staging-checklist-v0.10.md` (≥ 5 сообщений в `GID` за сегодня, `daily_review_enabled=1`, другие чаты
тестовой БД без баланса).

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Группа | После 22:00 | Обзор пришёл, строка про `/review_off` последней |
| 2 | Сервер | `q "SELECT model, label, billed, output, thinking, cost_usd FROM usage_log WHERE label='daily_review' ORDER BY id DESC LIMIT 1"` | `model='claude-opus-5-5'`, `billed=1` |
| 3 | Сервер | `logs 200 \| grep "Дневной обзор обрезан"` | Пусто (лимит 1000 токенов включает thinking) |

Не хотите ждать — проверить в первую ночь на проде тем же SELECT'ом (с `docker exec claudushka`).

## 5. `/cost` и цены (ТЗ, проверка 4)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-админ | В личке `/cost <GID>` | «Платное»: `Opus 5.5: N выз., …` (у Opus 5.5 с `(из них thinking …)`, если thinking был); «Бесплатное»: `Opus 5: 1 выз. … — $5.00` (легаси-строка, подпись **без** «нет в реестре») |
| 2 | TG-админ | В группе `/sonnet`, одно обращение к боту | Ответ Sonnet |
| 3 | Сервер | `q "SELECT model, input, output, cache_write, cache_read, cost_usd, ROUND((input*2.0+output*10.0+cache_read*0.2+cache_write*2.5)/1e6,8) AS min, ROUND((input*2.0+output*10.0+cache_read*0.2+cache_write*4.0)/1e6,8) AS max FROM usage_log WHERE model='claude-sonnet-5' ORDER BY id DESC LIMIT 1"` | `min ≤ cost_usd ≤ max` (Sonnet по $2/$10) |
| 4 | TG-админ | В группе `/fable` (один раз, без повтора) | Предупреждение «примерно в 5 раз дороже» (50/10 от Sonnet). Не подтверждать |
| 5 | TG-админ | Вернуть группу: `/opus` | `… → Opus 5.5.` |

## 6. Free-чат с сохранённым Opus (ТЗ, проверка 6)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-юзер | `/models` в личке | Режим бесплатный, `▸ Haiku 4.5`, строка `Прошлый выбор (Opus 5.5) вернётся в платном режиме.` |
| 2 | TG-юзер | Любое сообщение боту | Ответ; `q "SELECT model FROM usage_log WHERE chat_id=$TUID ORDER BY id DESC LIMIT 1"` → `claude-haiku-4-5-20251001` |
| 3 | Сервер | `q "SELECT model FROM chat_models WHERE chat_id=$TUID"` | Всё ещё `claude-opus-5-5` (free её не трогал) |
| 4 | TG-админ | `/topup <TUID> 3` | У TG-юзера: `Платный режим включён: Opus 5.5, картинки — …` |
| 5 | TG-юзер | Сообщение боту | Ответ; последняя строка `usage_log` для `$TUID` — `claude-opus-5-5` |

## 7. Логи (ТЗ, проверка 7)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | `docker logs claudushka-test 2>&1 \| grep "нет в таблице цен"` | Пусто. Если есть — **прислать строку целиком**: в ней `requested=` и `response.model=`, по ним добавлю фактическую строку в `LEGACY_PRICES`/`MODELS` |
| 2 | Сервер | `q "SELECT model, COUNT(*) AS n FROM usage_log WHERE kind='llm' GROUP BY model"` | Только известные строки: `claude-haiku-4-5-20251001`, `claude-sonnet-5`, `claude-opus-5-5`, `claude-opus-5` (легаси), возможно `claude-opus-4-8` (был фолбэк safeguards — это нормально, но сообщить) |
| 3 | Сервер | `docker logs claudushka-test 2>&1 \| grep -E "Traceback\|ERROR" \| tail -20` | Пусто или только известный шум |

## 8. (Опционально) Миграция на копии боевой БД

Проверяет миграцию на реальных данных и заодно команду бэкапа из ТЗ. Прод бэкап не ломает (онлайн-копия SQLite).

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | Бэкап прода (команда из ТЗ): `cd ~/claudushka && docker exec claudushka python3 -c "import sqlite3; s=sqlite3.connect('/app/data/claudushka.db'); d=sqlite3.connect('/app/data/claudushka.db.bak-opus55'); s.backup(d); d.close()"` | Файл `~/claudushka/data/claudushka.db.bak-opus55` |
| 2 | Сервер | Сколько чатов на старом Opus в проде: `docker exec claudushka python3 -c "import sqlite3; print(sqlite3.connect('/app/data/claudushka.db').execute(\"SELECT model, COUNT(*) FROM chat_models GROUP BY model\").fetchall())"` | Запомнить число `claude-opus-5` |
| 3 | Сервер | `cd ~/claudushka-test && docker compose -f docker-compose.test.yml down && rm -rf data-test && mkdir data-test && cp ~/claudushka/data/claudushka.db.bak-opus55 data-test/claudushka.db && docker compose -f docker-compose.test.yml up -d` | — |
| 4 | Сервер | **Сразу**: `qw "UPDATE allowed_chats SET daily_review_enabled=0"` — иначе в 22:00 стенд потратит Opus на боевые чаты | `изменено строк: N` |
| 5 | Сервер | `q "SELECT model, COUNT(*) AS n FROM chat_models GROUP BY model"` | `claude-opus-5` нет; `claude-opus-5-5` = было Opus 5 + было Opus 5.5 (скорее всего 0) |
| 6 | Сервер | `q "SELECT model, COUNT(*) AS n, ROUND(SUM(cost_usd),4) AS usd FROM usage_log WHERE model='claude-opus-5'"` | Совпадает с продом до миграции (история не тронута) |
| 7 | Сервер | **Убрать прод-данные со стенда**: `docker compose -f docker-compose.test.yml down && rm -rf data-test && mkdir data-test` | Стенд снова пустой |

Бэкап `claudushka.db.bak-opus55` на проде при этом уже готов, но **перед реальным `/update` его всё равно сделать заново**:
миграция идёт при старте бота, бэкап должен быть снят с состояния прямо перед ней.

---

## Что прислать по итогам
1. По разделам 1–7 (и 8, если делали): совпало / не совпало; если нет — что увидели.
2. Вывод `cost55 10` после раздела 3.
3. Любые строки с `нет в таблице цен`, `обрезан по max_tokens`, `400`/`invalid_request`.
4. Если `/opus` сказал «недоступна» — `logs 60` целиком.

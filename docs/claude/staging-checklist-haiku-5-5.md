# Чек-лист стенда: ТЗ `feat/haiku-5-5` (Haiku 4.5 → Haiku 5.5, `claude-haiku-5-5`)

Как поднять стенд — `docs/claude/staging.md`, как устроен учёт — `docs/claude/models-and-costs.md`
(раздел «Haiku 4.5 → Haiku 5.5», «Учёт токенов», «Миграция chat_models»). Места **TG-админ / TG-юзер / Сервер**
и функции `q` / `qw` / `logs` — как в `docs/claude/staging-checklist-v0.10.md`, раздел 0.2.

```bash
GID=-100...     # тестовая группа
TUID=...        # второй, НЕ админский аккаунт
ADM=592441      # админский аккаунт

# Ожидаемая цена Haiku 5.5 по запросу (PRICE_MARKUP=1.0). Ступень выбирается ПО ЗАПРОСУ:
# input+cache_write+cache_read > 100000 → 0.50/2.50, иначе 0.10/0.50. Запись в кэш хранится суммой,
# поэтому две границы: всё 5m (×1.25) и всё 1h (×2.0).
cost55h() { q "SELECT id, label, model, input, output, thinking, cache_write, cache_read, cost_usd,
  ROUND((input + cache_write + cache_read) > 100000) AS tier,
  ROUND(CASE WHEN input+cache_write+cache_read > 100000
    THEN (input*0.5 + output*2.5 + cache_read*0.05 + cache_write*0.625)
    ELSE (input*0.1 + output*0.5 + cache_read*0.01 + cache_write*0.125) END/1e6, 8) AS min_5m
  FROM usage_log WHERE kind='llm' ORDER BY id DESC LIMIT ${1:-5}"; }
```

`cost_usd` должен совпасть с `min_5m` (нет 1h-записей) либо лежать между `min_5m` и «всё как 1h».

---

## 0. Подготовка и миграция (на СТАРОМ коде засеять, потом переключить стенд на ветку)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | На `main`: `qw "INSERT OR REPLACE INTO chat_models VALUES ($GID, 'claude-haiku-4-5-20251001')"`; в группе платный баланс (`/topup <GID> 5` от админа) | `изменено строк: 1` |
| 2 | Сервер | Переключить стенд на `feat/haiku-5-5`, перезапуск; `logs 30` | Старт без ошибок, нет `Traceback` |
| 3 | Сервер | `q "SELECT model FROM chat_models WHERE chat_id=$GID"` | `claude-haiku-5-5` (запись **не удалена**, а мигрирована) |
| 4 | Сервер | Повторный рестарт, шаг 3 заново | Без изменений (идемпотентность) |
| 5 | TG-админ | `/models` в группе | `▸ Haiku 5.5`, цена `$0.10/$0.50`, окно 1M |
| 6 | TG-админ | `/haiku` (смена модели → `_probe_model`) | Принято, без «модель недоступна» — пул принимает `claude-haiku-5-5` (уже проверено curl на сервере 2026-10-09; здесь — через `_probe_model`) |

## 1. Служебные вызовы: нет пустых ответов (ТЗ п.7.2)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-юзер | «какой сейчас курс евро?» (роутинг поиска) | Поиск сработал, в логе `Search decision ... 'курс евро ...'`, не пусто |
| 2 | TG-юзер | «как дела?» | `Search decision ...: 'NO'`, поиска нет |
| 3 | TG-юзер | 12+ реплик в личке (память раз в 10) и 20+ в группе (раз в 15) | Нет `обрезан по max_tokens`, `JSON не разобрать`, `Пустой ответ модели` в `logs 200`; `q "SELECT fact FROM memory ORDER BY id DESC LIMIT 5"` — свежие факты |
| 4 | TG-юзер | `/imagine кот в шляпе` (перевод промпта) | Картинка пришла; `q "SELECT label, output, thinking FROM usage_log WHERE label='draw_translate' ORDER BY id DESC LIMIT 1"` → `thinking = 0` |
| 5 | TG-юзер | Фото/кружок в группе (`media_describe`) | Описание непустое; запомнить `input` — изображения у Haiku 5.5 дороже по токенам |
| 6 | Сервер | `q "SELECT label, COUNT(*) n, SUM(thinking) th, MAX(output) mx FROM usage_log WHERE kind='llm' AND model='claude-haiku-5-5' GROUP BY label"` | У служебных меток (`should_search`, `extract_*`, `draw_translate`, `captcha_*`, `greet*`, `media_describe`) `th = 0` |
| 7 | Сервер | Новый участник → капча и приветствие | Вопрос капчи непустой, ответ проверяется, приветствие пришло |

## 2. Диалог (ТЗ п.7.3, 7.4)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | TG-юзер + группа | 10–15 реплик: болтовня, ирония, спор, просьба «помоги с кодом» | Характер Клодушки, русский, ирония; **нет рассуждений в видимом тексте** («Хм, пользователь спрашивает…»), нет морализаторства (в System Card у 5.5 «wet blanket» хуже, 2.09 против 1.70) |
| 2 | Сервер | `q "SELECT output, thinking FROM usage_log WHERE label LIKE 'dialog%' AND model='claude-haiku-5-5' ORDER BY id DESC LIMIT 15"` | Доля `thinking` в `output` видна; данные для подбора `thinking_headroom` (сейчас 4000) |
| 3 | TG-юзер | Вопрос с поиском: «что нового в Python 3.15?», «курс доллара» | Ищет на `low`; если лениво отвечает по памяти — зафиксировать, решение по системному промпту — после стенда |
| 4 | Сервер | Если на шаге 1 течёт рассуждение: `HAIKU_DIALOG_EFFORT=medium` в `.env` → `docker compose up -d --force-recreate claudushka` | Рассуждения пропали (код не меняем) |
| 5 | TG-юзер | Фото с подписью-вопросом, затем документ | Ответ непустой, `stop_reason=max_tokens` нет |
| 6 | TG-юзер | Запрос на грани фильтров (безобидный, но «острый») | Если `refusal` — сообщение «Отказалась отвечать — это фильтры на стороне модели…», без падения и без ретраев |

## 3. Деньги (ТЗ п.7.5–7.7)

| Шаг | Где | Что делаю | Ожидание |
|---|---|---|---|
| 1 | Сервер | `cost55h 15` | `cost_usd` по формуле, `tier=0` на обычных вызовах; нет warning «нет в таблице цен» |
| 2 | TG-админ | `/cost <GID>` | Строки Haiku 5.5 по новой цене; «(из них thinking N)» у диалога |
| 3 | Сервер | Ступень >100k на стенде искусственно не воспроизвести (нужен реальный вход >100k) — юнит-тест `calc_llm_cost` проверяет только нашу формулу (100 000 → 0.10, 100 001 → 0.50), а НЕ расчёт Anthropic: учёт кэша в длине промпта — ДОПУЩЕНИЕ, в доке не уточнено (консервативное: завышает). Если вход всё же перевалит — `logs` покажет warning `верхняя ступень цены (x5)`, а `cost55h` — `tier=1` | На обычном трафике предупреждений нет |
| 4 | Сервер | Sonnet 5.5 кэш: `/sonnet` (платный чат), 2 запроса подряд, `cost55`-формула из `staging-checklist-opus-5-5.md` с `cache_read*0.10` вместо `0.20` | Чтение кэша по 0.05× ($0.10/MTok) — после фикса 16ed147 |
| 5 | TG-юзер | Обнулить баланс (шаг 5 из чек-листа Opus 5.5) → сообщение в free | Отвечает Haiku 5.5: `q "SELECT model FROM usage_log WHERE chat_id=$TUID ORDER BY id DESC LIMIT 1"` → `claude-haiku-5-5`; лимиты непроверенных не сломаны |
| 6 | Сервер | **Сравнение с Haiku 4.5**: `q "SELECT model, COUNT(*) n, ROUND(AVG(cost_usd),6) avg_cost, ROUND(AVG(output),0) avg_out FROM usage_log WHERE label LIKE 'dialog%' AND model IN ('claude-haiku-4-5-20251001','claude-haiku-5-5') GROUP BY model"` | Средняя цена реплики ниже примерно в 7–8× (токенизатор +30%, минус thinking); записать реальное число в отчёт |
| 7 | Сервер | `q "SELECT model, COUNT(*) n FROM usage_log WHERE kind='llm' GROUP BY model"` | Только известные строки: `claude-haiku-4-5-20251001` (история), `claude-haiku-5-5`, Sonnet/Opus/Fable |

## 4. Откат

Вернуть `MODELS["haiku"]` на `claude-haiku-4-5-20251001` (1/5, окно 200k, `thinking_headroom` 0, убрать `effort` и `tier_*`) —
`aux_params`/`dialog_params` вернут `{}` сами. `chat_models` вручную:
`qw "UPDATE chat_models SET model='claude-haiku-4-5-20251001' WHERE model='claude-haiku-5-5'"`.

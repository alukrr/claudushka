# Чек-лист стенда: `feat/gemini-models` (Gemini как модели чата, поиск GPT, `/searchmode`)

Порядок и общие правила стенда — `docs/claude/staging.md`. Ветка добавляет таблицы `chat_free_model` и
`chat_search_mode` (создаются в `init_db`, обратимо — таблицы можно просто не читать), новых зависимостей нет,
`docker-compose.yml` не менялся (restart достаточно).

Юнит-тесты (без сети): `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts -q`.

## 0. Шаг 0 для `gemini-4-argon` (НЕ проверялся)
Как в Шаге 0 фолбэка (`model-fallback.md`): `POST /v1/messages` с `model=gemini-4-argon` — текст, system-блоки, картинка,
транскрипт, стрим, `usage`; `response.model`; пустой `thinking`-блок первым; `cache_control`/`output_config` → 400.
Отдельно: нестримовый вывод — лимит 32K (`out_tokens(argon, 4096)` = 8096 с запасом). Не прошёл → третья ступень отпадает,
сказать Алексею, не подменять другой моделью. Результат записать в `model-fallback.md`.

## 1. Модели в платном чате
- `/argon`, `/flash`, `/lite`: проба, переключение, ответ в характере. На `/flash` — каждый из 5 режимов характера.
- Группа: транскрипт и обращение по имени. Фото с вопросом.
- Маркеры: «нарисуй …» в разговоре → `[[DRAW]]`; «пришли файлом» → `[[FILE: …]]`; служебные пометки в текст не протекают.
- `/models`: два блока, цены, текущая `▸`, строка «Поиск: …».

## 2. Кэш промпта у Gemini
В `usage_log` ответа `/flash` смотреть `cache_read`: приходит ли неявный кэш Google. Записать в `prompt-cache.md`.
```sql
SELECT id, model, label, input, output, cache_write, cache_read, thinking, cost_usd FROM usage_log
WHERE model LIKE 'gemini%' ORDER BY id DESC LIMIT 10;
```

## 3. Поиск
- Платный Claude-чат, `auto` → нативный (`label=native_search`).
- Платный `/flash`, `auto` → GPT: кликабельные источники под ответом, без `utm_source`; в логе нет `gpt_search не дал результата`.
- `/searchmode tavily`, `/searchmode gpt` на Claude-чате; `/searchmode claude` на Gemini-чате → GPT и честная строка в `/models`.
- `reasoning.effort`: принимает ли пул (`SEARCH_GPT_EFFORT=low`)? В логе не должно быть «HTTP 400 при reasoning.effort».
- Время ответа на `/flash` с поиском — замерить; больше 20 с — сказать Алексею.

## 4. Фолбэк в обе стороны
- Claude подменён на `claude-nonexistent` → ответ Gemini, поиск GPT.
- Gemini-модель подменена на несуществующую (`/flash` с битым ID в `MODELS` на стенде) → ответ Sonnet; для `/lite` → Haiku.
- В `usage_log` метка `dialog_fallback`, модель фактическая.

## 5. Free
- `/lite` в бесплатном чате → ответ Lite, запись в `chat_free_model`, `chat_models` НЕ тронут.
- `/flash` в free → «доступно в платном режиме»; `/haiku` возвращает Haiku.
- Пополнение → восстанавливается прошлый платный выбор; обнуление → free-выбор (Lite) снова действует.
- Поиск в free: Tavily; `/searchmode` не-админу отказ.
- Стоимость 10 одинаковых сообщений на Haiku и на Lite в free-чате — приложить к отчёту:
```sql
SELECT model, COUNT(*), SUM(input), SUM(output), SUM(cache_read), SUM(cost_usd) FROM usage_log
WHERE chat_id = <free_chat> AND label = 'dialog' GROUP BY model;
```

## 6. Учёт
- Gemini-модели с правильными ценами, рассуждения в выводе (`thinking`), строки `gpt_search` (llm) и `search/gpt`
  (число строк `search` = `web_search_requests`). Цена поиска `GPT_SEARCH_PRICE = 0.0` — замерить по балансу пула.
- Сравнить `output_tokens` Anthropic-полосы и нативной (`thoughtsTokenCount`) на одном запросе: входят ли рассуждения
  Gemini (п. 1.6 ТЗ; если нет — `/cost` занижен уже на фолбэке v1.8.0).

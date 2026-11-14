# Чек-лист стенда: `fix/lite-gone` (Flash-Lite убрана пулом, служебка и free на GPT-6 Luna, v2.16.0)

ТЗ — `docs/claude/tz-lite-gone.md`, устройство — `docs/claude/models-and-costs.md` (таблица моделей, «Замеры пула 2026-11-14»),
`docs/claude/model-fallback.md` (пара Luna → Flash), `docs/claude/media.md` (голос). Прогон — Алексей, только тестовый бот и `data-test/`;
прод, dnd, `edge` не трогать.

Выкатка: в `~/claudushka-test` `git fetch && git checkout --detach origin/fix/lite-gone`, затем
`docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test`. Новых переменных, томов и файлов нет —
`.env` и compose не менялись, на проде выкатка обычным `/update`. Если на стенде в `.env` задан `GEMINI_AUDIO_MODEL` — убрать или
поставить `gemini-3.8-flash` (иначе шаг 5 проверит не дефолт).

## Прогон
1. **Старт.** В логе нет `MODEL NOT FOUND` и предупреждений `check_fallback_models` (`models: … нет в каталоге пула`, `у … нет цены`);
   есть строки `lite_gone: chat_models gemini-3.1-flash-lite -> gpt-6-luna: N` и `Migration applied: lite_gone`. На копии прод-базы N = 0,
   на стенде с выбором `/lite` в старых строках — сколько их было. Перед стартом на стенде можно подложить строку
   `INSERT INTO chat_models VALUES (<chat_id>, 'gemini-3.1-flash-lite')` — после старта в ней `gpt-6-luna`; повторный `docker restart`
   миграцию второй раз не выполняет (маркера в `schema_migrations` достаточно, строка в логе только одна).
2. **Бесплатный чат.** Чат без баланса: ответ приходит; в `usage_log` модель `gpt-6-luna`, метка `dialog` (без `_fallback`):
   `SELECT model, label FROM usage_log ORDER BY id DESC LIMIT 5`.
3. **Платная группа.** Служебные вызовы — `should_search`, `extract_chat_memory`, `media_describe`, `activity_check`, `abuse_check` —
   в `usage_log` на `gpt-6-luna` без `_fallback`; фото описывается (проверить, что `media_describe` вернул текст, а не пусто).
4. **Команды.** `/luna` в платном чате переключает (без пробного запроса); `/lite` отвечает заглушкой «Gemini Flash-Lite убрал поставщик…»,
   в `chat_models` ничего не пишет; `/flash` и `/argon` — как раньше (с пробой). `/models`: Luna под заголовком «GPT (кэш промпта
   автоматический)», Flash и Argon — под «Gemini», цена `$0.1/$0.5`, окно `872k`, в free строка «В бесплатном режиме работает только Luna».
   `/help` (и админский) показывают `/luna` с ценой и описанием фолбэка «luna → Gemini 3.8 Flash».
5. **Голос.** Голосовое из Telegram (OGG/Opus) расшифровывается; в тексте нет рассуждений модели (части `thought` отброшены); в `usage_log`
   строка `label=voice`, `model=gemini-3.8-flash`, выход включает рассуждения (`thinking` > 0). **Если Gemini в пуле лежит (529/503) —
   шаг отложить и записать это здесь:** 14.11 голос на живом пуле не проверялся вовсе.
6. **`/cost <chat_id>`** (админ): старые строки Flash-Lite подписаны «Gemini 3.1 Flash-Lite», а не «нет в реестре»; цены старых строк не изменились.
7. **Фолбэк Luna → Flash.** На стенде временно подменить id Luna на несуществующий (например, `MODELS["luna"]["id"]`-эквивалент правкой
   на стенде или тестовой моделью в `.env`-переопределении, если стенд позволяет; иначе — по логу после реального сбоя пула): ответ приходит
   через `gemini-3.8-flash`, в `usage_log` метка `dialog_fallback`, в логе `FALLBACK gpt-6-luna → gemini-3.8-flash`, админу ушло одно
   уведомление; вернуть id и убедиться, что через 5 минут (липкое окно) снова Luna. Параметры при переходе: запас 4000 (в логе запроса нет — проверить
   юнит-тестом `test_luna_down_answers_via_flash_with_flash_headroom`).

## Перед прокатом на прод
- `git diff --stat main..fix/lite-gone` без «переехавших» окончаний строк (в репозитории только LF).
- Прод: выкатка `/update` (compose, `.env`, volume не менялись); `chat_models` на проде строк с Lite не содержит (6 × Flash, 7 × Argon на 14.11) —
  миграция затронет 0 строк. После выкатки: в логе нет `MODEL NOT FOUND`, служебные вызовы в `usage_log` без `_fallback`.
- Тег после стенда (ставит Алексей): `git tag -a v2.16.0 -m "Flash-Lite убрана пулом: служебные вызовы и бесплатный режим на GPT-6 Luna, голос на Gemini 3.8 Flash"`.

## Результат прогона на стенде
Прогон 2026-11-14, коммит `e793849`, стенд `@claudushka_test_bot`:
- Шаг 1 — пройден: `lite_gone: … -> gpt-6-luna: 1`, `Migration applied: lite_gone`, без `MODEL NOT FOUND` и предупреждений каталога.
- Шаги 2, 4, 6 — пройдены (Алексей, вручную): бесплатный чат отвечает, в `usage_log` `gpt-6-luna` / `dialog` и `should_search` без `_fallback`.
- Шаг 3 — частично: служебные вызовы на Luna без `_fallback`; `media_describe` (фото в платной группе) вживую не проверен.
- Шаг 5 — пройден после того, как пул поднял Gemini (~17:30 Berlin): два голосовых OGG/Opus в платной группе расшифрованы верно, `voice` на
  `gemini-3.8-flash`, рассуждения (67 и 214 токенов) в текст не попали. Отдельная проба синтезированной речью — Flash и Argon вернули точный текст.
- Шаг 7 — не проверен вживую (только юнит-тесты).
- Попутно найдено, к ветке не относится: голосовое или кружочек реплаем на бота в группе не считается обращением (`is_bot_mentioned` выходит на
  сообщении без текста) — расшифровывается, но ответа нет. Чинится отдельной веткой.

# ТЗ `fix/lite-gone`: пул убрал Gemini Flash-Lite — служебные вызовы и бесплатный режим на GPT-6 Luna

Статус: **не сделано**. Решения Алексея 2026-11-14: Luna вместо Lite везде (служебные вызовы и бесплатный диалог),
голос — на `gemini-3.8-flash`, `/lite` — заглушка, Hydrogen и Carbon пока не добавлять.

## Что случилось

2026-11-14 ~13:00 Berlin пул `router.apitoken.sale` убрал четыре модели: `gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`,
`gemini-2.5-flash`, `gemini-3-flash-preview` (404 на всех полосах). Живые Gemini: `gemini-3.8-flash`, `gemini-4-argon`,
новые `gemini-4-hydrogen`, `gemini-4-carbon`. Замер — `docs/claude/bench/2026-11-14-1525.txt`.

Последствия на проде (v2.15.0):
- `DEFAULT_MODEL_ID` = `gemini-3.1-flash-lite` — бесплатная модель и ВСЕ служебные вызовы. Каждый вызов сначала получает
  404, потом идёт на `gpt-6-luna` через фолбэк (липкое окно 5 минут, затем снова 404). В логе — `MODEL NOT FOUND` и
  уведомления админам раз в 30 минут; расход пишется с меткой `*_fallback`.
- Голосовые не расшифровываются: `GEMINI_AUDIO_MODEL_NAME` = та же Lite, фолбэка там нет (`Gemini audio non-retryable
  error: HTTP 404`). GPT в пуле аудио не принимает: `input_audio` → 400 на `chat/completions` и `responses`,
  `/v1/audio/transcriptions` → 404 (проверено 2026-11-14). Аудио умеет только Gemini через нативный `generateContent`.
- В `chat_models` прода строк с Lite нет (6 × Flash, 7 × Argon).

Почему Luna, а не Flash: за 14 дней Lite — 8211 вызовов, 13.0M входных и 0.76M выходных токенов, $4.42. Те же токены на
Luna ≈ $1.7, на Flash ≈ $20 (Flash думает всегда, ~300 токенов рассуждений на вызов) — оценки по токенам, не замер.
Luna уже обслуживала эти вызовы как фолбэк (581 вызов за 14 дней до сбоя, вся служебка с 13:00 14.11).

## Что сделать

Ветка `fix/lite-gone` от `main`. Сначала прочитать: `docs/claude/models-and-costs.md`, `docs/claude/model-fallback.md`,
`docs/claude/media.md`, `.claude/rules/db.md`, раздел «Definition of Done» в `CLAUDE.md`.

### 1. Реестр моделей (`bot.py`)

- В `MODELS` запись `"lite"` заменить записью `"luna"`: `id` `gpt-6-luna`, `label` `GPT-6 Luna`, `provider` `openai`,
  `in` 0.10, `out` 0.50, `cache_read_mult` 0.1, множители записи — `_GPT_MULTS`, `context` 872_000,
  `thinking_headroom` 2000. Цены листовые, со страницы пула, сверено 2026-11-14.
- `DEFAULT_MODEL_KEY = "luna"`, `FREE_MODEL_KEYS = ("luna",)`.
- Из `LEGACY_PRICES` убрать `gpt-6-luna` (теперь в `MODELS`). Добавить туда `gemini-3.1-flash-lite` (0.25 / 1.50,
  `cache_read_mult` 0.1, запись 1.0, `provider` `gemini`) — для подписей и цен старых строк `usage_log`.
- Команда `/luna` (`cmd_luna` → `_set_chat_model(update, context, "luna")`) вместо `/lite`. Luna — дефолт: проба перед
  записью ей не нужна, как раньше Lite (сейчас это условие завязано на `DEFAULT_MODEL_KEY` — проверить, что так и есть,
  а не на строку `"lite"`).
- `/lite` — заглушка на один релиз по образцу `cmd_claude_gone`: «Gemini Flash-Lite убрал поставщик. Вместо неё —
  /luna (GPT-6 Luna), остальные модели — в /models.» В тексте `cmd_claude_gone` заменить `/lite` на `/luna`.

### 2. Фолбэк (`bot.py`)

- `FALLBACK_PAIRS`: убрать ключ `gemini-3.1-flash-lite`, добавить `"gpt-6-luna": "gemini-3.8-flash"`. Пары Flash → Terra
  и Argon → Sol не трогать. Переход по-прежнему один: Luna → Flash → (дальше не идём), Flash → Terra.
- Проверить и при необходимости поправить всё, что предполагало «основная модель — всегда Gemini, пара — всегда GPT»:
  докстринг и логику `fallback_model_for`, `_call_with_fallback`, `kwargs_for_model` (запас при переходе Luna → Flash
  должен стать 4000 вместо 2000), `check_fallback_models`, тексты уведомлений.
- Аудит мест, где поведение зависит от провайдера модели: найти все вызовы `model_provider(` и все обращения к полю
  `"provider"`, а также `is_safety_block` / `_dialog_call`, `search_provider_for`, блоки в `/models`, `service_block`.
  Для каждого места решить, корректно ли оно с GPT-моделью в `MODELS`, и перечислить результат в отчёте (место →
  «менять не нужно, потому что…» или «поправлено»). Молча пропускать нельзя.
- Документация пула: у GPT `max_tokens` «принимается и игнорируется» — лимит не обрезает ответ. Служебные вызовы с
  малым лимитом (30–512) это не ломает, но если где-то логика опирается на `stop_reason == "max_tokens"` именно у
  дефолтной модели — назвать это место в отчёте.

### 3. Голос (`bot.py`, `docs/claude/media.md`)

- Дефолт `GEMINI_AUDIO_MODEL_NAME` → `gemini-3.8-flash` (переменная `.env` `GEMINI_AUDIO_MODEL` остаётся).
- В `_transcribe_audio_gemini` не включать в текст части с `"thought": true` (Flash думает; сейчас склеиваются все
  `parts`).
- Проверить `_record_gemini_usage`: цена берётся по id модели и учитывает `thoughtsTokenCount` как выход. Если нет —
  поправить (рассуждения тарифицируются как выход).
- Комментарий над константой дополнить: 2026-11-14 Lite убрана; аудио в пуле принимает только Gemini.

### 4. База (`db.py`)

- Новая одноразовая миграция через `_run_once` (имя `lite_gone`): `UPDATE chat_models SET model='gpt-6-luna' WHERE
  model='gemini-3.1-flash-lite'` (точное сравнение), счётчик в лог. На проде затронет 0 строк, на стенде и в копиях
  могут быть. Без неё платный чат, выбравший `/lite`, молча переедет на Flash (в 7 раз дороже).
- `_mig_no_anthropic` не трогать (историческая, уже выполнена). SQL-дефолт колонки `chat_models.model` в `CREATE TABLE`
  заменить на `gpt-6-luna` (влияет только на новую базу).

### 5. Остальной код

- `dedup_memory.py`, `dedup_tui.py`: алиас `lite` → `luna` (`gpt-6-luna`) в `MODEL_ALIASES`, `PRICING`,
  `THINKING_HEADROOM` (2000), `THINKING_EST`, дефолты `--model` и параметров функций, текст справки в шапке. Проверить,
  что вызов дедупа не шлёт Luna параметров, которые она не принимает.
- `whatsapp.py`: `WA_AUX_MODEL = "gpt-6-luna"` (сервис на паузе, меняется только константа; `.claude/rules/whatsapp.md`
  — если модель там названа).
- `scripts/bench_pool.py` уже обновлён под новый каталог (не закоммичен, лежит в рабочей копии вместе с двумя сырыми
  выводами в `docs/claude/bench/`) — закоммитить в эту ветку как есть.

### 6. Тексты для пользователя

Заменить «Lite» / «Gemini 3.1 Flash-Lite» на «Luna» / «GPT-6 Luna» и цену $0.10/$0.50, окно 872K:
- сообщение о переходе в бесплатный режим («…перешла в бесплатный режим: Lite…»);
- примечание настройки модели («бесплатный режим — всегда Lite»), строка в `/models` («В бесплатном режиме работает
  только Lite…»), блок моделей в `/models` — Luna не должна оказаться под заголовком «Gemini»;
- `USER_HELP` и `ADMIN_HELP`: `/lite` → `/luna`, описание фолбэка (Luna → Flash, Flash → Terra, Argon → Sol);
- `README.md` (те же места: бесплатный режим, таблицы команд, фолбэк, стек).

Комментарии и докстринги со словом «Lite» как названием служебной модели — поправить там, где они формулируют правило
(`DAILY_REVIEW_MODEL_ID`, `FORGET_PICK_MODEL_ID`, `get_chat_model`, `_set_chat_model`); остальные — по ходу, без
отдельной охоты. `MEMORY_BACKFILL_EST_COST` не менять.

### 7. Тесты

Прогнать всё в `scripts/test_*.py` (как они запускаются сейчас — посмотреть в самих файлах и в `docs/claude/staging.md`),
обновить упавшие под новую модель: как минимум `test_model_fallback.py` (пары, `test_dedup_pricing_matches_bot`),
`test_gemini_models.py`, `test_settings_units.py`, `settings_golden.json`, `test_settings_registry.py`. Добавить
проверки: `fallback_model_for("gpt-6-luna") == "gemini-3.8-flash"`; `kwargs_for_model` при переходе Luna → Flash даёт
запас 4000; миграция `lite_gone` на временной базе идемпотентна; расшифровка отбрасывает части с `thought`.
Плюс обязательные проверки из Definition of Done (`ast.parse` каждого изменённого `.py`, дубли имён верхнего уровня).

### 8. Документация (в том же коммите, что код)

- `CLAUDE.md`: индекс (`/argon /flash /luna`, заглушка `/lite`, это ТЗ и чек-лист), инварианты — «`MODELS` — только
  Gemini», «GPT-моделей чата нет», «обратного фолбэка нет», «служебные вызовы — `DEFAULT_MODEL_ID` (Lite)», «free →
  всегда Lite» переписать под Luna; строка версии.
- `docs/claude/models-and-costs.md`: таблица моделей, роли, раздел «Замеры пула 2026-11-14» с таблицей из
  `docs/claude/bench/2026-11-14-1525.txt` и оговоркой: Carbon, Argon, аудио и нативная полоса не измерены — Gemini в
  пуле лежал (529/503) с ~15:27 Berlin.
- `docs/claude/model-fallback.md`: таблица пар, абзац «Обратно не нужно» — заменить на описание пары Luna → Flash.
- `docs/claude/media.md` (модель голоса), `docs/claude/billing.md`, `.claude/rules/db.md` (миграция) — где названа Lite.
- `docs/claude/incidents.md`: инцидент 2026-11-14 (что убрали, что сломалось, как держался фолбэк, что голос без
  фолбэка).
- `docs/claude/open-tasks.md`: повторить замер Gemini, когда пул стабилизируется; решить по Hydrogen и Carbon;
  голос остаётся без запасного провайдера.
- `docs/claude/staging-checklist-lite-gone.md` — чек-лист стенда (ниже).

Старые ТЗ и чек-листы (`tz-*.md`, `staging-checklist-*.md` прошлых задач) не переписывать — это история.

### 9. Чек-лист стенда (написать файл, прогон — Алексей)

1. Старт без `MODEL NOT FOUND` и без предупреждений `check_fallback_models`; в логе строка миграции `lite_gone`.
2. Бесплатный чат: ответ приходит, в `usage_log` модель `gpt-6-luna`, метка без `_fallback`.
3. Платная группа: служебные вызовы (`should_search`, `extract_chat_memory`, `media_describe`, `activity_check`,
   `abuse_check`) — `gpt-6-luna` без `_fallback`; фото описывается.
4. `/luna` в платном чате переключает, `/lite` отвечает заглушкой, `/models` и `/help` показывают Luna с ценой.
5. Голосовое из Telegram (OGG/Opus) расшифровывается, в тексте нет рассуждений модели, строка `voice` в `usage_log`
   на `gemini-3.8-flash`. Если Gemini в пуле лежит — шаг отложить и записать это.
6. `/cost <chat_id>`: старые строки Lite подписаны «Gemini 3.1 Flash-Lite», а не «нет в реестре».
7. Фолбэк Luna → Flash: временно подменить id Luna на несуществующий (на стенде), убедиться, что ответ пришёл через
   Flash с меткой `_fallback`; вернуть.

## Чего не делать

- Не добавлять Hydrogen, Carbon, `gpt-image-2.6-*`, рисование Terra.
- Не переименовывать `call_claude`, `aux_params`, `dialog_params` и другие обёртки.
- Не трогать `docker-compose.yml` и `.env`: выкатка должна остаться обычным `/update`.
- Не заходить на сервер, не мержить в `main`, не ставить тег.

## Размер контекста

Промпты и лимиты не меняются. Окно Luna 872K против 1M у Lite; лимиты истории и памяти калиброваны под 200k —
не поднимать. У GPT вход >272K тарифицируется ×2 — при текущих лимитах недостижимо.

## Версия

Предлагается **v2.16.0** (minor: команда `/luna` вместо `/lite`, другая модель бесплатного режима). Тег после стенда:
`git tag -a v2.16.0 -m "Flash-Lite убрана пулом: служебные вызовы и бесплатный режим на GPT-6 Luna, голос на Gemini 3.8 Flash"`.

## Отчёт исполнителя

Список изменённых файлов; результат аудита из п. 2 по каждому месту; вывод тестов; что не сделано или не проверено
(голос на живом Gemini почти наверняка окажется здесь).

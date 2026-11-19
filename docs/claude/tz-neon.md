# ТЗ: модель Gemini 4 Neon — `/neon` (пул) и `/neon_g` (Google напрямую)

Ветка: `feat/neon`. Версия: **v2.18.0** (minor: новые команды). Стенд: короткий, `docs/claude/staging-checklist-neon.md`.
Работать в WSL: `~/projects/claudushka`, команды Linux (`python3`). Перед началом — `git pull`.

## Зачем

2026-11-19 Google выпустил Gemini 4 Neon — модель «для кода и сложных рассуждений», по тестам Google обходит GPT-6.2 Sol.
Нужна как ещё одна модель чата в платном режиме, по образцу пары Argon: через пул (`/neon`) и напрямую у Google (`/neon_g`).
Модель дефолтом НИГДЕ не становится: ни для free, ни для служебки, ни для обзора, ни для `/dev`.

## Что уже известно (замер 2026-11-19, прод-контейнер)

- **Пул**: в каталоге `GET /v1/models` есть `google/gemini-4-neon`: окно 1 048 576, выход 65 536, рассуждения `low/medium/high`.
  Цена в каталоге (`apitoken.pricing.standard`): вход $1.20, выход $6.00, кэш-чтение $0.06 за MTok (для сравнения, Argon в
  каталоге — $0.80/$4.00, а в `MODELS` у него $2.0/$10.0 — **листовая** цена со страницы пула, см. комментарий над `MODELS`).
- **На 2026-11-19 пул отключил Gemini целиком**: любой запрос к `gemini-*`, в том числе к Neon, получает 400 «Gemini models are
  currently unavailable…». Поэтому `/neon` сейчас НЕ пройдёт пробу `_probe_model` — это нормально, команда честно откажет. Фолбэк
  для таких 400 уже есть (v2.17.1, `api_errors.is_model_unavailable`).
- **Google напрямую**: `models/gemini-4-neon` есть, окно 1 048 576, выход 65 536. Ответ в характере — 6–9 с, JSON-служебка —
  4.5 с, чистый JSON. Думает всегда: ~400–550 токенов рассуждений даже на короткую реплику (`thoughtsTokenCount`), они идут в
  выход и оплачиваются.

## Цены и решения (2026-11-19, Алексей: цены найти самой)

1. **Листовая цена Neon в пуле** (для `MODELS["neon"]`) — найти самой на публичной странице моделей/цен пула (apitoken.sale,
   поиском по сайту; зачёркнутая цена у карточки модели). Для проверки, что берёшь «ту» цену: у Argon на той же странице
   должна получиться листовая $2 / $10 (как в `MODELS["argon"]`), при каталожной $0.80 / $4.00. Соотношение у Neon должно быть
   похожим (каталог $1.20 / $6.00). Не нашла страницу или цифры не сходятся с Argon — СТОП, спросить Алексея, не выдумывать.
2. **Прайс Google для `gemini-4-neon`** (вход/выход, доля кэш-чтения, акционная цена со сроком, ступень >200k токенов) —
   найти самой на странице цен Gemini API (ai.google.dev → Gemini API pricing). Проверка: для Argon там же должно быть
   $2 / $10 до 30.11.2026 и $4 / $20 потом (как в `MODELS["argon_g"]` без наценки). В бот идёт прайс × `GOOGLE_NATIVE_MARKUP`
   (`_gprice`); акция со сроком → `price_steps`, как у `argon_g`. Ступень >200k, если есть, — записать в отчёт и в
   `models-and-costs.md`, в код не вводить (у нас ступеней по длине нет).
3. В отчёте и в комментарии над `MODELS` — откуда цены: ссылки на обе страницы и дата сверки.
4. **`restricted` — нет** (решение Алексея): Neon в группе переключают так же, как Argon.

## Что сделать

### 1. `MODELS` (bot.py)
- `"neon"`: `id` `gemini-4-neon`, `label` `Gemini 4 Neon`, `provider` `gemini`, цена — п. 1 решений, `cache_read_mult` — по
  прайсу пула (у Argon 0.05; каталог Neon: 0.06/1.20 = 0.05), `cache_write_5m_mult`/`cache_write_1h_mult` 1.0,
  `context` 1_000_000, `thinking_headroom` 4000.
- `"neon_g"`: по образцу `argon_g` — `id` `google-direct/gemini-4-neon`, `api_model` `gemini-4-neon`, `native: True`,
  `label` `Gemini 4 Neon (Google)`, цена `_gprice(...)` из п. 2, при необходимости `price_steps`, `thinking_headroom` 4000.
- Порядок ключей в `MODELS` — как видит пользователь в `/models`: `neon` после `argon`, `neon_g` после `argon_g`.

### 2. Фолбэк (`FALLBACK_PAIRS`, `docs/claude/model-fallback.md`)
- `"gemini-4-neon": "google-direct/gemini-4-neon"` и `"google-direct/gemini-4-neon": "gemini-4-neon"`.
- В `FALLBACK_PAIRS_NO_GOOGLE` (нет ключа Google): `"gemini-4-neon": "gpt-6.1-sol"` (сильная → сильная, как Argon).
- Переход по-прежнему ОДИН. Ничего в `_call_with_fallback`/`sync_create`/`should_fallback` не менять.

### 3. Команды
- `cmd_neon` → `_set_chat_model(update, context, "neon")`, `cmd_neon_g` → `"neon_g"`, рядом с `cmd_argon`/`cmd_argon_g`;
  `CommandHandler("neon", ...)` и `CommandHandler("neon_g", ...)` рядом с `argon`/`argon_g` в `main()`.
- Права, проба, free-отказ — уже внутри `_set_chat_model` → `apply_setting("model")` → `can_change`. В хендлере своих
  проверок НЕ писать (инвариант «Новая настройка/права — только через `SETTINGS`/`can_change`»).
- Мини-приложение берёт список моделей из `MODELS` — проверить глазами, что Neon там появился; отдельный код не писать.
- `cmd_claude_gone` (текст заглушки `/haiku /sonnet /opus /fable`): добавить `/neon` и `/neon_g` в перечисление.

### 4. Справка (Definition of Done п. 3)
- `USER_HELP`: строка про модели — добавить `/neon` и `/neon_g`.
- `ADMIN_HELP`, блок «Модели»: две строки в стиле соседних, с ценой и окном; в строке про фолбэк — пара Neon.
- `README.md` — таблица моделей и абзац про фолбэк.

### 5. Дедуп памяти
`dedup_memory.py` держит ручную копию моделей ПУЛА (`MODEL_ALIASES`, `PRICING`, `THINKING_HEADROOM`) — добавить `neon`
(нативные туда не идут). Это проверяет `scripts/test_model_fallback.py::test_dedup_pricing_matches_bot`.

### 6. Тесты (`scripts/`)
- `test_gemini_models.py`: множество ключей `MODELS`, порядок команд, тексты справки — дополнить Neon.
- `test_google_native.py`: `neon_g` нативная, цена = прайс ×1.10, не в `FREE_MODEL_KEYS`.
- `test_model_fallback.py`: пары `neon ↔ neon_g`, без ключа Google — `neon → gpt-6.1-sol`, цены пар есть.
- Запуск: `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts -q`. Известные падения, которые были ДО этой
  ветки (на Windows; в WSL проверить и, если падают так же на `main`, не чинить в этой ветке): `test_forget_confirm::
  test_backup_file_content_and_restore_from_json`, `test_update_chown::test_only_walk_paths_are_recursive`,
  `test_miniapp_costs::test_costs_200_bot_admin_sees_free`. Сравнить с `git stash` на `main`, в отчёте назвать.

### 7. Документация (Definition of Done п. 1)
- `docs/claude/models-and-costs.md`: Neon в таблице моделей и в разделе «Нативная полоса Google», замер 2026-11-19 (раздел
  «Что уже известно» выше).
- `docs/claude/model-fallback.md`: строки таблицы пар.
- `CLAUDE.md`: в индексе «Модели и цены» упомянуть `/neon /neon_g`; в «Стек» — список моделей `MODELS`; «Текущая версия»
  → v2.18.0 с коротким описанием (старая — в «предыдущая:»).
- В индекс CLAUDE.md пойнтер на этот ТЗ уже добавлен.

## Чего НЕ делать

- Не делать Neon дефолтом нигде, не трогать `DEFAULT_MODEL_KEY`, `DAILY_REVIEW_MODEL_ID`, `FORGET_PICK_MODEL_ID`, режим `/dev`.
- Не передавать модели `thinking`/`output_config`/`reasoning_effort`/`tools`/`cache_control` — `kwargs_for_model` их режет,
  уровень рассуждений не настраиваем (Gemini через пул отвечает 400).
- Не звать модель мимо `call_claude`/`sync_create`/`_messages_create`; в новых вызовах `MODELS["neon"]["id"]` не хардкодить.
- Не трогать сервер, прод, `.env`; не ставить тег; не мержить в `main` без Алексея. Push только ветки `feat/neon`.
- Не добавлять миграцию `chat_models`: новых строк там никто не трогает, старые остаются как есть.

## Проверки перед сдачей (Definition of Done п. 4)

```bash
python3 -c "import ast; ast.parse(open('bot.py').read()); ast.parse(open('dedup_memory.py').read())"
python3 -c "import ast,collections as c; t=ast.parse(open('bot.py').read()); print([k for k,v in c.Counter(n.name for n in t.body if hasattr(n,'name')).items() if v>1])"   # должно быть []
TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python3 -m pytest scripts -q
git diff --stat   # без раздутия из-за окончаний строк (в репо только LF)
```

## Отчёт Алексею (на русском)

- Что сделано, по пунктам; какие цены взяты и откуда (ссылка на страницу Google, кто дал цену пула).
- Результат тестов, отдельно — падения, которые были и на `main`.
- Версия v2.18.0 и готовая команда (тег ставит Алексей):
  `git tag -a v2.18.0 -m "v2.18.0: Gemini 4 Neon — /neon (пул) и /neon_g (Google напрямую)"`.
- Что НЕ сделано / не проверено; влияние на контекст — нет (system-prompt, память и история не меняются).
- Выкатка — после стенда, обычным `/update` (`docker-compose.yml` и `.env` не меняются).

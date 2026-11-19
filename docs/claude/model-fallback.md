# Фолбэк между провайдерами пула: Gemini → GPT и Luna → Gemini (ТЗ no-anthropic, 2026-10-28; Luna → Flash — ТЗ lite-gone, 2026-11-14; до того — Claude → Gemini, ТЗ feat/model-fallback)

Перед правкой `call_claude` / `sync_create`, `_call_with_fallback`, `should_fallback`, `FALLBACK_*`, `kwargs_for_model`,
цен GPT в `LEGACY_PRICES` — читай этот файл. Ошибки API вообще — `api-errors.md`.

## Идея
Модели чата — Gemini (Flash, Argon) и GPT-6 Luna через пул `router.apitoken.sale` (`/v1/messages`, ключ бота). Если модель
недоступна, тот же запрос уходит на **модель из другого провайдера** на той же полосе и с тем же ключом — один сбой пула не
роняет обе модели (Gemini → GPT, Luna → Gemini).
Ответ приходит в формате Anthropic, поэтому `response_text`, `_track_response`, групповой транскрипт и история не менялись.

## История: Claude выключен (2026-10-28)
Пул закрыл модели Anthropic (`403 permission_error`, `error_code: model_blocked`). Хотфикс v1.11.1 (`CLAUDE_DISABLED=1`) гнал
Claude-модели на Gemini; v2.0.0 убрал Claude целиком: `CLAUDE_DISABLED`, обратный фолбэк Gemini → Claude, `FALLBACK_MODEL`,
`FALLBACK_MODEL_CHEAP`, `FALLBACK_THINKING_HEADROOM`, `NativeSearchUnavailable`, `call_claude_stream` удалены.
Старые Claude-id остались только в `LEGACY_PRICES` (подписи старых строк `usage_log`), `model_blocked` — в
`UPSTREAM_MARKERS` (безвредно). Переезд — `docs/claude/tz-no-anthropic.md`.

## Пары и конфиг
`FALLBACK_PAIRS` (bot.py, константа). **С 2026-11-19 (решение Алексея) Gemini пула и Luna уходят на Gemini напрямую у Google,
не на GPT** — пул выключил Gemini целиком (400 «Gemini models are currently unavailable… use another model», инцидент
2026-11-19 в `incidents.md`), а GPT-фолбэк, по оценке Алексея, «звучит как ChatGPT».

| Основная | Фолбэк | Почему |
|---|---|---|
| `gpt-6-luna` (free, ВСЕ служебные вызовы) | `google-direct/gemini-3.8-flash` | другой провайдер; до 2026-11-19 — Flash пула |
| `gemini-3.8-flash` (дефолт paid) | `google-direct/gemini-3.8-flash` | та же модель у другого поставщика; до 2026-11-19 — `gpt-6.1-terra` |
| `gemini-4-argon` | `google-direct/gemini-4-argon` | та же модель; до 2026-11-19 — `gpt-6.1-sol` |
| `google-direct/gemini-4-argon` / `…3.8-flash` | та же модель пула | v2.17.0 |
| `google-direct/gemini-3.1-flash-lite` | `gemini-3.8-flash` (пул) | Lite в пуле нет |

**Без `GEMINI_API_KEY`** (стенд без ключа) пара на нативную недоступна — `fallback_model_for` берёт `FALLBACK_PAIRS_NO_GOOGLE`
(прежние пары: Luna → Flash пула, Flash → Terra, Argon → Sol). Ключ проверяется в момент вызова, не при старте.

Переход всегда **один**: Luna → Flash у Google, дальше цепочка не идёт (если лёг и Flash у Google — `reply_api_error`; Flash
Google → Flash пула срабатывает только когда нативная — основная модель вызова). Пар для Terra и Sol нет. Пара Luna → Flash
дороже основной ×8 ($0.825/$4.125 против $0.10/$0.50; с 2027-01-01 Google вдвое дороже) и Flash думает всегда: при
`max_tokens = visible + 2000` у Luna `kwargs_for_model` пересобирает запас под Flash (`visible + 4000`). Расход фолбэка на
нативную идёт со счёта Google (`GEMINI_API_KEY`), не с баланса пула; лимиты ключа Google (RPM/TPM) не проверялись — если пул
лежит долго, на Google уходят ВСЕ служебные вызовы при сбое Luna и все платные чаты на Gemini пула.

| Переменная | Дефолт | Что |
|---|---|---|
| `FALLBACK_ENABLED` | `1` | `0` — выключено, поведение без фолбэка (старая лестница `RETRY_DELAYS`) |
| `FALLBACK_STICKY_SECONDS` | `300` | липкое окно, см. ниже |

Модели пар должны быть в `LEGACY_PRICES` (иначе `usage_log` посчитает по цене дефолтной модели); при старте
`check_fallback_models` (фон, только warning) сверяет `MODELS` и пары с `GET /v1/models` и с таблицей цен.
**Наличие в каталоге работу не гарантирует**: `gemini-3.6/3.7-flash` были в каталоге, а апстрим отвечал 404.

**Нативная полоса Google (v2.17.0).** Модели `google-direct/…` (`/argon_g`, `/flash_g`, `/lite`) при сбое Google уходят на ТУ ЖЕ модель
через пул (Lite → Flash пула), не на GPT; дальше цепочка не идёт. С 2026-11-19 есть и обратный путь — пул → нативные (см.
таблицу выше). Вызов любой модели — через
`_messages_create` (нативная → `google_native_create`, иначе SDK пула). `docs/claude/models-and-costs.md` → «Нативная полоса Google».

## Когда переключаемся — `api_errors.should_fallback(exc)`
Да: 5xx/529, 429, обрыв/таймаут, ошибка посреди стрима, **404** (модель неизвестна — громкий `MODEL NOT FOUND`),
401/403 с признаками апстрима (`UPSTREAM_MARKERS`), **400/422 «модель выключена, бери другую»** (`is_model_unavailable`,
`MODEL_UNAVAILABLE_MARKERS`: «currently unavailable», «use another model» — с 2026-11-19; повтора на основной нет, 400 не
ретраится, сразу пара и липкое окно). Нет: **402** (баланс общий, `_alert_admin_balance`), **прочие 400/422 включая
`prompt is too long`** (лечит ветка обрезки), 401/403 без признаков (ключ).

## Где — одна точка, а не по месту
- `call_claude` → `_call_with_fallback`: на основной модели **одна** повторная попытка (`FALLBACK_PRIMARY_DELAYS = (2,)`),
  потом пара с полной лестницей `RETRY_DELAYS`. Упала и пара — пользователь получает `reply_api_error`.
  `fallback=False` — для проб (`_probe_model`: проба несуществующей модели обязана упасть).
- `sync_create` / `aux_create`: SDK-ретраи, затем пара на том же клиенте.
- `kwargs_for_model(kwargs, model)` — одна функция и для первого вызова, и для фолбэка (на КОПИИ — оригиналы нужны ветке
  обрезки): убирает `thinking`, `output_config`, `tools`, `tool_choice`, **все `cache_control`**; `max_tokens` = видимая часть
  + `thinking_headroom` целевой модели (Gemini 4000, GPT 2000; при Luna → Flash запас 2000 заменяется на 4000). Для GPT `cache_control` на `/v1/messages` пул принимает
  (200, бенч 28.10), но действие не доказано — не шлём; кэш у GPT автоматический.

## Липкое окно (circuit breaker)
Основная модель X упала → следующие `FALLBACK_STICKY_SECONDS` запросов на X сразу идут на пару, без попытки и ожидания
(`_fallback_until`, в памяти, по ID модели). Сброс — рестарт бота или истечение окна.

## Поиск
Нативного поиска нет вообще (удалён). Поиск — `gpt` (`_gpt_web_search`, запасной Tavily), `docs/claude/native-search.md`.

## Открыто (проверить на стенде)
- Приходят ли у GPT на `/v1/messages` `cache_read_input_tokens` в `usage` и считает ли `_track_response` их (п. 1.7 ТЗ).
- Тарифицируется ли запись в кэш GPT (в `_GPT_MULTS` 1.25× по таблице пула; на `/v1/messages` не проверено).
- Входят ли токены рассуждений GPT в `usage.output_tokens` (для Gemini — да, стенд 2026-10-24).
- `thinking_headroom` GPT (2000) — стартовое значение, не замерено; поправить по `usage_log.thinking`.

## Деньги и видимость (описано для Gemini-фолбэка Claude; цены GPT — LEGACY_PRICES)
- Цена — по фактической `response.model` (`_billed_model`): Gemini-модели в `MODELS` (одна цена на строку, не
  дублируется в `LEGACY_PRICES`). Фолбэк-ответы в `/cost` — по метке `*_fallback`. Кошелёк списывается по цене
  фактической модели — так задумано.
- Множитель записи в кэш Gemini в доках пула не указан → 1.0 (**непроверено**, но `cache_control` на этом
  endpoint режется, записи в кэш не возникает). Чтение — 0.1× («cached input bills at 10%»).
- `logger.warning("FALLBACK <claude> → <gemini> …")` на каждое переключение; админам `notify_admins` —
  один раз при входе в окно и не чаще `FALLBACK_ALERT_COOLDOWN` (30 мин). Пользователю ничего; в историю
  модель и пометка не пишутся.

## Результаты Шага 0 (сервер, `/v1/messages`, ключ бота, 2026-10-23, SDK 1.7.0)
| Проверка | gemini-3.8-flash | gemini-3.1-flash-lite | gemini-2.5-flash-lite |
|---|---|---|---|
| текст, system строкой | OK ~1.8–2.7 с | OK ~0.7 с | OK ~1–2 с |
| system списком блоков (без `cache_control`) | OK | OK | OK |
| картинка base64 (фото-хендлер) | OK («Красная»), in=1120 | OK | OK |
| транскрипт (user/assistant/user/user) | OK | OK | OK |
| `stream` + `get_final_message()` | OK ~2 с, Anthropic SSE | OK ~0.7 с | OK ~0.9 с |
| `usage` | input/output_tokens есть; `cache_*` = None/0 | то же | то же |
| `response.model` | `gemini-3.8-flash` (голая строка) | голая | голая |
| время (3 замера) | 1.7 / 1.8 / 2.4 с | 0.8 / 1.1 / 1.5 с | 0.7 / 1.0 / 1.1 с |
| `cache_control` | 400 `Unsupported parameter: 'cache_control'` (проверено Алексеем) | то же | то же |
| `output_config` | 400 `not supported with this endpoint` | — | — |

Что важно:
- В `content` первым идёт `thinking` (пустой текст + `signature`), в стриме — `text` раньше `thinking`:
  только `response_text()`, не `content[0]`. Thinking-блоки в историю не сохраняются.
- **`gemini-3.8-flash` думает ВСЕГДА** (~300–480 токенов; `thinking: disabled` игнорируется, `budget_tokens`
  принимается). При `max_tokens=300` ответ обрывается (`stop_reason=max_tokens`, **недодуманное протекает в
  текст**), при 2000+ нормально. Отсюда `FALLBACK_THINKING_HEADROOM = 4000`. Lite-модели почти не думают
  (4–36 выходных токенов на короткий ответ) и отвечают на русском в характере одинаково; дефолт cheap —
  `gemini-3.1-flash-lite` (новее; $0.25/$1.50 против $0.10/$0.40 у 2.5 — копейки).
- `output_config` и `cache_control` пул отвечает 400 (fail-closed) — поэтому вычищаем оба.

## Как выключить / откатить
`FALLBACK_ENABLED=0` в `.env` + `docker compose up -d --force-recreate claudushka` (env читается при создании
контейнера). Только на время сбоя без рестарта — отдельной команды нет; окно само истекает через
`FALLBACK_STICKY_SECONDS`.

## Тесты
`scripts/test_model_fallback.py` (замоканный клиент, без сети): матрица `should_fallback`, чистка параметров и замена запаса,
пары, липкое окно и возврат, 402/400/ключ не переключают, `FALLBACK_ENABLED=0`, проба без фолбэка, `sync_create`, сверка цен
дедупа с `MODELS`. Запуск: `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts -q`.

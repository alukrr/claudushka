# Фолбэк между провайдерами: цепочка до двух переходов (v2.21.0, ТЗ `docs/claude/tz-fallback-chain.md`, issue #5; до того — один переход: ТЗ no-anthropic 2026-10-28, lite-gone 2026-11-14, Claude → Gemini ТЗ feat/model-fallback)

Перед правкой `call_claude` / `sync_create`, `_call_with_fallback`, `_FallbackRun`, `fallback_chain`, `should_fallback`, `fallback_reason`,
`_enter_fallback`, `FALLBACK_*`, `kwargs_for_model`, цен GPT в `LEGACY_PRICES` — читай этот файл. Ошибки API вообще — `api-errors.md`.

## Идея
Модели чата — Gemini (Flash, Argon, Neon) и GPT-6 Luna через пул `router.apitoken.sale` (`/v1/messages`) и Gemini напрямую у Google
(нативная полоса, `GEMINI_API_KEY`). Если модель недоступна, тот же запрос уходит по **цепочке до двух переходов**: другой
поставщик той же Gemini, а последним звеном — GPT пула (Terra для Flash, Sol для Argon/Neon). Решение Алексея 2026-11-24: лучше
ответить не так, как обычно, чем промолчать (19.11 лежали пул-Gemini, а Luna/Terra отвечали за 1–2 с).
Ответ приходит в формате Anthropic, поэтому `response_text`, `_track_response`, групповой транскрипт и история не менялись.

## История: Claude выключен (2026-10-28)
Пул закрыл модели Anthropic (`403 permission_error`, `error_code: model_blocked`). Хотфикс v1.11.1 (`CLAUDE_DISABLED=1`) гнал
Claude-модели на Gemini; v2.0.0 убрал Claude целиком: `CLAUDE_DISABLED`, обратный фолбэк Gemini → Claude, `FALLBACK_MODEL`,
`FALLBACK_MODEL_CHEAP`, `FALLBACK_THINKING_HEADROOM`, `NativeSearchUnavailable`, `call_claude_stream` удалены.
Старые Claude-id остались только в `LEGACY_PRICES` (подписи старых строк `usage_log`), `model_blocked` — в
`UPSTREAM_MARKERS` (безвредно). Переезд — `docs/claude/tz-no-anthropic.md`.

## Цепочки и конфиг
`FALLBACK_CHAINS` (bot.py, константа) → `fallback_chain(model_id, max_hops=2)` (чистая функция). Основная → 1-й переход → 2-й переход:

| Основная | 1-й переход | 2-й переход |
|---|---|---|
| `gpt-6-luna` (free, ВСЕ служебные вызовы) | `google-direct/gemini-3.8-flash` | `gemini-3.8-flash` (пул) |
| `gemini-3.8-flash` (дефолт paid) | `google-direct/gemini-3.8-flash` | `gpt-6.1-terra` |
| `gemini-4-argon` | `google-direct/gemini-4-argon` | `gpt-6.1-sol` |
| `gemini-4-neon` | `google-direct/gemini-4-neon` | `gpt-6.1-sol` |
| `google-direct/gemini-3.8-flash` | `gemini-3.8-flash` | `gpt-6.1-terra` |
| `google-direct/gemini-4-argon` | `gemini-4-argon` | `gpt-6.1-sol` |
| `google-direct/gemini-4-neon` | `gemini-4-neon` | `gpt-6.1-sol` |
| `google-direct/gemini-3.1-flash-lite` | `gemini-3.8-flash` | `gpt-6.1-terra` |

GPT — всегда ТОЛЬКО последнее звено; у Luna GPT в цепочке нет (она сама GPT: Luna → Gemini у Google → Gemini пула). Для Terra и Sol цепочек нет.
История: до 2026-11-19 Gemini пула уходили на GPT; в 2026-11-19 (пул выключил Gemini целиком, инцидент в `incidents.md`) GPT заменён Google, а
GPT «звучит как ChatGPT»; v2.21.0 вернул GPT как последний рубеж на случай, когда лежат и пул, и Google.

- **Без `GEMINI_API_KEY`** нативные звенья выкидываются из цепочки (`fallback_chain` проверяет ключ в момент вызова): Luna → Flash пула,
  Flash → Terra, Argon/Neon → Sol (поведение прежних `FALLBACK_PAIRS_NO_GOOGLE`, которых больше нет). Нативная основная без ключа вообще не вызывается.
- **Дневной обзор** (`DAILY_REVIEW_MODEL_ID` = Argon у Google) и **автодедуп** (`_DedupClientShim`, Flash пула) зовут `sync_create(..., max_hops=1)`:
  GPT в их цепочку не входит (решение Алексея: обзор от GPT «скучный»; слияние фактов GPT не поручаем). Лежат оба — ошибка, как раньше.
  `fallback=False` (пробы `_probe_model`, safety-скан) — цепочки нет вовсе. Голос, картинки, поиск, `whatsapp.py` — не затронуты.
- Модель любого звена должна быть в `MODELS`/`LEGACY_PRICES` (иначе `usage_log` посчитает по цене дефолтной); при старте `check_fallback_models`
  (фон, только warning) сверяет `MODELS` и звенья с `GET /v1/models` и с таблицей цен. **Наличие в каталоге работу не гарантирует**
  (`gemini-3.6/3.7-flash` были в каталоге, а апстрим отвечал 404).
- Переход Luna → Flash дороже ×8 ($0.825/$4.125 против $0.10/$0.50; с 2027-01-01 Google вдвое дороже), Flash думает всегда: `kwargs_for_model`
  пересобирает запас под целевую модель (`visible + 4000` у Gemini, `+ 2000` у GPT) — для КАЖДОГО звена от параметров ОСНОВНОЙ модели.
  Расход на нативную идёт со счёта Google (`GEMINI_API_KEY`), не с баланса пула; лимиты ключа Google (RPM/TPM) не проверялись.

| Переменная | Дефолт | Что |
|---|---|---|
| `FALLBACK_ENABLED` | `1` | `0` — выключено, поведение без фолбэка (старая лестница `RETRY_DELAYS`) |
| `FALLBACK_STICKY_SECONDS` | `300` | липкое окно, см. ниже |
| `FALLBACK_MAX_HOPS` (константа) | `2` | потолок переходов; на вызове — параметр `max_hops=` |
| `FALLBACK_ALERT_COOLDOWN` (константа) | `1800` | кулдаун алерта админам — НА КАЖДУЮ ПРИЧИНУ |

## Обход цепочки — одна логика на обе обёртки
`_FallbackRun(model, fallback, max_hops)` (bot.py): план = основная + звенья, у которых НЕ открыто своё липкое окно (все окна открыты — пробуем
последнее звено); `.model` — текущее, `.delays` — лестница ретраев, `.advance(exc, label)` — перейти дальше (`should_fallback(exc, next_model=…)`,
`_enter_fallback`) или пробросить. `_call_with_fallback` (async, `call_with_retry`) и `sync_create` (sync, SDK-ретраи) исполняют попытку по-своему, а
обходят одинаково — тест `test_async_and_sync_walk_the_chain_identically`. Фолбэк живёт ТОЛЬКО в обёртках (инвариант CLAUDE.md).
- **Ретраи:** на основной и промежуточном звене — `FALLBACK_PRIMARY_DELAYS = (2,)` (одна повторная попытка), на ПОСЛЕДНЕМ звене плана — полная
  `RETRY_DELAYS = (2, 6, 15)`. Единственное живое звено (остальные в окнах или цепочки нет) — тоже полная лестница, как до цепочки.
- Если все звенья упали, пользователю идёт ошибка ПОСЛЕДНЕГО звена (`reply_api_error`).
- Учёт: метка `*_fallback` и модель по `response.model` — для любого звена (`_usage_label`, `_track_response`).

## Когда переключаемся — `api_errors.should_fallback(exc, next_model=None)`
Повод зависит от того, КТО вернул ошибку и КУДА идём дальше.
- **Ошибка Google напрямую** (тело `{"error": {"type": "google_native"}}`, его ставит `_google_status_error`, `api_errors.is_google_native_error`):
  **401/403/429 — ВСЕГДА повод** (ключ, биллинг или квота Google; у пула свой ключ и кошелёк); **400/422 — повод, только если в тексте** есть признак
  `GOOGLE_KEY_BILLING_MARKERS` (`api key`, `billing`, `permission`); `prompt is too long` и safety-блок — нет; 5xx/таймаут/сеть — да (общее правило).
- **402 пула (баланс)** — повод, только если `next_model` нативная (`google-direct/…`): у Google отдельный кошелёк. Следующее звено на том же
  пуле (Flash → Terra) — нет, баланс общий (`_alert_admin_balance`).
- **Остальное — как раньше:** да — 5xx/529, 429, обрыв/таймаут, ошибка посреди стрима, 404, 401/403 пула с признаками апстрима (`UPSTREAM_MARKERS`),
  400/422 «модель выключена» (`is_model_unavailable`: «currently unavailable», «use another model»; повтора нет, сразу следующее звено и окно);
  нет — прочие 400/422 (включая `prompt is too long` — лечит обрезка, и safety-блок), 401/403 пула без признаков (ключ).

### Что Google отдаёт на ошибки ключа и баланса
- **Неверный ключ — проверено вживую 2026-11-25** (`generateContent`, `x-goog-api-key: xxx`): **HTTP 400**, `status: INVALID_ARGUMENT`, текст
  «API key not valid. Please pass a valid API key.», `details[0].reason = API_KEY_INVALID`. НЕ 401/403 — поэтому правило «Google 400 + `api key`».
- **Запрос без ключа** — проверено: **HTTP 403** `PERMISSION_DENIED`, «Permission denied on the service.» (`reason = ACCESS_DENIED`).
- **Нулевой prepay-баланс / выключенный биллинг / исчерпанная квота — НЕ проверено живьём** (проекта без денег нет). По документации Google и по
  описаниям из практики: исчерпанные prepay-кредиты и квота отдаются как **429 `RESOURCE_EXHAUSTED`** (в тексте — про кредиты/квоту/биллинг), закрытый
  биллинг — как 403/400 с «billing» в тексте. Все три варианта покрыты правилом выше (429/403 — всегда, 400 — по `billing`). Первая живая ошибка
  должна быть сверена: статус и текст — в лог (`FALLBACK … причина=… HTTP …`), поправить `GOOGLE_KEY_BILLING_MARKERS`.

## Алерты админам с причиной
`_enter_fallback(model, fb_model, exc, label, tried)` — открывает липкое окно упавшего звена, пишет WARNING `FALLBACK a → b [label] причина=<ключ> …`,
говорит админам (`notify_admins`) ПРИЧИНУ (`api_errors.fallback_reason`): «Google: кончились деньги или квота (HTTP 429)», «Google: ключ или биллинг
(HTTP 400/401/403)», «Google не отвечает (…)», «пул: кончился баланс (402)», «пул выключил модель (HTTP …)», «пул не отвечает (…)».
**Кулдаун — по причине** (словарь `_last_fallback_alert`, ключи `google_quota`/`google_key`/`google_down`/`pool_balance`/`pool_model_off`/`pool_down`):
«Google без денег» не глушит «пул лежит». Алерт только при ОТКРЫТИИ окна (`newly`). **Второй переход — отдельная строка**, свой кулдаун (`hop2:<причина>`):
«⚠️ {основная} и {звено1} не отвечают ({причина звена1}), отвечаю через {звено2} ближайшие N мин.» Пользователю ничего; в историю модель и пометка не пишутся.

## Сколько HTTP-запросов на один ответ (худший случай)
`call_claude` (async, `client_noretry`, у Google `retries=0`): основная 2 + звено 1 2 + звено 2 4 = **8**. Повтор без истории в `_dialog_call` при safety-блоке
(только 400 safety — фолбэк его не включает) идёт через ту же цепочку; звенья, упавшие в первом проходе, в липких окнах и пропускаются, поэтому худший
случай — **9** (5 в первом проходе: 2 + 2 + safety-ответ звена 2; 4 в повторе: полная лестница на оставшемся звене; тест
`test_dialog_call_safety_retry_worst_case_is_nine_requests`). `sync_create`: на звено — SDK-ретраи пула (`ANTHROPIC_SDK_RETRIES=3` → до 4 запросов) или
`retries=1` у Google (2) → до ~10 запросов на три звена. Время ответа в худшем случае — два перехода × (отказ + 2 с паузы) + лестница 2+6+15 с на последнем
звене (замер на стенде — `docs/claude/staging-checklist-fallback-chain.md`).

## `kwargs_for_model` — параметры под звено
- `kwargs_for_model(kwargs, model)` — одна функция и для первого вызова, и для фолбэка (на КОПИИ — оригиналы нужны ветке
  обрезки): убирает `thinking`, `output_config`, `tools`, `tool_choice`, **все `cache_control`**; `max_tokens` = видимая часть
  + `thinking_headroom` целевой модели (Gemini 4000, GPT 2000; при Luna → Flash запас 2000 заменяется на 4000). Для GPT `cache_control` на `/v1/messages` пул принимает
  (200, бенч 28.10), но действие не доказано — не шлём; кэш у GPT автоматический.

## Липкое окно (circuit breaker)
Звено X упало → следующие `FALLBACK_STICKY_SECONDS` вызовов пропускают X без попытки и ожидания (`_fallback_until`, в памяти, по ID модели, общее для всех
цепочек: Flash у Google, упавший как звено цепочки Flash, пропускается и в цепочке Luna). Основная в окне — сразу на первое живое звено. Сброс — рестарт
бота или истечение окна. Окно открывает ТОЛЬКО переход по цепочке; ошибки без перехода (400, 402 в пул) окон не открывают.

## Поиск
Нативного поиска нет вообще (удалён). Поиск — `gpt` (`_gpt_web_search`, запасной Tavily), `docs/claude/native-search.md`.

## Открыто (проверить на стенде)
- **Цепочка до двух переходов (v2.21.0):** что реально отдаёт Google при нулевом prepay-балансе (см. «Что Google отдаёт…»), время ответа при двух
  переходах, качество/характер ответа GPT как последнего звена (`docs/claude/staging-checklist-fallback-chain.md`).
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
- `logger.warning("FALLBACK a → b …")` на каждое переключение; админам `notify_admins` — один раз при входе в окно и не чаще
  `FALLBACK_ALERT_COOLDOWN` (30 мин) на каждую причину (см. «Алерты админам с причиной»). Пользователю ничего; в историю модель и пометка не пишутся.

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
первые звенья цепочек, липкое окно и возврат, 400/ключ не переключают, `FALLBACK_ENABLED=0`, проба без фолбэка, `sync_create`, сверка цен
дедупа с `MODELS`. `scripts/test_fallback_chain.py` (v2.21.0): таблица `fallback_chain` (без ключа Google, `FALLBACK_ENABLED=0`, `max_hops`), `should_fallback`
по поставщикам и `next_model`, `fallback_reason`, обход на замоканном `_messages_create` (один и два перехода, все три упали, окна звеньев, `max_hops=1`),
ОДИНАКОВОЕ поведение async и sync на десяти сценариях, лестница ретраев, потолок запросов с safety-повтором, алерты и кулдаун по причине. Запуск: `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts -q`.

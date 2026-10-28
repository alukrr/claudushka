# Фолбэк Claude → Gemini при сбоях пула (ТЗ feat/model-fallback, 2026-10-23)

Перед правкой `call_claude` / `call_claude_stream` / `sync_create`, `_call_with_fallback`, `should_fallback`,
`FALLBACK_*`, цен Gemini в `LEGACY_PRICES` — читай этот файл. Ошибки API вообще — `api-errors.md`.

## Идея
Claude на пуле `router.apitoken.sale` часто падает. Если Claude недоступен, тот же запрос уходит на Gemini
через тот же пул и тот же ключ: **тот же Anthropic Messages API (`/v1/messages`), другая модель**, без
нативного `google-genai` и без абстракции провайдеров. Ответ приходит в формате Anthropic, поэтому
`response_text`, `_track_response`, групповой транскрипт и сохранение истории не менялись.

## 2026-10-28: Claude выключен — `CLAUDE_DISABLED` (хотфикс v1.11.1)
Пул перестал отдавать модели Anthropic: `403 permission_error`, `error_code: model_blocked` («Model 'claude-haiku-5-5' is
blocked for this account»). 403 без признаков апстрима фолбэк НЕ переключал — служебные вызовы (решение о поиске, память,
описание медиа) падали. Сделано:
- `CLAUDE_DISABLED` (env, дефолт `1`): Claude-модель ВСЕГДА идёт сразу на фолбэк-модель (`_sticky_fallback` возвращает её
  без окна) — ни попытки Claude, ни алерта админам; работает и при `FALLBACK_ENABLED=0`. Haiku и все служебные →
  `FALLBACK_MODEL_CHEAP` (Lite), Sonnet/Opus/Fable и дневной обзор → `FALLBACK_MODEL` (3.8 Flash).
- Обратного фолбэка Gemini → Claude нет (`fallback_model_for` → `None` для не-Anthropic моделей).
- Поиск в платных чатах с Claude-моделью — `gpt` (`search_provider_for`), нативного `web_search` нет.
- `model_blocked` добавлен в `UPSTREAM_MARKERS` — на случай `CLAUDE_DISABLED=0`.
- Пробы с `fallback=False` (`_probe_model` при `/haiku /sonnet /opus /fable`) по-прежнему идут в Claude и получат 403 —
  переключение на Claude-модель честно ответит ошибкой. Чаты, у которых в `chat_models` Claude, отвечают через Gemini;
  в `usage_log` метка `*_fallback`, модель фактическая.
- Тесты: `scripts/test_claude_disabled.py`; старые тесты фолбэка закреплены на `CLAUDE_DISABLED=0`.
Это заплатка до полного переезда — `docs/claude/tz-no-anthropic.md`.

## Конфиг (env, всё с дефолтами; откат — без правки кода)
| Переменная | Дефолт | Что |
|---|---|---|
| `FALLBACK_ENABLED` | `1` | `0` — выключено, поведение как до ветки (старая лестница `RETRY_DELAYS`) |
| `FALLBACK_MODEL` | `gemini-3.8-flash` | чаты на Sonnet/Opus/Fable |
| `FALLBACK_MODEL_CHEAP` | `gemini-3.1-flash-lite` | Haiku-чаты (free) и ВСЕ служебные вызовы (они всегда на Haiku) |
| `FALLBACK_STICKY_SECONDS` | `300` | липкое окно, см. ниже |

Обе модели должны быть в `LEGACY_PRICES` (иначе `usage_log` посчитает по цене Haiku); при старте
`check_fallback_models` (фон, только warning) сверяет их с `GET /v1/models` и с таблицей цен.
**Наличие в каталоге работу не гарантирует**: `gemini-3.6-flash`/`3.7-flash` в каталоге, а апстрим отвечает 404.
В каталоге id с префиксом `google/`, в запросе и в `response.model` — голая строка.

## Когда переключаемся — `api_errors.should_fallback(exc)`
Да: 5xx/529, 429, обрыв/таймаут, ошибка посреди стрима (`stream_error_type`), **404** (модель неизвестна —
громкий `MODEL NOT FOUND` в логе), 401/403 с признаками апстрима в тексте (`UPSTREAM_MARKERS`: `subscription`,
`upstream`, `no available`, `overloaded`, `channel`, `quota`; список уточнять по логу `FALLBACK …`).
Нет: **402** (баланс общий, остаётся `_alert_admin_balance`), **400/422 включая `prompt is too long`** (лечит
ветка обрезки), 401/403 без признаков (ключ).

## Где — одна точка, а не по месту
- `call_claude` / `call_claude_stream` → `_call_with_fallback`: на Claude **одна** повторная попытка
  (`FALLBACK_CLAUDE_DELAYS = (2,)`), потом Gemini с полной лестницей `RETRY_DELAYS`. Упал и Gemini — пользователь
  получает обычное `reply_api_error` по последней ошибке. `fallback=False` — для проб (`_probe_model`: проба
  несуществующей модели обязана упасть, а не «успешно» ответить Gemini).
- `sync_create` / `aux_create`: SDK-ретраи Claude (как раньше), затем Gemini на том же клиенте.
- `_fallback_kwargs` (на КОПИИ — оригиналы нужны ветке обрезки): убирает `thinking`, `output_config`, `tools`,
  `tool_choice`, **все `cache_control`** (system и messages), `max_tokens += FALLBACK_THINKING_HEADROOM (4000)`.

## Липкое окно (circuit breaker)
Claude-модель X упала переключаемой ошибкой → следующие `FALLBACK_STICKY_SECONDS` запросы на X сразу идут на
Gemini, без попытки и ожидания (`_fallback_until`, в памяти процесса, по ID модели, не по чату). Сброс —
рестарт бота или истечение окна. Без него при лежащем пуле каждое сообщение ждало бы попытку и ретрай.

## Нативный поиск
Gemini `web_search` не получает. Запрос с `tools` при сбое/липком окне → `NativeSearchUnavailable`; хендлер
(рядом с `_native_search_rejected`) уходит на поиск через GPT (`_gpt_web_search`, запасной — Tavily; см.
`native-search.md`, «Провайдеры поиска») и повторяет запрос обычным `call_claude` (который в окне идёт на Gemini).
👀 ставится только если реально искали.

## Обратный фолбэк: Gemini-модель чата → Claude (v1.9.0)
Gemini стала выбираемой моделью чата (`/argon /flash /lite`, `docs/claude/models-and-costs.md`). Упала она —
ответ даёт Claude по тем же правилам `should_fallback` и с тем же липким окном (ключ — ID Gemini-модели):
`argon`/`flash` → Sonnet 5.5, `lite` → Haiku 5.5 (`fallback_model_for`). Один переход: Claude-фолбэк дальше не
фолбэчится. Параметры приводит та же функция `kwargs_for_model(kwargs, model)`, что чистит вызов Gemini-модели
(и первый, и фолбэк): → Gemini без `thinking`/`output_config`/`tools`/`cache_control`; из Gemini → Claude запас
Gemini (4000) меняется на запас целевой модели, thinking/effort не добавляются (запас покрывает adaptive-thinking Haiku).
Метка `usage_log` фолбэк-ответа — с суффиксом `_fallback` (`dialog_fallback`; `_usage_label`), модель в строке —
фактическая. Это и есть видимость фолбэка в `/cost`: подпись «(фолбэк)» у моделей убрана — у Gemini-моделей теперь
обычные подписи из `MODELS`.

## Открыто (проверить на стенде): токены рассуждения на Anthropic-полосе
Не проверено, входят ли в `usage.output_tokens` Anthropic-полосы (`/v1/messages`) для Gemini-моделей токены
рассуждения. На нативной полосе они отдельные (`thoughtsTokenCount`, ~480 на короткий ответ в 215 токенов).
Сравнить один запрос на обеих полосах; если на Anthropic-полосе рассуждения не попадают в `output_tokens`, `/cost`
на фолбэке занижен — чинить отдельно. Результат записать сюда.

## Стенд 2026-10-24: `gemini-4-argon` и токены рассуждения (ТЗ feat/gemini-models)
Замер с сервера (`/v1/messages`, ключ бота), все модели через Anthropic-полосу пула:
- **`gemini-4-argon` проходит Шаг 0:** текст, system-блоки, картинка (16×16 PNG → «Красный»), групповой транскрипт, стрим
  (события `message_start … message_stop`, `usage`), `response.model == "gemini-4-argon"`, первым блоком пустой
  `thinking`. `cache_control` и `output_config` → 400 (как у остальных). Нестримовый `max_tokens=32000` принят (200).
  Argon почти не тратит токены на рассуждения: на `17*23` нативная полоса вернула `candidatesTokenCount 37` без
  `thoughtsTokenCount`.
- **Рассуждения Gemini входят в `usage.output_tokens` Anthropic-полосы** (п. 1.6 ТЗ закрыт). `gemini-3.8-flash`, тот же
  вопрос ×3: Anthropic-полоса `output_tokens` 664 / 697 / 667, нативная `candidates 78/76/79 + thoughts 493/631/474`;
  видимый ответ ~320 символов ≈ 80 токенов. Значит `/cost` на фолбэке НЕ занижен. У `gemini-3.1-flash-lite` на коротких
  вопросах рассуждений почти нет (`output_tokens` 2–63).
- Пул Claude 24.10 днём отвечал 529 (Overloaded) на Haiku/Sonnet часами — живой прогон фолбэка в обе стороны на стенде.

## Деньги и видимость
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
`scripts/test_model_fallback.py` (замоканный клиент, без сети): матрица `should_fallback`, чистка параметров,
липкое окно и возврат, 402/400/ключ не переключают, `FALLBACK_ENABLED=0`, проба без фолбэка, нативный поиск,
`sync_create`. Запуск: `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_model_fallback.py`.

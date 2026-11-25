# Чек-лист стенда: `feat/fallback-chain` (v2.21.0)

ТЗ — `docs/claude/tz-fallback-chain.md` (issue #5), описание — `docs/claude/model-fallback.md`. Стенд — `docs/claude/staging.md` (worktree `~/claudushka-test`,
тестовый бот; пул и ключ Google — реальные деньги, но на этих шагах это копейки). Меняется поведение ВСЕХ вызовов модели, поэтому стенд обязателен.
Стенд прогоняют Алексей и сестра; агент на сервер не ходит. Имена людей из чатов в отчёты не писать (репозиторий публичный).

## Подготовка
1. `git fetch && git checkout origin/feat/fallback-chain` в `~/claudushka-test`, `docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test`.
   В логе `claudushka-test`: «Application started», нет `Traceback`, нет warning про цены/каталог для `gpt-6.1-sol` и `gpt-6.1-terra`.
2. Локально до стенда: `TELEGRAM_BOT_TOKEN=x ANTHROPIC_API_KEY=x python -m pytest scripts/test_fallback_chain.py scripts/test_model_fallback.py -q` — зелёные.
3. Скрипт стенда: `docker cp scripts/stand_fallback_chain.py claudushka-test:/tmp/` (он работает в ОТДЕЛЬНОМ процессе контейнера и не трогает работающего бота;
   цепочку и ключ Google подменяет только у себя).

## Шаги
| # | Действие | Ожидание |
|---|---|---|
| 1 | `docker exec claudushka-test python3 /tmp/stand_fallback_chain.py real` | Ответ от `gemini-3.8-flash` (или `google-direct/…`, если пул-Gemini выключен); время — для сравнения; в `usage_log` метка без `_fallback` |
| 2 | `… stand_fallback_chain.py two-hops` | Цепочка `['google-direct/gemini-3.8-flash', 'gpt-6.1-terra']`; в логе два `FALLBACK`: `gemini-9-nonexistent → google-direct/…` (причина `pool_model_off`, 404) и `google-direct/… → gpt-6.1-terra` (причина `google_key`, HTTP 400 «API key not valid»); ответ `model='gpt-6.1-terra'`; в `usage_log` метка `*_fallback`, модель Terra, цена Terra. **Записать время ответа** (ожидание: десятки секунд не должны накапливаться — по одной повторной попытке на звено) |
| 3 | Тексты алертов в выводе скрипта («некому уведомить админа… ⚠️ …») | Первый: «Модель … не отвечает — пул выключил модель (HTTP 404)…»; второй — отдельной строкой: «⚠️ gemini-9-nonexistent и google-direct/… не отвечают (Google: ключ или биллинг (HTTP 400)), отвечаю через gpt-6.1-terra …» |
| 4 | `… stand_fallback_chain.py google-key` | `/flash_g` с неверным ключом: переход на Flash пула (причина `google_key`); если пул-Gemini выключен — ещё переход на Terra. Ответ получен в обоих случаях |
| 5 | `.env.test`: `GEMINI_API_KEY=bad`, `docker compose … up -d --force-recreate claudushka-test`; в тестовой группе `/flash_g`, обычное сообщение боту | Бот ответил (через Flash пула/Terra); админам тестового бота пришёл алерт с причиной «Google: ключ или биллинг (HTTP 400)»; в `usage_log` метка `dialog_fallback`; повтор сообщения в течение 5 минут — без нового алерта и без обращения к Google (липкое окно) |
| 6 | В том же состоянии (ключ Google плохой) чат на `/argon` (платный), обычное сообщение | Пул-Argon жив → ответ от него, фолбэка нет, Google не трогается (плохой ключ не мешает, пока пул отвечает) |
| 7 | Вернуть настоящий `GEMINI_API_KEY`, `--force-recreate`; `/flash`, `/argon`, `/luna` в чате | Ответы от своих моделей, `*_fallback` в `usage_log` не появляются |
| 8 | Дневной обзор: `/review` на чате с моделью чата (ручной `/review` идёт на модель чата) и (по возможности) автообзор 21:00 | Обзор приходит; в логе при сбое Argon — переход только на Argon пула, GPT-звена нет (`max_hops=1`; живой отказ имитировать не нужно — проверено тестом `test_max_hops_one_keeps_gpt_out`) |
| 9 | `/cost` | Строки `*_fallback` с моделью Terra/Sol и их ценой |
| 10 | Пустой `GEMINI_API_KEY` в `.env.test`, чат на `/flash`, пул отвечает | Без изменений: ответ от Flash пула; нативные звенья из цепочки выброшены (`fallback_chain("gemini-3.8-flash") == ["gpt-6.1-terra"]`, проверено тестом) |

## Что записать в отчёт стенда
- Время ответа при двух переходах (шаг 2) и при одном (шаг 4).
- Текст и статус, которые Google вернул на неверный ключ (должно совпасть с `model-fallback.md`: 400 «API key not valid»).
- **Не проверено без нулевого баланса:** что Google отдаёт на пустой prepay-баланс (ожидаем 429 `RESOURCE_EXHAUSTED`). Если у Алексея появится проект Google
  без денег — прогнать `real` с его ключом и сверить статус и текст; поправить `GOOGLE_KEY_BILLING_MARKERS` по факту.
- Как звучит Terra/Sol как последнее звено (по ответам шагов 2, 5, 6) — решение Алексея 2026-11-24: «лучше ответить не так, чем промолчать».

## Итог
Каждый шаг: пройден / не пройден (что в логе) / пропущен (почему). Стенд вернуть на `main`, `.env.test` вернуть к настоящему ключу.

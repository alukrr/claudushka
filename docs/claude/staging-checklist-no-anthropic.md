# Чек-лист стенда: `feat/no-anthropic` (ТЗ `docs/claude/tz-no-anthropic.md`, Шаги 1 и 3)

Выкатка: изменился код, `db.py` (миграция) — `docker restart` достаточно; `docker-compose.yml` и новые .py не менялись.
Стенд — по `docs/claude/staging.md`. Перед миграцией снять копию БД стенда.

1. **Миграция.** До старта: `SELECT model, COUNT(*) FROM chat_models GROUP BY 1;` и `SELECT mode, COUNT(*) FROM chat_search_mode GROUP BY 1;`.
   После старта в логе `Migration applied: no_anthropic_models` + строки `no_anthropic: ...`. Повтор: те же SELECT'ы — Claude нет,
   `claude` в режимах нет; `SELECT name FROM schema_migrations WHERE name='no_anthropic_models';` — одна строка.
2. **Старт.** В логе нет `NativeSearch`/`CLAUDE_DISABLED`; `check_fallback_models` не пишет warning про каталог/цены (или
   пишет только про то, чего правда нет в пуле).
3. **free-чат:** ответ идёт на `gemini-3.1-flash-lite` (`PROMPT … model=`), `/lite` → «и так на Lite», `/flash` → отказ с ценой.
4. **paid-чат:** по умолчанию Flash; `/argon` (проба проходит), `/lite`, `/flash`; `/models` — только 3 Gemini, нет Claude и «/fable»;
   `/haiku` `/sonnet` `/opus` `/fable` → заглушка.
5. **Поиск (paid):** вопрос про свежие новости → 👀, ответ с источниками, `usage_log`: `gpt_search` (llm) + `search/gpt`; `/searchmode claude` → usage.
6. **Служебные вызовы** (должны давать непустой результат на Lite): `should_search`, перевод `draw_translate`, `/imagine` (`image_prompt_rewrite`),
   описание фото в группе (`media_describe`), извлечение памяти (счётчик фактов растёт), приветствие `greet`.
7. **Голос:** голосовое в личку/группу → расшифровка; `SELECT label, model, inp, out, cost_usd FROM usage_log WHERE label='voice' ORDER BY id DESC LIMIT 3;`
   — строка есть, `model='gemini-3.1-flash-lite'`. Если транскрипт пуст — OGG/Opus не принимается: отправить отчёт, не чинить молча.
8. **Фолбэк:** на стенде заменить в `.env` `ANTHROPIC_BASE_URL` на несуществующий хост или временно подменить пару → ответ приходит от
   `gpt-6.1-terra`, в `usage_log` `dialog_fallback`; `SELECT model, label, inp, out, cache_read FROM usage_log WHERE label LIKE '%fallback' ORDER BY id DESC LIMIT 5;`
   — проверить `cache_read` у GPT (открытый вопрос).
9. **Обзор:** `/review` в тестовой группе на Argon: непустой, влезает, блоки тем (ветка A и, если лог большой, B).
10. **Режим разработчика** (`/dev`): ответ с файлами на модели чата, `max_tokens` не режется.
11. **`/cost`** (админ): старые Claude-строки подписаны («Haiku 5.5», …), а не «нет в реестре».
12. **Дедуп:** `dedup --mode llm --limit-groups 1` на копии БД: дефолт `lite`, оценка стоимости печатается, TUI выбор модели — 3 Gemini.
13. **Документы/фото/видео** в личке на Flash — ответ непустой.

Откат: `git revert` + `docker restart`; миграция данных назад не делается (Claude-строки в `chat_models` заменены) —
перед стендом бэкап БД.

---

## Результаты прогона 2026-10-28 (стенд `@claudushka_test_bot`, коммит 79d8f83 + правка каталога)

БД стенда — копия прод-данных (бэкап до миграции: `~/stand-db-before-no-anthropic.db` на сервере).

| # | Проверка | Итог |
|---|---|---|
| 1 | Миграция | OK: `chat_models` haiku→lite 1, sonnet→flash 3, opus→argon 6, fable 0; `chat_search_mode` пуста (ветка claude→auto на живых данных не проверена, только юнит-тест); маркер в `schema_migrations` |
| 2 | Старт | OK, без warning про каталог/цены, без ошибок |
| 3–4 | `/models`, `/cost` в free и paid, заглушки `/sonnet` | OK по сообщению Алексея («всё работает»); текст ответов в чек-лист не копировали |
| 5 | Поиск (paid) | в `usage_log` 4444: `should_search` (Lite) + `gpt_search` (Luna) + `search/gpt` — цепочка работает |
| 6 | Служебные на Lite | `should_search`, `media_describe`, `extract_chat_memory` в `usage_log` на `gemini-3.1-flash-lite`, непустые |
| 7 | Голос (OGG на Lite), фото | OK по сообщению Алексея; строки `label='voice'` отдельно не выписывали |
| 8 | Фолбэк | OK: `gemini-9-nonexistent` (в `FALLBACK_PAIRS` → Terra) → 404 пула → ответ `gpt-6.1-terra`, в `usage_log` метка `*_fallback`, модель и цена Terra |
| 9 | `/review` | OK. Ручной `/review` идёт на МОДЕЛИ ЧАТА (Lite: 654 вых. токена, $0.0015); на Argon (чат переключён): 934 вых., $0.014, не обрезан. На Argon именно ежедневный автообзор (`DAILY_REVIEW_MODEL_ID`), его живой прогон — 21:00 Berlin, на стенде не гонялся |
| 11 | `/cost` — старые Claude-строки | подписи из `LEGACY_PRICES` (в 4444 есть старые строки haiku) |

Прямые вызовы из контейнера (`call_claude`, 2026-10-28): все 6 моделей (3 Gemini + Luna/Terra/Sol) отвечают по-русски.
Кэш на `/v1/messages` (промпт ~9k токенов, второй вызов): **GPT Terra — `cache_read_input_tokens` приходит (9088 из 9251), цена по 0.05×**;
`gemini-3.8-flash` — тоже приходит (4079, неявный кэш), `cache_write` у обоих 0. Открытый вопрос п. 1.7 ТЗ закрыт для чтения;
запись в кэш GPT на этой полосе не наблюдается (множитель 1.25× не используется). Рассуждения: `gemini-3.8-flash` ~375–490 выходных
токенов на короткий ответ (входят в `output`), Lite/Argon/GPT — единицы–десятки.

Не проверено: ежедневный автообзор на Argon вживую, `/dev`, дедуп (`--limit-groups 1`), документы, WhatsApp.

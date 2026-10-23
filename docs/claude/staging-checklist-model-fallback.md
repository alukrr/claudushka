# Чек-лист стенда: ТЗ `feat/model-fallback` (фолбэк Claude → Gemini)

Правила стенда — `staging.md`; суть фичи — `model-fallback.md`. Юнит-тесты (`scripts/test_model_fallback.py`)
покрывают пп. 3, 4, 8 без сети; остальное — только живьём.

1. **Норма.** Сообщения идут через Claude, в логе нет `FALLBACK`, `/cost` как раньше.
2. **Сломать Claude.** В БД стенда подменить модель чата на `claude-nonexistent` (`chat_models`).
   Ждём: 404 → `MODEL NOT FOUND` в логе → ответ от Gemini, админам ОДНО уведомление; повторное сообщение сразу
   (липкое окно, без попытки на Claude). Вернуть модель после проверки.
3. **5xx/529** — юнит-тест (замоканный клиент): переключение, окно, возврат после окна.
4. **402 и 400 prompt-too-long не переключают** — юнит-тест.
5. **Платный чат с поиском при сбое:** поиск идёт через Tavily, источники выводятся, 👀 только на реальный поиск.
6. **Группа и фото через фолбэк:** ответ осмысленный, в `group_messages` сохранён с `is_bot=True`.
7. **Цены:** Haiku-чат → `FALLBACK_MODEL_CHEAP`, Sonnet-чат → `FALLBACK_MODEL`; в `usage_log` модели `gemini-*`
   и цены по таблице:
   `SELECT model, label, COUNT(*), SUM(cost) FROM usage_log WHERE model LIKE 'gemini%' GROUP BY model, label;`
8. **`FALLBACK_ENABLED=0`** (+ `--force-recreate`) → поведение как до ветки.
9. **Старт:** в логе нет warning'ов `fallback: … нет в каталоге пула` / `нет цены`.
10. **Русский в характере** на `gemini-3.1-flash-lite` (cheap) и `gemini-3.8-flash` — на глаз, 3–5 реплик в личке
    и в группе; thinking не протекает в текст.

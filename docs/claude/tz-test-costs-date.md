# ТЗ: тест «Расходы» не должен зависеть от сегодняшней даты (issue #2)

Ветка `fix/test-costs-date`. Код бота НЕ меняется — только тест. Без стенда, без версии (тег не нужен: пользователю ничего не меняется).

## Что сломано
`scripts/test_miniapp_costs.py::test_costs_200_bot_admin_sees_free` падает на `main` с ~2026-11-17:
`assert any(not r["billed"] for r in j["by_model"])` → False.

Причина: тесты эндпоинта `GET /api/chats/{id}/costs` кладут строки `usage_log` на фиксированные даты (`at(2026, 11, 9)`), а
«сейчас» не фиксируют — `_cost_data(now=None)` берёт `bot._berlin_now()`, т.е. настоящую дату. Разбивки `by_model`/`by_label`
считаются за последние 7 дней (`start = now - 6 дней`, bot.py `_cost_data`), и строки 9.11 из окна выпали. В файле уже есть
`NOW = datetime(2026, 11, 10, 12, 0, tzinfo=BERLIN)`, но через `_berlin_now` его подставляет только
`test_cost_detail_text_has_months_and_fits_telegram`.

## Что сделать
1. В фикстуре `env` (scripts/test_miniapp_costs.py) — `monkeypatch.setattr(bot, "_berlin_now", lambda: NOW)`, чтобы ВСЕ тесты
   эндпоинта жили в 2026-11-10. Убедиться, что `_cost_data` и всё, что он зовёт (`_cost_week_windows`, `_subscription_estimate`,
   `_stars_week_line` и т.п.), берут время через `_berlin_now`, а не `datetime.now()`/`time.time()` напрямую. Если что-то берёт
   напрямую — в тесте подменить и это (код бота не трогать; если без правки кода никак — остановиться и написать Алексею).
2. Проверить, что тесты, которые ждут «будущее относительно NOW не считается» (`test_cost_data_partial_current_month_and_ordering`)
   и прочие, по-прежнему проверяют то, что задумано.
3. Пройтись по остальным `scripts/test_*.py`: где ещё строки кладутся на фиксированную дату, а «сейчас» — настоящее. Список в
   отчёт; чинить тем же способом только если тест реально зависит от окна по текущей дате.

## Проверка
- `python3 -m pytest scripts/test_miniapp_costs.py -q` — зелёный.
- Полный `python3 -m pytest scripts -q` в WSL — зелёный. (В Windows падают ещё два теста —
  `test_forget_confirm::test_backup_file_content_and_restore_from_json` (права 600 на файле) и
  `test_update_chown::test_only_walk_paths_are_recursive` (`os.chown`) — это особенности Windows, в задачу не входят.)
- Для уверенности: прогнать файл с подменой системной даты на «через месяц» (`faketime '2026-12-24' python3 -m pytest …`,
  если `faketime` есть в WSL) — результат не должен меняться.

## Документация
Не нужна (поведение бота не меняется). Закрыть issue #2 ссылкой на коммит.

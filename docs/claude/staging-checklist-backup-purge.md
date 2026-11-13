# Чек-лист стенда: `feat/backup-purge` (автоудаление старых копий базы, v2.15.0)

ТЗ — `docs/claude/tz-backup-purge.md`, устройство — `docs/claude/staging.md` → «Автоудаление копий». Удаление необратимо, поэтому короткий стенд обязателен.
Выкатка: в `~/claudushka-test` `git fetch && git checkout --detach origin/feat/backup-purge`, затем
`docker compose -f docker-compose.test.yml up -d --force-recreate claudushka-test` (новых переменных и volume нет).
Только тестовый бот и `data-test/`. Прод, dnd, `edge` не трогать. Руками на стенде ничего не удалять — удаляет только джоб.

## Подготовка (до перезапуска бота)
Файлы-пустышки с давним временем изменения в `~/claudushka-test/data-test/` (`touch -d`):
- `backups/old1.db`, `backups/old2.db` — «30 days ago»; `claudushka.db.bak-old1`, `claudushka.db.bak-old2` — «20 days ago» (+ `claudushka.db.bak-old1-wal` — тоже «20 days ago»);
- свежие: `backups/new1.db` — «1 day ago», `claudushka.db.bak-new2` — «2 days ago», `backups/new3.db` — «3 days ago»;
- граница: `backups/edge.db` — `touch -d '14 days ago 1 hour'`-эквивалент «13 суток 23 часа»: старше трёх свежих, но не старше 14 суток — должен остаться;
- не трогать: `old.txt` в `backups/` («100 days ago»), `forget-backups/x.json` («100 days ago»), `dedup_runs/x.db` («100 days ago»), реальный `claudushka.db`.
Записать `ls -la --time-style=full-iso` каталога до.

## Прогон
1. `docker restart claudushka-test`; в логе (`logs`) строки `memory_purge: копии базы: удалено 4, освобождено …, осталось …` (считаются копии, спутник `-wal` — в их число не входит) и строкой ниже `удалены копии базы: …`.
2. Ожидание: удалены `old1.db`, `old2.db`, `claudushka.db.bak-old1` (и её `-wal`), `claudushka.db.bak-old2`; остались `new1`, `new2`, `new3`, `edge.db`, `old.txt`, `forget-backups/x.json` (если моложе 30 дней — иначе его убирает старое правило /forget, это штатно), `dedup_runs/x.db`, `claudushka.db`.
3. Повторный `docker restart claudushka-test`: «удалено 0», остаток тот же.
4. Три самые свежие: убрать все копии, кроме трёх, и поставить им всем «400 days ago», перезапустить: удалено 0, все три на месте.
5. В логе нет Traceback из `memory_purge`; бот отвечает в личку (смоделировать любое сообщение).

## Перед прокатом на прод
До выкатки показать Алексею список, который удалит джоб на проде (только чтение, с его «да»; `purge_old_db_backups(dry_run=True)` в контейнере или та же логика скриптом снаружи).
Сверить: `ls -la --time-style=full-iso data/backups data/claudushka.db.bak-*` с этим списком. Ориентир на 2026-11-12: старше 14 дней — 32 копии; с 2026-11-14 добавится 21 копия дедупа от 2026-10-30.
Выкатка на прод — `git pull && docker restart claudushka` (compose не менялся). Первый запуск джоба — на старте контейнера: сверить строку «удалено N, освобождено, осталось» со списком.

## Результат прогона на стенде (2026-11-13, `origin/feat/backup-purge` @ `49b3b05`, только `data-test/`)
Подложены файлы по списку выше (время — `touch -d @epoch`: `"13 days 23 hours ago"` GNU понимает как «+13 дней», в будущее); к ним — настоящие копии стенда. Сухой прогон
(та же логика скриптом снаружи) предсказал: 21 копия, удалить 6 (42 МБ), остаться 15. После `--force-recreate` лог: `копии базы: удалено 6, освобождено 42.4 МБ, осталось 15` и
строка с именами — ровно эти 6 (`bak-old1` (+ `-wal`), `bak-old2`, `old1.db`, `old2.db`, два дедупа 10-18/10-19). Остались `new1`, `new3`, `bak-new2`, `edge.db` (13 сут. 23 ч), три `restore`,
`old.txt`, `dedup_runs/x.db`, все ручные `bak-pre-*` моложе 14 дней, `claudushka.db`. `forget-backups/x.json` (100 дн.) убрало старое правило /forget (1 файл) — штатно.
Повторный `docker restart`: «удалено 0, осталось 15», Traceback нет. Шаг 4 (три копии по 400 дней) вживую не гонял — покрыт `scripts/test_backup_purge.py`.

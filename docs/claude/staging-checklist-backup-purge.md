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

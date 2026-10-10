# Функции для прогона чек-листов на тестовом стенде (docs/claude/staging*.md).
# Подключить один раз НА СЕРВЕРЕ (стенд лежит в ~/claudushka-test):
#   echo 'source ~/claudushka-test/docs/claude/staging-aliases.sh' >> ~/.bashrc && source ~/.bashrc
# Контейнер по умолчанию claudushka-test; другой — export STAGE_CT=имя перед вызовом.

_ct() { echo "${STAGE_CT:-claudushka-test}"; }

# q "SELECT ..."  — читать
q() { docker exec "$(_ct)" python3 -c "
import sqlite3, sys
c = sqlite3.connect('/app/data/claudushka.db'); c.row_factory = sqlite3.Row
for r in c.execute(sys.argv[1]): print(dict(r))
" "$1"; }

# qw "UPDATE ..." — писать (INSERT/UPDATE/DELETE), с коммитом
qw() { docker exec "$(_ct)" python3 -c "
import sqlite3, sys
c = sqlite3.connect('/app/data/claudushka.db')
n = c.execute(sys.argv[1]).rowcount; c.commit(); print('изменено строк:', n)
" "$1"; }

# logs [N] — последние N строк логов бота (по умолчанию 60); logs 200 | grep PROMPT — с фильтром
logs() { docker logs "$(_ct)" 2>&1 | tail -"${1:-60}"; }

# cache [N] — последние N диалоговых вызовов: input / cache_write / cache_read / цена
cache() { q "SELECT id, chat_id, model, input, cache_write, cache_read, output, cost_usd FROM usage_log
  WHERE label LIKE 'dialog%' ORDER BY id DESC LIMIT ${1:-6}"; }

# bychat <id> — по чатам: сколько реплик и в скольких читался кэш (id — первая строка после рестарта)
bychat() { q "SELECT chat_id, COUNT(*) n, SUM(cache_read>0) with_read, MAX(cache_read) max_read
  FROM usage_log WHERE label LIKE 'dialog%' AND id > ${1:?id первой строки после рестарта} GROUP BY chat_id"; }

# Сверка цены с формулой (PRICE_MARKUP=1.0). Запись в кэш хранится суммой, поэтому две границы:
# min — всё как 5m, max — всё как 1h. cost_usd должен попасть в [min, max].
# cost55 [N]  — Opus 5.5 ($4/$20, чтение 0.20, запись 5.0/8.0)
cost55() { q "SELECT id, label, model, input, output, thinking, cache_write, cache_read, cost_usd,
  ROUND((input*4.0 + output*20.0 + cache_read*0.20 + cache_write*5.0)/1e6, 8) AS min,
  ROUND((input*4.0 + output*20.0 + cache_read*0.20 + cache_write*8.0)/1e6, 8) AS max
  FROM usage_log WHERE kind='llm' ORDER BY id DESC LIMIT ${1:-5}"; }

# cost55s [N] — Sonnet 5.5 ($2/$10, чтение 0.10 (0.05x), запись 2.5/4.0)
cost55s() { q "SELECT id, label, model, input, output, thinking, cache_write, cache_read, cost_usd,
  ROUND((input*2.0 + output*10.0 + cache_read*0.10 + cache_write*2.5)/1e6, 8) AS min,
  ROUND((input*2.0 + output*10.0 + cache_read*0.10 + cache_write*4.0)/1e6, 8) AS max
  FROM usage_log WHERE kind='llm' ORDER BY id DESC LIMIT ${1:-5}"; }

# cost55h [N] — ТОЛЬКО строки Haiku 5.5 ($0.10/$0.50, ступень >100k по запросу — 0.50/2.50); min = всё как 5m
cost55h() { q "SELECT id, label, model, input, output, thinking, cache_write, cache_read, cost_usd,
  ROUND((input + cache_write + cache_read) > 100000) AS tier,
  ROUND(CASE WHEN input+cache_write+cache_read > 100000
    THEN (input*0.5 + output*2.5 + cache_read*0.05 + cache_write*0.625)
    ELSE (input*0.1 + output*0.5 + cache_read*0.01 + cache_write*0.125) END/1e6, 8) AS min_5m
  FROM usage_log WHERE kind='llm' AND model='claude-haiku-5-5' ORDER BY id DESC LIMIT ${1:-5}"; }

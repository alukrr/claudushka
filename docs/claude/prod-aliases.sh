# Функции для работы с боевым контейнером.
# Подключить один раз НА СЕРВЕРЕ (прод лежит в ~/claudushka):
#   echo 'source ~/claudushka/docs/claude/prod-aliases.sh' >> ~/.bashrc && source ~/.bashrc
# Контейнер по умолчанию claudushka; другой — export PROD_CT=имя перед вызовом.

# dedup [аргументы]  — dedup_memory.py в боевом контейнере; без аргументов — TUI
dedup() {
  local ct="${PROD_CT:-claudushka}"
  if [ "$(docker inspect -f '{{.State.Running}}' "$ct" 2>/dev/null)" != "true" ]; then
    echo "контейнер $ct не запущен" >&2; return 1
  fi
  local tty=-i
  [ -t 0 ] && [ -t 1 ] && tty=-it
  docker exec $tty "$ct" python3 -u /repo/dedup_memory.py "$@"
}

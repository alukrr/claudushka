# Задание для Claude Code: настроить тестер стенда на сервере (issue #6)

Ветка `feat/stand-tester`. Что такое тестер и как им пользоваться — `docs/claude/staging.md` → «Тестер стенда»; формат сценариев —
шапка `scripts/stand_tester.py`. Здесь — только порядок настройки и кто что делает. Файл одноразовый: после настройки отметь
итог в issue #6 и удали этот файл отдельным коммитом (или оставь, если Алексей скажет).

## Правила, которые здесь особенно важны

- **На сервер — только с явного «да, заходи» Алексея в ТЕКУЩЕЙ сессии** (CLAUDE.md → «Доступ к серверу»). Сначала чтение, перед
  каждым изменением (checkout в worktree, mkdir, правка файлов, запуск контейнера) — спросить ещё раз.
- Работать только в `~/claudushka-test` (worktree стенда). `~/claudushka` (прод) не трогать: ни checkout, ни compose оттуда.
- `TG_API_HASH`, файлы `data-tester/*.session` — секреты: не печатать, не копировать в чат и в git. При проверке — длина/наличие.
- **Коды входа и пароль 2FA вводит Алексей сам, в своём терминале.** `tester login` интерактивный (`docker compose run` с TTY);
  через неинтерактивный SSH из Claude Code он не пройдёт, и код в чат не просить.
- `TESTER_BOT` — только тестовый бот стенда, не прод-бот. Тестовая группа — только тестовая (staging.md → «Не добавлять тестового бота…»).

## 0. До сервера — проверить ветку локально

```bash
git fetch origin && git checkout feat/stand-tester
python3 -m pytest scripts/test_stand_tester.py -q                       # 23 passed
TESTER_SCENARIOS=scripts/stand_scenarios python3 scripts/stand_tester.py check   # ok smoke, ok group-two
```

(В PowerShell — `python`, не `python3`; переменная — `$env:TESTER_SCENARIOS="scripts/stand_scenarios"`.)

## 1. Что нужно от Алексея (спросить заранее, одним сообщением)

1. **api_id / api_hash**: https://my.telegram.org → войти любым своим аккаунтом → API development tools → создать приложение
   (название любое, например «claudushka stand tester»). Алексей сам впишет их в `data-tester/tester.env` на сервере (шаг 4) —
   в чат не присылать.
2. **@username тестового бота стенда** (не секрет, можно в чат).
3. Два резервных аккаунта: на каждый должен приходить код входа (в Telegram-приложение, где аккаунт уже залогинен, или SMS).
   Есть ли на них 2FA-пароль — пусть держит под рукой.
4. Тестовая группа: есть ли уже группа с тестовым ботом, куда можно добавить оба резервных аккаунта. Если нет — Алексей создаёт
   сам (группа, тестовый бот + оба аккаунта); бот в группе на стенде — непроверенный, это нормально для `smoke`.
5. На какой ветке сейчас стенд и можно ли переключить worktree на `feat/stand-tester` (шаг 3) — или сначала смержить ветку в `main`.

## 2. Сервер: осмотр (только чтение, после «да, заходи»)

```bash
cd ~/claudushka-test && git status -sb && git log --oneline -1    # на чём стоит стенд, нет ли локальных правок
docker compose version                                            # profiles нужны (Compose v2 — ок)
docker ps --format '{{.Names}}\t{{.Status}}' | grep claudushka    # стенд жив? прод не трогаем
ls -ld data-tester 2>/dev/null; grep -n "staging-aliases" ~/.bashrc
```

Сообщить Алексею, что увидел; если в worktree есть локальные правки — остановиться и спросить.

## 3. Сервер: подтянуть ветку в worktree (спросить перед этим)

```bash
cd ~/claudushka-test
git fetch origin
git checkout origin/feat/stand-tester        # или origin/main, если ветку уже смержили
```

Код самого бота в ветке тот же, что `main` на момент `daad115` (v2.21.0). Если стенд до этого стоял на другой ветке — бот
подхватит новый код только после `docker compose -f docker-compose.test.yml up -d --force-recreate`; нужно ли это сейчас —
спросить Алексея. Для тестера пересоздавать бота не нужно.

Функция `tester` — в `docs/claude/staging-aliases.sh`. Если строки `source ~/claudushka-test/docs/claude/staging-aliases.sh` в
`~/.bashrc` нет — предложить Алексею добавить (staging-aliases.sh, первые строки); после — `source ~/.bashrc`.

## 4. Сервер: каталог и настройки (спросить; секреты вписывает Алексей)

```bash
cd ~/claudushka-test
mkdir -p data-tester && chmod 700 data-tester
cp scripts/stand_tester.env.example data-tester/tester.env && chmod 600 data-tester/tester.env
```

Дальше Алексей сам: `nano ~/claudushka-test/data-tester/tester.env` — `TG_API_ID`, `TG_API_HASH`, `TESTER_BOT=@…`.
`TESTER_GROUP` пока пустой (шаг 6). Проверка без вывода секретов:

```bash
awk -F= '/^(TG_|TESTER_)/{print $1, "len=" length($2)}' data-tester/tester.env
git -C ~/claudushka-test status --short | grep -c data-tester     # 0 — каталог в .gitignore
```

## 5. Вход в аккаунты — делает Алексей в своём терминале

```bash
tester login a      # номер в международном формате, код, 2FA — если спросит
tester login b
tester whoami       # оба аккаунта: имя, @username, id
```

Первый запуск дольше (образ python:3.12-slim + pip install telethon). Если `tester: command not found` — шаг 3 про `.bashrc`, или
полной командой: `cd ~/claudushka-test && docker compose -f docker-compose.test.yml --profile tester run --rm stand-tester whoami`.
Ошибки и что делать:
- `нет TG_API_ID/TG_API_HASH` — `tester.env` не заполнен или не в `data-tester/`.
- `PhoneNumberInvalid` / `ApiIdInvalid` — опечатка в номере или в api_id/api_hash.
- `FloodWait` / просьба подождать — Telegram ограничил частоту входа: ждать указанное время, не повторять в цикле.
- Код не приходит — он приходит в Telegram-приложение, где этот аккаунт уже открыт (не SMS). Если такого приложения нет —
  остановиться и сказать Алексею, не дописывать скрипт на ходу.

## 6. Первый прогон (сообщения в личку тестовому боту — можно с согласия Алексея)

```bash
tester run smoke          # личка, модель не вызывается; ждём «итог: ✓ 6  ✗ 0»
```

Группа (если есть, шаг 1.4):

```bash
tester chats a            # найти id тестовой группы (-100…)
#   Алексей вписывает TESTER_GROUP=-100… в data-tester/tester.env
tester run group-two      # тратит копейки пула; две последние проверки зависят от модели и Group Privacy
```

`group-two` может честно упасть на `b: ${TESTER_BOT} …`, если у тестового бота включён Group Privacy и упоминание не дошло —
см. staging.md → «Group Privacy в BotFather». Это не баг тестера.

## 7. Итог

- Комментарий в issue #6: что прошло (`whoami` — без номеров телефонов, `smoke`, `group-two`), что нет и почему, лог прогона
  лежит в `~/claudushka-test/data-tester/runs/` (содержимое — только если в нём нет личных данных).
- Отметить пункты критериев готовности в issue #6.
- Если по ходу пришлось менять код/доки — коммит в `feat/stand-tester` (не на сервере, а в рабочей копии), пуш, на сервере снова
  `git checkout origin/feat/stand-tester`.
- Мерж в `main` — по решению Алексея. Тег не нужен: код бота не менялся (тестер — инструмент стенда).

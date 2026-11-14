# Единый реестр настроек чата (`feat/settings-registry`, v2.4.1)

ТЗ — `docs/claude/tz-settings-registry.md`; готовит почву для мини-приложения (`docs/claude/tz-miniapp.md`): оно должно применять ровно те
же правила, что команды. Это РЕФАКТОРИНГ — пользователь ничего не заметил: тексты ответов, порядок проверок, тарифная логика и запись не менялись
(доказательство — `scripts/test_settings_registry.py`, 720 случаев на нетронутом коде, ожидания в рефакторинге не правились).

## Что в реестре (bot.py, секция «Реестр настроек чата», перед `_parse_target_chat`)
`SETTINGS: dict[str, ChatSetting]` — восемь ключей:

| Ключ | Команда(ы) | Кто меняет | Free |
|---|---|---|---|
| `model` | `/argon /flash /luna` | любой; `restricted` в группе — админ чата/бота | `admin_saves` (проба модели перед записью, кроме Luna и free) |
| `image_provider` | `/banana /bananapro /gptimage /flare /sunburst` | как `model` (`bananapro` restricted) | `admin_saves` |
| `persona` | `/classic /business /toxic /flirt /granny` | личка — сам; группа — админ чата/бота | `admin_saves` |
| `review_persona` | `/review_persona` | админ чата/бота; смотреть — все; только группы | `admin_saves` |
| `review_enabled` | `/review_on /review_off` | админ чата/бота; только группы; нет строки чата — отказ записи | `allow` |
| `search_mode` | `/searchmode` | любой | `admin_saves` |
| `activity` | `/activity` | админ чата/бота; смотреть — все; только группы; 0–100 | `allow` (сохраняется, но не работает) |
| `autoignore` | `/autoignore` | админ чата/бота, смотреть — тоже; только группы | `allow` |

Чужой чат (`chat_id` аргументом) — во всех командах, где он есть, только админ бота. `/ratelimit`, `/unignore`, деньги и баны — НЕ в реестре.

`ChatSetting` (frozen dataclass): `key`, `label`, `scope` (`group|both`), `kind` (`choice|int|bool`), `choices(chat_id)` — значения и подписи из
реестров-источников (`MODELS`, `IMAGE_PROVIDERS`, `PERSONAS`, `SEARCH_MODES`), `int_range`, `get(chat_id)` → `{"value","stored","note"}`,
`who`/`view_who` (кто меняет/смотрит), `restricted_values()` (в группе — только админ чата/бота), `free_policy`
(`deny` — никто; `admin_saves` — не-админ бота отказ, админ сохраняет с пометкой; `allow` — без тарифной проверки), `validate`
(текст ошибки или `None`; `validate_late` — после прав, как у `/autoignore`), `apply` (async-запись с побочными эффектами), `texts` —
тексты отказов по причинам `foreign | group_only | free | restricted | chat_admin` (дословно как у команд до реестра).

## `can_change` — единственное место прав
```python
can_change(key, *, actor_id, is_bot_admin, is_chat_admin, target_chat_id, in_target_chat, tier, value=None) -> (bool, str)
```
Чистая функция: без Telegram, БД и записи. `is_chat_admin` считает вызывающий (`_check_change`: запрос к Telegram только когда он может повлиять) и
передаёт флагом. `value=None` — «посмотреть» (команда без значения). `in_target_chat=False` — `chat_id` пришёл аргументом (чужой чат).
Порядок: **чужой чат → «только группы» → значение → free → restricted → админ чата** (автоигнор проверяет значение после прав).
Группа определяется по знаку `target_chat_id` (группы < 0, личка = user_id > 0).
«Админ чата» в правах = результат `is_chat_admin`, включая **управляющих чата** (`chat_managers`, `docs/claude/group-chat.md` → «Управляющие чата»): для `can_change` это просто `is_chat_admin=True`.

## `apply_setting`
```python
await apply_setting(key, chat_id, value, *, context=None, probe_chat_id=None) -> (bool, str)
```
Валидация значения (`validate`) → `apply`. Неверное значение или упавшая проба модели → `(False, текст)`, ничего не пишем. Права НЕ проверяет —
их проверяет `can_change` в команде/мини-приложении ДО вызова. Для `model` проба идёт на чат `probe_chat_id` (в командах — текущий чат,
как раньше), в free и для Luna пробы нет.

## Как команда использует реестр
`_parse_target_chat` → `(chat_id, in_target, err)` разбирает только ID (у не-админа не разбирает: отказ «чужой чат» идёт раньше ошибок формата) →
`_check_change` (флаги + `can_change` + ответ об отказе) → тексты успеха/предупреждений остаются в команде → `apply_setting`.
Информационные команды (`/models`, `/imagemodels`, `/personas` без аргументов) реестр не трогают.

## Как добавить настройку
1. Ключ в `SETTINGS` (`ChatSetting` со всеми полями: `get`, `apply`, `texts`, `validate`, `choices` из реестра-источника, без хардкода).
2. Команда: разбор аргументов → `_check_change` → `apply_setting`; права НЕ писать в хендлере.
3. Тест: строки в `scripts/test_settings_units.py` (таблица `can_change`, валидация, `apply_setting`), а для команды — случаи в характеризации.

**Инвариант:** права на настройку чата — только `can_change`; новая настройка чата — только через `SETTINGS`.
Мини-приложение (`docs/claude/miniapp.md`) читает и пишет через тот же реестр: форма строится из `SETTINGS`, права — `can_change`, запись — `apply_setting`.

## Известные тонкости (сохранены намеренно)
- `/autoignore` проверяет право и на просмотр; `/review_on|off` и `/autoignore` не принимают `chat_id` (чужого чата там не было и нет).
- `/autoignore on|off` не проверяет, нашлась ли строка чата (как было); `/review_on|off` и `/activity` — проверяют и отказывают.
- `/activity` в free сохраняет значение (пометка в ответе), в отличие от `admin_saves` — это `allow`.
- Для не-админа с `chat_id`-аргументом ID не разбирается: сначала «менять чужой чат может только админ».
- Один лишний запрос `get_chat_member` возможен там, где команда в free всё равно откажет раньше (раньше его не было) — на решение не влияет.

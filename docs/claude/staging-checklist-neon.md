# Чек-лист стенда: `feat/neon` (Gemini 4 Neon, v2.18.0)

Коротко: ТЗ — `docs/claude/tz-neon.md`. Стенд — `docs/claude/staging.md` (тестовый бот, пустая БД, тот же пул и тот же
`GEMINI_API_KEY`, поэтому тратятся реальные деньги — по центам). Заходить на сервер — только с согласия Алексея в текущей сессии.

## Подготовка

1. На сервере: `cd ~/claudushka && git fetch && git checkout feat/neon && git pull`, затем пересобрать стенд по
   `docs/claude/staging.md` и убедиться, что `claudushka-test` поднялся (`docker logs --tail 30 claudushka-test` —
   «Application started», нет `Traceback`; нет warning `models: 'gemini-4-neon' нет в каталоге пула`).
2. Тестовый чат в платном режиме (`/topup <id> 1` от админа стенда), либо личка админа.

## Шаги

| # | Действие | Ожидание |
|---|---|---|
| 1 | `/models` | В списке есть Gemini 4 Neon и Gemini 4 Neon (Google), цены как в ТЗ (Google — с наценкой 10%) |
| 2 | `/help` (админ) и `/help` (не админ) | Есть `/neon` и `/neon_g`; ответ админу не обрезан |
| 3 | `/neon_g` | «… → Gemini 4 Neon (Google).» (проба прошла) |
| 4 | Написать «почему кот спит 16 часов?» | Ответ в характере, без пустого текста. В логе `PROMPT … model=google-direct/gemini-4-neon` |
| 5 | `/cost` | Строка Neon (Google) с ненулевой ценой, метка `dialog` |
| 6 | `/neon` | Пока пул отдаёт 400 на Gemini (с 2026-11-19) — вежливый отказ пробы, модель не записана. Если пул Gemini вернул — «→ Gemini 4 Neon» и ответ на шаге 4 через пул |
| 7 | Фолбэк (только если шаг 6 записал `/neon`, иначе пропустить с пометкой) | При 400/5xx пула в логе `FALLBACK gemini-4-neon → google-direct/gemini-4-neon`, ответ пришёл, в `/cost` метка `dialog_fallback` |
| 8 | Бесплатный чат: `/neon` от не-админа | Отказ «в бесплатном режиме — только Luna» (как у `/argon`) |
| 9 | Вернуть модель: `/flash` | Записано, Neon больше не отвечает |

## Итог

Отметить каждый шаг: пройден / не пройден (что в логе) / пропущен (почему). Стенд вернуть на `main`
(`git checkout main && git pull`, пересборка). Результат — в отчёт и коротко в `docs/claude/models-and-costs.md`.

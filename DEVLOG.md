# Devlog — Клодушка

## 2026-09-20

**ТЗ v0.10: учёт стоимости, кошельки, тарифы, открытый доступ** (ветка `feat/billing-v0.10`, на стенд — до прода)

- Каждый платный вызов (LLM / картинка / Tavily) пишется в `usage_log` с ценой и привязкой к чату через
  `contextvars` (`gate_update`, group=-1); балансы — `chat_credits`, баланс = пополнения − платные траты.
- Два измерения: доверие (banned / непроверенный / проверенный → лимиты) и тариф по балансу (paid / free →
  модели, картинки, обзор). Бот отвечает всем, кроме забаненных; белые списки, рефералы и десяток
  админ-команд удалены, вместо них `/verify /unverify /ban /unban /topup`.
- Free: только Haiku и GPT-картинки с дневным лимитом, выбор платного режима не стирается. Paid: все модели
  (дефолт Sonnet), banana, ежедневный обзор на Opus. `/fable` просит подтверждения.
- `/cost` переписан: баланс, режим, день/неделя/прошлая неделя, топ чатов, по видам; админу — детализация.
- Подробности — `docs/claude/billing.md`.

---

## 2026-05-06

**Ревизия и синхронизация репозитория**

Проект давно не трогали — провели ревизию состояния. Обнаружили, что документация сильно отстала от реального кода:
- `CLAUDE.md` описывал начальную минималистичную версию (только python-telegram-bot + anthropic), не зная про db.py, whatsapp.py, Tavily, Gemini/FLUX
- `README.md` не имел `/imagine`, `/update`, `/activity` в командах; не упоминал WhatsApp-сервис и Gemini/FLUX в стеке
- `.claude.md` не знал про `whatsapp.py` и внешние API

Обновили все три файла, синхронизировали WSL → GitHub → Hetzner (все три на `e9a89c9`). Контейнеры `claudushka` и `claudushka-wa` работают штатно.

---

## 2026-04-06

**Серия фиксов и фич за один день**

- `feat: self-update via /update command` — бот умеет делать git pull и перезапускать себя через Docker socket
- `fix: inform Claudushka about self-update capability in system prompt` — добавили в системный промпт знание о /update
- `one-time: bulk set premium for existing users` — миграция ролей для текущих пользователей
- `fix: create user if not exists when assigning role via admin commands`
- `fix: disable auto photo processing in group chats without mention`
- `feat: handle document files` — JSON, YAML, код, txt
- WhatsApp-бот: фиксы и добавлен токен
- `feat: Gemini refusal triggers prompt rewrite via Haiku before FLUX fallback`
- Несколько итераций настройки стиля ответов (лаконичность, без ChatGPT-эффекта)
- Улучшены триггеры веб-поиска (новости/события, без лекций от LLM)

# Devlog — Клодушка

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

# Project Guidelines

## Code Style
Python 3.12, minimal dependencies. See [CLAUDE.md](CLAUDE.md) for stack and rules.

## Architecture
Telegram bot with Claude API backend. Key components: [bot.py](bot.py) (main logic), [db.py](db.py) (SQLite database), [allowed.json](allowed.json) (whitelists). Roles: admin, premium, referral, street, banned. Memory extraction every 10 messages.

## Build and Test
Run: `docker compose up -d`
No formal tests; manual testing via Telegram.

## Conventions
User operations via telegram_id. Role checks at entry points. Group triggers: @bot mention, "клод"/"claude" prefix, or reply. Referral auto-promotion. Daily limits for street role.

See [README.md](README.md) for features and commands.
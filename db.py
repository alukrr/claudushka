import sqlite3
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("/app/data/claudushka.db")

# Дефолтный провайдер картинок для ПЛАТНЫХ чатов, никогда не выставлявших /banana или
# /gptimage (ТЗ v0.10, docs/claude/billing.md). Бесплатные чаты всегда рисуют через GPT —
# это решает bot.get_chat_image_provider по тарифу, а не эта константа. Строка в
# chat_image_provider появляется только после явной команды.
DEFAULT_IMAGE_PROVIDER = "banana"

# Баланс ниже порога считается нулевым (тариф free). Порог, а не «> 0», чтобы пыль от
# float-сложений (1e-9) не оставляла чат в платном режиме на один лишний вызов.
PAID_MIN_BALANCE = 0.0001


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Значения на момент однократной миграции — заморожены; живой конфиг — bot.py.
_MIG_BONUS_GROUP = 10.0
_MIG_BONUS_PRIVATE = 5.0


def _run_once(conn: sqlite3.Connection, name: str, fn) -> None:
    """Однократная миграция данных: маркер в schema_migrations, откат при ошибке."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)).fetchone():
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        fn(conn)
        conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                     (name, int(time.time())))
        conn.commit()
        logger.info(f"Migration applied: {name}")
    except Exception:
        conn.rollback()
        logger.exception(f"Migration FAILED (повторится при следующем старте): {name}")


def _mig_access(conn: sqlite3.Connection) -> None:
    """Очистка бан-листа и рефералов (ТЗ v0.10, п.10.2). Роли banned/referral и статус
    rejected остаются валидными значениями CHECK, но больше никем не выставляются."""
    conn.execute("UPDATE allowed_chats SET status = 'pending' WHERE status = 'rejected'")
    conn.execute("UPDATE users SET role = 'street' WHERE role = 'banned'")
    conn.execute("UPDATE users SET role = 'premium', verified = 1 WHERE role = 'referral'")


def _mig_defaults(conn: sqlite3.Connection) -> None:
    """Старые глобальные дефолты (haiku / gpt) писались явными строками — без их удаления
    платные чаты остались бы на Haiku/GPT вместо нового дефолта Sonnet/banana."""
    conn.execute("DELETE FROM chat_models WHERE model = 'claude-haiku-4-5-20251001'")
    conn.execute("DELETE FROM chat_image_provider WHERE provider = 'gpt'")


def _mig_starter_bonus(conn: sqlite3.Connection) -> None:
    """Стартовый бонус проверенным (approved-группы, admin/premium) с активностью за 30 дней.
    Остальные проверенные получат его при первом новом сообщении (bot.py)."""
    now = int(time.time())
    cutoff = now - 30 * 86400
    groups = conn.execute(
        "SELECT chat_id FROM allowed_chats WHERE status = 'approved' AND chat_id < 0 AND EXISTS "
        "(SELECT 1 FROM group_messages g WHERE g.chat_id = allowed_chats.chat_id AND g.timestamp >= ?)",
        (cutoff,)).fetchall()
    users = conn.execute(
        "SELECT telegram_id FROM users WHERE role IN ('admin', 'premium') AND EXISTS "
        "(SELECT 1 FROM conversations c WHERE c.user_id = users.telegram_id AND c.timestamp >= ?)",
        (cutoff,)).fetchall()
    for row, amount in [(r, _MIG_BONUS_GROUP) for r in groups] + [(r, _MIG_BONUS_PRIVATE) for r in users]:
        _grant_bonus_conn(conn, row[0], amount)


def _grant_bonus_conn(conn: sqlite3.Connection, chat_id: int, amount: float) -> bool:
    cur = conn.execute(
        "INSERT INTO chat_credits (ts, chat_id, amount, kind, note, by_user) "
        "SELECT ?, ?, ?, 'bonus', 'starter', NULL "
        "WHERE NOT EXISTS (SELECT 1 FROM chat_credits WHERE chat_id = ? AND kind = 'bonus')",
        (int(time.time()), chat_id, amount, chat_id))
    return cur.rowcount > 0


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            role TEXT DEFAULT 'street' CHECK(role IN ('admin','premium','referral','street','banned')),
            referred_by INTEGER,
            referral_code TEXT UNIQUE,
            verified INTEGER DEFAULT 0,
            daily_messages INTEGER DEFAULT 0,
            daily_reset TEXT,
            created_at INTEGER,
            last_active INTEGER
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(telegram_id)
        );

        CREATE TABLE IF NOT EXISTS memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            fact TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT 'private',
            chat_id INTEGER,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(telegram_id)
        );

        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            sender_name TEXT,
            content TEXT NOT NULL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            timestamp INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS allowed_chats (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            status TEXT DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            requested_by INTEGER,
            approved_by INTEGER,
            created_at INTEGER,
            approved_at INTEGER,
            daily_review_enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS wa_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS wa_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            fact TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_models (
            chat_id INTEGER PRIMARY KEY,
            model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001'
        );

        CREATE TABLE IF NOT EXISTS chat_image_provider (
            chat_id INTEGER PRIMARY KEY,
            provider TEXT NOT NULL DEFAULT 'banana'
        );

        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            chat_id INTEGER,
            chat_type TEXT,
            user_id INTEGER,
            kind TEXT NOT NULL,
            model TEXT,
            label TEXT,
            input INTEGER DEFAULT 0,
            output INTEGER DEFAULT 0,
            cache_write INTEGER DEFAULT 0,
            cache_read INTEGER DEFAULT 0,
            thinking INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL,
            billed INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS chat_credits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            note TEXT,
            by_user INTEGER
        );

        CREATE TABLE IF NOT EXISTS daily_usage (
            day TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            msgs INTEGER NOT NULL DEFAULT 0,
            limit_notified INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_extract_state (
            chat_id INTEGER PRIMARY KEY,
            last_extract_id INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chat_rate_limits (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            interval_seconds INTEGER NOT NULL,
            last_ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
        CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(user_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user_id);
        CREATE INDEX IF NOT EXISTS idx_group_chat ON group_messages(chat_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_usage_chat_ts ON usage_log(chat_id, ts);
        CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_log(ts);
        -- покрывающие для баланса (SUM по chat_id без чтения таблицы)
        CREATE INDEX IF NOT EXISTS idx_usage_balance ON usage_log(chat_id, billed, cost_usd);
        CREATE INDEX IF NOT EXISTS idx_credits_balance ON chat_credits(chat_id, amount);
        CREATE INDEX IF NOT EXISTS idx_credits_chat ON chat_credits(chat_id);
        CREATE INDEX IF NOT EXISTS idx_wa_conv_phone ON wa_conversations(phone, timestamp);
        CREATE INDEX IF NOT EXISTS idx_wa_memory_phone ON wa_memory(phone);
    """)
    conn.commit()

    # Migrations for existing databases
    for migration in [
        "ALTER TABLE memory ADD COLUMN context TEXT NOT NULL DEFAULT 'private'",
        "ALTER TABLE memory ADD COLUMN chat_id INTEGER",
        "ALTER TABLE group_messages ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE memory ADD COLUMN tier TEXT NOT NULL DEFAULT 'long'",
        "ALTER TABLE memory ADD COLUMN expires_at INTEGER",
        # v0.9.0: чаты, залипшие на четвёртом поколении, переезжают на пятое.
        # Идемпотентно: после первого прогона строк под условие не остаётся.
        # Haiku не трогаем — claude-haiku-4-5-20251001 остаётся дефолтом.
        "UPDATE chat_models SET model='claude-sonnet-5' WHERE model LIKE 'claude-sonnet-4-%'",
        # (с 2026-09-23 — сразу на Opus 5.5: claude-opus-5 из выбора убран, см. ниже)
        "UPDATE chat_models SET model='claude-opus-5-5' WHERE model LIKE 'claude-opus-4-%'",
        "ALTER TABLE allowed_chats ADD COLUMN daily_review_enabled INTEGER NOT NULL DEFAULT 1",
        # 2026-09-19: Fable 5 -> Fable 5.1 (пул подтверждён живым запросом). Без этой
        # миграции чаты на старой строке попадут в model_meta() фолбэк на дефолт
        # (Haiku) и /cost посчитает их по ценам Haiku, а не Fable.
        "UPDATE chat_models SET model='claude-fable-5-1' WHERE model='claude-fable-5'",
        # ТЗ v0.10: бан — отдельные колонки, не роль/статус (бан не трогает баланс и проверку).
        "ALTER TABLE allowed_chats ADD COLUMN banned INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0",
        # 2026-09-23 (ТЗ feat/opus-5-5): Opus 5 -> Opus 5.5. chat_models — единственное
        # место, где хранится выбор модели чата, и оно же «прошлые платные настройки»:
        # free-режим эту таблицу не трогает (get_chat_model подменяет на лету). Точное
        # сравнение, не LIKE: claude-opus-5-5 и прочие строки не задеваем. usage_log —
        # история по замороженной цене, НЕ мигрируется.
        "UPDATE chat_models SET model='claude-opus-5-5' WHERE model='claude-opus-5'",
        # Токены thinking (usage.output_tokens_details.thinking_tokens) — входят в output,
        # отдельно только для видимости доли thinking в расходах.
        "ALTER TABLE usage_log ADD COLUMN thinking INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists

    _run_once(conn, "billing_access", _mig_access)
    _run_once(conn, "billing_defaults", _mig_defaults)
    _run_once(conn, "billing_starter_bonus", _mig_starter_bonus)
    # daily_usage нужна только за сегодня; строки старше недели — мусор.
    try:
        conn.execute("DELETE FROM daily_usage WHERE day < date('now', '-7 days')")
        conn.commit()
    except Exception:
        logger.exception("daily_usage cleanup failed")

    conn.close()
    logger.info("Database initialized")


# --- Users ---

def get_user(telegram_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def create_user(telegram_id: int, username: str = None, full_name: str = None,
                role: str = "street") -> dict:
    conn = get_conn()
    now = int(time.time())
    today = time.strftime("%Y-%m-%d")
    # referral_code/referred_by/daily_messages/daily_reset — мёртвые колонки (ТЗ v0.10):
    # таблицу не пересобирали (CHECK на role, UNIQUE на referral_code), NULL в UNIQUE безопасен.
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username, full_name, role, "
        "verified, daily_messages, daily_reset, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (telegram_id, username, full_name, role,
         1 if role in ('admin', 'premium') else 0, today, now, now)
    )
    conn.commit()
    conn.close()
    return get_user(telegram_id)


def get_or_create_user(telegram_id: int, username: str = None, full_name: str = None) -> dict:
    user = get_user(telegram_id)
    if not user:
        user = create_user(telegram_id, username, full_name)
    else:
        conn = get_conn()
        conn.execute("UPDATE users SET last_active = ?, username = COALESCE(?, username), "
                     "full_name = COALESCE(?, full_name) WHERE telegram_id = ?",
                     (int(time.time()), username, full_name, telegram_id))
        conn.commit()
        conn.close()
    return user


def set_role(telegram_id: int, role: str):
    conn = get_conn()
    conn.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id))
    if role in ('admin', 'premium'):
        conn.execute("UPDATE users SET verified = 1 WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()


def set_verified(telegram_id: int, verified: bool = True):
    conn = get_conn()
    conn.execute("UPDATE users SET verified = ? WHERE telegram_id = ?", (1 if verified else 0, telegram_id))
    conn.commit()
    conn.close()


def list_users_by_role(role: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users WHERE role = ? ORDER BY created_at", (role,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_users() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM users ORDER BY role, created_at").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Conversations ---

def save_message(user_id: int, role: str, content: str):
    conn = get_conn()
    conn.execute("INSERT INTO conversations (user_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
                 (user_id, role, content, int(time.time())))
    conn.commit()
    conn.close()


def get_conversation(user_id: int, limit: int = 40) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?",
        (user_id, limit)
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def clear_conversation(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# --- Memory ---

def get_memory(user_id: int, context: str = "private", chat_id: int = None,
               long_limit: int = None, medium_limit: int = None) -> list[str]:
    """Факты одного человека в одном контексте (личка, либо конкретный чат группы).

    long_limit/medium_limit — потолок на tier для ЭТОГО конкретного чтения; по умолчанию
    (None) — MEMORY_LONG_PER_USER/MEMORY_MEDIUM_PER_USER, те же, что кормят system-prompt
    (см. get_all_chat_memory). `/memory` передаёт побольше — MEMORY_LONG_DISPLAY/
    MEMORY_MEDIUM_DISPLAY (30/10), `/memory_full` — заведомо огромные числа (без потолка
    вообще). Без потолка по умолчанию /memory в группе для давнего активного участника
    рассыпалась на несколько сообщений вперемешку с обрывками слов посреди строки (найдено
    2026-08-31: 740 фактов на одного человека в одном чате, 34k символов — извлечение
    памяти плодит почти дубли типа "Поделился фото"/"Поделился фото в чате"/"Отправил фото
    в чат" вместо дедупа; сама гигиена дублей — отдельная открытая задача, см.
    docs/claude/open-tasks.md).
    """
    if long_limit is None:
        long_limit = MEMORY_LONG_PER_USER
    if medium_limit is None:
        medium_limit = MEMORY_MEDIUM_PER_USER
    conn = get_conn()
    now = int(time.time())
    chat_filter = "AND chat_id = ?" if (context == "group" and chat_id) else "AND chat_id IS NULL"
    params = (user_id, context) + ((chat_id,) if (context == "group" and chat_id) else ()) + (now,)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT fact, created_at, COALESCE(tier, 'long') AS tier,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(tier, 'long')
                       ORDER BY created_at DESC
                   ) AS rn
            FROM memory
            WHERE user_id = ? AND context = ? {chat_filter}
              AND (expires_at IS NULL OR expires_at > ?)
        )
        SELECT fact FROM ranked
        WHERE (tier = 'medium' AND rn <= ?) OR (tier != 'medium' AND rn <= ?)
        ORDER BY created_at
        """,
        params + (medium_limit, long_limit)
    ).fetchall()
    conn.close()
    return [r["fact"] for r in rows]


def add_memory_facts(user_id: int, facts: list[str], context: str = "private", chat_id: int = None,
                     tier: str = "long", expires_at: int = None):
    conn = get_conn()
    existing = set(get_memory(user_id, context, chat_id))
    now = int(time.time())
    for fact in facts:
        if fact not in existing:
            conn.execute(
                "INSERT INTO memory (user_id, fact, context, chat_id, created_at, tier, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, fact, context, chat_id, now, tier, expires_at)
            )
    conn.commit()
    conn.close()


def clear_memory(user_id: int, context: str = None):
    conn = get_conn()
    if context:
        conn.execute("DELETE FROM memory WHERE user_id = ? AND context = ?", (user_id, context))
    else:
        conn.execute("DELETE FROM memory WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_memory_for_private(user_id: int, long_limit: int = None, medium_limit: int = None) -> list[str]:
    """Чтение памяти для ЛИЧКИ (асимметрия секретности).

    Возвращает личные факты про человека ПЛЮС все групповые факты про него
    (узнанное в группах течёт вверх в личку). Обратного потока нет: групповое
    чтение (get_memory(context='group', chat_id)) личку не видит.
    Дедуп с сохранением порядка — один и тот же факт мог осесть и в личке, и в группе.
    Просроченные среднесрочные факты не включаются.

    long_limit/medium_limit — см. get_memory: по умолчанию (None) MEMORY_LONG_PER_USER/
    MEMORY_MEDIUM_PER_USER (то, что кормит system-prompt), `/memory` передаёт побольше,
    `/memory_full` — без потолка. Потолок по умолчанию обязателен: без него активный
    участник многих групп копит факты без ограничения — найдено 2026-08-31 у одного
    пользователя 1860 фактов, ~80k символов. Это и роняло /memory (BadRequest: Text is
    too long, лимит Telegram 4096), и молча раздувало system-prompt личных диалогов на
    КАЖДОЕ сообщение — тот же класс проблемы, что инцидент 2026-07-26, только на
    масштабе одного пользователя, а не всего чата.
    """
    if long_limit is None:
        long_limit = MEMORY_LONG_PER_USER
    if medium_limit is None:
        medium_limit = MEMORY_MEDIUM_PER_USER
    conn = get_conn()
    now = int(time.time())
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT fact, created_at, COALESCE(tier, 'long') AS tier,
                   ROW_NUMBER() OVER (
                       PARTITION BY COALESCE(tier, 'long')
                       ORDER BY created_at DESC
                   ) AS rn
            FROM memory
            WHERE user_id = ? AND context IN ('private', 'group')
              AND (expires_at IS NULL OR expires_at > ?)
        )
        SELECT fact FROM ranked
        WHERE (tier = 'medium' AND rn <= ?) OR (tier != 'medium' AND rn <= ?)
        ORDER BY created_at
        """,
        (user_id, now, medium_limit, long_limit)
    ).fetchall()
    conn.close()
    seen, out = set(), []
    for r in rows:
        if r["fact"] not in seen:
            seen.add(r["fact"])
            out.append(r["fact"])
    return out


# --- Group messages ---

def save_group_message(chat_id: int, user_id: int, sender_name: str, content: str, is_bot: bool = False):
    conn = get_conn()
    conn.execute(
        "INSERT INTO group_messages (chat_id, user_id, sender_name, content, is_bot, timestamp) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, user_id, sender_name, content, 1 if is_bot else 0, int(time.time()))
    )
    # Keep only last 1000 per chat
    conn.execute("""
        DELETE FROM group_messages WHERE chat_id = ? AND id NOT IN (
            SELECT id FROM group_messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 1000
        )
    """, (chat_id, chat_id))
    conn.commit()
    conn.close()


def get_group_history(chat_id: int, limit: int = 30, since_ts: int = 0) -> list[str]:
    """since_ts — брать только сообщения не старше этой метки (ежедневный обзор: с полуночи)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT sender_name, content FROM group_messages WHERE chat_id = ? AND timestamp >= ? "
        "ORDER BY timestamp DESC LIMIT ?",
        (chat_id, since_ts, limit)
    ).fetchall()
    conn.close()
    return [f"{r['sender_name']}: {r['content']}" for r in reversed(rows)]


def get_group_transcript(chat_id: int, limit: int = 40) -> list[dict]:
    """Групповая история для многоголосого messages-контекста.
    Возвращает старые->новые: [{"sender", "text", "is_bot", "ts"}]."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT sender_name, content, is_bot, timestamp FROM group_messages "
        "WHERE chat_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
        (chat_id, limit)
    ).fetchall()
    conn.close()
    return [
        {"sender": r["sender_name"], "text": r["content"], "is_bot": bool(r["is_bot"]), "ts": r["timestamp"]}
        for r in reversed(rows)
    ]


def count_chat_messages(chat_id: int) -> int:
    """Сколько не-бот сообщений в чате — для чат-уровневого каденса извлечения памяти."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM group_messages WHERE chat_id = ? AND is_bot = 0",
        (chat_id,)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def should_extract_chat_memory(chat_id: int, every: int) -> bool:
    """True, если в ЭТОМ чате с прошлого извлечения накопилось >= every сообщений.

    Считает строки чата новее last_extract_id, а не COUNT(*) по всему чату: подрезка
    до 1000 в save_group_message делает общий счётчик немонотонным, и проверка по
    модулю от него залипает.
    Дельту MAX(id) брать нельзя — id это глобальный AUTOINCREMENT на все чаты сразу,
    поэтому болтовня в соседней группе продвигала бы счётчик тихому чату и извлечение
    (вызов Haiku на 50 реплик) запускалось бы почти на каждом его сообщении.
    Побочный эффект: при True сразу двигает last_extract_id — повторного вызова не будет.
    """
    conn = get_conn()
    cur = conn.execute(
        "SELECT MAX(id) FROM group_messages WHERE chat_id = ?", (chat_id,)
    ).fetchone()[0] or 0
    row = conn.execute(
        "SELECT last_extract_id FROM chat_extract_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    last_id = row[0] if row else 0
    fresh = conn.execute(
        "SELECT COUNT(*) FROM group_messages WHERE chat_id = ? AND id > ?", (chat_id, last_id)
    ).fetchone()[0]
    if fresh < every:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO chat_extract_state (chat_id, last_extract_id) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET last_extract_id = excluded.last_extract_id",
        (chat_id, cur)
    )
    conn.commit()
    conn.close()
    return True


def get_user_id_by_name_in_chat(chat_id: int, sender_name: str) -> int | None:
    """Ищет user_id по sender_name в group_messages — для атрибуции фактов при извлечении памяти."""
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id FROM group_messages WHERE chat_id = ? AND sender_name = ? "
        "AND user_id IS NOT NULL ORDER BY timestamp DESC LIMIT 1",
        (chat_id, sender_name)
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


MEMORY_LONG_PER_USER = 12
MEMORY_MEDIUM_PER_USER = 8

# Отдельный, более щедрый потолок для команды /memory (человек читает сам, не модель
# на каждый промпт) — 30 долгосрочных + 10 недельных, против 12+8 для system-prompt.
MEMORY_LONG_DISPLAY = 30
MEMORY_MEDIUM_DISPLAY = 10


def get_all_chat_memory(chat_id: int) -> list[dict]:
    """Актуальные факты об участниках чата, не более N на человека по каждому tier.

    Возвращает [{"name": str, "long": [str, ...], "medium": [str, ...]}].
    Берутся самые свежие. Просроченные среднесрочные не включаются. Изоляция по chat_id.
    Потолок на человека обязателен: без него старые чаты раздували system-prompt
    до сотен тысяч токенов, и Клодушка переставала отвечать.
    """
    now = int(time.time())
    conn = get_conn()
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT m.user_id, m.fact, m.created_at,
                   COALESCE(m.tier, 'long') AS tier,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.user_id, COALESCE(m.tier, 'long')
                       ORDER BY m.created_at DESC
                   ) AS rn
            FROM memory m
            WHERE m.context = 'group' AND m.chat_id = ?
              AND (m.expires_at IS NULL OR m.expires_at > ?)
        )
        SELECT r.user_id, r.fact, r.tier,
               (SELECT gm.sender_name FROM group_messages gm
                WHERE gm.chat_id = ? AND gm.user_id = r.user_id AND gm.sender_name IS NOT NULL
                ORDER BY gm.timestamp DESC LIMIT 1) AS sender_name
        FROM ranked r
        WHERE (r.tier = 'medium' AND r.rn <= ?) OR (r.tier != 'medium' AND r.rn <= ?)
        ORDER BY r.user_id, r.tier, r.created_at
        """,
        (chat_id, now, chat_id, MEMORY_MEDIUM_PER_USER, MEMORY_LONG_PER_USER)
    ).fetchall()
    conn.close()
    by_user: dict[int, dict] = {}
    for r in rows:
        uid = r["user_id"]
        if uid not in by_user:
            by_user[uid] = {"name": r["sender_name"] or str(uid), "long": [], "medium": []}
        tier = r["tier"] if r["tier"] in ("long", "medium") else "long"
        by_user[uid][tier].append(r["fact"])
    return list(by_user.values())


# --- Allowed chats ---
# Статус проверки группы: approved = проверенная, pending = нет. Бан — отдельная колонка
# banned и не трогает ни статус, ни баланс (docs/claude/billing.md).

def get_chat_row(chat_id: int) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def ensure_group(chat_id: int, name: str = None, added_by: int = None) -> bool:
    """Регистрирует неизвестную группу как pending (непроверенную). Существующую строку
    не трогает (не сбрасывает approved/banned), только освежает имя. True — строка создана."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT OR IGNORE INTO allowed_chats (chat_id, name, status, requested_by, created_at) "
        "VALUES (?, ?, 'pending', ?, ?)",
        (chat_id, name, added_by, int(time.time()))
    )
    created = cur.rowcount > 0
    if not created and name:
        conn.execute("UPDATE allowed_chats SET name = ? WHERE chat_id = ? AND (name IS NULL OR name != ?)",
                     (name, chat_id, name))
    conn.commit()
    conn.close()
    return created


def set_chat_status(chat_id: int, status: str, approved_by: int = None) -> bool:
    """Возвращает True, если строка чата найдена и обновлена. Если chat_id не встречался
    в allowed_chats (чат подключён до появления трекинга через handle_new_chat, либо
    строку почему-то удалили) — UPDATE молча ничего не меняет, rowcount=0. Вызывающий
    обязан это проверять и не докладывать об успехе, которого не было — см. инцидент
    2026-09-16/17 в docs/claude/incidents.md, контракт — .claude/rules/db.md."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE allowed_chats SET status = ?, approved_by = ?, approved_at = ? WHERE chat_id = ?",
        (status, approved_by, int(time.time()) if status == "approved" else None, chat_id)
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def get_review_chats() -> list[dict]:
    """Группы-кандидаты на ежедневный обзор: не забанены, флаг включён. Платность
    (тариф) проверяет вызывающий — баланс живёт в chat_credits/usage_log."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM allowed_chats WHERE chat_id < 0 AND banned = 0 AND daily_review_enabled = 1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_group_verified(chat_id: int) -> bool:
    """Проверенная группа = allowed_chats.status='approved'."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM allowed_chats WHERE chat_id = ? AND status = 'approved'", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def set_chat_review_enabled(chat_id: int, enabled: bool) -> bool:
    """Возвращает True, если строка чата найдена и обновлена (чат уже известен боту)."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE allowed_chats SET daily_review_enabled = ? WHERE chat_id = ?",
        (1 if enabled else 0, chat_id)
    )
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


# --- Chat model settings ---

def get_chat_model_db(chat_id: int, default: str | None = None) -> str | None:
    """Сохранённый выбор модели или default, если строки нет. Эффективную модель с учётом
    тарифа (free → Haiku) решает bot.get_chat_model — здесь только хранилище."""
    conn = get_conn()
    row = conn.execute("SELECT model FROM chat_models WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row["model"] if row else default


def set_chat_model_db(chat_id: int, model: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chat_models (chat_id, model) VALUES (?, ?)",
        (chat_id, model)
    )
    conn.commit()
    conn.close()


# --- Chat image provider settings (banana / gpt, см. IMAGE_PROVIDERS в bot.py) ---

def get_chat_image_provider_db(chat_id: int) -> str:
    """Сохранённый выбор провайдера или DEFAULT_IMAGE_PROVIDER (дефолт ПЛАТНОГО чата);
    в free bot.get_chat_image_provider всегда отдаёт gpt, не читая эту функцию."""
    conn = get_conn()
    row = conn.execute("SELECT provider FROM chat_image_provider WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row["provider"] if row else DEFAULT_IMAGE_PROVIDER


def set_chat_image_provider_db(chat_id: int, provider: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chat_image_provider (chat_id, provider) VALUES (?, ?)",
        (chat_id, provider)
    )
    conn.commit()
    conn.close()


# --- Group rate limits (per chat_id + user_id, set by chat admins) ---
# Отсутствие строки = без ограничений. Строка есть = не чаще раза в interval_seconds.

def set_group_rate_limit(chat_id: int, user_id: int, interval_seconds: int):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chat_rate_limits (chat_id, user_id, interval_seconds, last_ts) VALUES (?, ?, ?, NULL)",
        (chat_id, user_id, interval_seconds)
    )
    conn.commit()
    conn.close()


def remove_group_rate_limit(chat_id: int, user_id: int = None):
    """user_id=None — снять лимиты со всех пользователей чата."""
    conn = get_conn()
    if user_id is None:
        conn.execute("DELETE FROM chat_rate_limits WHERE chat_id = ?", (chat_id,))
    else:
        conn.execute("DELETE FROM chat_rate_limits WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
    conn.commit()
    conn.close()


def get_group_rate_limit(chat_id: int, user_id: int) -> int | None:
    """Текущий лимит (в секундах) конкретного пользователя, без побочных эффектов."""
    conn = get_conn()
    row = conn.execute(
        "SELECT interval_seconds FROM chat_rate_limits WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row["interval_seconds"] if row else None


def get_group_rate_limits(chat_id: int) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, interval_seconds FROM chat_rate_limits WHERE chat_id = ? ORDER BY user_id",
        (chat_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def check_group_rate_limit(chat_id: int, user_id: int) -> bool:
    """True — можно отвечать. False — лимит ещё не истёк, надо молча промолчать."""
    conn = get_conn()
    row = conn.execute(
        "SELECT interval_seconds, last_ts FROM chat_rate_limits WHERE chat_id = ? AND user_id = ?",
        (chat_id, user_id)
    ).fetchone()
    if row is None:
        conn.close()
        return True
    now = int(time.time())
    if row["last_ts"] is not None and now - row["last_ts"] < row["interval_seconds"]:
        conn.close()
        return False
    conn.execute(
        "UPDATE chat_rate_limits SET last_ts = ? WHERE chat_id = ? AND user_id = ?",
        (now, chat_id, user_id)
    )
    conn.commit()
    conn.close()
    return True


def get_all_chats_for_status() -> list[dict]:
    """Все группы из allowed_chats: сохранённая модель (None = не выбирали), бан, баланс —
    для команды /chats. Эффективную модель считает вызывающий по тарифу."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ac.chat_id, ac.name, ac.status, ac.banned,
               cm.model AS model,
               COALESCE((SELECT SUM(amount) FROM chat_credits WHERE chat_id = ac.chat_id), 0)
             - COALESCE((SELECT SUM(cost_usd) FROM usage_log
                         WHERE chat_id = ac.chat_id AND billed = 1), 0) AS balance
        FROM allowed_chats ac
        LEFT JOIN chat_models cm ON cm.chat_id = ac.chat_id
        ORDER BY ac.status, ac.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Бан (ТЗ v0.10) ---
# Отдельные колонки users.banned / allowed_chats.banned. Бан НЕ трогает ни баланс, ни
# статус проверки — разбан возвращает всё как было.

def is_user_banned(telegram_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT banned FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return bool(row and row["banned"])


def set_user_banned(telegram_id: int, banned: bool) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE users SET banned = ? WHERE telegram_id = ?", (1 if banned else 0, telegram_id))
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


def is_chat_banned(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT banned FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return bool(row and row["banned"])


def set_chat_banned(chat_id: int, banned: bool) -> bool:
    conn = get_conn()
    cur = conn.execute("UPDATE allowed_chats SET banned = ? WHERE chat_id = ?", (1 if banned else 0, chat_id))
    conn.commit()
    updated = cur.rowcount > 0
    conn.close()
    return updated


# --- Кредиты и баланс (ТЗ v0.10) ---
# Баланс НЕ хранится: SUM(chat_credits.amount) - SUM(usage_log.cost_usd WHERE billed=1).
# Для личных чатов chat_id = telegram_id пользователя.

_BALANCE_SQL = (
    "SELECT COALESCE((SELECT SUM(amount) FROM chat_credits WHERE chat_id = ?), 0) "
    "- COALESCE((SELECT SUM(cost_usd) FROM usage_log WHERE chat_id = ? AND billed = 1), 0)"
)


def _balance_conn(conn: sqlite3.Connection, chat_id: int) -> float:
    return round(conn.execute(_BALANCE_SQL, (chat_id, chat_id)).fetchone()[0], 8)


def get_balance(chat_id: int) -> float:
    conn = get_conn()
    bal = _balance_conn(conn, chat_id)
    conn.close()
    return bal


def get_total_balance() -> float:
    """Сумма положительных балансов по всем чатам (для /cost админа)."""
    conn = get_conn()
    row = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN b > 0 THEN b ELSE 0 END), 0) FROM (
            SELECT chat_id, SUM(v) AS b FROM (
                SELECT chat_id, amount AS v FROM chat_credits
                UNION ALL
                SELECT chat_id, -cost_usd FROM usage_log WHERE billed = 1 AND chat_id IS NOT NULL
            ) GROUP BY chat_id
        )
    """).fetchone()
    conn.close()
    return round(row[0], 8)


def add_credit(chat_id: int, amount: float, kind: str, note: str = None, by_user: int = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO chat_credits (ts, chat_id, amount, kind, note, by_user) VALUES (?, ?, ?, ?, ?, ?)",
        (int(time.time()), chat_id, amount, kind, note, by_user))
    conn.commit()
    conn.close()


def topup_total(chat_id: int) -> float:
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM chat_credits WHERE chat_id = ? AND kind = 'topup'",
        (chat_id,)).fetchone()
    conn.close()
    return round(row[0], 8)


def has_bonus(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM chat_credits WHERE chat_id = ? AND kind = 'bonus'", (chat_id,)).fetchone()
    conn.close()
    return row is not None


def grant_starter_bonus(chat_id: int, amount: float) -> bool:
    """Идемпотентно: не выдаёт, если у chat_id уже есть kind='bonus'. True — выдан сейчас."""
    conn = get_conn()
    granted = _grant_bonus_conn(conn, chat_id, amount)
    conn.commit()
    conn.close()
    return granted


def settle_negative_balance(chat_id: int) -> float:
    """Баланс не уходит в минус: если он <0 — пишет writeoff ровно на дефицит (note='auto').
    Возвращает сумму списания (0.0 — ничего не делали)."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        bal = _balance_conn(conn, chat_id)
        written = 0.0
        if bal < 0:
            written = -bal
            conn.execute(
                "INSERT INTO chat_credits (ts, chat_id, amount, kind, note, by_user) "
                "VALUES (?, ?, ?, 'writeoff', 'auto', NULL)", (int(time.time()), chat_id, written))
        conn.commit()
        return written
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Учёт использования (ТЗ v0.10) ---

def record_usage(chat_id: int | None, chat_type: str | None, user_id: int | None, kind: str,
                 model: str | None, label: str | None, cost_usd: float, billed: bool,
                 inp: int = 0, out: int = 0, cache_write: int = 0, cache_read: int = 0,
                 thinking: int = 0, settle: bool = True) -> bool:
    """Пишет строку usage_log. Для платной записи (billed and settle) в той же транзакции
    добивает баланс до нуля writeoff-ом, если он ушёл в минус.

    Возвращает True, если ЭТА запись перевела чат из paid во free (баланс был выше порога,
    стал ниже) — ровно один раз на переход, сообщение о переходе шлёт вызывающий."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = _balance_conn(conn, chat_id) if (billed and settle and chat_id is not None) else None
        conn.execute(
            "INSERT INTO usage_log (ts, chat_id, chat_type, user_id, kind, model, label, input, output, "
            "cache_write, cache_read, thinking, cost_usd, billed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), chat_id, chat_type, user_id, kind, model, label,
             inp, out, cache_write, cache_read, thinking, cost_usd, 1 if billed else 0))
        went_free = False
        if before is not None:
            after = _balance_conn(conn, chat_id)
            if after < 0:
                conn.execute(
                    "INSERT INTO chat_credits (ts, chat_id, amount, kind, note, by_user) "
                    "VALUES (?, ?, ?, 'writeoff', 'auto', NULL)", (int(time.time()), chat_id, -after))
                after = 0.0
            went_free = before > PAID_MIN_BALANCE and after <= PAID_MIN_BALANCE
        conn.commit()
        return went_free
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def usage_count(kind: str, since_ts: int, chat_id: int, user_id: int = None, billed: bool = None) -> int:
    """Число записей usage_log с since_ts — для дневных лимитов картинок и поиска."""
    sql = "SELECT COUNT(*) FROM usage_log WHERE kind = ? AND ts >= ? AND chat_id = ?"
    params: list = [kind, since_ts, chat_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    if billed is not None:
        sql += " AND billed = ?"
        params.append(1 if billed else 0)
    conn = get_conn()
    n = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return n


_USAGE_GROUP_COLS = {"kind", "chat_type", "model", "label", "chat_id"}


def usage_grouped(since_ts: int, until_ts: int, cols: tuple = (), *, chat_id: int = None,
                  billed: bool = None, chat_null: bool = None) -> list[dict]:
    """Сумма cost_usd (+ токены и число вызовов) по колонкам cols за [since_ts, until_ts).
    chat_null=True — только служебное (chat_id IS NULL), False — только с чатом."""
    bad = set(cols) - _USAGE_GROUP_COLS
    if bad:
        raise ValueError(f"недопустимые колонки группировки: {bad}")
    select = "".join(f"{c}, " for c in cols)
    sql = (f"SELECT {select}SUM(cost_usd) AS cost, COUNT(*) AS calls, SUM(input) AS input, "
           f"SUM(output) AS output, SUM(cache_write) AS cache_write, SUM(cache_read) AS cache_read, "
           f"SUM(thinking) AS thinking "
           f"FROM usage_log WHERE ts >= ? AND ts < ?")
    params: list = [since_ts, until_ts]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    if billed is not None:
        sql += " AND billed = ?"
        params.append(1 if billed else 0)
    if chat_null is True:
        sql += " AND chat_id IS NULL"
    elif chat_null is False:
        sql += " AND chat_id IS NOT NULL"
    if cols:
        sql += " GROUP BY " + ", ".join(cols)
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows if r["cost"] is not None]


def credits_sum(kind: str, since_ts: int, until_ts: int, chat_id: int = None) -> float:
    sql = "SELECT COALESCE(SUM(amount), 0) FROM chat_credits WHERE kind = ? AND ts >= ? AND ts < ?"
    params: list = [kind, since_ts, until_ts]
    if chat_id is not None:
        sql += " AND chat_id = ?"
        params.append(chat_id)
    conn = get_conn()
    v = conn.execute(sql, params).fetchone()[0]
    conn.close()
    return round(v, 8)


# --- Дневные счётчики сообщений непроверенных (ТЗ v0.10, замена daily_messages) ---
# day — строка даты по BERLIN_TZ, считает bot.py. Считаются только сообщения, на которые
# бот реально ответил (bump зовётся после успешного ответа модели).

def daily_msgs_get(day: str, chat_id: int, user_id: int) -> int:
    conn = get_conn()
    row = conn.execute("SELECT msgs FROM daily_usage WHERE day = ? AND chat_id = ? AND user_id = ?",
                       (day, chat_id, user_id)).fetchone()
    conn.close()
    return row["msgs"] if row else 0


def daily_msgs_bump(day: str, chat_id: int, user_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO daily_usage (day, chat_id, user_id, msgs) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(day, chat_id, user_id) DO UPDATE SET msgs = msgs + 1",
        (day, chat_id, user_id))
    conn.commit()
    conn.close()


def daily_limit_notice_claim(day: str, chat_id: int, user_id: int) -> bool:
    """True — уведомление о лимите ещё не слали сегодня (и теперь помечено). Атомарно."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE daily_usage SET limit_notified = 1 "
        "WHERE day = ? AND chat_id = ? AND user_id = ? AND limit_notified = 0",
        (day, chat_id, user_id))
    conn.commit()
    claimed = cur.rowcount > 0
    conn.close()
    return claimed


# --- WhatsApp: conversations ---

def save_message_by_key(phone: str, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO wa_conversations (phone, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (phone, role, content, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_conversation_by_key(phone: str, limit: int = 40) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM wa_conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT ?",
        (phone, limit)
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# --- WhatsApp: memory ---

def get_memory_by_key(phone: str, context: str = "whatsapp") -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT fact FROM wa_memory WHERE phone = ? ORDER BY created_at",
        (phone,)
    ).fetchall()
    conn.close()
    return [r["fact"] for r in rows]


def add_memory_facts_by_key(phone: str, facts: list[str], context: str = "whatsapp"):
    conn = get_conn()
    existing = set(get_memory_by_key(phone))
    now = int(time.time())
    for fact in facts:
        if fact not in existing:
            conn.execute(
                "INSERT INTO wa_memory (phone, fact, created_at) VALUES (?, ?, ?)",
                (phone, fact, now)
            )
    conn.commit()
    conn.close()

import sqlite3
import time
import uuid
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("/app/data/claudushka.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
            approved_at INTEGER
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
        "UPDATE chat_models SET model='claude-opus-5'   WHERE model LIKE 'claude-opus-4-%'",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # Column already exists

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
                role: str = "street", referred_by: int = None) -> dict:
    conn = get_conn()
    ref_code = uuid.uuid4().hex[:8]
    now = int(time.time())
    today = time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT OR IGNORE INTO users (telegram_id, username, full_name, role, referred_by, "
        "referral_code, verified, daily_messages, daily_reset, created_at, last_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (telegram_id, username, full_name, role, referred_by, ref_code,
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


def get_referral_code(telegram_id: int) -> str | None:
    user = get_user(telegram_id)
    return user["referral_code"] if user else None


def get_user_by_referral(code: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE referral_code = ?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


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


def increment_daily_messages(telegram_id: int) -> int:
    conn = get_conn()
    today = time.strftime("%Y-%m-%d")
    user = get_user(telegram_id)
    if user and user["daily_reset"] != today:
        conn.execute("UPDATE users SET daily_messages = 1, daily_reset = ? WHERE telegram_id = ?",
                     (today, telegram_id))
        conn.commit()
        conn.close()
        return 1
    else:
        conn.execute("UPDATE users SET daily_messages = daily_messages + 1 WHERE telegram_id = ?",
                     (telegram_id,))
        conn.commit()
        row = conn.execute("SELECT daily_messages FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
        conn.close()
        return row["daily_messages"] if row else 0


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

def get_memory(user_id: int, context: str = "private", chat_id: int = None) -> list[str]:
    """Факты одного человека в одном контексте (личка, либо конкретный чат группы).

    Потолок MEMORY_LONG_PER_USER/MEMORY_MEDIUM_PER_USER (те же, что у get_all_chat_memory
    и get_memory_for_private) — без него /memory в группе для давнего активного участника
    рассыпалась на несколько сообщений вперемешку с обрывками слов посреди строки (найдено
    2026-08-31: 740 фактов на одного человека в одном чате, 34k символов — извлечение
    памяти плодит почти дубли типа "Поделился фото"/"Поделился фото в чате"/"Отправил фото
    в чат" вместо дедупа; сама гигиена дублей — отдельная открытая задача, см. CLAUDE.md).
    """
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
        params + (MEMORY_MEDIUM_PER_USER, MEMORY_LONG_PER_USER)
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


def get_memory_for_private(user_id: int) -> list[str]:
    """Чтение памяти для ЛИЧКИ (асимметрия секретности).

    Возвращает личные факты про человека ПЛЮС все групповые факты про него
    (узнанное в группах течёт вверх в личку). Обратного потока нет: групповое
    чтение (get_memory(context='group', chat_id)) личку не видит.
    Дедуп с сохранением порядка — один и тот же факт мог осесть и в личке, и в группе.
    Просроченные среднесрочные факты не включаются.

    Потолок MEMORY_LONG_PER_USER/MEMORY_MEDIUM_PER_USER (те же, что у get_all_chat_memory)
    обязателен: без него активный участник многих групп копит факты без ограничения —
    найдено 2026-08-31 у одного пользователя 1860 фактов, ~80k символов. Это и роняло
    /memory (BadRequest: Text is too long, лимит Telegram 4096), и молча раздувало
    system-prompt личных диалогов на КАЖДОЕ сообщение — тот же класс проблемы, что
    инцидент 2026-07-26, только на масштабе одного пользователя, а не всего чата.
    """
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
        (user_id, now, MEMORY_MEDIUM_PER_USER, MEMORY_LONG_PER_USER)
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


def get_group_history(chat_id: int, limit: int = 30) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT sender_name, content FROM group_messages WHERE chat_id = ? ORDER BY timestamp DESC LIMIT ?",
        (chat_id, limit)
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


def count_user_messages_in_chat(chat_id: int, user_id: int) -> int:
    """Сколько собственных (не-бот) сообщений написал юзер в чате — для каденса извлечения памяти."""
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM group_messages WHERE chat_id = ? AND user_id = ? AND is_bot = 0",
        (chat_id, user_id)
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


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

def add_allowed_chat(chat_id: int, name: str = None, added_by: int = None, status: str = "approved"):
    conn = get_conn()
    now = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO allowed_chats (chat_id, name, status, requested_by, approved_by, created_at, approved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, name, status, added_by, added_by if status == "approved" else None, now, now if status == "approved" else None)
    )
    conn.commit()
    conn.close()


def set_chat_status(chat_id: int, status: str, approved_by: int = None):
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_chats SET status = ?, approved_by = ?, approved_at = ? WHERE chat_id = ?",
        (status, approved_by, int(time.time()) if status == "approved" else None, chat_id)
    )
    conn.commit()
    conn.close()


def get_pending_chats() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM allowed_chats WHERE status = 'pending'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_allowed_chat(chat_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_allowed_chats() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM allowed_chats WHERE status = 'approved'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_chat_allowed(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM allowed_chats WHERE chat_id = ? AND status = 'approved'", (chat_id,)).fetchone()
    conn.close()
    return row is not None


# --- Chat model settings ---

def get_chat_model_db(chat_id: int) -> str:
    conn = get_conn()
    row = conn.execute("SELECT model FROM chat_models WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row["model"] if row else "claude-haiku-4-5-20251001"


def set_chat_model_db(chat_id: int, model: str):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO chat_models (chat_id, model) VALUES (?, ?)",
        (chat_id, model)
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
    """Все чаты из allowed_chats с их моделью — для команды /chats."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT ac.chat_id, ac.name, ac.status,
               COALESCE(cm.model, 'claude-haiku-4-5-20251001') AS model
        FROM allowed_chats ac
        LEFT JOIN chat_models cm ON cm.chat_id = ac.chat_id
        ORDER BY ac.status, ac.name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def clear_conversation_by_key(phone: str):
    conn = get_conn()
    conn.execute("DELETE FROM wa_conversations WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()


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


def clear_memory_by_key(phone: str):
    conn = get_conn()
    conn.execute("DELETE FROM wa_memory WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()


# --- Migration from JSON ---

def migrate_from_json(allowed_file: str, data_dir: Path):
    """One-time migration from JSON files to SQLite."""
    import json

    # Migrate allowed.json
    try:
        with open(allowed_file, "r") as f:
            data = json.load(f)
        for u in data.get("users", []):
            user = get_user(u["id"])
            if not user:
                create_user(u["id"], full_name=u.get("name"), role="premium")
            else:
                set_role(u["id"], "premium")
        for c in data.get("chats", []):
            add_allowed_chat(c["id"], c.get("name"))
        logger.info("Migrated allowed.json")
    except FileNotFoundError:
        pass

    # Migrate memory.json
    try:
        mem_file = data_dir / "memory.json"
        if mem_file.exists():
            with open(mem_file, "r") as f:
                mem_data = json.load(f)
            for uid, facts in mem_data.items():
                tid = int(uid)
                get_or_create_user(tid)
                add_memory_facts(tid, facts)
            logger.info("Migrated memory.json")
    except Exception as e:
        logger.error(f"Memory migration error: {e}")

    # Migrate conversations.json
    try:
        conv_file = data_dir / "conversations.json"
        if conv_file.exists():
            with open(conv_file, "r") as f:
                conv_data = json.load(f)
            for uid, messages in conv_data.items():
                tid = int(uid)
                get_or_create_user(tid)
                for msg in messages:
                    save_message(tid, msg["role"], msg["content"])
            logger.info("Migrated conversations.json")
    except Exception as e:
        logger.error(f"Conversations migration error: {e}")

    # Migrate verified_users.json
    try:
        ver_file = data_dir / "verified_users.json"
        if ver_file.exists():
            with open(ver_file, "r") as f:
                ver_data = json.load(f)
            for uid in ver_data:
                tid = int(uid)
                get_or_create_user(tid, full_name=ver_data[uid].get("name"))
                set_verified(tid, True)
            logger.info("Migrated verified_users.json")
    except Exception as e:
        logger.error(f"Verified migration error: {e}")
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
            created_at INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(telegram_id)
        );

        CREATE TABLE IF NOT EXISTS group_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            sender_name TEXT,
            content TEXT NOT NULL,
            timestamp INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS allowed_chats (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            added_by INTEGER,
            created_at INTEGER
        );

        CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id);
        CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversations(user_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_memory_user ON memory(user_id);
        CREATE INDEX IF NOT EXISTS idx_group_chat ON group_messages(chat_id, timestamp);
    """)
    conn.commit()
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

def get_memory(user_id: int) -> list[str]:
    conn = get_conn()
    rows = conn.execute("SELECT fact FROM memory WHERE user_id = ? ORDER BY created_at", (user_id,)).fetchall()
    conn.close()
    return [r["fact"] for r in rows]


def add_memory_facts(user_id: int, facts: list[str]):
    conn = get_conn()
    existing = set(get_memory(user_id))
    now = int(time.time())
    for fact in facts:
        if fact not in existing:
            conn.execute("INSERT INTO memory (user_id, fact, created_at) VALUES (?, ?, ?)",
                         (user_id, fact, now))
    conn.commit()
    conn.close()


def clear_memory(user_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM memory WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


# --- Group messages ---

def save_group_message(chat_id: int, user_id: int, sender_name: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO group_messages (chat_id, user_id, sender_name, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (chat_id, user_id, sender_name, content, int(time.time()))
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


# --- Allowed chats ---

def add_allowed_chat(chat_id: int, name: str = None, added_by: int = None):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO allowed_chats (chat_id, name, added_by, created_at) VALUES (?, ?, ?, ?)",
                 (chat_id, name, added_by, int(time.time())))
    conn.commit()
    conn.close()


def remove_allowed_chat(chat_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def get_allowed_chats() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM allowed_chats").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def is_chat_allowed(chat_id: int) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return row is not None


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
                user = get_or_create_user(tid, full_name=ver_data[uid].get("name"))
                set_verified(tid, True)
            logger.info("Migrated verified_users.json")
    except Exception as e:
        logger.error(f"Verified migration error: {e}")

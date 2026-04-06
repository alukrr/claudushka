#!/usr/bin/env python3
"""One-time script to set premium role for users who were manually added before."""

import sqlite3
import uuid
import time

DB_PATH = "/app/data/claudushka.db"

PREMIUM_IDS = [
    784749059,   # Loveve
    348731131,   # simbiotiq
    816539072,   # Julia Tynec
    217694962,   # DeadORC
    195010033,   # Lennon
    315526885,   # Anton Brovkin
    184720260,   # Елена Радость
    289808542,   # Olga
    375164565,   # Фейхоевое варенье
    51511130,    # Alyenor
    48096097,    # Вячеслав Cujo
    145816958,   # Tuki Tuki
    151915362,   # Viktor Matushkin
    164169466,   # Kristina V
    1716852373,  # Amazing Jade Stern
    288744651,   # Пухтаст Пропужденный
    199857144,   # Ilya Savin
    617864508,   # anteater
]

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")

updated = 0
created = 0

for uid in PREMIUM_IDS:
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (uid,)).fetchone()
    if row:
        conn.execute("UPDATE users SET role = 'premium', verified = 1 WHERE telegram_id = ?", (uid,))
        print(f"Updated: {uid} ({row['full_name'] or row['username'] or '?'})")
        updated += 1
    else:
        ref_code = uuid.uuid4().hex[:8]
        now = int(time.time())
        today = time.strftime("%Y-%m-%d")
        conn.execute(
            "INSERT INTO users (telegram_id, role, referral_code, verified, daily_messages, daily_reset, created_at, last_active) "
            "VALUES (?, 'premium', ?, 1, 0, ?, ?, ?)",
            (uid, ref_code, today, now, now)
        )
        print(f"Created: {uid}")
        created += 1

conn.commit()
conn.close()

print(f"\nDone. Updated: {updated}, Created: {created}")

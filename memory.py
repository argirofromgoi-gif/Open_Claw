import sqlite3
import threading
from pathlib import Path

DB_PATH = Path("conversation_memory.db")
_lock = threading.Lock()
MAX_HISTORY = 20


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            role     TEXT NOT NULL,
            content  TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS image_cache (
            channel_id  TEXT PRIMARY KEY,
            last_url    TEXT,
            last_prompt TEXT,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def add_to_history(channel_id: str, role: str, content: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)",
            (str(channel_id), role, str(content)),
        )
        # Keep only the last MAX_HISTORY messages per channel
        conn.execute(
            """
            DELETE FROM history
            WHERE channel_id = ?
              AND id NOT IN (
                SELECT id FROM history
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT ?
              )
            """,
            (str(channel_id), str(channel_id), MAX_HISTORY),
        )
        conn.commit()
        conn.close()


def get_history(channel_id: str) -> list[dict]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT role, content FROM history WHERE channel_id = ? ORDER BY id ASC",
            (str(channel_id),),
        ).fetchall()
        conn.close()
    return [{"role": row[0], "content": row[1]} for row in rows]


def save_last_image(channel_id: str, url: str, prompt: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO image_cache (channel_id, last_url, last_prompt, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(channel_id) DO UPDATE SET
                last_url    = excluded.last_url,
                last_prompt = excluded.last_prompt,
                updated_at  = CURRENT_TIMESTAMP
            """,
            (str(channel_id), url, prompt),
        )
        conn.commit()
        conn.close()


def get_last_image(channel_id: str) -> dict | None:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT last_url, last_prompt FROM image_cache WHERE channel_id = ?",
            (str(channel_id),),
        ).fetchone()
        conn.close()
    return {"url": row[0], "prompt": row[1]} if row else None


def clear_history(channel_id: str) -> None:
    with _lock:
        conn = _get_conn()
        conn.execute("DELETE FROM history WHERE channel_id = ?", (str(channel_id),))
        conn.commit()
        conn.close()

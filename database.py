import sqlite3
import shutil
from datetime import datetime, timezone
from pathlib import Path
from config import DB_PATH, TARGET_DIR


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create the threads table if it doesn't exist."""
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id      TEXT PRIMARY KEY,
            subject        TEXT,
            last_msg_count INTEGER,
            category       TEXT,
            urgency        TEXT,
            folder_name    TEXT,
            processed_at   TEXT
        )
    """)
    conn.commit()
    conn.close()


def get_thread(thread_id: str) -> dict | None:
    """Look up a thread by its Gmail thread ID."""
    conn = _conn()
    row = conn.execute(
        "SELECT * FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def should_process(thread_id: str, current_msg_count: int) -> str:
    """
    Returns:
        'new'     — never seen before, process it
        'updated' — msg count increased, delete old folder & re-process
        'skip'    — same count, nothing changed
    """
    existing = get_thread(thread_id)
    if existing is None:
        return "new"
    if current_msg_count > existing["last_msg_count"]:
        return "updated"
    return "skip"


def upsert_thread(
    thread_id: str,
    subject: str,
    msg_count: int,
    category: str,
    urgency: str,
    folder_name: str,
):
    """Insert or update a thread record after processing."""
    conn = _conn()
    conn.execute(
        """
        INSERT INTO threads (thread_id, subject, last_msg_count, category,
                             urgency, folder_name, processed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(thread_id) DO UPDATE SET
            subject        = excluded.subject,
            last_msg_count = excluded.last_msg_count,
            category       = excluded.category,
            urgency        = excluded.urgency,
            folder_name    = excluded.folder_name,
            processed_at   = excluded.processed_at
        """,
        (
            thread_id, subject, msg_count, category, urgency,
            folder_name, datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def delete_thread_folder(folder_name: str):
    """Wipe an existing output folder before re-processing."""
    folder_path = TARGET_DIR / folder_name
    if folder_path.exists():
        shutil.rmtree(folder_path)


def get_all_threads() -> list[dict]:
    """Return all processed threads (for building index.json)."""
    conn = _conn()
    rows = conn.execute(
        "SELECT * FROM threads ORDER BY processed_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Auto-init on import
init_db()
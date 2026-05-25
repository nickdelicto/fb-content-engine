"""SQLite operations for tracking which posts have been marked as posted."""
import datetime
import sqlite3
from typing import Optional


def get_status_lookup(conn: sqlite3.Connection, niche: str, target_date: str) -> dict:
    """Return dict keyed by (niche, target_date, post_id) → {posted_at, posted_by}."""
    cur = conn.execute(
        "SELECT niche, target_date, post_id, posted_at, posted_by "
        "FROM post_status WHERE niche = ? AND target_date = ?",
        (niche, target_date),
    )
    return {
        (row["niche"], row["target_date"], row["post_id"]): {
            "posted_at": row["posted_at"],
            "posted_by": row["posted_by"],
        }
        for row in cur.fetchall()
    }


def mark_posted(conn: sqlite3.Connection, niche: str, target_date: str,
                post_id: str, email: str) -> None:
    """Mark a post as posted (or update if already exists)."""
    conn.execute(
        "INSERT OR REPLACE INTO post_status "
        "(niche, target_date, post_id, posted_at, posted_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (niche, target_date, post_id, datetime.datetime.now().isoformat(timespec="seconds"), email),
    )
    conn.commit()


def unmark_posted(conn: sqlite3.Connection, niche: str, target_date: str, post_id: str) -> None:
    """Unmark (delete the status record) — useful if operator clicks by mistake."""
    conn.execute(
        "DELETE FROM post_status WHERE niche = ? AND target_date = ? AND post_id = ?",
        (niche, target_date, post_id),
    )
    conn.commit()

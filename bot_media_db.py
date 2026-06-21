"""bot_media_db.py — Persistent SQLite database for Bot API file mapping.

Maps channel message IDs to their Bot API file info so we never need to
forward the same message twice.  After the user client forwards a channel
video to the bot, we capture the Bot API file_id via getUpdates, call
getFile to download it to the local Bot API file cache, and store the
mapping here.

Schema
------
channel_msg_id  INTEGER PRIMARY KEY  — the original message ID in the channel
bot_chat_msg_id INTEGER              — the message ID in the bot's chat
botapi_file_id  TEXT                 — Bot API file_id (usable with getFile)
file_path       TEXT                 — Bot API file_path (from getFile response)
disk_path       TEXT                 — absolute local path (BOT_API_DIR + file_path)
size            INTEGER              — file size in bytes
mime_type       TEXT                 — MIME type
filename        TEXT                 — original filename
forward_date    REAL                 — timestamp when forwarded
last_used_at    REAL                 — last time this entry was used for streaming
"""

import os
import sqlite3
import time as _time

import config as _cfg

# ── Database path ──────────────────────────────────────────────────────────
_DB_PATH = os.path.join(_cfg._here, "bot_media.db")
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """Return the database connection (singleton, thread-safe)."""
    global _conn
    if _conn is not None:
        return _conn
    _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("""CREATE TABLE IF NOT EXISTS bot_media(
            channel_msg_id  INTEGER PRIMARY KEY,
            bot_chat_msg_id INTEGER,
            botapi_file_id  TEXT,
            file_path       TEXT,
            disk_path       TEXT,
            size            INTEGER,
            mime_type       TEXT,
            filename        TEXT,
            forward_date    REAL,
            last_used_at    REAL
        )""")
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_media_last_used "
        "ON bot_media(last_used_at)"
    )
    _conn.commit()
    try:
        row = _conn.execute("SELECT COUNT(*) FROM bot_media").fetchone()
        count = row[0] if row else 0
        if count > 0:
            print(f"[bot_media_db] loaded {count} cached file mappings")
        else:
            print("[bot_media_db] empty — will populate on first stream")
    except Exception:
        pass
    return _conn


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════


def lookup(channel_msg_id: int) -> dict | None:
    """Look up a channel message ID in the database.

    Returns a dict with all columns, or None if not found.
    Also updates last_used_at on hit.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT channel_msg_id, bot_chat_msg_id, botapi_file_id, "
        "       file_path, disk_path, size, mime_type, filename, "
        "       forward_date, last_used_at "
        "FROM bot_media WHERE channel_msg_id = ?",
        (channel_msg_id,),
    ).fetchone()
    if row is None:
        return None
    # Update last_used_at
    now = _time.time()
    conn.execute(
        "UPDATE bot_media SET last_used_at = ? WHERE channel_msg_id = ?",
        (now, channel_msg_id),
    )
    conn.commit()
    return {
        "channel_msg_id": row[0],
        "bot_chat_msg_id": row[1],
        "botapi_file_id": row[2],
        "file_path": row[3],
        "disk_path": row[4],
        "size": row[5],
        "mime_type": row[6],
        "filename": row[7],
        "forward_date": row[8],
        "last_used_at": now,
    }


def store(
    channel_msg_id: int,
    bot_chat_msg_id: int,
    botapi_file_id: str,
    file_path: str,
    disk_path: str,
    size: int,
    mime_type: str,
    filename: str,
) -> None:
    """Store a new mapping in the database (upsert).

    This is called after a full forward + getFile cycle, so both
    file_path and disk_path are available.
    """
    conn = _get_conn()
    now = _time.time()
    conn.execute(
        """INSERT OR REPLACE INTO bot_media(
            channel_msg_id, bot_chat_msg_id, botapi_file_id,
            file_path, disk_path, size, mime_type, filename,
            forward_date, last_used_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_msg_id,
            bot_chat_msg_id,
            botapi_file_id,
            file_path,
            disk_path,
            size,
            mime_type,
            filename,
            now,
            now,
        ),
    )
    conn.commit()


def store_forward_only(
    channel_msg_id: int,
    bot_chat_msg_id: int,
    botapi_file_id: str,
    size: int,
    mime_type: str,
    filename: str,
) -> None:
    """Store a forward-only mapping — file_id known but file NOT downloaded yet.

    Called by botapi_sync_all() during proactive sync. The file_id is captured
    from getUpdates, but getFile is NOT called (no download). When the video is
    played later, _botapi_resolve_file() will call getFile to download on-demand
    and then call update_paths() to fill in file_path and disk_path.
    """
    conn = _get_conn()
    now = _time.time()
    conn.execute(
        """INSERT OR REPLACE INTO bot_media(
            channel_msg_id, bot_chat_msg_id, botapi_file_id,
            file_path, disk_path, size, mime_type, filename,
            forward_date, last_used_at
        ) VALUES(?, ?, ?, '', '', ?, ?, ?, ?, ?)""",
        (
            channel_msg_id,
            bot_chat_msg_id,
            botapi_file_id,
            size,
            mime_type,
            filename,
            now,
            now,
        ),
    )
    conn.commit()


def update_paths(channel_msg_id: int, file_path: str, disk_path: str) -> None:
    """Update file_path and disk_path after a re-download (file re-appeared)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE bot_media SET file_path = ?, disk_path = ?, last_used_at = ? "
        "WHERE channel_msg_id = ?",
        (file_path, disk_path, _time.time(), channel_msg_id),
    )
    conn.commit()


def count() -> int:
    """Return total number of cached mappings."""
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) FROM bot_media").fetchone()
    return row[0] if row else 0


def stats() -> dict:
    """Return database statistics."""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(*) FROM bot_media").fetchone()[0]
    on_disk = 0
    total_size = 0
    if total > 0:
        rows = conn.execute("SELECT disk_path, size FROM bot_media").fetchall()
        for disk_path, size in rows:
            total_size += size or 0
            if disk_path and os.path.exists(disk_path):
                on_disk += 1
    return {
        "total": total,
        "on_disk": on_disk,
        "total_size_mb": round(total_size / 1024 / 1024, 1),
    }

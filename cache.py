import re, json, asyncio, os, sqlite3

DELETED_ALBUMS_FILE = "deleted_albums.json"
import aiohttp
import config as _cfg  # module import so _cfg.client is always live
from config import (
    _here,
    CACHE_FILE,
    ALBUMS_FILE,
    IMDB_CACHE_FILE,
    POSTERS_DIR,
    _safe_mime,
)
from helpers import (
    is_video,
    get_filename,
    get_mime,
    get_size,
    get_duration,
    get_video_attrs,
    get_doc_attrs,  # ← single-pass extractor
    _quality_from_dims,
    derive_album,
    album_key,
    _canonicalize_album,
    _parse_caption,
    _fmt_dur,
    _fmt_size,
    _parse_season,
)

# ── HLS SEGMENT CACHE DIR ─────────────────────────────────────────────────────
_HLS_DIR = os.path.join(_here, "hls_cache")

# ── INCREMENTAL FETCH STATE ───────────────────────────────────────────────────
_FETCH_STATE_FILE = os.path.join(_here, ".fetch_state.json")


def _load_fetch_state() -> dict:
    try:
        if os.path.exists(_FETCH_STATE_FILE):
            with open(_FETCH_STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_id": 0}


def _save_fetch_state(state: dict) -> None:
    try:
        with open(_FETCH_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"[fetch_state] save error: {e}")


# ── FTS5 SHADOW INDEX ─────────────────────────────────────────────────────────
_FTS_DB = os.path.join(_here, "cache_fts.db")
_fts_conn: sqlite3.Connection = None


def _fts_init() -> sqlite3.Connection:
    global _fts_conn
    if _fts_conn is not None:
        return _fts_conn
    os.makedirs(os.path.dirname(_FTS_DB) or ".", exist_ok=True)
    _fts_conn = sqlite3.connect(_FTS_DB, check_same_thread=False)
    _fts_conn.execute("PRAGMA journal_mode=WAL")
    _fts_conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
            message_id UNINDEXED,
            title, filename, album, caption,
            tokenize='unicode61'
        )""")
    _fts_conn.execute("""CREATE TABLE IF NOT EXISTS album_names(
            album TEXT PRIMARY KEY
        )""")
    # Plain table for O(1) exact lookups (album assign, manage page)
    _fts_conn.execute("""CREATE TABLE IF NOT EXISTS videos_plain(
            message_id INTEGER PRIMARY KEY,
            album TEXT,
            title TEXT,
            filename TEXT,
            caption TEXT,
            thumb_url TEXT,
            date TEXT,
            size_bytes INTEGER,
            size TEXT,
            duration TEXT,
            quality TEXT,
            mime_type TEXT,
            has_override INTEGER DEFAULT 0
        )""")
    _fts_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_plain_album ON videos_plain(album)"
    )
    _fts_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_plain_date ON videos_plain(date)"
    )
    _fts_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_plain_title ON videos_plain(title)"
    )
    _fts_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_plain_size ON videos_plain(size_bytes)"
    )
    _fts_conn.commit()
    # Log index state on startup
    try:
        row_count = _fts_conn.execute("SELECT COUNT(*) FROM videos_plain").fetchone()[0]
        if row_count > 0:
            print(f"[fts] Loaded existing index with {row_count} videos from disk")
        else:
            print("[fts] Index is empty — will build on first cache load")
    except Exception:
        pass
    return _fts_conn


def _sanitize(s):
    """Strip surrogate characters that break utf-8 encoding."""
    if not s:
        return s or ""
    return s.encode("utf-8", errors="ignore").decode("utf-8")


def _fts_sync(data: list) -> None:
    """Replace FTS5 index with current cache contents (called after every load/save)."""
    conn = _fts_init()
    conn.execute("DELETE FROM videos_fts")
    conn.execute("DELETE FROM album_names")
    conn.execute("DELETE FROM videos_plain")
    conn.executemany(
        "INSERT INTO videos_fts(message_id,title,filename,album,caption) VALUES(?,?,?,?,?)",
        [
            (
                v["message_id"],
                v.get("title", "") or "",
                v.get("filename", "") or "",
                v.get("album", "") or "",
                v.get("caption", "") or "",
            )
            for v in data
            if v.get("message_id") is not None
        ],
    )
    conn.executemany(
        """INSERT OR REPLACE INTO videos_plain(
            message_id,album,title,filename,caption,
            thumb_url,date,size_bytes,size,duration,quality,mime_type,has_override
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                v["message_id"],
                v.get("album", "") or "",
                v.get("title", "") or "",
                v.get("filename", "") or "",
                v.get("caption", "") or "",
                v.get("thumb_url") or "",
                v.get("date", "") or "",
                v.get("size_bytes", 0) or 0,
                v.get("size", "") or "",
                v.get("duration", "") or "",
                v.get("quality", "") or "",
                v.get("mime_type", "") or "",
                1 if v.get("album_pinned") else 0,
            )
            for v in data
        ],
    )
    albums = {v.get("album", "") for v in data if v.get("album")}
    conn.executemany(
        "INSERT OR IGNORE INTO album_names(album) VALUES(?)", [(a,) for a in albums]
    )
    conn.commit()


def _fts_upsert(videos: list) -> None:
    """Fast-path: upsert a small batch of new videos without wiping the whole index.

    Used by _fetch_incremental_merge so the expensive DELETE+re-INSERT only
    happens on a full reload, not on every background sync of 1–N new items.
    """
    if not videos:
        return
    conn = _fts_init()
    # FTS5 has no UPSERT — delete the rows we're about to re-insert
    ids = [v["message_id"] for v in videos]
    conn.executemany("DELETE FROM videos_fts WHERE message_id = ?", [(i,) for i in ids])
    conn.executemany(
        "INSERT INTO videos_fts(message_id,title,filename,album,caption) VALUES(?,?,?,?,?)",
        [
            (
                v["message_id"],
                v.get("title", "") or "",
                v.get("filename", "") or "",
                v.get("album", "") or "",
                v.get("caption", "") or "",
            )
            for v in videos
        ],
    )
    conn.executemany(
        """INSERT OR REPLACE INTO videos_plain(
            message_id,album,title,filename,caption,
            thumb_url,date,size_bytes,size,duration,quality,mime_type,has_override
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                v["message_id"],
                v.get("album", "") or "",
                v.get("title", "") or "",
                v.get("filename", "") or "",
                v.get("caption", "") or "",
                v.get("thumb_url") or "",
                v.get("date", "") or "",
                v.get("size_bytes", 0) or 0,
                v.get("size", "") or "",
                v.get("duration", "") or "",
                v.get("quality", "") or "",
                v.get("mime_type", "") or "",
                1 if v.get("album_pinned") else 0,
            )
            for v in videos
        ],
    )
    new_albums = {v.get("album", "") for v in videos if v.get("album")}
    conn.executemany(
        "INSERT OR IGNORE INTO album_names(album) VALUES(?)",
        [(a,) for a in new_albums],
    )
    conn.commit()


def fts_search(query: str) -> list:
    """Return message_ids matching FTS query, ranked by BM25."""
    conn = _fts_init()
    try:
        rows = conn.execute(
            "SELECT message_id FROM videos_fts WHERE videos_fts MATCH ? ORDER BY rank",
            (query,),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def fts_album_ids(album_name: str) -> list:
    """Return message_ids whose album exactly matches album_name — O(log n)."""
    conn = _fts_init()
    try:
        rows = conn.execute(
            "SELECT message_id FROM videos_fts WHERE album MATCH ? ORDER BY rank",
            (f'"{album_name.replace(chr(34), "")}"',),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []


def fts_album_names() -> list:
    """Return sorted unique album names from the FTS shadow table."""
    conn = _fts_init()
    rows = conn.execute("SELECT album FROM album_names ORDER BY album").fetchall()
    return [r[0] for r in rows]


def fts_album_data(album_name: str) -> list:
    """Single indexed SQL on videos_plain WHERE album = ? — O(log n), no dict walk."""
    try:
        conn = _fts_init()
        rows = conn.execute(
            """SELECT message_id, title, filename, caption, thumb_url,
                      date, size_bytes, size, duration, quality, mime_type
               FROM videos_plain WHERE album = ?""",
            (album_name,),
        ).fetchall()
        return [
            {
                "message_id": r[0],
                "title": r[1] or "",
                "filename": r[2] or "",
                "caption": r[3] or "",
                "thumb_url": r[4] or "",
                "date": r[5] or "",
                "size_bytes": r[6] or 0,
                "size": r[7] or "",
                "duration": r[8] or "",
                "quality": r[9] or "",
                "mime_type": r[10] or "",
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[fts] fts_album_data error: {e}")
        return []


def fts_manage_videos() -> list:
    """Return all videos for /manage from the plain indexed table — O(n) but no Python loop."""
    try:
        conn = _fts_init()
        rows = conn.execute(
            """SELECT message_id, album, title, thumb_url, date, size_bytes, size, has_override
               FROM videos_plain ORDER BY date DESC"""
        ).fetchall()
        return [
            {
                "message_id": r[0],
                "album": r[1],
                "title": r[2],
                "thumb_url": r[3] or "",
                "date": r[4] or "",
                "size_bytes": r[5] or 0,
                "size": r[6] or "",
                "album_pinned": bool(r[7]),
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[fts] fts_manage_videos error: {e}")
        return _load_cache()


def fts_manage_videos_page(
    offset: int = 0,
    limit: int = 200,
    search: str = "",
    album_filter: str = "",
    sort_key: str = "date",
    sort_dir: str = "desc",
) -> tuple:
    """Return (rows, total_count) for the /manage page — paginated, filtered, sorted.

    Search uses FTS5 MATCH (BM25-ranked, indexed) instead of LIKE full-table scan.
    All filtering happens inside SQLite — zero Python iteration.
    """
    try:
        conn = _fts_init()
    except Exception as e:
        print(f"[fts] fts_manage_videos_page DB init error: {e}")
        data = _load_cache()
        if search:
            q = search.lower()
            data = [
                v
                for v in data
                if q in (v.get("title", "") or "").lower()
                or q in (v.get("album", "") or "").lower()
            ]
        if album_filter:
            data = [v for v in data if v.get("album", "") == album_filter]
        _SORT_PY = {"date": "date", "title": "title", "size": "size_bytes"}
        sk = _SORT_PY.get(sort_key, "date")
        data = sorted(data, key=lambda v: v.get(sk) or "", reverse=(sort_dir != "asc"))
        total = len(data)
        page = data[offset : offset + limit]
        return (
            [
                {
                    "message_id": v["message_id"],
                    "album": v.get("album", ""),
                    "title": v.get("title", ""),
                    "thumb_url": v.get("thumb_url", "") or "",
                    "date": v.get("date", "") or "",
                    "size_bytes": v.get("size_bytes", 0) or 0,
                    "size": v.get("size", "") or "",
                    "album_pinned": bool(v.get("album_pinned")),
                }
                for v in page
            ],
            total,
        )

    _SORT_MAP = {"date": "date", "title": "title", "size": "size_bytes"}
    col = _SORT_MAP.get(sort_key, "date")
    direction = "ASC" if sort_dir == "asc" else "DESC"

    if search:
        # ── FTS5 path: indexed full-text match, no table scan ─────────────────
        # Build a prefix-match query: each token gets a * so partial words match.
        # e.g. "euphoria sea" → "euphoria* sea*"
        tokens = search.strip().split()
        fts_query = " ".join(t.replace('"', "") + "*" for t in tokens if t)

        # Get matching IDs from FTS (fast), then JOIN to videos_plain for metadata.
        # album_filter applied as an additional WHERE on videos_plain.
        album_where = "AND p.album = ?" if album_filter else ""
        album_params = [album_filter] if album_filter else []

        try:
            total = conn.execute(
                f"""SELECT COUNT(*) FROM videos_fts f
                    JOIN videos_plain p ON p.message_id = CAST(f.message_id AS INTEGER)
                    WHERE videos_fts MATCH ?
                    {album_where}""",
                [fts_query] + album_params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""SELECT p.message_id, p.album, p.title, p.thumb_url,
                           p.date, p.size_bytes, p.size, p.has_override
                    FROM videos_fts f
                    JOIN videos_plain p ON p.message_id = CAST(f.message_id AS INTEGER)
                    WHERE videos_fts MATCH ?
                    {album_where}
                    ORDER BY p.{col} {direction}
                    LIMIT ? OFFSET ?""",
                [fts_query] + album_params + [limit, offset],
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed FTS query (special chars etc.) — fall back to LIKE
            q = search.lower().strip()
            like_where = "(LOWER(p.title) LIKE ? OR LOWER(p.album) LIKE ?)"
            like_params = [f"%{q}%", f"%{q}%"]
            if album_filter:
                like_where += " AND p.album = ?"
                like_params.append(album_filter)
            total = conn.execute(
                f"SELECT COUNT(*) FROM videos_plain p WHERE {like_where}", like_params
            ).fetchone()[0]
            rows = conn.execute(
                f"""SELECT message_id, album, title, thumb_url, date, size_bytes, size, has_override
                    FROM videos_plain p WHERE {like_where}
                    ORDER BY {col} {direction} LIMIT ? OFFSET ?""",
                like_params + [limit, offset],
            ).fetchall()
    else:
        # ── No search term: pure indexed scan on videos_plain ─────────────────
        wheres = []
        params: list = []
        if album_filter:
            wheres.append("album = ?")
            params.append(album_filter)
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM videos_plain {where_sql}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""SELECT message_id, album, title, thumb_url, date, size_bytes, size, has_override
                FROM videos_plain {where_sql}
                ORDER BY {col} {direction}
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()

    return (
        [
            {
                "message_id": r[0],
                "album": r[1],
                "title": r[2],
                "thumb_url": r[3] or "",
                "date": r[4] or "",
                "size_bytes": r[5] or 0,
                "size": r[6] or "",
                "album_pinned": bool(r[7]),
            }
            for r in rows
        ],
        total,
    )


def fts_all_albums_distinct() -> list:
    """Return sorted list of distinct non-empty album names — O(log n)."""
    try:
        conn = _fts_init()
        rows = conn.execute("SELECT album FROM album_names ORDER BY album").fetchall()
        result = [r[0] for r in rows if r[0]]
        if not result:
            data = _load_cache()
            result = sorted({v.get("album", "") for v in data if v.get("album")})
        return result
    except Exception as e:
        print(f"[fts] fts_all_albums_distinct error: {e}")
        try:
            data = _load_cache()
            return sorted({v.get("album", "") for v in data if v.get("album")})
        except Exception:
            return []


# ── UNCATEGORIZED ALBUM COUNT ──────────────────────────────────────────────────
_UNCATEGORIZED_NAMES = {"uncategorised", "uncategorized", ""}


def get_uncategorized_count() -> int:
    """Return count of videos whose album is empty or 'Uncategorised'."""
    try:
        conn = _fts_init()
        rows = conn.execute(
            "SELECT COUNT(*) FROM videos_plain WHERE LOWER(album) IN ('uncategorised', 'uncategorized', '') OR album IS NULL OR album = ''"
        ).fetchone()
        return rows[0] if rows else 0
    except Exception as e:
        print(f"[cache] get_uncategorized_count error: {e}")
        return 0


# ── STREAM HISTORY DATABASE ──────────────────────────────────────────────────
_HISTORY_DB = os.path.join(_here, "stream_history.db")
_history_conn: sqlite3.Connection = None


def _history_init() -> sqlite3.Connection:
    """Initialize the stream history database."""
    global _history_conn
    if _history_conn is not None:
        return _history_conn
    os.makedirs(os.path.dirname(_HISTORY_DB) or ".", exist_ok=True)
    _history_conn = sqlite3.connect(_HISTORY_DB, check_same_thread=False)
    _history_conn.execute("PRAGMA journal_mode=WAL")
    _history_conn.execute("""CREATE TABLE IF NOT EXISTS stream_history(
            message_id INTEGER PRIMARY KEY,
            title TEXT,
            album TEXT,
            thumb_url TEXT,
            duration TEXT,
            quality TEXT,
            size TEXT,
            last_played INTEGER,
            play_count INTEGER DEFAULT 1
        )""")
    _history_conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_history_last ON stream_history(last_played DESC)"
    )
    _history_conn.commit()
    return _history_conn


def history_add(
    message_id: int,
    title: str = "",
    album: str = "",
    thumb_url: str = "",
    duration: str = "",
    quality: str = "",
    size: str = "",
) -> None:
    """Add or update a stream history entry (upsert with play_count increment)."""
    import time as _t

    conn = _history_init()
    now = int(_t.time())
    # Check if entry exists
    existing = conn.execute(
        "SELECT play_count FROM stream_history WHERE message_id = ?", (message_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE stream_history
               SET title = ?, album = ?, thumb_url = ?, duration = ?, quality = ?,
                   size = ?, last_played = ?, play_count = play_count + 1
               WHERE message_id = ?""",
            (title, album, thumb_url, duration, quality, size, now, message_id),
        )
    else:
        conn.execute(
            """INSERT INTO stream_history(message_id, title, album, thumb_url, duration, quality, size, last_played, play_count)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (message_id, title, album, thumb_url, duration, quality, size, now),
        )
    conn.commit()


def history_list(limit: int = 50) -> list:
    """Return history entries sorted by last_played DESC, deduped by message_id."""
    try:
        conn = _history_init()
        rows = conn.execute(
            """SELECT message_id, title, album, thumb_url, duration, quality, size, last_played, play_count
               FROM stream_history
               ORDER BY last_played DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "message_id": r[0],
                "title": r[1] or "",
                "album": r[2] or "",
                "thumb_url": r[3] or "",
                "duration": r[4] or "",
                "quality": r[5] or "",
                "size": r[6] or "",
                "last_played": r[7],
                "play_count": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[history] history_list error: {e}")
        return []


def history_remove(message_id: int) -> bool:
    """Remove a history entry. Returns True if removed."""
    try:
        conn = _history_init()
        cursor = conn.execute(
            "DELETE FROM stream_history WHERE message_id = ?", (message_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        print(f"[history] history_remove error: {e}")
        return False


# ── FTS5 DETAILED SEARCH ────────────────────────────────────────────────────
def fts_search_detailed(query: str, limit: int = 50) -> list:
    """Return full video metadata matching FTS query, ranked by BM25."""
    try:
        conn = _fts_init()
        tokens = query.strip().split()
        fts_query = " ".join(t.replace('"', "") + "*" for t in tokens if t)

        try:
            rows = conn.execute(
                """SELECT p.message_id, p.album, p.title, p.thumb_url, p.date,
                          p.size_bytes, p.size, p.duration, p.quality, p.mime_type, p.caption
                   FROM videos_fts f
                   JOIN videos_plain p ON p.message_id = CAST(f.message_id AS INTEGER)
                   WHERE videos_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Fallback to LIKE for malformed FTS queries
            q = query.lower().strip()
            rows = conn.execute(
                """SELECT message_id, album, title, thumb_url, date,
                          size_bytes, size, duration, quality, mime_type, caption
                   FROM videos_plain
                   WHERE LOWER(title) LIKE ? OR LOWER(album) LIKE ?
                   ORDER BY date DESC LIMIT ?""",
                (f"%{q}%", f"%{q}%", limit),
            ).fetchall()

        return [
            {
                "message_id": r[0],
                "album": r[1] or "",
                "title": r[2] or "",
                "thumb_url": r[3] or "",
                "date": r[4] or "",
                "size_bytes": r[5] or 0,
                "size": r[6] or "",
                "duration": r[7] or "",
                "quality": r[8] or "",
                "mime_type": r[9] or "",
                "caption": r[10] or "",
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[fts] fts_search_detailed error: {e}")
        return []


# ── SIMPLE TTL CACHE FOR API RESPONSES ─────────────────────────────────────
_api_cache: dict = {}  # key -> (value, expiry_timestamp)
_API_CACHE_TTL = {
    "uncategorized_count": 10,
    "albums_list": 5,
    "history": 5,
}


def api_cache_get(key: str):
    """Return cached value if not expired, else None."""
    import time as _t

    entry = _api_cache.get(key)
    if entry is None:
        return None
    value, expiry = entry
    if _t.time() > expiry:
        _api_cache.pop(key, None)
        return None
    return value


def api_cache_set(key: str, value, ttl: float = None) -> None:
    """Store value in API cache with TTL."""
    import time as _t

    ttl = ttl or _API_CACHE_TTL.get(key, 5)
    _api_cache[key] = (value, _t.time() + ttl)


def _xor_bytes(data: bytes) -> bytes:
    """XOR data against a key — used only for JSON cache encryption."""
    key = _thumb_key()
    klen = len(key)
    dlen = len(data)
    tiled = bytearray((key * (dlen // klen + 1))[:dlen])
    src = bytearray(data)
    for i in range(dlen):
        src[i] ^= tiled[i]
    return bytes(src)


_CACHE_KEY_FILE = os.path.join(_here, ".cache_key")
_THUMB_KEY_FILE = os.path.join(_here, ".thumb_key")  # legacy — migration only
_THUMB_KEY: bytes = None


def _thumb_key() -> bytes:
    global _THUMB_KEY
    if _THUMB_KEY is None:
        if os.path.exists(_THUMB_KEY_FILE) and not os.path.exists(_CACHE_KEY_FILE):
            try:
                import shutil as _sh

                _sh.copy2(_THUMB_KEY_FILE, _CACHE_KEY_FILE)
                print("[cache] Migrated .thumb_key → .cache_key")
            except Exception:
                pass
        if os.path.exists(_CACHE_KEY_FILE):
            with open(_CACHE_KEY_FILE, "rb") as f:
                key = f.read()
            if len(key) == 256:
                _THUMB_KEY = key
                return _THUMB_KEY
        if os.path.exists(_THUMB_KEY_FILE):
            with open(_THUMB_KEY_FILE, "rb") as f:
                key = f.read()
            if len(key) == 256:
                _THUMB_KEY = key
                return _THUMB_KEY
        key = os.urandom(256)
        with open(_CACHE_KEY_FILE, "wb") as f:
            f.write(key)
        _THUMB_KEY = key
    return _THUMB_KEY


# ── JSON ENCRYPTION ───────────────────────────────────────────────────────────
def _save_json(path: str, data) -> None:
    """Serialise data to JSON, XOR-encrypt, write to disk."""
    raw = json.dumps(data).encode()
    with open(path, "wb") as f:
        f.write(_xor_bytes(raw))


def _load_json(path: str):
    """Read XOR-encrypted JSON from disk and return parsed object."""
    with open(path, "rb") as f:
        enc = f.read()
    return json.loads(_xor_bytes(enc))


# ── MANUAL ALBUM OVERRIDES ────────────────────────────────────────────────────
def _load_album_overrides() -> dict:
    if not os.path.exists(ALBUMS_FILE):
        return {}
    try:
        return _load_json(ALBUMS_FILE)
    except Exception:
        try:
            with open(ALBUMS_FILE) as f:
                data = json.load(f)
            _save_json(ALBUMS_FILE, data)
            print("[cache] Migrated tg_albums.json → encrypted")
            return data
        except Exception:
            return {}


def _save_album_overrides(overrides: dict):
    _save_json(ALBUMS_FILE, overrides)


# ── OMDB / TVMaze METADATA CACHE ─────────────────────────────────────────────
_imdb_cache_mem: dict = {}


def _load_imdb_cache() -> dict:
    global _imdb_cache_mem
    if _imdb_cache_mem:
        return _imdb_cache_mem
    if not os.path.exists(IMDB_CACHE_FILE):
        _imdb_cache_mem = {}
        return {}
    try:
        with open(IMDB_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        _imdb_cache_mem = {k: v for k, v in raw.items() if isinstance(v, dict)}
        if len(_imdb_cache_mem) < len(raw):
            print(
                f"[imdb_cache] Dropped {len(raw) - len(_imdb_cache_mem)} non-dict entries"
            )
    except Exception:
        _imdb_cache_mem = {}
    return _imdb_cache_mem


def _save_imdb_cache():
    try:
        with open(IMDB_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_imdb_cache_mem, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[imdb_cache] save error: {e}")


# ── TMDB-POWERED IMDB GAP FILL ──────────────────────────────────────────────
def _fill_imdb_gaps_sync(album_names: list | None = None) -> dict:
    """Fill missing IMDB metadata using TMDB-first search (runs in executor).

    If album_names is None, scans all albums for gaps.
    Returns {filled: int, skipped: int, errors: int, details: [...]}.
    """
    import config as _cfg
    from imdb_search import fill_imdb_gaps, search_imdb_for_album

    tmdb_key = getattr(_cfg, "TMDB_API_KEY", "") or "10822052cd7c36868f387e3f713ad713"
    _load_imdb_cache()
    _load_cache()

    # Build list of album names to check
    if album_names:
        names = album_names
    else:
        # All distinct album names from cache
        names = sorted({v.get("album", "") for v in _cache_mem if v.get("album")})

    filled = 0
    skipped = 0
    errors = 0
    details = []

    for name in names:
        if not name or not name.strip():
            continue
        key = name.strip().lower()
        existing = _imdb_cache_mem.get(key, {})

        # Check if there are gaps to fill
        has_gap = False
        for field in ("poster", "year", "rating", "type", "plot"):
            val = existing.get(field, "")
            if not val or val == "N/A":
                has_gap = True
                break

        if not has_gap and existing:
            skipped += 1
            continue

        try:
            if existing:
                updated = fill_imdb_gaps(name, existing, tmdb_api_key=tmdb_key)
            else:
                updated = search_imdb_for_album(name, tmdb_api_key=tmdb_key)

            if updated and any(
                updated.get(f) for f in ("poster", "year", "rating", "plot")
            ):
                # Ensure core fields exist
                for field in ("poster", "year", "rating", "type", "plot", "source"):
                    if field not in updated:
                        updated[field] = ""
                _imdb_cache_mem[key] = updated
                filled += 1
                details.append(
                    {
                        "album": name,
                        "status": "filled",
                        "source": updated.get("source", ""),
                    }
                )
            else:
                skipped += 1
                details.append({"album": name, "status": "no_result"})
        except Exception as e:
            errors += 1
            details.append({"album": name, "status": "error", "error": str(e)})
            print(f"[imdb_gap] error for {name!r}: {e}")

    if filled > 0:
        _save_imdb_cache()

    return {"filled": filled, "skipped": skipped, "errors": errors, "details": details}


async def _fetch_media_meta(session, album_name: str) -> dict:
    """Search TMDB→IMDb GraphQL→TVMaze via imdb_search.py. Returns {poster, year, rating, type, plot, ...}."""
    key = album_name.strip().lower()
    cache = _load_imdb_cache()
    if key in cache:
        return cache[key]

    import config as _cfg
    from imdb_search import search_imdb_for_album

    tmdb_key = getattr(_cfg, "TMDB_API_KEY", "") or "10822052cd7c36868f387e3f713ad713"
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, lambda: search_imdb_for_album(album_name, tmdb_api_key=tmdb_key)
        )
    except Exception as e:
        print(f"[imdb_search] error for {album_name!r}: {e}")
        result = {}

    if not isinstance(result, dict):
        result = {}

    # Sanitize: never store relative/local URLs as poster
    if result.get("poster", "").startswith("/"):
        result["poster"] = ""

    _imdb_cache_mem[key] = result
    _save_imdb_cache()
    return result


async def _fetch_all_meta(album_names: list) -> dict:
    """Concurrently fetch TMDB/IMDb/TVMaze metadata for all album names via imdb_search."""
    _load_imdb_cache()

    results = {}
    to_fetch = [n for n in album_names if n.strip().lower() not in _imdb_cache_mem]
    cached = {n: _imdb_cache_mem.get(n.strip().lower(), {}) for n in album_names}

    if to_fetch:
        # _fetch_media_meta is async and uses run_in_executor internally;
        # session param is unused by the new implementation — pass None.
        tasks = {n: _fetch_media_meta(None, n) for n in to_fetch}
        fetched = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks.keys(), fetched):
            if isinstance(res, Exception):
                print(f"[meta] fetch failed for {name!r}: {res}")
                results[name] = {}
            else:
                results[name] = res

    for name in album_names:
        if name not in results:
            v = cached.get(name, {})
            results[name] = v if isinstance(v, dict) else {}
    return results


# ── RESUME POSITIONS ──────────────────────────────────────────────────────────
_RESUME_FILE = os.path.join(_here, "resume_positions.json")
_resume_mem: dict = {}


def _load_resume() -> dict:
    global _resume_mem
    if _resume_mem:
        return _resume_mem
    if not os.path.exists(_RESUME_FILE):
        _resume_mem = {}
        return {}
    try:
        with open(_RESUME_FILE, "r", encoding="utf-8") as f:
            _resume_mem = json.load(f)
    except Exception:
        _resume_mem = {}
    return _resume_mem


def _save_resume() -> None:
    try:
        with open(_RESUME_FILE, "w", encoding="utf-8") as f:
            json.dump(_resume_mem, f)
    except Exception as e:
        print(f"[resume] save error: {e}")


def resume_get(msg_id: int) -> float:
    """Return saved playback position in seconds for msg_id, or 0.0."""
    _load_resume()
    entry = _resume_mem.get(str(msg_id), {})
    if not isinstance(entry, dict):
        return 0.0
    pos = float(entry.get("pos", 0) or 0)
    dur = float(entry.get("dur", 0) or 0)
    if dur > 0 and pos >= dur - 30:
        return 0.0
    return pos


def resume_set(msg_id: int, pos: float, dur: float = 0.0) -> None:
    """Persist playback position for msg_id."""
    _load_resume()
    _resume_mem[str(msg_id)] = {"pos": round(pos, 1), "dur": round(dur, 1)}
    _save_resume()


# ── IN-MEMORY CACHE ───────────────────────────────────────────────────────────
_cache_mem: list = []
_cache_meta: dict = {}
_albums_index: list = []
_albums_dirty: bool = True
_poster_mem: dict = {}
_index_html_cache: str = None


def _rebuild_albums_index():
    global _albums_index, _albums_dirty
    from helpers import album_key as _ak

    buckets: dict = {}
    for v in _cache_mem:
        a = v["album"]
        k = _ak(a)
        if k not in buckets:
            buckets[k] = {"names": {}, "videos": []}
        buckets[k]["names"][a] = buckets[k]["names"].get(a, 0) + 1
        buckets[k]["videos"].append(v)
    result = []
    for bucket in buckets.values():
        canonical = max(
            bucket["names"],
            key=lambda n: (
                bucket["names"][n],
                sum(1 for t in n.split() if t == t.upper()),
            ),
        )
        result.append(
            {
                "name": canonical,
                "videos": bucket["videos"],
            }
        )
    _albums_index = sorted(result, key=lambda x: x["name"].lower())
    _albums_dirty = False


def _load_cache():
    global _cache_mem, _cache_meta
    if _cache_mem:
        return _cache_mem
    if not os.path.exists(CACHE_FILE):
        _cache_mem, _cache_meta = [], {}
        return []
    try:
        data = _load_json(CACHE_FILE)
    except Exception as _dec_err:
        print(
            f"[cache] WARN: XOR decrypt failed ({_dec_err}) — trying plain JSON fallback"
        )
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            _save_json(CACHE_FILE, data)
            print("[cache] Migrated tg_cache.json → encrypted")
        except Exception as _plain_err:
            print(f"[cache] ERROR: plain JSON also failed ({_plain_err})")
            print(
                "[cache] Cache unreadable with current key — delete tg_cache.json to re-fetch"
            )
            _cache_mem, _cache_meta = [], {}
            return []
    overrides = _load_album_overrides()
    for v in data:
        caption = v.get("caption", "")
        filename = v.get("filename", "")
        parsed = _parse_caption(caption)
        mid = str(v.get("message_id", ""))
        if mid in overrides:
            v["album"] = _canonicalize_album(overrides[mid])
            v["album_pinned"] = True
        else:
            if v.get("album_pinned") or v.get("album"):
                # User-managed or previously auto-derived: only fix apostrophes,
                # never re-derive (would clobber manual album assignments).
                v["album"] = _canonicalize_album(v.get("album") or "")
            else:
                v["album"] = derive_album(caption, filename)
            v["album_pinned"] = bool(v.get("album_pinned"))
        v["title"] = _sanitize(
            re.sub(
                r"[\._]+", " ", os.path.splitext(parsed["filename"] or filename)[0]
            ).strip()
        )
        v["album"] = _sanitize(v.get("album") or "")
        v["caption"] = _sanitize(v.get("caption") or "")
        v["filename"] = _sanitize(v.get("filename") or "")
        v["quality"] = v.get("quality") or parsed["quality"] or ""
        v["duration"] = v.get("duration") or parsed["duration"] or ""
        v["size"] = v.get("size") or parsed["size"] or ""
    _cache_mem = data
    _cache_meta = {v["message_id"]: v for v in data}
    global _albums_dirty
    _albums_dirty = True
    # Skip FTS rebuild if the DB already has rows — it persists on disk between
    # runs so a full DELETE+reinsert of 23k rows on every startup is wasted work.
    # _fts_sync is still called after _save_cache (writes) to keep it in sync.
    try:
        conn = _fts_init()
        row = conn.execute("SELECT COUNT(*) FROM videos_plain").fetchone()
        if not row or row[0] == 0:
            _fts_sync(data)
        else:
            print(f"[cache] FTS DB has {row[0]} rows — skipping rebuild on load")
    except Exception:
        _fts_sync(data)
    return data


def _meta(msg_id: int) -> dict:
    """O(1) lookup of a single video's metadata — no disk I/O."""
    if msg_id in _cache_meta:
        return _cache_meta[msg_id]
    _load_cache()
    return _cache_meta.get(msg_id, {})


def _save_cache(data):
    global _cache_mem, _cache_meta, _albums_dirty, _poster_mem, _index_html_cache
    _save_json(CACHE_FILE, data)
    _cache_mem = data
    _cache_meta = {v["message_id"]: v for v in data}
    _albums_dirty = True
    _poster_mem = {}
    _index_html_cache = None
    _fts_sync(data)


# ── Deleted-album blacklist ───────────────────────────────────────────────────
_deleted_albums_cache: set = set()


def _load_deleted_albums() -> set:
    global _deleted_albums_cache
    if _deleted_albums_cache:
        return _deleted_albums_cache
    try:
        with open(DELETED_ALBUMS_FILE, "r", encoding="utf-8") as f:
            _deleted_albums_cache = set(json.load(f))
    except (FileNotFoundError, Exception):
        _deleted_albums_cache = set()
    return _deleted_albums_cache


def add_deleted_album(album_name: str):
    """Persist album_name to the blacklist so fetch never re-adds its videos."""
    global _deleted_albums_cache
    key = album_name.strip().lower()
    bl = _load_deleted_albums()
    bl.add(key)
    _deleted_albums_cache = bl
    try:
        with open(DELETED_ALBUMS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(bl), f)
    except Exception as e:
        print(f"[blacklist] write error: {e}")


def is_album_deleted(album_name: str) -> bool:
    return album_name.strip().lower() in _load_deleted_albums()


async def get_videos(force=False):
    if not force and _cache_mem:
        return _cache_mem
    if not force and os.path.exists(CACHE_FILE):
        # Fast path: load from disk in executor (XOR decrypt + 23k entry loop
        # can take 5–30s on large libraries — must not block the event loop).
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, _load_cache)
        if data:
            asyncio.create_task(_background_incremental_sync())
            return data
    try:
        data = await _fetch_incremental_merge()
        return data
    except Exception as e:
        print(f"[TG] Fetch error: {e}")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _load_cache)


async def _background_incremental_sync():
    """Silently check for new messages and update cache without blocking."""
    try:
        await asyncio.sleep(2)  # let server fully start first
        await _fetch_incremental_merge()
    except Exception as e:
        print(f"[sync] background incremental sync error: {e}")


def group_by_album(videos):
    from helpers import album_key as _ak

    # First pass: bucket by normalised key, collect all name variants
    buckets: dict = {}
    for v in videos:
        a = v["album"]
        k = _ak(a)
        if k not in buckets:
            buckets[k] = {"names": {}, "videos": []}
        buckets[k]["names"][a] = buckets[k]["names"].get(a, 0) + 1
        buckets[k]["videos"].append(v)
    # Pick canonical name: prefer all-uppercase single tokens (X > x), else most frequent
    result = []
    for bucket in buckets.values():
        # most-frequent name; ties broken by preferring uppercase tokens
        canonical = max(
            bucket["names"],
            key=lambda n: (
                bucket["names"][n],
                sum(1 for t in n.split() if t == t.upper()),
            ),
        )
        result.append(
            {
                "name": canonical,
                "videos": bucket["videos"],
            }
        )
    return sorted(result, key=lambda x: x["name"].lower())


# ── FETCH ALL FROM TELEGRAM ───────────────────────────────────────────────────


def _msg_to_video(msg) -> dict:
    """Convert a Telegram message to a video metadata dict."""
    from streaming import _needs_ffmpeg

    tg_filename, mime, w, h, tg_dur = get_doc_attrs(msg)
    size_bytes = get_size(msg)
    caption = (msg.message or "").strip()
    parsed = _parse_caption(caption)
    filename = parsed["filename"] or tg_filename
    title = re.sub(r"[\._]+", " ", os.path.splitext(filename)[0]).strip()
    quality = _quality_from_dims(w, h) or parsed["quality"] or ""
    duration_s = tg_dur or 0
    return {
        "message_id": msg.id,
        "filename": filename,
        "caption": caption,
        "album": derive_album(caption, filename),
        "title": title,
        "quality": quality,
        "size": parsed["size"] or _fmt_size(size_bytes),
        "size_bytes": size_bytes,
        "duration": parsed["duration"] or _fmt_dur(duration_s),
        "thumb_url": "",
        "date": msg.date.isoformat() if msg.date else "",
        "mime_type": mime,
        "_duration_s": float(duration_s) if duration_s else 0.0,
        "_needs_hls": _needs_ffmpeg(_safe_mime(mime), filename, ""),
    }


def _dedup(videos: list) -> list:
    seen: set = set()
    out = []
    for v in videos:
        key = (v["caption"], v["size_bytes"])
        if key in seen:
            print(
                f"[dedup] skipping duplicate msg={v['message_id']} size={v['size_bytes']}"
            )
            continue
        seen.add(key)
        out.append(v)
    if len(out) < len(videos):
        print(f"[dedup] removed {len(videos) - len(out)} duplicate(s)")
    return out


async def _fetch_all(incremental: bool = True):
    """Fetch video messages from Telegram.

    incremental=True  → only fetch messages newer than last_seen_id (fast).
    incremental=False → full rescan (used on manual refresh/force).
    """
    from streaming import _get_entity

    ent = await _get_entity()
    state = _load_fetch_state()
    last_id = state.get("last_id", 0) if incremental else 0

    # ── Collect new messages only ─────────────────────────────────────────────
    new_msgs = []
    max_id_seen = last_id
    _batch_count = 0
    async for msg in _cfg.client.iter_messages(ent, reverse=True, min_id=last_id):
        if is_video(msg):
            new_msgs.append(msg)
        if msg.id > max_id_seen:
            max_id_seen = msg.id
        _batch_count += 1
        if _batch_count % 200 == 0:
            # Yield control every 200 messages so aiohttp can serve requests
            # during the full-channel scan (critical for 23k+ libraries).
            await asyncio.sleep(0)

    if not new_msgs and incremental and last_id > 0:
        print(f"[fetch] No new messages since id={last_id} — using cache")
        _save_fetch_state({"last_id": max_id_seen})
        return None  # Signal: nothing changed

    print(
        f"[fetch] {'Incremental' if incremental and last_id else 'Full'}: {len(new_msgs)} new video(s)"
    )

    new_videos = [_msg_to_video(m) for m in new_msgs]
    new_videos = _dedup(new_videos)

    _save_fetch_state({"last_id": max_id_seen})
    return new_videos


async def _fetch_incremental_merge():
    """Load cache, fetch only new messages, merge, save. Returns merged list."""
    existing = _load_cache()  # fast — from disk or memory
    existing_ids = {v["message_id"] for v in existing}

    new_videos = await _fetch_all(incremental=True)

    if new_videos is None:
        # Nothing new
        return existing

    bl = _load_deleted_albums()
    truly_new = [
        v
        for v in new_videos
        if v["message_id"] not in existing_ids
        and v.get("album", "").strip().lower() not in bl
    ]
    if not truly_new:
        print("[fetch] All fetched IDs already in cache")
        return existing

    merged = existing + truly_new
    # Sort by message_id descending (newest first) to match channel order
    merged.sort(key=lambda v: v["message_id"], reverse=True)

    # Persist full cache to disk and update in-memory state
    _save_json(CACHE_FILE, merged)
    global _cache_mem, _cache_meta, _albums_dirty, _poster_mem, _index_html_cache
    _cache_mem = merged
    _cache_meta = {v["message_id"]: v for v in merged}
    _albums_dirty = True
    _poster_mem = {}
    _index_html_cache = None

    # Fast-path FTS update: upsert only the new rows instead of wiping+rebuilding
    _fts_upsert(truly_new)

    print(
        f"[fetch] Merged {len(truly_new)} new video(s) into cache ({len(merged)} total)"
    )
    return merged

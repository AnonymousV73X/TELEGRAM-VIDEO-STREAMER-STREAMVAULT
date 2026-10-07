"""subtitles.py — StreamVault subtitle discovery, indexing, and caching.

Architecture
------------
The core problem: subtitle SRT/VTT files in Telegram channels can be uploaded
thousands of messages away from the matching video — before OR after. A naive
sliding-window scan (±N messages) will always miss them.

Solution: a PERSISTENT subtitle index backed by SQLite.

  subtitle_index.db
  └── table: subtitle_files
      ├── msg_id      INTEGER PRIMARY KEY  — Telegram message ID
      ├── filename    TEXT                 — e.g. "Dragon.S02E04.srt"
      ├── season      INTEGER              — extracted season number (or NULL)
      ├── episode     INTEGER              — extracted episode number (or NULL)
      ├── ext         TEXT                 — srt / vtt / ass / sub / zip
      └── indexed_at  REAL                 — unix timestamp of index run

On every app launch / channel refresh this index is updated incrementally
(only new messages since last scan). When a subtitle is needed for a video,
we query the index for the best-scoring candidate across the ENTIRE channel
history, then download + cache it on demand.
"""

import os
import asyncio
import re
import sqlite3
import time
import uuid
import hashlib
import zipfile
import tarfile
import shutil
import subprocess
import sys
import tempfile
import io
import difflib
import aiohttp
import config as _cfg
from telethon.tl.types import MessageMediaDocument, DocumentAttributeFilename

# ── Paths & Safe File Handling ────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
SUB_DIR = os.path.join(_here, ".cache", "subtitles")
OPTIONS_DIR = os.path.join(SUB_DIR, "options")
os.makedirs(SUB_DIR, exist_ok=True)
os.makedirs(OPTIONS_DIR, exist_ok=True)

# In-memory search results cache: (msg_id, query_title, lang) -> (timestamp, data_dict)
_SUB_SEARCH_CACHE: dict[tuple, tuple[float, dict]] = {}
_SEARCH_CACHE_TTL = 900  # 15 minutes TTL

def _get_option_cache_path(source: str, sub_id: str | int, url: str, filename: str) -> str:
    """Generate a persistent content-addressed filepath for downloaded subtitle options."""
    raw = f"{source}_{sub_id}_{url}_{filename}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    clean_fn = re.sub(r"[^\w\-.]", "_", os.path.basename(filename))[:40]
    return os.path.join(OPTIONS_DIR, f"{source}_{h}_{clean_fn}.srt")

def _make_temp_sub_path(msg_id: int) -> str:
    """Generate a unique temporary filepath to avoid file locking collisions between concurrent tasks."""
    return os.path.join(SUB_DIR, f"{msg_id}_{uuid.uuid4().hex[:8]}.tmp")

def _safe_remove(path: str | None):
    """Safely remove a temporary file without raising errors on Windows locks."""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def _safe_save_subtitle(msg_id: int, content: str | bytes) -> str:
    """Save subtitle content to cache safely, handling Windows file locks gracefully."""
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    is_bytes = isinstance(content, (bytes, bytearray))
    mode = "wb" if is_bytes else "w"
    kwargs = {} if is_bytes else {"encoding": "utf-8"}

    # 1. Try direct write to standard cache_path (with retry if player temporarily locks it)
    for attempt in range(3):
        try:
            with open(cache_path, mode, **kwargs) as f:
                f.write(content)
            return cache_path
        except (PermissionError, OSError):
            time.sleep(0.1)

    # 2. Fallback: if standard cache_path is locked by active player (VLC / Windows file lock),
    # write to a unique versioned file and return it.
    versioned_path = os.path.join(SUB_DIR, f"{msg_id}_{int(time.time() * 1000)}_en.srt")
    try:
        with open(versioned_path, mode, **kwargs) as f:
            f.write(content)
        return versioned_path
    except Exception as e:
        print(f"[subtitles] Failed to write fallback subtitle: {e}")
        return cache_path

_INDEX_DB = os.path.join(_here, ".cache", "subtitle_index.db")
_SUBTITLE_EXTS = {".srt", ".vtt", ".ass", ".sub", ".zip", ".rar", ".7z", ".tar", ".gz"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}

# ── Noise words to strip during fuzzy matching ─────────────────────────────────
_NOISE_WORDS = {
    "1080p", "720p", "480p", "360p", "2160p", "4k", "uhd", "hdr", "hdr10",
    "bluray", "bdrip", "brrip", "webrip", "webdl", "web-dl", "hdrip", "hdtv",
    "dvdrip", "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "10bit",
    "aac", "ac3", "eac3", "dts", "flac", "mp3", "opus", "ddp5", "5.1", "7.1",
    "repack", "proper", "unrated", "extended", "remastered", "theatrical",
    "yify", "yts", "psa", "eztv", "rarbg", "galaxytv", "tgx", "ettv",
    "eng", "english", "sdh", "sub", "subs", "subtitle", "subtitles",
    "srt", "vtt", "ass", "mp4", "mkv", "avi", "webm", "mov",
    # Season/series descriptors that shouldn't count as show title keywords
    "season", "episode", "series", "complete", "collection",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    "1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "10th",
}


# ── Subtitle Index DB ──────────────────────────────────────────────────────────

def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_INDEX_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subtitle_files (
            msg_id      INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            season      INTEGER,
            episode     INTEGER,
            ext         TEXT,
            indexed_at  REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_subtitles (
            msg_id      INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            source      TEXT NOT NULL,
            applied_at  REAL DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def _get_last_indexed_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM index_state WHERE key='last_id'").fetchone()
    return int(row[0]) if row else 0


def _set_last_indexed_id(conn: sqlite3.Connection, msg_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO index_state(key,value) VALUES('last_id',?)",
        (str(msg_id),),
    )
    conn.commit()


# ── Season / Episode extraction ────────────────────────────────────────────────

def extract_season_episode(text: str) -> tuple[int | None, int | None]:
    """Extract Season and Episode numbers from a string or file path.
    Returns (season, episode) or (None, None).
    Handles:
      - S02E04, S2 E4, s01e07, 2x04, 01x07
      - Season 2 Episode 4, Season 1/07.srt, S02/04.srt
      - Standalone episode tags: Ep 4, Episode 07, E07, Part 4, Pt 4
      - 3/4-digit codes: 107 (S1E07), 215 (S2E15), H2O_107.srt
      - Leading/bracketed episode numbers: 07.srt, 07 - Title.srt, [07], (07)
      - Trailing episode numbers: Title - 07.srt
    """
    if not text:
        return None, None

    # Replace path separators so directories count as context (e.g. "Season 1/07.srt")
    path_norm = text.replace("\\", "/").strip()

    # 0. Ordinal season words: "first-season", "second season", "season one", etc.
    _ORDINALS_MAP = {
        "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
        "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
        "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
        "tenth": 10, "10th": 10,
    }
    norm_ord = path_norm.lower().replace("-", " ").replace("_", " ")
    for word, num in _ORDINALS_MAP.items():
        if f"{word} season" in norm_ord or f"season {word}" in norm_ord:
            return num, None

    # 1. Season folder + filename: "Season 1/07.srt" or "S02/04.srt"
    m = re.search(r"(?i)(?:Season|S)\s*(\d{1,2})[/\-_ ]+.*?(\d{1,3})(?:\.\w+)?$", path_norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Normalize delimiters for word boundary matching
    norm = re.sub(r"[._\-+\[\]()]", " ", path_norm)
    # Strip codec/tech noise that produces false episode matches (e.g. x264→E264, H.264→E264)
    # Must handle both "x264" (no space) and "H 264" (space after delimiter normalization)
    norm = re.sub(r"(?i)\b[hx]\s*26[0-9]\b", "", norm)   # x264, x265, h264, h265, H 264, H 265
    norm = re.sub(r"(?i)\b(xvid|divx|hevc|avc|avc1|av1|vp9|hdr|dts|dd\s*\d|ddp\s*\d|atmos|truehd|flac|aac|mp3|ac3|eac3)\b", "", norm)
    norm = re.sub(r"\b\d+p\b", "", norm)  # 720p 1080p 2160p etc.

    # 2. Standard S01E04, S1 E4, S01  E04
    m = re.search(r"(?i)\bS(\d{1,2})\s*E(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 3. 1x04, 01x04
    m = re.search(r"(?i)\b(\d{1,2})\s*x\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 4. Season 1 Episode 4 / Season 01 Ep 4
    m = re.search(r"(?i)\bSeason\s*(\d{1,2})\s*.*?Ep(?:isode)?\s*(\d{1,3})\b", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 5. Season only with trailing episode: "Season 1 ... 07"
    m = re.search(r"(?i)\bSeason\s*(\d{1,2})\b.*?(\d{1,3})\s*$", norm)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 6. Standalone: Episode 4 / Ep. 4 / E04
    m = re.search(r"(?i)\b(?:Ep(?:isode)?|E)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    # 7. Part 4 / Pt 4
    m = re.search(r"(?i)\b(?:Part|Pt)\s*(\d{1,3})\b", norm)
    if m:
        return None, int(m.group(1))

    # 8. 3-digit season/episode: e.g. "107" -> Season 1, Episode 7; "214" -> Season 2, Episode 14
    m = re.search(r"\b([1-9])(\d{2})\b", norm)
    if m:
        num = int(m.group(0))
        if num not in {720, 480, 576, 360, 240, 108, 264, 265} and not (1900 <= num <= 2099):
            return int(m.group(1)), int(m.group(2))

    # 9. Bracketed or isolated episode number: "[07]", "(07)", "^07.srt", "07 - Title.srt"
    base = os.path.basename(path_norm)
    base_no_ext = os.path.splitext(base)[0].strip()
    m = re.search(r"^(?:\[|\()?(\d{1,3})(?:\]|\))?(?:\s*[-_ ]|\s*$)", base_no_ext)
    if m:
        return None, int(m.group(1))

    # 10. Trailing episode number: e.g. "Show Title 07.srt", "Show - 07.srt"
    m = re.search(r"(?:^|[-_ ])0*(\d{1,3})\s*$", base_no_ext)
    if m:
        return None, int(m.group(1))

    return None, None



# ── Text tokenization ──────────────────────────────────────────────────────────

def tokenize_clean(text: str) -> set[str]:
    """Break text into significant keyword tokens, stripping release noise."""
    if not text:
        return set()
    cleaned = re.sub(r"[._\-\[\](){}+,;:!?'\"/\\]", " ", text.lower())
    return {w for w in cleaned.split() if len(w) >= 2 and w not in _NOISE_WORDS}


# ── Fuzzy match scoring ────────────────────────────────────────────────────────

def _get_show_title_keywords(title: str, filename: str) -> set[str]:
    """Extract only the show title tokens (strip SxxExx, Season x, etc.)."""
    combined = f"{title} {filename}"
    # Trim everything from S01E01 / Season 1 / 1x04 onwards
    cleaned = re.sub(r"(?i)\b(S\d+E\d+|S\d+|Season\s*\d+|E\d+|Ep\s*\d+|\d+x\d+)\b.*", "", combined)
    toks = tokenize_clean(cleaned)
    if not toks:
        toks = tokenize_clean(combined)  # fallback if nothing survived
    return toks


def calculate_match_score(
    video_title: str, video_filename: str, sub_filename: str, distance: int = 0
) -> float:
    """Score a subtitle candidate against a video (0 = worst, higher = better).

    Returns -1000 if there is a definitive mismatch (wrong episode number or
    completely different show title), signalling the caller to reject this candidate.
    """
    v_combined = f"{video_title} {video_filename}".lower()
    s_clean = sub_filename.lower()
    ext = os.path.splitext(s_clean)[1].lower()
    is_archive = ext in _ARCHIVE_EXTS

    v_tok = tokenize_clean(v_combined)
    s_tok = tokenize_clean(s_clean)
    show_toks = _get_show_title_keywords(video_title, video_filename)

    show_overlap = show_toks & s_tok
    total_overlap = v_tok & s_tok

    # Substring check for core show name (handles hyphens/dots in archive names)
    norm_t = re.sub(r"[^\w\s]", " ", video_title.lower())
    clean_show = re.sub(r"(?i)\b(s\d+e\d+|s\d+|season\s*\d+|e\d+|ep\s*\d+|\d+x\d+)\b.*", "", norm_t).strip()
    clean_show_nospace = re.sub(r"\s+", "", clean_show)
    sub_nospace = re.sub(r"[^\w]", "", s_clean)

    # Allow partial show match if title tokens overlap significantly
    has_title_match = (
        bool(show_overlap)
        or (len(clean_show_nospace) >= 3 and (
            clean_show_nospace in sub_nospace or sub_nospace.startswith(clean_show_nospace)
        ))
    )

    # If candidate shares NO show-title tokens -> completely different show -> reject
    if not has_title_match:
        return -1000.0

    # 1. Season / Episode agreement
    v_s, v_e = extract_season_episode(v_combined)
    s_s, s_e = extract_season_episode(s_clean)
    score = 0.0

    if v_e is not None:
        if s_e is not None:
            if v_e != s_e:
                return -1000.0          # Wrong episode — hard reject
            score += 70.0               # Exact episode match
            if v_s is not None and s_s is not None:
                if v_s != s_s:
                    return -1000.0      # Wrong season — hard reject
                score += 50.0           # Exact season + episode match
        elif is_archive:
            # It's a season/series pack archive — great candidate for extraction
            if v_s is not None and s_s is not None:
                if v_s != s_s:
                    return -1000.0      # Wrong season pack
                score += 80.0           # Correct season pack — very high reward
            else:
                score += 60.0           # Complete series pack (season unknown in sub name)
        else:
            score -= 10.0               # No ep tag in sub name (non-archive)
    elif s_e is not None:
        score -= 15.0                   # Sub has episode tag, video doesn't

    # 2. Keyword / token overlap (Jaccard + title-subset ratio)
    if v_tok and s_tok:
        jaccard = len(total_overlap) / max(len(v_tok | s_tok), 1)
        score += jaccard * 40.0
        score += (len(show_overlap) / max(len(show_toks), 1)) * 40.0

    # 3. String similarity (SequenceMatcher)
    sim = difflib.SequenceMatcher(
        None,
        " ".join(sorted(v_tok)),
        " ".join(sorted(s_tok)),
    ).ratio()
    score += sim * 25.0

    # 4. Proximity tie-breaker (max +10 for adjacent messages)
    score += max(0.0, 10.0 - distance * 0.001)

    return score


# ── Background subtitle index builder ─────────────────────────────────────────

async def build_subtitle_index(incremental: bool = True) -> int:
    """Scan the Telegram channel for subtitle files and persist them in the local DB.

    This is the key function that eliminates the "±N window" problem.
    It runs a full or incremental pass over the channel — just like _fetch_all()
    does for videos — and stores every subtitle it finds in subtitle_index.db.

    Args:
        incremental: If True, only scan messages newer than the last scan.
                     If False, do a full channel rescan (e.g. after /refresh).

    Returns:
        Number of new subtitle entries added this run.
    """
    from streaming import _get_entity

    client = _cfg.client
    if not client:
        return 0

    ent = await _get_entity()
    if not ent:
        return 0

    conn = _db_connect()
    last_id = _get_last_indexed_id(conn) if incremental else 0

    new_count = 0
    max_id_seen = last_id
    batch = 0

    print(f"[subtitle_index] Scanning from msg_id={last_id} (incremental={incremental})")

    async for msg in client.iter_messages(ent, reverse=True, min_id=last_id):
        if msg.id > max_id_seen:
            max_id_seen = msg.id

        batch += 1
        if batch % 500 == 0:
            await asyncio.sleep(0)      # Yield to aiohttp every 500 msgs

        if not msg.media or not isinstance(msg.media, MessageMediaDocument):
            continue

        doc_name = ""
        for attr in msg.media.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                doc_name = attr.file_name
                break

        if not doc_name:
            continue

        ext = os.path.splitext(doc_name)[1].lower()
        if ext not in _SUBTITLE_EXTS:
            continue

        season, episode = extract_season_episode(doc_name)

        # Upsert into the index
        conn.execute(
            """INSERT OR REPLACE INTO subtitle_files
               (msg_id, filename, season, episode, ext, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (msg.id, doc_name, season, episode, ext, time.time()),
        )
        new_count += 1

    if new_count:
        conn.commit()
    
    _set_last_indexed_id(conn, max_id_seen)
    conn.close()

    print(f"[subtitle_index] Done. {new_count} subtitle(s) indexed. max_id={max_id_seen}")
    return new_count


def query_subtitle_index(
    video_title: str,
    video_filename: str,
    season: int | None = None,
    episode: int | None = None,
) -> list[tuple[int, str, float]]:
    """Query the local subtitle index for candidates matching a video.

    Uses season/episode pre-filtering when available (O(log n) index scan),
    then fuzzy-scores the remaining candidates and returns them sorted by score.
    Archive files (.zip/.rar etc.) are *always* included as separate candidates
    because they may contain season/series packs that cover the requested episode.

    Returns:
        List of (msg_id, filename, score) tuples, best match first.
    """
    conn = _db_connect()

    try:
        if season is not None and episode is not None:
            # Fast path: exact season + episode
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE season=? AND episode=?",
                (season, episode),
            ).fetchall()
            if not rows:
                # Retry with episode only (some subs omit season number)
                rows = conn.execute(
                    "SELECT msg_id, filename FROM subtitle_files WHERE episode=? AND season IS NULL",
                    (episode,),
                ).fetchall()
            # Always add archives — they may be season packs containing this episode
            archive_rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE ext IN ('.zip','.rar','.7z','.tar','.gz')"
            ).fetchall()
            # Merge without duplicates
            seen = {r[0] for r in rows}
            rows = list(rows) + [r for r in archive_rows if r[0] not in seen]

        elif episode is not None:
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE episode=?",
                (episode,),
            ).fetchall()
            archive_rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files WHERE ext IN ('.zip','.rar','.7z','.tar','.gz')"
            ).fetchall()
            seen = {r[0] for r in rows}
            rows = list(rows) + [r for r in archive_rows if r[0] not in seen]
        else:
            # No episode info — fetch all and rely purely on fuzzy scoring
            rows = conn.execute(
                "SELECT msg_id, filename FROM subtitle_files"
            ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    scored: list[tuple[int, str, float]] = []
    for msg_id, filename in rows:
        sc = calculate_match_score(video_title, video_filename, filename)
        if sc > 15.0:
            scored.append((msg_id, filename, sc))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored



# ── Subdl online search + download ────────────────────────────────────────────

_SUBDL_SEARCH_URL = "https://api.subdl.com/api/v1/subtitles"
_SUBDL_DL_BASE    = "https://dl.subdl.com"

async def search_subdl(
    title: str,
    season: int | None = None,
    episode: int | None = None,
    imdb_id: str | None = None,
    tmdb_id: int | None = None,
    languages: str = "EN",
) -> list[dict]:
    """Search Subdl for subtitle candidates and return a normalised list.

    Each item in the returned list has:
        id          – unique subdl subtitle id (str)
        filename    – display name shown in the picker
        url         – full download URL (zip or srt)
        language    – e.g. "EN"
        ext         – "zip" or "srt"
        hi          – bool (hearing impaired)
        score       – float used for sorting in the picker
        source      – "subdl"
    """
    api_key = _cfg.SUBDL_API_KEY
    if not api_key:
        return []

    params: dict = {
        "api_key": api_key,
        "languages": languages,
        "subs_per_page": 30,
    }

    if imdb_id and imdb_id.startswith("tt"):
        params["imdb_id"] = imdb_id
    elif tmdb_id:
        params["tmdb_id"] = str(tmdb_id)

    # Always prepare clean title for fallback or direct search
    clean_title = re.sub(
        r"(?i)\b(s\d{1,2}e\d{1,2}|s\d{1,2}|season\s*\d+|e\d{1,2}|ep\s*\d+|\d+x\d+)\b.*",
        "", title
    ).strip()
    clean_title = re.sub(r"\s*\(\d{4}\)\s*$", "", clean_title).strip()

    if "imdb_id" not in params and "tmdb_id" not in params and clean_title:
        params["film_name"] = clean_title or title

    if season is not None:
        params["season_number"] = str(season)
        params["type"] = "tv"
    if episode is not None:
        params["episode_number"] = str(episode)

    headers = {
        "User-Agent": "StreamVault/1.0",
        "x-api-key": api_key,
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(
                _SUBDL_SEARCH_URL, params=params, headers=headers, timeout=10
            ) as resp:
                if resp.status != 200:
                    print(f"[subdl] Search failed: HTTP {resp.status}")
                    return []
                data = await resp.json(content_type=None)

        if not data.get("status"):
            print(f"[subdl] API error: {data.get('error', 'unknown')}")
            return []

        raw_subs = data.get("subtitles", [])

        # If searching by title and no subtitles or to check for better title variant (e.g. '&' vs 'and')
        if (not raw_subs or "&" in title or " and " in title.lower()) and clean_title:
            variants = []
            if "&" in clean_title:
                variants.append(clean_title.replace("&", "and"))
            elif " and " in clean_title.lower():
                variants.append(re.sub(r"(?i)\band\b", "&", clean_title))

            for v in variants:
                fb_params = {
                    "api_key": api_key,
                    "languages": languages,
                    "subs_per_page": 30,
                    "film_name": v,
                }
                if season is not None:
                    fb_params["season_number"] = str(season)
                    fb_params["type"] = "tv"
                if episode is not None:
                    fb_params["episode_number"] = str(episode)
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.get(_SUBDL_SEARCH_URL, params=fb_params, headers=headers, timeout=10) as resp:
                            if resp.status == 200:
                                fb_data = await resp.json(content_type=None)
                                if fb_data.get("status"):
                                    more_subs = fb_data.get("subtitles", [])
                                    if more_subs:
                                        seen_urls = {s.get("url") for s in raw_subs}
                                        for ms in more_subs:
                                            if ms.get("url") not in seen_urls:
                                                raw_subs.append(ms)
                except Exception as fe:
                    print(f"[subdl] Variant search error for '{v}': {fe}")

        results = []
        for sub in raw_subs:
            url_path = sub.get("url", "")
            if not url_path:
                continue
            full_url = (
                url_path if url_path.startswith("http")
                else f"{_SUBDL_DL_BASE}{url_path}"
            )
            ext = "zip" if full_url.lower().endswith(".zip") else "srt"
            name = sub.get("release_name") or sub.get("name") or os.path.basename(url_path)
            lang = sub.get("language") or sub.get("lang", "EN")
            results.append({
                "id": str(sub.get("id", url_path)),
                "filename": name,
                "url": full_url,
                "language": lang,
                "ext": ext,
                "hi": bool(sub.get("hi", False)),
                "score": 80.0,   # neutral; picker will show all results
                "source": "subdl",
            })

        print(f"[subdl] Found {len(results)} subtitle(s) for '{title}'")
        return results

    except Exception as e:
        print(f"[subdl] Search error for '{title}': {e}")
        return []


async def download_subdl_option(
    msg_id: int,
    url: str,
    filename: str,
    target_title: str = "",
    target_filename: str = "",
) -> str | None:
    """Download a subtitle from Subdl (zip or raw srt) and cache it.

    Handles:
    - Raw .srt files -> normalise and cache directly.
    - .zip files     -> extract best matching .srt using the existing archive logic.
    """
    temp_path = _make_temp_sub_path(msg_id)

    headers = {"User-Agent": "StreamVault/1.0"}
    api_key = _cfg.SUBDL_API_KEY
    if api_key:
        headers["x-api-key"] = api_key

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=20) as resp:
                if resp.status != 200:
                    print(f"[subdl] Download failed: HTTP {resp.status} for {url}")
                    return None
                data = await resp.read()

        with open(temp_path, "wb") as f:
            f.write(data)

        # Decide format: zip -> extract; otherwise treat as raw subtitle text
        is_zip = (
            filename.lower().endswith(".zip")
            or url.lower().endswith(".zip")
            or zipfile.is_zipfile(temp_path)
        )

        if is_zip:
            try:
                with tempfile.TemporaryDirectory() as extract_dir:
                    sub_files = _extract_all_recursive(temp_path, extract_dir)
                    if sub_files:
                        chosen_name = None
                        norm_text = None

                        # 1. Primary: Match by target video title & filename (which contains S01E03)
                        chosen_name, norm_text = _find_best_file_in_archive_tree(
                            sub_files, extract_dir, target_title, target_filename
                        )

                        # 2. Secondary: Try matching using card release name as title & filename hint
                        if not chosen_name or not norm_text:
                            if filename:
                                chosen_name, norm_text = _find_best_file_in_archive_tree(
                                    sub_files, extract_dir, filename, filename + ".srt"
                                )

                        # 3. Fallback: prefer English-named, then take largest subtitle
                        if not chosen_name or not norm_text:
                            valid_subs = [p for p in sub_files if p.lower().endswith((".srt", ".vtt", ".ass"))]
                            if valid_subs:
                                eng = [p for p in valid_subs if any(
                                    w in os.path.basename(p).lower() for w in ("english", "eng", ".en.")
                                )]
                                pick_list = eng if eng else valid_subs
                                pick_list.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
                                best_p = pick_list[0]
                                chosen_name = os.path.relpath(best_p, extract_dir).replace("\\", "/")
                                with open(best_p, "rb") as f:
                                    norm_text = _normalize_subtitle_text(f.read(), best_p)

                        if chosen_name and norm_text:
                            print(f"[subdl] Extracted '{chosen_name}' from '{filename}'")
                            try:
                                opt_cache_file = _get_option_cache_path("subdl", url, url, filename)
                                with open(opt_cache_file, "w", encoding="utf-8") as of:
                                    of.write(norm_text)
                            except Exception:
                                pass
                            return _safe_save_subtitle(msg_id, norm_text)
                    print(f"[subdl] No subtitle found in zip '{filename}'")
            except Exception as ze:
                print(f"[subdl] Zip extract error for '{filename}': {ze}")
            return None

        else:
            # Raw .srt / .vtt / .ass
            with open(temp_path, "rb") as f:
                raw = f.read()
            norm_text = _normalize_subtitle_text(raw, filename)
            try:
                opt_cache_file = _get_option_cache_path("subdl", url, url, filename)
                with open(opt_cache_file, "w", encoding="utf-8") as of:
                    of.write(norm_text)
            except Exception:
                pass
            print(f"[subdl] Cached subtitle '{filename}' for msg_id={msg_id}")
            return _safe_save_subtitle(msg_id, norm_text)

    except Exception as e:
        print(f"[subdl] Download error for '{filename}': {e}")
        return None
    finally:
        _safe_remove(temp_path)


# ── SubSource online search + download ────────────────────────────────────────

_SUBSOURCE_BASE = "https://api.subsource.net/api/v1"

async def search_subsource(
    title: str,
    season: int | None = None,
    episode: int | None = None,
    imdb_id: str | None = None,
    languages: str = "english",
) -> list[dict]:
    """Search SubSource for subtitle candidates and return a normalized list."""
    api_key = getattr(_cfg, "SUBSOURCE_API_KEY", "")
    if not api_key:
        return []

    headers = {
        "X-API-Key": api_key,
        "User-Agent": "StreamVault/1.0",
    }

    try:
        async with aiohttp.ClientSession() as sess:
            movie_ids = []  # collect all candidate movieIds

            # 1. Look up movie ID by IMDb ID
            if imdb_id and imdb_id.startswith("tt"):
                # Use params= so aiohttp URL-encodes everything correctly
                async with sess.get(
                    f"{_SUBSOURCE_BASE}/movies/search",
                    params={"searchType": "imdb", "imdb": imdb_id},
                    headers=headers, timeout=10,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("data", [])
                        if items and isinstance(items, list):
                            for item in items:
                                mid = item.get("movieId") or item.get("id")
                                if mid:
                                    item_season = item.get("season")
                                    if season is not None and item_season is not None:
                                        if item_season == season:
                                            movie_ids.insert(0, mid)
                                        else:
                                            movie_ids.append(mid)
                                    else:
                                        movie_ids.append(mid)

            if not movie_ids and title:
                # Strip SxxExx codes from title before text search
                clean_q = re.sub(
                    r"(?i)\b(s\d+e\d+|s\d+|season\s*\d+|e\d+|ep\s*\d+|\d+x\d+)\b.*",
                    "", title
                ).strip() or title
                clean_q = re.sub(r"\s*\(\d{4}\)\s*$", "", clean_q).strip()

                # Generate title search queries (original + '&'/'and' variants)
                search_queries = [clean_q]
                if "&" in clean_q:
                    search_queries.append(clean_q.replace("&", "and"))
                elif " and " in clean_q.lower():
                    search_queries.append(re.sub(r"(?i)\band\b", "&", clean_q))

                found_items = []
                for sq in search_queries:
                    async with sess.get(
                        f"{_SUBSOURCE_BASE}/movies/search",
                        params={"searchType": "text", "q": sq},
                        headers=headers, timeout=10,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("data", [])
                            if items and isinstance(items, list):
                                found_items.extend(items)
                                if items:
                                    break

                if found_items:
                    # Pick best-matching title across all results
                    clean_norm = re.sub(r"[^\w\s]", "", clean_q.lower().replace("&", "and")).strip()
                    best_score = -1
                    best_title = None
                    for item in found_items:
                        t = (item.get("title") or "").lower()
                        alt = (item.get("alternateTitle") or "").lower()
                        t_norm = re.sub(r"[^\w\s]", "", t.replace("&", "and")).strip()
                        alt_norm = re.sub(r"[^\w\s]", "", alt.replace("&", "and")).strip()

                        score = 0
                        if t_norm == clean_norm or alt_norm == clean_norm:
                            score = 100
                        elif t_norm.startswith(clean_norm) or clean_norm.startswith(t_norm):
                            score = 80
                        elif clean_norm in t_norm or clean_norm in alt_norm:
                            score = 60
                        else:
                            q_words = set(re.split(r"\W+", clean_norm))
                            t_words = set(re.split(r"\W+", t_norm))
                            score = len(q_words & t_words) * 10
                        if score > best_score:
                            best_score = score
                            best_title = t

                    # Collect ALL movieIds matching the best title (all season entries)
                    seen_mids = set()
                    for item in found_items:
                        t = (item.get("title") or "").lower()
                        alt = (item.get("alternateTitle") or "").lower()
                        if t == best_title or alt == best_title:
                            mid = item.get("movieId") or item.get("id")
                            if mid and mid not in seen_mids:
                                seen_mids.add(mid)
                                item_season = item.get("season")
                                if season is not None and item_season is not None:
                                    if item_season == season:
                                        movie_ids.insert(0, mid)
                                    else:
                                        movie_ids.append(mid)
                                else:
                                    movie_ids.append(mid)

            if not movie_ids:
                return []

            # 2. Fetch subtitles from all candidate movieIds and merge
            _LANG_MAP = {
                "en": "english",
                "es": "spanish",
                "fr": "french",
                "de": "german",
                "it": "italian",
                "pt": "portuguese",
                "ru": "russian",
                "ar": "arabic",
                "tr": "turkish",
                "sv": "swedish",
            }
            raw_lang = (languages or "english").strip().lower()
            lang_param = _LANG_MAP.get(raw_lang, raw_lang)

            all_subs_data: list[dict] = []
            seen_sub_ids: set = set()
            for movie_id in movie_ids[:5]:  # cap at 5 to avoid too many requests
                async with sess.get(
                    f"{_SUBSOURCE_BASE}/subtitles",
                    params={"movieId": movie_id, "language": lang_param, "limit": 30},
                    headers=headers, timeout=10,
                ) as resp:
                    if resp.status != 200:
                        continue
                    subs_data = await resp.json()
                    for sub in subs_data.get("data", []):
                        sub_id = sub.get("subtitleId") or sub.get("id")
                        if sub_id and sub_id not in seen_sub_ids:
                            seen_sub_ids.add(sub_id)
                            all_subs_data.append(sub)

        results = []
        for sub in all_subs_data:
            sub_id = sub.get("subtitleId") or sub.get("id")
            if not sub_id:
                continue

            rel_info = sub.get("releaseInfo", [])
            lang = sub.get("language", "English")
            hi = bool(sub.get("hearingImpaired", False))
            dl_url = f"{_SUBSOURCE_BASE}/subtitles/{sub_id}/download"

            display_names = []
            if isinstance(rel_info, list) and rel_info:
                display_names = [str(r).strip() for r in rel_info if str(r).strip()]

            if not display_names:
                comm = (sub.get("commentary") or "").split("\n")[0].strip()
                display_names = [comm or f"SubSource_{sub_id}"]

            for idx, d_name in enumerate(display_names):
                sub_season, sub_ep = extract_season_episode(d_name)
                # Only skip subs that explicitly tag a DIFFERENT episode
                if episode is not None and sub_ep is not None and sub_ep != episode:
                    continue
                if season is not None and sub_season is not None and sub_season != season:
                    continue

                clean_name = d_name if d_name.lower().endswith(".srt") else f"{d_name}.srt"

                results.append({
                    "id": f"{sub_id}_{idx}" if len(display_names) > 1 else str(sub_id),
                    "filename": clean_name,
                    "url": dl_url,
                    "language": lang,
                    "ext": "srt",
                    "hi": hi,
                    "score": 80.0,
                    "source": "subsource",
                    "season": sub_season,
                    "episode": sub_ep,
                })

        print(f"[subsource] Found {len(results)} subtitle(s) for '{title}'")
        return results

    except Exception as e:
        print(f"[subsource] Search error for '{title}': {e}")
        return []


async def download_subsource_option(
    msg_id: int,
    url: str,
    filename: str,
    target_title: str = "",
    target_filename: str = "",
) -> str | None:
    """Download a subtitle from SubSource (authenticated zip) and cache it."""
    temp_path = _make_temp_sub_path(msg_id)

    api_key = getattr(_cfg, "SUBSOURCE_API_KEY", "")
    headers = {
        "User-Agent": "StreamVault/1.0",
        "X-API-Key": api_key,
    }

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=20) as resp:
                if resp.status != 200:
                    print(f"[subsource] Download failed: HTTP {resp.status} for {url}")
                    return None
                data = await resp.read()

        with open(temp_path, "wb") as f:
            f.write(data)

        # SubSource delivers a .zip archive
        try:
            with tempfile.TemporaryDirectory() as extract_dir:
                sub_files = _extract_all_recursive(temp_path, extract_dir)
                if sub_files:
                    chosen_name = None
                    norm_text = None

                    # 1. Primary: Match by target video title & filename (which contains S01E03)
                    chosen_name, norm_text = _find_best_file_in_archive_tree(
                        sub_files, extract_dir, target_title, target_filename
                    )

                    # 2. Secondary: Try matching using card release name as both title & filename hint
                    if not chosen_name or not norm_text:
                        if filename:
                            chosen_name, norm_text = _find_best_file_in_archive_tree(
                                sub_files, extract_dir, filename, filename + ".srt"
                            )

                    # 3. Fallback: prefer English-named, then take largest subtitle
                    if not chosen_name or not norm_text:
                        valid_subs = [p for p in sub_files if p.lower().endswith((".srt", ".vtt", ".ass"))]
                        if valid_subs:
                            # Prefer files with "english"/"eng" in name
                            eng = [p for p in valid_subs if any(
                                w in os.path.basename(p).lower() for w in ("english", "eng", ".en.")
                            )]
                            pick_list = eng if eng else valid_subs
                            pick_list.sort(key=lambda p: os.path.getsize(p) if os.path.exists(p) else 0, reverse=True)
                            best_p = pick_list[0]
                            chosen_name = os.path.relpath(best_p, extract_dir).replace("\\", "/")
                            with open(best_p, "rb") as f:
                                norm_text = _normalize_subtitle_text(f.read(), best_p)

                    if chosen_name and norm_text:
                        print(f"[subsource] Extracted '{chosen_name}' from '{filename}'")
                        try:
                            opt_cache_file = _get_option_cache_path("subsource", url, url, filename)
                            with open(opt_cache_file, "w", encoding="utf-8") as of:
                                of.write(norm_text)
                        except Exception:
                            pass
                        return _safe_save_subtitle(msg_id, norm_text)
                print(f"[subsource] No subtitle found in zip '{filename}'")
        except Exception as ze:
            print(f"[subsource] Zip extract error for '{filename}': {ze}")
        return None

    except Exception as e:
        print(f"[subsource] Download error for '{filename}': {e}")
        return None
    finally:
        _safe_remove(temp_path)


# ── Yify online fallback ───────────────────────────────────────────────────────

async def fetch_from_yify(msg_id: int, imdb_id: str) -> str | None:
    """Query Yify subtitle sites for English subtitle and cache it."""
    if not imdb_id or not imdb_id.startswith("tt"):
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    domains = ["yifysubtitles.ch", "yifysubtitles.org", "yify-subtitles.org", "yts-subs.com"]

    for domain in domains:
        url = f"https://{domain}/movie-imdb/{imdb_id}"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()

                    matches = re.findall(r'href="(/subtitles/[^"]+-english-yify-[^"]+)"', html)
                    if not matches:
                        matches = re.findall(
                            r'href="(/subtitles/[^"]+)"[^>]*>.*?English',
                            html, re.DOTALL | re.IGNORECASE,
                        )
                    if not matches:
                        continue

                    sub_page_url = f"https://{domain}{matches[0]}"
                    async with sess.get(sub_page_url, headers=headers, timeout=10) as sub_resp:
                        if sub_resp.status != 200:
                            continue
                        sub_html = await sub_resp.text()

                        zip_matches = re.findall(r'href="(/subtitle/[^"]+\.zip)"', sub_html)
                        if not zip_matches:
                            slug = matches[0].replace("/subtitles/", "")
                            zip_url = f"https://{domain}/subtitle/{slug}.zip"
                        else:
                            zip_url = f"https://{domain}{zip_matches[0]}"

                        async with sess.get(zip_url, headers=headers, timeout=15) as zip_resp:
                            if zip_resp.status != 200:
                                continue
                            zip_data = await zip_resp.read()
                            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                                for name in z.namelist():
                                    if name.lower().endswith(".srt"):
                                        srt_content = z.read(name)
                                        print(f"[subtitles] Yify ({domain}): saved {name}")
                                        return _safe_save_subtitle(msg_id, srt_content)
        except Exception as e:
            print(f"[subtitles] Yify error on {domain} for imdb_id={imdb_id}: {e}")
            continue

    return None


# ── Download & cache a subtitle message ───────────────────────────────────────

# ── Subtitle text normalization (UTF-8, UTF-16, Latin-1, CP1252, VTT) ─────────

def _normalize_subtitle_text(raw_bytes: bytes, filename: str) -> str:
    """Decode raw subtitle bytes across encodings (UTF-8-BOM, UTF-16, Latin-1, CP1252)
    and return a clean UTF-8 string with WebVTT headers converted to SRT if needed."""
    if not raw_bytes:
        return ""
    text = ""
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1", "utf-8"):
        try:
            text = raw_bytes.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if not text:
        text = raw_bytes.decode("utf-8", errors="ignore")

    # Clean WebVTT header if present
    if filename.lower().endswith(".vtt") or text.startswith("WEBVTT"):
        text = re.sub(r"^WEBVTT[^\n]*\n", "", text)
        # Convert period timestamps (00:00:01.000) to comma timestamps (00:00:01,000)
        text = re.sub(r"(\d{2}:\d{2}:\d{2})\.(\d{3})", r"\1,\2", text)

    return text.strip()


# ── Recursive Multi-Format Archive Extraction (ZIP, RAR, 7z, TAR) ─────────────

def _extract_single_archive(archive_path: str, dest_dir: str) -> bool:
    """Extract a single archive file (ZIP, RAR, 7z, TAR, etc.) into dest_dir."""
    # 1. Native Python zipfile (filtered for blazing speed)
    if zipfile.is_zipfile(archive_path):
        try:
            with zipfile.ZipFile(archive_path, "r") as z:
                # Fast extraction: only unpack subtitle and nested archive members
                relevant = [
                    m for m in z.namelist()
                    if not m.startswith("__MACOSX")
                    and not os.path.basename(m).startswith("._")
                    and any(m.lower().endswith(ext) for ext in (_SUBTITLE_EXTS | _ARCHIVE_EXTS))
                ]
                if relevant:
                    z.extractall(dest_dir, members=relevant)
                else:
                    z.extractall(dest_dir)
            return True
        except Exception:
            pass

    # 2. Native Python tarfile
    if tarfile.is_tarfile(archive_path):
        try:
            with tarfile.open(archive_path, "r:*") as t:
                relevant_tar = [
                    m for m in t.getmembers()
                    if any(m.name.lower().endswith(ext) for ext in (_SUBTITLE_EXTS | _ARCHIVE_EXTS))
                ]
                if relevant_tar:
                    t.extractall(dest_dir, members=relevant_tar)
                else:
                    t.extractall(dest_dir)
            return True
        except Exception:
            pass

    # Hidden console flag for Windows so background archive tools don't pop up a console/Terminal window
    _no_win = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    # 3. System bsdtar (built into Windows 10/11, macOS, Linux — supports .rar, .7z, .zip, etc.)
    tar_exe = shutil.which("tar")
    if tar_exe:
        try:
            res = subprocess.run(
                [tar_exe, "-xf", archive_path, "-C", dest_dir],
                capture_output=True,
                text=True,
                timeout=45,
                creationflags=_no_win,
            )
            if res.returncode == 0:
                return True
        except Exception as e:
            print(f"[subtitles] tar extraction error on '{archive_path}': {e}")

    # 4. External tools if present (7z, 7za, unrar, winrar)
    for tool, args in [
        ("7z", ["7z", "x", "-y", f"-o{dest_dir}", archive_path]),
        ("7za", ["7za", "x", "-y", f"-o{dest_dir}", archive_path]),
        ("unrar", ["unrar", "x", "-y", archive_path, dest_dir]),
        ("winrar", ["winrar", "x", "-ibck", "-y", archive_path, dest_dir]),
    ]:
        if shutil.which(tool):
            try:
                res = subprocess.run(
                    args,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    creationflags=_no_win,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                pass

    return False


def _extract_all_recursive(archive_path: str, extract_base: str, max_depth: int = 3) -> list[str]:
    """Recursively unpack an archive and any nested archives within it (e.g. Series.zip -> Season 1.zip/rar).
    Returns a list of absolute paths to all discovered subtitle files."""
    # Initial unpack
    _extract_single_archive(archive_path, extract_base)

    # Recursively find and unpack nested archives
    for _ in range(max_depth):
        found_nested = False
        for root, _, files in os.walk(extract_base):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in _ARCHIVE_EXTS:
                    nested_archive = os.path.join(root, f)
                    sub_out = os.path.join(root, f"_unpacked_{os.path.splitext(f)[0]}")
                    os.makedirs(sub_out, exist_ok=True)
                    if _extract_single_archive(nested_archive, sub_out):
                        found_nested = True
                        try:
                            os.remove(nested_archive)
                        except Exception:
                            pass
        if not found_nested:
            break

    # Discover all extracted subtitle files
    sub_files = []
    for root, _, files in os.walk(extract_base):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in {".srt", ".vtt", ".ass", ".sub"}:
                if not f.startswith("._") and "__MACOSX" not in root:
                    sub_files.append(os.path.join(root, f))

    return sub_files


def _find_best_file_in_archive_tree(
    sub_files: list[str], base_dir: str, target_title: str, target_filename: str
) -> tuple[str | None, str | None]:
    """Find the best matching subtitle file from all recursively extracted files.
    Returns (chosen_rel_name, normalized_utf8_text)."""
    if not sub_files:
        return None, None

    # Single subtitle in archive: immediate direct match
    if len(sub_files) == 1:
        chosen_path = sub_files[0]
        rel_name = os.path.relpath(chosen_path, base_dir).replace("\\", "/")
        with open(chosen_path, "rb") as f:
            raw = f.read()
        return rel_name, _normalize_subtitle_text(raw, chosen_path)

    v_s, v_e = extract_season_episode(f"{target_title} {target_filename}")

    scored: list[tuple[float, str]] = []
    for full_p in sub_files:
        rel_name = os.path.relpath(full_p, base_dir).replace("\\", "/")
        sc = calculate_match_score(target_title, target_filename, rel_name)

        s_s, s_e = extract_season_episode(rel_name)
        if v_e is not None:
            if s_e is not None:
                if s_e == v_e:
                    sc += 500.0
                    if v_s is not None and s_s is not None and v_s == s_s:
                        sc += 100.0
                else:
                    # Penalize wrong episode severely so it is NEVER chosen over the correct one
                    sc -= 1000.0

        # Bonus for English indicators
        if any(w in rel_name.lower() for w in ["eng", "english", ".en."]):
            sc += 15.0

        scored.append((sc, full_p))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_path = scored[0][1]
    rel_name = os.path.relpath(best_path, base_dir).replace("\\", "/")
    with open(best_path, "rb") as f:
        raw = f.read()
    return rel_name, _normalize_subtitle_text(raw, best_path)


# ── Download & cache a subtitle message ───────────────────────────────────────

async def _download_subtitle_msg(
    client,
    entity,
    msg_id: int,
    sub_msg_id: int,
    filename: str,
    target_title: str = "",
    target_filename: str = "",
) -> str | None:
    """Download a subtitle Telegram message and save it to the local cache.
    Intelligently handles single .srt/.vtt files as well as full-season / series .zip/.rar packs.
    """
    temp_path = _make_temp_sub_path(msg_id)

    try:
        msgs = await client.get_messages(entity, ids=[sub_msg_id])
        if not msgs or not msgs[0]:
            return None
        sub_msg = msgs[0]

        await client.download_media(sub_msg, file=temp_path)

        if not os.path.exists(temp_path) or os.path.getsize(temp_path) == 0:
            print(f"[subtitles] Downloaded file is empty for sub_msg_id={sub_msg_id}")
            return None

        ext = os.path.splitext(filename)[1].lower()
        is_archive = (
            ext in _ARCHIVE_EXTS
            or zipfile.is_zipfile(temp_path)
            or tarfile.is_tarfile(temp_path)
        )

        if is_archive:
            try:
                with tempfile.TemporaryDirectory() as extract_dir:
                    sub_files = _extract_all_recursive(temp_path, extract_dir)
                    if sub_files:
                        chosen_name, norm_text = _find_best_file_in_archive_tree(
                            sub_files, extract_dir, target_title, target_filename
                        )
                        if chosen_name and norm_text:
                            print(
                                f"[subtitles] Extracted '{chosen_name}' from archive '{filename}' for '{target_title}'"
                            )
                            try:
                                opt_cache_file = _get_option_cache_path("telegram", sub_msg_id, filename, filename)
                                with open(opt_cache_file, "w", encoding="utf-8") as of:
                                    of.write(norm_text)
                            except Exception:
                                pass
                            return _safe_save_subtitle(msg_id, norm_text)
                        else:
                            print(f"[subtitles] No matching episode subtitle found in archive '{filename}'")
                    else:
                        print(f"[subtitles] No subtitle files discovered inside archive '{filename}'")
            except Exception as ze:
                print(f"[subtitles] Archive extract failed for '{filename}': {ze}")
            return None

        else:
            # Single .srt / .vtt / .ass / .sub file
            try:
                with open(temp_path, "rb") as f:
                    raw = f.read()
                norm_text = _normalize_subtitle_text(raw, filename)
                try:
                    opt_cache_file = _get_option_cache_path("telegram", sub_msg_id, filename, filename)
                    with open(opt_cache_file, "w", encoding="utf-8") as of:
                        of.write(norm_text)
                except Exception:
                    pass
                return _safe_save_subtitle(msg_id, norm_text)
            except Exception as fe:
                print(f"[subtitles] Error processing single sub '{filename}': {fe}")
                return None

    except Exception as e:
        print(f"[subtitles] Download error sub_msg_id={sub_msg_id}: {e}")
        return None
    finally:
        _safe_remove(temp_path)



# ── Main entry point ───────────────────────────────────────────────────────────

async def fetch_english_subtitle(msg_id: int, title: str, filename: str) -> str | None:
    """Find, download and cache the best subtitle for a given video.

    Search order:
    1. Local file cache (instant).
    2. Full-channel subtitle index (covers entire channel history, not just ±N).
    3. Yify online fallback (movies with IMDb ID).
    """
    # 1. Local cache hit
    cache_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    if os.path.exists(cache_path):
        return cache_path

    try:
        from streaming import _get_entity
        client = _cfg.client
        if not client:
            return None
        entity = await _get_entity()
        if not entity:
            return None

        # 2. Query the full-channel subtitle index
        v_s, v_e = extract_season_episode(f"{title} {filename}")
        candidates = query_subtitle_index(title, filename, season=v_s, episode=v_e)

        if candidates:
            best_msg_id, best_filename, best_score = candidates[0]
            print(
                f"[subtitles] Index match for '{title}': "
                f"'{best_filename}' score={best_score:.1f} (sub_msg={best_msg_id})"
            )
            path = await _download_subtitle_msg(
                client,
                entity,
                msg_id,
                best_msg_id,
                best_filename,
                target_title=title,
                target_filename=filename,
            )
            if path:
                return path

        # No index hit — run a one-time incremental index build and retry
        print(f"[subtitles] No index candidates for '{title}', triggering index scan...")
        added = await build_subtitle_index(incremental=True)
        if added > 0:
            candidates = query_subtitle_index(title, filename, season=v_s, episode=v_e)
            if candidates:
                best_msg_id, best_filename, best_score = candidates[0]
                path = await _download_subtitle_msg(
                    client,
                    entity,
                    msg_id,
                    best_msg_id,
                    best_filename,
                    target_title=title,
                    target_filename=filename,
                )
                if path:
                    return path

    except Exception as e:
        print(f"[subtitles] Index lookup error for msg_id={msg_id}: {e}")

    # 3. Yify online fallback (movies)
    try:
        import cache as _cache
        video = _cache._cache_meta.get(msg_id, {})
        pm = _cache._poster_mem.get(video.get("album", ""), {})
        imdb_id = pm.get("meta", {}).get("imdb_id")
        if imdb_id:
            path = await fetch_from_yify(msg_id, imdb_id)
            if path:
                return path
    except Exception as e:
        print(f"[subtitles] Yify fallback error: {e}")

    return None


async def list_subtitle_options(msg_id: int, title: str, filename: str, search_query: str = "", lang: str = "EN") -> dict:
    """Return all available subtitle options from the local channel index and online sources."""
    query_title = search_query.strip() if search_query and search_query.strip() else title
    clean_lang = (lang or "EN").strip().upper()

    # 0. Check fast memory cache for instant modal load / switching
    cache_key = (msg_id, query_title.lower().strip(), clean_lang.lower().strip())
    current_sub_path = os.path.join(SUB_DIR, f"{msg_id}_en.srt")
    has_active_sub = os.path.exists(current_sub_path)

    # Get active subtitle info from DB
    active_sub_name = ""
    active_sub_source = ""
    if has_active_sub:
        try:
            conn = _db_connect()
            row = conn.execute(
                "SELECT filename, source FROM active_subtitles WHERE msg_id=?",
                (msg_id,),
            ).fetchone()
            if row:
                active_sub_name = row[0]
                active_sub_source = row[1]
            conn.close()
        except Exception:
            pass

    if not search_query and cache_key in _SUB_SEARCH_CACHE:
        ts, cached_data = _SUB_SEARCH_CACHE[cache_key]
        if time.time() - ts < _SEARCH_CACHE_TTL:
            cached_copy = dict(cached_data)
            cached_opts = [dict(opt) for opt in cached_data.get("options", [])]
            for opt in cached_opts:
                opt["is_active"] = bool(active_sub_name and active_sub_name in (opt.get("filename"), opt.get("original_filename")))
            cached_copy["options"] = cached_opts
            cached_copy["has_active_sub"] = has_active_sub
            cached_copy["active_sub_name"] = active_sub_name
            cached_copy["active_sub_source"] = active_sub_source
            return cached_copy

    v_s, v_e = extract_season_episode(f"{query_title} {filename}")
    candidates = query_subtitle_index(query_title, filename, season=v_s, episode=v_e)

    if not candidates and not search_query:
        try:
            await build_subtitle_index(incremental=True)
            candidates = query_subtitle_index(query_title, filename, season=v_s, episode=v_e)
        except Exception:
            pass

    options = []
    imdb_id = None
    tmdb_id = None

    try:
        import cache as _cache
        video = _cache._cache_meta.get(msg_id, {}) or _cache._cache_meta.get(str(msg_id), {})
        album_id = video.get("album", "")
        pm = _cache._poster_mem.get(album_id, {}) if album_id else {}
        meta_dict = pm.get("meta", {})
        imdb_id = meta_dict.get("imdb_id")
        tmdb_id = meta_dict.get("tmdb_id")

        if not imdb_id and (query_title or title):
            try:
                from imdb_search import search_imdb_for_album
                search_res = search_imdb_for_album(query_title or title, filename)
                if search_res:
                    meta_res = search_res.get("meta", {})
                    imdb_id = meta_res.get("imdb_id") or imdb_id
                    tmdb_id = meta_res.get("tmdb_id") or tmdb_id
            except Exception as ie:
                print(f"[subtitles] IMDb lookup error: {ie}")
    except Exception:
        pass

    # Concurrently search Subdl and SubSource in parallel for maximum speed
    async def _fetch_subdl_task():
        if not getattr(_cfg, "SUBDL_API_KEY", ""):
            return []
        try:
            return await search_subdl(
                title=query_title,
                season=v_s,
                episode=v_e,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                languages=clean_lang,
            )
        except Exception as e:
            print(f"[subtitles] Subdl options error: {e}")
            return []

    async def _fetch_subsource_task():
        if not getattr(_cfg, "SUBSOURCE_API_KEY", ""):
            return []
        try:
            return await search_subsource(
                title=query_title,
                season=v_s,
                episode=v_e,
                imdb_id=imdb_id,
                languages=clean_lang,
            )
        except Exception as e:
            print(f"[subtitles] SubSource options error: {e}")
            return []

    subdl_results, subsource_results = await asyncio.gather(
        _fetch_subdl_task(),
        _fetch_subsource_task(),
    )

    # 1. Subdl Options
    for sub in (subdl_results or []):
        s_opt, e_opt = extract_season_episode(sub["filename"])
        eff_season = s_opt if s_opt is not None else v_s
        eff_ep    = e_opt if e_opt is not None else v_e

        display_fn = sub["filename"]
        if eff_season is not None and eff_ep is not None and e_opt is None:
            ep_tag_str = f"S{str(eff_season).zfill(2)}E{str(eff_ep).zfill(2)}"
            display_fn = re.sub(r"(?i)\bComplete[. _-]?", "", sub["filename"]).strip()
            display_fn = re.sub(r"(?i)\bS\d{1,2}\b", ep_tag_str, display_fn, count=1)
            display_fn = display_fn.strip(" .-_")
            if not display_fn:
                display_fn = sub["filename"]

        options.append({
            "id": sub["id"],
            "filename": display_fn,
            "original_filename": sub["filename"],   # keep for download matching
            "score": sub["score"],
            "source": "subdl",
            "url": sub["url"],
            "language": sub.get("language", clean_lang),
            "hi": sub.get("hi", False),
            "ext": sub["ext"],
            "season": eff_season,
            "episode": eff_ep,
            "is_active": bool(active_sub_name and active_sub_name in (display_fn, sub["filename"])),
        })

    # 2. SubSource Options
    for sub in (subsource_results or []):
        s_opt, e_opt = extract_season_episode(sub["filename"])
        eff_season = s_opt if s_opt is not None else v_s
        eff_ep    = e_opt if e_opt is not None else v_e

        display_fn = sub["filename"]
        if eff_season is not None and eff_ep is not None and e_opt is None:
            ep_tag_str = f"S{str(eff_season).zfill(2)}E{str(eff_ep).zfill(2)}"
            display_fn = re.sub(r"(?i)\bComplete[. _-]?", "", sub["filename"]).strip()
            display_fn = re.sub(r"(?i)\bS\d{1,2}\b", ep_tag_str, display_fn, count=1)
            display_fn = display_fn.strip(" .-_")
            if not display_fn:
                display_fn = sub["filename"]

        options.append({
            "id": sub["id"],
            "filename": display_fn,
            "original_filename": sub["filename"],
            "score": sub["score"],
            "source": "subsource",
            "url": sub["url"],
            "language": sub.get("language", clean_lang),
            "hi": sub.get("hi", False),
            "ext": sub["ext"],
            "season": eff_season,
            "episode": eff_ep,
            "is_active": bool(active_sub_name and active_sub_name in (display_fn, sub["filename"])),
        })

    # 3. PRIORITY 3: Telegram Channel Candidates
    for sub_msg_id, sub_filename, score in candidates:
        s, e = extract_season_episode(sub_filename)
        options.append({
            "id": sub_msg_id,
            "filename": sub_filename,
            "score": round(score, 1),
            "source": "telegram",
            "is_archive": any(sub_filename.lower().endswith(x) for x in _ARCHIVE_EXTS),
            "ext": os.path.splitext(sub_filename)[1].lower().lstrip("."),
            "season": s,
            "episode": e,
            "is_active": bool(active_sub_name and active_sub_name == sub_filename),
        })

    res_data = {
        "ok": True,
        "msg_id": msg_id,
        "title": title,
        "filename": filename,
        "search_query": search_query,
        "lang": clean_lang,
        "has_active_sub": has_active_sub,
        "active_sub_name": active_sub_name,
        "active_sub_source": active_sub_source,
        "options": options,
    }
    _SUB_SEARCH_CACHE[cache_key] = (time.time(), res_data)
    return res_data


def _record_active_sub(msg_id: int, filename: str, source: str):
    """Record which subtitle is currently active for this video."""
    try:
        conn = _db_connect()
        conn.execute(
            """INSERT OR REPLACE INTO active_subtitles (msg_id, filename, source, applied_at)
               VALUES (?, ?, ?, ?)""",
            (msg_id, filename, source, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[subtitles] Failed to record active sub: {e}")


async def apply_subtitle_option(
    msg_id: int,
    sub_msg_id: int | None,
    filename: str,
    source: str = "telegram",
    url: str = "",
) -> str | None:
    """Download and set the specific chosen subtitle for this video with instant option cache."""
    # 0. Instant Cache Check: If this option was previously downloaded/unzipped, apply it in 0ms!
    opt_key_id = sub_msg_id if sub_msg_id is not None else (url or filename)
    opt_cache_file = _get_option_cache_path(source, opt_key_id, url, filename)
    if os.path.exists(opt_cache_file) and os.path.getsize(opt_cache_file) > 0:
        try:
            with open(opt_cache_file, "r", encoding="utf-8", errors="ignore") as f:
                cached_text = f.read()
            if cached_text.strip():
                print(f"[subtitles] Instant local option cache hit for '{filename}'")
                path = _safe_save_subtitle(msg_id, cached_text)
                if path:
                    _record_active_sub(msg_id, filename, source)
                return path
        except Exception as ce:
            print(f"[subtitles] Option cache read error: {ce}")

    import cache as _cache
    video = _cache._cache_meta.get(msg_id) or _cache._cache_meta.get(str(msg_id)) or {}
    if not video:
        try:
            conn = _cache._fts_init()
            row = conn.execute(
                "SELECT title, filename, album FROM videos_plain WHERE message_id = ?",
                (msg_id,),
            ).fetchone()
            if row:
                video = {"title": row[0] or "", "filename": row[1] or "", "album": row[2] or ""}
        except Exception:
            pass

    title = video.get("title", "")
    target_filename = video.get("filename", "")

    path = None
    if source == "subdl":
        if not url:
            return None
        path = await download_subdl_option(
            msg_id=msg_id,
            url=url,
            filename=filename,
            target_title=title,
            target_filename=target_filename,
        )

    elif source == "subsource":
        if not url:
            return None
        path = await download_subsource_option(
            msg_id=msg_id,
            url=url,
            filename=filename,
            target_title=title,
            target_filename=target_filename,
        )

    else:
        # Telegram source
        from streaming import _get_entity
        client = _cfg.client
        if not client or not sub_msg_id:
            return None
        entity = await _get_entity()
        if not entity:
            return None

        path = await _download_subtitle_msg(
            client,
            entity,
            msg_id,
            sub_msg_id,
            filename,
            target_title=title,
            target_filename=target_filename,
        )

    if path:
        _record_active_sub(msg_id, filename, source)
    return path



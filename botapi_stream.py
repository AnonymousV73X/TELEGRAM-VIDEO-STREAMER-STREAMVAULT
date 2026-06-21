"""botapi_stream.py — Pure Telethon streaming for StreamVault (v8).

v8 Architecture — ZERO downloading, pure streaming
--------------------------------------------------
Like main.py: Telethon's iter_download streams directly from Telegram
MTProto to VLC. No getFile, no Bot API downloads, no disk caching.

When user clicks play:
  1. Look up channel message via Telethon
  2. Stream via iter_download (chunks flow: Telegram -> app -> VLC)
  3. Supports HTTP Range for seeking
  4. In-memory LRU chunk cache for seeks (same pattern as main.py)

The Bot API server is kept running for the file_id mapping database,
but streaming uses pure Telethon — no getFile, no downloads, no waiting.

The Bot API mapping DB (bot_media.db) is still used for forward→file_id
lookups if the user wants to use Bot API URLs in the future, but the
DEFAULT streaming path is pure Telethon.
"""

import re, asyncio, os, time as _time, logging, threading
from collections import OrderedDict
from urllib.parse import unquote
from aiohttp import web, ClientSession, ClientTimeout
import config as _cfg
from config import CHANNEL_ID, _safe_mime
from cache import _meta
from helpers import get_filename, get_mime, get_size
from bot_media_db import (
    lookup as _db_lookup,
    store as _db_store,
    store_forward_only as _db_store_forward,
    update_paths as _db_update_paths,
    count as _db_count,
)

# ── Debug logger ────────────────────────────────────────────────────────────
_dbg = logging.getLogger("streamvault")
if not _dbg.handlers:
    _dbg.setLevel(logging.DEBUG)
    _fh = logging.FileHandler(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug.log"),
        encoding="utf-8",
    )
    _fh.setFormatter(
        logging.Formatter("%(asctime)s.%(msecs)03d  %(message)s", datefmt="%H:%M:%S")
    )
    _dbg.addHandler(_fh)
    _dbg.propagate = False


def _ts() -> float:
    return _time.perf_counter()


# ═══════════════════════════════════════════════════════════════════════════════
# STREAMING CONSTANTS (same as main.py)
# ═══════════════════════════════════════════════════════════════════════════════

CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB logical chunk
REQUEST_SIZE = 1024 * 1024  # Telegram's practical MTProto request ceiling
PREFETCH = 8  # parallel TG requests
CACHE_AHEAD_CHUNKS = 16  # pre-fetch 16 MB ahead
CACHE_BEHIND_CHUNKS = 32  # keep 32 MB behind
WRITE_BLOCK = 1024 * 1024  # max bytes per resp.write()

# ── Per-stream byte cache ───────────────────────────────────────────────────
_cache: dict[int, OrderedDict] = {}  # msg_id -> {chunk_offset: bytes}
_cache_lock = threading.RLock()
_chunk_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _cache_put(msg_id: int, off: int, data: bytes):
    with _cache_lock_sync:
        od = _cache.setdefault(msg_id, OrderedDict())
        if off not in od:
            od[off] = data
            od.move_to_end(off)


_cache_lock_sync = _cache_lock


def _cache_get(msg_id: int, off: int) -> bytes | None:
    od = _cache.get(msg_id)
    if od is None:
        return None
    return od.get(off)


def _cache_evict(msg_id: int, cursor_offset: int):
    evict_before = cursor_offset - CACHE_BEHIND_CHUNKS * CHUNK_SIZE
    with _cache_lock_sync:
        od = _cache.get(msg_id)
        if not od:
            return
        to_del = [k for k in od if k < evict_before]
        for k in to_del:
            del od[k]
    for k in list(_chunk_locks):
        if k[0] == msg_id and k[1] < evict_before:
            _chunk_locks.pop(k, None)


# ═══════════════════════════════════════════════════════════════════════════════
# BOT API AVAILABILITY CHECK (kept for compatibility)
# ═══════════════════════════════════════════════════════════════════════════════

_botapi_session: ClientSession | None = None


async def _botapi_get_session() -> ClientSession:
    global _botapi_session
    if _botapi_session is None or _botapi_session.closed:
        _botapi_session = ClientSession(
            timeout=ClientTimeout(total=600, connect=10),
            headers={"Connection": "keep-alive"},
        )
    return _botapi_session


async def botapi_check_available() -> bool:
    """Check if the local Bot API server is reachable."""
    url = getattr(_cfg, "BOT_API_URL", "")
    token = _cfg.BOT_TOKEN
    if not url or not token:
        return False
    if not getattr(_cfg, "USE_BOT_API", False):
        return False
    try:
        session = await _botapi_get_session()
        async with session.get(
            f"{url}/bot{token}/getMe",
            timeout=ClientTimeout(total=5),
        ) as resp:
            data = await resp.json()
            if data.get("ok"):
                bot_info = data.get("result", {})
                print(
                    f"[botapi] Server reachable - bot: @{bot_info.get('username', '?')} "
                    f"({bot_info.get('first_name', '')})"
                )
                return True
            print(f"[botapi] getMe failed: {data.get('description', 'unknown')}")
            return False
    except Exception as e:
        print(f"[botapi] Server not reachable at {url}: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# MESSAGE FETCHING — get the Telegram message for streaming
# ═══════════════════════════════════════════════════════════════════════════════

# Cache of recently fetched messages: msg_id -> message
_msg_cache: dict[int, object] = {}
_msg_cache_lock = asyncio.Lock()


async def _get_message(msg_id: int):
    """Fetch a channel message by ID using Telethon.

    Uses the bot client first (fresh access_hash), falls back to user client.
    """
    if msg_id in _msg_cache:
        return _msg_cache[msg_id]

    client = _cfg.bot_client or _cfg.client
    if client is None:
        client = _cfg.client
    if client is None:
        return None

    try:
        channel_entity = await client.get_entity(
            int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID
        )
        msgs = await client.get_messages(channel_entity, ids=msg_id)
        msg = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
        if msg and msg.media:
            # Cache the message for a while
            if len(_msg_cache) > 500:
                # Evict oldest entries
                keys = list(_msg_cache.keys())[:200]
                for k in keys:
                    del _msg_cache[k]
            _msg_cache[msg_id] = msg
            return msg
        return None
    except Exception as e:
        print(f"[botapi] _get_message failed for msg={msg_id}: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# PURE STREAMING — Telethon iter_download (like main.py)
# ═══════════════════════════════════════════════════════════════════════════════


async def _fetch_chunk(msg, msg_id: int, off: int, file_size: int) -> bytes:
    """Fetch one CHUNK_SIZE piece from Telegram via iter_download.

    Uses per-chunk deduplication: if multiple requests want the same chunk,
    only one fetches from TG; others wait on the lock and read from cache.
    """
    cached = _cache_get(msg_id, off)
    if cached is not None:
        return cached

    key = (msg_id, off)
    lock = _chunk_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _chunk_locks[key] = lock

    async with lock:
        # Re-check cache after acquiring lock
        cached = _cache_get(msg_id, off)
        if cached is not None:
            return cached

        # Pick a client for this fetch (round-robin across pool)
        client = _cfg.client
        if _cfg.client_pool:
            idx = hash(key) % len(_cfg.client_pool)
            client = _cfg.client_pool[idx]

        buf = bytearray()
        try:
            async for piece in client.iter_download(
                msg.media,
                offset=off,
                chunk_size=CHUNK_SIZE,
                request_size=REQUEST_SIZE,
                file_size=file_size or None,
                limit=1,
            ):
                buf.extend(piece)
                if len(buf) >= CHUNK_SIZE:
                    break
        except Exception as e:
            _dbg.debug(f"[TG-ERR] msg={msg_id} off={off} error={e}")
            raise

        data = bytes(buf[:CHUNK_SIZE])
        _cache_put(msg_id, off, data)
        return data


# ═══════════════════════════════════════════════════════════════════════════════
# VLC STREAM HANDLER — Pure Telethon streaming (v8)
# ═══════════════════════════════════════════════════════════════════════════════


async def botapi_vlc_stream(request: web.Request):
    """VLC stream handler — pure Telethon streaming, ZERO downloading.

    v8: Streams directly from Telegram via iter_download, exactly like main.py.
    No getFile, no Bot API downloads, no disk caching.

    Flow:
      1. Fetch channel message via Telethon
      2. Parse Range header
      3. Stream via iter_download with prefetch + LRU chunk cache
      4. Chunks flow: Telegram MTProto -> app -> VLC

    This is the ONLY streaming mechanism when USE_BOT_API=1. No Bot API
    downloads involved.
    """
    msg_id = int(request.match_info["msg_id"])
    filename_from_url = request.match_info.get("filename", "")

    # ── Get message ─────────────────────────────────────────────────────────
    msg = await _get_message(msg_id)
    if not msg or not msg.media:
        return web.Response(status=404, text="Not found")

    # ── Get file info ───────────────────────────────────────────────────────
    meta = _meta(msg_id)
    total = get_size(msg) or meta.get("size_bytes", 0)
    raw_mime = meta.get("mime_type") or get_mime(msg)
    raw_mime = _safe_mime(raw_mime)
    filename = (
        unquote(filename_from_url)
        if filename_from_url
        else meta.get("filename", get_filename(msg) or f"video_{msg_id}.mp4")
    )

    # ── Parse Range header ──────────────────────────────────────────────────
    range_hdr = request.headers.get("Range")
    start, end = 0, total - 1 if total else 0

    if range_hdr and total:
        try:
            rng = range_hdr.split("=")[1]
            s, e = rng.split("-")
            start = int(s) if s else 0
            end = int(e) if e else total - 1
        except Exception:
            raise web.HTTPRequestRangeNotSatisfiable()

    # Align start to chunk boundary
    offset = start - (start % CHUNK_SIZE)
    first_skip = start - offset
    length = end - start + 1 if total else None

    _dbg.debug(
        f"[REQ  ] msg={msg_id} range={range_hdr or 'none'} "
        f"start={start} end={end} total={total} offset={offset} skip={first_skip}"
    )

    # ── Prepare response ────────────────────────────────────────────────────
    resp = web.StreamResponse(
        status=206 if range_hdr else 200,
        headers={
            "Content-Type": raw_mime,
            "Accept-Ranges": "bytes",
            "Content-Disposition": f'inline; filename="{filename}"',
            "Connection": "keep-alive",
            **(
                {
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {start}-{end}/{total}",
                }
                if total
                else {}
            ),
        },
    )
    await resp.prepare(request)

    # ── Stream via iter_download with prefetch (same as main.py) ────────────
    remaining = length
    _chunks_written = 0
    _bytes_written = 0
    _cache_hits = 0
    _tg_fetches = 0

    try:
        from collections import deque

        cur_offset = offset
        skip = first_skip
        window: deque = deque()
        _enqueue_frontier = [offset]

        def _enqueue():
            while len(window) < PREFETCH:
                next_off = _enqueue_frontier[0]
                read_head = window[0][0] if window else cur_offset
                if next_off > read_head + CACHE_AHEAD_CHUNKS * CHUNK_SIZE:
                    break
                if remaining is not None and next_off >= offset + (length or 0):
                    break
                window.append(
                    (
                        next_off,
                        asyncio.ensure_future(
                            _fetch_chunk(msg, msg_id, next_off, total)
                        ),
                    )
                )
                _enqueue_frontier[0] = next_off + CHUNK_SIZE

        _enqueue()

        while window:
            off, task = window.popleft()
            try:
                chunk = await asyncio.shield(task)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _dbg.debug(f"[FETCH-ERR] msg={msg_id} off={off} error={e}")
                break

            _cache_evict(msg_id, off)
            _enqueue()

            if not chunk:
                _dbg.debug(f"[END  ] msg={msg_id} empty chunk at off={off}")
                break

            if skip:
                chunk = chunk[skip:]
                skip = 0
                if not chunk:
                    continue

            if remaining is not None:
                if len(chunk) > remaining:
                    chunk = chunk[:remaining]
                remaining -= len(chunk)

            # Drip-write in WRITE_BLOCK slices
            view = memoryview(chunk)
            pos = 0
            while pos < len(view):
                await resp.write(bytes(view[pos : pos + WRITE_BLOCK]))
                pos += WRITE_BLOCK

            _chunks_written += 1
            _bytes_written += len(chunk)

            if remaining is not None and remaining <= 0:
                break

    except (
        ConnectionResetError,
        ConnectionError,
        asyncio.CancelledError,
        BrokenPipeError,
        OSError,
    ) as exc:
        _dbg.debug(f"[DISC ] msg={msg_id} {type(exc).__name__}: {exc}")
    except Exception as e:
        _dbg.debug(f"[STREAM-ERR] msg={msg_id} {type(e).__name__}: {e}")

    _total_ms = 0  # simplified — we don't track per-chunk timing here
    _dbg.debug(f"[DONE ] msg={msg_id} chunks={_chunks_written} bytes={_bytes_written}")

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# FMP4 FEED — for browser stream_handler (FFmpeg stdin)
# ═══════════════════════════════════════════════════════════════════════════════


async def botapi_feed_ffmpeg(msg_id: int, offset: int, length: int | None):
    """Async generator that yields file data from Telethon for FFmpeg stdin."""
    msg = await _get_message(msg_id)
    if not msg or not msg.media:
        return

    meta = _meta(msg_id)
    total = get_size(msg) or meta.get("size_bytes", 0)

    sent = 0
    cur_offset = offset - (offset % CHUNK_SIZE)
    skip = offset - cur_offset

    while True:
        chunk = await _fetch_chunk(msg, msg_id, cur_offset, total)
        if not chunk:
            break

        if skip:
            chunk = chunk[skip:]
            skip = 0
            if not chunk:
                cur_offset += CHUNK_SIZE
                continue

        if length is not None and sent + len(chunk) > length:
            chunk = chunk[: length - sent]

        yield chunk
        sent += len(chunk)

        if length is not None and sent >= length:
            break

        cur_offset += CHUNK_SIZE


# ═══════════════════════════════════════════════════════════════════════════════
# CACHE CHECK & STATS
# ═══════════════════════════════════════════════════════════════════════════════


def botapi_is_cached(msg_id: int) -> bool:
    """Check if a file has chunks in the streaming cache."""
    od = _cache.get(msg_id)
    return bool(od)


def botapi_stats() -> dict:
    """Return streaming cache statistics."""
    total_chunks = 0
    total_size = 0
    for od in _cache.values():
        total_chunks += len(od)
        for data in od.values():
            total_size += len(data)

    db_count = _db_count()
    return {
        "total": db_count,
        "cached_chunks": total_chunks,
        "cached_mb": round(total_size / 1024 / 1024, 1),
        "messages": len(_msg_cache),
    }


async def botapi_background_download(msg_id: int):
    """No-op in v8 — we don't pre-download anything.
    Kept for API compatibility with streaming.py.
    """
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC — removed in v8 (no forwarding 30k files)
# ═══════════════════════════════════════════════════════════════════════════════


async def botapi_sync_all():
    """No-op in v8 — we don't forward 30k files to the bot.
    Pure on-demand streaming via Telethon iter_download.
    """
    print("[botapi] v8: no startup sync — pure on-demand Telethon streaming")
    print("[botapi] Files stream directly from Telegram, no downloading")


def get_sync_progress() -> dict:
    """Return current progress (v8: always 'done')."""
    return {
        "total": 0,
        "resolving": 0,
        "status": "v8-pure-streaming",
    }

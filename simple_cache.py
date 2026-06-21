# Simple in-memory chunk cache (ported from F2L/main.py)
import threading
import asyncio
from collections import OrderedDict

# Global structures
_cache: dict[int, OrderedDict] = {}
_cache_lock = threading.Lock()
_chunk_locks: dict[tuple[int, int], asyncio.Lock] = {}
_active_streams: dict[int, int] = {}
_playhead: dict[int, dict] = {}
_PER_VIDEO_MAX_CAP = 1024 * 1024 * 1024  # 1 GB per video limit

def _cache_put(msg_id: int, off: int, data: bytes):
    """Store a chunk for a message id at the given offset."""
    with _cache_lock:
        od = _cache.setdefault(msg_id, OrderedDict())
        if off not in od:
            od[off] = data
            od.move_to_end(off)

def _cache_get(msg_id: int, off: int) -> bytes | None:
    od = _cache.get(msg_id)
    if od is None:
        return None
    return od.get(off)

def _cache_evict(msg_id: int, cursor_offset: int):
    """Drop chunks that are far behind the current read cursor."""
    # Evict chunks that are more than 32 * CHUNK_SIZE behind the cursor.
    # CHUNK_SIZE is defined in streaming.py; we import it lazily.
    from streaming import CHUNK_SIZE
    evict_before = cursor_offset - 32 * CHUNK_SIZE
    with _cache_lock:
        od = _cache.get(msg_id)
        if not od:
            return
        to_del = [k for k in od if k < evict_before]
        for k in to_del:
            del od[k]
    # Clean stale locks
    for k in list(_chunk_locks):
        if k[0] == msg_id and k[1] < evict_before:
            _chunk_locks.pop(k, None)

def _cache_clear_msg(msg_id: int):
    with _cache_lock:
        _cache.pop(msg_id, None)
    for k in list(_chunk_locks):
        if k[0] == msg_id:
            _chunk_locks.pop(k, None)
    _playhead.pop(msg_id, None)
    _active_streams.pop(msg_id, None)

def _cache_clear_all():
    with _cache_lock:
        _cache.clear()
    _chunk_locks.clear()
    _playhead.clear()
    _active_streams.clear()

def update_playhead(msg_id: int, position_s: float, duration_s: float, total_bytes: int):
    """Update the playhead bookkeeping for a streaming session."""
    if total_bytes <= 0 or duration_s <= 0:
        return
    byte_pos = int((position_s / duration_s) * total_bytes)
    _playhead[msg_id] = {
        "byte_pos": byte_pos,
        "duration_s": duration_s,
        "total_bytes": total_bytes,
        "updated_at": asyncio.get_event_loop().time(),
    }

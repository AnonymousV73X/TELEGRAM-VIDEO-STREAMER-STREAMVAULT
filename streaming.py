"""streaming.py — Resilient Sequential Streaming for StreamVault.

Architecture (v2 — "Resilient Sequential Streaming")
=====================================================

Previous approach used 6 parallel MTProto workers with stride-based offset
management and a reordering queue.  This was fragile: boundary bugs in
_clamp_req caused "invalid limit" errors, and connection drops cascaded
through the worker pool.

New approach:
  - **2-lane sequential download**: one lane serves the active VLC request,
    one lane prefetches ahead of the playhead.  Each lane uses a simple
    sequential iter_download loop — no parallel workers, no reordering.
  - **Adaptive chunk size**: instead of burst→cruise phase switching, we
    use a single adaptive request_size that scales with file size.
  - **Bulletproof clamping**: _safe_read_size() guarantees every Telegram
    request is valid: multiple of 4096, and offset + size < file_size.
  - **Automatic retry**: connection drops (WinError 10054) are caught and
    retried with exponential backoff instead of killing the pipeline.
  - **One unified prefetcher**: replaces three separate systems (prewarm,
    lookahead, playhead-ahead) with a single loop that fills gaps ahead
    of the playhead.

Module map
----------
_StreamCache       — in-memory rolling seek-back cache (unchanged)
_tg_read()         — resilient sequential read from Telegram
vlc_stream_handler — raw byte-range stream for VLC (simplified)
stream_handler     — fMP4 remux stream for browser (simplified)
_prefetch_loop()   — unified background prefetcher
HLS routes         — unchanged
"""

import re, asyncio, os, shutil, json, time as _time, math, threading
from collections import OrderedDict, deque
from urllib.parse import unquote
from aiohttp import web
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo as _DocVid
import config as _cfg
from config import (
    CHANNEL_ID,
    PORT,
    _here,
    _safe_mime,
    VLC_BLOCK_SIZE as _VLC_BLOCK_SIZE,
    VLC_WORKERS as _VLC_WORKERS,
    VLC_WRITE_CHUNK as _VLC_WRITE_CHUNK,
    VLC_PREFETCH_BLOCKS as _VLC_PREFETCH_BLOCKS,
    TG_WINDOW_BLOCKS as _TG_WINDOW_BLOCKS,
    CHUNK_READ_TIMEOUT as _CHUNK_READ_TIMEOUT,
    CIRCUIT_BREAKER_THRESHOLD as _CIRCUIT_BREAKER_THRESHOLD,
    CIRCUIT_BREAKER_GLOBAL as _CIRCUIT_BREAKER_GLOBAL,
    MAX_RETRIES as _MAX_RETRIES,
    RETRY_BASE_S as _RETRY_BASE_S,
    RETRY_MAX_S as _RETRY_MAX_S,
    DC_UNHEALTHY_THRESHOLD as _DC_UNHEALTHY_THRESHOLD,
    DC_RECOVERY_S as _DC_RECOVERY_S,
    PREFETCH_AHEAD_S as _PREFETCH_AHEAD_S,
    PREFETCH_CHUNK as _PREFETCH_CHUNK,
    PROGRESSIVE_SEGMENT as _PROGRESSIVE_SEGMENT,
    PROGRESSIVE_MAX_LANES as _PROGRESSIVE_MAX_LANES,
    ACTIVE_READ_ZONE as _ACTIVE_READ_ZONE,
    PROGRESSIVE_STALL_FALLBACK_S as _PROGRESSIVE_STALL_FALLBACK_S,
)
from helpers import get_filename, get_mime, get_size, get_duration
from cache import (
    _meta,
    _load_cache,
    _save_cache,
    _HLS_DIR,
)

# ── DC-aware client selection ────────────────────────────────────────────────
# Telegram stores files on specific DCs.  When a file is on a different DC
# than the client's home DC, Telethon must create a connection to that DC.
# This module tracks which clients have connections to which DCs and selects
# the best client for each file.
#
# v3 DC FIX: The original implementation only scanned 200 messages, stopped
# after 5 DCs, and only warmed up 1 client per DC.  The new implementation:
#   - Scans ALL cached messages to discover every DC used by channel files
#   - Explicitly exports authorization to each non-home DC via Telethon's
#     internal _sender export mechanism
#   - Warms up ALL pool clients to each DC (not just one)
#   - Dynamically updates _dc_client_map when a client successfully connects
#     to a new DC during streaming
#   - On DC error in _tg_read(), forces a fresh DC connection before retrying

_dc_client_map: dict[int, list] = {}  # dc_id → [client, ...]
_home_dc: int | None = None  # main client's home DC
_dc_warmup_done: bool = False  # True once preconnect_dcs() finishes


def _get_file_dc(msg) -> int | None:
    """Return the DC ID where a file is stored, or None if unknown."""
    try:
        if msg and msg.media and hasattr(msg.media, "document") and msg.media.document:
            doc = msg.media.document
            return getattr(doc, "dc_id", None)
    except Exception:
        pass
    return None


def _get_client_dc(client) -> int | None:
    """Return the DC ID a client is connected to (its home DC)."""
    try:
        return getattr(getattr(client, "session", None), "dc_id", None)
    except Exception:
        return None


def _register_dc_client(dc_id: int, client):
    """Record that a client has an active connection to the given DC.

    Called whenever we confirm a client can reach a DC — during warmup,
    after successful downloads, or after DC error recovery.  This keeps
    _dc_client_map dynamically updated as the session progresses.
    """
    if dc_id is None:
        return
    if dc_id not in _dc_client_map:
        _dc_client_map[dc_id] = []
    if client not in _dc_client_map[dc_id]:
        _dc_client_map[dc_id].append(client)
        dc_list = sorted(_dc_client_map.keys())
        print(
            f"[dc] + client registered for DC {dc_id} (now {len(_dc_client_map[dc_id])} clients). Map: {dc_list}"
        )


async def _export_dc_auth(client, target_dc: int):
    """Force a client to establish a connection to a target DC.

    Telethon stores DC connections in client._sender and the session's
    _dc_addresses.  When we call iter_download on a file from a different
    DC, Telethon's internal logic should auto-migrate — but this can fail
    silently if the authorization export hasn't happened yet.

    This function explicitly triggers the DC connection by calling
    Telethon's internal _export_auth method, which does the
    auth.ExportAuthorization RPC to the target DC.
    """
    try:
        # Method 1: Use Telethon's built-in DC connection mechanism.
        # Telethon's iter_download calls _get_download_client() which
        # calls client._borrow_exported_sender(dc_id).  This internally
        # calls _export_auth if needed.  But sometimes it fails to
        # auto-detect the DC.  We force it by accessing the DC sender.
        sender = await client._borrow_exported_sender(target_dc)
        if sender is not None:
            # Successfully got a sender for this DC — return it
            await client._return_exported_sender(sender)
            return True
    except Exception as e:
        print(f"[dc] _borrow_exported_sender for DC {target_dc} failed: {e}")

    try:
        # Method 2: Force a tiny download which triggers Telethon's
        # auto-migration.  We use the main client to get a message,
        # then try to download from it with this client.
        # This is a last-resort fallback.
        pass  # Handled by _warmup_client_to_dc below
    except Exception:
        pass

    return False


async def _warmup_client_to_dc(client, dc_id: int, msg):
    """Warm up a client's connection to a specific DC by downloading a
    tiny chunk from a file known to be on that DC.

    This forces Telethon to:
    1. Resolve the DC address from the session's DC map
    2. Export authorization to that DC
    3. Create an MTProto connection to that DC
    4. Download at least one chunk (proving the connection works)

    Returns True if the warmup succeeded.
    """
    try:
        # First try: just iter_download a single chunk.
        # Use a 10-second timeout — if the DC connection is dead,
        # this will hang and timeout rather than returning immediately.
        got_data = False
        async for chunk in client.iter_download(
            msg, offset=0, request_size=4096, limit=1
        ):
            if isinstance(chunk, (bytes, bytearray)) and len(chunk) > 0:
                got_data = True
            break  # Just need one chunk to trigger DC connection
        if got_data:
            _register_dc_client(dc_id, client)
            return True
        else:
            print(f"[dc] warmup to DC {dc_id}: iter_download returned empty data")
            return False
    except Exception as e:
        err_str = str(e).lower()
        # If the error is a DC migration error, try to force it
        if any(
            kw in err_str
            for kw in (
                "file migrate",
                "filemigrate",
                "wrong dc",
            )
        ):
            try:
                # Extract the target DC from the error
                # FileMigrateError(xx) — xx is the DC number
                import re as _re

                dc_match = _re.search(r"(\d+)", str(e))
                if dc_match:
                    forced_dc = int(dc_match.group(1))
                    print(
                        f"[dc] FileMigrateError says DC {forced_dc}, forcing connection"
                    )
                    # Force DC connection via _borrow_exported_sender
                    sender = await client._borrow_exported_sender(forced_dc)
                    if sender:
                        await client._return_exported_sender(sender)
                    # Retry the download
                    async for chunk in client.iter_download(
                        msg, offset=0, request_size=4096, limit=1
                    ):
                        if isinstance(chunk, (bytes, bytearray)) and len(chunk) > 0:
                            _register_dc_client(forced_dc, client)
                            return True
                        break
            except Exception as e2:
                print(f"[dc] forced DC migration for DC {dc_id} failed: {e2}")
        # Check for "server closed" which means the DC connection is dead
        if (
            "server closed" in err_str
            or "10054" in err_str
            or "forcibly closed" in err_str
        ):
            print(
                f"[dc] warmup to DC {dc_id}: server closed connection — DC may be unreachable"
            )
        else:
            print(f"[dc] warmup to DC {dc_id} failed: {e}")
        return False


async def _dc_keepalive_loop():
    """Keep exported sender connections alive and detect dead ones early.

    Telethon disconnects exported senders after 60 seconds of idle
    (_DISCONNECT_EXPORTED_AFTER = 60).  This loop borrows/returns each
    active sender every 45 seconds to reset the idle timer.  If the
    borrow fails (connection is dead), the sender is invalidated so the
    next download creates a fresh TCP connection instead of reusing a
    stale one.
    """
    await asyncio.sleep(60)  # Let initial warmup finish first
    while True:
        await asyncio.sleep(45)  # Reset idle timer well before 60s cutoff
        if not _cfg.client_pool:
            continue
        for dc_id in list(_dc_client_map.keys()):
            if dc_id == _home_dc:
                continue  # Home DC has its own keepalive via Telethon's updates loop
            for client in list(_cfg.client_pool or [_cfg.client]):
                # FIX: Don't rely on _borrowed_senders internals (Telethon 1.43+ changed
                # the structure).  Always attempt a borrow/return to reset idle timer.
                # _borrow_exported_sender is a no-op if no cached sender exists — it just
                # creates one, which is exactly what we want for keepalive.
                try:
                    sender = await client._borrow_exported_sender(dc_id)
                    if sender is not None:
                        await client._return_exported_sender(sender)
                except Exception:
                    # Connection is dead — invalidate so next download gets a fresh one
                    await _invalidate_dc_sender(client, dc_id)
                    print(f"[dc] keepalive: DC {dc_id} sender dead, invalidated")


async def preconnect_dcs():
    """Pre-connect ALL pool clients to ALL DCs used by channel files.

    v4: Instead of fetching 500 messages via _get_msg (which forwards each
    one to the bot inbox — extremely slow), we directly fetch a batch of
    recent messages from the channel and check their dc_id.  Telegram
    stores files in only 5 DCs (1-5), so a small sample is enough to
    discover all of them.

    Then we warm up ALL pool clients to each discovered DC by exporting
    authorization via _borrow_exported_sender.
    """
    global _home_dc, _dc_warmup_done
    if not _cfg.client_pool:
        return

    _home_dc = _get_client_dc(_cfg.client)
    print(f"[dc] Home DC: {_home_dc}")

    # ── Phase 1: Discover DCs by fetching recent channel messages ────────
    # Telegram has only 5 DCs (1-5).  A sample of ~100 recent messages
    # is statistically guaranteed to cover all DCs used by the channel.
    # We use the user client's get_messages (NOT _get_msg which forwards
    # to bot inbox — way too slow for 100 messages).
    dc_sample: dict[int, int] = {}  # dc_id → msg_id (one representative per DC)

    try:
        ent = await _get_entity()
        # Fetch 100 recent messages — fast, single API call
        messages = await _cfg.client.get_messages(ent, limit=100)
        for msg in messages:
            if not msg or not msg.media:
                continue
            dc = _get_file_dc(msg)
            if dc and dc not in dc_sample:
                dc_sample[dc] = msg.id
                print(
                    f"[dc] Found DC {dc} (msg={msg.id}), total DCs so far: {len(dc_sample)}"
                )
    except Exception as e:
        print(f"[dc] channel scan error: {e}")

    # If the channel scan didn't find enough DCs, also try Telethon's
    # internal DC list from the session.  The session stores all DC
    # addresses the client has ever connected to.
    if len(dc_sample) < 2:
        try:
            # Telethon stores DC IPs in session._dc_addresses
            session = _cfg.client.session
            dc_addrs = getattr(session, "_dc_addresses", None) or {}
            if dc_addrs:
                print(f"[dc] Session knows about DCs: {sorted(dc_addrs.keys())}")
                for dc_id in dc_addrs:
                    if dc_id not in dc_sample and dc_id != _home_dc:
                        # We know this DC exists but don't have a sample msg.
                        # Still register it so _borrow_exported_sender can be tried.
                        dc_sample[dc_id] = None
                        print(f"[dc] Added DC {dc_id} from session (no sample msg)")
        except Exception as e:
            print(f"[dc] session DC scan error: {e}")

    if not dc_sample:
        print("[dc] No file DCs discovered — will connect on-demand")
        if _home_dc:
            _dc_client_map[_home_dc] = list(_cfg.client_pool)
        _dc_warmup_done = True
        return

    # ── Phase 2: Warm up ALL pool clients to each DC ────────────────────
    pool = _cfg.client_pool
    total_warmups = 0
    failed_warmups = 0

    for dc_id, sample_msg_id in dc_sample.items():
        if dc_id == _home_dc:
            # Home DC — all clients are already connected
            _register_dc_client(dc_id, None)  # ensure key exists
            _dc_client_map[dc_id] = list(pool)  # all clients work for home DC
            print(f"[dc] DC {dc_id} (home) — all {len(pool)} clients ready")
            continue

        dc_ready_clients = []
        max_dc_warmups = len(pool)
        for idx, client in enumerate(pool):
            if len(dc_ready_clients) >= max_dc_warmups:
                break

            total_warmups += 1
            is_flood_wait = False
            try:
                # Method 1: Borrow exported sender — forces auth export + connection
                sender = await client._borrow_exported_sender(dc_id)
                if sender is not None:
                    await client._return_exported_sender(sender)
                    # VERIFY: actually download a chunk to prove the connection works.
                    # Auth export alone does NOT prove data transfer — the TCP
                    # connection can be established but immediately killed by
                    # Telegram's DC (WinError 10054).  Only a real download proves
                    # the connection is alive.
                    verified = False
                    if sample_msg_id is not None:
                        try:
                            ent = await _get_entity()
                            smsg = await _cfg.client.get_messages(
                                ent, ids=sample_msg_id
                            )
                            if smsg and smsg.media:
                                verified = await _warmup_client_to_dc(
                                    client, dc_id, smsg
                                )
                        except Exception:
                            pass
                    if verified:
                        dc_ready_clients.append(client)
                        _register_dc_client(dc_id, client)
                        print(
                            f"[dc] ✓ DC {dc_id} client {idx} warmed up (auth + download verified)"
                        )
                        continue
                    # Auth export succeeded but download failed — the TCP
                    # connection is dead.  Invalidate and fall through to
                    # Method 2 (direct download warmup).
                    print(
                        f"[dc] ⚠ DC {dc_id} auth export OK but download verify FAILED — trying direct download"
                    )
                    await _invalidate_dc_sender(client, dc_id)
            except Exception as e:
                err_msg = str(e)
                if "wait of" in err_msg.lower() or "flood" in err_msg.lower():
                    print(
                        f"[dc] DC {dc_id} auth export rate-limited by Telegram (FloodWait). Stopping DC {dc_id} warmup."
                    )
                    is_flood_wait = True
                else:
                    print(f"[dc] DC {dc_id} client {idx} auth export failed: {e}")

            if is_flood_wait:
                failed_warmups += 1
                break

            # Method 2: If we have a sample msg, try downloading a chunk
            if sample_msg_id is not None:
                try:
                    ent = await _get_entity()
                    msg = await _cfg.client.get_messages(ent, ids=sample_msg_id)
                    if msg and msg.media:
                        ok = await _warmup_client_to_dc(client, dc_id, msg)
                        if ok:
                            dc_ready_clients.append(client)
                            print(
                                f"[dc] ✓ DC {dc_id} client {idx} warmed up via download"
                            )
                            continue
                except Exception as e2:
                    err_msg2 = str(e2)
                    if "wait of" in err_msg2.lower() or "flood" in err_msg2.lower():
                        print(
                            f"[dc] DC {dc_id} download warmup rate-limited by Telegram (FloodWait). Stopping DC {dc_id} warmup."
                        )
                        is_flood_wait = True
                    else:
                        print(
                            f"[dc] DC {dc_id} client {idx} download warmup failed: {e2}"
                        )

            failed_warmups += 1
            if is_flood_wait:
                break
            print(f"[dc] ✗ DC {dc_id} client {idx} warmup failed")

        if dc_ready_clients:
            _dc_client_map[dc_id] = dc_ready_clients
            print(
                f"[dc] DC {dc_id} — {len(dc_ready_clients)}/{len(pool)} clients ready"
            )
        else:
            print(
                f"[dc] ⚠ DC {dc_id} — NO clients could connect! Will retry on-demand."
            )

    # Ensure home DC is registered
    if _home_dc and _home_dc not in _dc_client_map:
        _dc_client_map[_home_dc] = list(pool)

    _dc_warmup_done = True
    dc_list = sorted(_dc_client_map.keys())
    print(
        f"[dc] Warmup complete. DCs: {dc_list}, ready: {total_warmups - failed_warmups}/{total_warmups}"
    )


def _get_vlc_pool() -> list:
    pool = getattr(_cfg, "client_pool", None)
    if not pool:
        return [_cfg.client]
    if len(pool) <= 2:
        return pool
    return pool[:2]  # Dedicate first 2 clients exclusively for VLC stream handling


def _get_bg_pool() -> list:
    pool = getattr(_cfg, "client_pool", None)
    if not pool:
        return [_cfg.client]
    if len(pool) <= 2:
        return pool
    return pool[
        2:
    ]  # Dedicate other clients for background prefetch/progressive/prewarm


def get_dc_aware_client(
    msg, fallback_client=None, is_bg: bool = False
) -> "TelegramClient":
    """Return the best client for downloading a file from the right DC.

    If we know which DC the file is on, and we have a client that already
    has a connection to that DC, use it.  Otherwise fall back to the
    provided client (which will trigger Telethon's on-demand DC migration).
    """
    dc_id = _get_file_dc(msg)
    pool = _get_bg_pool() if is_bg else _get_vlc_pool()

    if dc_id and dc_id in _dc_client_map:
        dc_clients = [c for c in _dc_client_map[dc_id] if c in pool]
        if dc_clients:
            return dc_clients[_time.time_ns() % len(dc_clients)]

    # Fallback: if the file is on the home DC, any client in our pool works
    if dc_id == _home_dc:
        home_clients = [c for c in pool if _get_client_dc(c) == _home_dc]
        if home_clients:
            return home_clients[_time.time_ns() % len(home_clients)]

    # Last resort: round robin the selected pool
    return pool[_time.time_ns() % len(pool)]


# Track per-DC failure streaks for staggered reconnect.
# When a DC has too many consecutive failures, we mark it as
# unhealthy and fall back to Telethon's built-in DC migration
# (which creates a fresh exported sender internally) instead of
# reusing our pre-warmed sender pool.
_dc_health: dict[int, dict] = (
    {}
)  # dc_id -> {"failures": int, "last_fail": float, "healthy": bool}
# _DC_UNHEALTHY_THRESHOLD / _DC_RECOVERY_S now sourced from config.py (env-tunable).


def _is_dc_healthy(dc_id: int) -> bool:
    """Check if a DC is considered healthy for downloads."""
    if dc_id not in _dc_health:
        return True  # no history = healthy
    info = _dc_health[dc_id]
    if info["healthy"]:
        return True
    # Check if enough time has passed to try again
    if _time.time() - info["last_fail"] > _DC_RECOVERY_S:
        info["healthy"] = True
        info["failures"] = 0
        print(f"[dc] DC {dc_id} recovery timer expired — marking healthy again")
        return True
    return False


def _mark_dc_failure(dc_id: int):
    """Record a failure for a DC and mark unhealthy if threshold reached."""
    if dc_id not in _dc_health:
        _dc_health[dc_id] = {"failures": 0, "last_fail": 0.0, "healthy": True}
    info = _dc_health[dc_id]
    info["failures"] += 1
    info["last_fail"] = _time.time()
    if info["failures"] >= _DC_UNHEALTHY_THRESHOLD:
        if info["healthy"]:
            info["healthy"] = False
            print(
                f"[dc] DC {dc_id} marked UNHEALTHY ({info['failures']} consecutive failures)"
            )


def _mark_dc_success(dc_id: int):
    """Record a successful download from a DC."""
    if dc_id not in _dc_health:
        _dc_health[dc_id] = {"failures": 0, "last_fail": 0.0, "healthy": True}
    _dc_health[dc_id]["failures"] = 0
    _dc_health[dc_id]["healthy"] = True


async def _invalidate_dc_sender(client, dc_id: int):
    """Invalidate Telethon's cached exported sender for a specific DC.

    Unlike client.disconnect() which kills ALL connections (including the
    home DC), this only drops the sender for one DC, forcing Telethon to
    create a fresh TCP connection + re-export auth on next use.

    Telethon 1.43+ stores borrowed senders at client._borrowed_senders
    (not client._sender._exported_senders which does not exist).
    """
    invalidated = False

    # Telethon 1.43+: _borrowed_senders is a dict[int, (_ExportState, MTProtoSender)]
    borrowed = getattr(client, "_borrowed_senders", None)
    if borrowed is not None and dc_id in borrowed:
        try:
            state, sender = borrowed[dc_id]
            try:
                await sender.disconnect()
            except Exception:
                pass
            # Mark as disconnected so need_connect() returns True if reused
            try:
                if hasattr(state, "mark_disconnected"):
                    state.mark_disconnected()
            except Exception:
                pass
        except Exception:
            pass
        try:
            del borrowed[dc_id]
        except Exception:
            pass
        invalidated = True
        print(f"[dc] invalidated cached sender for DC {dc_id} (via _borrowed_senders)")

    # Older Telethon fallback: _exported_senders on client._sender
    if not invalidated:
        try:
            sender = getattr(client, "_sender", None)
            if sender is not None:
                exported = getattr(sender, "_exported_senders", None)
                if exported is not None and dc_id in exported:
                    try:
                        exported[dc_id]._disconnect()
                    except Exception:
                        pass
                    try:
                        del exported[dc_id]
                    except Exception:
                        pass
                    invalidated = True
                    print(
                        f"[dc] invalidated cached sender for DC {dc_id} (via _sender._exported_senders)"
                    )
        except Exception:
            pass

    if not invalidated:
        print(f"[dc] _invalidate_dc_sender: no cached sender found for DC {dc_id}")


async def force_dc_connection(client, dc_id: int, msg=None):
    """Force a client to connect to a specific DC, used for DC error recovery.

    This is called when a download fails with a DC-related error.  It:
    1. Tries to borrow an exported sender for the target DC
    2. If a message is provided, verifies the connection with a real download
    3. Registers the client in _dc_client_map on success

    Returns True if the connection was VERIFIED with actual data transfer.
    """
    global _home_dc
    if _home_dc is None:
        _home_dc = _get_client_dc(_cfg.client)
    if dc_id == _home_dc:
        try:
            if not client.is_connected():
                await client.connect()
            return True
        except Exception as e:
            print(f"[dc] Home DC reconnection failed: {e}")
            return False

    # ALWAYS invalidate any cached sender first to force a fresh TCP connection.
    # Telethon's _borrow_exported_sender reuses cached senders that may
    # have dead TCP connections (half-open state where Telegram closed the
    # socket but Telethon's state._connected is still True).
    await _invalidate_dc_sender(client, dc_id)

    # Method 1: Borrow exported sender (forces auth export + NEW connection)
    try:
        sender = await client._borrow_exported_sender(dc_id)
        if sender is not None:
            await client._return_exported_sender(sender)
            # VERIFY: auth export succeeded, but actually download a chunk
            # to prove the TCP connection can transfer data.
            if msg and msg.media:
                ok = await _warmup_client_to_dc(client, dc_id, msg)
                if ok:
                    return True
                # Download verify failed — auth export created a sender but
                # the underlying TCP connection is dead.  Invalidate and
                # fall through to Method 2.
                print(
                    f"[dc] force_dc_connection: DC {dc_id} auth OK but download FAILED — invalidating sender"
                )
                await _invalidate_dc_sender(client, dc_id)
            else:
                # No message to verify with — trust auth export
                _register_dc_client(dc_id, client)
                return True
    except Exception as e:
        print(
            f"[dc] force_dc_connection: _borrow_exported_sender(DC {dc_id}) failed: {e}"
        )

    # Method 2: If we have a message on this DC, warm up by downloading
    if msg and msg.media:
        ok = await _warmup_client_to_dc(client, dc_id, msg)
        if ok:
            return True

    # Method 3: Try each pool client until one can connect
    if _cfg.client_pool:
        for alt_client in _cfg.client_pool:
            if alt_client is client:
                continue
            try:
                sender = await alt_client._borrow_exported_sender(dc_id)
                if sender is not None:
                    await alt_client._return_exported_sender(sender)
                    # Verify with download
                    if msg and msg.media:
                        ok = await _warmup_client_to_dc(alt_client, dc_id, msg)
                        if ok:
                            return True
                    else:
                        _register_dc_client(dc_id, alt_client)
                        return True
            except Exception:
                pass

    return False


# ── Bot API streaming (local server path) ─────────────────────────────────
# When USE_BOT_API is enabled and the local Bot API server is running,
# stream handlers route through botapi_stream.py instead of Telethon
# MTProto.  This eliminates file_reference expiration, invalid limit
# errors, and the 20MB download cap.
try:
    from botapi_stream import (
        botapi_vlc_stream as _botapi_vlc_stream,
        botapi_feed_ffmpeg as _botapi_feed_ffmpeg,
        botapi_resolve_stream as _botapi_resolve_stream,
        botapi_check_available as botapi_check_available,
        botapi_stats as botapi_stats,
        botapi_is_cached as _botapi_is_cached,
        botapi_background_download as _botapi_background_download,
        _botapi_file_cache,
    )

    _HAS_BOTAPI = True
except ImportError:
    _HAS_BOTAPI = False
    botapi_check_available = None
    botapi_stats = None
    _botapi_is_cached = None
    _botapi_background_download = None
    _botapi_file_cache = {}

# ═══════════════════════════════════════════════════════════════════════════════
# ROLLING STREAM BUFFER — unchanged from v1 (well-designed, no reason to change)
# ═══════════════════════════════════════════════════════════════════════════════


from simple_cache import (
    _cache as _cache_dict,
    _cache_lock as _cache_lock,
    _chunk_locks as _chunk_locks,
    _playhead as _playhead,
    _cache_put as _stream_put,
    _cache_get as _stream_get,
    _cache_evict as _stream_evict,
    update_playhead as _stream_update_playhead,
    _cache_clear_msg as _stream_clear_msg,
    _cache_clear_all as _stream_clear_all,
)


class _StreamCache:
    """In-memory rolling cache for streaming seeks.

    Keeps up to ~4 min of data behind the playhead so backward seeks are
    instant.  Data that VLC already received is cached as it streams; when
    the cache is full, chunks furthest behind the playhead are evicted
    (not the oldest-inserted — that would throw away the seek-back window).
    """

    _CHUNK_SIZE = 1 * 1024 * 1024  # 1 MB per chunk
    _CACHE_MAX_BYTES = int(
        os.environ.get("STREAMVAULT_CACHE_MAX_BYTES", str(4 * 1024 * 1024 * 1024))
    )  # 4.0 GB total cache limit
    _PER_VIDEO_MAX_DEFAULT = int(
        os.environ.get("STREAMVAULT_PER_VIDEO_MAX_CAP", str(4 * 1024 * 1024 * 1024))
    )  # 4.0 GB fallback
    _PER_VIDEO_MAX_CAP = int(
        os.environ.get("STREAMVAULT_PER_VIDEO_MAX_CAP", str(4 * 1024 * 1024 * 1024))
    )  # 4.0 GB ceiling per video
    _HEADER_PIN = 2 * 1024 * 1024  # First 2 MB always cached
    _TTL_SECONDS = 600  # 10 min TTL for inactive videos
    _ROLLBEHIND_S = 240  # Keep 4 min behind playhead for instant rewind
    _ACTIVE_AHEAD_PIN = 512 * 1024 * 1024  # Keep a large forward runway for 5 min ahead
    _MAX_CACHE_SERVE = 32 * 1024 * 1024  # Legacy compat

    def __init__(self):
        self._data = {}
        self._sizes = {}
        self._last_access = {}
        self._total_bytes = 0
        self._playhead = {}
        self._per_video_max = {}

    def repair_oversized_chunks(self):
        fixed = 0
        for msg_id, chunks in list(self._data.items()):
            for co in list(chunks):
                data = chunks[co]
                if len(data) > self._CHUNK_SIZE:
                    chunks[co] = data[: self._CHUNK_SIZE]
                    excess = len(data) - self._CHUNK_SIZE
                    self._sizes[msg_id] -= excess
                    self._total_bytes -= excess
                    fixed += 1
        if fixed:
            print(
                f"[stream_cache] repaired {fixed} oversized chunks, "
                f"now {self._total_bytes / 1024 / 1024:.1f} MB in use"
            )

    def _chunk_offset(self, byte_offset: int) -> int:
        return (byte_offset // self._CHUNK_SIZE) * self._CHUNK_SIZE

    def get(self, msg_id: int, offset: int, length: int) -> bytes | None:
        if msg_id not in self._data:
            return None
        chunks = self._data[msg_id]
        result = bytearray(length)
        pos = offset
        end = offset + length
        out_pos = 0
        while pos < end:
            co = self._chunk_offset(pos)
            chunk_data = chunks.get(co)
            if chunk_data is None:
                return None
            inner_off = pos - co
            raw_available = len(chunk_data) - inner_off
            available = min(raw_available, self._CHUNK_SIZE - inner_off)
            if available <= 0:
                return None
            take = min(end - pos, available)
            result[out_pos : out_pos + take] = chunk_data[inner_off : inner_off + take]
            out_pos += take
            pos += take
        self._last_access[msg_id] = _time.time()
        return bytes(result)

    def get_prefix(self, msg_id: int, offset: int, length: int) -> tuple[bytes, int]:
        if msg_id not in self._data or length <= 0:
            return (b"", length)
        chunks = self._data[msg_id]
        result = bytearray()
        pos = offset
        end = offset + length
        while pos < end:
            co = self._chunk_offset(pos)
            chunk_data = chunks.get(co)
            if chunk_data is None:
                break
            inner_off = pos - co
            raw_available = len(chunk_data) - inner_off
            available = min(raw_available, self._CHUNK_SIZE - inner_off)
            if available <= 0:
                break
            take = min(end - pos, available)
            result.extend(chunk_data[inner_off : inner_off + take])
            pos += take
        if result:
            self._last_access[msg_id] = _time.time()
        return (bytes(result), end - pos)

    def available_from(self, msg_id: int, offset: int, length: int) -> int:
        if msg_id not in self._data or length <= 0:
            return 0
        chunks = self._data[msg_id]
        pos = offset
        end = offset + length
        available_total = 0
        while pos < end:
            co = self._chunk_offset(pos)
            chunk_data = chunks.get(co)
            if chunk_data is None:
                break
            inner_off = pos - co
            raw_available = len(chunk_data) - inner_off
            available = min(raw_available, self._CHUNK_SIZE - inner_off)
            if available <= 0:
                break
            take = min(end - pos, available)
            available_total += take
            pos += take
        if available_total:
            self._last_access[msg_id] = _time.time()
        return available_total

    def put(self, msg_id: int, offset: int, data: bytes) -> None:
        if not data:
            return
        if msg_id not in self._data:
            self._data[msg_id] = OrderedDict()
            self._sizes[msg_id] = 0
        chunks = self._data[msg_id]
        pos = offset
        data_pos = 0
        while data_pos < len(data):
            co = self._chunk_offset(pos)
            inner_off = pos - co
            remaining_data = len(data) - data_pos
            space_in_chunk = self._CHUNK_SIZE - inner_off
            take = min(remaining_data, space_in_chunk)
            if co in chunks:
                existing = chunks[co]
                if len(existing) < self._CHUNK_SIZE:
                    new_end = inner_off + take
                    if inner_off > len(existing):
                        pass
                    elif new_end > len(existing):
                        new_chunk = bytearray(new_end)
                        new_chunk[: len(existing)] = existing
                        new_chunk[inner_off : inner_off + take] = data[
                            data_pos : data_pos + take
                        ]
                        added = len(new_chunk) - len(existing)
                        chunks[co] = bytes(new_chunk)
                        self._sizes[msg_id] += added
                        self._total_bytes += added
                    else:
                        mut = bytearray(existing)
                        mut[inner_off : inner_off + take] = data[
                            data_pos : data_pos + take
                        ]
                        chunks[co] = bytes(mut)
                    chunks.move_to_end(co)
                else:
                    chunks.move_to_end(co)
            else:
                if inner_off == 0:
                    chunk_len = min(self._CHUNK_SIZE, take)
                    new_chunk = data[data_pos : data_pos + chunk_len]
                    chunks[co] = bytes(new_chunk)
                    self._sizes[msg_id] += len(new_chunk)
                    self._total_bytes += len(new_chunk)
            pos += take
            data_pos += take
        self._last_access[msg_id] = _time.time()
        _vcap = self._per_video_max.get(msg_id, self._PER_VIDEO_MAX_DEFAULT)
        if self._sizes[msg_id] > _vcap:
            self._evict_video(msg_id, self._sizes[msg_id] - _vcap)
        if self._total_bytes > self._CACHE_MAX_BYTES:
            self._evict_global(self._total_bytes - self._CACHE_MAX_BYTES)

    def _protected_window(
        self, playhead: dict | None
    ) -> tuple[int | None, int | None, int | None]:
        if not playhead:
            return (None, None, None)
        playhead_byte = int(playhead.get("byte_pos", 0) or 0)
        duration_s = float(playhead.get("duration_s", 0) or 0)
        total_bytes = int(playhead.get("total_bytes", 0) or 0)
        if playhead_byte <= 0 or duration_s <= 0 or total_bytes <= 0:
            return (playhead_byte, 0, playhead_byte + self._ACTIVE_AHEAD_PIN)
        bytes_per_sec = total_bytes / duration_s
        seekback_start = max(0, playhead_byte - int(self._ROLLBEHIND_S * bytes_per_sec))
        active_ahead_end = min(total_bytes, playhead_byte + self._ACTIVE_AHEAD_PIN)
        return (playhead_byte, seekback_start, active_ahead_end)

    def _eviction_order(self, chunks: OrderedDict, playhead: dict | None) -> list[int]:
        candidates = [co for co in chunks if co >= self._HEADER_PIN]
        if not candidates:
            return []

        playhead_byte, seekback_start, active_ahead_end = self._protected_window(
            playhead
        )
        if playhead_byte is None:
            return sorted(candidates)

        far_ahead = [co for co in candidates if co > active_ahead_end]
        old_behind = [co for co in candidates if co < seekback_start]
        near_ahead = [
            co for co in candidates if playhead_byte <= co <= active_ahead_end
        ]
        protected_behind = [
            co for co in candidates if seekback_start <= co < playhead_byte
        ]

        return (
            sorted(far_ahead, reverse=True)
            + sorted(old_behind)
            + sorted(near_ahead, reverse=True)
            + sorted(protected_behind)
        )

    def _evict_video(self, msg_id: int, bytes_to_free: int) -> None:
        if msg_id not in self._data:
            return
        chunks = self._data[msg_id]
        playhead = self._playhead.get(msg_id)
        freed = 0
        for co in self._eviction_order(chunks, playhead):
            if freed >= bytes_to_free or co not in chunks:
                break
            data = chunks.pop(co)
            freed += len(data)
            self._sizes[msg_id] -= len(data)
            self._total_bytes -= len(data)

    def _evict_global(self, bytes_to_free: int) -> None:
        sorted_ids = sorted(self._last_access, key=lambda x: self._last_access[x])
        freed = 0
        for mid in sorted_ids:
            if freed >= bytes_to_free:
                break
            if mid not in self._data:
                continue
            chunks = self._data[mid]
            playhead = self._playhead.get(mid)
            for co in self._eviction_order(chunks, playhead):
                if freed >= bytes_to_free or co not in chunks:
                    break
                data = chunks.pop(co)
                freed += len(data)
                self._sizes[mid] -= len(data)
                self._total_bytes -= len(data)
            if not chunks:
                del self._data[mid]
                del self._sizes[mid]
                del self._last_access[mid]

    def cleanup_expired(self) -> None:
        now = _time.time()
        expired = [
            mid for mid, ts in self._last_access.items() if now - ts > self._TTL_SECONDS
        ]
        for mid in expired:
            if mid in self._data:
                self._total_bytes -= self._sizes[mid]
                del self._data[mid]
                del self._sizes[mid]
            del self._last_access[mid]
        if expired:
            print(
                f"[stream_cache] expired {len(expired)} inactive video(s), "
                f"{self._total_bytes / 1024 / 1024:.1f} MB in use"
            )

    def clear_video(self, msg_id: int) -> None:
        if msg_id in self._data:
            self._total_bytes -= self._sizes[msg_id]
            del self._data[msg_id]
            del self._sizes[msg_id]
        self._last_access.pop(msg_id, None)
        self._per_video_max.pop(msg_id, None)

    def update_playhead(
        self, msg_id: int, position_s: float, duration_s: float, total_bytes: int
    ):
        if total_bytes <= 0 or duration_s <= 0:
            return
        byte_pos = int((position_s / duration_s) * total_bytes)
        self._per_video_max[msg_id] = self._PER_VIDEO_MAX_CAP
        self._playhead[msg_id] = {
            "byte_pos": byte_pos,
            "duration_s": duration_s,
            "total_bytes": total_bytes,
            "updated_at": _time.time(),
        }

    def remove_playhead(self, msg_id: int):
        self._playhead.pop(msg_id, None)

    def stats(self) -> dict:
        return {
            "videos": len(self._data),
            "total_mb": round(self._total_bytes / 1024 / 1024, 1),
            "max_mb": round(self._CACHE_MAX_BYTES / 1024 / 1024, 1),
            "videos_cached": list(self._data.keys()),
            "playheads": {
                k: {"byte_pos": v["byte_pos"], "dur": round(v["duration_s"], 1)}
                for k, v in self._playhead.items()
            },
        }


_stream_cache = _StreamCache()
_stream_cache.repair_oversized_chunks()


class DiskCacheSession:
    """Persistent disk-backed byte cache for a single video file.

    Pre-allocates the full file on disk and writes downloaded chunks as they
    arrive from Telegram. Acts as a persistent L2 cache behind the in-memory
    _stream_cache, allowing seeks to be served from disk instantly after the
    first playthrough. Range maps are saved to a JSON sidecar so partial
    downloads survive app restarts.
    """

    def __init__(self, msg_id, file_size, filename):
        self.msg_id = msg_id
        self.file_size = file_size
        self.filename = filename
        self.filepath = os.path.join(_VLC_TMP_DIR, f"video_{msg_id}.tmp")
        self.ranges_path = self.filepath + ".json"
        self._io_lock = threading.Lock()  # guards file seek+read/write pairs
        self.ref_count = 0
        self.fully_cached = False

        os.makedirs(_VLC_TMP_DIR, exist_ok=True)

        # Restore persisted ranges from previous run
        self.ranges = self._load_ranges()

        if os.path.exists(self.filepath):
            self.file = open(self.filepath, "r+b")
        else:
            self.file = open(self.filepath, "w+b")

        self.fully_cached = self._check_fully_cached()
        if self.fully_cached:
            print(
                f"[disk_cache] msg={msg_id} FULLY CACHED on disk ({file_size // 1024 // 1024} MB)"
            )
        elif self.ranges:
            covered = sum(e - s for s, e in self.ranges)
            pct = covered * 100 // file_size if file_size else 0
            print(
                f"[disk_cache] msg={msg_id} resumed {len(self.ranges)} ranges ({pct}% of {file_size // 1024 // 1024} MB)"
            )

    # ── Range persistence ──────────────────────────────────────────────────

    def _load_ranges(self) -> list:
        """Load persisted byte ranges from the sidecar JSON file."""
        try:
            if os.path.exists(self.ranges_path):
                with open(self.ranges_path, "r") as f:
                    data = json.load(f)
                    return [tuple(r) for r in data.get("ranges", [])]
        except Exception:
            pass
        return []

    def _save_ranges(self) -> None:
        """Persist current byte ranges to the sidecar JSON file."""
        try:
            with open(self.ranges_path, "w") as f:
                json.dump({"ranges": self.ranges, "file_size": self.file_size}, f)
        except Exception:
            pass

    def _check_fully_cached(self) -> bool:
        """Return True if the entire file is already cached on disk."""
        if not self.ranges or self.file_size <= 0:
            return False
        for s, e in self.ranges:
            if s == 0 and e >= self.file_size:
                return True
        return False

    # ── Byte-range operations ──────────────────────────────────────────────

    def add_range(self, start, end):
        with self._io_lock:
            new_ranges = []
            inserted = False
            for s, e in self.ranges:
                if end < s:
                    if not inserted:
                        new_ranges.append((start, end))
                        inserted = True
                    new_ranges.append((s, e))
                elif start > e:
                    new_ranges.append((s, e))
                else:
                    start = min(start, s)
                    end = max(end, e)
            if not inserted:
                new_ranges.append((start, end))
            self.ranges = new_ranges
            if not self.fully_cached:
                self.fully_cached = self._check_fully_cached()
                if self.fully_cached:
                    print(f"[disk_cache] msg={self.msg_id} FULLY CACHED")
                    self._save_ranges()

    def get_available_length(self, offset, wanted):
        for s, e in self.ranges:
            if s <= offset < e:
                return min(wanted, e - offset)
        return 0

    def write(self, offset, data):
        if not data:
            return
        with self._io_lock:
            try:
                self.file.seek(offset)
                self.file.write(data)
            except Exception:
                return
        self.add_range(offset, offset + len(data))

    def read(self, offset, length) -> bytes:
        with self._io_lock:
            try:
                self.file.seek(offset)
                return self.file.read(length)
            except Exception:
                return b""

    async def async_write(self, offset, data) -> None:
        """Non-blocking disk write via thread-pool executor."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.write, offset, data)

    async def async_read(self, offset, length) -> bytes:
        """Non-blocking disk read via thread-pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.read, offset, length)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def close(self):
        try:
            self._save_ranges()
            self.file.close()
        except Exception:
            pass


_active_disk_sessions: dict[int, DiskCacheSession] = {}
_disk_session_lock = asyncio.Lock()
_VLC_TMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vlc_tmp")

_fetch_locks = {}

_client_locks = {}


def _get_client_lock(client):
    cid = id(client)
    if cid not in _client_locks:
        _client_locks[cid] = asyncio.Lock()
    return _client_locks[cid]


async def acquire_disk_session(
    msg_id: int, file_size: int, filename: str
) -> "DiskCacheSession | None":
    """Get or create a DiskCacheSession and increment its ref_count.

    Thread-safe. Multiple concurrent stream requests for the same msg_id
    share one DiskCacheSession so only one .tmp file exists per video.
    Returns None if the session could not be created (e.g. disk full).
    """
    async with _disk_session_lock:
        sess = _active_disk_sessions.get(msg_id)
        if sess is None:
            try:
                sess = DiskCacheSession(msg_id, file_size, filename)
                _active_disk_sessions[msg_id] = sess
            except Exception as e:
                print(f"[disk_cache] msg={msg_id} session create failed: {e}")
                return None
        sess.ref_count += 1
        return sess


async def release_disk_session(msg_id: int) -> None:
    """Decrement ref_count and persist ranges. Session stays alive for future reads.

    The .tmp file is only removed explicitly via route_clear_stream_cache or
    _clear_one, so partially-downloaded files survive VLC close/reconnect.
    """
    async with _disk_session_lock:
        sess = _active_disk_sessions.get(msg_id)
        if sess is None:
            return
        sess.ref_count = max(0, sess.ref_count - 1)
        # Always persist ranges so partial downloads survive app restarts
        try:
            sess._save_ranges()
        except Exception:
            pass


async def _fetch_and_cache(
    msg_id: int,
    chunk_offset: int,
    fetch_len: int,
    msg=None,
    client=None,
    max_retries: int = 8,
) -> int:
    """Download chunk and write to disk/cache. Retries on partial failure until full data is ready.

    CRITICAL: Uses per-chunk locking so concurrent callers (VLC + prefetcher)
    never download the same byte range twice. Retries on Telegram errors with
    backoff so the HTTP response to VLC is NEVER truncated mid-stream.
    """
    if fetch_len <= 0:
        return 0
    session = _active_disk_sessions.get(msg_id)
    if session:
        avail = session.get_available_length(chunk_offset, fetch_len)
        if avail >= fetch_len:
            return fetch_len
        key = (msg_id, chunk_offset)
        if key not in _fetch_locks:
            _fetch_locks[key] = asyncio.Lock()
        async with _fetch_locks[key]:
            # Re-check under lock (another task may have finished it)
            avail = session.get_available_length(chunk_offset, fetch_len)
            if avail >= fetch_len:
                _fetch_locks.pop(key, None)
                return fetch_len
            # Retry loop — keeps retrying until all bytes are written to disk
            delay = 1.0
            for attempt in range(max_retries):
                avail = session.get_available_length(chunk_offset, fetch_len)
                if avail >= fetch_len:
                    break
                # ALWAYS refetch the whole chunk to prevent corrupt frames!
                # If a previous attempt failed halfway, its partial write might be corrupted.
                # Overwriting the whole chunk ensures completely clean data.
                read_offset = chunk_offset
                read_len = fetch_len
                write_pos = read_offset
                try:
                    async for chunk in _tg_read(
                        msg_id, read_offset, read_len, client=client
                    ):
                        await session.async_write(write_pos, chunk)
                        write_pos += len(chunk)

                    # If tg_read returns early (e.g. invalid limit treated as EOF), check if it's natural EOF
                    if (
                        write_pos < chunk_offset + fetch_len
                        and write_pos < session.file_size
                    ):
                        raise ConnectionResetError("tg_read returned early before EOF")
                except ValueError as ve:
                    # If vlc_stream_handler timed out, it closes the session
                    if (
                        "closed file" in str(ve).lower()
                        or "i/o operation on closed file" in str(ve).lower()
                    ):
                        print(
                            f"[fetch_and_cache] aborting msg={msg_id} off={chunk_offset}: session closed"
                        )
                        break
                    print(
                        f"[fetch_and_cache] attempt={attempt+1}/{max_retries} msg={msg_id} off={chunk_offset}: {type(ve).__name__}: {ve}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay)
                        delay = min(delay * 1.5, 8.0)
                except Exception as e:
                    print(
                        f"[fetch_and_cache] attempt={attempt+1}/{max_retries} msg={msg_id} off={chunk_offset}: {type(e).__name__}: {e}"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(delay)
                        delay = min(delay * 1.5, 8.0)
            final_avail = session.get_available_length(chunk_offset, fetch_len)
            _fetch_locks.pop(key, None)
            return final_avail

    key = (msg_id, chunk_offset)
    cached_len = _stream_cache.available_from(msg_id, chunk_offset, fetch_len)
    if cached_len >= fetch_len:
        return fetch_len
    if key not in _fetch_locks:
        _fetch_locks[key] = asyncio.Lock()
    async with _fetch_locks[key]:
        cached_len = _stream_cache.available_from(msg_id, chunk_offset, fetch_len)
        if cached_len >= fetch_len:
            _fetch_locks.pop(key, None)
            return cached_len
        delay = 1.0
        fs = getattr(msg.media.document, "size", 0) if msg and msg.media else 0
        for attempt in range(max_retries):
            cached_len = _stream_cache.available_from(msg_id, chunk_offset, fetch_len)
            if cached_len >= fetch_len:
                break
            read_offset = chunk_offset
            read_len = fetch_len
            write_pos = read_offset
            try:
                async for chunk in _tg_read(
                    msg_id, read_offset, read_len, client=client
                ):
                    _stream_cache.put(msg_id, write_pos, chunk)
                    write_pos += len(chunk)
                if write_pos < chunk_offset + fetch_len and fs and write_pos < fs:
                    raise ConnectionResetError("tg_read returned early before EOF")
            except Exception as e:
                print(
                    f"[fetch_and_cache] attempt={attempt+1}/{max_retries} msg={msg_id} off={chunk_offset}: {type(e).__name__}: {e}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(delay)
                    delay = min(delay * 1.5, 8.0)
        final_cached = _stream_cache.available_from(msg_id, chunk_offset, fetch_len)
        _fetch_locks.pop(key, None)
        return final_cached


async def _fetch_chunk(msg_id: int, off: int, chunk_size: int, client=None) -> bytes:
    await _fetch_and_cache(msg_id, off, chunk_size, client=client)
    data, _ = _stream_cache.get_prefix(msg_id, off, chunk_size)
    return data


async def _stream_cache_cleanup_loop():
    while True:
        await asyncio.sleep(60)
        try:
            _stream_cache.cleanup_expired()
        except Exception as e:
            print(f"[stream_cache] cleanup error: {e}")


_TG_ALIGN = 4096  # Telegram requires limit to be a multiple of 4096


def _safe_read_size(offset: int, wanted: int, file_size: int) -> int:
    """Compute a Telegram-safe request_size for iter_download.

    Telegram requires all chunk limits to be a multiple of 4096.
    We round UP to the nearest multiple of 4096 so we can read all
    the way to the end of the file.
    """
    if file_size <= 0 or offset >= file_size:
        return 0
    max_available = file_size - offset
    size = min(wanted, max_available)
    # Round UP to the nearest 4096 multiple
    aligned = ((size + _TG_ALIGN - 1) // _TG_ALIGN) * _TG_ALIGN
    return aligned


def _adaptive_request_size(file_size: int) -> int:
    """Use Telethon's practical max request_size per chunk.

    MTProto supports 1 MB chunks, but Telethon clamps iter_download
    request_size to 512 KB. Matching this avoids hidden clamping and keeps
    window sizing predictable.
    """
    return 512 * 1024


# _TG_WINDOW_BLOCKS now sourced from config.py (env-tunable).

# ── RETRY CONFIG ──────────────────────────────────────────────────────────────
# _MAX_RETRIES / _RETRY_BASE_S / _RETRY_MAX_S / _CHUNK_READ_TIMEOUT /
# _CIRCUIT_BREAKER_THRESHOLD / _CIRCUIT_BREAKER_GLOBAL now sourced from
# config.py (env-tunable).


async def _tg_read(msg_or_id, offset: int, length: int | None, client=None):
    """Stream from Telegram, yielding chunks immediately as they arrive.

    Uses a single iter_download call per window with limit derived from the
    requested length so Telethon pipelines the full window in one MTProto
    context — no RTT gap between chunks. Each piece is yielded immediately
    (no accumulation buffer) so the HTTP response sees continuous data.

    DC-aware: If no client is provided, automatically selects the best client
    for the file's DC. When DC errors occur, retries with a different client
    and a fresh message reference.
    """
    if isinstance(msg_or_id, int):
        msg = await _get_msg(msg_or_id)
        _msg_id = msg_or_id
    else:
        msg = msg_or_id
        _msg_id = getattr(msg, "id", None)

    if not msg or not msg.media:
        print(f"[tg_read] msg={_msg_id} not found or has no media")
        return

    file_size = getattr(msg.media.document, "size", None) if msg.media else 0
    fs = file_size or 0
    req_size = _adaptive_request_size(fs)  # 512 KB (Telethon max chunk size)

    # DC-aware client selection: pick a client connected to the file's DC
    # NOTE: We do NOT override with bot_client here.  The forwarding mechanism
    # (_get_msg → _forward_to_bot_inbox) provides a fresh file_reference, but
    # the actual download should use a DC-aware user client that was properly
    # warmed up in preconnect_dcs().  The bot client is never warmed up for
    # non-home DCs, so using it for downloads guarantees DC connection failures.
    # The forwarded message's document (id, access_hash, dc_id) is identical to
    # the original — only the file_reference changes, which any client can use.
    if client is None:
        client = get_dc_aware_client(msg, _cfg.client)
    _client = client
    remaining = length
    pos = offset

    # Track current block_size across the outer while loop so "invalid limit"
    # reductions persist between windows (not just within one for-attempt loop).
    cur_block_size = req_size
    consecutive_failures = (
        0  # track consecutive ConnectionResetErrors for circuit breaker
    )
    total_failures = 0  # track total failures for this _tg_read call
    _window_count = 0  # ramp window size: small first window → fast first byte

    while remaining is None or remaining > 0:
        # Reset consecutive failure counter when we make forward progress
        if pos > offset:
            consecutive_failures = 0
        if fs and pos >= fs:
            break

        # Use full window from the first request — no ramp-up stutter.
        _effective_blocks = _TG_WINDOW_BLOCKS
        window = (
            min(cur_block_size * _effective_blocks, remaining)
            if remaining is not None
            else cur_block_size * _effective_blocks
        )
        safe_size = _safe_read_size(pos, window, fs) if fs else window
        if fs and safe_size <= 0:
            return

        # block_size must be a power of 2 and a multiple of 4096.
        # Near EOF safe_size < cur_block_size, so round UP to next power of 2
        # to ensure the request size is valid for Telegram while covering safe_size.
        if fs and safe_size < cur_block_size:
            # Round up to next power of 2
            p = 1 << (safe_size - 1).bit_length() if safe_size > 0 else _TG_ALIGN
            block_size = min(max(p, _TG_ALIGN), cur_block_size)
            n_blocks = 1
        else:
            block_size = cur_block_size
            n_blocks = max(1, min(safe_size // block_size, _TG_WINDOW_BLOCKS))

        for attempt in range(_MAX_RETRIES):
            try:
                if attempt > 0 and _msg_id is not None:
                    msg = await _get_msg(_msg_id)
                    if not msg or not msg.media:
                        print(f"[tg_read] retry resolution failed for msg={_msg_id}")
                        return

                window_sent = 0
                # BEFORE creating the iter_download generator, check if the
                # file's DC is healthy.  If not, invalidate the cached sender
                # so Telethon creates a FRESH connection on its own (bypassing
                # our pre-warmed sender which is known-dead).  Telethon's
                # internal _get_download_client() → _borrow_exported_sender()
                # will handle the re-export auth + new TCP connection.
                _file_dc = _get_file_dc(msg)
                if _file_dc and not _is_dc_healthy(_file_dc):
                    print(
                        f"[tg_read] DC {_file_dc} is unhealthy — clearing cached sender, letting Telethon auto-migrate"
                    )
                    await _invalidate_dc_sender(_client, _file_dc)
                    # Also try the main client as fallback — it may have a
                    # different route to the DC.
                    if _cfg.client and _cfg.client is not _client:
                        _client = _cfg.client
                generator = _client.iter_download(
                    msg, offset=pos, request_size=block_size, limit=n_blocks
                )
                try:
                    while True:
                        try:
                            # _CHUNK_READ_TIMEOUT seconds timeout per chunk read
                            piece = await asyncio.wait_for(
                                generator.__anext__(), timeout=_CHUNK_READ_TIMEOUT
                            )
                        except StopAsyncIteration:
                            break
                        if not isinstance(piece, bytes):
                            piece = bytes(piece)
                        if not piece:
                            continue
                        take = (
                            min(len(piece), remaining - window_sent)
                            if remaining is not None
                            else len(piece)
                        )
                        if take <= 0:
                            break
                        yield piece[:take]
                        window_sent += take
                        await asyncio.sleep(
                            0
                        )  # yield to selector — prevents starvation on Windows
                        if remaining is not None and window_sent >= remaining:
                            break
                except asyncio.TimeoutError:
                    print(
                        f"[tg_read] chunk read timeout at offset {pos} for msg={_msg_id}"
                    )
                    raise ConnectionResetError("Telegram read timed out")

                pos += window_sent
                if remaining is not None:
                    remaining -= window_sent
                if window_sent == 0:
                    # iter_download returned nothing — EOF
                    return
                # Dynamic DC registration: if this download succeeded,
                # record that this client can reach the file's DC
                _file_dc = _get_file_dc(msg)
                if _file_dc:
                    _register_dc_client(_file_dc, _client)
                    _mark_dc_success(_file_dc)
                # Reset failure counters on successful window
                consecutive_failures = 0
                total_failures = 0
                _window_count += 1
                break

            except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
                consecutive_failures += 1
                total_failures += 1

                # Track DC health for this file
                _file_dc = _get_file_dc(msg)
                if _file_dc:
                    _mark_dc_failure(_file_dc)

                # ── Circuit breaker: force DC reconnect after sustained failures ──
                if (
                    consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD
                    and _msg_id is not None
                ):
                    print(
                        f"[tg_read] circuit breaker: {consecutive_failures} consecutive failures at offset {pos} — forcing DC reconnect"
                    )
                    if _file_dc:
                        # Invalidate the stale exported sender for this DC
                        # so Telethon creates a fresh TCP connection.
                        # DO NOT call _client.disconnect() — that kills ALL
                        # connections including the home DC, making things worse.
                        await _invalidate_dc_sender(_client, _file_dc)
                        dc_ok = await force_dc_connection(_client, _file_dc, msg)
                        if dc_ok:
                            consecutive_failures = 0
                            _mark_dc_success(_file_dc)
                        else:
                            # force_dc_connection also failed — the DC is
                            # genuinely unreachable.  Mark unhealthy and
                            # switch to Telethon's auto-migration (fresh
                            # sender on next iter_download call).
                            print(
                                f"[tg_read] DC {_file_dc} reconnect failed — falling back to Telethon auto-migration"
                            )
                            if _cfg.client and _cfg.client is not _client:
                                _client = _cfg.client
                                print(
                                    f"[tg_read] switched to main client for DC {_file_dc} fallback"
                                )
                    # Also refresh the message reference (file_reference may be stale)
                    try:
                        _msg_cache.pop(_msg_id, None)
                        _msg_refresh_ts.pop(_msg_id, None)
                        _fwd_inbox_id.pop(_msg_id, None)
                        fresh = await _get_msg(_msg_id)
                        if fresh and fresh.media:
                            msg = fresh
                    except Exception:
                        pass

                # ── Global failure cap: give up entirely ────────────────────
                if total_failures >= _CIRCUIT_BREAKER_GLOBAL:
                    print(
                        f"[tg_read] giving up after {total_failures} total failures at offset {pos}: {exc}"
                    )
                    return

                if attempt < _MAX_RETRIES - 1:
                    delay = min(_RETRY_BASE_S * (2 ** min(attempt, 4)), _RETRY_MAX_S)
                    import random as _rng

                    jitter = _rng.uniform(0, delay * 0.3)
                    print(
                        f"[tg_read] connection reset at offset {pos}, retry {attempt + 1}/{_MAX_RETRIES} in {delay:.1f}s (streak={consecutive_failures})"
                    )
                    await asyncio.sleep(delay + jitter)
                    continue
                print(
                    f"[tg_read] giving up after {_MAX_RETRIES} retries at offset {pos}: {exc}"
                )
                return

            except Exception as exc:
                err_str = str(exc).lower()
                err_cls = type(exc).__name__.lower()

                # ── DC-specific errors ─────────────────────────────────────
                # FileMigrateError, FileIdInvalidError, wrong DC, etc.
                # v3 FIX: Instead of just refreshing the message and hoping
                # a different client works, we EXPLICITLY force a DC connection
                # to the file's DC before retrying.
                _is_dc_error = any(
                    kw in err_str
                    for kw in (
                        "file migrate",
                        "filemigrate",
                        "wrong dc",
                        "dc_id",
                        "file_id_invalid",
                        "fileidinvalid",
                        "file_id invalid",
                        "location invalid",
                        "locationinvalid",
                    )
                ) or any(
                    kw in err_cls
                    for kw in (
                        "filemigrate",
                        "fileidinvalid",
                        "locationinvalid",
                    )
                )

                if _is_dc_error and _msg_id is not None:
                    print(
                        f"[tg_read] DC error at offset {pos} for msg={_msg_id}: {exc}"
                    )

                    # Step 1: Refresh message to get fresh file_reference
                    _msg_cache.pop(_msg_id, None)
                    _msg_refresh_ts.pop(_msg_id, None)
                    _fwd_inbox_id.pop(_msg_id, None)
                    fresh = None
                    try:
                        fresh = await _get_msg(_msg_id)
                    except Exception as _re:
                        print(f"[tg_read] DC recovery msg refresh failed: {_re}")

                    if fresh and fresh.media:
                        msg = fresh
                        target_dc = _get_file_dc(msg)
                        print(
                            f"[tg_read] DC recovery: msg={_msg_id} file_dc={target_dc}, attempting forced DC connection"
                        )

                        # Step 2: Force the CURRENT client to connect to the file's DC
                        if target_dc:
                            dc_ok = await force_dc_connection(_client, target_dc, msg)
                            if dc_ok:
                                print(
                                    f"[tg_read] DC recovery: client now connected to DC {target_dc}, retrying offset {pos}"
                                )
                                continue

                        # Step 3: Try a DC-aware client (may already have a connection)
                        dc_client = get_dc_aware_client(msg, None)
                        if dc_client and dc_client is not _client:
                            _client = dc_client
                            target_dc_2 = _get_file_dc(msg)
                            if target_dc_2:
                                dc_ok = await force_dc_connection(
                                    _client, target_dc_2, msg
                                )
                                if dc_ok:
                                    print(
                                        f"[tg_read] DC recovery: alt client connected to DC {target_dc_2}, retrying offset {pos}"
                                    )
                                    continue
                            print(
                                f"[tg_read] DC recovery: switched to DC-aware client, retrying offset {pos}"
                            )
                            continue

                    # Step 4: Try alternate pool clients with forced DC connection
                    if _cfg.client_pool:
                        target_dc = _get_file_dc(msg) if msg and msg.media else None
                        for alt_client in _cfg.client_pool:
                            if alt_client is _client:
                                continue
                            if target_dc:
                                dc_ok = await force_dc_connection(
                                    alt_client, target_dc, msg
                                )
                                if dc_ok:
                                    _client = alt_client
                                    print(
                                        f"[tg_read] DC recovery: pool client connected to DC {target_dc}, retrying offset {pos}"
                                    )
                                    break
                            else:
                                _client = alt_client
                                print(
                                    f"[tg_read] DC recovery: switching to pool client (no DC info), retrying offset {pos}"
                                )
                                break
                        else:
                            # None of the pool clients could connect either
                            if attempt < _MAX_RETRIES - 1:
                                delay = min(_RETRY_BASE_S * (2**attempt), _RETRY_MAX_S)
                                await asyncio.sleep(delay)
                                continue
                            print(
                                f"[tg_read] DC recovery failed after all retries at offset {pos}: {exc}"
                            )
                            return
                        continue  # retry with the new client

                    if attempt < _MAX_RETRIES - 1:
                        delay = min(_RETRY_BASE_S * (2**attempt), _RETRY_MAX_S)
                        await asyncio.sleep(delay)
                        continue
                    print(
                        f"[tg_read] DC recovery failed after all retries at offset {pos}: {exc}"
                    )
                    return

                # ── File reference expired ─────────────────────────────────
                # Telethon raises either FileReferenceExpiredError (proper class)
                # or a generic BadRequestError with FILE_REFERENCE_X_EXPIRED.
                # The class-name check catches both paths regardless of how
                # Telethon normalizes the error string.
                _is_fileref_error = (
                    "file reference" in err_str
                    or "filereference" in err_str
                    or "file_reference" in err_str
                    or err_cls in ("filereferenceexpirederror", "badrequesterror")
                    or "FILE_REFERENCE" in str(exc)
                )
                if _is_fileref_error and _msg_id is not None:
                    print(
                        f"[tg_read] file ref expired at offset {pos}, refreshing msg={_msg_id}"
                    )
                    _msg_cache.pop(_msg_id, None)
                    _msg_refresh_ts.pop(_msg_id, None)
                    _fwd_inbox_id.pop(
                        _msg_id, None
                    )  # force re-forward on next _get_msg
                    try:
                        fresh = await _get_msg(_msg_id)
                        if fresh and fresh.media:
                            msg = fresh
                            # Also force DC connection in case ref expired
                            # due to DC connection issue
                            target_dc = _get_file_dc(msg)
                            if target_dc:
                                await force_dc_connection(_client, target_dc, msg)
                            _client = get_dc_aware_client(msg, _client)
                            print(
                                f"[tg_read] msg={_msg_id} refreshed (dc={target_dc}), retrying offset {pos}"
                            )
                            continue
                    except Exception as _re:
                        print(f"[tg_read] msg refresh failed: {_re}")
                    print(f"[tg_read] fatal error at offset {pos}: {exc}")
                    return

                # ── Invalid limit ──────────────────────────────────────────
                if "invalid limit" in err_str:
                    if block_size <= _TG_ALIGN:
                        print(
                            f"[tg_read] invalid limit at offset {pos} with min block — treating as EOF"
                        )
                        return
                    # Near EOF: if within one req_size of reported file size,
                    # actual file is slightly smaller than metadata says — bail immediately.
                    if fs > 0 and (fs - pos) <= req_size:
                        print(
                            f"[tg_read] invalid limit at offset {pos} near EOF (fs={fs}) — treating as EOF"
                        )
                        return
                    new_block = max(_TG_ALIGN, block_size // 2)
                    block_size = new_block
                    n_blocks = max(1, min(safe_size // block_size, _TG_WINDOW_BLOCKS))
                    cur_block_size = block_size  # persist reduction across windows
                    print(
                        f"[tg_read] invalid limit at offset {pos}, reducing block to {block_size}"
                    )
                    continue

                # ── Generic retry with client rotation ─────────────────────
                # This catches ALL remaining exceptions that weren't handled by
                # the specific handlers above (DC errors, file_ref, invalid limit).
                # Examples: FloodWaitError, ChatWriteForbiddenError from bot
                # forwarding, TimeoutError from Telethon internals, etc.
                total_failures += 1
                if total_failures >= _CIRCUIT_BREAKER_GLOBAL:
                    print(
                        f"[tg_read] giving up after {total_failures} total failures at offset {pos}: {exc}"
                    )
                    return

                if attempt < _MAX_RETRIES - 1:
                    delay = min(_RETRY_BASE_S * (2**attempt), _RETRY_MAX_S)
                    # Always invalidate message cache on unknown errors — the
                    # file_reference might be stale even if the error message
                    # doesn't say so (Telethon sometimes wraps the real error).
                    if attempt > 0 and _msg_id is not None:
                        _msg_cache.pop(_msg_id, None)
                        _msg_refresh_ts.pop(_msg_id, None)
                        _fwd_inbox_id.pop(_msg_id, None)
                    # On retry, try a different client from the pool
                    # (the current client might have a stale DC connection)
                    if _cfg.client_pool and attempt > 0:
                        alt_client = _cfg.client_pool[
                            (attempt + 1) % len(_cfg.client_pool)
                        ]
                        if alt_client is not _client:
                            _client = alt_client
                            # Also invalidate DC sender for the new client
                            _file_dc = _get_file_dc(msg)
                            if _file_dc:
                                await _invalidate_dc_sender(_client, _file_dc)
                            print(
                                f"[tg_read] retrying with alternate client at offset {pos}"
                            )
                    await asyncio.sleep(delay)
                    continue
                print(f"[tg_read] fatal error at offset {pos}: {exc}")
                return


def _next_pool_client():
    """Return the next client from the pool (round-robin).

    NOTE: For streaming, callers should pin a client once at stream start
    (pinned_client = _next_pool_client()) and pass it to all _tg_read calls
    for that stream, rather than calling this per-chunk. This prevents two
    concurrent streams from sharing the same MTProto connection mid-flight.
    """
    global _pool_idx
    pool = _get_bg_pool()
    if not pool:
        return _cfg.client
    client = pool[_pool_idx % len(pool)]
    _pool_idx += 1
    return client


# ═══════════════════════════════════════════════════════════════════════════════
# UNIFIED PREFETCHER
# ═══════════════════════════════════════════════════════════════════════════════
# Replaces three separate systems:
#   - _prewarm_resume (one-shot pre-fetch at resume position)
#   - _lookahead_prefetch (one-shot pre-fetch ahead of high-water mark)
#   - _playhead_prefetcher (continuous loop fetching ahead of playhead)
#
# The new unified prefetcher does ALL of this in one loop:
#   1. On first cycle: pre-warm at the resume/start position (if provided)
#   2. Every cycle: find gaps ahead of the playhead and fill them
#   3. Stop when VLC closes (playhead removed)

# _PREFETCH_AHEAD_S / _PREFETCH_CHUNK / _ACTIVE_READ_ZONE now sourced from
# config.py (env-tunable).
_PREFETCH_LANES = 1  # Always single-lane sequential — never split bandwidth
_active_prefetchers: set[int] = set()
_active_prefetcher_tasks: dict[int, asyncio.Task] = {}
_pool_idx: int = 0  # round-robin counter; must be initialised before first call

# Progressive download mode for VLC: aggressively fill cache from the
# requested start offset while VLC reads from that cache.
_PROGRESSIVE_VLC_ENABLED = True
# _PROGRESSIVE_SEGMENT / _PROGRESSIVE_MAX_LANES / _PROGRESSIVE_STALL_FALLBACK_S
# now sourced from config.py (env-tunable).
_PROGRESSIVE_POLL_S = 0.005
_PROGRESSIVE_IDLE_STOP_S = 4.0
_PROGRESSIVE_SEEK_RESTART_BYTES = 4 * 1024 * 1024

_progressive_tasks: dict[int, asyncio.Task] = {}
_progressive_stop_tasks: dict[int, asyncio.Task] = {}
_progressive_consumers: dict[int, int] = {}
_progressive_seed_start: dict[int, int] = {}


def _cancel_prefetch_task(msg_id: int) -> None:
    task = _active_prefetcher_tasks.pop(msg_id, None)
    if task and not task.done():
        task.cancel()
    _active_prefetchers.discard(msg_id)


def _cancel_progressive_task(msg_id: int, clear_consumers: bool = True) -> None:
    stop_task = _progressive_stop_tasks.pop(msg_id, None)
    if stop_task and not stop_task.done():
        stop_task.cancel()
    task = _progressive_tasks.pop(msg_id, None)
    if task and not task.done():
        task.cancel()
    if clear_consumers:
        _progressive_consumers.pop(msg_id, None)
    _progressive_seed_start.pop(msg_id, None)


def _cancel_progressive_others(active_msg_id: int) -> None:
    for mid in list(_progressive_tasks.keys()):
        if mid != active_msg_id:
            _cancel_progressive_task(mid)
            _cancel_prefetch_task(mid)


def _progressive_consumer_enter(msg_id: int) -> None:
    _progressive_consumers[msg_id] = _progressive_consumers.get(msg_id, 0) + 1
    stop_task = _progressive_stop_tasks.pop(msg_id, None)
    if stop_task and not stop_task.done():
        stop_task.cancel()


def _progressive_consumer_exit(msg_id: int) -> None:
    cur = _progressive_consumers.get(msg_id, 0)
    if cur <= 1:
        _progressive_consumers[msg_id] = 0

        async def _stop_after_idle():
            try:
                await asyncio.sleep(_PROGRESSIVE_IDLE_STOP_S)
                if _progressive_consumers.get(msg_id, 0) <= 0:
                    _cancel_progressive_task(msg_id)
                    _cancel_prefetch_task(msg_id)
                    print(f"[progdl] msg={msg_id} stopped after idle")
            except asyncio.CancelledError:
                return

        _progressive_stop_tasks[msg_id] = asyncio.create_task(_stop_after_idle())
    else:
        _progressive_consumers[msg_id] = cur - 1


async def _progressive_download_loop(
    msg_id: int,
    start_offset: int,
    total_bytes: int,
    msg,
    pinned_client,
):
    """Continuously pre-download forward bytes into stream cache for VLC."""
    if total_bytes <= 0:
        return

    cursor = max(
        0, (start_offset // _stream_cache._CHUNK_SIZE) * _stream_cache._CHUNK_SIZE
    )
    dc_id = _get_file_dc(msg)
    bg_pool = _get_bg_pool()
    if dc_id and dc_id in _dc_client_map:
        clients_for_video = [c for c in _dc_client_map[dc_id] if c in bg_pool]
    else:
        clients_for_video = []

    if not clients_for_video:
        clients_for_video = bg_pool

    pool = clients_for_video
    lanes = max(1, min(_PROGRESSIVE_MAX_LANES, len(pool)))

    in_flight: dict[int, tuple[asyncio.Task, int]] = {}
    pool_rr = 0
    current_task = asyncio.current_task()
    print(
        f"[progdl] msg={msg_id} start={start_offset} "
        f"lanes={lanes} seg={_PROGRESSIVE_SEGMENT // 1024 // 1024}MB"
    )

    try:
        while True:
            if _progressive_tasks.get(msg_id) is not current_task:
                break

            progressive_ceiling = total_bytes
            # When a disk session is active, bypass the in-memory ceiling and
            # download the full file at maximum speed — exactly like YouTube's
            # background buffering which fills the entire video to disk.
            _has_disk_sess = _active_disk_sessions.get(msg_id) is not None
            if not _has_disk_sess:
                ph = _stream_cache._playhead.get(msg_id)
                if ph:
                    ph_total = int(ph.get("total_bytes", total_bytes) or total_bytes)
                    ph_duration = float(ph.get("duration_s", 0) or 0)
                    if ph_total > 0 and ph_duration > 0:
                        bytes_per_sec = ph_total / ph_duration
                        # YouTube-style: only prefetch ahead of current playhead
                        progressive_ceiling = min(
                            total_bytes,
                            ph["byte_pos"] + int(_PREFETCH_AHEAD_S * bytes_per_sec),
                            ph["byte_pos"]
                            + _stream_cache._PER_VIDEO_MAX_CAP
                            - 64 * 1024 * 1024,
                        )
                        progressive_ceiling = max(progressive_ceiling, start_offset)

            while len(in_flight) < lanes and cursor < progressive_ceiling:
                seg_len = min(_PROGRESSIVE_SEGMENT, progressive_ceiling - cursor)
                if seg_len <= 0:
                    break
                lane_client = pool[pool_rr % len(pool)]
                pool_rr += 1
                t = asyncio.create_task(
                    _fetch_and_cache(msg_id, cursor, seg_len, client=lane_client)
                )
                in_flight[cursor] = (t, seg_len)
                cursor += seg_len

            if not in_flight:
                if _progressive_consumers.get(msg_id, 0) <= 0:
                    await asyncio.sleep(_PROGRESSIVE_POLL_S)
                    continue
                await asyncio.sleep(_PROGRESSIVE_POLL_S)
                continue

            done, _ = await asyncio.wait(
                {task for task, _seg_len in in_flight.values()},
                timeout=0.1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue

            for off, (t, seg_len) in list(in_flight.items()):
                if t in done:
                    ready = 0
                    try:
                        ready = int(await t or 0)
                    except Exception as e:
                        print(f"[progdl] msg={msg_id} offset={off} lane error: {e}")
                    finally:
                        in_flight.pop(off, None)

                    if ready < seg_len:
                        missing_at = off + max(0, ready)
                        for other_off, (other_t, _other_len) in list(in_flight.items()):
                            if other_off > off:
                                if not other_t.done():
                                    other_t.cancel()
                                in_flight.pop(other_off, None)
                        cursor = min(cursor, missing_at)
                        await asyncio.sleep(0.15)
                        break

            if cursor >= total_bytes and not in_flight:
                print(f"[progdl] msg={msg_id} fully downloaded to cache, exiting loop")
                break

    except asyncio.CancelledError:
        pass
    finally:
        for t, _seg_len in list(in_flight.values()):
            if not t.done():
                t.cancel()
        if _progressive_tasks.get(msg_id) is current_task:
            _progressive_tasks.pop(msg_id, None)
        print(f"[progdl] msg={msg_id} loop ended")


def _ensure_progressive_downloader(
    msg_id: int,
    start_offset: int,
    total_bytes: int,
    msg,
    pinned_client,
) -> None:
    if not _PROGRESSIVE_VLC_ENABLED or total_bytes <= 0:
        return

    _cancel_progressive_others(msg_id)

    seed = _progressive_seed_start.get(msg_id)
    task = _progressive_tasks.get(msg_id)
    should_restart = (
        seed is None
        or abs(start_offset - seed) >= _PROGRESSIVE_SEEK_RESTART_BYTES
        or task is None
        or task.done()
    )
    if not should_restart:
        return

    _cancel_progressive_task(msg_id, clear_consumers=False)
    _progressive_seed_start[msg_id] = start_offset
    _progressive_tasks[msg_id] = asyncio.create_task(
        _progressive_download_loop(
            msg_id, start_offset, total_bytes, msg, pinned_client
        )
    )


async def _prefetch_loop(
    msg_id: int,
    total_bytes: int,
    duration_s: float = 0.0,
    resume_pos_s: float = 0.0,
    pinned_client=None,
    msg=None,
):
    """Unified background prefetch loop.

    Started when VLC opens a video.  Runs until the playhead is removed
    (VLC closes) or we reach EOF.  Fills gaps ahead of the playhead
    so forward seeks are instant.
    """
    global _pool_idx
    current_task = asyncio.current_task()
    if (
        msg_id in _active_prefetcher_tasks
        and _active_prefetcher_tasks[msg_id] != current_task
    ):
        old_task = _active_prefetcher_tasks[msg_id]
        if not old_task.done():
            print(f"[prefetch] msg={msg_id} cancelling old prefetcher task")
            old_task.cancel()
            try:
                await old_task
            except (asyncio.CancelledError, Exception):
                pass

    _active_prefetcher_tasks[msg_id] = current_task
    _active_prefetchers.add(msg_id)
    print(f"[prefetch] msg={msg_id} started unified prefetcher")
    if msg is None:
        try:
            msg = await _get_msg(msg_id)
        except Exception:
            pass

    active_ranges = {}  # dict of Task -> (start_offset, end_offset)

    async def _fetch_lane(fs: int, fetch_len: int, lane_client):
        try:
            await _fetch_and_cache(msg_id, fs, fetch_len, client=lane_client)
        except Exception as e:
            print(f"[prefetch] msg={msg_id} lane offset={fs} error: {e}")

    try:
        # Phase 1: Pre-warm at resume position (if any)
        if resume_pos_s > 0 and duration_s > 0 and total_bytes > 0:
            resume_byte = int((resume_pos_s / duration_s) * total_bytes)
            if resume_byte > 0 and resume_byte < total_bytes:
                prewarm_start = _stream_cache._chunk_offset(resume_byte)
                # Check if already cached
                already = False
                session = _active_disk_sessions.get(msg_id)
                if session:
                    if (
                        session.get_available_length(prewarm_start, 8 * 1024 * 1024)
                        >= 8 * 1024 * 1024
                    ):
                        already = True
                elif (
                    msg_id in _stream_cache._data
                    and prewarm_start in _stream_cache._data[msg_id]
                ):
                    already = True

                if not already:
                    fetch_len = _safe_read_size(
                        prewarm_start,
                        min(8 * 1024 * 1024, total_bytes - prewarm_start),
                        total_bytes,
                    )
                    if fetch_len > 0:
                        print(
                            f"[prefetch] msg={msg_id} prewarm at offset={prewarm_start} "
                            f"len={fetch_len} resume={resume_pos_s:.1f}s"
                        )
                        try:
                            prewarm_client = pool[0] if pool else _cfg.client
                            await _fetch_and_cache(
                                msg_id, prewarm_start, fetch_len, client=prewarm_client
                            )
                        except Exception as e:
                            print(f"[prefetch] msg={msg_id} prewarm failed: {e}")

        # Phase 2: Sequential prefetch ahead of playhead — YouTube style.
        # One download at a time, always from the next uncached chunk just
        # ahead of the playhead, ceiling capped at PREFETCH_AHEAD_S seconds.
        # This concentrates 100% of bandwidth on data VLC needs soonest.
        if msg_id not in _stream_cache._playhead and total_bytes > 0:
            _stream_cache.update_playhead(
                msg_id,
                resume_pos_s if resume_pos_s > 0 else 0.0,
                duration_s if duration_s > 0 else 1.0,
                total_bytes,
            )

        active_fetch: asyncio.Task | None = None
        active_fetch_end: int = 0

        while True:
            # Stop only if playhead was explicitly removed (VLC closed)
            ph = _stream_cache._playhead.get(msg_id)
            if ph is None:
                if active_fetch and not active_fetch.done():
                    active_fetch.cancel()
                break

            ph_total = ph.get("total_bytes", total_bytes)
            if ph_total <= 0:
                break

            # 30-second lookahead ceiling — YouTube-style tight window
            ph_duration = ph.get("duration_s", duration_s) or 1.0
            bytes_per_sec = ph_total / ph_duration
            prefetch_ceiling = min(
                ph["byte_pos"] + int(_PREFETCH_AHEAD_S * bytes_per_sec),
                ph_total,
                ph["byte_pos"] + _stream_cache._PER_VIDEO_MAX_CAP - 32 * 1024 * 1024,
            )

            # Pick client each cycle (DC-aware)
            dc_id = _get_file_dc(msg) if msg else None
            bg_pool = _get_bg_pool()
            if dc_id and dc_id in _dc_client_map:
                clients_for_video = [c for c in _dc_client_map[dc_id] if c in bg_pool]
            else:
                clients_for_video = []
            if not clients_for_video:
                clients_for_video = bg_pool
            if len(clients_for_video) > 1 and pinned_client is not None:
                pool = [c for c in clients_for_video if c is not pinned_client]
            else:
                pool = clients_for_video

            cs = _stream_cache._CHUNK_SIZE

            # Reap completed fetch
            if active_fetch is not None and active_fetch.done():
                active_fetch = None
                active_fetch_end = 0

            # If a fetch is already in-flight, just wait for it
            if active_fetch is not None:
                await asyncio.sleep(0.01)  # 10 ms poll — 100 completions/sec
                continue

            # Find the next uncached chunk just ahead of the playhead
            active_zone = max(2 * 1024 * 1024, int(2 * bytes_per_sec))
            scan_start = ((ph["byte_pos"] + active_zone) // cs) * cs
            next_gap: int | None = None
            for co in range(scan_start, int(prefetch_ceiling), cs):
                session = _active_disk_sessions.get(msg_id)
                if session:
                    if session.get_available_length(co, cs) >= cs:
                        continue
                elif (
                    msg_id in _stream_cache._data and co in _stream_cache._data[msg_id]
                ):
                    continue
                next_gap = co
                break

            if next_gap is None:
                # All caught up to ceiling — sleep until playhead advances
                await asyncio.sleep(0.5)
                continue

            # Launch one sequential fetch for the next gap
            fetch_len = _safe_read_size(
                next_gap,
                min(_PREFETCH_CHUNK, int(prefetch_ceiling) - next_gap),
                ph_total,
            )
            if fetch_len <= 0:
                await asyncio.sleep(0.1)
                continue

            lane_client = pool[_pool_idx % len(pool)]
            _pool_idx = (_pool_idx + 1) % len(pool)
            active_fetch_end = next_gap + fetch_len

            async def _fetch_lane(fs: int, fl: int, lc):
                try:
                    await _fetch_and_cache(msg_id, fs, fl, client=lc)
                except Exception as e:
                    print(f"[prefetch] msg={msg_id} lane offset={fs} error: {e}")

            active_fetch = asyncio.create_task(
                _fetch_lane(next_gap, fetch_len, lane_client)
            )
            await asyncio.sleep(0.01)  # 10 ms — yield but come back fast

    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"[prefetch] msg={msg_id} loop error: {e}")
    finally:
        # Cancel any in-flight sequential fetch
        if active_fetch is not None and not active_fetch.done():
            active_fetch.cancel()
        if _active_prefetcher_tasks.get(msg_id) == current_task:
            _active_prefetcher_tasks.pop(msg_id, None)
            _active_prefetchers.discard(msg_id)
        print(f"[prefetch] msg={msg_id} stopped")


# Legacy compat wrappers (routes.py imports these)
async def _prewarm_resume(msg_id, resume_pos_s, duration_s, total_bytes):
    """One-shot pre-fetch at the resume byte position.

    Called by routes.py when VLC is launched with --start-time.
    Fetches 8 MB at the resume offset so VLC's first seek finds data
    already in the rolling cache — eliminates initial buffer.
    """
    if total_bytes <= 0 or duration_s <= 0 or resume_pos_s <= 0:
        return

    byte_offset = int((resume_pos_s / duration_s) * total_bytes)
    if byte_offset <= 0:
        return
    prewarm_start = _stream_cache._chunk_offset(byte_offset)

    # Don't prewarm if already cached
    session = _active_disk_sessions.get(msg_id)
    if session:
        if (
            session.get_available_length(prewarm_start, 8 * 1024 * 1024)
            >= 8 * 1024 * 1024
        ):
            return
    elif msg_id in _stream_cache._data:
        if prewarm_start in _stream_cache._data[msg_id]:
            return

    fetch_size = _safe_read_size(prewarm_start, 8 * 1024 * 1024, total_bytes)
    if fetch_size <= 0:
        return

    print(f"[prewarm] msg={msg_id} offset={prewarm_start} size={fetch_size}")
    try:
        await _fetch_and_cache(
            msg_id, prewarm_start, fetch_size, client=_next_pool_client()
        )
    except Exception as e:
        print(f"[prewarm] msg={msg_id} failed: {e}")


async def _stream_cache_prewarm_offset(msg_id, byte_offset, size, total_bytes):
    """One-shot pre-fetch at an explicit byte offset (no duration needed)."""
    if total_bytes <= 0 or byte_offset < 0:
        return
    prewarm_start = _stream_cache._chunk_offset(byte_offset)

    session = _active_disk_sessions.get(msg_id)
    if session:
        if session.get_available_length(prewarm_start, size) >= size:
            return
    elif msg_id in _stream_cache._data:
        if prewarm_start in _stream_cache._data[msg_id]:
            return

    fetch_size = _safe_read_size(
        prewarm_start, size + (byte_offset - prewarm_start), total_bytes
    )
    if fetch_size <= 0:
        return

    print(f"[prewarm] msg={msg_id} offset={prewarm_start} size={fetch_size}")
    try:
        await _fetch_and_cache(
            msg_id, prewarm_start, fetch_size, client=_next_pool_client()
        )
    except Exception as e:
        print(f"[prewarm] msg={msg_id} failed: {e}")


async def _playhead_prefetcher(msg_id, total_bytes):
    """Start the unified prefetch loop for continuous playhead-ahead fetching."""
    # Get duration from playhead if available, otherwise from cache
    ph = _stream_cache._playhead.get(msg_id)
    duration_s = ph.get("duration_s", 0) if ph else 0
    await _prefetch_loop(msg_id, total_bytes, duration_s=duration_s, resume_pos_s=0)


def stop_prefetcher(msg_id: int):
    _active_prefetchers.discard(msg_id)


# ═══════════════════════════════════════════════════════════════════════════════
# FFmpeg & PROBING
# ═══════════════════════════════════════════════════════════════════════════════

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
if _FFMPEG:
    print(f"[stream] ffmpeg found: {_FFMPEG}")
else:
    print("[stream] ffmpeg NOT found — direct pass-through only")


def _needs_ffmpeg(mime: str, filename: str, codec: str = "") -> bool:
    if not _FFMPEG:
        return False
    ext = os.path.splitext(filename)[1].lower()
    if ext in {".webm", ".ogg"} or mime in {"video/webm", "video/ogg"}:
        return False
    return True


# ── ENTITY / MESSAGE CACHE ────────────────────────────────────────────────────
_entity_cache = None
_bot_entity_cache = None


async def _get_entity():
    global _entity_cache
    if _entity_cache is None:
        _entity_cache = await _cfg.client.get_entity(
            int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID
        )
    return _entity_cache


async def _get_bot_entity():
    """Resolve channel entity via bot client (cached). Falls back to user client."""
    global _bot_entity_cache
    bc = _cfg.bot_client
    if bc is None:
        return await _get_entity()
    if _bot_entity_cache is None:
        try:
            _bot_entity_cache = await bc.get_entity(
                int(CHANNEL_ID) if CHANNEL_ID.lstrip("-").isdigit() else CHANNEL_ID
            )
        except Exception as e:
            print(f"[bot] get_entity failed ({e}), falling back to user client")
            return await _get_entity()
    return _bot_entity_cache


_msg_cache: OrderedDict = OrderedDict()
_MSG_CACHE_MAX = 100
_msg_fetch_locks: dict = {}
# Timestamp of last bot-refresh per msg_id — don't re-fetch more than once per minute
_msg_refresh_ts: dict = {}
_MSG_REFRESH_INTERVAL = (
    60  # seconds — reduced from 120s; file_reference can expire mid-stream
    # causing silent audio loss. 60s balances freshness vs API load.
    # On error, the cache is force-invalidated anyway so this only
    # affects proactive refresh.
)

# ── Forward-inbox cache ───────────────────────────────────────────────────────
# Maps channel msg_id → bot-inbox message id of the forwarded copy.
# The bot fetches its own inbox copy which always has a live file_reference
# and the bot's own access_hash — identical to how main.py works.
# Entries are permanent for the session; the forwarded message stays in the
# bot's Saved Messages (or designated chat) and remains accessible.
_fwd_inbox_id: dict[int, int] = {}  # channel_msg_id → bot_inbox_msg_id
_fwd_locks: dict[int, asyncio.Lock] = {}

# Set BOT_CHAT_ID in .env to a private chat/group the bot can write to.
# Defaults to the bot's own Saved Messages (None → bot sends to itself).
_BOT_CHAT_ID_RAW = __import__("os").getenv("BOT_CHAT_ID")
_BOT_CHAT_ID = (
    int(_BOT_CHAT_ID_RAW) if _BOT_CHAT_ID_RAW else None
)  # None = Saved Messages


async def _get_bot_inbox_chat():
    """Return the entity the bot forwards messages into (Saved Messages by default)."""
    bc = _cfg.bot_client
    if bc is None:
        return None
    if _BOT_CHAT_ID is not None:
        try:
            return await bc.get_entity(_BOT_CHAT_ID)
        except Exception as e:
            print(
                f"[fwd] get BOT_CHAT_ID={_BOT_CHAT_ID} failed: {e}, falling back to Saved Messages"
            )
    # Saved Messages = bot's own user object
    return await bc.get_me()


async def _forward_to_bot_inbox(msg_id: int):
    """Forward channel msg_id to bot's inbox and return the inbox message.

    The forwarded message gets a brand-new file_reference + bot's access_hash,
    exactly like main.py's events.NewMessage path.  Result is cached in
    _fwd_inbox_id so we only forward once per session per msg_id.
    """
    bc = _cfg.bot_client
    if bc is None:
        return None

    if msg_id not in _fwd_locks:
        _fwd_locks[msg_id] = asyncio.Lock()

    async with _fwd_locks[msg_id]:
        # Re-check under lock
        if msg_id in _fwd_inbox_id:
            inbox_id = _fwd_inbox_id[msg_id]
            try:
                inbox_chat = await _get_bot_inbox_chat()
                fwd_msg = await bc.get_messages(inbox_chat, ids=inbox_id)
                if fwd_msg and fwd_msg.media:
                    return fwd_msg
                # Forwarded copy was deleted or expired — re-forward below
                print(
                    f"[fwd] cached inbox msg {inbox_id} gone, re-forwarding msg={msg_id}"
                )
                del _fwd_inbox_id[msg_id]
            except Exception as e:
                print(f"[fwd] inbox fetch failed ({e}), re-forwarding msg={msg_id}")
                _fwd_inbox_id.pop(msg_id, None)

        try:
            channel_ent = await _get_entity()
            inbox_chat = await _get_bot_inbox_chat()

            # User client forwards the channel message into the bot's inbox chat
            fwd_msgs = await _cfg.client.forward_messages(
                entity=inbox_chat,
                messages=[msg_id],
                from_peer=channel_ent,
            )
            fwd_msg = fwd_msgs[0] if isinstance(fwd_msgs, list) else fwd_msgs
            if not fwd_msg:
                print(f"[fwd] forward returned nothing for msg={msg_id}")
                return None

            _fwd_inbox_id[msg_id] = fwd_msg.id
            print(f"[fwd] msg={msg_id} → inbox msg={fwd_msg.id} (fresh file_reference)")
            return fwd_msg

        except Exception as e:
            print(f"[fwd] forward failed for msg={msg_id}: {e}")
            return None

    _fwd_locks.pop(msg_id, None)
    return None


async def _get_msg(msg_id):
    """Return a live message object for streaming.

    Strategy (in order):
    1. Direct fast fetch via user client (Instant, 100ms) with 30s cache.
    2. Fallback: If bot client is configured and direct fetch failed, use
       the forward-to-bot-inbox path to ensure we have a fresh file_reference.
    """
    import time as _t

    # ── Path A: Direct fast fetch via user client ────────────────────────────
    if msg_id in _msg_cache:
        last_refresh = _msg_refresh_ts.get(msg_id, 0)
        if _t.time() - last_refresh < _MSG_REFRESH_INTERVAL:
            _msg_cache.move_to_end(msg_id)
            return _msg_cache[msg_id]

    if msg_id not in _msg_fetch_locks:
        _msg_fetch_locks[msg_id] = asyncio.Lock()
    async with _msg_fetch_locks[msg_id]:
        if msg_id in _msg_cache:
            last_refresh = _msg_refresh_ts.get(msg_id, 0)
            if _t.time() - last_refresh < _MSG_REFRESH_INTERVAL:
                _msg_cache.move_to_end(msg_id)
                return _msg_cache[msg_id]

        try:
            ent = await _get_entity()
            msg = await _cfg.client.get_messages(ent, ids=msg_id)
            if msg and msg.media:
                _msg_cache[msg_id] = msg
                _msg_refresh_ts[msg_id] = _t.time()
                if len(_msg_cache) > _MSG_CACHE_MAX:
                    old_id, _ = _msg_cache.popitem(last=False)
                    _msg_refresh_ts.pop(old_id, None)
                    _fwd_inbox_id.pop(old_id, None)
                _msg_fetch_locks.pop(msg_id, None)
                return msg
        except Exception as e:
            print(f"[get_msg] direct fast fetch failed for msg={msg_id}: {e}")

    # ── Path B: Fallback (forward-then-fetch via bot client) ─────────────────
    bc = _cfg.bot_client
    if bc is not None:
        if msg_id not in _msg_fetch_locks:
            _msg_fetch_locks[msg_id] = asyncio.Lock()
        async with _msg_fetch_locks[msg_id]:
            fwd_msg = await _forward_to_bot_inbox(msg_id)
            if fwd_msg and fwd_msg.media:
                _msg_cache[msg_id] = fwd_msg
                _msg_refresh_ts[msg_id] = _t.time()
                _fwd_inbox_id[msg_id] = fwd_msg.id
                if len(_msg_cache) > _MSG_CACHE_MAX:
                    old_id, _ = _msg_cache.popitem(last=False)
                    _msg_refresh_ts.pop(old_id, None)
                    _fwd_inbox_id.pop(old_id, None)
                _msg_fetch_locks.pop(msg_id, None)
                return fwd_msg

    _msg_fetch_locks.pop(msg_id, None)
    return _msg_cache.get(msg_id)


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM HANDLER — browser fMP4
# ═══════════════════════════════════════════════════════════════════════════════


async def stream_handler(request: web.Request):
    """Stream a Telegram video via FFmpeg fMP4 remux pipe.

    Uses pure Telethon MTProto streaming with DC-aware client selection.
    Bot API path is disabled (requires disk storage).
    """
    msg_id = int(request.match_info["msg_id"])
    meta = _meta(msg_id)

    # ── Pure Telethon MTProto streaming path ──────────────────────────────
    # Fire msg fetch immediately — also primes _msg_cache so _tg_read's
    # first call hits cache instead of waiting for another RPC round-trip.
    msg = await _get_msg(msg_id)
    if not msg or not msg.media:
        return web.Response(status=404, text="Not found")

    # ── v3 DC FIX: Proactive DC connection check ─────────────────────────
    file_dc = _get_file_dc(msg)
    if file_dc and file_dc not in _dc_client_map:
        print(
            f"[stream] msg={msg_id} file on DC {file_dc} (not in map) — forcing connection"
        )
        for pool_client in _cfg.client_pool:
            try:
                ok = await force_dc_connection(pool_client, file_dc, msg)
                if ok:
                    break
            except Exception:
                pass

    filename = meta.get("filename", get_filename(msg) if msg else "")
    raw_mime = meta.get("mime_type") or (get_mime(msg) if msg else "video/mp4")
    total = meta.get("size_bytes", 0) or (get_size(msg) if msg else 0)

    rng = request.headers.get("Range", "").strip()
    start = 0
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))

    do_transcode = request.rel_url.query.get("transcode") == "1"
    mode = "transcode→H.264" if do_transcode else "remux(copy)"
    source = "mtproto"
    print(f"[stream] {mode} msg={msg_id} start={start} file={filename} [{source}]")

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "video/mp4",
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "none",
        },
    )
    await resp.prepare(request)

    if not _FFMPEG:
        # No FFmpeg — raw pass-through
        try:
            read_len = total - start if total else 8 * 1024 * 1024
            if read_len <= 0:
                return resp
            async for chunk in _tg_read(msg_id, start, read_len):
                await resp.write(chunk)
        except (ConnectionResetError, asyncio.CancelledError, ConnectionError):
            pass
        return resp

    # FFmpeg fMP4 remux
    base_in = [
        _FFMPEG,
        "-loglevel",
        "error",
        "-probesize",
        "256K",
        "-analyzeduration",
        "50K",
        "-fflags",
        "+genpts+discardcorrupt+nobuffer+fastseek",
        "-flags",
        "low_delay",
        "-thread_queue_size",
        "512",
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    fmp4_out = [
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+faststart",
        "-frag_duration",
        "50000",
        "-flush_packets",
        "1",
        "-f",
        "mp4",
        "pipe:1",
    ]
    if do_transcode:
        cmd = (
            base_in
            + [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-tune",
                "zerolatency",
                "-profile:v",
                "high",
                "-level",
                "4.1",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ac",
                "2",
                "-avoid_negative_ts",
                "make_zero",
            ]
            + fmp4_out
        )
    else:
        cmd = base_in + ["-c", "copy", "-avoid_negative_ts", "make_zero"] + fmp4_out

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        print(f"[stream] FFmpeg launch error: {e}")
        return resp

    async def _feed_ffmpeg():
        try:
            read_len = total - start if total else 50 * 1024 * 1024
            if read_len <= 0:
                return

            cs = _stream_cache._CHUNK_SIZE  # 1 MB
            offset = start - (start % cs)
            skip = start - offset

            cur_offset = offset
            window = deque()
            _enqueue_frontier = [offset]

            # Use large prefetch to keep FFmpeg stdin always saturated
            PREFETCH = 8
            CACHE_AHEAD_CHUNKS = 16

            def _enqueue():
                while len(window) < PREFETCH:
                    next_off = _enqueue_frontier[0]
                    read_head = window[0][0] if window else cur_offset
                    if next_off > read_head + CACHE_AHEAD_CHUNKS * cs:
                        break
                    if next_off >= offset + read_len:
                        break
                    if next_off >= total:
                        break

                    client = get_dc_aware_client(msg, _cfg.client)
                    if _cfg.client_pool:
                        idx = (next_off // cs) % len(_cfg.client_pool)
                        client = _cfg.client_pool[idx]

                    window.append(
                        (
                            next_off,
                            asyncio.ensure_future(
                                _fetch_chunk(msg_id, next_off, cs, client=client)
                            ),
                        )
                    )
                    _enqueue_frontier[0] = next_off + cs

            _enqueue()
            sent_to_ffmpeg = 0

            while window and proc.returncode is None:
                off, task = window.popleft()
                _chunk_retries = 0
                _MAX_CHUNK_RETRIES = 3
                while _chunk_retries < _MAX_CHUNK_RETRIES:
                    try:
                        chunk = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        raise
                    except Exception as _chunk_err:
                        _chunk_retries += 1
                        if _chunk_retries >= _MAX_CHUNK_RETRIES:
                            print(
                                f"[stream] chunk fetch failed at offset {off} after {_MAX_CHUNK_RETRIES} retries: {_chunk_err}"
                            )
                            break
                        # Refresh message and retry with a different client
                        _msg_cache.pop(msg_id, None)
                        _msg_refresh_ts.pop(msg_id, None)
                        _fwd_inbox_id.pop(msg_id, None)
                        alt_client = (
                            _cfg.client_pool[(_chunk_retries) % len(_cfg.client_pool)]
                            if _cfg.client_pool
                            else _cfg.client
                        )
                        task = asyncio.ensure_future(
                            _fetch_chunk(msg_id, off, cs, client=alt_client)
                        )
                        await asyncio.sleep(0.3 * _chunk_retries)
                        continue
                    break  # chunk fetch succeeded
                else:
                    break  # all retries failed

                _enqueue()

                if not chunk:
                    break

                if skip:
                    chunk = chunk[skip:]
                    skip = 0
                    if not chunk:
                        continue

                remaining_bytes = read_len - sent_to_ffmpeg
                if len(chunk) > remaining_bytes:
                    chunk = chunk[:remaining_bytes]

                # Write in 64KB pieces so FFmpeg gets data ASAP and can emit
                # the first fMP4 fragment without waiting for a full 1MB drain.
                WRITE_PIECE = 64 * 1024
                pos = 0
                while pos < len(chunk) and proc.returncode is None:
                    piece = chunk[pos : pos + WRITE_PIECE]
                    proc.stdin.write(piece)
                    await proc.stdin.drain()
                    pos += len(piece)
                sent_to_ffmpeg += len(chunk)

                if sent_to_ffmpeg >= read_len:
                    break

        except (
            asyncio.CancelledError,
            ConnectionResetError,
            BrokenPipeError,
            ConnectionError,
        ):
            pass
        except Exception as e:
            print(f"[stream] feed error msg={msg_id}: {e}")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def _log_ffmpeg():
        try:
            async for line in proc.stderr:
                txt = line.decode(errors="replace").strip()
                if txt:
                    print(f"[ffmpeg] {txt}")
        except Exception:
            pass

    feed_task = asyncio.ensure_future(_feed_ffmpeg())
    log_task = asyncio.ensure_future(_log_ffmpeg())

    try:
        while True:
            chunk = await proc.stdout.read(1024 * 1024)
            if not chunk:
                break
            await resp.write(chunk)
    except (
        ConnectionResetError,
        asyncio.CancelledError,
        BrokenPipeError,
        ConnectionError,
        OSError,  # FIX: WinError 10054 on Windows SelectorEventLoop
    ):
        pass
    except Exception as e:
        _cls = e.__class__.__name__
        if _cls in (
            "ClientOSError",
            "ClientDisconnectedError",
            "ServerDisconnectedError",
        ):
            pass
        else:
            print(f"[stream] write error msg={msg_id}: {e}")
    finally:
        feed_task.cancel()
        log_task.cancel()
        try:
            proc.kill()
        except Exception:
            pass

    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# VLC RAW STREAM — TG STREAMER parallel-worker approach (proven zero-buffer)
# ═══════════════════════════════════════════════════════════════════════════════

# ── Per-block file cache (video_cache/{msg_id}_{offset}.bin) ─────────────────
# _VLC_BLOCK_SIZE / _VLC_WORKERS / _VLC_WRITE_CHUNK / _VLC_PREFETCH_BLOCKS now
# sourced from config.py (env-tunable, see VLC_* settings there).

_VIDEO_CACHE_DIR = os.path.join(_here, "video_cache")
os.makedirs(_VIDEO_CACHE_DIR, exist_ok=True)

_vlc_prefetch_tasks: dict[int, asyncio.Task] = {}


def _vlc_cache_path(msg_id: int, offset: int) -> str:
    return os.path.join(_VIDEO_CACHE_DIR, f"{msg_id}_{offset}.bin")


def _vlc_prune_cache(msg_id: int, current_offset: int, cache_limit_bytes: int):
    """Remove cached block files that are too far behind the playhead."""
    try:
        for entry in os.scandir(_VIDEO_CACHE_DIR):
            if entry.is_file() and entry.name.startswith(f"{msg_id}_"):
                try:
                    parts = entry.name.split("_")
                    if len(parts) == 2 and parts[1].endswith(".bin"):
                        file_offset = int(parts[1][:-4])
                        if file_offset < current_offset - cache_limit_bytes:
                            os.remove(entry.path)
                except Exception:
                    pass
    except Exception as e:
        print(f"[vlc_cache] prune error: {e}")


async def _download_block_pool(msg, offset: int, max_retries: int = 5) -> bytes:
    """Download one 1 MB block from Telegram with pool-client round-robin and retry."""
    pool = _cfg.client_pool or [_cfg.client]
    backoff = 0.5
    for attempt in range(max_retries):
        client = pool[attempt % len(pool)]
        try:
            block = b""
            async for chunk in client.iter_download(
                msg, offset=offset, request_size=_VLC_BLOCK_SIZE, limit=1
            ):
                if not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                block += chunk
            if block:
                return block
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[vlc_dl] attempt {attempt+1} failed at offset {offset}: {e}")
            if attempt == max_retries - 1:
                raise
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 8.0)
    return b""


async def _vlc_prefetch_run(msg, current_offset: int, cache_limit_bytes: int):
    """Prefetch the next _VLC_PREFETCH_BLOCKS blocks ahead of current_offset to disk."""
    msg_id = msg.id
    file_size = getattr(msg.media.document, "size", None) if msg.media else None
    if not file_size:
        return
    for i in range(1, _VLC_PREFETCH_BLOCKS + 1):
        target_offset = current_offset + i * _VLC_BLOCK_SIZE
        if target_offset >= file_size:
            break
        path = _vlc_cache_path(msg_id, target_offset)
        if os.path.exists(path):
            continue
        try:
            block = await _download_block_pool(msg, target_offset)
            if block:
                temp = path + ".tmp"
                with open(temp, "wb") as f:
                    f.write(block)
                os.replace(temp, path)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[vlc_prefetch] error at {target_offset}: {e}")
            break


async def _trigger_vlc_prefetch(msg, current_offset: int, cache_limit_bytes: int):
    msg_id = msg.id
    existing = _vlc_prefetch_tasks.get(msg_id)
    if existing and not existing.done():
        existing.cancel()
    _vlc_prefetch_tasks[msg_id] = asyncio.create_task(
        _vlc_prefetch_run(msg, current_offset, cache_limit_bytes)
    )


async def _iter_tg_pool(msg, start: int = 0):
    """4-worker parallel Telegram downloader with per-block disk cache.

    Exact port of TG STREAMER's _iter_tg, adapted for STREAM VAULT's client pool.
    Workers stride by (workers * block_size) so they never overlap.
    Output is reordered by offset so VLC sees a contiguous byte stream.
    Blocks are written to video_cache/ so seeks replay from disk instantly.
    """
    from helpers import get_duration

    file_size = getattr(msg.media.document, "size", None) if msg.media else None
    aligned_start = (start // _VLC_BLOCK_SIZE) * _VLC_BLOCK_SIZE
    skip = start - aligned_start
    stride = _VLC_WORKERS * _VLC_BLOCK_SIZE

    result_q: asyncio.Queue = asyncio.Queue(maxsize=_VLC_WORKERS * 4)
    _SENTINEL = object()

    duration_s = get_duration(msg)
    if duration_s and file_size:
        bytes_per_second = file_size / duration_s
    else:
        bytes_per_second = 1024 * 1024
    cache_limit_bytes = int(bytes_per_second * 240)  # keep 4 min of data on disk

    _vlc_prune_cache(msg.id, aligned_start, cache_limit_bytes)
    await _trigger_vlc_prefetch(msg, aligned_start, cache_limit_bytes)

    async def _worker(worker_id: int):
        off = aligned_start + worker_id * _VLC_BLOCK_SIZE
        try:
            while True:
                if file_size and off >= file_size:
                    break
                block = b""
                path = _vlc_cache_path(msg.id, off)
                if os.path.exists(path):
                    try:
                        with open(path, "rb") as f:
                            block = f.read()
                    except Exception:
                        pass

                if not block:
                    try:
                        block = await _download_block_pool(msg, off)
                        if block:
                            temp = path + ".tmp"
                            with open(temp, "wb") as f:
                                f.write(block)
                            os.replace(temp, path)
                    except Exception:
                        pass

                if not block:
                    break
                await result_q.put((off, block))
                off += stride
        except (asyncio.CancelledError, GeneratorExit):
            pass
        except Exception as exc:
            print(f"[vlc_worker{worker_id}] error at offset {off}: {exc}")
        finally:
            await result_q.put((_SENTINEL, _SENTINEL))

    worker_tasks = [asyncio.ensure_future(_worker(i)) for i in range(_VLC_WORKERS)]
    finished_workers = 0
    pending: dict = {}
    leftover = b""
    next_emit = aligned_start

    try:
        while finished_workers < _VLC_WORKERS or pending or leftover:
            try:
                off, data = result_q.get_nowait()
            except asyncio.QueueEmpty:
                if finished_workers == _VLC_WORKERS and not pending:
                    break
                off, data = await result_q.get()

            if off is _SENTINEL:
                finished_workers += 1
                continue

            pending[off] = data

            while next_emit in pending:
                block = pending.pop(next_emit)
                if skip:
                    block = block[skip:]
                    skip = 0
                if not block:
                    next_emit += _VLC_BLOCK_SIZE
                    continue
                data_out = leftover + block
                leftover = b""
                pos = 0
                while pos + _VLC_WRITE_CHUNK <= len(data_out):
                    yield data_out[pos : pos + _VLC_WRITE_CHUNK]
                    pos += _VLC_WRITE_CHUNK
                leftover = data_out[pos:]
                next_emit += _VLC_BLOCK_SIZE

        if leftover:
            yield leftover
    finally:
        for t in worker_tasks:
            t.cancel()
        try:
            while not result_q.empty():
                result_q.get_nowait()
        except Exception:
            pass


async def vlc_stream_handler(request: web.Request):
    """Raw byte-range stream for VLC — TG STREAMER parallel-worker approach."""
    msg_id = int(request.match_info["msg_id"])
    filename_from_url = request.match_info.get("filename", "")

    meta = _meta(msg_id)
    msg = await _get_msg(msg_id)
    if not msg or not msg.media:
        return web.Response(status=404, text="Not found")

    total = meta.get("size_bytes", 0) or get_size(msg)
    raw_mime = meta.get("mime_type") or get_mime(msg)
    filename = (
        unquote(filename_from_url)
        if filename_from_url
        else meta.get("filename", get_filename(msg))
    )

    # Duration for playhead tracking
    duration_s = 0.0
    if msg.media and hasattr(msg.media, "document"):
        for attr in msg.media.document.attributes:
            if isinstance(attr, _DocVid):
                duration_s = float(attr.duration or 0)
                break

    # ── Parse Range header ───────────────────────────────────────────────
    rng = request.headers.get("Range", "").strip()
    start = 0
    end = total - 1 if total else None
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else (total - 1 if total else None)

    content_length = (end - start + 1) if (end is not None) else None

    # Update playhead so background prefetcher knows where VLC is
    if duration_s > 0 and total > 0:
        _stream_cache.update_playhead(
            msg_id, (start / total) * duration_s, duration_s, total
        )

    # ── Build 206 response headers ───────────────────────────────────────
    headers = {
        "Content-Type": raw_mime or "video/mp4",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if end is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{total or '*'}"
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    resp = web.StreamResponse(status=206 if rng else 200, headers=headers)
    await resp.prepare(request)

    sent = 0
    _PLAYHEAD_INTERVAL = 2 * 1024 * 1024
    _last_ph_update = 0

    def _update_ph(sent_bytes):
        nonlocal _last_ph_update
        if (
            duration_s > 0
            and total > 0
            and sent_bytes - _last_ph_update >= _PLAYHEAD_INTERVAL
        ):
            _stream_cache.update_playhead(
                msg_id, ((start + sent_bytes) / total) * duration_s, duration_s, total
            )
            _last_ph_update = sent_bytes

    try:
        print(f"[vlc_stream] msg={msg_id} start={start} workers={_VLC_WORKERS}")
        drain_count = 0
        async for chunk in _iter_tg_pool(msg, start=start):
            if content_length is not None and sent + len(chunk) > content_length:
                chunk = chunk[: content_length - sent]
            if not chunk:
                break
            try:
                await resp.write(chunk)
            except (OSError, ConnectionResetError, BrokenPipeError, ConnectionError):
                return resp
            sent += len(chunk)
            _update_ph(sent)
            drain_count += 1
            if drain_count % 16 == 0:
                await asyncio.sleep(0)
            if content_length is not None and sent >= content_length:
                break
    except asyncio.CancelledError:
        pass
    except Exception as e:
        if e.__class__.__name__ not in (
            "ClientOSError",
            "ClientDisconnectedError",
            "ServerDisconnectedError",
        ):
            print(f"[vlc_stream] msg={msg_id} off={start}: {type(e).__name__}: {e}")

    return resp


# HLS — unchanged from v1
# ═══════════════════════════════════════════════════════════════════════════════

_TRANSCODE_REQUIRED = {"hevc", "h265", "av1", "mpeg2video", "mpeg4", "theora"}
_probe_cache: dict = {}
_hls_sessions: dict = {}


def _hls_dir(msg_id: int) -> str:
    return os.path.join(_HLS_DIR, str(msg_id))


async def _cleanup_session(msg_id: int):
    sess = _hls_sessions.pop(msg_id, None)
    if not sess:
        return
    try:
        sess["proc"].kill()
    except Exception:
        pass
    d = sess["dir"]
    await asyncio.sleep(0.5)
    try:
        shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass
    print(f"[hls] cleaned up session msg={msg_id}")


async def _probe_codec(msg_id: int, msg) -> tuple:
    if msg_id in _probe_cache:
        return _probe_cache[msg_id]

    cache_data = _load_cache()
    cached_entry = next((v for v in cache_data if v["message_id"] == msg_id), None)
    if cached_entry and cached_entry.get("_codec"):
        codec = cached_entry["_codec"]
        duration_s = cached_entry.get("_duration_s", 0.0)
        _probe_cache[msg_id] = (codec, duration_s)
        print(f"[probe] msg={msg_id} cached codec={codec!r}")
        return (codec, duration_s)

    duration_s = 0.0
    try:
        for attr in msg.media.document.attributes:
            if isinstance(attr, _DocVid):
                duration_s = float(getattr(attr, "duration", 0) or 0)
                break
    except Exception:
        pass

    codec = ""
    if _FFPROBE:
        try:
            probe_cmd = [
                _FFPROBE,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "v:0",
                "-i",
                "pipe:0",
            ]
            proc = await asyncio.create_subprocess_exec(
                *probe_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            PROBE_BYTES = 2 * 1024 * 1024
            fed = 0
            async for chunk in _tg_read(msg_id, 0, PROBE_BYTES):
                proc.stdin.write(chunk)
                fed += len(chunk)
                if fed >= PROBE_BYTES:
                    break
            try:
                proc.stdin.close()
            except Exception:
                pass
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            info = json.loads(stdout or b"{}")
            streams = info.get("streams", [])
            if streams:
                codec = streams[0].get("codec_name", "").lower()
            print(
                f"[probe] msg={msg_id} ffprobe codec={codec!r} duration={duration_s:.1f}s"
            )
        except Exception as e:
            print(f"[probe] ffprobe error msg={msg_id}: {e}")
    else:
        mime = getattr(msg.media.document, "mime_type", "") or ""
        if mime in ("video/webm",):
            codec = "vp9"
        print(f"[probe] msg={msg_id} mime-inferred codec={codec!r} (no ffprobe)")

    result = (codec, duration_s)
    _probe_cache[msg_id] = result

    if cached_entry is not None:
        cached_entry["_codec"] = codec
        cached_entry["_needs_hls"] = _needs_ffmpeg(
            _safe_mime(cached_entry.get("mime_type", "")),
            cached_entry.get("filename", ""),
            codec,
        )
        _save_cache(cache_data)

    return result


async def _start_hls_session(msg_id: int, msg, codec: str, duration_s: float):
    seg_dir = _hls_dir(msg_id)
    os.makedirs(seg_dir, exist_ok=True)
    playlist = os.path.join(seg_dir, "playlist.m3u8")
    seg_pattern = os.path.join(seg_dir, "seg%05d.ts")

    need_transcode = codec in _TRANSCODE_REQUIRED or codec == ""
    hls_out = [
        "-f",
        "hls",
        "-hls_time",
        "1",
        "-hls_list_size",
        "0",
        "-hls_flags",
        "independent_segments+temp_file",
        "-hls_segment_type",
        "mpegts",
        "-hls_segment_filename",
        seg_pattern,
        "-start_number",
        "0",
        playlist,
    ]
    dur_meta = ["-metadata", f"duration={duration_s:.3f}"] if duration_s > 0 else []

    if need_transcode:
        print(f"[hls] transcode msg={msg_id} codec={codec!r}")
        cmd = (
            [
                _FFMPEG,
                "-loglevel",
                "error",
                "-probesize",
                "2M",
                "-analyzeduration",
                "500K",
                "-fflags",
                "+genpts+discardcorrupt+fastseek",
                "-thread_queue_size",
                "512",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-tune",
                "zerolatency",
                "-profile:v",
                "high",
                "-level",
                "4.1",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ac",
                "2",
                "-avoid_negative_ts",
                "make_zero",
            ]
            + dur_meta
            + hls_out
        )
    else:
        print(f"[hls] remux msg={msg_id} codec={codec!r}")
        cmd = (
            [
                _FFMPEG,
                "-loglevel",
                "error",
                "-probesize",
                "2M",
                "-analyzeduration",
                "500K",
                "-fflags",
                "+genpts+discardcorrupt+fastseek",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-avoid_negative_ts",
                "make_zero",
            ]
            + dur_meta
            + hls_out
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        print(f"[hls] FFmpeg launch error: {e}")
        return

    # Feed TG data to ffmpeg stdin
    async def _feed():
        try:
            async for chunk in _tg_read(msg_id, 0, None):
                if proc.returncode is not None:
                    break
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            print(f"[hls] feed error msg={msg_id}: {e}")
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    async def _log():
        try:
            async for line in proc.stderr:
                txt = line.decode(errors="replace").strip()
                if txt:
                    print(f"[hls-ffmpeg] {txt}")
        except Exception:
            pass

    feed_task = asyncio.ensure_future(_feed())
    log_task = asyncio.ensure_future(_log())

    _hls_sessions[msg_id] = {
        "proc": proc,
        "dir": seg_dir,
        "feed_task": feed_task,
        "log_task": log_task,
    }


async def route_hls_start(req: web.Request):
    msg_id = int(req.match_info["msg_id"])
    msg = await _get_msg(msg_id)
    if not msg or not msg.media:
        return web.json_response({"ok": False, "error": "not found"}, status=404)

    codec, duration_s = await _probe_codec(msg_id, msg)

    if msg_id not in _hls_sessions:
        await _start_hls_session(msg_id, msg, codec, duration_s)

    # Wait for playlist to appear (up to 15s)
    playlist_path = os.path.join(_hls_dir(msg_id), "playlist.m3u8")
    for _ in range(30):
        if os.path.exists(playlist_path):
            break
        await asyncio.sleep(0.5)

    return web.json_response({"ok": True, "codec": codec})


async def route_hls_playlist(req: web.Request):
    msg_id = req.match_info["msg_id"]
    playlist_path = os.path.join(_hls_dir(msg_id), "playlist.m3u8")
    if not os.path.exists(playlist_path):
        return web.Response(status=404, text="Playlist not ready")
    with open(playlist_path, "r") as f:
        content = f.read()
    return web.Response(
        content_type="application/vnd.apple.mpegurl",
        text=content,
        headers={"Cache-Control": "no-cache"},
    )


async def route_hls_segment(req: web.Request):
    msg_id = req.match_info["msg_id"]
    seg_name = req.match_info["segment"]
    seg_path = os.path.join(_hls_dir(msg_id), seg_name)
    if not os.path.exists(seg_path):
        return web.Response(status=404)
    with open(seg_path, "rb") as f:
        data = f.read()
    return web.Response(
        body=data,
        content_type="video/mp2t",
        headers={"Cache-Control": "no-cache"},
    )


async def route_hls_stop(req: web.Request):
    msg_id = int(req.match_info["msg_id"])
    await _cleanup_session(msg_id)
    return web.json_response({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════════════════════

_bg_started = False
_bg_tasks: list[asyncio.Task] = []


def start_background_tasks():
    global _bg_started
    if _bg_started:
        return
    _bg_started = True
    _bg_tasks.append(asyncio.create_task(_stream_cache_cleanup_loop()))


async def stop_background_tasks():
    global _bg_started
    for task in list(_bg_tasks):
        task.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        _bg_tasks.clear()
    _bg_started = False


async def route_clear_stream_cache(req: web.Request):
    """POST /api/clear_cache[?msg_id=NNN]

    Wipes in-memory byte cache + message cache for a video so the next play
    re-fetches clean data from Telegram.  Omit msg_id to wipe all buffered videos.
    Does NOT cancel active streams or prefetch tasks — those keep running.
    """
    raw = req.rel_url.query.get("msg_id", "").strip()
    if raw:
        try:
            msg_id = int(raw)
        except ValueError:
            return web.json_response(
                {"ok": False, "error": "invalid msg_id"}, status=400
            )
        _clear_one(msg_id)
        print(f"[clear_cache] evicted msg={msg_id}")
        return web.json_response({"ok": True, "cleared": [msg_id]})
    else:
        ids = list(_stream_cache._data.keys())
        for mid in ids:
            _clear_one(mid)
        print(f"[clear_cache] evicted all ({len(ids)} videos)")
        return web.json_response({"ok": True, "cleared": ids})


def _clear_one(msg_id: int) -> None:
    """Purge a single msg_id from every in-memory cache layer.

    Does NOT cancel progressive/prefetch tasks (would stop playback) and does
    NOT touch _fetch_locks (mid-flight fetches must finish normally — clearing
    their lock entry causes duplicate concurrent fetches and cache corruption).
    After clearing, the next cache miss re-fetches clean data from Telegram.
    """
    # 1. byte-range cache + playhead
    _stream_cache.clear_video(msg_id)
    _stream_cache.remove_playhead(msg_id)

    # 2. Telethon message / forward caches → forces re-fetch on next _get_msg()
    _msg_cache.pop(msg_id, None)
    _msg_refresh_ts.pop(msg_id, None)
    _fwd_inbox_id.pop(msg_id, None)
    _fwd_locks.pop(msg_id, None)
    _msg_fetch_locks.pop(msg_id, None)

    # 3. Disk cache files — removes vlc_tmp/video_{msg_id}.tmp and .json sidecar
    sess = _active_disk_sessions.pop(msg_id, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass
    _tmp_path = os.path.join(_VLC_TMP_DIR, f"video_{msg_id}.tmp")
    for _fp in (_tmp_path, _tmp_path + ".json"):
        try:
            if os.path.exists(_fp):
                os.remove(_fp)
                print(f"[disk_cache] deleted {os.path.basename(_fp)}")
        except Exception:
            pass


def prepare_new_stream_session(msg_id: int) -> None:
    """Prepare a message ID for a new stream session by cancelling any existing prefetcher/progressive downloader tasks and clearing the cache."""
    print(f"[session] preparing new stream session for msg={msg_id}")
    _cancel_prefetch_task(msg_id)
    _cancel_progressive_task(msg_id, clear_consumers=True)
    _clear_one(msg_id)


async def _vlc_instant_prewarm(
    msg_id: int, offset_sizes: list[tuple[int, int]], wait_timeout_s: float = 1.5
):
    """Prewarm multiple byte offsets concurrently.

    Each offset is fetched independently.  If wait_timeout_s elapses before
    all fetches complete the remaining tasks are left running in the background
    (NOT cancelled) so data still lands in the cache — only the caller's wait
    is released early.  This avoids the old bug where timeout cancelled a
    mid-flight Telegram download, leaving the cache half-filled and causing
    VLC to stall on the very byte we were trying to warm.
    """
    if not offset_sizes:
        return
    import cache as _cache_mod

    total_bytes = _cache_mod._meta(msg_id).get("size_bytes", 0)
    if total_bytes <= 0:
        return
    tasks = [
        asyncio.create_task(
            _stream_cache_prewarm_offset(msg_id, off, size, total_bytes)
        )
        for off, size in offset_sizes
        if off >= 0
    ]
    if tasks:
        try:
            done, pending = await asyncio.wait(tasks, timeout=wait_timeout_s)
            if pending:
                print(
                    f"[vlc_prewarm] msg={msg_id} {len(pending)} fetch(es) still running "
                    f"after {wait_timeout_s}s — continuing in background"
                )
                # Leave pending tasks running — they will fill the cache and
                # benefit the next VLC range request for those bytes.
        except Exception as e:
            print(f"[vlc_prewarm] msg={msg_id} error: {e}")

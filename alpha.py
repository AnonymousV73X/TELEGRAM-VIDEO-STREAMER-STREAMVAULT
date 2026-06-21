"""alpha.py — StreamVault entrypoint.

This file is intentionally minimal: bootstrap, start client, wire app, run.
All logic lives in the other modules so Pylance indexes small files, not one
4 000-line monolith with 32 KB of embedded string literals.

Module map
----------
config.py      — env, constants, TelegramClient reference
helpers.py     — is_video, get_*, derive_album, fmt utils
cache.py       — fetch/cache, encryption, OMDB, album index
streaming.py   — _iter_tg, stream_handler, vlc_stream_handler, HLS
render.py      — _head, _nav, _render_index, _render_album (loads static/)
routes.py      — all route_* handlers, auth, make_app
static/        — style.css, modal.html, virtual_album.js
"""

# ── Bootstrap (dep install + .env load) lives in config.py ───────────────────
import config  # noqa: F401 — side-effects: installs packages, loads .env

import os, sys, shutil, asyncio, socket as _socket, traceback
from contextlib import suppress
from aiohttp import web
from telethon import TelegramClient
from telethon.network import ConnectionTcpAbridged

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import config as _cfg
from cache import _load_cache
from streaming import _HLS_DIR
from routes import make_app

try:
    from render import _make_lqip_b64, _make_poster_sm

    _HAS_LQIP = True
except ImportError:
    _HAS_LQIP = False

# Bot API server subprocess manager — auto-starts telegram-bot-api binary
try:
    from bot_api_server import (
        start_bot_api_server,
        stop_bot_api_server,
        get_bot_api_status,
    )

    _HAS_BOTAPI_SERVER = True
except ImportError:
    _HAS_BOTAPI_SERVER = False

    async def start_bot_api_server():
        return False

    async def stop_bot_api_server():
        pass

    def get_bot_api_status():
        return {"running": False, "ready": False}


# ── MAIN ──────────────────────────────────────────────────────────────────────
async def _main():
    bg_tasks = []
    runner = None

    def _spawn_bg(coro, name):
        task = asyncio.create_task(coro, name=name)
        bg_tasks.append(task)
        return task

    if not _cfg.API_ID or not _cfg.API_HASH:
        print("\n  ⚠  Set API_ID and API_HASH in your .env!\n")
        return False
    if not _cfg.CHANNEL_ID:
        print("\n  ⚠  Set CHANNEL_ID in your .env!\n")
        return False

    print("=" * 52)
    print("  StreamVault  —  aiohttp edition")
    print("=" * 52)
    print(f"\n  Channel : {_cfg.CHANNEL_ID}")

    # ── Startup housekeeping ──────────────────────────────────────────────────
    if os.path.isdir(_HLS_DIR):
        shutil.rmtree(_HLS_DIR, ignore_errors=True)
        print("[setup] Cleared stale hls_cache/")
    os.makedirs(_HLS_DIR, exist_ok=True)

    # ── Start Telegram user client ─────────────────────────────────────────────
    print("[setup] Connecting to Telegram...")
    try:
        _cfg.client = TelegramClient(
            _cfg.SESSION,
            _cfg.API_ID,
            _cfg.API_HASH,
            connection=ConnectionTcpAbridged,
            connection_retries=10,
            auto_reconnect=True,  # ensure auto-reconnect on DC drops
            retry_delay=1,  # fast reconnect (default 1s, explicit for clarity)
            flood_sleep_threshold=20,
            request_retries=5,
        )
        await _cfg.client.start()
        me = await _cfg.client.get_me()
        print(f"  ✓ Logged in as {me.first_name}")
    except Exception as e:
        print(f"\n  ✗ FAILED to start Telegram client: {e}")
        print(f"  Full traceback:")
        traceback.print_exc()
        print("\n  Common fixes:")
        print("    1. Delete 'session.session' file and re-run (re-auth)")
        print("    2. Check your internet connection")
        print("    3. Verify API_ID and API_HASH in .env")
        print("    4. If session is locked, close other StreamVault instances\n")
        return False

    # Method 2: verify cryptg C-extension is active (transparent AES speedup)
    try:
        import cryptg

        print(f"[setup] cryptg active: {cryptg.__file__} — C-AES enabled")
    except ImportError:
        print(
            "[setup] ⚠ cryptg NOT loaded — pure-Python AES caps throughput at ~3-5 MBps"
        )
        print(
            "[setup]   Fix: pip install cryptg  (requires C compiler / pre-built wheel)"
        )

    # ── Multi-DC client pool — each extra client = independent rate-limit bucket ─
    # Uses StringSession exported from the already-authenticated main client so
    # the SQLite .session file is never opened twice (Windows locks it).
    _POOL_SIZE = int(os.environ.get("STREAMVAULT_POOL_SIZE", "8"))
    try:
        from telethon.sessions import StringSession

        _auth_str = StringSession.save(_cfg.client.session)
        for _i in range(_POOL_SIZE - 1):
            _extra = TelegramClient(
                StringSession(_auth_str),
                _cfg.API_ID,
                _cfg.API_HASH,
                connection=ConnectionTcpAbridged,
                connection_retries=10,
                auto_reconnect=True,  # auto-reconnect on DC drops
                retry_delay=1,
                flood_sleep_threshold=20,
                request_retries=5,
            )
            await _extra.start()
            _cfg.client_pool.append(_extra)
        _cfg.client_pool.insert(0, _cfg.client)
        print(f"[setup] MTProto pool: {len(_cfg.client_pool)} connections active")
    except Exception as _pe:
        print(
            f"[setup] ⚠ pool spin-up failed ({_pe}), falling back to single connection"
        )
        _cfg.client_pool = [_cfg.client]

    # ── DC pre-connection — warm up connections to all file DCs ────────────────
    # Scans cached messages, discovers which DCs files are stored on, and
    # forces pool clients to connect to those DCs.  This eliminates the
    # first-play delay for files on non-home DCs.
    try:
        from streaming import preconnect_dcs, _dc_keepalive_loop

        _spawn_bg(preconnect_dcs(), "streamvault-dc-preconnect")
        _spawn_bg(_dc_keepalive_loop(), "streamvault-dc-keepalive")
        print("[setup] DC pre-connection + keepalive started in background")
    except ImportError:
        print("[setup] DC pre-connection not available — will connect on-demand")

    # ── Bot client — used for Bot API streaming + fresh access_hash ─────────────
    # A bot session never expires and always returns a valid document reference.
    # The user client fetches channel history; the bot client serves streams.
    if _cfg.BOT_TOKEN:
        try:
            _cfg.bot_client = TelegramClient(
                os.path.join(_cfg._here, "bot_session"),
                _cfg.API_ID,
                _cfg.API_HASH,
                connection=ConnectionTcpAbridged,
                connection_retries=10,
                auto_reconnect=True,
                retry_delay=1,
                flood_sleep_threshold=20,
                request_retries=5,
            )
            await _cfg.bot_client.start(bot_token=_cfg.BOT_TOKEN)
            _bot_me = await _cfg.bot_client.get_me()
            print(f"[setup] Bot client started: @{_bot_me.username}")
        except Exception as _be:
            print(
                f"[setup] ⚠ Bot client failed to start ({_be}) — using user client for streams"
            )
            _cfg.bot_client = None
    else:
        print(
            "[setup] BOT_TOKEN not set — bot client disabled (streams use user client)"
        )

    # ── Local Bot API Server — DISABLED (pure Telethon streaming with DC fix) ──
    # Bot API streaming was disabled because it requires downloading files to
    # disk (via getFile), which defeats the purpose of a streaming platform.
    # Instead, we use pure Telethon MTProto streaming with DC-aware client
    # selection to handle files on different data centers.
    _botapi_server_ok = False
    print(
        "[setup] Bot API streaming DISABLED — using pure Telethon with DC-aware streaming"
    )

    # ── Bot API sync — DISABLED (pure Telethon streaming) ────────────────────────
    _botapi_sync_task = None
    print("[setup] Bot API sync DISABLED — using pure Telethon streaming")

    # Signal launcher: real Telegram auth complete
    _tg_ev = getattr(sys.modules[__name__], "_telegram_ready", None)
    if _tg_ev is not None:
        _tg_ev.set()
    # PORT printed after binding (may change if default port is occupied)

    # ── Start aiohttp ─────────────────────────────────────────────────────────
    def _find_free_port(start: int, host: str = "0.0.0.0", max_tries: int = 20) -> int:
        for port in range(start, start + max_tries):
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
                try:
                    s.bind((host, port))
                    return port
                except OSError:
                    continue
        raise OSError(f"No free port found in range {start}–{start + max_tries - 1}")

    _orig_port = _cfg.PORT
    _cfg.PORT = _find_free_port(_cfg.PORT, _cfg.HOST)
    if _cfg.PORT != _orig_port:
        print(f"[setup] Port {_orig_port} in use — binding to port {_cfg.PORT} instead")

    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, _cfg.HOST, _cfg.PORT, backlog=512)
    await site.start()
    print(f"  → http://localhost:{_cfg.PORT}\n")

    try:
        srv_sock = site._server.sockets[0]
        srv_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF, 4 * 1024 * 1024)
        srv_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_RCVBUF, 1 * 1024 * 1024)
        srv_sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        print("[setup] SO_SNDBUF=4MB SO_RCVBUF=1MB TCP_NODELAY=1 set on server socket")
    except Exception as _se:
        print(f"[setup] Could not set socket buffers: {_se}")

    # Start streaming background tasks (cleanup loop, etc.)
    from streaming import start_background_tasks, stop_background_tasks

    start_background_tasks()

    # Signal launcher: cache loaded, background tasks running
    _cache_ev = getattr(sys.modules[__name__], "_cache_ready", None)
    if _cache_ev is not None:
        _cache_ev.set()

    # ── Backfill LQIP for posters cached before this feature existed ──────────
    async def _backfill_lqip():
        if not _HAS_LQIP:
            return
        if not os.path.isdir(_cfg.POSTERS_DIR):
            return
        loop = asyncio.get_event_loop()

        existing = set(os.listdir(_cfg.POSTERS_DIR))
        lqip_count = sm_count = 0

        for fname in existing:
            if not fname.endswith(".jpg") or fname.endswith("_sm.jpg"):
                continue
            slug = fname[:-4]
            jpg_path = os.path.join(_cfg.POSTERS_DIR, fname)
            lqip_path = os.path.join(_cfg.POSTERS_DIR, f"{slug}.lqip")
            sm_path = os.path.join(_cfg.POSTERS_DIR, f"{slug}_sm.jpg")
            need_lqip = f"{slug}.lqip" not in existing
            need_sm = f"{slug}_sm.jpg" not in existing
            if not need_lqip and not need_sm:
                continue

            def _gen(p=jpg_path, lp=lqip_path, sp=sm_path, nl=need_lqip, ns=need_sm):
                try:
                    with open(p, "rb") as _f:
                        data = _f.read()
                    if nl:
                        lqip = _make_lqip_b64(data)
                        if lqip:
                            with open(lp, "w") as _f:
                                _f.write(lqip)
                    if ns:
                        _make_poster_sm(data, sp)
                except Exception:
                    pass

            await loop.run_in_executor(None, _gen)
            if need_lqip:
                lqip_count += 1
            if need_sm:
                sm_count += 1
            await asyncio.sleep(0)

        if lqip_count:
            print(f"[lqip] Backfilled {lqip_count} LQIP placeholder(s)")
        if sm_count:
            print(f"[poster] Backfilled {sm_count} small variant(s)")

    _spawn_bg(_backfill_lqip(), "streamvault-lqip-backfill")

    # ── Periodic stream cache stats ──────────────────────────────────────────
    async def _cache_stats_loop():
        """Log stream cache stats every 5 minutes."""
        while True:
            await asyncio.sleep(300)
            try:
                from streaming import _stream_cache

                stats = _stream_cache.stats()
                if stats:
                    videos = stats.get("videos", 0)
                    total_mb = stats.get("total_mb", 0)
                    print(
                        f"[stream_cache] {videos} videos cached, {total_mb:.1f} MB in use"
                    )
            except Exception:
                pass

    _spawn_bg(_cache_stats_loop(), "streamvault-cache-stats")

    # ── Run forever with graceful shutdown ─────────────────────────────────────
    try:
        await asyncio.Event().wait()
    finally:
        for task in bg_tasks:
            task.cancel()
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        if runner is not None:
            with suppress(Exception):
                await runner.cleanup()

        with suppress(Exception):
            await stop_background_tasks()

        for client in list(getattr(_cfg, "client_pool", []) or []):
            with suppress(Exception):
                await client.disconnect()
        if _cfg.bot_client is not None:
            with suppress(Exception):
                await _cfg.bot_client.disconnect()
        if _cfg.client is not None and _cfg.client not in getattr(
            _cfg, "client_pool", []
        ):
            with suppress(Exception):
                await _cfg.client.disconnect()

        # Stop the Bot API server subprocess on exit
        if _HAS_BOTAPI_SERVER:
            print("[shutdown] Stopping Bot API server...")
            await stop_bot_api_server()
            print("[shutdown] Bot API server stopped")


if __name__ == "__main__":
    import platform

    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        print("[setup] WindowsSelectorEventLoopPolicy → higher stream bitrate")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n[shutdown] StreamVault stopped by user")
    except Exception as e:
        print(f"\n[shutdown] StreamVault crashed: {e}")
        traceback.print_exc()

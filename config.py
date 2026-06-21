import subprocess, sys, os

_is_frozen = getattr(sys, "frozen", False)

if _is_frozen:
    _here = os.path.dirname(sys.executable)
    _bundle_dir = sys._MEIPASS
    _parent = os.path.dirname(_here)
    if os.path.basename(_here).lower() == "dist" and (
        os.path.exists(os.path.join(_parent, ".env"))
        or os.path.exists(os.path.join(_parent, "session.session"))
    ):
        _here = _parent
else:
    _here = os.path.dirname(os.path.abspath(__file__))
    _bundle_dir = _here

# FIX 1: Removed __pycache__ nuke — it deleted Python's bytecode cache on every
# launch, forcing full re-parse of all modules on next run (+100–400 ms cold start).
# Delete manually with `find . -name "*.pyc" -delete` if stale bytecode is needed.

if not _is_frozen:
    for _pkg, _imp in [
        ("aiohttp", "aiohttp"),
        ("telethon", "telethon"),
        ("cryptg", "cryptg"),
        ("orjson", "orjson"),
    ]:
        try:
            __import__(_imp)
        except ImportError:
            print(f"\n[setup] Installing {_pkg}...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg])
                # FIX 3: Use os.execv instead of subprocess.call + sys.exit.
                # subprocess.call forks a child and then exits the parent, which can
                # leave orphan processes. os.execv replaces the current process image
                # cleanly — no fork, no orphan.
                print("\n[setup] Packages installed! Restarting...")
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except subprocess.CalledProcessError:
                print(f"[setup] ⚠️ FAILED to install {_pkg}.")
                if _pkg == "cryptg":
                    print(
                        "[setup] -> cryptg is optional (requires C-compiler). Skipping!"
                    )
                else:
                    print(
                        "[setup] -> Fatal error installing required package. Exiting."
                    )
                    sys.exit(1)

# ─────────────────────────────────────────────────────────────────────────────
import re, json, asyncio
from urllib.parse import unquote
from aiohttp import web
from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
)

# ── CONFIG ────────────────────────────────────────────────────────────────────
_exe_dir = os.path.dirname(sys.executable)

_env_paths = [
    os.path.join(_here, ".env"),
    os.path.join(os.path.dirname(_here), ".env"),
    os.path.join(os.getcwd(), ".env"),
    os.path.join(_exe_dir, ".env"),
    os.path.join(os.path.dirname(_exe_dir), ".env"),
]

_env_path = next((p for p in _env_paths if os.path.exists(p)), None)

if _env_path:
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip()
            # FIX 2: Only strip one matching pair of quotes rather than blindly
            # calling .strip('"').strip("'") which corrupts mixed-quote values
            # like TOKEN='abc"def' and breaks auth silently.
            if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                _v = _v[1:-1]
            os.environ[_k] = _v
    print("[setup] Loaded .env")

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
CACHE_FILE = os.path.join(_here, "tg_cache.json")
ALBUMS_FILE = os.path.join(_here, "tg_albums.json")
IMDB_CACHE_FILE = os.path.join(_here, "imdbcache.json")
POSTERS_DIR = os.path.join(_here, ".posters")
SESSION = os.path.join(_here, "session")
PORT = int(os.environ.get("PORT", 5000))
HOST = os.environ.get("HOST", "0.0.0.0")
_PASSWORD_HASH = os.environ.get("PASSWORD_HASH", "")
SESSION_TIMEOUT = 3600  # 1 hour
LOGIN_REQUIRED = os.environ.get("LOGIN_REQUIRED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# ── LOCAL BOT API SERVER ───────────────────────────────────────────────────────
# When set, streaming uses the local Bot API server instead of Telethon MTProto.
# This eliminates file_reference expiration, invalid limit errors, and the 20MB
# download cap.  The server can be auto-started by StreamVault (see bot_api_server.py).
BOT_API_URL = os.environ.get("BOT_API_URL", "http://localhost:8081")
# Directory where the Bot API stores downloaded files (--dir flag).
# If set, StreamVault reads files directly from disk for zero-latency seeks.
# If empty, falls back to the Bot API's HTTP file endpoint (still fast on localhost).
# Auto-detected by bot_api_server.py if not set — creates tg-bot-api-files/ next
# to the project.
BOT_API_DIR = os.environ.get("BOT_API_DIR", "")
# Master switch: set to "1" to enable Bot API streaming (requires disk storage).
# Default is "0" — pure Telethon streaming with DC-aware client selection.
USE_BOT_API = os.environ.get("USE_BOT_API", "0").strip().lower() not in (
    "1",
    "true",
    "yes",
)
# Path to the telegram-bot-api binary.  If empty, bot_api_server.py will
# auto-detect it by searching PATH and common directories.
BOT_API_BIN = os.environ.get("BOT_API_BIN", "")
# HTTP port for the local Bot API server (only used when auto-starting).
BOT_API_PORT = int(os.environ.get("BOT_API_PORT", "8081"))
# Directory for the Bot API server's file cache (--dir flag).
# If empty, bot_api_server.py creates tg-bot-api-files/ next to the project.
BOT_API_FILES_DIR = os.environ.get("BOT_API_FILES_DIR", "")
# Use --local flag (removes 20MB download cap).  Default: on.
BOT_API_LOCAL = os.environ.get("BOT_API_LOCAL", "1").strip().lower() not in (
    "0",
    "false",
    "no",
)
# Optional: path to a log file for the Bot API server output.
BOT_API_LOG = os.environ.get("BOT_API_LOG", "")


# ── VLC STREAM TUNING ──────────────────────────────────────────────────────────
# Tweak these in .env based on the user's network speed/stability.
VLC_BLOCK_SIZE = int(
    os.environ.get("VLC_BLOCK_SIZE", 1 * 1024 * 1024)
)  # bytes per MTProto call
VLC_WORKERS = int(os.environ.get("VLC_WORKERS", 2))  # parallel download workers
VLC_WRITE_CHUNK = int(
    os.environ.get("VLC_WRITE_CHUNK", 512 * 1024)
)  # bytes per resp.write()
VLC_PREFETCH_BLOCKS = int(
    os.environ.get("VLC_PREFETCH_BLOCKS", 30)
)  # blocks to pre-fetch ahead of cursor

# ── STREAMING RESILIENCE / RETRY TUNING ─────────────────────────────────────────
# Existing env var names kept as-is so any .env already set up for these
# keeps working unchanged.
TG_WINDOW_BLOCKS = int(os.environ.get("STREAMVAULT_TG_WINDOW_BLOCKS", "64"))
CHUNK_READ_TIMEOUT = float(os.environ.get("STREAMVAULT_CHUNK_READ_TIMEOUT", "12"))
CIRCUIT_BREAKER_THRESHOLD = int(
    os.environ.get("STREAMVAULT_CIRCUIT_BREAKER_THRESHOLD", "3")
)
PREFETCH_AHEAD_S = int(
    os.environ.get("STREAMVAULT_PREFETCH_AHEAD_S", "86400")
)  # Prefetch 24h (no limit) ahead of playhead
PREFETCH_CHUNK = int(os.environ.get("STREAMVAULT_PREFETCH_CHUNK_MB", "4")) * 1024 * 1024
PROGRESSIVE_SEGMENT = (
    int(os.environ.get("STREAMVAULT_PROGRESSIVE_SEGMENT_MB", "16")) * 1024 * 1024
)
PROGRESSIVE_MAX_LANES = int(
    os.environ.get("STREAMVAULT_PROGRESSIVE_MAX_LANES", "8")
)  # Multi-lane sequential loading

# New knobs (no prior env var existed; fresh names, same defaults as before).
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "12"))  # max retries per read window
RETRY_BASE_S = float(
    os.environ.get("RETRY_BASE_S", "0.1")
)  # fast first retry on transient drops
RETRY_MAX_S = float(os.environ.get("RETRY_MAX_S", "5.0"))
CIRCUIT_BREAKER_GLOBAL = int(
    os.environ.get("CIRCUIT_BREAKER_GLOBAL", "10")
)  # total failures before giving up on this offset
DC_UNHEALTHY_THRESHOLD = int(
    os.environ.get("DC_UNHEALTHY_THRESHOLD", "3")
)  # consecutive failures before marking DC unhealthy
DC_RECOVERY_S = float(
    os.environ.get("DC_RECOVERY_S", "30.0")
)  # seconds before retrying an unhealthy DC
ACTIVE_READ_ZONE = int(
    os.environ.get("ACTIVE_READ_ZONE", str(16 * 1024 * 1024))
)  # buffer zone reserved for active VLC reads
PROGRESSIVE_STALL_FALLBACK_S = float(
    os.environ.get("PROGRESSIVE_STALL_FALLBACK_S", "0.3")
)

# ── VLC HTTP STATUS API (routes.py local control interface) ───────────────────
VLC_HTTP_PORT = int(
    os.environ.get("VLC_HTTP_PORT", "9091")
)  # VLC's own HTTP status API (separate from PORT)
VLC_HTTP_PASS = os.environ.get(
    "VLC_HTTP_PASS", ""
)  # Windows requires non-empty password for VLC's HTTP interface

# ── TELETHON CLIENT (set in main) ─────────────────────────────────────────────
client: TelegramClient = None
# Bot client — started alongside the user client in alpha.py.
# Used exclusively by _get_msg() in streaming.py to re-fetch messages with a
# fresh access_hash so streams never expire during playback.
# Set BOT_TOKEN in .env; leave blank to disable (falls back to user client).
bot_client: TelegramClient = None
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Pool of extra TelegramClient instances for multi-DC parallel streaming.
# Populated in alpha.py after auth. Workers round-robin across these for
# independent rate-limit buckets per connection.
client_pool: list = []

# ── MIME MAP ──────────────────────────────────────────────────────────────────
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".ts", ".wmv"}

_MIME_MAP = {
    "video/x-matroska": "video/mp4",
    "video/x-msvideo": "video/mp4",
    "video/quicktime": "video/mp4",
    "video/x-flv": "video/mp4",
    "application/octet-stream": "video/mp4",
}


def _safe_mime(mime):
    return _MIME_MAP.get(mime, mime) or "video/mp4"

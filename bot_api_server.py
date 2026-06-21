"""bot_api_server.py — Auto-manage the Local Telegram Bot API Server subprocess.

Starts the `telegram-bot-api` binary as a child process with the correct
flags, waits for it to become ready, and tears it down on exit.  This
eliminates the need to manually launch the server before StreamVault.

Architecture
------------
1. _find_binary()         — locate the telegram-bot-api binary on disk
2. _detect_files_dir()    — find or create the Bot API file cache directory
3. start_bot_api_server() — launch subprocess, wait for /getMe to respond
4. stop_bot_api_server()  — graceful SIGTERM → SIGKILL shutdown
5. get_bot_api_status()   — health check / introspection

Usage in alpha.py
-----------------
    from bot_api_server import start_bot_api_server, stop_bot_api_server

    # After loading config, before starting aiohttp:
    await start_bot_api_server()

    # On shutdown:
    await stop_bot_api_server()

Environment Variables
---------------------
BOT_API_BIN       — path to telegram-bot-api binary (auto-detected if empty)
BOT_API_PORT      — HTTP port for the Bot API server (default: 8081)
BOT_API_FILES_DIR — directory for Bot API file cache (auto-created if empty)
BOT_API_LOCAL     — use --local flag to remove 20MB limit (default: 1)
BOT_API_LOG       — path to Bot API log file (default: stderr only)
"""

import asyncio
import os
import signal
import shutil
import subprocess
import sys
import threading
import time as _time

import config as _cfg

# ═══════════════════════════════════════════════════════════════════════════════
# BINARY DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════

_BINARY_NAMES = ["telegram-bot-api", "telegram-bot-api.exe"]

_SEARCH_PATHS = [
    "",
    ".",
    "bin",
    os.path.join("..", "bin"),
    r"C:\telegram-bot-api",
    r"C:\tools\telegram-bot-api",
    "/usr/local/bin",
    "/usr/bin",
    "/opt/telegram-bot-api",
    os.path.join(_cfg._here, "telegram-bot-api"),
    os.path.join(_cfg._here, "..", "telegram-bot-api"),
    os.path.join(_cfg._here, "bin", "telegram-bot-api"),
]

# subprocess.Popen handle (works under WindowsSelectorEventLoopPolicy)
_proc: subprocess.Popen | None = None
_started_at: float = 0.0
_ready: bool = False


def _find_binary() -> str | None:
    env_bin = os.environ.get("BOT_API_BIN", "").strip()
    if env_bin:
        if os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
            return env_bin
        if sys.platform == "win32" and os.path.isfile(env_bin):
            return env_bin
        print(f"[botapi_server] BOT_API_BIN={env_bin} not found or not executable")
        return None

    for name in _BINARY_NAMES:
        found = shutil.which(name)
        if found:
            return found

    for search_dir in _SEARCH_PATHS:
        for name in _BINARY_NAMES:
            candidate = os.path.join(search_dir, name) if search_dir else name
            if os.path.isfile(candidate) and (
                os.access(candidate, os.X_OK) or sys.platform == "win32"
            ):
                return os.path.abspath(candidate)

    return None


def _detect_files_dir() -> str:
    env_dir = os.environ.get("BOT_API_FILES_DIR", "").strip()
    if env_dir:
        os.makedirs(env_dir, exist_ok=True)
        return env_dir
    default_dir = os.path.join(_cfg._here, "tg-bot-api-files")
    os.makedirs(default_dir, exist_ok=True)
    return default_dir


# ═══════════════════════════════════════════════════════════════════════════════
# SERVER LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════


async def start_bot_api_server() -> bool:
    global _proc, _started_at, _ready

    if not getattr(_cfg, "USE_BOT_API", False):
        print("[botapi_server] USE_BOT_API=0 — not starting Bot API server")
        return False

    if not _cfg.BOT_TOKEN:
        print("[botapi_server] BOT_TOKEN not set — not starting Bot API server")
        return False

    if _proc is not None and _proc.poll() is None:
        print("[botapi_server] already running")
        return True

    binary = _find_binary()
    if not binary:
        print(
            "[botapi_server] telegram-bot-api binary not found!\n"
            "  Set BOT_API_BIN in .env to the full path, or place the binary\n"
            "  in the project directory or system PATH.\n"
            "  Falling back to MTProto streaming."
        )
        return False

    api_id = _cfg.API_ID
    api_hash = _cfg.API_HASH
    http_port = int(os.environ.get("BOT_API_PORT", "8081"))
    files_dir = _detect_files_dir()
    use_local = os.environ.get("BOT_API_LOCAL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )
    log_path = os.environ.get("BOT_API_LOG", "").strip()

    _cfg.BOT_API_DIR = files_dir
    _cfg.BOT_API_URL = f"http://localhost:{http_port}"

    cmd = [
        binary,
        "--api-id",
        str(api_id),
        "--api-hash",
        api_hash,
        "--http-port",
        str(http_port),
        "--dir",
        files_dir,
        "--verbosity",
        "1",
    ]

    if use_local:
        cmd.append("--local")

    # --no-file-limit: disable the 20MB upload/download cap entirely.
    # When --local is set the cap is already lifted, but adding this
    # explicitly is belt-and-suspenders.
    cmd.append("--no-file-limit")

    # Print the full command for debugging (mask api-hash)
    masked_cmd = list(cmd)
    for i, arg in enumerate(masked_cmd):
        if arg == "--api-hash" and i + 1 < len(masked_cmd):
            masked_cmd[i + 1] = masked_cmd[i + 1][:6] + "..."
    print(f"[botapi_server] Starting: {' '.join(masked_cmd)}")
    print(f"[botapi_server] Files dir: {files_dir}")

    log_file = None
    if log_path:
        try:
            log_file = open(log_path, "a")
            print(f"[botapi_server] Logging to: {log_path}")
        except Exception as e:
            print(f"[botapi_server] Can't open log file {log_path}: {e}")

    # Use subprocess.Popen — works under WindowsSelectorEventLoopPolicy.
    # asyncio.create_subprocess_exec requires ProactorEventLoop on Windows.
    try:
        _proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file if log_file else subprocess.PIPE,
            stderr=log_file if log_file else subprocess.STDOUT,
            # On Windows, create a new process group so we can terminate
            # the whole tree cleanly on shutdown without killing the parent.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
            ),
        )
    except FileNotFoundError:
        print(f"[botapi_server] Failed to start: binary not found at {binary}")
        if log_file:
            log_file.close()
        return False
    except PermissionError:
        print(f"[botapi_server] Failed to start: permission denied for {binary}")
        if log_file:
            log_file.close()
        return False
    except Exception as e:
        print(f"[botapi_server] Failed to start: {e}")
        if log_file:
            log_file.close()
        return False

    _started_at = _time.time()
    print(f"[botapi_server] Process started (PID={_proc.pid})")

    # Drain stdout in a daemon thread to prevent pipe-buffer blocking.
    if not log_file and _proc.stdout:

        def _drain():
            try:
                for line in _proc.stdout:
                    decoded = line.decode(errors="replace").strip()
                    if not decoded:
                        continue
                    # Only surface errors and key lifecycle events;
                    # suppress noisy "Option can't be set" spam.
                    if (
                        "[ 1]" in decoded
                        or "Logged in" in decoded
                        or "server started" in decoded
                    ) and "Option can't be set" not in decoded:
                        print(f"[botapi] {decoded}")
            except Exception:
                pass

        t = threading.Thread(target=_drain, daemon=True)
        t.start()

    print("[botapi_server] Waiting for server to become ready...")
    ready = await _wait_for_ready(http_port, timeout=30)

    if ready:
        print(
            f"[botapi_server] ✓ Server ready on port {http_port} "
            f"(startup took {_time.time() - _started_at:.1f}s)"
        )
        print(f"[botapi_server]   BOT_API_DIR={files_dir}")
        print(f"[botapi_server]   BOT_API_URL=http://localhost:{http_port}")
        _ready = True
    else:
        print(
            "[botapi_server] ✗ Server did not become ready in time — falling back to MTProto"
        )
        await stop_bot_api_server()
        _ready = False

    return _ready


async def _wait_for_ready(port: int, timeout: float = 30) -> bool:
    from aiohttp import ClientSession, ClientTimeout

    token = _cfg.BOT_TOKEN
    url = f"http://localhost:{port}/bot{token}/getMe"

    deadline = _time.time() + timeout
    attempt = 0

    while _time.time() < deadline:
        # Poll sync Popen handle
        if _proc is not None and _proc.poll() is not None:
            print(f"[botapi_server] Process exited with code {_proc.returncode}")
            return False

        attempt += 1
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=2, connect=1)
            ) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("ok"):
                            return True
        except (ConnectionRefusedError, OSError, asyncio.TimeoutError):
            pass
        except Exception:
            pass

        wait = min(0.2 + attempt * 0.1, 1.0)
        await asyncio.sleep(wait)

    return False


async def stop_bot_api_server():
    global _proc, _ready

    if _proc is None:
        return

    _ready = False

    if _proc.poll() is not None:
        try:
            _proc.stdout.close()
        except Exception:
            pass
        _proc = None
        return

    print("[botapi_server] Stopping...")

    try:
        if sys.platform == "win32":
            # On Windows, use CTRL_BREAK_EVENT to the process group
            # (more reliable than terminate() for graceful shutdown)
            try:
                _proc.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                _proc.terminate()
        else:
            _proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        _proc = None
        return
    except Exception:
        pass

    # Poll for up to 5 seconds for graceful exit
    try:
        for _ in range(50):  # 50 × 0.1s = 5s
            if _proc.poll() is not None:
                break
            await asyncio.sleep(0.1)

        if _proc.poll() is not None:
            print(f"[botapi_server] Stopped (exit code: {_proc.returncode})")
        else:
            # Force kill
            print("[botapi_server] Force killing (graceful timeout)...")
            _proc.kill()
            _proc.wait(timeout=3)
    except Exception:
        pass
    finally:
        # Close stdout to release the drain thread
        try:
            _proc.stdout.close()
        except Exception:
            pass
        _proc = None


def get_bot_api_status() -> dict:
    running = _proc is not None and _proc.poll() is None
    return {
        "running": running,
        "ready": _ready,
        "pid": _proc.pid if running else None,
        "uptime_s": round(_time.time() - _started_at, 1) if running else 0,
        "binary_found": _find_binary() is not None,
        "url": getattr(_cfg, "BOT_API_URL", ""),
        "files_dir": getattr(_cfg, "BOT_API_DIR", ""),
    }

import sys
import os
import asyncio
import traceback
import multiprocessing
import threading
import shutil

# Add current dir to path to find local modules like alpha
_is_frozen = getattr(sys, "frozen", False)
_HERE = (
    os.path.dirname(sys.executable)
    if _is_frozen
    else os.path.dirname(os.path.abspath(__file__))
)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# Ensure multiprocessing works in frozen executable
multiprocessing.freeze_support()

# Redirect stdout/stderr to log file so we can debug headless execution
_user_data_dir = os.path.join(os.getenv('LOCALAPPDATA', _HERE), 'StreamVault')
# Look for .env in the executable directory (when bundled) and in the user data dir
_env_paths = [
    os.path.join(_user_data_dir, '.env'),
    os.path.join(_HERE, '.env'),
    os.path.join(os.path.dirname(_HERE), '.env'),
]
for _p in _env_paths:
    if os.path.exists(_p):
        _ENV_PATH = _p
        break
else:
    _ENV_PATH = None
if _ENV_PATH:
    # Load env variables (simplified: rely on config module later)
    os.environ['STREAMVAULT_ENV_PATH'] = _ENV_PATH

def _copy_if_missing(src_dir, name):
    src = os.path.join(src_dir, name)
    dst = os.path.join(_HERE, name)
    try:
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
    except Exception:
        pass

for _base in [
    os.path.dirname(_HERE),
    os.path.dirname(os.path.dirname(_HERE)),
    os.path.dirname(os.path.dirname(os.path.dirname(_HERE))),
]:
    if _base and os.path.isdir(_base):
        for _name in (
            "session.session",
            "bot_session.session",
            "tg_cache.json",
            "tg_albums.json",
            "imdbcache.json",
        ):
            _copy_if_missing(_base, _name)

os.makedirs(_user_data_dir, exist_ok=True)
_LOG = os.path.join(_user_data_dir, "backend_headless.log")
_lf = open(_LOG, "w", buffering=1, encoding="utf-8")

def _log(msg):
    _lf.write(msg + "\n")
    _lf.flush()
    try:
        # Also print to original stdout so Electron can read it
        print(msg, flush=True)
    except Exception:
        pass

# We don't overwrite sys.stdout here so that Electron can capture it
sys.stderr = _lf

def main():
    _log("[backend] Starting headless StreamVault backend...")
    
    try:
        import alpha
        
        # Mock events that launcher.pyw usually sets
        alpha._telegram_ready = threading.Event()
        alpha._cache_ready = threading.Event()
        
        # Override the alpha stdout logging to also use our _log
        original_print = builtins_print = __builtins__.get('print') if isinstance(__builtins__, dict) else __builtins__.print
        def custom_print(*args, **kwargs):
            msg = " ".join(str(a) for a in args)
            _lf.write(msg + "\n")
            _lf.flush()
            original_print(*args, **kwargs)
        
        if isinstance(__builtins__, dict):
            __builtins__['print'] = custom_print
        else:
            __builtins__.print = custom_print
            
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        _log("[backend] Running asyncio loop...")
        result = loop.run_until_complete(alpha._main())
        if result is False:
            _log("[backend] Startup failed.")
            sys.exit(1)
    except KeyboardInterrupt:
        _log("[backend] Interrupted by user/parent process. Exiting cleanly.")
    except Exception as e:
        _log(f"[backend] CRASHED:\n{traceback.format_exc()}")
        sys.exit(1)

if __name__ == "__main__":
    main()

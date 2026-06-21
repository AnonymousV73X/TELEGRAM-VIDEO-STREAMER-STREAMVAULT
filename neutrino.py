"""
launcher.py — StreamVault Desktop (Neutralinojs edition)
Replaces Electron with Neutralinojs for a ~2 MB frameless window shell.
The aiohttp backend (alpha.py) is untouched.

Bootstrap flow
--------------
1. Download neutralino binaries + neu CLI into <HERE>/neu_app/ (once)
2. Write neutralino.config.json + resources/index.html bridge
3. Start aiohttp backend in a daemon thread
4. Wait for backend port to open
5. Run neutralino binary — window opens
6. When neutralino exits, backend daemon dies with process
"""

import sys, os, time, subprocess, asyncio, threading, socket, traceback, multiprocessing, json, shutil, platform, urllib.request, zipfile, stat

multiprocessing.freeze_support()

_is_frozen = getattr(sys, "frozen", False)
_HERE = (
    os.path.dirname(sys.executable)
    if _is_frozen
    else os.path.dirname(os.path.abspath(__file__))
)

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_LOG = os.path.join(_HERE, "streamvault.log")
_lf = open(_LOG, "w", buffering=1, encoding="utf-8")
_con = sys.__stdout__


def _log(msg):
    _lf.write(msg + "\n")
    _lf.flush()
    try:
        if _con:
            _con.write(msg + "\n")
            _con.flush()
    except Exception:
        pass


sys.stdout = _lf
sys.stderr = _lf

_log(f"[launcher] frozen={_is_frozen}  HERE={_HERE}")
_log(f"[launcher] sys.executable={sys.executable}")

PORT = int(os.environ.get("PORT", 5000))
_NAPP = os.path.join(_HERE, "neu_app")

# ── Neutralinojs release config ───────────────────────────────────────────────
_NEU_VERSION = "6.5.0"
_NEU_BASE = (
    f"https://github.com/neutralinojs/neutralinojs/releases/download/v{_NEU_VERSION}"
)

_PLAT = platform.system().lower()  # 'windows' | 'linux' | 'darwin'
_ARCH = platform.machine().lower()  # 'amd64'/'x86_64' | 'arm64'

# binary name inside the zip / on disk
if _PLAT == "windows":
    _BIN_NAME = "neutralino-win_x64.exe"
    _CLI_NAME = "neu-win.exe"
elif _PLAT == "darwin":
    _BIN_NAME = "neutralino-mac_x64" if "x86" in _ARCH else "neutralino-mac_arm64"
    _CLI_NAME = "neu-mac"
else:
    _BIN_NAME = "neutralino-linux_x64"
    _CLI_NAME = "neu-linux"

_BIN_PATH = os.path.join(_NAPP, _BIN_NAME)


# ── neutralino.config.json ────────────────────────────────────────────────────
def _neu_config():
    return {
        "applicationId": "com.streamvault.app",
        "version": "1.0.0",
        "defaultMode": "window",
        "cli": {
            "binaryName": _BIN_NAME.replace(".exe", ""),
            "resourcesPath": "/resources/",
            "extensionsPath": "/extensions/",
            "clientLibrary": "/resources/neutralino.js",
            "binaryVersion": _NEU_VERSION,
            "clientVersion": "6.5.0",
        },
        "modes": {
            "window": {
                "title": "StreamVault",
                "width": 1843,
                "height": 972,
                "minWidth": 1200,
                "minHeight": 700,
                "center": True,
                "fullScreen": False,
                "alwaysOnTop": False,
                "icon": "",
                "enableInspector": False,
                "borderless": True,
                "maximize": False,
                "hidden": False,
                "resizable": True,
                "exitProcessOnClose": True,
                "useSavedState": False,
                "injectClientLibrary": True,
                "injectScript": "/resources/shim.js",
            }
        },
        "url": f"http://127.0.0.1:{PORT}/",
        "documentRoot": "/resources/",
        "enableServer": True,
        "enableNativeAPI": True,
        "tokenSecurity": "one-time",
        "logging": {"enabled": False},
        "nativeBlockList": [],
        "globalVariables": {"SV_PORT": PORT},
    }


# shim.js — injected by Neutralino into every page via injectScript.
# Runs after neutralino.js is injected, wires up pywebview compat + shows sv-wc.
_SHIM_JS = """
(function() {
  // Wire pywebview shim so existing onclick handlers work unchanged
  if (typeof Neutralino !== 'undefined' && !window.pywebview) {
    Neutralino.init();
    window.pywebview = {
      api: {
        minimize:   function() { Neutralino.window.minimize(); },
        toggle_max: function() {
          Neutralino.window.isMaximized().then(function(m) {
            m ? Neutralino.window.unmaximize() : Neutralino.window.maximize();
          });
        },
        close_win: function() { Neutralino.app.exit(); },
      }
    };
  }
  // Show window-control bar
  function _showWC() {
    var wc = document.getElementById('sv-wc');
    if (wc) wc.style.display = 'flex';
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _showWC);
  } else {
    _showWC();
  }
})();
"""

# index.html — tiny bridge that immediately navigates to the real app.
# We use window.location rather than loading it directly in config so the
# Neutralino JS runtime initialises first (gives us window controls).
_INDEX_HTML = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{
    width:100%; height:100%;
    background:#080808;
    display:flex; align-items:center; justify-content:center;
    font-family:'Segoe UI', system-ui, sans-serif;
    overflow:hidden;
  }}
  .card {{
    display:flex; flex-direction:column; align-items:center;
    transition:opacity 180ms ease, transform 180ms ease;
  }}
  .card.out {{ opacity:0; transform:scale(.97); }}
  .logo-wrap {{
    width:36px; height:36px; background:#f5c518; border-radius:18px;
    display:flex; align-items:center; justify-content:center;
    margin-bottom:22px;
    box-shadow:0 0 48px rgba(245,197,24,0.22),0 0 0 1px rgba(245,197,24,0.08);
  }}
  .logo-wrap svg {{ width:24px; height:24px; }}
  .title {{ font-size:23px; font-weight:700; letter-spacing:-0.4px; margin-bottom:38px; line-height:1; }}
  .s1 {{ color:#f5c518; }} .s2 {{ color:#e8e8e8; }}
  .ring {{
    width:32px; height:32px; border:2.5px solid #1e1e1e;
    border-top-color:#f5c518; border-radius:50%;
    animation:spin 0.65s linear infinite; margin-bottom:14px;
  }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
  .status {{
    font-size:10.5px; color:#3a3a3a; letter-spacing:0.8px;
    text-transform:uppercase; animation:pulse 2.2s ease-in-out infinite;
    margin-left:12px;
  }}
  @keyframes pulse {{ 0%,100%{{ opacity:.45; }} 50%{{ opacity:1; }} }}
  .version {{
    position:fixed; bottom:18px; font-size:9.5px; color:#1f1f1f;
    letter-spacing:1.2px; text-transform:uppercase;
  }}
</style>
</head>
<body>
<div class="card" id="card">
  <div class="logo-wrap">
    <svg viewBox="0 0 34 34" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M11 8L27 17L11 26V8Z" fill="#080808"/>
    </svg>
  </div>
  <div class="title"><span class="s1">Stream</span><span class="s2">Vault</span></div>
  <div class="ring"></div>
  <div class="status" id="st">Starting…</div>
</div>
<div class="version">StreamVault · Desktop</div>
<script src="neutralino.js"></script>
<script>
  Neutralino.init();
  var msgs = ["Starting…","Connecting to Telegram…","Loading media…","Almost ready…"];
  var i = 0;
  setInterval(function(){{
    i = (i+1) % msgs.length;
    document.getElementById('st').textContent = msgs[i];
  }}, 1900);

  // Poll until backend is up, then navigate
  function _tryLoad() {{
    fetch('http://127.0.0.1:{PORT}/')
      .then(function(r) {{
        if (r.ok) {{
          document.getElementById('card').classList.add('out');
          setTimeout(function() {{
            window.location.href = 'http://127.0.0.1:{PORT}/';
          }}, 200);
        }} else {{ setTimeout(_tryLoad, 400); }}
      }})
      .catch(function() {{ setTimeout(_tryLoad, 400); }});
  }}
  _tryLoad();

  // Window controls — Neutralino.window.* (called from alpha.py frontend)
  window.svBridge = {{
    minimize:  function() {{ Neutralino.window.minimize(); }},
    toggleMax: function() {{
      Neutralino.window.isMaximized().then(function(m) {{
        m ? Neutralino.window.unmaximize() : Neutralino.window.maximize();
      }});
    }},
    close: function() {{ Neutralino.app.exit(); }},
  }};
  // pywebview shim so existing alpha.py frontend calls keep working unchanged
  window.pywebview = {{
    api: {{
      minimize:   function() {{ Neutralino.window.minimize(); }},
      toggle_max: function() {{
        Neutralino.window.isMaximized().then(function(m) {{
          m ? Neutralino.window.unmaximize() : Neutralino.window.maximize();
        }});
      }},
      close_win: function() {{ Neutralino.app.exit(); }},
    }}
  }};
</script>
</body>
</html>"""


# ── Download helpers ──────────────────────────────────────────────────────────
def _download(url, dest, label):
    _log(f"[neu] downloading {label}…")
    try:
        urllib.request.urlretrieve(url, dest)
        _log(f"[neu] {label} OK")
        return True
    except Exception as e:
        _log(f"[neu] download failed ({label}): {e}")
        return False


def _extract_zip(zpath, member, dest):
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():
            if os.path.basename(name) == member:
                data = z.read(name)
                with open(dest, "wb") as f:
                    f.write(data)
                return True
    return False


def _ensure_neutralino():
    os.makedirs(os.path.join(_NAPP, "resources"), exist_ok=True)

    # Write config
    with open(
        os.path.join(_NAPP, "neutralino.config.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(_neu_config(), f, indent=2)

    # Write bridge index
    with open(
        os.path.join(_NAPP, "resources", "index.html"), "w", encoding="utf-8"
    ) as f:
        f.write(_INDEX_HTML)

    # Write shim (injected into every page by Neutralino via injectScript)
    with open(os.path.join(_NAPP, "resources", "shim.js"), "w", encoding="utf-8") as f:
        f.write(_SHIM_JS)

    # Download neutralino.js client lib if missing
    neu_js = os.path.join(_NAPP, "resources", "neutralino.js")
    if not os.path.isfile(neu_js):
        url = f"https://cdn.jsdelivr.net/npm/@neutralinojs/lib@{_NEU_VERSION}/dist/neutralino.js"
        if not _download(url, neu_js, "neutralino.js"):
            return False

    # Download binary if missing
    # Release zip: neutralinojs-v{VERSION}.zip — contains all platform binaries flat
    if not os.path.isfile(_BIN_PATH):
        zip_name = f"neutralinojs-v{_NEU_VERSION}.zip"
        zip_url = f"{_NEU_BASE}/{zip_name}"
        zip_path = os.path.join(_NAPP, zip_name)
        if not _download(zip_url, zip_path, zip_name):
            return False
        if not _extract_zip(zip_path, _BIN_NAME, _BIN_PATH):
            _log(
                f"[neu] {_BIN_NAME} not found in zip — check release assets for v{_NEU_VERSION}"
            )
            return False
        try:
            os.remove(zip_path)
        except Exception:
            pass

        # make executable on unix
        if _PLAT != "windows":
            st = os.stat(_BIN_PATH)
            os.chmod(_BIN_PATH, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    _log(f"[neu] binary ready: {_BIN_PATH}")
    return True


# ── Backend thread ────────────────────────────────────────────────────────────
def _run_backend():
    try:
        _log("[backend] starting…")
        import alpha

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(alpha._main())
    except Exception:
        _log("[backend] CRASHED:\n" + traceback.format_exc())


threading.Thread(target=_run_backend, daemon=True).start()


# ── Wait for backend port ─────────────────────────────────────────────────────
def _wait_ready(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=0.5)
            s.close()
            return True
        except OSError:
            time.sleep(0.4)
    return False


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not _ensure_neutralino():
        _log("[launcher] failed to set up Neutralinojs — exiting")
        sys.exit(1)

    _log("[launcher] waiting for backend…")
    if not _wait_ready():
        _log("[launcher] backend timeout — exiting")
        sys.exit(1)
    _log(f"[launcher] backend ready on port {PORT}")

    env = os.environ.copy()
    env["SV_PORT"] = str(PORT)

    _log(f"[launcher] starting Neutralino → http://127.0.0.1:{PORT}/")
    try:
        proc = subprocess.Popen(
            [_BIN_PATH, "--load-dir-res", "--path=."],
            cwd=_NAPP,
            env=env,
        )
        proc.wait()
        _log(f"[launcher] Neutralino exited (code {proc.returncode})")
    except FileNotFoundError:
        _log(f"[launcher] ERROR: binary not found at {_BIN_PATH}")
        sys.exit(1)
    except Exception:
        _log("[launcher] Neutralino crashed:\n" + traceback.format_exc())
        sys.exit(1)

    _log("[launcher] exiting")
    sys.exit(0)


"""
launcher.py — StreamVault Desktop (Electron edition)
Replaces pywebview with Electron for smooth frameless rendering on Windows
at any window size / DPI. The aiohttp backend (alpha.py) is untouched.

Bootstrap flow
--------------
1. Write Electron project files to  <HERE>/electron_app/
2. `npm install electron` there (once — skipped if node_modules exists)
3. Start aiohttp backend in a daemon thread
4. Wait for backend port to open
5. `npx electron .` — Electron opens the app window
6. When Electron exits, kill backend and exit Python
"""

import sys, os, time, subprocess, asyncio, threading, socket, traceback, multiprocessing, json, shutil

# ── CRITICAL: must be first thing in __main__ for frozen exe ──────────────────
multiprocessing.freeze_support()

_is_frozen = getattr(sys, "frozen", False)
_HERE = (
    os.path.dirname(sys.executable)
    if _is_frozen
    else os.path.dirname(os.path.abspath(__file__))
)

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ── Log file (tee to console) ─────────────────────────────────────────────────
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
_EAPP = os.path.join(_HERE, "electron_app")

# ── Electron project files ────────────────────────────────────────────────────
_PACKAGE_JSON = {
    "name": "streamvault",
    "version": "1.0.0",
    "description": "StreamVault Desktop",
    "main": "main.js",
    "scripts": {"start": "electron ."},
    "dependencies": {"electron": "^30.0.0"},
}

# main.js — frameless window, loads localhost, exposes window controls via IPC
_MAIN_JS = r"""
const { app, BrowserWindow, ipcMain, screen } = require('electron');
const path = require('path');

const PORT = process.env.SV_PORT || 5000;

// ── Single instance lock ─────────────────────────────────────────────────────
if (!app.requestSingleInstanceLock()) { app.quit(); process.exit(0); }

// ── Smooth rendering flags ───────────────────────────────────────────────────
app.commandLine.appendSwitch('enable-gpu-rasterization');
app.commandLine.appendSwitch('enable-zero-copy');
app.commandLine.appendSwitch('disable-gpu-vsync');
app.commandLine.appendSwitch('disable-frame-rate-limit');

// ── Kill elastic/rubberband overscroll ───────────────────────────────────────
app.commandLine.appendSwitch('disable-features', 'OverscrollHistoryNavigation,TouchpadOverscrollHistoryNavigation,ElasticOverscroll');

let win;

function createWindow() {
  const wa = screen.getPrimaryDisplay().workAreaSize;
  const W = Math.floor(wa.width  * 0.9);
  const H = Math.floor(wa.height * 0.9);

  win = new BrowserWindow({
    width:  W,
    height: H,
    x: Math.floor((wa.width  - W) / 2),
    y: Math.floor((wa.height - H) / 2),
    frame: false,
    transparent: false,
    backgroundColor: '#080808',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js'),
    },
  });

  // ── Drop frame rate during resize, restore after ─────────────────────────
  let _rt = null;
  win.on('will-resize', () => { win.webContents.setFrameRate(30); });
  win.on('resize', () => {
    clearTimeout(_rt);
    _rt = setTimeout(() => { win.webContents.setFrameRate(60); }, 150);
  });

  win.loadURL(`http://127.0.0.1:${PORT}/`);
  win.webContents.on('did-finish-load', () => {
    win.webContents.insertCSS('html, body, * { overscroll-behavior: none !important; }');
  });
  win.once('ready-to-show', () => { win.show(); win.focus(); });
  win.on('closed', () => { win = null; app.quit(); });
}

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
app.on('second-instance', () => {
  if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
});

// ── Window control IPC ───────────────────────────────────────────────────────
ipcMain.on('sv-minimize',     () => win && win.minimize());
ipcMain.on('sv-maximize',     () => win && (win.isMaximized() ? win.unmaximize() : win.maximize()));
ipcMain.on('sv-close',        () => win && win.close());
ipcMain.handle('sv-is-maximized', () => win ? win.isMaximized() : false);
"""

# preload.js — exposes svBridge AND a pywebview.api shim so existing
# alpha.py button handlers (window.pywebview.api.*) work without changes.
_PRELOAD_JS = r"""
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('svBridge', {
  minimize:    () => ipcRenderer.send('sv-minimize'),
  toggleMax:   () => ipcRenderer.send('sv-maximize'),
  close:       () => ipcRenderer.send('sv-close'),
  isMaximized: () => ipcRenderer.invoke('sv-is-maximized'),
});

// pywebview compatibility shim — keeps alpha.py window-control calls working
// without any changes to the frontend HTML/JS.
contextBridge.exposeInMainWorld('pywebview', {
  api: {
    minimize:   () => ipcRenderer.send('sv-minimize'),
    toggle_max: () => ipcRenderer.send('sv-maximize'),
    close_win:  () => ipcRenderer.send('sv-close'),
  },
});
"""


def _write_electron_project():
    os.makedirs(_EAPP, exist_ok=True)
    with open(os.path.join(_EAPP, "package.json"), "w", encoding="utf-8") as f:
        json.dump(_PACKAGE_JSON, f, indent=2)
    with open(os.path.join(_EAPP, "main.js"), "w", encoding="utf-8") as f:
        f.write(_MAIN_JS)
    with open(os.path.join(_EAPP, "preload.js"), "w", encoding="utf-8") as f:
        f.write(_PRELOAD_JS)
    _log("[electron] project files written")


def _npm_install():
    nm = os.path.join(_EAPP, "node_modules", "electron")
    if os.path.isdir(nm):
        _log("[electron] node_modules present — skipping npm install")
        return True
    _log("[electron] running npm install (first run, ~30 s)…")
    npm = shutil.which("npm") or "npm"
    try:
        _NO_WIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        r = subprocess.run(
            [npm, "install"],
            cwd=_EAPP,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=_NO_WIN,
        )
        if r.returncode != 0:
            _log(f"[electron] npm install FAILED:\n{r.stderr}")
            return False
        _log("[electron] npm install OK")
        return True
    except FileNotFoundError:
        _log(
            "[electron] ERROR: npm not found — install Node.js from https://nodejs.org"
        )
        return False
    except subprocess.TimeoutExpired:
        _log("[electron] npm install timed out")
        return False


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
    _write_electron_project()

    if not _npm_install():
        _log("[launcher] cannot continue without Electron — exiting")
        sys.exit(1)

    _log("[launcher] waiting for backend…")
    if not _wait_ready():
        _log("[launcher] backend timeout — exiting")
        sys.exit(1)
    _log(f"[launcher] backend ready on port {PORT}")

    npx = shutil.which("npx") or "npx"
    env = os.environ.copy()
    env["SV_PORT"] = str(PORT)

    _log(f"[launcher] starting Electron → http://127.0.0.1:{PORT}/")
    try:
        _NO_WIN = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen([npx, "electron", "."], cwd=_EAPP, env=env, creationflags=_NO_WIN)
        proc.wait()
        _log(f"[launcher] Electron exited (code {proc.returncode})")
    except FileNotFoundError:
        _log(
            "[launcher] ERROR: npx not found — install Node.js from https://nodejs.org"
        )
        sys.exit(1)
    except Exception:
        _log("[launcher] Electron crashed:\n" + traceback.format_exc())
        sys.exit(1)

    _log("[launcher] exiting")
    sys.exit(0)
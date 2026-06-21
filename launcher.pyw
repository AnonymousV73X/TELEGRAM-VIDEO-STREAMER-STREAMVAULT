"""
launcher.py — StreamVault Desktop (pywebview edition)
"""

import sys, os, time, subprocess, asyncio, threading, socket, traceback, multiprocessing, ctypes

multiprocessing.freeze_support()

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

_is_frozen = getattr(sys, "frozen", False)
_HERE = (
    os.path.dirname(sys.executable)
    if _is_frozen
    else os.path.dirname(os.path.abspath(__file__))
)

if _HERE not in sys.path:
    sys.path.insert(
        0, os.path.dirname(os.path.abspath(__file__)) if not _is_frozen else _HERE
    )

# Use LocalAppData for logs to avoid permission issues
_user_data_dir = os.path.join(os.getenv('LOCALAPPDATA', _HERE), 'StreamVault')
os.makedirs(_user_data_dir, exist_ok=True)
_LOG = os.path.join(_user_data_dir, "streamvault.log")
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

if not _is_frozen:
    _DEPS = [
        ("pywebview", "webview"),
        ("aiohttp", "aiohttp"),
        ("telethon", "telethon"),
    ]
    _restart = False
    for _pip, _imp in _DEPS:
        try:
            __import__(_imp)
        except ImportError:
            _log(f"[setup] Installing {_pip}...")
            try:
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", _pip],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _restart = True
            except subprocess.CalledProcessError:
                _log(f"[setup] ⚠ Failed to install {_pip}. Exiting.")
                sys.exit(1)

    if _restart:
        _log("[setup] Deps installed — restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)

_log("[launcher] importing webview...")
import webview

_log("[launcher] webview imported ok")

PORT = int(os.environ.get("PORT", 5000))


def _dwm_enable_hwaccel(hwnd):
    try:
        dwmapi = ctypes.WinDLL("dwmapi")

        class _MARGINS(ctypes.Structure):
            _fields_ = [
                ("cxLeftWidth", ctypes.c_int),
                ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int),
                ("cyBottomHeight", ctypes.c_int),
            ]

        hr = dwmapi.DwmExtendFrameIntoClientArea(
            hwnd, ctypes.byref(_MARGINS(-1, -1, -1, -1))
        )
        _log(f"[dwm] DwmExtendFrameIntoClientArea HRESULT={hr:#010x}")
        try:
            border_color = ctypes.c_uint32(0xFFFFFFFE)  # DWMWA_COLOR_NONE (removes border)
            hr_border = dwmapi.DwmSetWindowAttribute(
                hwnd,
                ctypes.c_int(34),  # DWMWA_BORDER_COLOR
                ctypes.byref(border_color),
                ctypes.sizeof(border_color)
            )
            _log(f"[dwm] DwmSetWindowAttribute Border Color None HRESULT={hr_border:#010x}")
            if hr_border != 0:
                black = ctypes.c_uint32(0x00000000)
                dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    ctypes.c_int(34),
                    ctypes.byref(black),
                    ctypes.sizeof(black)
                )
        except Exception as border_ex:
            _log(f"[dwm] failed to set border attribute: {border_ex}")
    except Exception as e:
        _log(f"[dwm] hw-accel failed: {e}")


def _dwm_set_maximized_state(hwnd, maximized):
    try:
        dwmapi = ctypes.WinDLL("dwmapi")

        class _MARGINS(ctypes.Structure):
            _fields_ = [
                ("cxLeftWidth", ctypes.c_int),
                ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int),
                ("cyBottomHeight", ctypes.c_int),
            ]

        if maximized:
            # Set DWM margins to 0 when maximized to remove borders and shadows completely
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_MARGINS(0, 0, 0, 0)))
            try:
                # Disable rounded corners on Win11 (DWMWA_WINDOW_CORNER_PREFERENCE = 33, DWMWCP_DONOTROUND = 1)
                corner = ctypes.c_uint32(1)
                dwmapi.DwmSetWindowAttribute(hwnd, ctypes.c_int(33), ctypes.byref(corner), ctypes.sizeof(corner))
            except Exception:
                pass
        else:
            # Re-enable frame extension for shadow/transitions when restored
            dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_MARGINS(-1, -1, -1, -1)))
            try:
                # Restore default corner preference
                corner = ctypes.c_uint32(0)
                dwmapi.DwmSetWindowAttribute(hwnd, ctypes.c_int(33), ctypes.byref(corner), ctypes.sizeof(corner))
            except Exception:
                pass
    except Exception as e:
        _log(f"[dwm] set maximized state={maximized} failed: {e}")


def _get_hwnd_for_webview():
    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        pid = os.getpid()
        found = []

        def _cb(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            wpid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                found.append(hwnd)
            return True

        ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return found[0] if found else None
    except Exception as e:
        _log(f"[dwm] HWND lookup failed: {e}")
        return None


_SPLASH_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box;}

html,body{
  width:100%;height:100%;
  background:#000;
  overflow:hidden;
  user-select:none;
  -webkit-user-select:none;
  font-family:'Segoe UI',system-ui,-apple-system,sans-serif;
}

/* ── Grid overlay ── */
.grid{
  position:fixed;inset:0;
  background-image:
    linear-gradient(rgba(245,197,24,0.03) 1px,transparent 1px),
    linear-gradient(90deg,rgba(245,197,24,0.03) 1px,transparent 1px);
  background-size:48px 48px;
  mask-image:radial-gradient(ellipse 60% 60% at 50% 50%,black 30%,transparent 80%);
  -webkit-mask-image:radial-gradient(ellipse 60% 60% at 50% 50%,black 30%,transparent 80%);
}

/* ── Center stage ── */
.stage{
  position:fixed;inset:0;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  gap:0;
  transition:opacity 180ms ease;
}
.stage.out{opacity:0;}

/* ── Icon mark ── */
.mark{
  width:56px;height:56px;
  margin-bottom:28px;
  opacity:0;
  animation:up 0.5s cubic-bezier(0.16,1,0.3,1) 0.1s forwards;
}
.mark-inner{
  width:100%;height:100%;
  border:1.5px solid rgba(245,197,24,0.35);
  border-radius:14px;
  display:flex;align-items:center;justify-content:center;
  position:relative;
  background:rgba(245,197,24,0.04);
}
.mark-inner::before{
  content:'';
  position:absolute;inset:0;
  border-radius:13px;
  background:radial-gradient(circle at 35% 30%,rgba(245,197,24,0.12) 0%,transparent 65%);
}

/* ── Wordmark ── */
.wordmark{
  font-size:32px;font-weight:700;
  letter-spacing:-0.5px;line-height:1;
  margin-bottom:6px;
  opacity:0;
  animation:up 0.55s cubic-bezier(0.16,1,0.3,1) 0.2s forwards;
}
.w-y{color:#f5c518;}
.w-w{color:#fff;}

/* ── Sub ── */
.sub{
  font-size:10px;font-weight:500;
  color:#333;
  letter-spacing:4px;
  text-transform:uppercase;
  margin-bottom:48px;
  opacity:0;
  animation:fade 0.5s ease 0.35s forwards;
}

/* ── Progress ── */
.bar-wrap{
  width:180px;height:1.5px;
  background:rgba(255,255,255,0.06);
  margin-bottom:18px;
  overflow:hidden;
  opacity:0;
  animation:fade 0.4s ease 0.55s forwards;
}
.bar-fill{
  height:100%;
  background:#f5c518;
  width:0%;
  transition:width 0.45s cubic-bezier(0.4,0,0.2,1);
}

/* ── Status ── */
.status{
  display:flex;align-items:center;gap:7px;
  opacity:0;
  animation:fade 0.4s ease 0.7s forwards;
}
.dot{
  width:3px;height:3px;border-radius:50%;
  background:#f5c518;
  flex-shrink:0;
  animation:blink 1.2s ease-in-out infinite;
}
@keyframes blink{0%,100%{opacity:.25;}50%{opacity:1;}}
.status-txt{
  font-size:9.5px;font-weight:600;
  color:#3a3a3a;
  letter-spacing:2.5px;
  text-transform:uppercase;
  transition:opacity 0.25s ease;
}

/* ── Bottom line sweep ── */
.sweep{
  position:fixed;bottom:0;left:0;right:0;
  height:1px;background:rgba(245,197,24,0.05);
  overflow:hidden;
}
.sweep::after{
  content:'';
  position:absolute;top:0;left:-40%;width:40%;height:100%;
  background:linear-gradient(90deg,transparent,rgba(245,197,24,0.5),transparent);
  animation:sweep 2s ease-in-out infinite;
}
@keyframes sweep{from{left:-40%;}to{left:140%;}}

/* ── Edition tag ── */
.edition{
  position:fixed;bottom:14px;right:16px;
  font-size:8px;font-weight:600;
  color:#1e1e1e;letter-spacing:2.5px;
  text-transform:uppercase;
}

/* ── Window controls ── */
#sp-wc{
  position:fixed;top:10px;right:12px;
  display:flex;align-items:center;gap:6px;
  z-index:9999;opacity:0;
  transition:opacity 200ms ease;
}
.wc{
  width:11px;height:11px;border-radius:50%;border:none;
  cursor:pointer;display:flex;align-items:center;justify-content:center;
  transition:transform .15s,opacity .15s;opacity:.7;
}
.wc:hover{opacity:1;transform:scale(1.25);}
.wc svg{pointer-events:none;}

@keyframes up{
  from{opacity:0;transform:translateY(12px);}
  to{opacity:1;transform:translateY(0);}
}
@keyframes fade{from{opacity:0;}to{opacity:1;}}
</style>
</head>
<body>
<div class="grid"></div>

<div class="stage" id="stage">
  <div class="mark">
    <div class="mark-inner">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M6 4.5L20 12L6 19.5V4.5Z" fill="#f5c518"/>
      </svg>
    </div>
  </div>

  <div class="wordmark"><span class="w-y">Stream</span><span class="w-w">Vault</span></div>
  <div class="sub">Your Telegram Cinema</div>

  <div class="bar-wrap">
    <div class="bar-fill" id="prog"></div>
  </div>

  <div class="status">
    <div class="dot"></div>
    <div class="status-txt" id="st">Initialising</div>
  </div>
</div>

<div id="sp-wc">
  <button class="wc" id="sp-min" title="Minimise" style="background:#febc2e;width:18px;height:18px;">
    <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>
  </button>
  <button class="wc" id="sp-max" title="Maximise" style="background:#28c840;width:18px;height:18px;">
    <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3.5" stroke-linecap="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>
  </button>
  <button class="wc" id="sp-close" title="Close" style="background:#ff5f57;width:18px;height:18px;">
    <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
  </button>
</div>

<div class="sweep"></div>
<div class="edition">Desktop Edition</div>

<script>
(function(){
  var prog=document.getElementById('prog');
  var stEl=document.getElementById('st');

  /* Python calls this at each real milestone — no timer, no guessing */
  window._sv_status=function(pct,msg){
    prog.style.width=pct+'%';
    stEl.style.opacity='0';
    setTimeout(function(){stEl.textContent=msg;stEl.style.opacity='1';},140);
  };

  /* Called only after real page DOMContentLoaded signals Python, which then
     calls this — so the splash never disappears before the page is painted */
  window._sv_dismiss=function(){
    prog.style.width='100%';
    stEl.style.opacity='0';
    setTimeout(function(){stEl.textContent='Ready';stEl.style.opacity='1';},180);
    setTimeout(function(){document.getElementById('stage').classList.add('out');},320);
  };

  function _wireWC(){
    document.getElementById('sp-min').onclick=function(){window.pywebview.api.minimize();};
    document.getElementById('sp-max').onclick=function(){window.pywebview.api.toggle_max();};
    document.getElementById('sp-close').onclick=function(){window.pywebview.api.close_win();};
    document.getElementById('sp-wc').style.opacity='1';
  }
  if(window.pywebview){_wireWC();}
  else{window.addEventListener('pywebviewready',_wireWC);}
})();
</script>
</body>
</html>"""


class _API:
    def __init__(self):
        self._win = None
        self._maximised = False

    def set_window(self, win):
        self._win = win

    def minimize(self):
        if self._win:
            self._win.minimize()

    def toggle_max(self):
        if not self._win:
            return
        hwnd = _get_hwnd_for_webview()
        if self._maximised:
            if hwnd:
                _dwm_set_maximized_state(hwnd, False)
            if getattr(self, '_prev_bounds', None):
                self._win.move(self._prev_bounds[0], self._prev_bounds[1])
                self._win.resize(self._prev_bounds[2], self._prev_bounds[3])
            else:
                self._win.restore()
            self._maximised = False
        else:
            try:
                import ctypes
                class _RECT(ctypes.Structure):
                    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
                
                if hwnd:
                    rect = _RECT()
                    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
                    dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
                    ctypes.windll.gdi32.DeleteDC(hdc)
                    scale = dpi / 96.0 if dpi else 1.0
                    self._prev_bounds = (int(rect.left/scale), int(rect.top/scale), int((rect.right-rect.left)/scale), int((rect.bottom-rect.top)/scale))
                
                wa = _RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0)
                hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
                ctypes.windll.gdi32.DeleteDC(hdc)
                scale = dpi / 96.0 if dpi else 1.0

                # Calculate in physical pixels first, then convert to logical.
                # Use bottom = wa.bottom - 1 to leave exactly 1 physical pixel gap for taskbar.
                phys_left = wa.left - int(2 * scale)
                phys_top = wa.top - int(2 * scale)
                phys_right = wa.right + int(2 * scale)
                phys_bottom = wa.bottom - 1
                
                x = int(round(phys_left / scale))
                y = int(round(phys_top / scale))
                w = int(round((phys_right - phys_left) / scale))
                h = int(round((phys_bottom - phys_top) / scale))
                
                if hwnd:
                    _dwm_set_maximized_state(hwnd, True)
                self._win.move(x, y)
                self._win.resize(w, h)
            except Exception as e:
                _log(f"[maximize] error: {e}")
                self._win.maximize()
            self._maximised = True

    def close_win(self):
        if self._win:
            self._win.destroy()

    def page_ready(self):
        if not _page_ready_event.is_set():
            _log("[orchestrate] page_ready() called from JS — signalling")
            _page_ready_event.set()


_api = _API()


def _run_backend():
    try:
        _log("[backend] starting...")
        import alpha

        alpha._telegram_ready = telegram_ready_event
        alpha._cache_ready = cache_ready_event

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(alpha._main())
    except Exception:
        _log("[backend] CRASHED:\n" + traceback.format_exc())


threading.Thread(target=_run_backend, daemon=True).start()


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


_page_ready_event = threading.Event()
telegram_ready_event = threading.Event()
cache_ready_event = threading.Event()


def _js_status(win, pct, msg):
    try:
        safe = msg.replace("'", "\\'")
        win.evaluate_js(f"window._sv_status && window._sv_status({pct},'{safe}');")
    except Exception:
        pass


def _orchestrate(win):
    try:
        _log("[orchestrate] waiting for aiohttp port...")
        _js_status(win, 10, "Starting backend")
        if not _wait_ready():
            _log("[orchestrate] backend timeout")
            _js_status(win, 100, "Timeout — check logs")
            return

        _log("[orchestrate] port open — waiting for Telegram...")
        _js_status(win, 35, "Connecting to Telegram")
        tg_ok = telegram_ready_event.wait(timeout=30)
        _log(f"[orchestrate] telegram={'ok' if tg_ok else 'timeout'}")

        _js_status(win, 68, "Loading media index")
        cache_ok = cache_ready_event.wait(timeout=20)
        _log(f"[orchestrate] cache={'ok' if cache_ok else 'timeout'}")

        _js_status(win, 92, "Almost ready")
        time.sleep(0.25)

        # Load real page first — splash stays visible while page builds its DOM.
        # render._head() calls window.pywebview.api.page_ready() on DOMContentLoaded
        # which sets _page_ready_event.  Only dismiss splash after that fires so the
        # user never sees a blank/half-painted transition.
        _log("[orchestrate] loading real URL")
        win.load_url(f"http://127.0.0.1:{PORT}/")

        signalled = _page_ready_event.wait(timeout=20)
        _log(f"[orchestrate] page-ready {'ok' if signalled else 'timeout'}")

        # render._head() already fades body 0→1 on DOMContentLoaded before calling
        # page_ready(), so the page is visually complete by the time we reach here.
        # No extra dismiss call needed — load_url replaced the splash document.

        hwnd = _get_hwnd_for_webview()
        if hwnd:
            _dwm_enable_hwaccel(hwnd)

    except Exception:
        _log("[orchestrate] error:\n" + traceback.format_exc())


if __name__ == "__main__":
    _log("[launcher] starting webview with splash...")

    try:

        class _RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        _wa = _RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(_wa), 0)

        try:
            _hdc = ctypes.windll.gdi32.CreateDCW("DISPLAY", None, None, None)
            _dpi = ctypes.windll.gdi32.GetDeviceCaps(_hdc, 88)
            ctypes.windll.gdi32.DeleteDC(_hdc)
            _scale = _dpi / 96.0
        except Exception:
            _scale = 1.0

        _WW = int((_wa.right - _wa.left) / _scale)
        _WH = int((_wa.bottom - _wa.top) / _scale)
        _WX = 8
        _WY = 10
        _log(
            f"[launcher] work area logical: {_WW}x{_WH} @ ({_WX},{_WY}) scale={_scale}"
        )

        win = webview.create_window(
            title="StreamVault",
            html=_SPLASH_HTML,
            width=_WW,
            height=_WH,
            x=_WX,
            y=_WY,
            frameless=True,
            easy_drag=False,
            text_select=True,
            zoomable=True,
            confirm_close=False,
            js_api=_api,
            background_color="#000000",
        )

        _api.set_window(win)

        # CEF Python is not compatible with the Python 3.12 build used here.
        # Prefer the modern WebView2 backend directly instead of probing CEF.
        _gui = "edgechromium"
        _log("[launcher] using WebView2")

        webview.start(
            func=_orchestrate,
            args=(win,),
            debug=False,
            http_server=False,
            gui=_gui,
        )
    except Exception:
        _log("[webview] CRASHED:\n" + traceback.format_exc())

    _log("[launcher] exiting")
    sys.exit(0)

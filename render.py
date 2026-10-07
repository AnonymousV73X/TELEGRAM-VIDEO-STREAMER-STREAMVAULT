"""render.py — HTML rendering for StreamVault.

CSS, modal HTML, and the album virtual-grid JS are loaded from static/ once at
import time.  This keeps large string literals OUT of this Python source file so
Pylance's AST never indexes them — the primary cause of VSCode 2 GB RAM usage.
"""

import os, re, json, asyncio

try:
    import orjson as _json_fast

    def _fast_dumps(obj):
        return _json_fast.dumps(obj).decode()

except ImportError:

    def _fast_dumps(obj):
        return json.dumps(obj, ensure_ascii=False)


import urllib.parse as _up
import sys
from aiohttp import web
from config import _here, _bundle_dir, POSTERS_DIR
from cache import (
    _fetch_all_meta,
    _poster_mem,
    _albums_dirty,
    _rebuild_albums_index,
    _albums_index,
    _cache_mem,
)


def _fmt_rating(r: str) -> str:
    """Ensure rating is always displayed as X.Y (e.g. '7' → '7.0')."""
    if not r:
        return r
    try:
        f = float(r)
        # If it's a whole number, force one decimal place
        if f == int(f):
            return f"{f:.1f}"
        return r  # already has decimals, keep as-is
    except ValueError:
        return r


# ── Load static assets once at startup — NOT embedded as string literals ──────
def _static_dir():
    # When frozen, PyInstaller extracts data to sys._MEIPASS (available via _bundle_dir)
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(_bundle_dir, "static")
    # Development mode: locate static relative to project root
    return os.path.join(_here, "static")


def _read_static(name: str) -> str:
    path = os.path.join(_static_dir(), name)
    with open(path, encoding="utf-8") as f:
        return f.read()


BASE_CSS = _read_static("style.css")
_MODAL_HTML = _read_static("modal.html")

def _get_virtual_album_js_tag():
    """Return a <script src=...> tag with a hash-based cache buster so changes
    are picked up immediately without a server restart or hard-refresh."""
    import hashlib
    path = os.path.join(_static_dir(), "virtual_album.js")
    try:
        with open(path, "rb") as f:
            h = hashlib.md5(f.read()).hexdigest()[:8]
        return f'<script src="/static/virtual_album.js?v={h}"></script>'
    except Exception:
        # Fallback: inline the JS as before
        try:
            with open(path, encoding="utf-8") as f:
                return f"<script>\n{f.read()}\n</script>"
        except Exception:
            return ""


# Limit concurrent blocking poster-download threads so asyncio.gather()
# actually runs them in parallel instead of serialising on the executor pool.
_POSTER_SEM = asyncio.Semaphore(6)


# ── POSTER HELPERS ────────────────────────────────────────────────────────────
def _make_lqip_b64(image_bytes: bytes) -> str:
    """Generate a tiny 20px-wide JPEG placeholder, return as data-URI Base64."""
    try:
        from PIL import Image as _PILImage
        import io as _io, base64 as _b64

        img = _PILImage.open(_io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        lq_w, lq_h = 20, max(1, int(h * 20 / w))
        img = img.resize((lq_w, lq_h), _PILImage.BILINEAR)
        buf = _io.BytesIO()
        img.save(buf, format="JPEG", quality=30, optimize=True)
        b64 = _b64.b64encode(buf.getvalue()).decode()
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return ""


_POSTER_SM_W = 300


def _make_poster_sm(image_bytes: bytes, sm_path: str) -> bool:
    try:
        from PIL import Image as _PILImage
        import io as _io

        img = _PILImage.open(_io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size
        if w <= _POSTER_SM_W:
            with open(sm_path, "wb") as f:
                f.write(image_bytes)
            return True
        sm_h = max(1, int(h * _POSTER_SM_W / w))
        img = img.resize((_POSTER_SM_W, sm_h), _PILImage.LANCZOS)
        img.save(sm_path, format="JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def _cache_poster_sync(album_name: str, remote_url: str) -> str:
    """Download a poster and cache locally. Returns /poster/<slug> or remote URL."""
    if not remote_url or not remote_url.startswith("http"):
        return ""
    import hashlib as _hl
    import urllib.request as _ur

    os.makedirs(POSTERS_DIR, exist_ok=True)
    slug = _hl.md5(album_name.encode()).hexdigest()
    local_path = os.path.join(POSTERS_DIR, f"{slug}.jpg")
    sm_path = os.path.join(POSTERS_DIR, f"{slug}_sm.jpg")
    lqip_path = os.path.join(POSTERS_DIR, f"{slug}.lqip")
    if os.path.exists(local_path):
        if not os.path.exists(sm_path):
            try:
                with open(local_path, "rb") as _f:
                    _make_poster_sm(_f.read(), sm_path)
            except Exception:
                pass
        return f"/poster/{slug}"
    try:
        req = _ur.Request(remote_url, headers={"User-Agent": "Mozilla/5.0"})
        with _ur.urlopen(req, timeout=8) as resp:
            data = resp.read()
        with open(local_path, "wb") as f:
            f.write(data)
        _make_poster_sm(data, sm_path)
        lqip = _make_lqip_b64(data)
        if lqip:
            with open(lqip_path, "w") as f:
                f.write(lqip)
        return f"/poster/{slug}"
    except Exception as e:
        msg = f"[poster] failed to cache {album_name!r}: {e}"
        print(msg)
        try:
            from routes import _push_notification

            _push_notification(msg, kind="err")
        except Exception:
            pass
        # On 404 return empty so caller skips this album's poster silently
        err_str = str(e)
        if "404" in err_str or "Not Found" in err_str:
            return ""
        return remote_url


# ── HTML FRAGMENTS ────────────────────────────────────────────────────────────
_ANTI_FLASH_STYLE = (
    "<style>"
    "/* Anti-flash: html/body bg locked to #080808 so WebView2 never composites a white frame. */"
    "html{background:#080808}"
    "body{background:#080808}"
    "/* Nav is always visible — never flicker on page change */"
    "nav{opacity:1!important}"
    "/* Only the page content below the nav fades in */"
    "#sv-page{opacity:0}"
    "</style>"
)

_PAGE_OPEN = '<div id="sv-page">'
_PAGE_CLOSE = "</div>"


def _head(title):
    return (
        f"""<!DOCTYPE html><html lang="en" class="no-wc"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<!-- Synchronous: remove no-wc before first paint.
     Checks pywebview presence OR a sessionStorage flag set on first confirmed load,
     so window controls are visible on frame-0 of every page navigation. -->
<script>if(window.pywebview||sessionStorage.getItem('sv_wc')==='1'){{document.documentElement.classList.remove('no-wc');}}</script>
<title>{title}</title>"""
        + _ANTI_FLASH_STYLE
        + """
<script>
/* Reveal body as soon as DOM + inline styles are ready — before images/fonts */
document.addEventListener('DOMContentLoaded', function() {
  /* Signal launcher that real page is painted — dismisses splash.
     pywebview bridge may not exist yet on first load, so retry on pywebviewready. */
  function _signalReady() {
    try { if (window.pywebview && window.pywebview.api) { window.pywebview.api.page_ready(); return true; } } catch(e) {}
    return false;
  }
  if (!_signalReady()) { window.addEventListener('pywebviewready', _signalReady); }
  /* Fade in page content only — nav stays solid at all times */
  var pg = document.getElementById('sv-page');
  if (pg) {
    pg.style.transition = 'opacity 80ms ease';
    requestAnimationFrame(function() {
      pg.style.opacity = '1';
    });
  }
});
/* Nav progress bar: slim accent bar + status pill — replaces black-screen flash */
(function() {
  var _bar = null, _pill = null, _pillTimer = null, _startTs = 0;
  function _createBar() {
    if (_bar) return;
    _bar = document.createElement('div');
    _bar.id = 'sv-nav-bar';
    _bar.style.cssText = 'position:fixed;top:0;left:0;width:0%;height:3px;z-index:99999;background:var(--accent,#f5c518);pointer-events:none;transition:width .18s cubic-bezier(.4,0,.2,1),opacity .3s;opacity:0;border-radius:0 2px 2px 0;box-shadow:0 0 8px rgba(245,197,24,.5);';
    document.documentElement.appendChild(_bar);
    _pill = document.createElement('div');
    _pill.id = 'sv-nav-pill';
    _pill.style.cssText = 'position:fixed;bottom:18px;left:50%;transform:translateX(-50%) translateY(10px);z-index:99999;background:rgba(10,10,10,.92);border:1.5px solid var(--accent,#f5c518);border-radius:50px;padding:7px 18px;font-size:.73rem;font-weight:700;color:var(--accent,#f5c518);pointer-events:none;opacity:0;transition:opacity .18s,transform .18s;white-space:nowrap;font-family:inherit;display:flex;align-items:center;gap:8px;';
    document.documentElement.appendChild(_pill);
  }
  function _spinnerSVG() {
    return '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" style="animation:sv-spin .7s linear infinite;flex-shrink:0"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>';
  }
  function _show(label) {
    _createBar();
    _startTs = Date.now();
    _bar.style.transition = 'none'; _bar.style.width = '0%'; _bar.style.opacity = '1';
    requestAnimationFrame(function() {
      _bar.style.transition = 'width .18s cubic-bezier(.4,0,.2,1),opacity .3s';
      requestAnimationFrame(function() { _bar.style.width = '72%'; });
    });
    _pill.innerHTML = _spinnerSVG() + '<span>' + label + '</span>';
    _pill.style.opacity = '1'; _pill.style.transform = 'translateX(-50%) translateY(0)';
    if (_pillTimer) clearTimeout(_pillTimer);
  }
  function _done() {
    if (!_bar) return;
    _bar.style.width = '100%';
    _pill.style.opacity = '0'; _pill.style.transform = 'translateX(-50%) translateY(8px)';
    setTimeout(function() { _bar.style.opacity = '0'; _bar.style.width = '0%'; }, 220);
  }
  var _NAV_LABELS = { '/': 'Loading Home…', '/settings': 'Opening Settings…', '/manage': 'Opening Manage…', '/manage/albums': 'Opening Albums…' };
  function _labelFor(href) {
    var path = href.split('?')[0].split('#')[0];
    return _NAV_LABELS[path] || 'Navigating…';
  }
  // Expose so pages can trigger the bar programmatically (e.g. doHardRefresh)
  window._svNavBar = { show: _show, done: _done };
  document.addEventListener('click', function(e) {
    var a = e.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href');
    if (!href || href.startsWith('#') || href.startsWith('http') || href.startsWith('mailto') || a.hasAttribute('download') || a.target === '_blank') return;
    _show(_labelFor(href));
  });
  // Hook doHardRefresh globally so any page's Refresh button uses the bar
  window.doHardRefresh = function() {
    _show('Refreshing\u2026');
    setTimeout(function(){ window.location.reload(true); }, 60);
  };
  window.doRefresh = window.doHardRefresh;
  window.addEventListener('pageshow', function() { _done(); var pg=document.getElementById('sv-page'); if(pg) pg.style.opacity='1'; });
  window.addEventListener('pagehide', function() { if (_bar) { _bar.style.width = '85%'; } });
})();
(function(){var s=document.createElement('style');s.textContent='@keyframes sv-spin{to{transform:rotate(360deg)}}';document.head.appendChild(s);})();
</script>

<link rel="preload" href="/static/fonts/outfit-300.ttf" as="font" type="font/ttf" crossorigin>
<link rel="preload" href="/static/fonts/outfit-400.ttf" as="font" type="font/ttf" crossorigin>
<link rel="preload" href="/static/fonts/outfit-500.ttf" as="font" type="font/ttf" crossorigin>
<link rel="preload" href="/static/fonts/outfit-600.ttf" as="font" type="font/ttf" crossorigin>
<link rel="preload" href="/static/fonts/outfit-700.ttf" as="font" type="font/ttf" crossorigin>
<link rel="stylesheet" href="/static/style.css"></head><body>"""
    )


def _nav(placeholder="Search albums…", show_search=True):
    return f"""<nav>
  <a class="logo" href="/"><div class="logo-icon"><svg viewBox="-0.5 0 7 7"><path transform="translate(-291,-3606)" d="M296.494737,3608.57322 L292.500752,3606.14219 C291.83208,3605.73542 291,3606.25002 291,3607.06891 L291,3611.93095 C291,3612.7509 291.83208,3613.26444 292.500752,3612.85767 L296.494737,3610.42771 C297.168421,3610.01774 297.168421,3608.98319 296.494737,3608.57322"/></svg></div><span style="display:inline-flex;gap:0"><span style="color:var(--accent)">Stream</span><span style="color:#e8e8e8">Vault</span></span></a>
  <div class="search-wrap" style="margin-left:auto;margin-right:8px;{"" if show_search else "display:none;"}">
    <div class="search-box">
      <svg class="search-ico" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input id="searchInput" type="text" placeholder="{placeholder}">
    </div>
  </div>
  <style>.nav-notif-badge{{position:absolute;top:-4px;right:-4px;min-width:16px;height:16px;border-radius:8px;background:#f5c518;color:#000;font-size:.6rem;font-weight:800;display:flex;align-items:center;justify-content:center;padding:0 4px;animation:bell-pulse 2s ease-in-out infinite;pointer-events:none;}}@keyframes bell-pulse{{0%,100%{{transform:scale(1)}}50%{{transform:scale(1.15)}}}}</style>
  <div class="nav-right nav-links">

    <button class="nav-btn nav-history-btn" id="navHistoryBtn" onclick="toggleHistoryCard(event)" data-tip="Stream History" data-tip-pos="bottom">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
      History
    </button>
    <a class="nav-btn" href="/manage/albums" data-tip="Albums" data-tip-pos="bottom">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
      Albums
    </a>
    <a class="nav-btn" href="/manage" data-tip="Manage" data-tip-pos="bottom">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      Manage
    </a>
    <a class="nav-btn" id="navSettingsBtn" href="/settings" data-tip="Settings" data-tip-pos="bottom" style="position:relative;">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
      Settings
      <span class="nav-notif-badge" id="settingsBadge" style="display:none;"></span>
    </a>
    <!-- Window controls: shown only inside pywebview desktop app -->
    <div id="sv-wc" style="align-items:center;gap:6px;border-left:1.5px solid var(--border);padding-left:8px;margin-left:4px;">
    
      <button class="sv-wc-btn" id="sv-min-btn" onclick="if(window.pywebview)window.pywebview.api.minimize()" data-tip="Minimise" data-tip-pos="bottom">
        <svg class="sv-wc-ico" width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg>
      </button>
      
      <button class="sv-wc-btn" id="sv-max-btn" onclick="if(window.pywebview){{window.pywebview.api.toggle_max();_svToggleMaxIcon();}}" data-tip="Maximise / Restore" data-tip-pos="bottom">
        <svg class="sv-wc-ico" id="sv-max-ico" width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/>
          <line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>
        </svg>
      </button>
      
      <button class="sv-wc-btn" id="sv-close-btn" onclick="if(window.pywebview)window.pywebview.api.close_win()" data-tip="Close" data-tip-pos="bottom">
        <svg class="sv-wc-ico" width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#000" stroke-width="3" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <style>

      .sv-wc-btn{{width:18px;height:18px;border-radius:50%;border:none;padding:0;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:transform .25s cubic-bezier(.34,1.56,.64,1),filter .3s ease;}}
      .sv-wc-ico{{opacity:.7;transition:opacity .2s ease;flex-shrink:0;}}
      #sv-min-btn{{background:#ff9a1f;}}
      #sv-max-btn{{background:#2ecc71;}}
      #sv-close-btn{{background:#ff6b6b;}}
      #sv-min-btn:hover{{transform:scale(1.05);filter:brightness(1.10);}}
      #sv-max-btn:hover{{transform:scale(1.05) rotate(10deg);filter:brightness(1.10);}}
      #sv-close-btn:hover{{transform:scale(1.05);filter:brightness(1.10);}} 
    </style>
    
    <script>

    function _svToggleMaxIcon(){{
      _svMaximised=!_svMaximised;
      var ico=document.getElementById('sv-max-ico');
      if(!ico)return;
      if(_svMaximised){{
        ico.innerHTML='<polyline points="8 3 3 3 3 8"/><polyline points="16 21 21 21 21 16"/><line x1="3" y1="3" x2="10" y2="10"/><line x1="21" y1="21" x2="14" y2="14"/>';
        document.getElementById('sv-max-btn').title='Restore';
      }}else{{
        ico.innerHTML='<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>';
        document.getElementById('sv-max-btn').title='Maximise';
      }}
    }}
    </script>
    
    <style>
      /* Controls visible by default; html.no-wc hides them.
         no-wc is removed synchronously in <head> if pywebview exists,
         so controls are present on first paint — no pop-in on navigation. */
      html.no-wc #sv-wc {{ display:none !important; }}
      #sv-wc {{ display:flex; }}
    </style>
    <script>
    /* Fallback: pywebviewready fires asynchronously on first load only.
       After that pywebview is always present before DOMContentLoaded. */
    (function(){{
      function _showWC(){{
        sessionStorage.setItem('sv_wc','1');
        document.documentElement.classList.remove('no-wc');
      }}
      if(window.pywebview){{ _showWC(); }}
      else {{ window.addEventListener('pywebviewready', _showWC); }}
    }})();
    </script>
  </div>
  <button class="nav-ham" id="navHam" aria-label="Menu" onclick="toggleDrawer()">
    <span></span><span></span><span></span>
  </button>
</nav>
<div class="history-card" id="historyCard" style="display:none;">
  <div class="history-card-header">
    <h3>Stream History</h3>
    <button class="history-close-btn" onclick="document.getElementById('historyCard').style.display='none'">&times;</button>
  </div>
  <div class="history-search-container" style="padding:10px 18px;border-bottom:1px solid #1c1c1c;">
    <input type="text" id="historySearchInput" placeholder="Search stream history..." style="width:100%;padding:8px 12px;background:#161616;border:1px solid #252525;border-radius:6px;color:#e8e8e8;font-size:.78rem;outline:none;transition:border-color .15s;" oninput="filterHistory(this.value)">
  </div>
  <div class="history-card-list" id="historyList">
    <div class="history-loading">Loading...</div>
  </div>
</div>
<style>.history-card{{position:fixed;top:54px;right:max(16px,3vw);width:560px;max-height:600px;background:#0d0d0d;border:1.5px solid #252525;border-radius:14px;box-shadow:0 16px 48px rgba(0,0,0,.8);z-index:1001;display:flex;flex-direction:column;overflow:hidden;}}.history-card-header{{display:flex;align-items:center;justify-content:space-between;padding:14px 18px 10px;border-bottom:1px solid #1c1c1c;}}.history-card-header h3{{font-size:.84rem;font-weight:800;color:#e8e8e8;letter-spacing:.02em;}}.history-close-btn{{background:none;border:none;color:#606060;font-size:1.2rem;cursor:pointer;transition:color .2s;}}.history-close-btn:hover{{color:#e8e8e8;}}.history-card-list{{flex:1;overflow-y:auto;padding:6px 0;}}.history-loading{{padding:24px;text-align:center;color:#606060;font-size:.78rem;}}.history-item{{display:flex;align-items:center;gap:14px;padding:10px 18px;transition:background .15s;cursor:default;}}.history-item:hover{{background:rgba(245,197,24,.06);}}.history-item-thumb{{width:72px;height:54px;border-radius:6px;background:var(--surface);flex-shrink:0;overflow:hidden;}}.history-item-thumb img{{width:100%;height:100%;object-fit:cover;}}.history-item-info{{flex:1;min-width:0;}}.history-item-title{{font-size:.82rem;font-weight:700;color:#e8e8e8;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-clamp:2;overflow:hidden;}}.history-item-meta{{font-size:.68rem;color:#606060;margin-top:2px;display:flex;gap:6px;align-items:center;}}.history-item-actions{{display:flex;gap:4px;flex-shrink:0;}}.history-action-btn{{width:28px;height:28px;border-radius:6px;border:1px solid #1c1c1c;background:transparent;color:#606060;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:border-color .15s,color .15s;}}.history-action-btn:hover{{border-color:var(--accent);color:var(--accent);}}.history-action-btn svg{{width:12px;height:12px;}}.history-empty{{padding:32px 24px;text-align:center;color:#606060;font-size:.78rem;}}.history-more-btn:hover{{background:rgba(245,197,24,.06);border-color:var(--accent);color:var(--accent);}}#historySearchInput:focus{{border-color:var(--accent);}}@media(max-width:640px){{.history-card{{width:calc(100vw - 32px);right:16px;}}}}</style>
<div class="nav-drawer" id="navDrawer">
  <div class="search-box" style="width:100%;">
    <svg class="search-ico" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    <input id="searchInputMobile" type="text" placeholder="{placeholder}" style="width:100%;" oninput="var d=document.getElementById('searchInput');if(d){{d.value=this.value;d.dispatchEvent(new Event('input'));}}">
  </div>
 
  <button class="nav-btn" onclick="toggleHistoryCard(event)" style="width:100%;justify-content:flex-start;border-radius:var(--radius-sm);height:42px;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
    History
  </button>
  <a class="nav-btn" href="/manage/albums" style="width:100%;justify-content:flex-start;border-radius:var(--radius-sm);height:42px;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
    Albums
  </a>

  <a class="nav-btn" href="/manage" style="width:100%;justify-content:flex-start;border-radius:var(--radius-sm);height:42px;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
    Manage
  </a>
  <a class="nav-btn" id="navSettingsMobile" href="/settings" style="width:100%;justify-content:flex-start;border-radius:var(--radius-sm);height:42px;position:relative;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
    Settings
    <span class="nav-notif-badge" id="settingsBadgeMobile" style="display:none;position:absolute;top:6px;right:10px;"></span>
  </a>
  

</div>
<script>
function doNavFetch(btn) {{
  var orig = btn ? btn.innerHTML : '';
  if (btn) {{ btn.disabled = true; btn.querySelector && (btn.querySelector('svg') ? btn.innerHTML = btn.innerHTML.replace(/Fetch/, 'Fetching…') : null); }}
  fetch('/api/fetch', {{method:'POST'}}).then(function(r){{return r.json();}}).then(function(d){{
    if (d.ok) window.location.href = '/';
    else {{ if (btn) {{ btn.disabled = false; btn.innerHTML = orig; }} }}
  }}).catch(function() {{ if (btn) {{ btn.disabled = false; btn.innerHTML = orig; }} }});
}}
document.addEventListener('keydown', function(e) {{
  if ((e.ctrlKey || e.metaKey) && e.key === 'r') {{
    e.preventDefault();
    if (window._svNavBar) window._svNavBar.show('Refreshing…');
    setTimeout(function() {{ window.location.reload(true); }}, 60);
  }}
}});
function toggleDrawer(){{
  var ham=document.getElementById('navHam');
  var drawer=document.getElementById('navDrawer');
  var open=ham.classList.toggle('open');
  if(open)drawer.classList.add('open'); else drawer.classList.remove('open');
}}
document.addEventListener('click',function(e){{
  var ham=document.getElementById('navHam');
  var drawer=document.getElementById('navDrawer');
  if(!ham||!drawer) return;
  if(!ham.contains(e.target)&&!drawer.contains(e.target)){{
    ham.classList.remove('open'); drawer.classList.remove('open');
  }}
}});
(function(){{
  function _pollBell(){{
    fetch('/api/uncategorized_count').then(function(r){{return r.json();}}).then(function(d){{
      var badge=document.getElementById('settingsBadge');
      var badgeM=document.getElementById('settingsBadgeMobile');
      var c=d.count||0;
      if(c>0){{
        if(badge){{badge.style.display='flex';badge.textContent=c>99?'99+':c;}}
        if(badgeM){{badgeM.style.display='flex';badgeM.textContent=c>99?'99+':c;}}
      }}else{{
        if(badge)badge.style.display='none';
        if(badgeM)badgeM.style.display='none';
      }}
    }}).catch(function(){{}});
  }}
  if('requestIdleCallback' in window){{requestIdleCallback(function(){{_pollBell();}});}}else{{_pollBell();}}
  setInterval(_pollBell,10000);
  document.addEventListener('visibilitychange',function(){{if(document.visibilityState==='visible')_pollBell();}});
  function _clearBell(){{var b=document.getElementById('settingsBadge'),bm=document.getElementById('settingsBadgeMobile');if(b)b.style.display='none';if(bm)bm.style.display='none';fetch('/api/notifications/seen',{{method:'POST'}}).catch(function(){{}});}}
  ['navSettingsBtn','navSettingsMobile'].forEach(function(id){{var el=document.getElementById(id);if(el)el.addEventListener('click',_clearBell,{{passive:true}});}});

  function _syncPlaybackStatus(){{
    fetch('/api/playback_status').then(function(r){{return r.json();}}).then(function(d){{
      if(!d||!d.ok||!d.active)return;
      var act=d.active;
      if(!act.msg_id||act.state==='stopped')return;
      var mid=act.msg_id,pos=act.pos||0,dur=act.dur||0;
      var pct=dur>0?Math.min(100,Math.max(0,(pos/dur)*100)):0;
      var hrs=Math.floor(pos/3600),mins=Math.floor((pos%3600)/60),secs=Math.floor(pos%60);
      var tStr=(hrs>0?hrs+':':'')+(mins<10&&hrs>0?'0':'')+mins+':'+(secs<10?'0':'')+secs;
      var els=document.querySelectorAll('[data-mid="'+mid+'"]');
      els.forEach(function(el){{
        // 1. Grid Cards (Album View / Feeds)
        var art = el.querySelector('.video-art');
        if(art){{
          var rightRow = art.querySelector('.badge-row-right');
          if(!rightRow){{
            rightRow = document.createElement('div');
            rightRow.className = 'badge-row-right';
            art.appendChild(rightRow);
          }}
          var wb = rightRow.querySelector('.watched-badge');
          if(wb) wb.remove();
          var b = rightRow.querySelector('.unfinished-badge');
          if(!b){{
            b = document.createElement('span');
            b.className = 'unfinished-badge';
            rightRow.appendChild(b);
          }}
          b.textContent = '● '+Math.round(pct)+'%';
          b.style.color = 'var(--accent,#f5c518)';

          var fill = art.querySelector('.resume-progress-fill');
          if(!fill){{
            var bar = document.createElement('div');
            bar.className = 'resume-progress-bar';
            fill = document.createElement('div');
            fill.className = 'resume-progress-fill';
            bar.appendChild(fill);
            art.appendChild(bar);
          }}
          fill.style.width = pct+'%';
        }}

        // 2. History Sidebar Items
        var thumb = el.querySelector('.history-item-thumb');
        if(thumb){{
          var fill = thumb.querySelector('.resume-progress-fill');
          if(!fill){{
            var progressDiv = document.createElement('div');
            progressDiv.style.cssText = 'position:absolute;bottom:0;left:0;height:3px;background:var(--accent);';
            progressDiv.className = 'resume-progress-fill';
            thumb.appendChild(progressDiv);
            fill = progressDiv;
          }}
          fill.style.width = pct+'%';
          var meta = el.querySelector('.history-item-meta');
          if(meta){{
            var resumeText = meta.querySelector('span[style*="color:var(--accent)"]');
            if(!resumeText){{
              resumeText = document.createElement('span');
              resumeText.style.cssText = 'color:var(--accent);font-weight:600;';
              meta.appendChild(resumeText);
            }}
            resumeText.textContent = '▶ '+tStr+' ('+Math.round(pct)+'%)';
          }}
        }}
      }});
      if(window._VDATA&&Array.isArray(window._VDATA)){{
        var item=window._VDATA.find(function(x){{return x.id==mid;}});
        if(item){{item.resume={{pos:pos,dur:dur,updated_at:Date.now()/1000}};}}
      }}
    }}).catch(function(){{}});
  }}
  setInterval(_syncPlaybackStatus,2000);
}})();
var _historyLoaded=false;
var _historyOffset=0;
var _historyLimit=50;
var _historySearchQuery="";
var _historySearchTimeout=null;
window.toggleHistoryCard = function(e){{
  e.preventDefault();e.stopPropagation();
  var card=document.getElementById('historyCard');
  if(!card)return;
  if(card.style.display==='none'){{
    card.style.display='flex';
    var searchInput=document.getElementById('historySearchInput');
    if(searchInput){{searchInput.value="";}}
    _historySearchQuery="";
    if(!_historyLoaded){{loadHistory(false);_historyLoaded=true;}}
    else{{loadHistory(false);}}
  }}else{{
    card.style.display='none';
  }}
}}
window.filterHistory = function(val){{
  clearTimeout(_historySearchTimeout);
  _historySearchTimeout=setTimeout(function(){{
    _historySearchQuery=val;
    loadHistory(false);
  }},250);
}}
function loadHistory(append){{
  var list=document.getElementById('historyList');
  if(!list)return;
  if(!append){{
    _historyOffset=0;
    list.innerHTML='<div class="history-loading">Loading...</div>';
  }}else{{
    var moreContainer=document.getElementById('historyMoreContainer');
    if(moreContainer) moreContainer.remove();
  }}
  var url='/api/history?limit='+_historyLimit+'&offset='+_historyOffset;
  if(_historySearchQuery){{
    url+='&q='+encodeURIComponent(_historySearchQuery);
  }}
  fetch(url).then(function(r){{return r.json();}}).then(function(d){{
    if(!d.ok||!d.history||!d.history.length){{
      if(!append){{
        list.innerHTML=_historySearchQuery ? '<div class="history-empty">No results found</div>' : '<div class="history-empty">No stream history yet</div>';
      }}
      return;
    }}
    var html='';
    d.history.forEach(function(h){{
      var progressHtml = '';
      var resumeTextHtml = '';
      if(h.resume_pos && h.resume_pos > 5){{
        var percent = 0;
        if(h.resume_dur && h.resume_dur > 0){{
          percent = Math.min(100, Math.round((h.resume_pos / h.resume_dur) * 100));
        }}
        var hrs = Math.floor(h.resume_pos / 3600);
        var mins = Math.floor((h.resume_pos % 3600) / 60);
        var secs = Math.floor(h.resume_pos % 60);
        var tStr = '';
        if(hrs > 0) {{ tStr += hrs + ':'; }}
        tStr += (mins < 10 && hrs > 0 ? '0' : '') + mins + ':';
        tStr += (secs < 10 ? '0' : '') + secs;
        resumeTextHtml = '<span style="color:var(--accent);font-weight:600;">Resume at ' + tStr + (percent > 0 ? ' ('+percent+'%)' : '') + '</span>';
        progressHtml = '<div style="position:absolute;bottom:0;left:0;height:3px;background:var(--accent);width:'+percent+'%;"></div>';
      }}
      var thumb=h.thumb_url?'<img src="'+h.thumb_url+'" alt="">':'';
      var dur=h.duration?'<span>'+h.duration+'</span>':'';
      var qual=h.quality?'<span>'+h.quality+'</span>':'';
      var alb=h.album?'<a href="/album/'+encodeURIComponent(h.album)+'" style="color:var(--accent);text-decoration:none;">'+h.album+'</a>':'';
      html+='<div class="history-item" data-mid="'+h.message_id+'">'
        +'<div class="history-item-thumb" style="position:relative;">'+thumb+progressHtml+'</div>'
        +'<div class="history-item-info">'
        +'<div class="history-item-title">'+(h.title||'Untitled')+'</div>'
        +'<div class="history-item-meta">'+dur+qual+resumeTextHtml+alb+'</div>'
        +'</div>'
        +'<div class="history-item-actions">'
        +'<button class="history-action-btn" data-tip="Resume in VLC" data-action="vlc" data-mid="'+h.message_id+'" data-alb="'+encodeURIComponent(h.album||'')+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/></svg></button>'
        +'<button class="history-action-btn" data-tip="Remove" data-action="remove" data-mid="'+h.message_id+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>'
        +'</div></div>';
    }});
    if(!append){{
      list.innerHTML=html;
    }}else{{
      list.insertAdjacentHTML('beforeend', html);
    }}
    _historyOffset+=d.history.length;
    if(d.history.length===_historyLimit){{
      var moreHtml='<div class="history-more-container" id="historyMoreContainer" style="padding:12px;text-align:center;">'
        +'<button class="history-more-btn" id="historyMoreBtn" onclick="event.stopPropagation();loadHistory(true)" style="background:transparent;border:1px solid #252525;color:#e8e8e8;padding:6px 16px;border-radius:6px;font-size:.78rem;cursor:pointer;transition:background .15s,border-color .15s;">See More</button>'
        +'</div>';
      list.insertAdjacentHTML('beforeend', moreHtml);
    }}
  }}).catch(function(){{
    if(!append){{
      list.innerHTML='<div class="history-empty">Failed to load history</div>';
    }}
  }});
}}
document.addEventListener('click',function(e){{
  var btn=e.target.closest('[data-action]');
  if(!btn)return;
  var action=btn.dataset.action,mid=parseInt(btn.dataset.mid),alb=btn.dataset.alb||'';
  if(action==='vlc'){{launchVlcFromHistory(mid,alb,btn);}}
  else if(action==='remove'){{removeHistory(mid,btn);}}
}});
function removeHistory(mid,btn){{
  fetch('/api/history/remove',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message_id:mid}})}}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.ok){{var item=btn.closest('.history-item');if(item)item.remove();}}
  }});
}}
function _svShowVlcPill(msg,ok){{
  var p=document.getElementById('_svVlcPill');
  if(!p){{p=document.createElement('div');p.id='_svVlcPill';p.style.cssText='position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(10,10,10,.96);border:1.5px solid #3a3;border-radius:50px;padding:10px 22px;font-size:.82rem;font-weight:600;color:#7f7;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;white-space:nowrap;z-index:99999;';document.body.appendChild(p);}}
  p.textContent=msg;p.style.borderColor=ok?'#3a3':'#f44';p.style.color=ok?'#7f7':'#f77';
  p.style.opacity='1';p.style.transform='translateX(-50%) translateY(0)';
  clearTimeout(p._t);p._t=setTimeout(function(){{p.style.opacity='0';p.style.transform='translateX(-50%) translateY(20px)';}},3000);
}}
function launchVlcFromHistory(mid,alb,btn){{
  btn.disabled=true;
  btn.innerHTML='<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>';
  fetch('/api/launch_vlc',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{msg_id:mid}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(!d.ok){{
        _svShowVlcPill('\u26a0 Could not launch VLC: '+(d.error||'unknown'),false);
      }}else{{
        _svShowVlcPill('\u25b6 Opened in VLC',true);
        if(alb){{
          window.location.href='/album/'+alb;
        }}
      }}
      btn.disabled=false;
      btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/></svg>';
    }})
    .catch(function(){{_svShowVlcPill('\u26a0 VLC launch failed',false);btn.disabled=false;btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/></svg>';}});
}}
document.addEventListener('click',function(e){{
  var card=document.getElementById('historyCard');
  var btn=document.getElementById('navHistoryBtn');
  if(card&&card.style.display!=='none'&&btn&&!btn.contains(e.target)&&!card.contains(e.target)){{
    card.style.display='none';
  }}
}});
</script>""" + _PAGE_OPEN


# ── RENDER INDEX ──────────────────────────────────────────────────────────────
def _render_index(albums, total):
    album_data = []
    for a in albums:
        m = a.get("meta", {}) or {}
        year = m.get("year", "") or ""
        year = re.sub(r"[\u2013\u2014\-]+\s*$", "", year).strip()
        # Find latest video update date in the album
        dates = [v.get("date", "") for v in a.get("videos", []) if v.get("date")]
        latest_date = max(dates) if dates else ""
        album_data.append(
            {
                "name": a["name"],
                "count": len(a["videos"]),
                "sm": a.get("poster_sm_url", "") or "",
                "full": a.get("poster_url", "") or "",
                "lqip": a.get("lqip", "") or "",
                "year": year,
                "rating": _fmt_rating(m.get("rating", "") or ""),
                "mtype": m.get("type", "") or "",
                "plot": (m.get("plot", "") or "")[:300],
                "updated": latest_date,
            }
        )

    albums_json = _fast_dumps(album_data)
    empty_html = '<div class="empty"><div class="empty-icon"><svg viewBox="0 0 24 24" stroke-width="1.5"><path stroke-linecap="round" d="M3 7h18M3 12h18M3 17h18"/></svg></div><h3>No videos found</h3><p>Hit Refresh to fetch from Telegram.</p></div>'

    html = _head("StreamVault") + _nav() + f"""
<div class="page">
  <div class="hero">
    <div class="hero-badge"><div class="hero-badge-dot"></div><span>Live from Telegram</span></div>
    <h1>Your <span style="color:var(--accent)">Stream</span><span style="color:#e8e8e8">Vault</span></h1>
    <p class="hero-sub">All your Telegram videos, streamed instantly in the browser.</p>
    <div class="stats">
      <div class="stat"><span class="stat-n">{total}</span><span class="stat-l">Videos</span></div>
      <div class="stat"><span class="stat-n">{len(albums)}</span><span class="stat-l">Albums</span></div>
    </div>
  </div>
  <div class="sec-row"><h2>Albums</h2><span class="pill">{len(albums)}</span><div class="divider"></div>
    <div style="display:flex;gap:6px;margin-left:auto;align-items:center;">
      <span style="font-size:0.7rem;color:var(--text3);font-weight:600;margin-right:2px;text-transform:uppercase;letter-spacing:0.5px;">Filter:</span>
      <button class="alb-filter-btn active" id="fltAll"    onclick="_setFilter('all')">All</button>
      <button class="alb-filter-btn"        id="fltSeries" onclick="_setFilter('series')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
        Series
      </button>
      <button class="alb-filter-btn"        id="fltMovies" onclick="_setFilter('movie')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><rect x="2" y="2" width="20" height="20" rx="2"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M17 7h5M2 17h5M17 17h5"/></svg>
        Movies
      </button>
      <span style="font-size:0.7rem;color:var(--text3);font-weight:600;margin-left:10px;margin-right:2px;text-transform:uppercase;letter-spacing:0.5px;">Sort:</span>
      <button class="alb-filter-btn active" id="srtDate" onclick="_setSort('date')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
        Recent
      </button>
      <button class="alb-filter-btn" id="srtAlpha" onclick="_setSort('alpha')">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M4 6h16M4 12h10M4 18h6"/></svg>
        A-Z
      </button>
    </div>
  </div>
  <style>
  .alb-filter-btn{{height:30px;padding:0 14px;border-radius:50px;border:1.5px solid var(--border);background:transparent;color:var(--text2);font-size:.72rem;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:5px;transition:border-color .15s,color .15s,background .15s;white-space:nowrap;}}
  .alb-filter-btn:hover{{border-color:var(--accent);color:var(--accent);}}
  .alb-filter-btn.active{{border-color:var(--accent);color:var(--accent);background:var(--accent-lo);}}
  .album-card{{content-visibility:auto;contain-intrinsic-size:0 320px;}}
  </style>
  {"" if albums else empty_html}
  <div id="albumsGrid" style="position:relative;min-height:10px;"><div id="albSentinel" style="position:absolute;top:0;left:0;width:1px;pointer-events:none;"></div></div>
</div>
<!-- Back to top button -->
<button id="bttBtn" aria-label="Back to top" data-tip="Back to top" data-tip-pos="left" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">
  <svg viewBox="0 0 24 24"><polyline points="18 15 12 9 6 15"/></svg>
</button>
<script>
(function(){{
  var btn=document.getElementById('bttBtn');
  var _shown=false;
  function _check(){{
    // Appear after user scrolls past 25% of the page
    var threshold=document.documentElement.scrollHeight*0.25;
    var on=window.scrollY>threshold;
    if(on!==_shown){{_shown=on;btn.classList.toggle('visible',on);}};
  }}
  window.addEventListener('scroll',_check,{{passive:true}});
  _check();
}})();
</script>
{_MODAL_HTML}
<script>
(function(){{
// ── Virtual grid for index page ───────────────────────────────────────────────
(function(){{
  var ALBUM_DATA = {albums_json};
  var GAP    = 18;
  var CARD_H = 320;
  var COLS   = 6;
  var _colW  = 0;
  var _gridTop = 0;
  var grid     = document.getElementById('albumsGrid');
  var sentinel = document.getElementById('albSentinel');
  function _recacheLayout(){{
    var w = grid.clientWidth || document.documentElement.clientWidth || window.innerWidth;
    COLS  = w < 500 ? 2 : w < 900 ? 3 : w < 1200 ? 5 : 6;
    _colW = Math.floor((w - GAP*(COLS-1)) / COLS);
    CARD_H = Math.round(_colW * 1.5);
    _gridTop = grid.getBoundingClientRect().top + window.scrollY;
  }}
  var _vy=0,_py=0,_pt=0;
  function _trackVel(){{
    var t=performance.now(),dt=t-_pt;
    if(dt>0&&dt<200) _vy=(window.scrollY-_py)/dt;
    _py=window.scrollY;_pt=t;
  }}
  function _overscan(){{ return 2+Math.min(4,Math.floor(Math.abs(_vy)/0.4)); }}
  var _filtered = [];
  var _activeFilter = (function(){{try{{return sessionStorage.getItem('sv_home_filter')||'all';}}catch(e){{return 'all';}}}})();
  var _activeSort = (function(){{try{{return sessionStorage.getItem('sv_home_sort')||'date';}}catch(e){{return 'date';}}}})();
  function rowCount(){{ return Math.ceil(_filtered.length/COLS); }}
  function totalH(){{ return Math.max(0,rowCount()*(CARD_H+GAP)-GAP)+80; }}
  function cardTop(i){{ return Math.floor(i/COLS)*(CARD_H+GAP); }}
  function cardLeft(i){{ return (i%COLS)*(_colW+GAP); }}
  var _html = {{}};
  function _esc(s){{ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;'); }}
  function _buildCard(a){{
    var name=_esc(a.name),href='/album/'+encodeURIComponent(a.name),badge=a.count+' video'+(a.count!==1?'s':''),metaRow='';
    if(a.year||a.mtype){{
      var yspan=a.year?'<span class="album-year">'+_esc(a.year)+'</span>':'';
      var tclass=a.mtype?'album-type-badge album-type-'+a.mtype.toLowerCase():'';
      var tbadge=tclass?'<span class="'+tclass+'">'+_esc(a.mtype)+'</span>':'';
      metaRow='<div class="album-meta-row">'+yspan+tbadge+'</div>';
    }}
    var ratingBadge=a.rating?'<span class="album-rating-badge">&#x2b50; '+_esc(a.rating)+'</span>':'';
    var ambientBg='';
    var art;
    if(a.sm||a.full){{
      var src=a.sm||a.full;
      var lqipStyle=a.lqip?'background-image:url('+JSON.stringify(a.lqip)+')':'';
      ambientBg='<div class="card-ambient" data-sm="'+_esc(src)+'" style="'+lqipStyle+'"></div>';
      art='<div class="lqip-wrap" data-lqip="'+_esc(a.lqip)+'" style="'+lqipStyle+'"></div>'
         +'<img data-sm="'+_esc(src)+'" data-full="'+_esc(a.full||src)+'" alt="'+name+'" decoding="async">'
         +'<div class="fallback" style="display:none"><div class="fallback-icon"><svg viewBox="-0.5 0 7 7"><path transform="translate(-291,-3606)" d="M296.494737,3608.57322 L292.500752,3606.14219 C291.83208,3605.73542 291,3606.25002 291,3607.06891 L291,3611.93095 C291,3612.7509 291.83208,3613.26444 292.500752,3612.85767 L296.494737,3610.42771 C297.168421,3610.01774 297.168421,3608.98319 296.494737,3608.57322" fill="currentColor"/></svg></div></div>';
    }}else{{
      art='<div class="fallback"><div class="fallback-icon"><svg viewBox="-0.5 0 7 7"><path transform="translate(-291,-3606)" d="M296.494737,3608.57322 L292.500752,3606.14219 C291.83208,3605.73542 291,3606.25002 291,3607.06891 L291,3611.93095 C291,3612.7509 291.83208,3613.26444 292.500752,3612.85767 L296.494737,3610.42771 C297.168421,3610.01774 297.168421,3608.98319 296.494737,3608.57322" fill="currentColor"/></svg></div></div>';
    }}
    var plotHtml='';
    if(a.plot){{
      plotHtml='<p class="album-card-plot">'+_esc(a.plot.length>120?a.plot.slice(0,120)+'\u2026':a.plot)+'</p>';
    }}
    return '<a class="album-card" href="'+href+'" data-title="'+name.toLowerCase()+'">'
         +ambientBg
         +'<div class="album-art">'+art+'<div class="album-art-overlay"></div>'+ratingBadge+'</div>'
         +'<div class="album-info"><h3>'+name+'</h3>'+metaRow+plotHtml+'<p>'+badge+'</p></div>'+'</a>';
  }}
  function _prebuild(){{_html={{}};for(var i=0;i<ALBUM_DATA.length;i++) _html[i]=_buildCard(ALBUM_DATA[i]);}}
  function _loadImgInSlot(slotEl){{
    var img=slotEl.querySelector('img[data-sm]');
    if(!img||img._svQueued) return;
    img._svQueued=true;
    var lqip=slotEl.querySelector('.lqip-wrap');
    var ambient=slotEl.querySelector('.card-ambient');
    // Reset real image — lqip blur is already painted via inline style
    img.src='';img.style.transition='';img.style.opacity='0';img.classList.remove('loaded');
    if(lqip){{lqip.classList.remove('lqip-done');lqip.style.opacity='';lqip.style.transition='';}}
    var sm=img.dataset.sm;
    if(!sm){{img._svQueued=false;return;}}
    // decode() off main thread — browser won't block scroll handler
    var tmp=new Image();
    tmp.src=sm;
    tmp.decode().then(function(){{
      if(!img._svQueued)return;
      // Commit src, then rAF so lqip blur has at least one painted frame
      // before the opacity transition starts — eliminates blank flash
      img.src=sm;
      if(ambient){{ambient.style.backgroundImage='url('+JSON.stringify(sm)+')';ambient.classList.add('ambient-loaded');}}
      requestAnimationFrame(function(){{
        if(!img._svQueued)return;
        img.style.transition='opacity 0.7s ease';
        img.style.opacity='1';
        img.classList.add('loaded');
        if(lqip){{
          lqip.style.transition='opacity 0.9s ease 0.2s';
          lqip.classList.add('lqip-done');
        }}
        img._svQueued=false;
      }});
    }}).catch(function(){{
      var fb=slotEl.querySelector('.fallback');
      if(fb)fb.style.display='flex';
      if(img)img.style.display='none';
      img._svQueued=false;
    }});
  }}
  // ── index map for O(1) origIdx lookup ─────────────────────────────────────
  var _origIdx={{}};
  function _buildOrigIdx(){{for(var i=0;i<ALBUM_DATA.length;i++)_origIdx[ALBUM_DATA[i].name]=i;}}
  var _slots=[],_slotOf={{}},_freeList=[];
  function _allocPool(n){{
    while(_slots.length<n){{
      var el=document.createElement('div');
      // Fixed base style — compositor-promoted, never rewritten during scroll
      el.style.cssText='position:absolute;width:0;height:0;overflow:hidden;border-radius:12px;'
        +'will-change:transform;transform:translate(-9999px,0);visibility:hidden;';
      grid.appendChild(el);
      var s={{el:el,idx:-1,x:-9999,y:0,vis:false}};
      _slots.push(s);_freeList.push(s);
    }}
  }}
  function resetPool(){{
    _slotOf={{}};_freeList=[];
    for(var i=0;i<_slots.length;i++){{
      var s=_slots[i];s.idx=-1;s.vis=false;
      s.el.style.visibility='hidden';
      var img=s.el.querySelector('img[data-sm]');if(img)img._svQueued=false;
      _freeList.push(s);
    }}
  }}
  function _rebuildFiltered(q){{
    _filtered=ALBUM_DATA.filter(function(a){{
      var matchQ=!q||a.name.toLowerCase().indexOf(q)>=0;
      var matchT=_activeFilter==='all'||a.mtype.toLowerCase()===_activeFilter;
      return matchQ&&matchT;
    }});
    if(_activeSort==='date'){{
      _filtered.sort(function(a,b){{
        var da=a.updated||'';
        var db=b.updated||'';
        if(da!==db) return db.localeCompare(da);
        return a.name.localeCompare(b.name);
      }});
    }}else if(_activeSort==='alpha'){{
      _filtered.sort(function(a,b){{
        return a.name.localeCompare(b.name);
      }});
    }}
  }}
  window._setFilter=function(type){{
    _activeFilter=type;
    try{{sessionStorage.setItem('sv_home_filter',type);}}catch(e){{}}
    ['All','Series','Movies'].forEach(function(l){{
      var id='flt'+l,el=document.getElementById(id);
      if(el)el.classList.toggle('active',
        (l==='All'&&type==='all')||(l==='Series'&&type==='series')||(l==='Movies'&&type==='movie'));
    }});
    var q=document.getElementById('searchInput');
    _rebuildFiltered(q?q.value.toLowerCase().trim():'');
    resetPool();window.scrollTo({{top:0,behavior:'instant'}});
    _recacheLayout();var th=totalH()+'px';sentinel.style.height=th;grid.style.minHeight=th;render();
  }};
  window._setSort=function(type){{
    _activeSort=type;
    try{{sessionStorage.setItem('sv_home_sort',type);}}catch(e){{}}
    ['Date','Alpha'].forEach(function(l){{
      var id='srt'+l,el=document.getElementById(id);
      if(el)el.classList.toggle('active',
        (l==='Date'&&type==='date')||(l==='Alpha'&&type==='alpha'));
    }});
    var q=document.getElementById('searchInput');
    _rebuildFiltered(q?q.value.toLowerCase().trim():'');
    resetPool();window.scrollTo({{top:0,behavior:'instant'}});
    _recacheLayout();var th=totalH()+'px';sentinel.style.height=th;grid.style.minHeight=th;render();
  }};
  var _totalH=0;
  function _setTotalH(){{
    var th=totalH();
    if(th===_totalH)return;
    _totalH=th;
    var s=th+'px';sentinel.style.height=s;grid.style.minHeight=s;
  }}
  function render(){{
    var sy=window.scrollY,vpH=window.innerHeight,ov=_overscan(),rowH=CARD_H+GAP,rel=sy-_gridTop;
    var fr=Math.max(0,Math.floor((rel-ov*rowH)/rowH)),lr=Math.ceil((rel+vpH+ov*rowH)/rowH);
    var fi=fr*COLS,li=Math.min(_filtered.length-1,(lr+1)*COLS-1);
    // Evict out-of-range slots → return to freeList
    for(var k in _slotOf){{
      var ki=parseInt(k);
      if(ki<fi||ki>li){{
        var sl=_slotOf[k];sl.el.style.visibility='hidden';sl.vis=false;sl.idx=-1;
        var img=sl.el.querySelector('img[data-sm]');if(img)img._svQueued=false;
        _freeList.push(sl);delete _slotOf[k];
      }}
    }}
    // Place in-range slots — only mutate what changed
    for(var i=fi;i<=li;i++){{
      if(_slotOf[i])continue;
      var a=_filtered[i];if(!a)continue;
      var origIdx=_origIdx[a.name];
      var slot=_freeList.pop();if(!slot)continue;
      if(slot.idx>=0)delete _slotOf[slot.idx];
      slot.el.style.visibility='hidden';slot.vis=false;
      slot.el.innerHTML=_html[origIdx]||'';
      slot.idx=i;_slotOf[i]=slot;
    }}
    // Position pass — write transform/size only when changed, make visible,
    // then kick _loadImgInSlot so the LQIP blur is already painted before
    // the real image decode begins (prevents blank-card flash entirely).
    for(var j in _slotOf){{
      var ji=parseInt(j),sl=_slotOf[j];
      var tx=cardLeft(ji),ty=cardTop(ji);
      if(!sl.vis||sl.x!==tx||sl.y!==ty){{
        sl.el.style.width=_colW+'px';
        sl.el.style.height=CARD_H+'px';
        sl.el.style.transform='translate('+tx+'px,'+ty+'px)';
        if(!sl.vis){{
          sl.el.style.visibility='visible';
          _loadImgInSlot(sl.el);
        }}
        sl.vis=true;sl.x=tx;sl.y=ty;
      }}
    }}
  }}
  var _rt=null;
  window.addEventListener('resize',function(){{clearTimeout(_rt);_rt=setTimeout(function(){{
    _recacheLayout();
    _setTotalH();resetPool();render();
  }},80);}});
  var _raf=false;
  window.addEventListener('scroll',function(){{_trackVel();if(_raf)return;_raf=true;requestAnimationFrame(function(){{_raf=false;render();}});}},{{passive:true}});
  var _st=null,_si=document.getElementById('searchInput');
  if(_si){{_si.addEventListener('input',function(){{var q=this.value.toLowerCase().trim();clearTimeout(_st);_st=setTimeout(function(){{_rebuildFiltered(q);resetPool();window.scrollTo({{top:0,behavior:'instant'}});_recacheLayout();var th=totalH()+'px';sentinel.style.height=th;grid.style.minHeight=th;render();}},80);}});}}
  _buildOrigIdx();_prebuild();_rebuildFiltered('');grid.style.position='relative';
  ['All','Series','Movies'].forEach(function(l){{
    var id='flt'+l,el=document.getElementById(id);
    if(el)el.classList.toggle('active', (l==='All'&&_activeFilter==='all')||(l==='Series'&&_activeFilter==='series')||(l==='Movies'&&_activeFilter==='movie'));
  }});
  ['Date','Alpha'].forEach(function(l){{
    var id='srt'+l,el=document.getElementById(id);
    if(el)el.classList.toggle('active', (l==='Date'&&_activeSort==='date')||(l==='Alpha'&&_activeSort==='alpha'));
  }});
  
  requestAnimationFrame(function(){{
    _recacheLayout();
    var vpRows=Math.ceil(window.innerHeight/CARD_H)+8,poolN=Math.min(ALBUM_DATA.length,vpRows*COLS);
    _allocPool(Math.max(poolN,16));
    _setTotalH();render();
    // Re-snap gridTop after fonts + hero finish rendering (they shift layout)
    // Run at 100ms, 300ms, 600ms — covers slow font loads and layout shifts
    [100,300,600].forEach(function(ms){{
      setTimeout(function(){{
        var prev=_gridTop;
        _gridTop=grid.getBoundingClientRect().top+window.scrollY;
        if(Math.abs(_gridTop-prev)>1) render();
      }},ms);
    }});
  }});
}})();
}})();
</script>""" + _PAGE_CLOSE + """</body></html>"""
    return html


# ── RENDER ALBUM ──────────────────────────────────────────────────────────────
def _album_hero(
    album: str, videos: list, meta: dict, poster_sm_url: str = "", lqip: str = ""
) -> str:
    """Poster + title + plot + stats block at the top of the album page."""
    plot = meta.get("plot", "") or ""
    year = meta.get("year", "") or ""
    rating = _fmt_rating(meta.get("rating", "") or "")
    mtype = meta.get("type", "") or ""
    count = len(videos)
    count_s = f"{count} video{'s' if count != 1 else ''}"

    # Trim year ranges like "2019–2024" to just the start year
    year_disp = re.sub(r"[\u2013\u2014\-].*$", "", year).strip()

    badges = ""
    if rating:
        badges += f'<span class="album-rating-badge" style="position:static;margin:0">\u2b50 {rating}</span>'
    if mtype:
        cls = "album-type-series" if mtype.lower() == "series" else "album-type-movie"
        badges += (
            f'<span class="album-type-badge {cls}" style="margin:0">{mtype}</span>'
        )

    # Prefer locally cached small variant; fall back to remote OMDB URL
    src = poster_sm_url or meta.get("poster", "") or ""
    poster_html = ""
    if src:
        lqip_div = ""
        if lqip:
            lqip_style = f"background-image:url({json.dumps(lqip)})"
            lqip_div = f'<div class="alb-hero-lqip" style="{lqip_style}"></div>'
        poster_html = (
            '<div class="alb-hero-poster">'
            + lqip_div
            + f'<img src="{src}" loading="eager" decoding="async" alt=""'
            + ' style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:0;transition:opacity 1.2s ease"'
            + " onload=\"this.style.opacity='1';var lw=this.previousElementSibling;"
            + "if(lw&&lw.classList.contains('alb-hero-lqip'))lw.style.opacity='0';"
            + 'if(window._recacheGridTop)_recacheGridTop()"'
            + " onerror=\"this.style.display='none'\">"
            + "</div>"
        )

    plot_html = ""
    if plot:
        # Escape for HTML attribute use
        plot_esc = (
            plot.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
        )
        plot_html = (
            '<div class="alb-hero-plot-wrap">'
            f'<p class="alb-hero-plot" id="albPlotText">{plot_esc}</p>'
            '<button class="alb-plot-toggle" id="albPlotToggle" onclick="(function(){'
            "var p=document.getElementById('albPlotText');"
            "var b=document.getElementById('albPlotToggle');"
            "var exp=p.classList.toggle('expanded');"
            "b.textContent=exp?'Show less':'Read more';"
            "if(window._recacheGridTop)_recacheGridTop();"
            '})()">Read more</button>'
            "</div>"
        )
    year_html = f'<span class="album-year">{year_disp}</span>' if year_disp else ""
    meta_row = (
        f'<div class="album-meta-row" style="gap:6px;margin-top:6px">{year_html}{badges}</div>'
        if (year_disp or badges)
        else ""
    )

    # Build blurred background layer from poster
    bg_style = ""
    if src:
        bg_style = f"background-image:url({json.dumps(src)})"
    bg_html = f'<div class="alb-hero-bg" style="{bg_style}"></div>' if src else ""

    return (
        "<style>"
        ".alb-hero{display:flex;gap:20px;align-items:flex-start;padding:20px 0 16px;position:relative;overflow:hidden}"
        ".alb-hero-bg{position:absolute;inset:-40px -max(16px,3vw);z-index:0;"
        "background-size:cover;background-position:center top;"
        "filter:blur(48px) brightness(.35) saturate(1.4);transform:scale(1.05);"
        "pointer-events:none;will-change:transform;}"
        # aspect-ratio reserves space before image loads → no layout shift → correct _gridTop
        ".alb-hero-poster{flex-shrink:0;width:110px;aspect-ratio:2/3;border-radius:10px;"
        "overflow:hidden;position:relative;background:var(--surface);"
        "box-shadow:0 8px 32px rgba(0,0,0,.6);border:1.5px solid var(--border);z-index:1}"
        ".alb-hero-lqip{position:absolute;inset:-6%;background-size:cover;"
        "background-position:center;image-rendering:pixelated;filter:blur(8px);"
        "transition:opacity 1.2s ease;pointer-events:none}"
        ".alb-hero-info{flex:1;min-width:0;position:relative;z-index:1}"
        ".alb-hero-info h1{font-size:clamp(1.3rem,4vw,2.4rem);font-weight:900;"
        "letter-spacing:-.03em;line-height:1.1}"
        ".alb-hero-plot-wrap{margin-top:8px;max-width:560px}"
        ".alb-hero-plot{font-size:.8rem;color:var(--text2);line-height:1.55;"
        "display:-webkit-box;-webkit-line-clamp:2;"
        "-webkit-box-orient:vertical;line-clamp:2;overflow:hidden}"
        ".alb-hero-plot.expanded{-webkit-line-clamp:unset;line-clamp:unset;overflow:visible;display:block}"
        ".alb-plot-toggle{background:none;border:none;color:var(--accent);font-size:.75rem;"
        "font-weight:700;cursor:pointer;padding:4px 0 0;display:block;letter-spacing:.02em}"
        ".alb-plot-toggle:hover{opacity:.75}"
        ".alb-hero-sub{font-size:.78rem;color:var(--text2);margin-top:8px;font-weight:500}"
        "@media(max-width:500px){.alb-hero-poster{width:80px}.alb-hero{gap:14px}}"
        "</style>"
        '<div class="alb-hero">' + bg_html + poster_html + '<div class="alb-hero-info">'
        f"<h1>{album}</h1>"
        + meta_row
        + plot_html
        + f'<div class="alb-hero-sub">{count_s}</div>'
        "</div>"
        "</div>"
    )


def _render_album(album, videos, meta=None, poster_sm_url="", lqip=""):
    empty = '<div class="empty"><div class="empty-icon"><svg viewBox="0 0 24 24" stroke-width="1.5"><path stroke-linecap="round" d="M3 7h18M3 12h18M3 17h18"/></svg></div><h3>No videos in this album</h3></div>'
    encoded_album = _up.quote(album, safe="")
    hero_html = _album_hero(
        album, videos, meta or {}, poster_sm_url=poster_sm_url, lqip=lqip
    )

    return (
        _head(f"{album} \u2013 StreamVault")
        + f"""<script>
window._VDATA = [];
function openVLC(id,btn){{
  if(btn){{btn.disabled=true;btn.dataset.origHtml=btn.innerHTML;btn.innerHTML='<svg class="sv-spin-ico" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>';}}
  fetch('/api/history/record',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message_id:id}})}}).catch(function(){{}});
  fetch('/api/launch_vlc',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{msg_id:id}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(!d.ok){{
        _svShowVlcPill('\u26a0 Could not launch VLC: '+(d.error||'unknown'),false);
      }}else{{
        _svShowVlcPill('\u25b6 Opened in VLC',true);
      }}
      if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origHtml;}}
    }})
    .catch(function(){{_svShowVlcPill('\u26a0 VLC launch failed',false);if(btn){{btn.disabled=false;btn.innerHTML=btn.dataset.origHtml;}}}});
}}
function _svShowVlcPill(msg,ok){{
  var p=document.getElementById('_svVlcPill');
  if(!p){{p=document.createElement('div');p.id='_svVlcPill';p.style.cssText='position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(10,10,10,.96);border:1.5px solid #3a3;border-radius:50px;padding:10px 22px;font-size:.82rem;font-weight:600;color:#7f7;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;white-space:nowrap;z-index:99999;';document.body.appendChild(p);}}
  p.textContent=msg;p.style.borderColor=ok?'#3a3':'#f44';p.style.color=ok?'#7f7':'#f77';
  p.style.opacity='1';p.style.transform='translateX(-50%) translateY(0)';
  clearTimeout(p._t);p._t=setTimeout(function(){{p.style.opacity='0';p.style.transform='translateX(-50%) translateY(20px)';}},3000);
}}
function copyUrl(url,btn){{
  navigator.clipboard.writeText(window.location.origin+url).then(function(){{
    var orig=btn.innerHTML;btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" width="13" height="13"><polyline points="20 6 9 17 4 12"/></svg>';
    setTimeout(function(){{btn.innerHTML=orig;}},1500);
  }}).catch(function(){{}});
}}
function openSubChooser(id, btn, customQuery, customLang){{
  var currentQuery = customQuery !== undefined ? customQuery : '';
  var currentLang = customLang || 'EN';

  if(btn && !customQuery && customLang === undefined){{
    btn.dataset.origHtml=btn.innerHTML;
    btn.innerHTML='<svg class="sv-spin-ico" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>';
  }}

  openModal('<div style="text-align:center;padding:28px 0;"><svg class="sv-spin-ico" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg><div style="margin-top:14px;font-size:.85rem;font-weight:600;color:#ccc">Scanning channel & online subtitles...</div></div>');

  fetch('/api/subtitles/list',{{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{msg_id:id, search_query: currentQuery, lang: currentLang}})
  }})
  .then(function(r){{return r.json();}})
  .then(function(d){{
    if(btn){{btn.innerHTML=btn.dataset.origHtml;}}
    
    var html = '<h3 style="font-size:1.05rem;font-weight:700;color:#e8e8e8;margin-bottom:4px">Select Subtitle</h3>';
    html += '<div style="font-size:.76rem;color:#888;margin-bottom:12px;word-break:break-word">'+(d.title||d.filename||'')+'</div>';
    
    // Custom Search Control Bar
    html += '<div style="display:flex;gap:8px;margin-bottom:14px;align-items:center;">';
    html += '  <input type="text" id="sub_search_input" value="' + (currentQuery ? currentQuery.replace(/"/g, '&quot;') : '') + '" placeholder="Custom search title (e.g. Inception 2010)" style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:6px 10px;color:#fff;font-size:.8rem;outline:none;" />';
    html += '  <button id="sub_search_btn" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:6px 14px;font-size:.8rem;font-weight:600;cursor:pointer;">Search</button>';
    html += '</div>';

    if(!d.ok||!d.options||d.options.length===0){{
      html += '<div style="text-align:center;padding:20px 0;"><p style="font-size:.8rem;color:#888;margin-bottom:16px;">No matching subtitle files were found.</p></div>';
      html += '<div style="text-align:right"><button class="sub-modal-select-btn" onclick="closeModal()">Close</button></div>';
      openModal(html);
    }} else {{
      html += '<div class="sub-modal-list">';
      d.options.forEach(function(opt, idx){{
        var isTg = opt.source === 'telegram';
        var isSubdl = opt.source === 'subdl';
        var isSubSource = opt.source === 'subsource';
        var tagClass = isTg ? 'tg' : (isSubdl ? 'subdl' : 'subsource');
        var tagLabel = isTg ? 'Telegram Channel' : (isSubdl ? 'Subdl (' + (opt.language || 'EN') + (opt.hi ? ' SDH' : '') + ')' : 'SubSource (' + (opt.language || 'EN') + (opt.hi ? ' SDH' : '') + ')');
        var scoreLabel = opt.score > 0 ? (Math.round(Math.min(100, opt.score)) + '% Match') : '';
        var subIdStr = (opt.id !== null && opt.id !== undefined) ? opt.id : 'null';
        var optUrl = opt.url || '';
        var isActive = opt.is_active || false;
        var itemId = 'sub_opt_' + (opt.id || opt.source || 'opt') + '_' + idx;
        var itemClass = 'sub-modal-item' + (isActive ? ' active' : '');
        html += '<div class="' + itemClass + '" id="' + itemId + '">';
        html += '  <div style="flex:1;min-width:0">';
        html += '    <div class="sub-modal-item-title">' + (isActive ? '<span style="color:#22c55e;margin-right:6px;font-weight:700;">&#10003;</span>' : '') + opt.filename + '</div>';
        html += '    <div class="sub-modal-item-meta">';
        html += '      <span class="sub-modal-tag ' + tagClass + '">' + tagLabel + '</span>';
        var epBadge = (opt.season != null && opt.episode != null) ? ('<span class="sub-modal-tag" style="background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.3);font-weight:600;">S' + (opt.season<10?'0':'') + opt.season + 'E' + (opt.episode<10?'0':'') + opt.episode + '</span>') : (opt.season != null ? ('<span class="sub-modal-tag" style="background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.3);">Season '+opt.season+'</span>') : '');
        if(epBadge) html += epBadge;
        if(isActive) html += '      <span class="sub-modal-tag active-tag" style="background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.3);">ACTIVE</span>';
        if(scoreLabel && isTg) html += '      <span class="sub-modal-tag match">' + scoreLabel + '</span>';
        if(opt.ext) html += '      <span style="text-transform:uppercase;color:#555">.' + opt.ext + '</span>';
        html += '    </div>';
        html += '  </div>';
        if(isActive) {{
          html += '  <button class="sub-modal-select-btn active" disabled>Active</button>';
        }} else {{
          html += '  <button class="sub-modal-select-btn sub-apply-btn"'
                + ' data-mid="' + id + '"'
                + ' data-sid="' + subIdStr + '"'
                + ' data-fn="' + encodeURIComponent(opt.filename) + '"'
                + ' data-src="' + encodeURIComponent(opt.source) + '"'
                + ' data-url="' + encodeURIComponent(optUrl) + '"'
                + '>Use This Subtitle</button>';
        }}
        html += '</div>';
      }});
      html += '</div>';
      openModal(html);
    }}

    // Bind search button and input enter key
    setTimeout(function(){{
      var sBtn = document.getElementById('sub_search_btn');
      var sInp = document.getElementById('sub_search_input');
      function doSearch(){{
        var q = sInp ? sInp.value : '';
        openSubChooser(id, null, q, 'EN');
      }}
      if(sBtn){{ sBtn.addEventListener('click', doSearch); }}
      if(sInp){{
        sInp.addEventListener('keydown', function(e){{
          if(e.key === 'Enter') doSearch();
        }});
      }}

      document.querySelectorAll('.sub-apply-btn').forEach(function(b){{
        b.addEventListener('click', function(){{
          var mid = parseInt(this.dataset.mid);
          var sid = this.dataset.sid === 'null' ? null : parseInt(this.dataset.sid);
          var fn  = decodeURIComponent(this.dataset.fn);
          var src = decodeURIComponent(this.dataset.src);
          var url = decodeURIComponent(this.dataset.url || '');
          _applySub(mid, sid, fn, src, url, this);
        }});
      }});
    }}, 0);
  }})
  .catch(function(){{
    if(btn){{btn.innerHTML=btn.dataset.origHtml;}}
    openModal('<h3 style="font-size:1rem;color:#f66;margin-bottom:8px">Error</h3><p style="font-size:.8rem;color:#aaa;margin-bottom:16px">Failed to load subtitles list.</p><div style="text-align:right"><button class="sub-modal-select-btn" onclick="closeModal()">Close</button></div>');
  }});
}}

function _applySub(msgId, subMsgId, filename, source, url, btn){{
  if(btn){{btn.disabled=true;btn.textContent='Applying...';}}
  fetch('/api/subtitles/apply',{{
    method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{
      msg_id: msgId,
      sub_msg_id: subMsgId,
      filename: filename,
      source: source,
      url: url
    }})
  }})
  .then(function(r){{return r.json();}})
  .then(function(d){{
    if(d.ok){{
      _svShowVlcPill('\u2713 Subtitle applied successfully!',true);
      closeModal();
      if(window._VDATA&&Array.isArray(window._VDATA)){{
        var item=window._VDATA.find(function(x){{return x.id==msgId;}});
        if(item) item.has_sub=true;
      }}
      var card=document.querySelector('[data-mid="'+msgId+'"]');
      if(card){{
        var leftRow=card.querySelector('.badge-row-left');
        if(leftRow&&!leftRow.querySelector('.cc-badge')){{
          var cc=document.createElement('span');
          cc.className='ep-badge cc-badge';
          cc.textContent='CC';
          cc.title='Change Subtitle';
          cc.onclick=function(ev){{ev.stopPropagation();openSubChooser(msgId);}};
          leftRow.appendChild(cc);
        }}
      }}
    }}else{{
      _svShowVlcPill('\u26a0 '+(d.error||'Failed to apply subtitle'),false);
      if(btn){{btn.disabled=false;btn.textContent='Use This Subtitle';}}
    }}
  }})
  .catch(function(){{
    _svShowVlcPill('\u26a0 Network error applying subtitle',false);
    if(btn){{btn.disabled=false;btn.textContent='Use This Subtitle';}}
  }});
}}

function fetchSub(id,btn){{
  openSubChooser(id, btn);
}}

// ── Download Manager ──────────────────────────────────────────────────────────
var _svDlPanel = null, _svDlInterval = null, _svDlTracked = {{}};

function _svDlFmt(bytes){{
  if(!bytes) return '0 B';
  var k=1024,m=k*k,g=m*k;
  if(bytes>=g) return (bytes/g).toFixed(2)+' GB';
  if(bytes>=m) return (bytes/m).toFixed(1)+' MB';
  if(bytes>=k) return (bytes/k).toFixed(0)+' KB';
  return bytes+' B';
}}

function _svDlEnsurePanel(){{
  if(_svDlPanel) return;
  var panel = document.createElement('div');
  panel.id = 'sv-dl-panel';
  panel.style.cssText = 'position:fixed;bottom:76px;right:20px;width:340px;max-height:420px;overflow-y:auto;background:var(--card,#1a1a2e);border:1.5px solid var(--border,rgba(255,255,255,.08));border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.6);z-index:9999;padding:14px 16px 12px;font-size:.82rem;color:var(--text,#e0e0e0);display:none;backdrop-filter:blur(10px);';
  panel.innerHTML = '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.06);">'
    +'<span style="font-weight:700;font-size:.88rem;letter-spacing:.02em;">Downloads</span>'
    +'<div style="display:flex;gap:6px;">'
    +'<button onclick="_svDlClosePanel()" style="background:transparent;border:none;color:var(--text2,#999);font-size:1.2rem;cursor:pointer;padding:0 4px;line-height:1;" title="Close">&times;</button>'
    +'</div></div>'
    +'<div id="sv-dl-list"></div>';
  document.body.appendChild(panel);
  _svDlPanel = panel;
}}

function _svDlOpenPanel(){{
  _svDlEnsurePanel();
  _svDlPanel.style.display='block';
  _svDlUpdateFabState(true);
}}

function _svDlClosePanel(){{
  if(_svDlPanel) _svDlPanel.style.display='none';
  _svDlUpdateFabState(false);
}}

function _svDlTogglePanel(){{
  _svDlEnsurePanel();
  var isOpen = (_svDlPanel.style.display !== 'none');
  if(isOpen){{
    _svDlClosePanel();
  }} else {{
    _svDlOpenPanel();
  }}
}}

function _svDlUpdateFabState(isOpen){{
  var fab = document.getElementById('sv-dl-fab');
  if(!fab) return;
  var icon = fab.querySelector('.sv-fab-icon');
  if(!icon) return;
  if(isOpen){{
    icon.style.transform = 'rotate(45deg)';
    fab.setAttribute('title', 'Close Downloads');
  }} else {{
    icon.style.transform = 'rotate(0deg)';
    fab.setAttribute('title', 'Open Downloads');
  }}
}}

function _svDlEnsureFab(){{
  if(document.getElementById('sv-dl-fab')) return;
  var fab = document.createElement('button');
  fab.id = 'sv-dl-fab';
  fab.title = 'Downloads';
  fab.onclick = _svDlTogglePanel;
  fab.style.cssText = 'display:none;position:fixed;bottom:20px;right:20px;width:48px;height:48px;border-radius:50%;background:var(--accent,#f5c518);border:none;color:#000;cursor:pointer;z-index:9998;align-items:center;justify-content:center;box-shadow:0 6px 20px rgba(0,0,0,.4);transition:transform .2s ease,box-shadow .2s ease;';
  fab.onmouseenter = function(){{ fab.style.transform = 'scale(1.06)'; }};
  fab.onmouseleave = function(){{ fab.style.transform = 'scale(1)'; }};
  fab.innerHTML = '<svg class="sv-fab-icon" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" style="transition:transform .25s cubic-bezier(.4,0,.2,1);"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>';
  document.body.appendChild(fab);
}}

function _svDlRenderItem(info){{
  var id = info.msg_id;
  var title = (info.title||'Video '+id);
  if(title.length>38) title=title.slice(0,35)+'...';
  var pct = Math.min(100,Math.round(info.percent||0));
  var statusTxt, barColor, statusKey;
  if(info.status==='error'){{
    statusTxt='\u26a0 Failed'; barColor='#ef4444'; statusKey='error';
  }} else {{
    statusTxt=pct+'% \u2013 '+_svDlFmt(info.downloaded||0)+' / '+_svDlFmt(info.total||0);
    barColor='var(--accent,#f5c518)'; statusKey='downloading';
  }}
  var html='<div data-dl-id="'+id+'" data-dl-status="'+statusKey+'" id="sv-dl-item-'+id+'" style="margin-bottom:12px;">'
    +'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px;">'
    +'<span style="font-size:.79rem;font-weight:500;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="'+(info.title||'')+'">'+title+'</span>'
    +'<span style="font-size:.73rem;color:var(--text2,#999);margin-left:8px;white-space:nowrap;">'+statusTxt+'</span>'
    +'</div>'
    +'<div style="height:5px;background:rgba(255,255,255,.08);border-radius:3px;overflow:hidden;">'
    +'<div style="height:100%;width:'+pct+'%;background:'+barColor+';border-radius:3px;transition:width .3s ease;"></div>'
    +'</div></div>';
  var existing = document.getElementById('sv-dl-item-'+id);
  if(existing) existing.outerHTML=html;
  else{{
    var list=document.getElementById('sv-dl-list');
    if(list) list.insertAdjacentHTML('afterbegin',html);
  }}
}}

function _svDlRemoveItem(id){{
  var el = document.getElementById('sv-dl-item-' + id);
  if(el){{
    el.style.transition = 'opacity .3s, transform .3s';
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(function(){{ if(el) el.remove(); }}, 300);
  }}
}}

function _svDlStartPolling(){{
  if(_svDlInterval) return;
  _svDlEnsureFab();
  _svDlInterval = setInterval(function(){{
    fetch('/api/download/all')
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if(!d.ok||!d.downloads) return;
        var active=0;
        var hasCompleted=false;
        d.downloads.forEach(function(info){{
          var isDone = info.is_downloaded || info.status === 'completed';
          if(isDone){{
            hasCompleted = true;
            _svDlRemoveItem(info.msg_id);
            if(!_svDlTracked['done_'+info.msg_id]){{
              _svDlTracked['done_'+info.msg_id]=true;
              _setVideoDownloaded(info.msg_id, true);
              _svShowVlcPill('\u2713 Download complete: ' + (info.title||'Video'), true);
            }}
          }} else if(info.status === 'downloading'){{
            active++;
            _svDlTracked[info.msg_id]=true;
            _svDlRenderItem(info);
          }} else if(info.status === 'error'){{
            _svDlRenderItem(info);
          }}
        }});

        if(hasCompleted){{
          fetch('/api/download/clear_completed', {{method:'POST'}}).catch(function(){{}});
        }}

        var fab = document.getElementById('sv-dl-fab');
        if(fab){{
          fab.style.display = (active > 0) ? 'flex' : 'none';
        }}

        if(active === 0){{
          clearInterval(_svDlInterval);
          _svDlInterval = null;
          // Auto close panel when all active downloads finish
          setTimeout(function(){{
            if(!active) _svDlClosePanel();
          }}, 800);
        }}
      }})
      .catch(function(){{}});
  }}, 1000);
}}

function svStartDownload(id, btn){{
  if(btn){{
    btn.disabled=true;
    btn.dataset.origHtml=btn.innerHTML;
    btn.innerHTML='<svg class="sv-spin-ico" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>';
    btn.title='Starting\u2026';
  }}
  fetch('/api/download/start',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{msg_id:id}})}})
  .then(function(r){{ return r.json(); }})
  .then(function(d){{
    if(!d.ok){{
      _svShowVlcPill('\u26a0 '+(d.error||'Failed to start download'),false);
      if(btn){{ btn.disabled=false; btn.innerHTML=btn.dataset.origHtml; btn.title='Download Video'; }}
      return;
    }}
    _svDlEnsurePanel();
    _svDlOpenPanel();
    _svDlStartPolling();
  }})
  .catch(function(){{
    _svShowVlcPill('\u26a0 Network error',false);
    if(btn){{ btn.disabled=false; btn.innerHTML=btn.dataset.origHtml; btn.title='Download Video'; }}
  }});
}}

function _setVideoDownloaded(id, isDl){{
  if(window._VDATA && Array.isArray(window._VDATA)){{
    var it = window._VDATA.find(function(x){{ return x.id == id; }});
    if(it) it.is_downloaded = isDl;
  }}
  if(window._videoById && window._videoById[id]){{
    window._videoById[id].is_downloaded = isDl;
  }}
  if(window._rerenderCard){{
    window._rerenderCard(id);
  }} else {{
    if(window._clearCardHtmlCache) window._clearCardHtmlCache(id);
    _updateCardDlButtons(id, isDl);
  }}
}}

function _updateCardDlButtons(id, isDl){{
  var card = document.querySelector('[data-mid="' + id + '"]');
  if(!card) return;
  var row = card.querySelector('.play-btn-row');
  if(!row) return;

  var playBtn = row.querySelector('.play-btn');
  if(playBtn){{
    playBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="11" height="11"><circle cx="12" cy="12" r="10"/><polygon points="10,8 16,12 10,16" fill="currentColor" stroke="none"/></svg>' + (isDl ? 'Play' : 'Stream');
  }}

  var copyBtn = row.querySelector('.copy-btn');
  if(copyBtn){{
    copyBtn.style.display = isDl ? 'none' : '';
  }}

  var oldDl = row.querySelector('.dl-btn');
  var oldDel = row.querySelector('.del-btn');
  if(oldDl) oldDl.remove();
  if(oldDel) oldDel.remove();

  if(isDl){{
    var dlBtn = document.createElement('button');
    dlBtn.className = 'play-btn-copy dl-btn downloaded';
    dlBtn.id = 'dl-btn-' + id;
    dlBtn.title = 'Downloaded (Offline ready)';
    dlBtn.style.cssText = 'border-color:#22c55e;color:#22c55e;cursor:default;';
    dlBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><polyline points="20 6 9 17 4 12"/></svg>';
    dlBtn.onclick = function(e){{ e.stopPropagation(); }};

    var delBtn = document.createElement('button');
    delBtn.className = 'play-btn-copy del-btn';
    delBtn.id = 'del-btn-' + id;
    delBtn.title = 'Delete downloaded video (free storage)';
    delBtn.style.cssText = 'border-color:rgba(239,68,68,.4);color:#ef4444;';
    delBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>';
    delBtn.onclick = function(e){{ e.stopPropagation(); svDeleteDownload(id, delBtn); }};

    row.appendChild(dlBtn);
    row.appendChild(delBtn);
  }} else {{
    var newDl = document.createElement('button');
    newDl.className = 'play-btn-copy dl-btn';
    newDl.id = 'dl-btn-' + id;
    newDl.title = 'Download Video';
    newDl.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="13" height="13"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
    newDl.onclick = function(e){{ e.stopPropagation(); svStartDownload(id, newDl); }};
    row.appendChild(newDl);
  }}
}}

function svDeleteDownload(id, btn){{
  _svAlert({{
    title: 'Delete Downloaded Video',
    body: 'Permanently remove the local offline video file to free up storage space? (You can re-download anytime)',
    confirmText: 'Delete',
    danger: true,
    onConfirm: function(){{
      if(btn){{ btn.disabled = true; }}
      fetch('/api/download/delete', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{msg_id: id}})
      }})
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if(d.ok){{
          _setVideoDownloaded(id, false);
          _svShowVlcPill('Deleted offline file. Storage freed.', true);
        }} else {{
          if(btn) btn.disabled = false;
          _svShowVlcPill('\u26a0 ' + (d.error || 'Failed to delete file'), false);
        }}
      }})
      .catch(function(){{
        if(btn) btn.disabled = false;
        _svShowVlcPill('\u26a0 Network error deleting file', false);
      }});
    }}
  }});
}}
(function(){{
  fetch('/api/album_data/{encoded_album}')
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      window._VDATA = d;
      if(window._initVirtualGrid) window._initVirtualGrid(d);
    }})
    .catch(function(e){{ console.error('album data load failed', e); }});
}})();
</script>"""
        + _nav("Search videos\u2026")
        + f"""
<style>
.alb-sort-lbl{{font-size:.72rem;color:var(--text2);font-weight:600;}}
.alb-sort-btn{{height:28px;padding:0 13px;background:transparent;border:1.5px solid var(--border);border-radius:50px;color:var(--text2);font-size:.72rem;font-weight:600;cursor:pointer;transition:border-color .15s,color .15s,background .15s;}}
.alb-sort-btn:hover{{border-color:var(--accent);color:var(--accent);}}
.alb-sort-btn.active{{border-color:var(--accent);color:var(--accent);background:var(--accent-lo);}}
.alb-crumb-row{{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;padding:12px 0 10px;border-bottom:1.5px solid var(--border);}}
.alb-crumb-row .breadcrumb{{margin:0;padding:0;border:none;}}
.alb-sort-group{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}}
@media(max-width:480px){{.alb-crumb-row{{flex-direction:column;align-items:flex-start;}}}}
</style>
<div class="page">
  <div class="alb-crumb-row">
    <a class="breadcrumb" href="/"><svg width="16" height="16" viewBox="0 0 24 24" stroke-width="2" stroke-linecap="round"><path d="M19 12H5M12 5l-7 7 7 7"/></svg>All Albums</a>
    <div class="alb-sort-group">
      <span class="alb-sort-lbl">Sort:</span>
      <button class="alb-sort-btn" id="albSort_date"  onclick="_albSetSort('date')">Date Added</button>
      <button class="alb-sort-btn" id="albSort_name"  onclick="_albSetSort('name')">Name</button>
      <button class="alb-sort-btn" id="albSort_size"  onclick="_albSetSort('size')">Size</button>
      <button class="alb-sort-btn" id="albDeleteAlbum" style="margin-left:8px;border-color:#c66;color:#c66;" onclick="_deleteAlbum(this.dataset.album)" data-album="{album.replace(chr(34), '&quot;')}">&#x1f5d1; Delete Album</button>
    </div>
  </div>
  
  {hero_html}
  <div class="season-tabs" id="seasonTabBar"></div>
  <div style="height:20px"></div>
  {"" if videos else empty}
  <div class="videos-grid" id="videosGrid" style="position:relative;min-height:100px;">
    <div id="vSentinel" style="position:absolute;top:0;left:0;width:1px;pointer-events:none;"></div>
  </div>
</div>
<script>
/* ── Custom alert card ────────────────────────────────────────────────────── */
(function(){{
  var _overlay=null;
  window._svAlert=function(opts){{
    /* opts: {{title, body, confirmText, cancelText, danger, onConfirm}} */
    if(_overlay){{_overlay.remove();_overlay=null;}}
    _overlay=document.createElement('div');
    _overlay.style.cssText='position:fixed;inset:0;z-index:9900;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px);';
    var card=document.createElement('div');
    card.style.cssText='background:#0d0d0d;border:1.5px solid #252525;border-radius:16px;padding:28px 28px 22px;max-width:380px;width:100%;box-shadow:0 24px 64px rgba(0,0,0,.8);';
    var titleEl=document.createElement('div');
    titleEl.style.cssText='font-size:1rem;font-weight:800;color:#e8e8e8;margin-bottom:10px;';
    titleEl.textContent=opts.title||'Confirm';
    var bodyEl=document.createElement('div');
    bodyEl.style.cssText='font-size:.82rem;color:#606060;line-height:1.6;margin-bottom:22px;';
    bodyEl.textContent=opts.body||'';
    var row=document.createElement('div');
    row.style.cssText='display:flex;gap:10px;justify-content:flex-end;';
    if(opts.cancelText!==false){{
      var cancelBtn=document.createElement('button');
      cancelBtn.textContent=opts.cancelText||'Cancel';
      cancelBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:1.5px solid #1c1c1c;background:transparent;color:#606060;font-size:.8rem;font-weight:600;cursor:pointer;';
      cancelBtn.onclick=function(){{_overlay.remove();_overlay=null;}};
      row.appendChild(cancelBtn);
    }}
    var confirmBtn=document.createElement('button');
    confirmBtn.textContent=opts.confirmText||'OK';
    confirmBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:none;background:'+(opts.danger?'#c0392b':'#f5c518')+';color:'+(opts.danger?'#fff':'#000')+';font-size:.8rem;font-weight:700;cursor:pointer;';
    confirmBtn.onclick=function(){{
      _overlay.remove();_overlay=null;
      if(opts.onConfirm)opts.onConfirm();
    }};
    row.appendChild(confirmBtn);
    card.appendChild(titleEl);card.appendChild(bodyEl);card.appendChild(row);
    _overlay.appendChild(card);
    document.body.appendChild(_overlay);
    _overlay.addEventListener('click',function(e){{if(e.target===_overlay){{_overlay.remove();_overlay=null;}}}});
    confirmBtn.focus();
  }};
  window._svNotify=function(msg,isErr){{
    var t=document.createElement('div');
    t.style.cssText='position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(10,10,10,.96);border:1.5px solid '+(isErr?'#f44':'#f5c518')+';border-radius:50px;padding:12px 22px;font-size:.84rem;font-weight:600;color:'+(isErr?'#f77':'#f5c518')+';opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;white-space:nowrap;z-index:9999;';
    t.textContent=msg;
    document.body.appendChild(t);
    requestAnimationFrame(function(){{t.style.opacity='1';t.style.transform='translateX(-50%) translateY(0)';}});
    setTimeout(function(){{t.style.opacity='0';setTimeout(function(){{t.remove();}},300);}},3200);
  }};
}})();

function _deleteAlbum(name){{
  _svAlert({{
    title: 'Delete Album',
    body: 'Permanently remove ALL videos in \u201c'+name+'\u201d from the local cache? (Does NOT delete from Telegram \u2014 hit Fetch to restore.',
    confirmText: 'Delete',
    danger: true,
    onConfirm: function(){{
      var btn=document.getElementById('albDeleteAlbum');
      if(btn){{btn.disabled=true;btn.textContent='\u23f3 Deleting\u2026';}}
      fetch('/api/delete_album',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{album:name}})}})
        .then(function(r){{return r.json();}})
        .then(function(j){{
          if(j.ok){{window.location.href='/';}}
          else{{
            if(btn){{btn.disabled=false;btn.textContent='\ud83d\uddd1 Delete Album';}}
            _svNotify('Error: '+(j.error||'unknown'),true);
          }}
        }})
        .catch(function(){{
          if(btn){{btn.disabled=false;btn.textContent='\ud83d\uddd1 Delete Album';}}
          _svNotify('Network error',true);
        }});
    }}
  }});
}}
</script>
{_MODAL_HTML}
{_get_virtual_album_js_tag()}"""
        + _PAGE_CLOSE
        + """</body></html>"""
    )

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
_VIRTUAL_ALBUM_JS = _read_static("virtual_album.js")

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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&display=swap" media="print" onload="this.media='all'">
<noscript><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700;800;900&display=swap"></noscript>
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
  <div class="history-card-list" id="historyList">
    <div class="history-loading">Loading...</div>
  </div>
</div>
<style>.history-card{{position:fixed;top:54px;right:max(16px,3vw);width:560px;max-height:600px;background:#0d0d0d;border:1.5px solid #252525;border-radius:14px;box-shadow:0 16px 48px rgba(0,0,0,.8);z-index:1001;display:flex;flex-direction:column;overflow:hidden;}}.history-card-header{{display:flex;align-items:center;justify-content:space-between;padding:14px 18px 10px;border-bottom:1px solid #1c1c1c;}}.history-card-header h3{{font-size:.84rem;font-weight:800;color:#e8e8e8;letter-spacing:.02em;}}.history-close-btn{{background:none;border:none;color:#606060;font-size:1.2rem;cursor:pointer;transition:color .2s;}}.history-close-btn:hover{{color:#e8e8e8;}}.history-card-list{{flex:1;overflow-y:auto;padding:6px 0;}}.history-loading{{padding:24px;text-align:center;color:#606060;font-size:.78rem;}}.history-item{{display:flex;align-items:center;gap:14px;padding:10px 18px;transition:background .15s;cursor:default;}}.history-item:hover{{background:rgba(245,197,24,.06);}}.history-item-thumb{{width:72px;height:54px;border-radius:6px;background:var(--surface);flex-shrink:0;overflow:hidden;}}.history-item-thumb img{{width:100%;height:100%;object-fit:cover;}}.history-item-info{{flex:1;min-width:0;}}.history-item-title{{font-size:.82rem;font-weight:700;color:#e8e8e8;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-clamp:2;overflow:hidden;}}.history-item-meta{{font-size:.68rem;color:#606060;margin-top:2px;display:flex;gap:6px;align-items:center;}}.history-item-actions{{display:flex;gap:4px;flex-shrink:0;}}.history-action-btn{{width:28px;height:28px;border-radius:6px;border:1px solid #1c1c1c;background:transparent;color:#606060;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:border-color .15s,color .15s;}}.history-action-btn:hover{{border-color:var(--accent);color:var(--accent);}}.history-action-btn svg{{width:12px;height:12px;}}.history-empty{{padding:32px 24px;text-align:center;color:#606060;font-size:.78rem;}}@media(max-width:640px){{.history-card{{width:calc(100vw - 32px);right:16px;}}}}</style>
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
}})();
var _historyLoaded=false;
window.toggleHistoryCard = function(e){{
  e.preventDefault();e.stopPropagation();
  var card=document.getElementById('historyCard');
  if(!card)return;
  if(card.style.display==='none'){{
    card.style.display='flex';
    if(!_historyLoaded){{loadHistory();_historyLoaded=true;}}
  }}else{{
    card.style.display='none';
  }}
}}
function loadHistory(){{
  var list=document.getElementById('historyList');
  if(!list)return;
  list.innerHTML='<div class="history-loading">Loading...</div>';
  fetch('/api/history').then(function(r){{return r.json();}}).then(function(d){{
    if(!d.ok||!d.history||!d.history.length){{list.innerHTML='<div class="history-empty">No stream history yet</div>';return;}}
    var html='';
    d.history.forEach(function(h){{
      var thumb=h.thumb_url?'<img src="'+h.thumb_url+'" alt="">':'';
      var dur=h.duration?'<span>'+h.duration+'</span>':'';
      var qual=h.quality?'<span>'+h.quality+'</span>':'';
      var alb=h.album?'<a href="/album/'+encodeURIComponent(h.album)+'" style="color:var(--accent);text-decoration:none;">'+h.album+'</a>':'';
      html+='<div class="history-item">'
        +'<div class="history-item-thumb">'+thumb+'</div>'
        +'<div class="history-item-info">'
        +'<div class="history-item-title">'+(h.title||'Untitled')+'</div>'
        +'<div class="history-item-meta">'+dur+qual+alb+'</div>'
        +'</div>'
        +'<div class="history-item-actions">'
        +'<button class="history-action-btn" data-tip="Resume in VLC" data-action="vlc" data-mid="'+h.message_id+'" data-alb="'+encodeURIComponent(h.album||'')+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polygon points="5 3 19 12 5 21 5 3" fill="currentColor" stroke="none"/></svg></button>'
        +'<button class="history-action-btn" data-tip="Remove" data-action="remove" data-mid="'+h.message_id+'"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>'
        +'</div></div>';
    }});
    list.innerHTML=html;
  }}).catch(function(){{list.innerHTML='<div class="history-empty">Failed to load history</div>';}});
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
      <button class="alb-filter-btn"        id="fltAll"    onclick="_setFilter('all')">All</button>
      <button class="alb-filter-btn active" id="fltSeries" onclick="_setFilter('series')">
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
  var _activeFilter = (function(){{try{{return sessionStorage.getItem('sv_home_filter')||'series';}}catch(e){{return 'series';}}}})();
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
  if(btn){{btn.disabled=true;btn.dataset.origHtml=btn.innerHTML;btn.innerHTML='<svg class="sv-spin-ico" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" style="animation:sv-spin .7s linear infinite"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>&nbsp;Preparing\u2026';}}
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
{_MODAL_HTML}<script>
{_VIRTUAL_ALBUM_JS}
</script>"""
        + _PAGE_CLOSE
        + """</body></html>"""
    )

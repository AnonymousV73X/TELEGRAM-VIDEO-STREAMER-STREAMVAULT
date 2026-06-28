"""routes.py — all HTTP route handlers for StreamVault."""

import re, os, asyncio, hashlib as _hashlib, shutil, subprocess, time as _time
from urllib.parse import unquote
from aiohttp import web

try:
    import orjson as _orjson

    def _fast_json_response(data):
        return web.Response(body=_orjson.dumps(data), content_type="application/json")

except ImportError:
    _fast_json_response = web.json_response

from config import (
    _here,
    _bundle_dir,
    PORT,
    POSTERS_DIR,
    _PASSWORD_HASH,
    SESSION_TIMEOUT,
    LOGIN_REQUIRED,
    _safe_mime,
    VLC_HTTP_PORT as _VLC_HTTP_PORT,
    VLC_HTTP_PASS as _VLC_HTTP_PASS,
)
from helpers import (
    get_filename,
    get_mime,
    get_size,
    get_duration,
    _parse_season,
)
from cache import (
    _load_cache,
    _save_cache,
    _meta,
    get_videos,
    _load_album_overrides,
    _save_album_overrides,
    _albums_dirty,
    _rebuild_albums_index,
    _albums_index,
    _cache_mem,
    _cache_meta,
    _poster_mem,
    _fetch_all_meta,
    fts_album_ids,
    fts_album_data,
    add_deleted_album as _add_deleted_album,
    fts_album_names,
    fts_manage_videos,
    fts_manage_videos_page,
    fts_all_albums_distinct,
    fts_search_detailed,
    history_add,
    history_list,
    history_remove,
)
from render import (
    _render_index,
    _render_album,
    BASE_CSS,
    _nav,
    _PAGE_CLOSE,
    _cache_poster_sync,
    _make_lqip_b64,
    _make_poster_sm,
)
from streaming import (
    stream_handler,
    vlc_stream_handler,
    route_hls_start,
    route_hls_playlist,
    route_hls_segment,
    route_hls_stop,
    route_clear_stream_cache,
    _get_msg,
    _stream_cache,
    _prewarm_resume,
    _playhead_prefetcher,
    _stream_cache_prewarm_offset,
    _vlc_instant_prewarm,
    prepare_new_stream_session,
)

# ── AUTH ──────────────────────────────────────────────────────────────────────
_AUTH_COOKIE = "sv_auth"
_PUBLIC_PATHS = {"/login", "/api/login", "/healthz"}
_PUBLIC_PREFIXES = ("/stream/", "/vlc/", "/poster/", "/static/")

# ── NOTIFICATION QUEUE ────────────────────────────────────────────────────────
_notifications: list = []

# Watermark: count of uncategorized videos the user has already "seen".
# Badge shows max(0, current_count - _seen_watermark).
_seen_watermark: int = -1


def _push_notification(msg: str, kind: str = "warn") -> None:
    import time as _t

    _notifications.append({"type": kind, "msg": msg, "ts": int(_t.time())})
    if len(_notifications) > 100:
        _notifications.pop(0)


def _make_token():
    hour = int(_time.time() // SESSION_TIMEOUT)
    return _hashlib.sha256(f"{_PASSWORD_HASH}:{hour}".encode()).hexdigest()


def _valid_token(token: str) -> bool:
    if not token:
        return False
    hour = int(_time.time() // SESSION_TIMEOUT)
    for h in (hour, hour - 1):
        if token == _hashlib.sha256(f"{_PASSWORD_HASH}:{h}".encode()).hexdigest():
            return True
    return False


def _login_page(error=False):
    err = (
        '<p style="color:#e05;font-size:.82rem;margin-top:12px;">Wrong password.</p>'
        if error
        else ""
    )
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>StreamVault \u2014 Login</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap" media="print" onload="this.media='all'">
<noscript><link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&display=swap"></noscript>
<link rel="stylesheet" href="/static/style.css">
<style>
body{{display:flex;align-items:center;justify-content:center;padding:16px;min-height:100vh;}}
.box{{width:100%;max-width:340px;padding:36px 28px;background:#0d0d0d;border:1px solid #1c1c1c;border-radius:18px;}}
.logo{{display:flex;align-items:center;gap:10px;margin-bottom:28px;}}
.logo-icon{{width:32px;height:32px;border-radius:8px;background:#f5c518;display:flex;align-items:center;justify-content:center;flex-shrink:0;}}
.logo-icon svg{{fill:#000;width:14px;height:14px;}}
.logo span{{font-size:1.25rem;font-weight:800;}}
label{{display:block;font-size:.78rem;font-weight:600;color:#606060;margin-bottom:6px;letter-spacing:.04em;text-transform:uppercase;}}
input[type=password]{{width:100%;height:44px;padding:0 14px;background:#0a0a0a;border:1px solid #1c1c1c;border-radius:10px;color:#e8e8e8;font-size:.95rem;outline:none;transition:border-color .2s;-webkit-appearance:none;}}
input[type=password]:focus{{border-color:#f5c518;box-shadow:0 0 0 2px rgba(245,197,24,.1);}}
button{{margin-top:16px;width:100%;height:44px;background:#f5c518;color:#000;font-size:.9rem;font-weight:700;border:none;border-radius:10px;cursor:pointer;transition:opacity .2s;display:flex;align-items:center;justify-content:center;gap:10px;}}
button:hover{{opacity:.88;}}
button:disabled{{opacity:.6;cursor:not-allowed;}}
@keyframes _spin{{to{{transform:rotate(360deg);}}}}
.btn-ring{{width:16px;height:16px;border:1.5px solid rgba(0,0,0,.25);border-top-color:#000;border-radius:50%;animation:_spin .65s linear infinite;flex-shrink:0;display:none;}}
button.loading .btn-ring{{display:block;}}
button.loading .btn-txt{{opacity:.7;}}
</style></head><body>
<div class="box">
  <div class="logo">
    <div class="logo-icon"><svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg></div>
    <span><span style="color:#f5c518">Stream</span><span style="color:#e8e8e8">Vault</span></span>
  </div>
  <form id="loginForm" autocomplete="off">
    <label for="pw">Password</label>
    <input id="pw" name="password" type="password"
           autocomplete="new-password" autocorrect="off" autocapitalize="off"
           spellcheck="false" autofocus>
    <div id="loginErr">{err}</div>
    <button type="submit" id="loginBtn">
      <div class="btn-ring"></div>
      <span class="btn-txt">Unlock</span>
    </button>
  </form>
</div>
<script>
document.getElementById('loginForm').addEventListener('submit',function(e){{
  e.preventDefault();
  var btn=document.getElementById('loginBtn');
  var errEl=document.getElementById('loginErr');
  var pw=document.getElementById('pw').value;
  btn.disabled=true;btn.classList.add('loading');
  errEl.innerHTML='';
  var fd=new FormData();fd.append('password',pw);
  fetch('/api/login',{{method:'POST',body:fd,redirect:'manual'}})
    .then(function(r){{
      if(r.type==='opaqueredirect'||r.status===0||r.status===302||r.status===200&&r.redirected){{
        window.location.href='/';
      }} else if(r.status===200){{
        return r.text().then(function(html){{
          // Server returned login page again = wrong password
          btn.disabled=false;btn.classList.remove('loading');
          errEl.innerHTML='<p style="color:#e05;font-size:.82rem;margin-top:12px;">Wrong password.</p>';
          document.getElementById('pw').value='';
          document.getElementById('pw').focus();
        }});
      }} else {{
        btn.disabled=false;btn.classList.remove('loading');
        errEl.innerHTML='<p style="color:#e05;font-size:.82rem;margin-top:12px;">Error '+r.status+'</p>';
      }}
    }})
    .catch(function(){{
      btn.disabled=false;btn.classList.remove('loading');
      errEl.innerHTML='<p style="color:#e05;font-size:.82rem;margin-top:12px;">Network error.</p>';
    }});
}});
</script></body></html>"""


def _clean_caption(c: str) -> str:
    if not c:
        return c
    # Strip leading emoji only (safe explicit ranges — no ASCII bleed)
    c = re.sub(
        r"^(?:[\U0001F300-\U0001F9FF]|[\U0001F000-\U0001F2FF]"
        r"|[\u2600-\u26FF]|[\u2700-\u27BF]|[\uFE00-\uFE0F]|\u200D)+\s*",
        "",
        c,
        flags=re.UNICODE,
    ).strip()
    # Cut at whitespace + emoji/arrow/@ promo bullet mid-text
    c = re.split(
        r"(?:[\s\u00a0]+)(?="
        r"[\U0001F300-\U0001F9FF]|[\U0001F000-\U0001F2FF]"
        r"|[\u2600-\u26FF]|[\u2700-\u27BF]"
        r"|[\u2190-\u21FF]|[\u2794-\u27BF]|\u27A0|\u00BB|@\w)",
        c,
        flags=re.UNICODE,
    )[0].strip()
    # Cut at any line with @handle / t.me / promo keywords
    lines = c.splitlines()
    clean = []
    for ln in lines:
        if re.search(r"@\w|t\.me/|join\s+us|channel|group", ln, re.I):
            break
        clean.append(ln)
    return "\n".join(clean).strip()


@web.middleware
async def auth_middleware(req: web.Request, handler):
    import config as _cfg

    if not _cfg.LOGIN_REQUIRED:
        return await handler(req)
    if req.path in _PUBLIC_PATHS or req.path.startswith(_PUBLIC_PREFIXES):
        return await handler(req)
    if not _valid_token(req.cookies.get(_AUTH_COOKIE, "")):
        if req.path.startswith("/api/"):
            return web.Response(status=401, text="Unauthorized")
        raise web.HTTPFound("/login")
    return await handler(req)


@web.middleware
async def compression_middleware(req, handler):
    resp = await handler(req)
    if resp.content_type and "text/html" in resp.content_type:
        resp.enable_compression()
    elif resp.content_type and "application/json" in resp.content_type:
        resp.enable_compression()
    return resp


# ── LOGIN / LOGOUT ────────────────────────────────────────────────────────────
async def route_login_get(req: web.Request):
    return web.Response(content_type="text/html", text=_login_page())


async def route_login_post(req: web.Request):
    data = await req.post()
    pw = data.get("password", "")
    if _hashlib.sha256(pw.encode()).hexdigest() == _PASSWORD_HASH:
        resp = web.HTTPFound("/")
        resp.set_cookie(
            _AUTH_COOKIE,
            _make_token(),
            max_age=SESSION_TIMEOUT * 2,
            httponly=True,
            samesite="Lax",
        )
        return resp
    return web.Response(content_type="text/html", text=_login_page(error=True))


async def route_logout(req: web.Request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(_AUTH_COOKIE)
    return resp


# ── INDEX ─────────────────────────────────────────────────────────────────────
async def route_index(req):
    import cache as _cache

    await get_videos()

    # Fast path: return cached HTML if nothing has changed since last render
    if _cache._index_html_cache is not None and not _cache._albums_dirty:
        return web.Response(content_type="text/html", text=_cache._index_html_cache)

    if _cache._albums_dirty:
        _cache._rebuild_albums_index()

    albums = _cache._albums_index

    uncached = [a for a in albums if a["name"] not in _cache._poster_mem]
    if uncached:
        album_names = [a["name"] for a in uncached]
        meta_map = await _fetch_all_meta(album_names)
        loop = asyncio.get_event_loop()

        def _slug_for(name: str) -> str:
            return _hashlib.md5(name.encode()).hexdigest()

        def _read_lqip(slug: str) -> str:
            lqip_path = os.path.join(POSTERS_DIR, f"{slug}.lqip")
            try:
                with open(lqip_path, "r") as _f:
                    return _f.read().strip()
            except Exception:
                return ""

        from render import _POSTER_SEM

        async def _resolve(a):
            m = meta_map.get(a["name"], {})
            if not isinstance(m, dict):
                m = {}
            thumb = a.get("thumb_url") or ""
            remote = m.get("poster") or (thumb if thumb.startswith("http") else "")
            if remote:
                async with _POSTER_SEM:
                    poster_url = await loop.run_in_executor(
                        None, _cache_poster_sync, a["name"], remote
                    )
                poster_sm_url = (
                    (poster_url + "?sm=1")
                    if poster_url.startswith("/poster/")
                    else poster_url
                )
                slug = _slug_for(a["name"])
                lqip = await loop.run_in_executor(None, _read_lqip, slug)
            else:
                poster_url = poster_sm_url = lqip = ""
            _cache._poster_mem[a["name"]] = {
                "poster_url": poster_url,
                "poster_sm_url": poster_sm_url,
                "lqip": lqip,
                "meta": m,
            }

        await asyncio.gather(*[_resolve(a) for a in uncached])

    render_albums = []
    for a in albums:
        pm = _cache._poster_mem.get(a["name"], {})
        render_albums.append(
            {
                "name": a["name"],
                "videos": a["videos"],
                "poster_url": pm.get("poster_url", ""),
                "poster_sm_url": pm.get("poster_sm_url", ""),
                "lqip": pm.get("lqip", ""),
                "meta": pm.get("meta", {}),
            }
        )

    html = _render_index(render_albums, len(_cache._cache_mem))
    # Inject scroll-position restore script before </body>
    # Retry loop: lazy images cause layout shifts after first scroll, keep snapping until landed.
    _SCROLL_SCRIPT = (
        "<script>"
        "(function(){"
        'var _SK="sv_idx_scroll";'
        'var _y=parseInt(sessionStorage.getItem(_SK)||"0",10);'
        "if(_y>0){"
        'document.documentElement.style.scrollBehavior="auto";'
        'document.body.style.scrollBehavior="auto";'
        "var _a=0;"
        "function _snap(){"
        'window.scrollTo({top:_y,behavior:"instant"});'
        "_a++;"
        "if(_a<40&&Math.abs(window.scrollY-_y)>4)setTimeout(_snap,50);"
        'else{document.documentElement.style.scrollBehavior="";document.body.style.scrollBehavior="";}}'
        "_snap();}"
        'window.addEventListener("pagehide",function(){sessionStorage.setItem(_SK,String(window.scrollY));},true);'
        'window.addEventListener("click",function(e){'
        'var a=e.target.closest("a[href]");'
        'if(a&&a.href&&a.href.indexOf("javascript")<0&&new URL(a.href,location.href).origin===location.origin){'
        "sessionStorage.setItem(_SK,String(window.scrollY));}"
        "},true);"
        "})();"
        "</script>"
    )
    html = html.replace("</body>", _SCROLL_SCRIPT + "</body>", 1)
    _cache._index_html_cache = html
    # Add ETag for conditional requests
    etag = '"' + _hashlib.md5(html.encode()).hexdigest()[:16] + '"'
    if req.headers.get("If-None-Match") == etag:
        return web.Response(status=304)
    return web.Response(content_type="text/html", text=html, headers={"ETag": etag})


async def route_healthz(req):
    return web.Response(content_type="text/plain", text="ok")


import sqlite3 as _sqlite3


def _fts_album_ids_safe(name: str) -> set:
    """fts_album_ids with automatic fallback + background rebuild on FTS corruption."""
    import cache as _cache

    try:
        return set(fts_album_ids(name))
    except (_sqlite3.DatabaseError, _sqlite3.OperationalError) as e:
        print(
            f"[fts] corrupt index ({e}), falling back to linear scan — triggering rebuild"
        )
        asyncio.ensure_future(_fts_rebuild_bg())
        return set()


async def _fts_rebuild_bg():
    """Rebuild FTS index in executor without blocking the event loop."""
    import cache as _cache

    loop = asyncio.get_event_loop()
    try:
        fn = (
            getattr(_cache, "_rebuild_fts_index", None)
            or getattr(_cache, "_build_fts_index", None)
            or getattr(_cache, "rebuild_fts", None)
        )
        if fn:
            await loop.run_in_executor(None, fn)
            print("[fts] index rebuilt OK")
        else:
            # fallback: force re-cache which rebuilds index as side-effect
            await get_videos(force=True)
            print("[fts] index rebuilt via get_videos")
    except Exception as e:
        print(f"[fts] rebuild failed: {e}")


async def route_album(req):
    import cache as _cache

    name = unquote(req.match_info["album_name"])
    ids = _fts_album_ids_safe(name)
    if ids:
        videos = [_cache._cache_meta[i] for i in ids if i in _cache._cache_meta]
    else:
        videos = [v for v in await get_videos() if v["album"] == name]
    pm = _cache._poster_mem.get(name, {})
    meta = pm.get("meta", {})
    poster_sm_url = pm.get("poster_sm_url", "") or pm.get("poster_url", "") or ""
    lqip = pm.get("lqip", "") or ""
    html = _render_album(name, videos, meta, poster_sm_url=poster_sm_url, lqip=lqip)
    html = html.encode("utf-8", errors="ignore").decode("utf-8")
    return web.Response(
        content_type="text/html",
        text=html,
    )


async def route_album_data(req: web.Request):
    """GET /api/album_data/{album_name} — JSON video list for async load."""
    import cache as _cache

    name = unquote(req.match_info["album_name"])
    # Fast path: single indexed SQL on videos_plain — O(log n), no dict walk
    videos = fts_album_data(name)
    if not videos:
        # Fallback: FTS ids → cache_meta dict (covers stale/empty DB)
        ids = _fts_album_ids_safe(name)
        if ids:
            videos = [_cache._cache_meta[i] for i in ids if i in _cache._cache_meta]
        else:
            videos = [v for v in await get_videos() if v["album"] == name]

    def _ep_key(v):
        """Sort key: (season, episode, title) — pure numeric, no string compare."""
        fname = v.get("filename", "") or v.get("title", "")
        # SxxExx or xx×xx
        m = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", fname)
        if m:
            return (int(m.group(1)), int(m.group(2)), fname.lower())
        m = re.search(r"(\d{1,2})[xX](\d{2})", fname)
        if m:
            return (int(m.group(1)), int(m.group(2)), fname.lower())
        # bare episode number e.g. "Episode 120" or trailing digits
        m = re.search(r"(?:Episode\s+|Ep\.?\s*)(\d+)", fname, re.I)
        if m:
            return (1, int(m.group(1)), fname.lower())
        m = re.search(r"(\d+)\s*$", fname)
        if m:
            return (1, int(m.group(1)), fname.lower())
        return (0, 0, fname.lower())

    videos_sorted = sorted(videos, key=_ep_key)

    payload = [
        {
            "id": v["message_id"],
            "title": v.get("title", ""),
            "caption": _clean_caption(v.get("caption", "")),
            "thumb": v.get("thumb_url") or "",
            "dur": v.get("duration", ""),
            "size": v.get("size", ""),
            "size_bytes": v.get("size_bytes", 0) or 0,
            "date": v.get("date", "") or "",
            "quality": v.get("quality", ""),
            "vlc": f"/stream/{v['message_id']}",
            "season": _parse_season(v.get("filename", "") or v.get("title", "")),
        }
        for v in videos_sorted
    ]
    return _fast_json_response(payload)


# ── THUMB / POSTER ────────────────────────────────────────────────────────────
async def route_poster(req: web.Request):
    """Serve locally cached poster images. ?sm=1 → 300px variant."""
    slug = req.match_info["slug"]
    if not re.match(r"^[a-f0-9]{32}$", slug):
        return web.Response(status=400)
    use_sm = req.rel_url.query.get("sm") == "1"
    if use_sm:
        path = os.path.join(POSTERS_DIR, f"{slug}_sm.jpg")
        if not os.path.exists(path):
            path = os.path.join(POSTERS_DIR, f"{slug}.jpg")
    else:
        path = os.path.join(POSTERS_DIR, f"{slug}.jpg")
    if not os.path.exists(path):
        return web.Response(status=404)
    with open(path, "rb") as f:
        data = f.read()
    return web.Response(
        body=data,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=604800"},
    )


# ── ALBUM MANAGEMENT ──────────────────────────────────────────────────────────
async def route_album_assign(req: web.Request):
    """POST /api/album_assign — assign or clear album override for msg_ids."""
    try:
        import cache as _cache
        from helpers import _canonicalize_album

        body = await req.json()
        msg_ids = [str(x) for x in body.get("msg_ids", [])]
        album = body.get("album", "").strip()
        if not msg_ids:
            return web.json_response({"ok": False, "error": "no msg_ids"}, status=400)

        overrides = _load_album_overrides()
        canonical = _canonicalize_album(album) if album else ""

        if canonical:
            for mid in msg_ids:
                overrides[mid] = canonical
        else:
            for mid in msg_ids:
                overrides.pop(mid, None)
        _save_album_overrides(overrides)

        # ── Apply immediately to in-memory cache + FTS (no restart needed) ──
        int_ids = {int(mid) for mid in msg_ids}
        changed = []
        for v in _cache._cache_mem:
            if v["message_id"] in int_ids:
                v["album"] = canonical if canonical else v.get("album", "")
                v["album_pinned"] = bool(canonical)
                changed.append(v)
        # Sync _cache_meta (same dicts, but keep meta up-to-date)
        for v in changed:
            _cache._cache_meta[v["message_id"]] = v
        if changed:
            _cache._fts_upsert(changed)
            _cache._albums_dirty = True
            _cache._index_html_cache = None
        # Persist full cache so pinned albums survive restart
        if changed:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, _cache._save_json, _cache.CACHE_FILE, _cache._cache_mem
            )

        # Invalidate uncategorized count cache so the nav bell updates
        _cache._api_cache.pop("uncategorized_count", None)

        return web.json_response(
            {"ok": True, "assigned": len(msg_ids), "album": canonical}
        )
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_uncategorized_count(req: web.Request):
    """GET /api/uncategorized_count — badge delta since last settings visit."""
    global _seen_watermark
    from cache import get_uncategorized_count, api_cache_get, api_cache_set

    cached = api_cache_get("uncategorized_count")
    if cached is None:
        cached = get_uncategorized_count()
        api_cache_set("uncategorized_count", cached)
    if _seen_watermark == -1:
        _seen_watermark = cached
    delta = max(0, cached - _seen_watermark)
    return _fast_json_response({"ok": True, "count": delta})


async def route_mark_notif_seen(req: web.Request):
    """POST /api/notifications/seen — set watermark to current total; badge → 0."""
    global _seen_watermark
    from cache import get_uncategorized_count, api_cache_get, api_cache_set

    cached = api_cache_get("uncategorized_count")
    if cached is None:
        cached = get_uncategorized_count()
        api_cache_set("uncategorized_count", cached)
    _seen_watermark = cached
    return _fast_json_response({"ok": True})


async def route_search(req: web.Request):
    """GET /api/search?q=... — FTS5 full-text search across all videos."""
    query = req.rel_url.query.get("q", "").strip()
    if not query:
        return _fast_json_response({"ok": True, "results": []})
    limit = int(req.rel_url.query.get("limit", "50"))
    results = fts_search_detailed(query, limit=limit)
    return _fast_json_response({"ok": True, "results": results})


async def route_stream_cache_stats(req: web.Request):
    """GET /api/stream_cache — rolling buffer cache statistics + Bot API stats."""
    from streaming import _stream_cache

    result = {"ok": True, **_stream_cache.stats()}

    # Include Bot API stats if available
    try:
        from botapi_stream import botapi_stats, get_sync_progress

        result["botapi"] = botapi_stats()
        result["botapi_sync"] = get_sync_progress()
    except ImportError:
        pass

    return _fast_json_response(result)


async def route_history(req: web.Request):
    """GET /api/history — list stream history."""
    from cache import api_cache_get, api_cache_set

    cached = api_cache_get("history")
    if cached is not None:
        return _fast_json_response({"ok": True, "history": cached})
    hist = history_list(limit=50)
    api_cache_set("history", hist)
    return _fast_json_response({"ok": True, "history": hist})


async def route_history_remove(req: web.Request):
    """POST /api/history/remove — remove a history entry."""
    try:
        body = await req.json()
        message_id = int(body.get("message_id", 0))
        if not message_id:
            return web.json_response(
                {"ok": False, "error": "message_id required"}, status=400
            )
        removed = history_remove(message_id)
        # Invalidate cache
        from cache import _api_cache

        _api_cache.pop("history", None)
        return web.json_response({"ok": True, "removed": removed})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_history_record(req: web.Request):
    """POST /api/history/record — record a stream playback."""
    from cache import _api_cache

    try:
        body = await req.json()
        message_id = int(body.get("message_id", 0))
        if not message_id:
            return web.json_response(
                {"ok": False, "error": "message_id required"}, status=400
            )
        meta = _meta(message_id)
        history_add(
            message_id=message_id,
            title=meta.get("title", ""),
            album=meta.get("album", ""),
            thumb_url=meta.get("thumb_url", ""),
            duration=meta.get("duration", ""),
            quality=meta.get("quality", ""),
            size=meta.get("size", ""),
        )
        _api_cache.pop("history", None)
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_album_list(req: web.Request):
    """GET /api/albums — all unique album names."""
    names = fts_album_names()
    if not names:
        videos = _load_cache()
        names = sorted({v["album"] for v in videos if v.get("album")})
    return web.json_response({"ok": True, "albums": names})


async def route_delete_videos(req: web.Request):
    """POST /api/delete_videos — remove from local cache (not from Telegram)."""
    try:
        body = await req.json()
        msg_ids = {int(x) for x in body.get("msg_ids", [])}
        if not msg_ids:
            return web.json_response({"ok": False, "error": "no msg_ids"}, status=400)
        videos = _load_cache()
        before = len(videos)
        videos = [v for v in videos if v["message_id"] not in msg_ids]
        removed = before - len(videos)
        _save_cache(videos)
        overrides = _load_album_overrides()
        for mid in msg_ids:
            overrides.pop(str(mid), None)
        _save_album_overrides(overrides)
        print(f"[delete] removed {removed} video(s) from cache: {msg_ids}")
        return web.json_response({"ok": True, "removed": removed})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_delete_album(req: web.Request):
    """POST /api/delete_album — remove all videos in an album from cache + thumbs + blacklist."""
    try:
        body = await req.json()
        album_name = (body.get("album") or "").strip()
        if not album_name:
            return web.json_response(
                {"ok": False, "error": "album required"}, status=400
            )
        import cache as _cache
        from helpers import album_key as _ak

        target_key = _ak(album_name)
        # Use in-memory cache — avoids full disk read on event loop
        videos = list(_cache._cache_mem) if _cache._cache_mem else _load_cache()
        to_delete = {
            v["message_id"] for v in videos if _ak(v.get("album", "")) == target_key
        }
        if not to_delete:
            return web.json_response(
                {"ok": False, "error": "album not found or already empty"}, status=404
            )
        remaining = [v for v in videos if v["message_id"] not in to_delete]

        # Update in-memory state immediately so subsequent requests see clean data
        _cache._cache_mem[:] = remaining
        _cache._cache_meta = {v["message_id"]: v for v in remaining}
        _cache._albums_dirty = True
        _cache._index_html_cache = None

        loop = asyncio.get_event_loop()

        # Offload all blocking I/O to thread pool — respond without waiting on disk
        slug = _hashlib.md5(album_name.encode()).hexdigest()
        poster_paths = [
            os.path.join(POSTERS_DIR, f"{slug}{s}")
            for s in (".jpg", "_sm.jpg", ".lqip")
        ]

        def _persist_and_rm():
            # Save cache
            _cache._save_json(_cache.CACHE_FILE, remaining)
            # Save overrides
            overrides = _load_album_overrides()
            for mid in to_delete:
                overrides.pop(str(mid), None)
            _save_album_overrides(overrides)
            for p in poster_paths:
                try:
                    os.remove(p)
                except FileNotFoundError:
                    pass

        asyncio.ensure_future(loop.run_in_executor(None, _persist_and_rm))

        # Blacklist: future fetches skip videos whose album matches this name
        _add_deleted_album(album_name)

        _cache._poster_mem.pop(album_name, None)
        _cache._imdb_cache_mem.pop(album_name.strip().lower(), None)
        print(
            f"[delete_album] '{album_name}' — removed {len(to_delete)} video(s), blacklisted"
        )
        return web.json_response(
            {"ok": True, "removed": len(to_delete), "album": album_name}
        )
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ── FETCH (pull new data from Telegram) ──────────────────────────────────────
async def route_fetch(req):
    try:
        data = await get_videos(force=True)
        return web.json_response({"ok": True, "count": len(data)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ── IMDB GAP FILL ────────────────────────────────────────────────────────────
async def route_imdb_gap_fill(req: web.Request):
    """POST /api/imdb_gap_fill — fill missing IMDB metadata using TMDB-first search."""
    try:
        import cache as _cache

        body = {}
        try:
            body = await req.json()
        except Exception:
            pass

        album_names = body.get("albums") or None  # None = scan all
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _cache._fill_imdb_gaps_sync, album_names
        )

        # Invalidate poster cache so new metadata shows
        _cache._poster_mem.clear()
        _cache._index_html_cache = None

        return web.json_response({"ok": True, **result})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


# ── REFRESH (full app restart / page reload signal) ───────────────────────────
async def route_refresh(req):
    """POST /api/refresh — tells the client to do a hard page reload."""
    return web.json_response({"ok": True, "reload": True})


# ── SETTINGS API ──────────────────────────────────────────────────────────────
async def route_settings_get(req: web.Request):
    """GET /api/settings — return current mutable settings."""
    import config as _cfg

    channel = _cfg.CHANNEL_ID or ""
    tmdb_key = getattr(_cfg, "TMDB_API_KEY", "") or ""
    return web.json_response(
        {
            "ok": True,
            "login_required": _cfg.LOGIN_REQUIRED,
            "channel_id": channel,
            "tmdb_api_key": tmdb_key,
        }
    )


async def route_settings_post(req: web.Request):
    """POST /api/settings — update login_required and/or channel_id at runtime."""
    import config as _cfg

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    changed = []

    if "login_required" in body:
        val = bool(body["login_required"])
        _cfg.LOGIN_REQUIRED = val
        _env_set("LOGIN_REQUIRED", "1" if val else "0")
        changed.append("login_required")

    if "channel_id" in body:
        cid = str(body["channel_id"]).strip()
        _cfg.CHANNEL_ID = cid
        _env_set("CHANNEL_ID", cid)
        changed.append("channel_id")

    if "tmdb_api_key" in body:
        key = str(body["tmdb_api_key"]).strip()
        _cfg.TMDB_API_KEY = key
        _env_set("TMDB_API_KEY", key)
        changed.append("tmdb_api_key")

    return web.json_response({"ok": True, "changed": changed})


def _env_set(key: str, value: str):
    """Persist a key=value into the .env file and update os.environ immediately."""
    import config as _cfg

    os.environ[key] = value  # take effect now, no restart needed

    env_path = os.path.join(_cfg._here, ".env")
    lines = []
    found = False
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        pass
    new_lines = []
    for ln in lines:
        if ln.strip().startswith(key + "=") or ln.strip().startswith(key + " ="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(ln)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


# ── SETTINGS PAGE ─────────────────────────────────────────────────────────────
async def route_settings_page(req: web.Request):
    global _seen_watermark
    from cache import get_uncategorized_count, api_cache_get, api_cache_set

    # Mark all current uncategorized as seen so badge clears on page load
    cached = api_cache_get("uncategorized_count")
    if cached is None:
        cached = get_uncategorized_count()
        api_cache_set("uncategorized_count", cached)
    _seen_watermark = cached
    import config as _cfg
    from render import _head, _nav, BASE_CSS, _PAGE_CLOSE

    login_checked = "checked" if _cfg.LOGIN_REQUIRED else ""
    channel_val = _cfg.CHANNEL_ID or ""
    tmdb_key_val = getattr(_cfg, "TMDB_API_KEY", "") or ""
    html = _head("Settings \u2013 StreamVault") + _nav() + f"""
<style>
.sv-settings-wrap{{max-width:600px;margin:40px auto;padding:0 max(16px,3vw) 80px;}}
.sv-settings-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-lg);padding:28px 28px 24px;margin-bottom:18px;}}
.sv-settings-card h2{{font-size:.88rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);margin-bottom:18px;}}
.sv-settings-row{{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid var(--border);gap:14px;}}
.sv-settings-row:last-child{{border-bottom:none;}}
.sv-settings-label{{font-size:.84rem;font-weight:600;color:var(--text);}}
.sv-settings-sub{{font-size:.72rem;color:var(--text2);margin-top:3px;}}
.sv-toggle{{position:relative;width:42px;height:24px;flex-shrink:0;}}
.sv-toggle input{{opacity:0;width:0;height:0;}}
.sv-toggle-track{{position:absolute;inset:0;background:var(--border-hi);border-radius:50px;cursor:pointer;transition:background .2s;}}
.sv-toggle input:checked+.sv-toggle-track{{background:var(--accent);}}
.sv-toggle-track::after{{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:transform .2s;}}
.sv-toggle input:checked+.sv-toggle-track::after{{transform:translateX(18px);}}
.sv-btn-row{{display:flex;gap:10px;flex-wrap:wrap;margin-top:4px;}}
.sv-btn{{height:38px;padding:0 18px;border-radius:var(--radius-sm);font-size:.8rem;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:7px;border:1.5px solid var(--border);background:transparent;color:var(--text2);transition:border-color .2s,color .2s;text-decoration:none;white-space:nowrap;}}
.sv-btn:hover{{border-color:var(--accent);color:var(--accent);}}
.sv-btn-primary{{background:var(--accent);color:#000;border-color:var(--accent);}}
.sv-btn-primary:hover{{opacity:.85;color:#000;}}
.sv-btn-danger{{border-color:#553;color:#c66;}}
.sv-btn-danger:hover{{border-color:#f66;color:#f66;}}
.sv-channel-input{{height:38px;padding:0 12px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:.85rem;outline:none;width:100%;font-family:inherit;}}
.sv-channel-input:focus{{border-color:var(--accent);}}
.sv-status{{font-size:.73rem;min-height:18px;margin-top:6px;}}
.sv-status.ok{{color:#4c4;}}
.sv-status.err{{color:#f66;}}
.sv-nav-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.sv-nav-tile{{display:flex;flex-direction:column;align-items:flex-start;gap:6px;padding:14px 16px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius-sm);cursor:pointer;text-decoration:none;color:var(--text2);font-size:.78rem;font-weight:600;transition:border-color .2s,color .2s;text-align:left;font-family:inherit;}}
.sv-nav-tile:hover{{border-color:var(--accent);color:var(--accent);}}
.sv-nav-tile svg{{opacity:.7;}}
.sv-nav-tile:hover svg{{opacity:1;}}
.sv-nav-tile-label{{font-size:.82rem;font-weight:700;color:var(--text);}}
.sv-nav-tile-sub{{font-size:.7rem;color:var(--text2);}}
.sv-fetch-tile{{background:var(--accent-lo);border-color:rgba(245,197,24,.25);}}
.sv-fetch-tile .sv-nav-tile-label{{color:var(--accent);}}
.sv-fetch-tile:hover{{border-color:var(--accent);background:rgba(245,197,24,.12);}}
.sv-imdb-fill-tile{{background:rgba(99,102,241,.08);border-color:rgba(99,102,241,.25);}}
.sv-imdb-fill-tile .sv-nav-tile-label{{color:#a5b4fc;}}
.sv-imdb-fill-tile:hover{{border-color:#6366f1;background:rgba(99,102,241,.12);}}
.sv-clear-cache-tile{{background:rgba(239,68,68,.06);border-color:rgba(239,68,68,.2);}}
.sv-clear-cache-tile .sv-nav-tile-label{{color:#fca5a5;}}
.sv-clear-cache-tile:hover{{border-color:#ef4444;background:rgba(239,68,68,.12);}}
.sv-logout-card{{background:#0d0808;border:1px solid #c66;border-radius:var(--radius-lg);padding:22px 28px;margin-bottom:18px;display:flex;align-items:center;justify-content:space-between;gap:16px;transition:border-color .2s;}}
.sv-logout-card:hover{{border-color:#3d1a1a;}}
.sv-logout-title{{font-size:.9rem;font-weight:700;color:#e88;}}
.sv-logout-sub{{font-size:.72rem;color:#a66;margin-top:3px;}}
.sv-logout-btn{{flex-shrink:0;height:40px;padding:0 20px;background:transparent;border:1.5px solid #c66;border-radius:var(--radius-sm);color:#c66;font-size:.82rem;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:8px;text-decoration:none;transition:border-color .2s,color .2s,background .2s;white-space:nowrap;}}
.sv-logout-btn:hover{{border-color:#f55;color:#fff;background:#c00022;}}
@media(max-width:480px){{.sv-nav-grid{{grid-template-columns:1fr;}}.sv-logout-card{{flex-direction:column;align-items:flex-start;}}}}
</style>
<div class="sv-settings-wrap">
  <h1 style="font-size:1.5rem;font-weight:900;margin-bottom:22px;">Settings</h1>

  <!-- Navigation actions -->
  <div class="sv-settings-card">
    <h2>Quick Actions</h2>
    <div class="sv-nav-grid">
      <a class="sv-nav-tile" href="/manage">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h7"/><circle cx="17" cy="18" r="3"/><path d="M17 15v3l2 1"/></svg>
        <span class="sv-nav-tile-label">Manage</span>
        <span class="sv-nav-tile-sub">Browse &amp; assign videos</span>
      </a>
      <a class="sv-nav-tile" href="/manage/albums">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="10" rx="1"/><rect x="14" y="3" width="7" height="10" rx="1"/><path d="M3 17h18M3 21h18"/></svg>
        <span class="sv-nav-tile-label">Albums</span>
        <span class="sv-nav-tile-sub">Edit posters &amp; metadata</span>
      </a>
      <button class="sv-nav-tile sv-fetch-tile" onclick="doFetch(this)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        <span class="sv-nav-tile-label">Fetch from Telegram</span>
        <span class="sv-nav-tile-sub">Pull latest videos</span>
      </button>
      <button class="sv-nav-tile" onclick="doRefresh(this)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round"><path d="M23 4v6h-6M1 20v-6h6"/><path d="M3.51 9a9 9 0 0114.13-3.36L23 10M1 14l5.36 4.36A9 9 0 0020.49 15"/></svg>
        <span class="sv-nav-tile-label">Refresh App</span>
        <span class="sv-nav-tile-sub">Reload the page</span>
      </button>
      <button class="sv-nav-tile sv-imdb-fill-tile" onclick="doImdbGapFill(this)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
        <span class="sv-nav-tile-label">Fill IMDB Gaps</span>
        <span class="sv-nav-tile-sub">TMDB-powered metadata search</span>
      </button>
      <button class="sv-nav-tile sv-clear-cache-tile" onclick="doClearCache(this)">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a1 1 0 011-1h4a1 1 0 011 1v2"/></svg>
        <span class="sv-nav-tile-label">Clear Stream Cache</span>
        <span class="sv-nav-tile-sub">Fix no-audio / jitter issues</span>
      </button>
    </div>
  </div>

  <!-- Auth -->
  <div class="sv-settings-card">
    <h2>Access</h2>
    <div class="sv-settings-row">
      <div>
        <div class="sv-settings-label">Require Login</div>
        <div class="sv-settings-sub">When off, anyone on the network can access StreamVault without a password.</div>
      </div>
      <label class="sv-toggle">
        <input type="checkbox" id="loginToggle" {login_checked} onchange="saveLoginRequired(this.checked)">
        <span class="sv-toggle-track"></span>
      </label>
    </div>
  </div>

  <!-- Channel -->
  <div class="sv-settings-card">
    <h2>Telegram Channel</h2>
    <div class="sv-settings-row" style="flex-direction:column;align-items:flex-start;">
      <div class="sv-settings-label">Channel ID / Username</div>
      <div class="sv-settings-sub">e.g. <code>-1001234567890</code> or <code>@mychannel</code></div>
      <input class="sv-channel-input" style="margin-top:10px;" id="channelInput" type="text" value="{channel_val}" placeholder="-1001234567890">
      <div class="sv-btn-row" style="margin-top:10px;">
        <button class="sv-btn sv-btn-primary" onclick="saveChannel()">Save Channel</button>
      </div>
      <div class="sv-status" id="channelStatus"></div>
    </div>
  </div>

  <!-- TMDB API -->
  <div class="sv-settings-card">
    <h2>TMDB Metadata</h2>
    <div class="sv-settings-row" style="flex-direction:column;align-items:flex-start;">
      <div class="sv-settings-label">TMDB API Key</div>
      <div class="sv-settings-sub">Used by <strong>Fill IMDB Gaps</strong> to search posters, plots, ratings &amp; stars. Default key works out of the box.</div>
      <input class="sv-channel-input" style="margin-top:10px;" id="tmdbKeyInput" type="text" value="{tmdb_key_val}" placeholder="Your TMDB v3 API key">
      <div class="sv-btn-row" style="margin-top:10px;">
        <button class="sv-btn sv-btn-primary" onclick="saveTmdbKey()">Save Key</button>
      </div>
      <div class="sv-status" id="tmdbKeyStatus"></div>
    </div>
  </div>

  <!-- Notifications -->
  <div class="sv-settings-card">
    <h2>Notifications</h2>
    <div id="notifList" style="font-size:.78rem;color:var(--text2);min-height:24px;">Loading…</div>
  </div>

  <!-- Logout -->
  <div class="sv-logout-card">
    <div>
      <div class="sv-logout-title">Sign Out</div>
      <div class="sv-logout-sub">You will be redirected to the login screen.</div>
    </div>
    <a class="sv-logout-btn" href="/logout">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
      Logout
    </a>
  </div>

</div>
<script>
async function saveLoginRequired(val) {{
  try {{
    var r = await fetch('/api/settings', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{login_required:val}})}});
    var j = await r.json();
    if(!j.ok) document.getElementById('loginToggle').checked = !val;
  }} catch(e) {{ document.getElementById('loginToggle').checked = !val; }}
}}
async function saveChannel() {{
  var cid = document.getElementById('channelInput').value.trim();
  var st = document.getElementById('channelStatus');
  st.className='sv-status';st.textContent='';
  try {{
    var r = await fetch('/api/settings', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{channel_id:cid}})}});
    var j = await r.json();
    if(j.ok){{st.className='sv-status ok';st.textContent='\u2713 Saved. Fetch to apply.';}}
    else{{st.className='sv-status err';st.textContent='\u2717 '+(j.error||'error');}}
  }} catch(e) {{st.className='sv-status err';st.textContent='\u2717 Network error';}}
}}
async function saveTmdbKey() {{
  var key = document.getElementById('tmdbKeyInput').value.trim();
  var st = document.getElementById('tmdbKeyStatus');
  st.className='sv-status';st.textContent='';
  try {{
    var r = await fetch('/api/settings', {{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tmdb_api_key:key}})}});
    var j = await r.json();
    if(j.ok){{st.className='sv-status ok';st.textContent='\u2713 Saved. Use Fill IMDB Gaps to apply.';}}
    else{{st.className='sv-status err';st.textContent='\u2717 '+(j.error||'error');}}
  }} catch(e) {{st.className='sv-status err';st.textContent='\u2717 Network error';}}
}}
function doFetch(btn) {{
  var orig=btn.innerHTML;
  btn.disabled=true;
  btn.querySelector('.sv-nav-tile-label').textContent='Fetching\u2026';
  fetch('/api/fetch',{{method:'POST'}}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.ok)window.location.href='/';
    else{{btn.disabled=false;btn.innerHTML=orig;}}
  }}).catch(function(){{btn.disabled=false;btn.innerHTML=orig;}});
}}
function doClearCache(btn) {{
  var orig=btn.innerHTML;
  btn.disabled=true;
  btn.querySelector('.sv-nav-tile-label').textContent='Clearing\u2026';
  fetch('/api/clear_cache',{{method:'POST'}}).then(function(r){{return r.json();}}).then(function(d){{
    btn.disabled=false;btn.innerHTML=orig;
    if(d.ok){{
      var n=d.cleared?d.cleared.length:0;
      btn.querySelector('.sv-nav-tile-sub').textContent='Cleared '+n+' video'+(n!==1?'s':'')+' \u2713';
      setTimeout(function(){{btn.querySelector('.sv-nav-tile-sub').textContent='Fix no-audio / jitter issues';}},3000);
    }} else {{
      alert('Error: '+(d.error||'unknown'));
    }}
  }}).catch(function(){{btn.disabled=false;btn.innerHTML=orig;alert('Network error');}});
}}
function doRefresh(btn) {{
  btn.disabled=true;
  btn.querySelector('.sv-nav-tile-label').textContent='Reloading\u2026';
  window.location.reload(true);
}}
function doImdbGapFill(btn) {{
  var orig=btn.innerHTML;
  btn.disabled=true;
  btn.querySelector('.sv-nav-tile-label').textContent='Searching\u2026';
  fetch('/api/imdb_gap_fill',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{}})}}).then(function(r){{return r.json();}}).then(function(d){{
    btn.disabled=false;btn.innerHTML=orig;
    if(d.ok){{
      var msg='Filled '+d.filled+' album'+(d.filled!==1?'s':'')+', skipped '+d.skipped;
      if(d.errors) msg+=', errors: '+d.errors;
      alert(msg);
      if(d.filled>0) window.location.reload();
    }} else {{
      alert('Error: '+(d.error||'unknown'));
    }}
  }}).catch(function(){{btn.disabled=false;btn.innerHTML=orig;alert('Network error');}});
}}
(async function initSettings() {{
  try {{
    var r=await fetch('/api/settings');
    var j=await r.json();
    if(j.ok) {{
      document.getElementById('loginToggle').checked=!!j.login_required;
    }}
  }} catch(e) {{}}
  try {{
    var r=await fetch('/api/notifications');
    var j=await r.json();
    var el=document.getElementById('notifList');
    if(!j.items||j.items.length===0){{el.textContent='No new notifications.';return;}}
    el.innerHTML=j.items.map(function(n){{
      var col=n.type==='err'?'#f66':n.type==='ok'?'#4c4':'#fa0';
      var d=new Date(n.ts*1000).toLocaleTimeString();
      return '<div style="padding:4px 0;border-bottom:1px solid var(--border);color:'+col+';">['+d+'] '+n.msg+'</div>';
    }}).join('');
  }} catch(e) {{}}
}})();
</script>""" + _PAGE_CLOSE + """</body></html>"""
    return web.Response(content_type="text/html", text=html)


async def route_meta(req: web.Request):
    """Return duration + mime from cache instantly — no FFmpeg, no TG fetch."""
    import cache as _cache

    msg_id = int(req.match_info["msg_id"])
    meta = _cache._cache_meta.get(msg_id)
    if not meta:
        _load_cache()
        meta = _cache._cache_meta.get(msg_id)
    if not meta:
        msg = await _get_msg(msg_id)
        if not msg or not msg.media:
            return web.json_response({"ok": False}, status=404)
        duration_s = float(get_duration(msg) or 0)
        mime = _safe_mime(get_mime(msg))
    else:
        duration_s = meta.get("_duration_s", 0)
        if not duration_s:
            msg = await _get_msg(msg_id)
            duration_s = float(get_duration(msg) or 0) if msg else 0
        mime = _safe_mime(meta.get("mime_type", "video/mp4"))
    return web.json_response(
        {
            "ok": True,
            "duration": duration_s,
            "mime": mime,
            "needs_hls": False,
            "stream_url": f"/stream/{msg_id}",
            "hls_start_url": f"/hls/{msg_id}/start",
        }
    )


# ── VLC LAUNCH ────────────────────────────────────────────────────────────────
def _find_vlc():
    """Locate the VLC executable on the current platform.

    Returns the absolute path to the VLC binary, or None if not found.
    Also sets the _VLC_PLUGIN_DIR module-level variable so the launcher
    can pass VLC_PLUGIN_PATH to the subprocess — without this, VLC
    spawned from Python cannot find its plugins and shows the
    "no plugins were found" error dialog.
    """
    import platform

    global _VLC_PLUGIN_DIR
    system = platform.system()
    if system == "Windows":
        for base in (
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
        ):
            p = os.path.join(base, "VideoLAN", "VLC", "vlc.exe")
            if os.path.exists(p):
                _VLC_PLUGIN_DIR = os.path.join(base, "VideoLAN", "VLC", "plugins")
                return p
    elif system == "Darwin":
        p = "/Applications/VLC.app/Contents/MacOS/VLC"
        if os.path.exists(p):
            _VLC_PLUGIN_DIR = "/Applications/VLC.app/Contents/MacOS/plugins"
            return p
    else:
        # Linux — check common plugin paths
        for plugin_dir in (
            "/usr/lib/vlc/plugins",
            "/usr/lib/x86_64-linux-gnu/vlc/plugins",
            "/usr/local/lib/vlc/plugins",
        ):
            if os.path.isdir(plugin_dir):
                _VLC_PLUGIN_DIR = plugin_dir
                break
    return shutil.which("vlc")


# _VLC_HTTP_PORT / _VLC_HTTP_PASS now sourced from config.py (env-tunable).
# Plugin directory discovered by _find_vlc(); passed as VLC_PLUGIN_PATH
# so VLC can locate its plugins when launched from Python subprocess.
_VLC_PLUGIN_DIR = None

_active_pollers: dict[int, asyncio.Task] = {}
_poller_session: dict[int, int] = {}  # msg_id → session generation counter
_poller_generation: int = 0  # global monotonic counter


async def _vlc_position_poller(msg_id: int, duration: float, generation: int):
    """Poll VLC HTTP API every 2 s, persist position. Stops when VLC closes.

    generation: must match _poller_session[msg_id] at every write — if a newer
    session has started (different video or replay), this poller silently exits
    instead of clobbering the new video's resume position and playhead.
    """
    import aiohttp as _aio
    import cache as _cache_mod
    from cache import resume_set

    url = f"http://127.0.0.1:{_VLC_HTTP_PORT}/requests/status.json"
    auth = _aio.BasicAuth("", _VLC_HTTP_PASS)
    no_resp = 0
    last_pos = 0.0
    stall_count = 0
    await asyncio.sleep(4)
    try:
        async with _aio.ClientSession() as sess:
            while True:
                await asyncio.sleep(2)
                # Cross-pollution guard: bail if a newer session has taken over
                if _poller_session.get(msg_id) != generation:
                    print(
                        f"[resume] poller msg={msg_id} gen={generation} superseded — exiting"
                    )
                    return
                try:
                    async with sess.get(
                        url, auth=auth, timeout=_aio.ClientTimeout(total=2)
                    ) as r:
                        if r.status == 401:
                            print("[resume] VLC HTTP auth failed")
                            break
                        if r.status != 200:
                            no_resp += 1
                        else:
                            no_resp = 0
                            j = await r.json(content_type=None)
                            pos = float(j.get("time", 0))
                            dur = float(j.get("length", 0)) or duration
                            state = j.get("state", "")

                            # Guard again after the await
                            if _poller_session.get(msg_id) != generation:
                                return

                            if state not in ("paused", "stopped") and pos > 0:
                                if abs(pos - last_pos) < 0.1:
                                    stall_count += 1
                                    if stall_count >= 4 and stall_count % 10 == 0:
                                        print(
                                            f"[resume] Playback is buffering at position {pos:.1f}s..."
                                        )
                                else:
                                    stall_count = 0
                            else:
                                stall_count = 0

                            if pos > 0 and abs(pos - last_pos) >= 1:
                                resume_set(msg_id, pos, dur)
                                last_pos = pos
                                # Update stream cache playhead so pre-fetcher
                                # knows where to cache behind
                                meta = _cache_mod._meta(msg_id)
                                total_bytes = meta.get("size_bytes", 0)
                                _stream_cache.update_playhead(
                                    msg_id, pos, dur, total_bytes
                                )
                                print(
                                    f"[resume] saved msg={msg_id} pos={pos:.1f}s dur={dur:.1f}s state={state}"
                                )
                            if state == "stopped":
                                break
                except asyncio.CancelledError:
                    break
                except Exception:
                    no_resp += 1
                if no_resp >= 10:
                    break
    except Exception as e:
        print(f"[resume] poller error msg={msg_id}: {e}")
    finally:
        # Only remove playhead if this is still the active generation
        if _poller_session.get(msg_id) == generation:
            _stream_cache.remove_playhead(msg_id)
        if _active_pollers.get(msg_id) == asyncio.current_task():
            _active_pollers.pop(msg_id, None)
    print(f"[resume] poller done msg={msg_id} gen={generation}")


async def _launch_vlc_direct(
    msg_id: int,
    vlc_path: str,
    display_title: str,
    duration: float,
    resume_pos: float,
    filename: str = "",
):
    """Launch VLC with the server's streaming URL — no local file download.

    VLC opens http://localhost:PORT/vlc/{msg_id}/{filename} which serves raw bytes
    with Range support and Content-Disposition header.  Including the filename in
    the URL allows VLSub and other VLC extensions to identify the video for
    subtitle search.  The server's rolling buffer cache handles fast seeks for
    recently-played positions.  This avoids downloading the full video to disk
    (wasting storage) and lets VLC handle all codecs (H265, AV1, etc.)
    natively via its own demuxer.
    """
    import config as _cfg
    from urllib.parse import quote as _url_quote

    # Build the streaming URL with filename for VLSub compatibility
    # VLSub uses the filename from the URL to search for subtitles
    if filename:
        safe_name = _url_quote(filename, safe="")
        stream_url = f"http://127.0.0.1:{PORT}/vlc/{msg_id}/{safe_name}"
    else:
        stream_url = f"http://127.0.0.1:{PORT}/vlc/{msg_id}"

    start_args = ["--start-time", str(int(resume_pos))] if resume_pos > 5 else []
    if resume_pos > 5:
        print(f"[resume] msg={msg_id} resuming at {resume_pos:.1f}s")

    import cache as _cache_mod

    total_bytes = _cache_mod._meta(msg_id).get("size_bytes", 0)
    _ext = os.path.splitext(filename)[1].lower() if filename else ""

    # Build the subprocess environment — inherit the current env but inject
    # VLC_PLUGIN_PATH so VLC can always locate its plugins directory.
    # Without this, VLC spawned from Python cannot find plugins and shows
    # the "no plugins were found" error dialog.
    _env = os.environ.copy()
    if _VLC_PLUGIN_DIR and os.path.isdir(_VLC_PLUGIN_DIR):
        _env["VLC_PLUGIN_PATH"] = _VLC_PLUGIN_DIR

    # On Windows, set the working directory to VLC's install folder so
    # it can locate libvlc.dll and plugins relative to the executable.
    # On other platforms this is a no-op (cwd stays the server's cwd).
    _vlc_cwd = os.path.dirname(vlc_path) if os.name == "nt" else None

    # Validate that the VLC executable actually exists before launching.
    # A stale path would produce a confusing FileNotFoundError from Popen.
    if not os.path.isfile(vlc_path):
        print(f"[vlc] ERROR: executable not found at {vlc_path!r}")
        return

    # Spawn VLC immediately — zero pre-launch blocking.
    # All cache warming happens async AFTER VLC is spawned so the GUI opens instantly.
    #
    # NOTE on command-line flags:
    #   --clock-synchro, --clock-jitter, and --adaptive-filling-threshold
    #   have been REMOVED because they are either (a) not valid CLI options
    #   in VLC 3.x (--adaptive-filling-threshold does not exist) or (b)
    #   they cause the "invalid command line options" error dialog when VLC
    #   is launched from subprocess.  Low-latency tuning is now handled
    #   purely by network-caching and file-caching values.
    subprocess.Popen(
        [
            vlc_path,
            "--meta-title",
            display_title,
            # VLC HTTP interface — Windows requires non-empty password
            "--extraintf=http",
            "--http-host=127.0.0.1",
            f"--http-port={_VLC_HTTP_PORT}",
            f"--http-password={_VLC_HTTP_PASS}",
            # Loopback delivery — reduce network-caching since 127.0.0.1 has
            # no real network jitter; large values only delay first-frame render.
            "--network-caching=2000",
            "--file-caching=0",
            "--live-caching=0",
            "--disc-caching=0",
            "--sout-mux-caching=0",
            # Auto-reconnect if the local aiohttp stream drops momentarily.
            "--http-reconnect",
            # Don't trust container timestamps blindly — avoids jump-forward on
            # streams where Telegram delivers chunks with irregular PTS values.
            "--no-ts-trust-pcr",
            *start_args,
            stream_url,
        ],
        cwd=_vlc_cwd,
        env=_env,
    )
    print(
        f"[vlc] launched: {vlc_path} url={stream_url} "
        f"title={display_title!r} resume={resume_pos:.1f}s"
    )

    # Build prewarm list — all executed async, VLC already spawned above.
    # Priority order: header → resume point → moov atom (EOF tail for MP4).
    async def _async_prewarm():
        if total_bytes <= 0:
            return

        # Phase 1: header + resume byte (greedy prewarm — fills VLC's first requests)
        phase1 = [(0, 4 * 1024 * 1024)]
        if resume_pos > 5 and duration > 0:
            resume_offset = int((resume_pos / duration) * total_bytes)
            phase1.append((resume_offset, 8 * 1024 * 1024))

        try:
            await _vlc_instant_prewarm(msg_id, phase1, wait_timeout_s=10.0)
        except Exception as e:
            print(f"[vlc] phase1 prewarm failed msg={msg_id}: {e}")

        # Phase 2: moov atom tail + continuation (MP4-specific; VLC needs
        # these to start rendering — without them bandwidth is seen but no video)
        phase2 = []
        if _ext in (".mp4", ".m4v", ""):
            phase2.append((max(0, total_bytes - 8 * 1024 * 1024), 8 * 1024 * 1024))
        if resume_pos > 5 and duration > 0:
            resume_offset = int((resume_pos / duration) * total_bytes)
            phase2.append((resume_offset + 4 * 1024 * 1024, 16 * 1024 * 1024))
        else:
            phase2.append((4 * 1024 * 1024, 16 * 1024 * 1024))

        try:
            await _vlc_instant_prewarm(msg_id, phase2, wait_timeout_s=20.0)
        except Exception as e:
            print(f"[vlc] phase2 prewarm failed msg={msg_id}: {e}")

    asyncio.create_task(_async_prewarm())

    # Cancel any existing VLC position pollers for any video, since only one VLC instance runs at a time
    for mid, task in list(_active_pollers.items()):
        if not task.done():
            task.cancel()
    _active_pollers.clear()

    # Advance the global generation counter and tag this poller session.
    # Any stale poller still running for this msg_id will see the generation
    # mismatch and exit without writing resume/playhead data.
    global _poller_generation
    _poller_generation += 1
    _poller_session[msg_id] = _poller_generation

    # Background poller — saves position + updates playhead for rolling cache
    poller_task = asyncio.create_task(
        _vlc_position_poller(msg_id, duration, _poller_generation)
    )
    _active_pollers[msg_id] = poller_task


async def _cleanup_vlc_tmp():
    """One-time cleanup: remove any leftover vlc_tmp/ files from the old
    download-based VLC launch approach.  No longer needed since we stream
    directly via HTTP URL.
    """
    tmp_dir = os.path.join(_here, "vlc_tmp")
    if not os.path.isdir(tmp_dir):
        return
    try:
        removed = 0
        for entry in os.scandir(tmp_dir):
            if entry.is_file():
                try:
                    os.remove(entry.path)
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[vlc] cleaned up {removed} old temp file(s) from vlc_tmp/")
        # Remove the directory itself if empty
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
    except Exception as e:
        print(f"[vlc] temp cleanup error: {e}")


async def route_launch_vlc(req: web.Request):
    """POST /api/launch_vlc  body: {"msg_id": 123}

    Launches VLC with the server's streaming URL (http://localhost:PORT/vlc/{msg_id}).
    VLC handles all codecs (H265, AV1, etc.) natively — no temp file download.
    The server's rolling buffer cache ensures fast seeks for recently-played positions.
    """
    from helpers import _clean_title
    import cache as _cache

    try:
        data = await req.json()
        msg_id = int(data.get("msg_id", 0))
        if not msg_id:
            return web.json_response(
                {"ok": False, "error": "missing msg_id"}, status=400
            )
        vlc = _find_vlc()
        if not vlc:
            return web.json_response(
                {"ok": False, "error": "VLC not found on this machine"}, status=404
            )

        # Resolve display title: caption → clean title → filename fallback
        meta = _cache._meta(msg_id)
        caption = meta.get("caption", "") or ""
        filename = meta.get("filename", "") or f"video_{msg_id}.mp4"
        duration = float(meta.get("_duration_s", 0) or 0)
        display_title = _clean_title(_clean_caption(caption), filename)

        # Resume: pick up where user left off
        resume_pos = _cache.resume_get(msg_id)

        # Prepare stream session (cancel tasks & clear cache)
        prepare_new_stream_session(msg_id)

        # Launch VLC directly with the streaming URL — instant, no download
        # Pass filename so VLSub can identify the video for subtitle search
        asyncio.ensure_future(
            _launch_vlc_direct(
                msg_id, vlc, display_title, duration, resume_pos, filename
            )
        )

        return web.json_response({"ok": True, "resume_pos": resume_pos})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_vlc_link(req: web.Request):
    """GET /api/vlc_link/{msg_id}

    Returns the raw HTTP stream URL for a video so the user can paste it into
    VLC → Media → Open Network Stream (or any other player).  Also returns a
    vlc:// deep-link that opens VLC directly when clicked in a browser.
    """
    from urllib.parse import quote as _q
    import cache as _cache

    try:
        msg_id = int(req.match_info["msg_id"])
        meta = _cache._meta(msg_id)
        filename = meta.get("filename", "") or f"video_{msg_id}.mp4"
        safe_name = _q(filename, safe="")
        stream_url = f"http://127.0.0.1:{PORT}/vlc/{msg_id}/{safe_name}"
        # vlc:// deep-link: clicking this in a browser hands the URL to VLC
        vlc_link = f"vlc://{stream_url[len('http://'):]}"
        return web.json_response(
            {
                "ok": True,
                "stream_url": stream_url,
                "vlc_link": vlc_link,
                "filename": filename,
            }
        )
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_manage(req: web.Request):
    """GET /manage — lightweight shell; rows loaded via /api/manage_videos."""
    import cache as _cache

    if not _cache._cache_mem:
        await get_videos()

    # Only need album list for the datalist + filter dropdown — tiny query
    all_albums = fts_all_albums_distinct()

    def _safe(s):
        """Strip surrogate characters that break utf-8 encoding."""
        return s.encode("utf-8", errors="ignore").decode("utf-8")

    album_opts = "".join(
        f'<option value="{_safe(a)}">{_safe(a)}</option>' for a in all_albums
    )
    datalist_opts = "".join(f'<option value="{_safe(a)}">' for a in all_albums)

    from render import _head as _rhead

    page = (
        _rhead("Manage Albums \u2013 StreamVault")
        + """<style>
.mng-bar{position:sticky;top:60px;z-index:100;background:rgba(0,0,0,.96);border-bottom:1px solid var(--border);padding:8px max(16px,3vw);display:flex;align-items:center;gap:8px;min-height:52px;flex-wrap:wrap;overflow:visible;height:auto;}

@media(max-width:700px){.mng-bar{flex-wrap:wrap;overflow-x:visible;padding:10px max(16px,3vw);gap:6px;}.mng-bar-sep{display:none;}.mng-input{width:100%;flex:1 1 auto;}.mng-filter-search{width:100%;flex:1 1 120px;}.mng-filter-album{flex:1 1 100px;max-width:none;}.mng-count{margin-left:0;order:99;width:100%;font-size:.7rem;}.mng-sort-wrap{flex-wrap:wrap;}}
.mng-sel-count{font-size:.75rem;font-weight:700;color:var(--accent);white-space:nowrap;min-width:72px;flex-shrink:0;}
.mng-input{height:32px;padding:0 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:.78rem;outline:none;width:180px;flex-shrink:0;}
.mng-input:focus{border-color:var(--accent);}
.mng-btn{height:32px;padding:0 13px;border-radius:var(--radius-sm);font-size:.74rem;font-weight:700;cursor:pointer;border:none;transition:opacity .15s;white-space:nowrap;flex-shrink:0;}
.mng-btn-assign{background:var(--accent);color:#000;}.mng-btn-clear{background:transparent;border:1px solid var(--border);color:var(--text2);}.mng-btn-del{background:transparent;border:1px solid #553;color:#c66;}.mng-btn:disabled{opacity:.3;cursor:default;}
.mng-bar-sep{width:1px;height:24px;background:var(--border);margin:0 4px;flex-shrink:0;}
.mng-filter{height:32px;padding:0 10px;background:var(--surface);border:1px solid var(--border);border-radius:50px;color:var(--text);font-size:.76rem;outline:none;flex-shrink:0;}.mng-filter:focus{border-color:var(--accent);}.mng-filter-search{width:160px;}.mng-filter-album{max-width:140px;}
.mng-pill{height:28px;padding:0 11px;background:transparent;border:1px solid var(--border);border-radius:50px;color:var(--text2);font-size:.72rem;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0;}.mng-pill:hover{border-color:var(--accent);color:var(--accent);}
.mng-sort-wrap{display:flex;align-items:center;gap:5px;flex-shrink:0;}.mng-sort-lbl{font-size:.68rem;color:var(--text2);font-weight:600;white-space:nowrap;}
.mng-sort-btn{height:26px;padding:0 10px;background:transparent;border:1px solid var(--border);border-radius:50px;color:var(--text2);font-size:.68rem;font-weight:600;cursor:pointer;transition:border-color .15s,color .15s;display:flex;align-items:center;gap:3px;white-space:nowrap;flex-shrink:0;}.mng-sort-btn:hover{border-color:var(--accent);color:var(--accent);}.mng-sort-btn.active{border-color:var(--accent);color:var(--accent);background:var(--accent-lo);}.mng-sort-btn .arr{font-size:.6rem;opacity:.7;}
.mng-count{font-size:.72rem;color:var(--text2);white-space:nowrap;margin-left:auto;flex-shrink:0;}
#mngViewport{position:relative;padding:0 max(16px,3vw) 80px;user-select:none;-webkit-user-select:none;}
#mngSpacer{width:1px;}
.mng-card{position:absolute;left:max(16px,3vw);right:max(16px,3vw);display:flex;align-items:center;gap:12px;padding:7px 10px;border-radius:var(--radius-sm);border:1px solid transparent;cursor:pointer;box-sizing:border-box;transition:background .1s;}.mng-card:hover{background:var(--surface);}.mng-card.selected{background:var(--accent-lo);border-color:rgba(245,197,24,.25);}
.mng-cb{width:16px;height:16px;flex-shrink:0;cursor:pointer;accent-color:var(--accent);margin:0;}
.mng-thumb{width:80px;height:45px;flex-shrink:0;border-radius:6px;overflow:hidden;background:#111;border:1px solid #2a2a2a;}.mng-abstract{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:1.05rem;font-weight:900;letter-spacing:-.03em;user-select:none;position:relative;overflow:hidden;}.mng-abstract-letter{position:relative;z-index:1;mix-blend-mode:overlay;opacity:.92;}
.mng-info{flex:1;min-width:0;}.mng-title{font-size:.82rem;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}.mng-album{font-size:.72rem;color:var(--text2);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}.mng-size{font-size:.68rem;color:var(--text2);white-space:nowrap;flex-shrink:0;padding-right:4px;opacity:.7;}
@media(max-width:480px){.mng-thumb{width:60px;height:34px;}.mng-size{display:none;}}
.mng-empty{padding:48px max(16px,3vw);color:var(--text2);font-size:.88rem;}
.mng-loading{padding:40px max(16px,3vw);color:var(--text2);font-size:.84rem;display:flex;align-items:center;gap:10px;min-height:80px;}
.mng-spinner{width:18px;height:18px;border:1.5px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:_msp .7s linear infinite;flex-shrink:0;}
@keyframes _msp{to{transform:rotate(360deg);}}
.mng-toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:rgba(10,10,10,.96);border:1px solid var(--border-hi);border-radius:50px;padding:12px 22px;font-size:.84rem;font-weight:600;color:var(--text);opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;white-space:nowrap;z-index:9999;}.mng-toast.show{opacity:1;transform:translateX(-50%) translateY(0);}.mng-toast.ok{border-color:var(--accent);color:var(--accent);}.mng-toast.err{border-color:#f44;color:#f77;}
</style>"""
        + _nav(show_search=False)
        + """
<div class="mng-bar">
  <span class="mng-sel-count" id="selCount"></span>
  <input id="albumInput" class="mng-input" type="text" placeholder="Album name\u2026" list="albumSuggestions" autocomplete="off">
  <datalist id="albumSuggestions">"""
        + datalist_opts
        + """</datalist>
  <button class="mng-btn mng-btn-assign" id="btnAssign" onclick="doAssign()" disabled>Assign</button>
  <button class="mng-btn mng-btn-clear" id="btnClear" onclick="doClear()" disabled>Clear</button>
  <button class="mng-btn mng-btn-del" id="btnDelete" onclick="doDelete()" disabled>\U0001f5d1</button>
  <div class="mng-bar-sep"></div>
  <input id="mngSearch" class="mng-filter mng-filter-search" type="text" placeholder="Filter\u2026" oninput="schedFilter()">
  <select id="mngAlbumFilter" class="mng-filter mng-filter-album" onchange="schedFilter()">
    <option value="">All albums</option>
    """
        + album_opts
        + """
  </select>
  <button class="mng-pill" onclick="selAll()">All</button>
  <button class="mng-pill" onclick="selNone()">None</button>
  <div class="mng-sort-wrap">
    <span class="mng-sort-lbl" style="margin-left:2px">Sort:</span>
    <button class="mng-sort-btn active" id="sortDate" onclick="setSort('date')">Date <span class="arr" id="arrDate">\u25be</span></button>
    <button class="mng-sort-btn" id="sortTitle" onclick="setSort('title')">Title <span class="arr" id="arrTitle"></span></button>
    <button class="mng-sort-btn" id="sortSize" onclick="setSort('size')">Size <span class="arr" id="arrSize"></span></button>
  </div>
  <span class="mng-count" id="mngCount">\u2026</span>
</div>
<div id="mngViewport">
  <div id="mngSpacer"></div>
</div>
<div id="mngLoading" class="mng-loading"><div class="mng-spinner"></div>Loading…</div>
<div class="mng-toast" id="mngToast"></div>
<script>
/* ── Custom alert card ───────────────────────────────────────────────────── */
(function(){
  var _overlay=null;
  window._svAlert=function(opts){
    if(_overlay){_overlay.remove();_overlay=null;}
    _overlay=document.createElement('div');
    _overlay.style.cssText='position:fixed;inset:0;z-index:9900;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px);';
    var card=document.createElement('div');
    card.style.cssText='background:#0d0d0d;border:1px solid #252525;border-radius:16px;padding:28px 28px 22px;max-width:380px;width:100%;box-shadow:0 24px 64px rgba(0,0,0,.8);';
    var titleEl=document.createElement('div');
    titleEl.style.cssText='font-size:1rem;font-weight:800;color:#e8e8e8;margin-bottom:10px;';
    titleEl.textContent=opts.title||'Confirm';
    var bodyEl=document.createElement('div');
    bodyEl.style.cssText='font-size:.82rem;color:#606060;line-height:1.6;margin-bottom:22px;';
    bodyEl.textContent=opts.body||'';
    var row=document.createElement('div');
    row.style.cssText='display:flex;gap:10px;justify-content:flex-end;';
    if(opts.cancelText!==false){
      var cancelBtn=document.createElement('button');
      cancelBtn.textContent=opts.cancelText||'Cancel';
      cancelBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:1px solid #1c1c1c;background:transparent;color:#606060;font-size:.8rem;font-weight:600;cursor:pointer;';
      cancelBtn.onclick=function(){_overlay.remove();_overlay=null;};
      row.appendChild(cancelBtn);
    }
    var confirmBtn=document.createElement('button');
    confirmBtn.textContent=opts.confirmText||'OK';
    confirmBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:none;background:'+(opts.danger?'#c0392b':'#f5c518')+';color:'+(opts.danger?'#fff':'#000')+';font-size:.8rem;font-weight:700;cursor:pointer;';
    confirmBtn.onclick=function(){
      _overlay.remove();_overlay=null;
      if(opts.onConfirm)opts.onConfirm();
    };
    row.appendChild(confirmBtn);
    card.appendChild(titleEl);card.appendChild(bodyEl);card.appendChild(row);
    _overlay.appendChild(card);
    document.body.appendChild(_overlay);
    _overlay.addEventListener('click',function(e){if(e.target===_overlay){_overlay.remove();_overlay=null;}});
    confirmBtn.focus();
  };
})();
</script>
<script>
(function(){
// ── state ─────────────────────────────────────────────────────────────────────
var _PAGE=200,_ITEM_H=61,_OVERSCAN=8;
var _data=[],_total=0,_offset=0,_fetching=false,_done=false;
var _sortKey='date',_sortDir='desc';
var _search='',_album='';
var _sel=new Set();
// card pool for DOM recycling
var _pool=[],_visible=[];
var _vp=document.getElementById('mngViewport');
var _spacer=document.getElementById('mngSpacer');
var _loading=document.getElementById('mngLoading');

// ── fetch ─────────────────────────────────────────────────────────────────────
function _buildUrl(){
  var u='/api/manage_videos?limit='+_PAGE+'&offset='+_offset
    +'&sort='+_sortKey+'&dir='+_sortDir;
  if(_search) u+='&q='+encodeURIComponent(_search);
  if(_album)  u+='&album='+encodeURIComponent(_album);
  return u;
}
function _fetchNext(){
  if(_fetching||_done)return;
  _fetching=true;
  _loading.style.display='flex';
  fetch(_buildUrl())
    .then(function(r){return r.json();})
    .then(function(j){
      _fetching=false;
      var rows=j.rows||[];
      _total=j.total||0;
      _data=_data.concat(rows);
      _offset+=rows.length;
      if(rows.length<_PAGE)_done=true;
      if(!_data.length)_done=true;
      _loading.style.display=_done?'none':'flex';
      document.getElementById('mngCount').textContent=_total+' video'+(_total!==1?'s':'');
      _render();
    })
    .catch(function(e){
      _fetching=false;
      _loading.style.display='none';
      mngToast('Load error: '+e,'err');
    });
}

// ── virtual scroll render ─────────────────────────────────────────────────────
var _vpTop=0;
function _cacheVpTop(){_vpTop=_vp.getBoundingClientRect().top+window.scrollY;}
function _abstractThumb(){
  // Uniform minimal yellow/black geometric card — same for all entries
  return '<div style="width:100%;height:100%;background:#0a0a0a;display:flex;align-items:center;justify-content:center;position:relative;overflow:hidden;">'
    +'<svg width="80" height="45" viewBox="0 0 80 45" xmlns="http://www.w3.org/2000/svg" style="position:absolute;inset:0;">'
    +'<rect width="80" height="45" fill="#0a0a0a"/>'
    +'<rect x="0" y="19" width="80" height="7" fill="#f5c518" opacity=".08"/>'
    +'<rect x="34" y="0" width="1" height="45" fill="#f5c518" opacity=".1"/>'
    +'<rect x="45" y="0" width="1" height="45" fill="#f5c518" opacity=".06"/>'
    +'<circle cx="40" cy="22.5" r="10" fill="none" stroke="#f5c518" stroke-width=".75" opacity=".18"/>'
    +'</svg>'
    +'<svg width="14" height="14" viewBox="0 0 24 24" fill="#f5c518" style="position:relative;z-index:1;opacity:.35;"><path d="M5 3l14 9-14 9V3z"/></svg>'
    +'</div>';
}
function _getCard(){
  if(_pool.length)return _pool.pop();
  var d=document.createElement('div');
  d.className='mng-card';
  d.innerHTML='<input type="checkbox" class="mng-cb">'
    +'<div class="mng-thumb"></div>'
    +'<div class="mng-info"><div class="mng-title"></div><div class="mng-album"></div></div>'
    +'<span class="mng-size"></span>';
  var cb=d.querySelector('.mng-cb');
  d.addEventListener('click',function(e){if(e.target===cb)return;cb.checked=!cb.checked;_toggle(d,cb);});
  cb.addEventListener('change',function(){_toggle(d,cb);});
  _vp.appendChild(d);
  return d;
}
function _toggle(card,cb){
  var id=card.dataset.id;
  if(cb.checked){_sel.add(id);card.classList.add('selected');}
  else{_sel.delete(id);card.classList.remove('selected');}
  _sync();
}
function _sync(){
  var n=_sel.size;
  document.getElementById('selCount').textContent=n?n+' selected':'';
  var dis=n===0;
  document.getElementById('btnAssign').disabled=dis;
  document.getElementById('btnClear').disabled=dis;
  document.getElementById('btnDelete').disabled=dis;
}
function _render(){
  var totalH=_data.length*_ITEM_H;
  _spacer.style.height=totalH+'px';
  // Use cached vpTop — live getBoundingClientRect during scroll causes jitter
  var relScroll=window.scrollY-_vpTop;
  if(relScroll<0)relScroll=0;
  var vpH=window.innerHeight;
  var firstVisible=Math.max(0,Math.floor(relScroll/_ITEM_H)-_OVERSCAN);
  var lastVisible=Math.min(_data.length-1,Math.ceil((relScroll+vpH)/_ITEM_H)+_OVERSCAN);

  // recycle cards not in view
  var keep=[];
  _visible.forEach(function(c){
    var idx=parseInt(c.dataset.rowIdx,10);
    if(idx<firstVisible||idx>lastVisible){
      c.style.display='none';
      _pool.push(c);
    }else{keep.push(c);}
  });
  _visible=keep;
  var rendered=new Set(_visible.map(function(c){return parseInt(c.dataset.rowIdx,10);}));

  for(var i=firstVisible;i<=lastVisible&&i<_data.length;i++){
    if(rendered.has(i))continue;
    var v=_data[i];
    var card=_getCard();
    card.dataset.rowIdx=i;
    card.dataset.id=String(v.message_id);
    card.style.top=(i*_ITEM_H)+'px';
    card.style.display='flex';
    // thumb — always abstract (no TG thumbnail downloads)
    var thumbDiv=card.querySelector('.mng-thumb');
    thumbDiv.innerHTML=_abstractThumb();
    // title
    card.querySelector('.mng-title').textContent=v.title||String(v.message_id);
    // album
    var pin=v.album_pinned?'\ud83d\udccc ':''
    card.querySelector('.mng-album').textContent=pin+(v.album||'');
    card.querySelector('.mng-album').dataset.vid=String(v.message_id);
    // size
    card.querySelector('.mng-size').textContent=v.size||'';
    // selection state
    var cb=card.querySelector('.mng-cb');
    cb.value=String(v.message_id);
    cb.checked=_sel.has(String(v.message_id));
    if(cb.checked)card.classList.add('selected');
    else card.classList.remove('selected');
    _visible.push(card);
  }

  // Trigger next page load if near bottom
  if(!_done&&_data.length>0){
    var triggerIdx=Math.max(0,_data.length-_PAGE/2);
    if(lastVisible>=triggerIdx)_fetchNext();
  }
  if(_done&&!_data.length){
    _loading.innerHTML='<div class="mng-empty">No videos in cache \u2014 hit <strong>Refresh</strong> to fetch from Telegram.</div>';
    _loading.style.display='flex';
  }
}

// ── sort / filter ─────────────────────────────────────────────────────────────
window.setSort=function(key){
  if(_sortKey===key){_sortDir=_sortDir==='asc'?'desc':'asc';}
  else{_sortKey=key;_sortDir=key==='title'?'asc':'desc';}
  ['date','title','size'].forEach(function(k){
    var btn=document.getElementById('sort'+k.charAt(0).toUpperCase()+k.slice(1));
    var arr=document.getElementById('arr'+k.charAt(0).toUpperCase()+k.slice(1));
    if(k===_sortKey){btn.className='mng-sort-btn active';arr.textContent=_sortDir==='asc'?'\u25b4':'\u25be';}
    else{btn.className='mng-sort-btn';arr.textContent='';}
  });
  _reset();
};
var _filterTimer=null;
window.schedFilter=function(){clearTimeout(_filterTimer);_filterTimer=setTimeout(_applyFilter,280);};
function _applyFilter(){
  _search=document.getElementById('mngSearch').value.trim();
  _album=document.getElementById('mngAlbumFilter').value;
  _reset();
}
function _reset(){
  // recycle all visible cards
  _visible.forEach(function(c){c.style.display='none';_pool.push(c);});
  _visible=[];
  _data=[];_offset=0;_done=false;_fetching=false;
  _spacer.style.height='0';
  _loading.style.display='flex';
  _loading.innerHTML='<div class="mng-spinner"></div>Loading\u2026';
  _fetchNext();
}

// ── selAll / selNone (visible-page-aware) ─────────────────────────────────────
window.selAll=function(){
  // Select all loaded IDs from _data, not just visible DOM cards
  _data.forEach(function(v){_sel.add(String(v.message_id));});
  _visible.forEach(function(c){
    var cb=c.querySelector('.mng-cb');
    cb.checked=true;c.classList.add('selected');
  });_sync();
};
window.selNone=function(){
  _sel.clear();
  _visible.forEach(function(c){
    var cb=c.querySelector('.mng-cb');
    cb.checked=false;c.classList.remove('selected');
  });_sync();
};

// ── actions ───────────────────────────────────────────────────────────────────
window.doAssign=async function(){
  var album=document.getElementById('albumInput').value.trim();
  if(!album){mngToast('Enter or pick an album name','err');return;}
  if(!_sel.size)return;
  var ids=Array.from(_sel).map(Number);
  var r=await fetch('/api/album_assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg_ids:ids,album:album})});
  var j=await r.json();
  if(j.ok){
    var pin='\ud83d\udccc '+album;
    var idSet=new Set(ids.map(String));
    // Update _data so virtual scroll re-renders with correct album name
    _data.forEach(function(v){if(idSet.has(String(v.message_id))){v.album=j.album;v.album_pinned=true;}});
    _visible.forEach(function(c){
      if(idSet.has(c.dataset.id))c.querySelector('.mng-album').textContent=pin;
    });
    mngToast('\u2713 '+j.assigned+' video'+(j.assigned!==1?'s':'')+' \u2192 '+album,'ok');
    selNone();
  }else{mngToast('Error: '+j.error,'err');}
};
window.doClear=async function(){
  if(!_sel.size)return;
  var ids=Array.from(_sel).map(Number);
  var r=await fetch('/api/album_assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg_ids:ids,album:''})});
  var j=await r.json();
  if(j.ok){mngToast('\u2713 Cleared override for '+j.assigned+' video'+(j.assigned!==1?'s':''),'ok');selNone();setTimeout(function(){_reset();},600);}
  else{mngToast('Error: '+j.error,'err');}
};
window.doDelete=function(){
  if(!_sel.size)return;
  var ids=Array.from(_sel).map(Number);
  window._svAlert({
    title: 'Remove from Cache?',
    body: 'Remove '+ids.length+' video'+(ids.length!==1?'s':'')+' from the local cache? Does NOT delete from Telegram \u2014 hit Refresh to restore.',
    confirmText: 'Remove',
    cancelText: 'Cancel',
    danger: true,
    onConfirm: async function() {
      var r=await fetch('/api/delete_videos',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({msg_ids:ids})});
      var j=await r.json();
      if(j.ok){
        var delSet=new Set(ids.map(String));
        _data=_data.filter(function(v){return !delSet.has(String(v.message_id));});
        _total=Math.max(0,_total-j.removed);
        _sel.clear();_sync();
        // full re-render to reposition
        _visible.forEach(function(c){c.style.display='none';_pool.push(c);});
        _visible=[];
        document.getElementById('mngCount').textContent=_total+' video'+(_total!==1?'s':'');
        _render();
        mngToast('\ud83d\uddd1 Removed '+j.removed+' video'+(j.removed!==1?'s':'')+' from cache','ok');
      }else{mngToast('Error: '+j.error,'err');}
    }
  });
};
window.doRefresh=function(){
  window.location.reload(true);
};
window.doFetch=function(){
  var btn=document.getElementById('refreshBtn');
  if(btn){btn.disabled=true;btn.textContent='Fetching\u2026';}
  fetch('/api/fetch',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
    if(d.ok)location.reload();
    else{if(btn){btn.disabled=false;btn.textContent='Fetch';}}
  }).catch(function(){if(btn){btn.disabled=false;btn.textContent='Fetch';}});
};
var _mT=null;
window.mngToast=function(msg,cls){
  var t=document.getElementById('mngToast');
  t.textContent=msg;t.className='mng-toast show '+(cls||'');
  clearTimeout(_mT);_mT=setTimeout(function(){t.className='mng-toast';},3000);
};

// ── scroll listener ───────────────────────────────────────────────────────────
var _rafPending=false;
var _lassoActive=false; // set by lasso block to suppress scroll-render during drag
window.addEventListener('resize',function(){_cacheVpTop();if(_lassoActive)return;if(_rafPending)return;_rafPending=true;requestAnimationFrame(function(){_rafPending=false;_render();});},{passive:true});
window.addEventListener('scroll',function(){
  if(_lassoActive)return;
  if(_rafPending)return;
  _rafPending=true;
  requestAnimationFrame(function(){_rafPending=false;_render();});
},{passive:true});

// ── lasso drag-select ─────────────────────────────────────────────────────────
// _sy = pageY of drag anchor (fixed for entire drag).
// _cy = clientY of current mouse (viewport coord, scroll-independent).
// Box drawn in fixed/viewport coords. Hit-test uses pageY so rows that
// scroll out of view stay selected — the lasso range only grows.
(function(){
  var EDGE=120,SPMAX=120;
  var _active=false,_anchorPageY=0,_cy=0,_raf=null,_box=null,_prevSel=null;

  function _ensureBox(){
    if(_box)return;
    _box=document.createElement('div');
    _box.style.cssText='position:fixed;pointer-events:none;z-index:8000;'
      +'border:1.5px solid var(--accent);background:var(--accent-lo);'
      +'box-sizing:border-box;opacity:0.75;left:0;right:0;top:0;height:0;';
    document.body.appendChild(_box);
  }

  function _frame(){
    if(!_active)return;

    // auto-scroll — linear ramp, SPMAX=18 ≈ 5-6 rows/sec at 60fps
    var delta=0;
    if(_cy<EDGE)
      delta=-Math.round(SPMAX*((EDGE-_cy)/EDGE));
    else if(_cy>window.innerHeight-EDGE)
      delta=Math.round(SPMAX*((_cy-(window.innerHeight-EDGE))/EDGE));
    if(delta)window.scrollBy(0,delta);

    // lasso box in viewport coords
    var curPageY=_cy+window.scrollY;
    var pageTop=Math.min(_anchorPageY,curPageY);
    var pageBot=Math.max(_anchorPageY,curPageY);
    // convert page-Y extents to viewport for drawing
    var bt=pageTop-window.scrollY;
    var bb=pageBot-window.scrollY;
    _box.style.top=bt+'px';
    _box.style.height=Math.max(0,bb-bt)+'px';

    // hit-test in page coords → row indices
    var vpTop=_vp.getBoundingClientRect().top+window.scrollY;
    var fi=Math.max(0,Math.floor((pageTop-vpTop)/_ITEM_H));
    var li=Math.min(_data.length-1,Math.floor((pageBot-vpTop)/_ITEM_H));

    _sel.clear();
    _prevSel.forEach(function(id){_sel.add(id);});
    for(var i=fi;i<=li;i++){
      if(_data[i])_sel.add(String(_data[i].message_id));
    }
    _visible.forEach(function(c){
      var on=_sel.has(c.dataset.id);
      var cb=c.querySelector('.mng-cb');
      cb.checked=on;
      if(on)c.classList.add('selected'); else c.classList.remove('selected');
    });
    _sync();
    _raf=requestAnimationFrame(_frame);
  }

  function _start(e){
    if(e.button!==0)return;
    if(e.target.closest('.mng-card'))return;
    e.preventDefault();
    _ensureBox();
    _active=true;
    _lassoActive=true;
    _anchorPageY=e.pageY;
    _cy=e.clientY;
    _prevSel=new Set(_sel);
    document.body.style.userSelect='none';
    document.body.style.webkitUserSelect='none';
    cancelAnimationFrame(_raf);
    _raf=requestAnimationFrame(_frame);
    document.addEventListener('mousemove',_move,{passive:true});
    document.addEventListener('mouseup',_end,{once:true});
  }

  function _move(e){_cy=e.clientY;}

  function _end(){
    _active=false;
    _lassoActive=false;
    cancelAnimationFrame(_raf);
    document.removeEventListener('mousemove',_move);
    document.body.style.userSelect='';
    document.body.style.webkitUserSelect='';
    if(_box){_box.style.top='-9999px';_box.style.height='0';}
    _render();
  }

  _vp.addEventListener('mousedown',_start);
  document.addEventListener('selectstart',function(e){if(_active)e.preventDefault();});
})();

// ── boot ──────────────────────────────────────────────────────────────────────
// Cache vpTop after DOM settles — nav/fonts may shift layout
requestAnimationFrame(function(){_cacheVpTop();setTimeout(_cacheVpTop,300);});
_fetchNext();
})();
</script>"""
        + _PAGE_CLOSE
        + """</body></html>"""
    )
    page = page.encode("utf-8", errors="ignore").decode("utf-8")
    return web.Response(content_type="text/html", text=page)


# ── MANAGE VIDEOS JSON API ────────────────────────────────────────────────────
async def route_api_manage_videos(req: web.Request):
    """GET /api/manage_videos — paginated, filtered, sorted video list for /manage."""
    import cache as _cache

    if not _cache._cache_mem:
        await get_videos()

    q = req.rel_url.query
    offset = max(0, int(q.get("offset", 0)))
    limit = min(500, max(1, int(q.get("limit", 200))))
    sort_key = q.get("sort", "date")
    sort_dir = q.get("dir", "desc")
    search = q.get("q", "").strip()
    album_filter = q.get("album", "").strip()

    loop = asyncio.get_event_loop()
    rows, total = await loop.run_in_executor(
        None,
        lambda: fts_manage_videos_page(
            offset=offset,
            limit=limit,
            search=search,
            album_filter=album_filter,
            sort_key=sort_key,
            sort_dir=sort_dir,
        ),
    )

    # FTS DB empty but cache has data → DB is stale/wiped, resync then retry
    if total == 0 and _cache._cache_mem:
        await loop.run_in_executor(None, lambda: _cache._fts_sync(_cache._cache_mem))
        rows, total = await loop.run_in_executor(
            None,
            lambda: fts_manage_videos_page(
                offset=offset,
                limit=limit,
                search=search,
                album_filter=album_filter,
                sort_key=sort_key,
                sort_dir=sort_dir,
            ),
        )

    return web.json_response({"rows": rows, "total": total, "offset": offset})


# ── ALBUM POSTER / META EDITOR ───────────────────────────────────────────────
async def route_manage_albums(req: web.Request):
    """GET /manage/albums — per-album poster & metadata editor."""
    import cache as _cache
    import hashlib as _hl

    await get_videos()
    if _cache._albums_dirty or not _cache._albums_index:
        _cache._rebuild_albums_index()
    # If still empty, _cache_mem may not have loaded yet — force a full fetch
    if not _cache._albums_index:
        await get_videos(force=True)
        _cache._rebuild_albums_index()

    albums = _cache._albums_index

    # Populate _poster_mem only for albums already in cache — don't block on downloads
    uncached = [a for a in albums if a["name"] not in _cache._poster_mem]
    if uncached:
        meta_map = await _fetch_all_meta([a["name"] for a in uncached])
        loop = asyncio.get_event_loop()

        def _read_lqip(slug):
            p = os.path.join(POSTERS_DIR, f"{slug}.lqip")
            try:
                with open(p) as _f:
                    return _f.read().strip()
            except Exception:
                return ""

        from render import _POSTER_SEM

        async def _resolve(a):
            m = meta_map.get(a["name"], {})
            if not isinstance(m, dict):
                m = {}
            thumb = a.get("thumb_url") or ""
            remote = m.get("poster") or (thumb if thumb.startswith("http") else "")
            if remote:
                async with _POSTER_SEM:
                    poster_url = await loop.run_in_executor(
                        None, _cache_poster_sync, a["name"], remote
                    )
                slug = _hl.md5(a["name"].encode()).hexdigest()
                lqip = await loop.run_in_executor(None, _read_lqip, slug)
                poster_sm_url = (
                    (poster_url + "?sm=1")
                    if poster_url.startswith("/poster/")
                    else poster_url
                )
            else:
                poster_url = poster_sm_url = lqip = ""
            _cache._poster_mem[a["name"]] = {
                "poster_url": poster_url,
                "poster_sm_url": poster_sm_url,
                "lqip": lqip,
                "meta": m,
            }

        # Fire poster fetches in background — don't await, serve page immediately
        asyncio.ensure_future(asyncio.gather(*[_resolve(a) for a in uncached]))

    def _safe(s):
        """Strip surrogate characters that break utf-8 encoding."""
        if not s:
            return s
        return s.encode("utf-8", errors="ignore").decode("utf-8")

    import json as _json

    album_data = []
    for a in albums:
        pm = _cache._poster_mem.get(a["name"], {})
        m = pm.get("meta", {}) or {}
        album_data.append(
            {
                "name": _safe(a["name"]),
                "type": (m.get("type") or "").lower(),
                "count": len(a["videos"]),
                "poster_src": pm.get("poster_sm_url") or pm.get("poster_url") or "",
                "poster_url": _safe(m.get("poster") or ""),
                "year": _safe(m.get("year") or ""),
                "rating": _safe(m.get("rating") or ""),
                "plot": _safe(m.get("plot") or ""),
                "mtype": _safe(m.get("type") or ""),
            }
        )

    album_data_json = _json.dumps(album_data, ensure_ascii=False)

    from render import _head as _rhead

    page = (
        _rhead("Album Editor \u2013 StreamVault")
        + """<style>
.albed-wrap{padding:20px max(16px,3vw) 80px;max-width:900px;margin:0 auto;}
.albed-topbar{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap;}
.albed-heading{font-size:1.15rem;font-weight:800;margin:0;}
.albed-filter-wrap{position:relative;flex:1;max-width:320px;}
.albed-filter-wrap svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);pointer-events:none;stroke:var(--text2);}
.albed-filter{height:36px;padding:0 14px 0 36px;background:var(--surface);border:1px solid var(--border);border-radius:50px;color:var(--text);font-size:.82rem;outline:none;width:100%;box-sizing:border-box;}
.albed-filter:focus{border-color:var(--accent);}
.albed-count-label{font-size:.75rem;color:var(--text2);white-space:nowrap;}
.albed-section-heading{font-size:.88rem;font-weight:800;color:var(--accent);letter-spacing:.06em;text-transform:uppercase;padding:18px 0 8px;border-top:1px solid var(--border);margin-top:8px;}
.albed-section-heading:first-child{border-top:none;margin-top:0;}
.albed-section-count{font-size:.72rem;font-weight:600;color:var(--text2);margin-left:6px;text-transform:none;letter-spacing:0;}
.albed-row{display:flex;gap:16px;align-items:flex-start;padding:18px 0;border-bottom:1px solid var(--border);}
.albed-poster{flex-shrink:0;width:80px;border-radius:8px;overflow:hidden;border:1px solid var(--border);background:var(--surface);min-height:110px;display:flex;align-items:center;justify-content:center;}
.albed-poster img{width:100%;display:block;}
.albed-no-poster{width:80px;height:110px;background:var(--card);}
.albed-body{flex:1;min-width:0;}
.albed-title{font-size:.9rem;font-weight:700;margin-bottom:10px;color:var(--text);}
.albed-count{font-size:.72rem;font-weight:500;color:var(--text2);margin-left:6px;}
.albed-fields{display:grid;grid-template-columns:1fr auto auto auto;gap:8px;align-items:start;}
.albed-name-label{grid-column:1/-1;}
.albed-plot-label{grid-column:1/-1;}
label{font-size:.7rem;font-weight:600;color:var(--text2);display:flex;flex-direction:column;gap:3px;text-transform:uppercase;letter-spacing:.04em;}
.albed-input{height:32px;padding:0 10px;background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);color:var(--text);font-size:.8rem;outline:none;font-family:inherit;width:100%;box-sizing:border-box;}
.albed-input:focus{border-color:var(--accent);}
.albed-sm{width:100px;}
.albed-textarea{height:auto;min-height:56px;resize:vertical;padding:6px 10px;line-height:1.5;}
.albed-actions{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;}
.albed-save-btn{height:32px;padding:0 14px;background:var(--accent);color:#000;border:none;border-radius:var(--radius-sm);font-size:.76rem;font-weight:700;cursor:pointer;font-family:inherit;}
.albed-save-btn:hover{opacity:.85;}
.albed-reset-btn{height:32px;padding:0 14px;background:transparent;border:1px solid var(--border);color:var(--text2);border-radius:var(--radius-sm);font-size:.76rem;font-weight:600;cursor:pointer;font-family:inherit;}
.albed-reset-btn:hover{border-color:var(--accent);color:var(--accent);}
.albed-del-btn{height:32px;padding:0 14px;background:transparent;border:1px solid var(--border) ;color:#c66;border-radius:var(--radius-sm);font-size:.76rem;font-weight:600;cursor:pointer;font-family:inherit;}
.albed-del-btn:hover{border-color:#f66;color:#f66;}
.albed-status{font-size:.73rem;margin-top:6px;min-height:18px;}
.albed-status.ok{color:#4c4;}
.albed-status.err{color:#f66;}
.albed-empty{color:var(--text2);padding:24px 0;font-size:.85rem;}
.search-wrap{display:none!important;}
@media(max-width:560px){
  .albed-fields{grid-template-columns:1fr 1fr;}
  .albed-row{flex-direction:column;}
  .albed-poster{width:100%;min-height:0;}
  .albed-no-poster{height:60px;width:100%;}
  .albed-filter-wrap{max-width:100%;}
}
</style>"""
        + _nav()
        + """
<div class="albed-wrap">
  <div class="albed-topbar">
    <div class="albed-heading">Album Editor</div>
    <div class="albed-filter-wrap">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input class="albed-filter" id="albedQ" type="text" placeholder="Filter albums\u2026" autocomplete="off">
    </div>
    <span class="albed-count-label" id="albedCount"></span>
  </div>
  <div id="albedList"></div>
</div>
<script>
/* ── Custom alert card ───────────────────────────────────────────────────── */
(function(){
  var _overlay=null;
  window._svAlert=function(opts){
    if(_overlay){_overlay.remove();_overlay=null;}
    _overlay=document.createElement('div');
    _overlay.style.cssText='position:fixed;inset:0;z-index:9900;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px);';
    var card=document.createElement('div');
    card.style.cssText='background:#0d0d0d;border:1px solid #252525;border-radius:16px;padding:28px 28px 22px;max-width:380px;width:100%;box-shadow:0 24px 64px rgba(0,0,0,.8);';
    var titleEl=document.createElement('div');
    titleEl.style.cssText='font-size:1rem;font-weight:800;color:#e8e8e8;margin-bottom:10px;';
    titleEl.textContent=opts.title||'Confirm';
    var bodyEl=document.createElement('div');
    bodyEl.style.cssText='font-size:.82rem;color:#606060;line-height:1.6;margin-bottom:22px;';
    bodyEl.textContent=opts.body||'';
    var row=document.createElement('div');
    row.style.cssText='display:flex;gap:10px;justify-content:flex-end;';
    if(opts.cancelText!==false){
      var cancelBtn=document.createElement('button');
      cancelBtn.textContent=opts.cancelText||'Cancel';
      cancelBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:1px solid #1c1c1c;background:transparent;color:#606060;font-size:.8rem;font-weight:600;cursor:pointer;';
      cancelBtn.onclick=function(){_overlay.remove();_overlay=null;};
      row.appendChild(cancelBtn);
    }
    var confirmBtn=document.createElement('button');
    confirmBtn.textContent=opts.confirmText||'OK';
    confirmBtn.style.cssText='height:38px;padding:0 18px;border-radius:8px;border:none;background:'+(opts.danger?'#c0392b':'#f5c518')+';color:'+(opts.danger?'#fff':'#000')+';font-size:.8rem;font-weight:700;cursor:pointer;';
    confirmBtn.onclick=function(){
      _overlay.remove();_overlay=null;
      if(opts.onConfirm)opts.onConfirm();
    };
    row.appendChild(confirmBtn);
    card.appendChild(titleEl);card.appendChild(bodyEl);card.appendChild(row);
    _overlay.appendChild(card);
    document.body.appendChild(_overlay);
    _overlay.addEventListener('click',function(e){if(e.target===_overlay){_overlay.remove();_overlay=null;}});
    confirmBtn.focus();
  };
})();
</script>
<script>
(function() {
  var _data = """
        + album_data_json
        + """;
  var _q = '';
  var _list = document.getElementById('albedList');
  var _countEl = document.getElementById('albedCount');

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _buildRow(d) {
    var posterHTML = d.poster_src ? '' : '<div class="albed-no-poster"></div>';
    var selMovie  = d.mtype === 'Movie'  ? ' selected' : '';
    var selSeries = d.mtype === 'Series' ? ' selected' : '';
    var countStr  = d.count + ' video' + (d.count !== 1 ? 's' : '');
    var row = document.createElement('div');
    row.className = 'albed-row';
    row.dataset.name = d.name;
    row.dataset.type = d.type;
    row.innerHTML =
      '<div class="albed-poster">' + posterHTML + '</div>' +
      '<div class="albed-body">' +
        '<div class="albed-title">' + _esc(d.name) + ' <span class="albed-count">' + countStr + '</span></div>' +
        '<div class="albed-fields">' +
          '<label class="albed-name-label">Album Name' +
            '<input class="albed-input" name="album_name" value="' + _esc(d.name) + '" placeholder="Album name">' +
          '</label>' +
          '<label>Poster URL' +
            '<input class="albed-input" name="poster" value="' + _esc(d.poster_url) + '" placeholder="https://\u2026/poster.jpg">' +
          '</label>' +
          '<label>Year<input class="albed-input albed-sm" name="year" value="' + _esc(d.year) + '" placeholder="2023"></label>' +
          '<label>Rating<input class="albed-input albed-sm" name="rating" value="' + _esc(d.rating) + '" placeholder="7.5"></label>' +
          '<label>Type<select class="albed-input albed-sm" name="type">' +
            '<option value="">Auto</option>' +
            '<option value="Movie"' + selMovie + '>Movie</option>' +
            '<option value="Series"' + selSeries + '>Series</option>' +
          '</select></label>' +
          '<label class="albed-plot-label">Plot' +
            '<textarea class="albed-input albed-textarea" name="plot">' + _esc(d.plot) + '</textarea>' +
          '</label>' +
        '</div>' +
        '<div class="albed-actions">' +
          '<button class="albed-save-btn" onclick="saveAlbum(this)">Save &amp; Refresh Poster</button>' +
          '<button class="albed-reset-btn" onclick="resetAlbum(this)">Re-fetch Metadata</button>' +
          '<button class="albed-del-btn" onclick="deleteAlbum(this)">Delete Album</button>' +
        '</div>' +
        '<div class="albed-status"></div>' +
      '</div>';
    if (d.poster_src) {
      var img = document.createElement('img');
      img.src = d.poster_src;
      img.loading = 'lazy';
      img.onerror = function() { this.style.display = 'none'; };
      row.querySelector('.albed-poster').appendChild(img);
    }
    return row;
  }

  function _section(heading, items) {
    var wrap = document.createDocumentFragment();
    var h = document.createElement('div');
    h.className = 'albed-section-heading';
    h.innerHTML = heading + ' <span class="albed-section-count">' + items.length + '</span>';
    wrap.appendChild(h);
    items.forEach(function(d) { wrap.appendChild(_buildRow(d)); });
    return wrap;
  }

  function render() {
    var q = _q.toLowerCase();
    var filtered = q
      ? _data.filter(function(d) { return d.name.toLowerCase().indexOf(q) !== -1; })
      : _data.slice();

    _list.innerHTML = '';

    if (!filtered.length) {
      var empty = document.createElement('div');
      empty.className = 'albed-empty';
      empty.textContent = _data.length ? 'No albums match \u201c' + _q + '\u201d.' : 'No albums found. Hit Refresh on the home page first.';
      _list.appendChild(empty);
      _countEl.textContent = '';
      return;
    }

    _countEl.textContent = filtered.length + ' album' + (filtered.length !== 1 ? 's' : '');

    var series = filtered.filter(function(d) { return d.type === 'series'; });
    var movies = filtered.filter(function(d) { return d.type === 'movie'; });
    var other  = filtered.filter(function(d) { return d.type !== 'series' && d.type !== 'movie'; });

    if (series.length) _list.appendChild(_section('Series', series));
    if (movies.length) _list.appendChild(_section('Movies', movies));
    if (other.length)  _list.appendChild(_section('Uncategorised', other));
  }

  var _timer = null;
  document.getElementById('albedQ').addEventListener('input', function() {
    _q = this.value.trim();
    clearTimeout(_timer);
    _timer = setTimeout(render, 120);
  });

  render();
})();

function doHardRefresh() {
  if(window._svNavBar) window._svNavBar.show('Refreshing\u2026');
  setTimeout(function(){ window.location.reload(true); }, 60);
}

async function saveAlbum(btn) {
  var row = btn.closest('.albed-row');
  var oldName = row.dataset.name;
  var newName = (row.querySelector('[name=album_name]').value || '').trim();
  var status = row.querySelector('.albed-status');
  btn.disabled = true; btn.textContent = 'Saving\u2026';
  status.className = 'albed-status'; status.textContent = '';
  try {
    if (newName && newName !== oldName) {
      var rr = await fetch('/api/album_rename', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_name:oldName,new_name:newName})});
      var rj = await rr.json();
      if (!rj.ok) { status.className='albed-status err'; status.textContent='\u2717 Rename failed: '+(rj.error||''); btn.disabled=false; btn.textContent='Save & Refresh Poster'; return; }
      row.dataset.name = newName;
      row.querySelector('.albed-title').firstChild.textContent = newName + ' ';
      oldName = newName;
    }
    var payload = {
      album:  oldName,
      poster: row.querySelector('[name=poster]').value.trim(),
      year:   row.querySelector('[name=year]').value.trim(),
      rating: row.querySelector('[name=rating]').value.trim(),
      type:   row.querySelector('[name=type]').value,
      plot:   row.querySelector('[name=plot]').value.trim(),
    };
    var r = await fetch('/api/update_album_meta', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var j = await r.json();
    if (j.ok) {
      status.className = 'albed-status ok'; status.textContent = '\u2713 Saved. Poster refreshed.';
      var img = row.querySelector('.albed-poster img');
      if (j.poster_url) {
        if (img) { img.src = j.poster_url + '?sm=1&t=' + Date.now(); img.style.display = ''; }
        else { row.querySelector('.albed-poster').innerHTML = '<img src="' + j.poster_url + '?sm=1&t=' + Date.now() + '" style="width:100%;display:block;">'; }
      }
    } else { status.className = 'albed-status err'; status.textContent = '\u2717 ' + j.error; }
  } catch(e) { status.className = 'albed-status err'; status.textContent = '\u2717 Network error'; }
  btn.disabled = false; btn.textContent = 'Save & Refresh Poster';
}

async function resetAlbum(btn) {
  var row = btn.closest('.albed-row');
  var name = row.dataset.name;
  var status = row.querySelector('.albed-status');
  window._svAlert({
    title: 'Re-fetch Metadata?',
    body: 'Re-fetch poster & meta for \u201c' + name + '\u201d? This will overwrite saved changes.',
    confirmText: 'Re-fetch',
    cancelText: 'Cancel',
    danger: true,
    onConfirm: async function() {
      status.className = 'albed-status'; status.textContent = '';
      btn.disabled = true; btn.textContent = 'Fetching\u2026';
      try {
        var r = await fetch('/api/update_album_meta', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album:name,reset:true})});
        var j = await r.json();
        if (j.ok) {
          status.className = 'albed-status ok'; status.textContent = '\u2713 Re-fetched. Reload to see updates.';
          if (j.poster_url) {
            var img = row.querySelector('.albed-poster img');
            if (img) { img.src = j.poster_url + '?sm=1&t=' + Date.now(); img.style.display = ''; }
            else { row.querySelector('.albed-poster').innerHTML = '<img src="' + j.poster_url + '?sm=1&t=' + Date.now() + '" style="width:100%;display:block;">'; }
          }
          if (j.meta) {
            var m = j.meta;
            if (m.poster  !== undefined) row.querySelector('[name=poster]').value  = m.poster  || '';
            if (m.year    !== undefined) row.querySelector('[name=year]').value    = m.year    || '';
            if (m.rating  !== undefined) row.querySelector('[name=rating]').value  = m.rating  || '';
            if (m.type    !== undefined) row.querySelector('[name=type]').value    = m.type    || '';
            if (m.plot    !== undefined) row.querySelector('[name=plot]').value    = m.plot    || '';
          }
        } else { status.className = 'albed-status err'; status.textContent = '\u2717 ' + j.error; }
      } catch(e) { status.className = 'albed-status err'; status.textContent = '\u2717 Network error'; }
      btn.disabled = false; btn.textContent = 'Re-fetch Metadata';
    }
  });
}

function deleteAlbum(btn) {
  var row = btn.closest('.albed-row');
  var name = row.dataset.name;
  var status = row.querySelector('.albed-status');
  window._svAlert({
    title: 'Delete Album?',
    body: 'Permanently delete ALL videos in \u201c' + name + '\u201d from StreamVault cache? This cannot be undone.',
    confirmText: 'Delete',
    cancelText: 'Cancel',
    danger: true,
    onConfirm: async function() {
      btn.disabled = true; btn.textContent = 'Deleting\u2026';
      status.className = 'albed-status'; status.textContent = '';
      try {
        var r = await fetch('/api/delete_album', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album:name})});
        var j = await r.json();
        if (j.ok) {
          status.className = 'albed-status ok';
          status.textContent = '\u2713 Deleted ' + j.removed + ' video' + (j.removed !== 1 ? 's' : '') + '.';
          setTimeout(function() { row.style.transition = 'opacity .4s'; row.style.opacity = '0'; setTimeout(function() { row.remove(); }, 400); }, 800);
        } else {
          status.className = 'albed-status err';
          status.textContent = '\u2717 ' + (j.error || 'error');
          btn.disabled = false; btn.textContent = 'Delete Album';
        }
      } catch(e) {
        status.className = 'albed-status err'; status.textContent = '\u2717 Network error';
        btn.disabled = false; btn.textContent = 'Delete Album';
      }
    }
  });
}
</script>"""
        + _PAGE_CLOSE
        + """</body></html>"""
    )
    page = page.encode("utf-8", errors="ignore").decode("utf-8")
    return web.Response(content_type="text/html", text=page)


async def route_api_update_album_meta(req: web.Request):
    """POST /api/update_album_meta — update/reset poster + meta for one album.

    Body (save):  {album, poster, year, rating, type, plot}
    Body (reset): {album, reset: true}

    Steps:
      1. Delete local poster files (.jpg, _sm.jpg, .lqip) for this album slug
      2. Evict _poster_mem and imdb cache entries
      3. If reset=true: drop imdb cache key so _fetch_media_meta re-queries
         If save: inject the supplied values directly into imdb cache
      4. If a poster URL was provided: download & cache it now
      5. Return {ok, poster_url, meta}
    """
    import cache as _cache

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    album = (body.get("album") or "").strip()
    if not album:
        return web.json_response({"ok": False, "error": "album required"}, status=400)

    reset = bool(body.get("reset", False))
    slug = _hashlib.md5(album.encode()).hexdigest()

    # ── 1. Delete disk poster files ───────────────────────────────────────────
    os.makedirs(POSTERS_DIR, exist_ok=True)
    for suffix in (".jpg", "_sm.jpg", ".lqip"):
        p = os.path.join(POSTERS_DIR, f"{slug}{suffix}")
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    # ── 2. Evict memory caches ────────────────────────────────────────────────
    _cache._poster_mem.pop(album, None)
    key = album.strip().lower()
    _cache._imdb_cache_mem.pop(key, None)

    # ── 3. Build or clear IMDB cache entry ───────────────────────────────────
    loop = asyncio.get_event_loop()

    if reset:
        # Re-fetch using TMDB→IMDb GraphQL→TVMaze pipeline (imdb_search.py)
        import config as _cfg
        from imdb_search import search_imdb_for_album

        tmdb_key = (
            getattr(_cfg, "TMDB_API_KEY", "") or "10822052cd7c36868f387e3f713ad713"
        )
        meta = await loop.run_in_executor(
            None, lambda: search_imdb_for_album(album, tmdb_api_key=tmdb_key)
        )
        if not isinstance(meta, dict):
            meta = {}
        # Sanitize local URLs
        if meta.get("poster", "").startswith("/"):
            meta["poster"] = ""
        _cache._imdb_cache_mem[key] = meta
        _cache._save_imdb_cache()
    else:
        poster_url_raw = (body.get("poster") or "").strip()
        meta = {
            "poster": poster_url_raw,
            "year": (body.get("year") or "").strip(),
            "rating": (body.get("rating") or "").strip(),
            "type": (body.get("type") or "").strip(),
            "plot": (body.get("plot") or "").strip(),
            "source": "manual",
        }
        _cache._imdb_cache_mem[key] = meta
        _cache._save_imdb_cache()

    # ── 4. Download + cache poster if we have a URL ───────────────────────────
    poster_remote = meta.get("poster") or ""
    poster_url = ""
    if poster_remote and not poster_remote.startswith("/"):
        poster_url = await loop.run_in_executor(
            None, _cache_poster_sync, album, poster_remote
        )
    elif poster_remote.startswith("/poster/"):
        poster_url = poster_remote

    # ── 5. Populate _poster_mem ───────────────────────────────────────────────
    lqip = ""
    lqip_path = os.path.join(POSTERS_DIR, f"{slug}.lqip")
    try:
        with open(lqip_path) as _f:
            lqip = _f.read().strip()
    except Exception:
        pass

    _cache._poster_mem[album] = {
        "poster_url": poster_url,
        "poster_sm_url": (
            (poster_url + "?sm=1") if poster_url.startswith("/poster/") else poster_url
        ),
        "lqip": lqip,
        "meta": meta,
    }
    _cache._index_html_cache = None

    print(f"[album_meta] Updated '{album}' → poster={poster_url!r} reset={reset}")
    return web.json_response({"ok": True, "poster_url": poster_url, "meta": meta})


async def route_album_rename(req: web.Request):
    """POST /api/album_rename — rename an album across all cache entries and overrides."""
    import cache as _cache

    try:
        body = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    old_name = (body.get("old_name") or "").strip()
    new_name = (body.get("new_name") or "").strip()
    if not old_name or not new_name:
        return web.json_response(
            {"ok": False, "error": "old_name and new_name required"}, status=400
        )
    if old_name == new_name:
        return web.json_response({"ok": True, "renamed": 0})

    import hashlib as _hl

    # ── Snapshot what needs changing — pure Python, fast enough on loop ───────
    old_key = old_name.strip().lower()
    new_key = new_name.strip().lower()
    old_slug = _hl.md5(old_name.encode()).hexdigest()
    new_slug = _hl.md5(new_name.encode()).hexdigest()

    # Mutate in-memory dicts immediately so live requests see new name at once
    data = _cache._cache_mem if _cache._cache_mem else _cache._load_cache()
    changed_videos = []
    for v in data:
        if v.get("album") == old_name:
            v["album"] = new_name
            changed_videos.append(v)
            _cache._cache_meta[v["message_id"]] = v

    _cache._albums_dirty = True
    _cache._index_html_cache = None

    if old_name in _cache._poster_mem:
        _cache._poster_mem[new_name] = _cache._poster_mem.pop(old_name)
    if old_key in _cache._imdb_cache_mem:
        _cache._imdb_cache_mem[new_key] = _cache._imdb_cache_mem.pop(old_key)

    # ── Everything that touches disk or SQLite goes to executor ──────────────
    def _persist():
        # FTS upsert — SQLite executemany, can be slow on large albums
        if changed_videos:
            _cache._fts_upsert(changed_videos)
        # Full cache JSON write
        _cache._save_json(_cache.CACHE_FILE, data)
        # Overrides
        overrides = _cache._load_album_overrides()
        for mid, alb in list(overrides.items()):
            if alb == old_name:
                overrides[mid] = new_name
        _cache._save_album_overrides(overrides)
        # IMDB cache
        if new_key in _cache._imdb_cache_mem:
            _cache._save_imdb_cache()
        # Poster files
        import shutil as _sh

        for suffix in (".jpg", "_sm.jpg", ".lqip"):
            old_p = os.path.join(POSTERS_DIR, f"{old_slug}{suffix}")
            new_p = os.path.join(POSTERS_DIR, f"{new_slug}{suffix}")
            if os.path.exists(old_p) and not os.path.exists(new_p):
                _sh.copy2(old_p, new_p)

    loop = asyncio.get_event_loop()
    asyncio.ensure_future(loop.run_in_executor(None, _persist))

    print(f"[album_rename] '{old_name}' → '{new_name}' ({len(changed_videos)} videos)")
    return web.json_response({"ok": True, "renamed": len(changed_videos)})


# ── NOTIFICATIONS ─────────────────────────────────────────────────────────────
async def route_notifications(req: web.Request):
    """GET /api/notifications — return and clear queued notifications."""
    items = list(_notifications)
    _notifications.clear()
    return web.json_response({"ok": True, "items": items})


# ── REFETCH MISSING THUMBS ────────────────────────────────────────────────────
# ── APP FACTORY ───────────────────────────────────────────────────────────────
def make_app():
    app = web.Application(
        middlewares=[compression_middleware, auth_middleware],
        client_max_size=64 * 1024 * 1024,
    )
    app.router.add_get("/login", route_login_get)
    app.router.add_post("/api/login", route_login_post)
    app.router.add_get("/logout", route_logout)
    app.router.add_get("/healthz", route_healthz)
    app.router.add_get("/", route_index)
    app.router.add_get("/album/{album_name}", route_album)
    app.router.add_get("/api/album_data/{album_name}", route_album_data)
    app.router.add_get("/poster/{slug}", route_poster)
    app.router.add_get("/stream/{msg_id:\\d+}", stream_handler)
    app.router.add_get("/vlc/{msg_id:\\d+}", vlc_stream_handler)
    app.router.add_get("/vlc/{msg_id:\\d+}/{filename:.*}", vlc_stream_handler)
    app.router.add_post("/api/fetch", route_fetch)
    app.router.add_post("/api/imdb_gap_fill", route_imdb_gap_fill)
    app.router.add_post("/api/refresh", route_refresh)
    app.router.add_get("/api/settings", route_settings_get)
    app.router.add_post("/api/settings", route_settings_post)
    app.router.add_get("/settings", route_settings_page)
    app.router.add_get("/api/meta/{msg_id:\\d+}", route_meta)
    app.router.add_post("/api/launch_vlc", route_launch_vlc)
    app.router.add_get("/api/vlc_link/{msg_id:\\d+}", route_vlc_link)
    app.router.add_post("/api/album_assign", route_album_assign)
    app.router.add_get("/api/albums", route_album_list)
    app.router.add_post("/api/delete_videos", route_delete_videos)
    app.router.add_post("/api/delete_album", route_delete_album)
    app.router.add_get("/manage", route_manage)
    app.router.add_get("/api/manage_videos", route_api_manage_videos)
    app.router.add_get("/manage/albums", route_manage_albums)
    app.router.add_post("/api/update_album_meta", route_api_update_album_meta)
    app.router.add_post("/api/album_rename", route_album_rename)
    app.router.add_get("/api/notifications", route_notifications)
    app.router.add_post("/api/notifications/seen", route_mark_notif_seen)
    app.router.add_get("/api/uncategorized_count", route_uncategorized_count)
    app.router.add_get("/api/search", route_search)
    app.router.add_get("/api/history", route_history)
    app.router.add_post("/api/history/remove", route_history_remove)
    app.router.add_post("/api/history/record", route_history_record)
    app.router.add_post("/hls/{msg_id:\\d+}/start", route_hls_start)
    app.router.add_get("/hls/{msg_id:\\d+}/playlist.m3u8", route_hls_playlist)
    app.router.add_get("/hls/{msg_id:\\d+}/{seg_name}", route_hls_segment)
    app.router.add_post("/hls/{msg_id:\\d+}/stop", route_hls_stop)
    app.router.add_get("/api/stream_cache", route_stream_cache_stats)
    app.router.add_post("/api/clear_cache", route_clear_stream_cache)
    # Static files (CSS served from disk — not embedded in Python source)
    app.router.add_static(
        "/static", os.path.join(_bundle_dir, "static"), show_index=False
    )
    # One-time cleanup of old vlc_tmp/ files from the download-based approach
    asyncio.ensure_future(_cleanup_vlc_tmp())
    return app

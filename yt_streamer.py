"""yt_streamer.py — Smooth YouTube streaming engine for StreamVault.

Solves the bandwidth starvation issue on 4K / 2K YouTube streaming (e.g. 15 Mbps line)
by running a progressive lookahead pre-buffer pipeline:
  1. Extracts video and audio format streams via yt-dlp without downloading.
  2. Spawns an internal stream session that muxes chosen video + audio via ffmpeg
     into a progressive MPEG-TS or fragmented MP4 spool file.
  3. Pre-buffers an initial cushion (configurable seconds / bytes) so playback
     can begin smoothly while download proceeds ahead of playhead.
  4. Exposes /api/yt/stream/{session_id} with full HTTP Range request support.
  5. Can launch VLC directly with network caching options.
"""

import asyncio
import os
import sys
import time
import uuid
import json
import shutil
import subprocess
from typing import Dict, Any, Optional
from aiohttp import web
import config as _cfg

# Temporary spool directory for active YouTube streams
_YT_CACHE_DIR = os.path.join(_cfg._here, "data", "yt_cache")
os.makedirs(_YT_CACHE_DIR, exist_ok=True)

# Active streaming sessions: session_id -> SessionDict
_SESSIONS: Dict[str, Any] = {}


def _cleanup_old_spools():
    now = time.time()
    try:
        if os.path.exists(_YT_CACHE_DIR):
            for fname in os.listdir(_YT_CACHE_DIR):
                p = os.path.join(_YT_CACHE_DIR, fname)
                # Remove spools older than 6 hours
                if os.path.isfile(p) and (now - os.path.getmtime(p) > 21600):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
    except Exception:
        pass


_cleanup_old_spools()


_YT_INFO_CACHE: Dict[str, Any] = {}


async def extract_yt_info(url: str) -> Dict[str, Any]:
    """Run yt-dlp in JSON extraction mode to get metadata & formats without downloading."""
    loop = asyncio.get_event_loop()

    # Clean URL tracking parameters (e.g. ?si=...)
    clean_url = url.split("?si=")[0].split("&si=")[0].strip()

    # Instant return if recently probed
    if clean_url in _YT_INFO_CACHE:
        return _YT_INFO_CACHE[clean_url]

    def _sync_extract():
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "js_runtimes": {"node": {}},
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
            "extract_flat": False,
            "http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
            },
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(clean_url, download=False)

    info = await loop.run_in_executor(None, _sync_extract)
    if not info:
        raise ValueError("Could not extract video metadata from link.")

    title = info.get("title", "YouTube Video")
    duration = info.get("duration", 0)
    thumbnail = info.get("thumbnail", "")
    webpage_url = info.get("webpage_url", clean_url)

    raw_formats = info.get("formats", [])
    qualities = []
    seen_heights = set()

    # Sort formats from highest resolution down
    # Prioritize real video streams (exclude storyboards / thumbnails / audio-only)
    valid_video_formats = []
    for f in raw_formats:
        vcodec = f.get("vcodec") or "none"
        format_note = (f.get("format_note") or "").lower()
        format_id = str(f.get("format_id") or "")
        h = f.get("height")
        # Exclude storyboard streams (sb0, sb1...) and low-res previews (< 144p)
        if vcodec != "none" and h and h >= 144 and not format_id.startswith("sb") and "storyboard" not in format_note:
            valid_video_formats.append(f)

    # Sort descending by height, but prioritize hardware-accelerated codecs (avc1/mp4) over software VP9/AV1
    def _format_sort_key(x):
        h = x.get("height", 0) or 0
        fps = x.get("fps", 0) or 0
        vcodec = (x.get("vcodec") or "").lower()
        # Prefer AVC / H.264 (hardware GPU decode) over VP9/AV1 (CPU heavy)
        is_avc = 1 if (vcodec.startswith("avc") or "h264" in vcodec) else 0
        tbr = x.get("tbr", 0) or 0
        return (h, is_avc, fps, tbr)

    valid_video_formats.sort(key=_format_sort_key, reverse=True)

    for f in valid_video_formats:
        h = f.get("height")
        if not h:
            continue
        fps = f.get("fps") or 30
        ext = f.get("ext", "mp4")
        vcodec = f.get("vcodec", "")
        format_id = f.get("format_id")
        tbr = f.get("tbr")  # total bitrate approx in kbps
        filesize = f.get("filesize") or f.get("filesize_approx")

        label = f"{h}p"
        if fps and fps > 30:
            label += f"{fps}"
        if h >= 2160:
            label += " (4K Ultra HD)"
        elif h >= 1440:
            label += " (2K Quad HD)"
        elif h >= 1080:
            label += " (Full HD)"
        elif h >= 720:
            label += " (HD)"

        # Group by label to keep selector crisp
        if label not in seen_heights:
            seen_heights.add(label)
            qualities.append({
                "label": label,
                "height": h,
                "fps": fps,
                "format_id": format_id,
                "ext": ext,
                "vcodec": vcodec,
                "url": f.get("url"),
                "tbr": round(tbr, 1) if tbr else None,
                "filesize": filesize,
            })

    # Find best audio direct URL (prefer mp4a/AAC for instant hardware sync with AVC video)
    best_audio_url = None
    audio_formats = [f for f in raw_formats if (f.get("vcodec") == "none" or not f.get("vcodec")) and f.get("acodec") != "none" and f.get("url")]
    if audio_formats:
        def _audio_sort_key(a):
            acodec = (a.get("acodec") or "").lower()
            is_aac = 1 if ("mp4a" in acodec or "aac" in acodec) else 0
            abr = a.get("abr", 0) or 0
            return (is_aac, abr)
        audio_formats.sort(key=_audio_sort_key, reverse=True)
        best_audio_url = audio_formats[0].get("url")

    result = {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "url": webpage_url,
        "audio_url": best_audio_url,
        "qualities": qualities,
    }
    _YT_INFO_CACHE[clean_url] = result
    return result


class YtProxyTrack:
    """Manages cached chunks and background lookahead prefetching for an upstream YouTube track."""

    CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB per chunk

    def __init__(self, track_type: str, url: str, content_type: str):
        self.track_type = track_type
        self.url = url
        self.content_type = content_type
        self.total_size = 0
        self.chunks: Dict[int, bytes] = {}
        self.playhead_byte = 0
        self.lock = asyncio.Lock()
        self.fetch_tasks: Dict[int, asyncio.Task] = {}
        self._bg_worker: Optional[asyncio.Task] = None

    async def init_size(self):
        """Probes content length via 1-byte range request."""
        import aiohttp
        async with aiohttp.ClientSession() as sess:
            headers = {"Range": "bytes=0-0"}
            async with sess.get(self.url, headers=headers) as r:
                cr = r.headers.get("Content-Range", "")
                if "/" in cr:
                    try:
                        self.total_size = int(cr.split("/")[-1])
                    except Exception:
                        pass
                if not self.total_size:
                    self.total_size = int(r.headers.get("Content-Length", 0) or 0)

    async def get_chunk(self, chunk_idx: int) -> bytes:
        """Returns chunk bytes, fetching from YouTube if not yet in cache."""
        async with self.lock:
            if chunk_idx in self.chunks:
                return self.chunks[chunk_idx]
            if chunk_idx in self.fetch_tasks:
                task = self.fetch_tasks[chunk_idx]
            else:
                task = asyncio.create_task(self._fetch_chunk_network(chunk_idx))
                self.fetch_tasks[chunk_idx] = task

        data = await task
        return data

    async def _fetch_chunk_network(self, chunk_idx: int) -> bytes:
        import aiohttp
        start_b = chunk_idx * self.CHUNK_SIZE
        if self.total_size > 0 and start_b >= self.total_size:
            return b""
        end_b = start_b + self.CHUNK_SIZE - 1
        if self.total_size > 0:
            end_b = min(end_b, self.total_size - 1)

        headers = {"Range": f"bytes={start_b}-{end_b}"}
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=25)) as r:
                        if r.status in (200, 206):
                            data = await r.read()
                            async with self.lock:
                                self.chunks[chunk_idx] = data
                                self.fetch_tasks.pop(chunk_idx, None)
                                self._prune_cache()
                            return data
            except Exception:
                await asyncio.sleep(0.5)

        async with self.lock:
            self.fetch_tasks.pop(chunk_idx, None)
        return b""

    def _prune_cache(self):
        """Keeps 100 MB behind playhead for instant rewind, evicting further behind only when large."""
        # Allow up to 1.5 GB in-memory per session
        max_chunks = 750  # ~1.5 GB
        if len(self.chunks) <= max_chunks:
            return
        cur_idx = self.playhead_byte // self.CHUNK_SIZE
        # Keep 50 chunks (100 MB) behind
        keep_behind_idx = max(0, cur_idx - 50)
        # Evict oldest chunks that are behind keep_behind_idx
        for idx in sorted(list(self.chunks.keys())):
            if len(self.chunks) <= max_chunks:
                break
            if idx < keep_behind_idx and idx > 0:  # keep chunk 0 (headers/metadata)
                self.chunks.pop(idx, None)

    async def prebuffer_bytes(self, target_bytes: int):
        """Fetches up to target_bytes sequentially ahead."""
        target_chunks = (target_bytes + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
        for c in range(target_chunks):
            await self.get_chunk(c)

    def start_lookahead(self):
        """Runs continuous background lookahead worker fetching at full connection speed."""
        if self._bg_worker is None or self._bg_worker.done():
            self._bg_worker = asyncio.create_task(self._lookahead_loop())

    async def _lookahead_loop(self):
        while True:
            cur_idx = self.playhead_byte // self.CHUNK_SIZE
            # Always keep 100 MB (50 chunks) ahead of current playhead
            lookahead_ahead = 50
            needed = [i for i in range(cur_idx, cur_idx + lookahead_ahead)
                      if i not in self.chunks and (self.total_size == 0 or i * self.CHUNK_SIZE < self.total_size)]
            if not needed:
                await asyncio.sleep(0.4)
                continue

            for idx in needed[:2]:  # fetch 2 pipelined chunks concurrently
                if idx not in self.chunks and idx not in self.fetch_tasks:
                    await self.get_chunk(idx)
            await asyncio.sleep(0.3)  # Gentle pacing: prevents CPU saturation while maintaining full bandwidth


class YtStreamSession:
    """Active streaming proxy session with video and audio caching tracks."""

    def __init__(self, session_id: str, title: str, video_url: str, audio_url: str):
        self.session_id = session_id
        self.title = title
        self.video_track = YtProxyTrack("video", video_url, "video/mp4")
        self.audio_track = YtProxyTrack("audio", audio_url, "audio/mp4") if audio_url else None
        self.ready_event = asyncio.Event()

    async def prepare(self, initial_buffer_mb: float = 100.0):
        # 1. Probe total size for both tracks
        tasks = [self.video_track.init_size()]
        if self.audio_track:
            tasks.append(self.audio_track.init_size())
        await asyncio.gather(*tasks)

        # 2. Pre-buffer initial cushion (60 MB on video, 8 MB on audio for quick start + high stability)
        target_v = int(min(60.0, initial_buffer_mb) * 1024 * 1024)
        target_a = min(8 * 1024 * 1024, target_v // 8)
        prebuf_tasks = [self.video_track.prebuffer_bytes(target_v)]
        if self.audio_track:
            prebuf_tasks.append(self.audio_track.prebuffer_bytes(target_a))
        await asyncio.gather(*prebuf_tasks)

        # 3. Start forward background lookahead workers
        self.video_track.start_lookahead()
        if self.audio_track:
            self.audio_track.start_lookahead()

        self.ready_event.set()


# ── Route handlers ─────────────────────────────────────────────────────────────

async def route_yt_info(req: web.Request):
    """POST /api/yt/info — Fetch video info and all available resolutions."""
    try:
        body = await req.json()
        url = (body.get("url") or "").strip()
        if not url:
            return web.json_response({"ok": False, "error": "URL is required"}, status=400)

        info = await extract_yt_info(url)
        return web.json_response({"ok": True, "data": info})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_yt_start_stream(req: web.Request):
    """POST /api/yt/start — Pre-buffers cushion, starts lookahead, and opens in VLC with hardware acceleration."""
    try:
        body = await req.json()
        url = (body.get("url") or "").strip()
        video_url = (body.get("video_url") or "").strip()
        audio_url = (body.get("audio_url") or "").strip()
        format_id = (body.get("format_id") or "").strip()
        title = (body.get("title") or "YouTube Video").strip()
        launch_vlc = bool(body.get("launch_vlc", True))
        initial_buffer_mb = float(body.get("buffer_mb", 60.0))

        if not url:
            return web.json_response({"ok": False, "error": "URL is required"}, status=400)

        # Resolve direct stream URLs if not provided
        if not video_url:
            info = await extract_yt_info(url)
            audio_url = info.get("audio_url") or ""
            target_q = None
            if format_id:
                for q in info.get("qualities", []):
                    if str(q.get("format_id")) == str(format_id):
                        target_q = q
                        break
            if not target_q and info.get("qualities"):
                target_q = info["qualities"][0]
            if target_q:
                video_url = target_q.get("url") or ""

        if not video_url:
            return web.json_response({"ok": False, "error": "Could not resolve stream URL"}, status=500)

        session_id = uuid.uuid4().hex[:12]
        session = YtStreamSession(session_id, title, video_url, audio_url)
        _SESSIONS[session_id] = session

        # Pre-buffer initial cushion
        await session.prepare(initial_buffer_mb=initial_buffer_mb)

        video_stream_url = f"http://127.0.0.1:{_cfg.PORT}/api/yt/stream/{session_id}/video"
        audio_stream_url = f"http://127.0.0.1:{_cfg.PORT}/api/yt/stream/{session_id}/audio" if audio_url else None

        if launch_vlc:
            from routes import _find_vlc
            vlc = _find_vlc()
            if vlc:
                vlc_cmd = [
                    vlc,
                    video_stream_url,
                    "--meta-title",
                    title,
                    "--avcodec-hw=any",       # Enable GPU hardware acceleration
                    "--network-caching=6000", # 6 second network cache
                    "--live-caching=6000",
                    "--file-caching=0",
                    "--http-reconnect",
                    "--no-drop-late-frames",  # Prevent dropping frames during seek
                    "--no-skip-frames",
                ]
                if audio_stream_url:
                    vlc_cmd.append(f"--input-slave={audio_stream_url}")

                _env = os.environ.copy()
                _vlc_cwd = os.path.dirname(vlc) if os.name == "nt" else None
                subprocess.Popen(vlc_cmd, cwd=_vlc_cwd, env=_env, close_fds=True)

        return web.json_response({
            "ok": True,
            "session_id": session_id,
            "video_url": video_stream_url,
            "audio_url": audio_stream_url,
            "buffered_mb": round(len(session.video_track.chunks) * 2, 1),
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_yt_stream(req: web.Request):
    """GET /api/yt/stream/{session_id}/{track} — Full HTTP Range proxy with rewind cache."""
    session_id = req.match_info.get("session_id", "")
    track_type = req.match_info.get("track", "video")
    session = _SESSIONS.get(session_id)
    if not session:
        return web.Response(status=404, text="Stream session not found or expired")

    track: YtProxyTrack = session.video_track if track_type == "video" else session.audio_track
    if not track:
        return web.Response(status=404, text="Track not found")

    total_size = track.total_size
    range_header = req.headers.get("Range", "")
    start_b = 0
    end_b = total_size - 1 if total_size > 0 else 0

    if range_header.startswith("bytes="):
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start_b = int(parts[0]) if parts[0] else 0
            if len(parts) > 1 and parts[1]:
                end_b = int(parts[1])
        except Exception:
            start_b = 0

    if total_size > 0 and end_b >= total_size:
        end_b = total_size - 1

    track.playhead_byte = start_b
    content_len = (end_b - start_b + 1) if total_size > 0 else None

    headers = {
        "Content-Type": track.content_type,
        "Accept-Ranges": "bytes",
    }
    if total_size > 0:
        headers["Content-Range"] = f"bytes {start_b}-{end_b}/{total_size}"
        headers["Content-Length"] = str(content_len)
        status = 206
    else:
        headers["Transfer-Encoding"] = "chunked"
        status = 200

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(req)

    cur_b = start_b
    while total_size == 0 or cur_b <= end_b:
        chunk_idx = cur_b // track.CHUNK_SIZE
        inner_off = cur_b % track.CHUNK_SIZE
        chunk_data = await track.get_chunk(chunk_idx)
        if not chunk_data or inner_off >= len(chunk_data):
            break

        to_take = len(chunk_data) - inner_off
        if total_size > 0:
            to_take = min(to_take, end_b - cur_b + 1)

        data = chunk_data[inner_off : inner_off + to_take]
        try:
            await resp.write(data)
        except (ConnectionResetError, asyncio.CancelledError):
            break

        cur_b += len(data)
        track.playhead_byte = cur_b

    return resp

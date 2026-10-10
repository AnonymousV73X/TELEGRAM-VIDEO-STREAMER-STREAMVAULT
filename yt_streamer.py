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


class YtMuxSession:
    """Manages an active on-the-fly FFmpeg copy stream session.

    Takes video URL and audio URL directly from YouTube, copies packets without re-encoding
    (-c copy → 0% CPU), muxes into a unified stream, and feeds VLC seamlessly.
    """

    def __init__(self, session_id: str, title: str, video_url: str, audio_url: str):
        self.session_id = session_id
        self.title = title
        self.video_url = video_url
        self.audio_url = audio_url
        self.spool_path = os.path.join(_YT_CACHE_DIR, f"{session_id}.mkv")
        self.process: Optional[asyncio.subprocess.Process] = None
        self.error: Optional[str] = None
        self.ready_event = asyncio.Event()

    async def start(self, initial_buffer_mb: float = 12.0, timeout_s: float = 25.0):
        ffmpeg_bin = shutil.which("ffmpeg") or "C:\\ProgramData\\chocolatey\\bin\\ffmpeg.EXE"

        # Build FFmpeg command to copy both streams into a single Matroska container
        # -re is not used so it fetches at maximum available connection speed
        cmd = [
            ffmpeg_bin,
            "-y",
            "-loglevel", "error",
            "-i", self.video_url,
        ]
        if self.audio_url and self.audio_url != self.video_url:
            cmd.extend(["-i", self.audio_url])
            cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
        else:
            cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

        cmd.extend([
            "-c:v", "copy",
            "-c:a", "copy",
            "-f", "matroska",
            self.spool_path,
        ])

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as e:
            self.error = str(e)
            self.ready_event.set()
            return

        target_bytes = int(initial_buffer_mb * 1024 * 1024)
        start_t = time.time()

        # Wait until target buffer cushion is written to disk
        while time.time() - start_t < timeout_s:
            if os.path.exists(self.spool_path):
                sz = os.path.getsize(self.spool_path)
                if sz >= target_bytes:
                    break
            if self.process.returncode is not None:
                break
            await asyncio.sleep(0.3)

        self.ready_event.set()

    def get_size(self) -> int:
        if os.path.exists(self.spool_path):
            return os.path.getsize(self.spool_path)
        return 0

    def close(self):
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except Exception:
                pass


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
    """POST /api/yt/start — Starts real-time FFmpeg copy muxer and launches unified stream in VLC."""
    try:
        body = await req.json()
        url = (body.get("url") or "").strip()
        video_url = (body.get("video_url") or "").strip()
        audio_url = (body.get("audio_url") or "").strip()
        format_id = (body.get("format_id") or "").strip()
        title = (body.get("title") or "YouTube Video").strip()
        launch_vlc = bool(body.get("launch_vlc", True))

        if not url:
            return web.json_response({"ok": False, "error": "URL is required"}, status=400)

        # Resolve direct stream URLs if needed
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
        session = YtMuxSession(session_id, title, video_url, audio_url)
        _SESSIONS[session_id] = session

        # Start copy muxer in background & wait for initial cushion (10 MB instant start)
        asyncio.create_task(session.start(initial_buffer_mb=10.0, timeout_s=30.0))
        await session.ready_event.wait()

        if session.error:
            return web.json_response({"ok": False, "error": session.error}, status=500)

        stream_url = f"http://127.0.0.1:{_cfg.PORT}/api/yt/stream/{session_id}"

        if launch_vlc:
            from routes import _find_vlc
            vlc = _find_vlc()
            if vlc:
                vlc_cmd = [
                    vlc,
                    stream_url,
                    "--meta-title",
                    title,
                    "--network-caching=5000",
                    "--live-caching=5000",
                    "--file-caching=2000",
                    "--http-reconnect",
                    "--no-drop-late-frames",
                    "--no-skip-frames",
                ]
                _env = os.environ.copy()
                _vlc_cwd = os.path.dirname(vlc) if os.name == "nt" else None
                subprocess.Popen(vlc_cmd, cwd=_vlc_cwd, env=_env, close_fds=True)

        return web.json_response({
            "ok": True,
            "session_id": session_id,
            "stream_url": stream_url,
            "buffered_mb": round(session.get_size() / (1024 * 1024), 1),
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_yt_stream(req: web.Request):
    """GET /api/yt/stream/{session_id} — Progressive HTTP streaming with full Range support."""
    session_id = req.match_info.get("session_id", "")
    session = _SESSIONS.get(session_id)
    if not session or not os.path.exists(session.spool_path):
        return web.Response(status=404, text="Stream session not found or expired")

    # Wait until spool has initial bytes
    while not session.get_size():
        await asyncio.sleep(0.2)

    total_size = session.get_size()
    range_header = req.headers.get("Range", "")
    start_byte = 0

    if range_header.startswith("bytes="):
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start_byte = int(parts[0]) if parts[0] else 0
        except Exception:
            start_byte = 0

    headers = {
        "Content-Type": "video/x-matroska",
        "Accept-Ranges": "bytes",
        "Transfer-Encoding": "chunked",
    }
    if start_byte > 0:
        headers["Content-Range"] = f"bytes {start_byte}-/*"
        status = 206
    else:
        status = 200

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(req)

    cur_pos = start_byte
    chunk_size = 128 * 1024

    try:
        with open(session.spool_path, "rb") as f:
            f.seek(cur_pos)
            while True:
                available = session.get_size()
                if cur_pos < available:
                    data = f.read(min(chunk_size, available - cur_pos))
                    if data:
                        await resp.write(data)
                        cur_pos += len(data)
                        continue

                # Wait for FFmpeg to produce more bytes
                if session.process and session.process.returncode is None:
                    await asyncio.sleep(0.2)
                    continue

                # Done
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass

    return resp

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


async def extract_yt_info(url: str) -> Dict[str, Any]:
    """Run yt-dlp in JSON extraction mode to get metadata & formats without downloading."""
    loop = asyncio.get_event_loop()

    def _sync_extract():
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, _sync_extract)
    if not info:
        raise ValueError("Could not extract video metadata from link.")

    title = info.get("title", "YouTube Video")
    duration = info.get("duration", 0)
    thumbnail = info.get("thumbnail", "")
    webpage_url = info.get("webpage_url", url)

    raw_formats = info.get("formats", [])
    qualities = []
    seen_heights = set()

    # Sort formats from highest resolution down
    # Prioritize video streams with height information
    valid_video_formats = []
    for f in raw_formats:
        vcodec = f.get("vcodec", "none")
        if vcodec != "none" and f.get("height"):
            valid_video_formats.append(f)

    # Sort descending by height and fps
    valid_video_formats.sort(
        key=lambda x: (x.get("height", 0), x.get("fps", 0) or 0, x.get("tbr", 0) or 0),
        reverse=True,
    )

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
                "tbr": round(tbr, 1) if tbr else None,
                "filesize": filesize,
            })

    return {
        "title": title,
        "duration": duration,
        "thumbnail": thumbnail,
        "url": webpage_url,
        "qualities": qualities,
    }


class YtStreamSession:
    """Manages an active progressive spool download and buffer state."""

    def __init__(self, session_id: str, url: str, format_id: str, title: str):
        self.session_id = session_id
        self.url = url
        self.format_id = format_id
        self.title = title
        self.spool_path = os.path.join(_YT_CACHE_DIR, f"{session_id}.mkv")
        self.process: Optional[asyncio.subprocess.Process] = None
        self.started_at = time.time()
        self.is_done = False
        self.error: Optional[str] = None
        self.ready_event = asyncio.Event()

    async def start(self, initial_buffer_mb: float = 8.0, timeout_s: float = 40.0):
        """Starts yt-dlp + ffmpeg progressive stream down to spool_path and waits for buffer cushion."""
        # yt-dlp format selector: chosen video + best audio
        fmt_spec = f"{self.format_id}+bestaudio/best" if self.format_id else "bestvideo+bestaudio/best"

        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        
        # Command to pipe video + audio directly into an MKV spool container
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            fmt_spec,
            "--no-part",
            "-o",
            self.spool_path,
            "--ffmpeg-location",
            os.path.dirname(ffmpeg_bin) if os.path.dirname(ffmpeg_bin) else ".",
            self.url,
        ]

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

        # Wait until spool file reaches buffer cushion
        while time.time() - start_t < timeout_s:
            if os.path.exists(self.spool_path):
                cur_size = os.path.getsize(self.spool_path)
                if cur_size >= target_bytes:
                    break
            if self.process.returncode is not None:
                # Finished or exited early
                break
            await asyncio.sleep(0.4)

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
    """POST /api/yt/start — Starts progressive buffering and returns stream URL once cushioned."""
    try:
        body = await req.json()
        url = (body.get("url") or "").strip()
        format_id = (body.get("format_id") or "").strip()
        title = (body.get("title") or "YouTube Video").strip()
        launch_vlc = bool(body.get("launch_vlc", True))

        if not url:
            return web.json_response({"ok": False, "error": "URL is required"}, status=400)

        session_id = uuid.uuid4().hex[:12]
        session = YtStreamSession(session_id, url, format_id, title)
        _SESSIONS[session_id] = session

        # Start download & cushion buffer (e.g., 6 MB cushion for instant start + headroom)
        asyncio.create_task(session.start(initial_buffer_mb=6.0, timeout_s=30.0))
        await session.ready_event.wait()

        if session.error:
            return web.json_response({"ok": False, "error": session.error}, status=500)

        stream_url = f"http://127.0.0.1:{_cfg.PORT}/api/yt/stream/{session_id}"

        if launch_vlc:
            # Locate VLC and launch with extended cache buffer
            from routes import _find_vlc
            vlc = _find_vlc()
            if vlc:
                vlc_cmd = [
                    vlc,
                    stream_url,
                    "--meta-title",
                    title,
                    "--network-caching=5000",
                    "--file-caching=5000",
                ]
                subprocess.Popen(vlc_cmd, close_fds=True)

        return web.json_response({
            "ok": True,
            "session_id": session_id,
            "stream_url": stream_url,
            "buffered_bytes": session.get_size(),
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def route_yt_stream(req: web.Request):
    """GET /api/yt/stream/{session_id} — Progressive HTTP streaming from the growing spool."""
    session_id = req.match_info.get("session_id", "")
    session = _SESSIONS.get(session_id)
    if not session or not os.path.exists(session.spool_path):
        return web.Response(status=404, text="Stream session not found or expired")

    # Wait until file has at least initial bytes
    while not session.get_size():
        await asyncio.sleep(0.2)

    total_size = session.get_size()
    range_header = req.headers.get("Range", "")
    start_byte = 0

    if range_header.startswith("bytes="):
        try:
            r = range_header.replace("bytes=", "").split("-")
            start_byte = int(r[0]) if r[0] else 0
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

    # Continuously stream growing file to client
    cur_pos = start_byte
    chunk_size = 64 * 1024

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

                # If downloader process is still running, wait for more bytes
                if session.process and session.process.returncode is None:
                    await asyncio.sleep(0.25)
                    continue

                # Process exited and all bytes read
                break
    except (ConnectionResetError, asyncio.CancelledError):
        pass

    return resp

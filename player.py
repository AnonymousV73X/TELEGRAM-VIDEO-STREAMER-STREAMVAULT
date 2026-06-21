"""stream_test.py — minimal Telegram → HTTP streaming probe.

Tests whether your Telegram credentials can stream a video file via raw
MTProto byte-range fetches, served over a local aiohttp server that VLC
(or any player) can open.

Usage:
    python stream_test.py

Reads API_ID, API_HASH, CHANNEL_ID from .env in the same folder.
On startup it lists the 5 most recent video messages and lets you pick one,
then serves it at http://127.0.0.1:6789/stream  — open that URL in VLC.

Dependencies (auto-installed): aiohttp, telethon
"""

import subprocess, sys, os

for _pkg in ["aiohttp", "telethon"]:
    try:
        __import__(_pkg)
    except ImportError:
        print(f"[setup] installing {_pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", _pkg])

import asyncio, re
from aiohttp import web
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

# ── Load .env ─────────────────────────────────────────────────────────────────
_here = os.path.dirname(os.path.abspath(__file__))
_env = os.path.join(_here, ".env")
if os.path.exists(_env):
    with open(_env) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            os.environ[k] = v

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
CHANNEL = os.environ.get("CHANNEL_ID", "")
PORT = 6789

if not API_ID or not API_HASH or not CHANNEL:
    print("ERROR: set API_ID, API_HASH, CHANNEL_ID in .env")
    sys.exit(1)

# ── Globals set at startup ────────────────────────────────────────────────────
client: TelegramClient = None
target_msg = None  # the chosen Telethon Message object
file_size: int = 0
filename: str = "video.mkv"

TG_ALIGN = 4096
REQ_SIZE = 512 * 1024  # 512 KB per MTProto request — fast first-byte
BURST_WRITE = 32 * 1024  # 32 KB write chunks during burst


def _clamp(offset: int, size: int, fs: int) -> int:
    """Return a valid MTProto request size: 4096-aligned, offset+size < fs."""
    if fs <= 0 or offset >= fs:
        return 0
    max_ok = fs - offset - TG_ALIGN
    if max_ok < TG_ALIGN:
        return 0
    aligned = (min(size, max_ok) // TG_ALIGN) * TG_ALIGN
    return max(TG_ALIGN, aligned)


async def stream(request: web.Request) -> web.StreamResponse:
    """Byte-range aware stream handler."""
    global target_msg, file_size, filename

    if target_msg is None:
        return web.Response(status=503, text="No video selected yet")

    rng = request.headers.get("Range", "")
    start = 0
    if rng:
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))

    end = file_size - 1 if file_size else None
    content_length = (end - start + 1) if end is not None else None

    status = 206 if (rng or file_size) else 200
    headers = {
        "Content-Type": "video/mp4",
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-cache",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    if end is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    if content_length is not None:
        headers["Content-Length"] = str(content_length)

    resp = web.StreamResponse(status=status, headers=headers)
    await resp.prepare(request)

    print(f"[stream] Range={rng or 'none'} → offset={start} file_size={file_size}")

    sent = 0
    off = start
    try:
        while True:
            if file_size and off >= file_size:
                break
            if content_length is not None and sent >= content_length:
                break

            req = REQ_SIZE
            if file_size:
                req = _clamp(off, req, file_size)
                if req <= 0:
                    print(f"[stream] EOF zone at offset={off}, stopping")
                    break

            block = b""
            async for chunk in client.iter_download(
                target_msg, offset=off, request_size=req, limit=None
            ):
                if not isinstance(chunk, bytes):
                    chunk = bytes(chunk)
                block += chunk
                if len(block) >= req:
                    break

            if not block:
                print(f"[stream] no data at offset={off}, stopping")
                break

            # Trim to content_length
            if content_length is not None:
                remaining = content_length - sent
                if len(block) > remaining:
                    block = block[:remaining]

            # Write in small pieces for low latency
            pos = 0
            while pos < len(block):
                piece = block[pos : pos + BURST_WRITE]
                await resp.write(piece)
                pos += len(piece)

            sent += len(block)
            off += len(block)
            print(
                f"[stream] sent {sent // 1024}KB / {(content_length or file_size) // 1024}KB",
                end="\r",
            )

    except (ConnectionResetError, asyncio.CancelledError, BrokenPipeError):
        print(f"\n[stream] client disconnected at offset={off}")
    except Exception as e:
        print(f"\n[stream] error at offset={off}: {e}")

    print(f"\n[stream] done — served {sent} bytes from offset {start}")
    return resp


async def _pick_video():
    """List recent video messages and let user pick one interactively."""
    global target_msg, file_size, filename

    ent = await client.get_entity(
        int(CHANNEL) if CHANNEL.lstrip("-").isdigit() else CHANNEL
    )
    print(f"\n[setup] Scanning recent messages in {CHANNEL}...")

    videos = []
    async for msg in client.iter_messages(ent, limit=200):
        if not msg.media or not isinstance(msg.media, MessageMediaDocument):
            continue
        doc = msg.media.document
        if not doc:
            continue
        mime = getattr(doc, "mime_type", "") or ""
        fname = ""
        dur = 0
        for attr in doc.attributes:
            if hasattr(attr, "file_name"):
                fname = attr.file_name or ""
            if isinstance(attr, DocumentAttributeVideo):
                dur = int(attr.duration or 0)
        if not (
            mime.startswith("video/")
            or fname.lower().endswith((".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v"))
        ):
            continue
        size_mb = doc.size / 1024 / 1024
        videos.append((msg, fname or f"msg_{msg.id}", size_mb, dur))
        if len(videos) >= 10:
            break

    if not videos:
        print("[setup] No video messages found in recent 200 messages.")
        return False

    print("\nRecent videos:")
    for i, (_, fname, size_mb, dur) in enumerate(videos):
        mm, ss = divmod(dur, 60)
        print(f"  [{i}] {fname}  ({size_mb:.1f} MB, {mm}m{ss:02d}s)")

    choice = input("\nPick a number (or press Enter for 0): ").strip()
    idx = int(choice) if choice.isdigit() and int(choice) < len(videos) else 0

    target_msg, filename, size_mb, _ = videos[idx]
    file_size = target_msg.media.document.size
    print(f"\n[setup] Selected: {filename} ({size_mb:.1f} MB, msg_id={target_msg.id})")
    return True


async def _main():
    global client

    print("=" * 50)
    print("  StreamVault — stream_test.py")
    print("=" * 50)

    client = TelegramClient("stream_test_session", API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    print(f"[setup] Logged in as {me.first_name}")

    ok = await _pick_video()
    if not ok:
        await client.disconnect()
        return

    app = web.Application()
    app.router.add_get("/stream", stream)
    app.router.add_get("/stream/{filename:.*}", stream)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    print(f"\n{'='*50}")
    print(f"  Stream URL (paste into VLC → Open Network Stream):")
    print(f"  http://127.0.0.1:{PORT}/stream/{filename}")
    print(f"{'='*50}\n")
    print("Press Ctrl+C to stop.\n")

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
        await client.disconnect()
        print("\n[setup] Stopped.")


if __name__ == "__main__":
    import platform

    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_main())

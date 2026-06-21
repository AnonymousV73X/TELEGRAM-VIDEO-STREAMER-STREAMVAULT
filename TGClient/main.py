import subprocess, sys


def install():
    for pkg in ["telethon", "python-dotenv", "aiohttp", "cryptg", "pytelegrambotapi"]:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


install()

import asyncio, os, time, logging
from pathlib import Path
from dotenv import load_dotenv
import aiohttp
import telebot
from telethon import TelegramClient
from telethon.errors import FloodWaitError

load_dotenv()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)

CHUNK = 1024 * 1024 * 4  # 4 MB

bot = telebot.TeleBot(BOT_TOKEN)
tg = TelegramClient("tgclient_session", API_ID, API_HASH)
loop = asyncio.get_event_loop()


def edit_or_send(chat_id: int, msg_id: list, text: str):
    try:
        if msg_id[0]:
            bot.edit_message_text(text, chat_id, msg_id[0])
        else:
            m = bot.send_message(chat_id, text)
            msg_id[0] = m.message_id
    except Exception:
        pass


async def download_file(url: str, chat_id: int, msg_id: list) -> Path:
    fname = url.split("/")[-1].split("?")[0] or "file"
    dest = DOWNLOAD_DIR / fname
    t0 = time.time()
    timeout = aiohttp.ClientTimeout(total=None, connect=30)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0))
            done = 0
            last_upd = 0
            with open(dest, "wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    now = time.time()
                    if now - last_upd > 3:
                        last_upd = now
                        mb = done / 1024 / 1024
                        spd = mb / max(now - t0, 0.1)
                        pct = f"{done/total*100:.1f}%" if total else f"{mb:.1f} MB"
                        edit_or_send(
                            chat_id, msg_id, f"📥 Downloading… {pct}  {spd:.2f} MB/s"
                        )
    elapsed = time.time() - t0
    size_mb = dest.stat().st_size / 1024 / 1024
    edit_or_send(
        chat_id,
        msg_id,
        f"✅ Download done  {size_mb:.1f} MB  {elapsed:.1f}s  ({size_mb/elapsed:.2f} MB/s)",
    )
    return dest


async def upload_file(path: Path, chat_id: int, msg_id: list):
    size_mb = path.stat().st_size / 1024 / 1024
    t0 = time.time()
    last = [time.time(), 0]

    def progress(sent, total):
        now = time.time()
        if now - last[0] < 3:
            return
        spd = (sent - last[1]) / max(now - last[0], 0.1) / 1024 / 1024
        last[0] = now
        last[1] = sent
        pct = sent / total * 100
        edit_or_send(chat_id, msg_id, f"📤 Uploading… {pct:.1f}%  {spd:.2f} MB/s")

    is_video = path.suffix.lower() in {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
    try:
        await tg.send_file(
            chat_id,
            path,
            caption=f"✅ <b>{path.name}</b>  <code>{size_mb:.1f} MB</code>",
            parse_mode="html",
            supports_streaming=is_video,
            progress_callback=progress,
        )
    except FloodWaitError as e:
        edit_or_send(chat_id, msg_id, f"⏳ FloodWait {e.seconds}s…")
        await asyncio.sleep(e.seconds + 2)
        await tg.send_file(
            chat_id, path, caption=path.name, supports_streaming=is_video
        )

    elapsed = time.time() - t0
    edit_or_send(
        chat_id,
        msg_id,
        f"🚀 Upload done  {size_mb:.1f} MB  {elapsed:.1f}s  ({size_mb/elapsed:.2f} MB/s)",
    )
    path.unlink(missing_ok=True)


async def handle_url(chat_id: int, url: str):
    msg_id = [None]
    edit_or_send(chat_id, msg_id, "⏳ Starting…")
    try:
        path = await download_file(url, chat_id, msg_id)
        await upload_file(path, chat_id, msg_id)
    except Exception as e:
        edit_or_send(chat_id, msg_id, f"❌ {e}")
        log.exception(e)


@bot.message_handler(commands=["start"])
def cmd_start(msg):
    bot.send_message(
        msg.chat.id,
        "Send a direct download URL — I'll download and upload it at full speed via TGClient.",
    )


@bot.message_handler(func=lambda m: m.text and m.text.strip().startswith("http"))
def cmd_url(msg):
    url = msg.text.strip()
    chat_id = msg.chat.id
    asyncio.run_coroutine_threadsafe(handle_url(chat_id, url), loop)


async def main():
    if not all([API_ID, API_HASH, BOT_TOKEN]):
        log.error("Set API_ID, API_HASH, BOT_TOKEN in .env")
        sys.exit(1)
    await tg.start()
    log.info("TGClient ready. Starting bot polling…")
    await loop.run_in_executor(None, bot.infinity_polling)


if __name__ == "__main__":
    loop.run_until_complete(main())

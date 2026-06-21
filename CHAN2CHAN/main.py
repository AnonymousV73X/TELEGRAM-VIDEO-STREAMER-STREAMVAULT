import subprocess
import sys


def install_deps():
    pkgs = ["telethon", "python-dotenv", "cryptg"]
    for pkg in pkgs:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            print(f"Installing {pkg}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])


install_deps()

import asyncio
import re
import os
import logging
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import (
    InputChannel,
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
)
from telethon.errors import (
    FloodWaitError,
    ChatForwardsRestrictedError,
    MessageIdInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    PeerIdInvalidError,
    UserBannedInChannelError,
    SessionPasswordNeededError,
    AuthKeyUnregisteredError,
    SlowModeWaitError,
    FileReferenceExpiredError,
    RPCError,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("forwarder.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BATCH = int(os.getenv("BATCH_SIZE", "50"))
PATTERN = re.compile(r"PRT", re.IGNORECASE)

# Raw channel IDs (no -100 prefix needed — Telethon handles it)
SOURCE_RAW = int(os.getenv("SOURCE_CHANNEL", ""))
DEST_RAW = int(os.getenv("DEST_CHANNEL", ""))


def get_file_name(msg) -> str:
    if not msg.media:
        return ""
    doc = getattr(msg.media, "document", None)
    if not doc:
        return ""
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            return attr.file_name
    return ""


def matches(msg) -> bool:
    parts = []
    if msg.message:
        parts.append(msg.message)
    fname = get_file_name(msg)
    if fname:
        parts.append(fname)
    return any(PATTERN.search(p) for p in parts)


async def warm_peer(client: TelegramClient, raw_id: int, label: str):
    try:
        entity = await client.get_entity(raw_id)
        log.info(
            f"{label} resolved: '{getattr(entity, 'title', entity.id)}' (id={entity.id})"
        )
        return entity
    except ValueError as e:
        log.error(
            f"{label}: Cannot resolve ID {raw_id}.\n"
            f"Error: {e}\n"
            "Ensure your account is a MEMBER of this channel. "
            "Delete prt_session.session and re-run to refresh peer cache."
        )
        sys.exit(1)
    except ChannelPrivateError:
        log.error(f"{label}: Channel {raw_id} is private and account is not a member.")
        sys.exit(1)
    except Exception as e:
        log.exception(f"{label}: Unexpected error resolving {raw_id}: {e}")
        sys.exit(1)


async def safe_forward(
    client: TelegramClient, dest, source, ids: list, retry: int = 3
) -> bool:
    for attempt in range(1, retry + 1):
        try:
            await client.forward_messages(dest, ids, source)
            return True
        except FloodWaitError as e:
            wait = e.seconds + 5
            log.warning(f"FloodWait {wait}s (attempt {attempt})")
            await asyncio.sleep(wait)
        except SlowModeWaitError as e:
            wait = e.seconds + 2
            log.warning(f"SlowModeWait {wait}s (attempt {attempt})")
            await asyncio.sleep(wait)
        except ChatForwardsRestrictedError:
            log.error("Source channel has forwards restricted. Cannot forward.")
            return False
        except UserBannedInChannelError:
            log.error("Account is banned in one of the channels.")
            return False
        except (ChannelPrivateError, ChatAdminRequiredError):
            log.error("No access or insufficient rights.")
            return False
        except MessageIdInvalidError:
            log.warning(f"Invalid message IDs in batch starting {ids[0]}, skipping.")
            return False
        except FileReferenceExpiredError:
            log.warning("File reference expired, skipping batch.")
            return False
        except RPCError as e:
            log.error(f"RPCError: {e} — attempt {attempt}")
            if attempt == retry:
                return False
            await asyncio.sleep(10)
        except Exception as e:
            log.exception(f"Unexpected forward error attempt {attempt}: {e}")
            if attempt == retry:
                return False
            await asyncio.sleep(10)
    return False


async def collect_matching(client: TelegramClient, source) -> list:
    matched = []
    scanned = 0
    log.info("Scanning source channel history...")
    try:
        async for msg in client.iter_messages(source, reverse=False):
            scanned += 1
            if scanned % 500 == 0:
                log.info(f"Scanned {scanned} | Matched {len(matched)}")
            try:
                if matches(msg):
                    matched.append(msg.id)
            except Exception as e:
                log.warning(f"Error checking msg {msg.id}: {e}")
    except FloodWaitError as e:
        log.warning(f"FloodWait during scan: sleeping {e.seconds}s")
        await asyncio.sleep(e.seconds + 5)
    except ChannelPrivateError:
        log.error("Cannot access source channel — not a member.")
        sys.exit(1)
    except RPCError as e:
        log.error(f"RPCError during scan: {e}")
    except Exception as e:
        log.exception(f"Unexpected scan error: {e}")

    log.info(f"Scan done. Scanned={scanned} Matched={len(matched)}")
    return matched


async def main():
    if not API_ID or not API_HASH:
        log.error("Missing API_ID or API_HASH in .env")
        sys.exit(1)

    client = TelegramClient("prt_session", API_ID, API_HASH)

    try:
        await client.start()
        log.info("Session started.")

        source = await warm_peer(client, SOURCE_RAW, "SOURCE")
        dest = await warm_peer(client, DEST_RAW, "DEST")

        matched = await collect_matching(client, source)

        if not matched:
            log.info("No PRT messages found. Done.")
            await client.disconnect()
            return

        total = len(matched)
        success = 0
        failed = 0
        total_batches = (total + BATCH - 1) // BATCH

        for i in range(0, total, BATCH):
            chunk = matched[i : i + BATCH]
            batch_num = i // BATCH + 1
            log.info(f"Batch {batch_num}/{total_batches} — {len(chunk)} messages...")

            ok = await safe_forward(client, dest, source, chunk)
            if ok:
                success += len(chunk)
                log.info(f"Batch {batch_num} OK.")
            else:
                failed += len(chunk)
                log.warning(f"Batch {batch_num} FAILED.")

            if i + BATCH < total:
                log.info("Pausing 30s...")
                await asyncio.sleep(30)

        log.info(f"Done. Forwarded={success} Failed={failed} Total={total}")

    except SessionPasswordNeededError:
        log.error("2FA required. Run interactively once to authenticate.")
        sys.exit(1)
    except AuthKeyUnregisteredError:
        log.error("Session expired. Delete prt_session.session and re-run.")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("Interrupted.")
    except Exception as e:
        log.exception(f"Fatal: {e}")
        sys.exit(1)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

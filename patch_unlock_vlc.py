import os

filepath = "streaming.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Remove lock from _tg_read — revert to plain download with no lock
target1 = """                async with _get_client_lock(_client):
                    generator = _client.iter_download(
                        msg, offset=pos, request_size=block_size, limit=n_blocks
                    )
                    try:
                        while True:
                            try:
                                # _CHUNK_READ_TIMEOUT seconds timeout per chunk read
                                piece = await asyncio.wait_for(
                                    generator.__anext__(), timeout=_CHUNK_READ_TIMEOUT
                                )
                            except StopAsyncIteration:
                                break
                            if not isinstance(piece, bytes):
                                piece = bytes(piece)
                            if not piece:
                                continue
                            take = (
                                min(len(piece), remaining - window_sent)
                                if remaining is not None
                                else len(piece)
                            )
                            if take <= 0:
                                break
                            yield piece[:take]
                            window_sent += take
                            await asyncio.sleep(
                                0
                            )  # yield to selector \u2014 prevents starvation on Windows
                            if remaining is not None and window_sent >= remaining:
                                break
                    except asyncio.TimeoutError:
                        print(
                            f"[tg_read] chunk read timeout at offset {pos} for msg={_msg_id}"
                        )
                        raise ConnectionResetError("Telegram read timed out")"""

replacement1 = """                generator = _client.iter_download(
                    msg, offset=pos, request_size=block_size, limit=n_blocks
                )
                try:
                    while True:
                        try:
                            # _CHUNK_READ_TIMEOUT seconds timeout per chunk read
                            piece = await asyncio.wait_for(
                                generator.__anext__(), timeout=_CHUNK_READ_TIMEOUT
                            )
                        except StopAsyncIteration:
                            break
                        if not isinstance(piece, bytes):
                            piece = bytes(piece)
                        if not piece:
                            continue
                        take = (
                            min(len(piece), remaining - window_sent)
                            if remaining is not None
                            else len(piece)
                        )
                        if take <= 0:
                            break
                        yield piece[:take]
                        window_sent += take
                        await asyncio.sleep(
                            0
                        )  # yield to selector \u2014 prevents starvation on Windows
                        if remaining is not None and window_sent >= remaining:
                            break
                except asyncio.TimeoutError:
                    print(
                        f"[tg_read] chunk read timeout at offset {pos} for msg={_msg_id}"
                    )
                    raise ConnectionResetError("Telegram read timed out")"""

if target1 in content:
    content = content.replace(target1, replacement1)
    print("Step 1: removed lock from _tg_read OK")
else:
    print("Step 1: target NOT found - checking line endings...")
    content_n = content.replace('\r\n', '\n')
    target1_n = target1.replace('\r\n', '\n')
    replacement1_n = replacement1.replace('\r\n', '\n')
    if target1_n in content_n:
        content = content_n.replace(target1_n, replacement1_n)
        print("Step 1: removed lock from _tg_read OK (normalized)")
    else:
        print("Step 1: NOT FOUND even after normalizing!")

# 2. Add client lock in _fetch_and_cache (the background-only path)
target2 = """            read_offset = chunk_offset
            read_len = fetch_len
            write_pos = read_offset
            try:
                async for chunk in _tg_read(msg_id, read_offset, read_len, client=client):
                    _stream_cache.put(msg_id, write_pos, chunk)
                    write_pos += len(chunk)
                    await asyncio.sleep(0)
                if write_pos < chunk_offset + fetch_len and fs and write_pos < fs:
                    raise ConnectionResetError("tg_read returned early before EOF")"""

replacement2 = """            read_offset = chunk_offset
            read_len = fetch_len
            write_pos = read_offset
            _dl_lock = _get_client_lock(client) if client is not None else None
            try:
                if _dl_lock is not None:
                    await _dl_lock.acquire()
                try:
                    async for chunk in _tg_read(msg_id, read_offset, read_len, client=client):
                        _stream_cache.put(msg_id, write_pos, chunk)
                        write_pos += len(chunk)
                        await asyncio.sleep(0)
                    if write_pos < chunk_offset + fetch_len and fs and write_pos < fs:
                        raise ConnectionResetError("tg_read returned early before EOF")
                finally:
                    if _dl_lock is not None and _dl_lock.locked():
                        _dl_lock.release()"""

if target2 in content:
    content = content.replace(target2, replacement2)
    print("Step 2: added client lock to _fetch_and_cache OK")
else:
    content_n = content if '\r\n' not in content else content.replace('\r\n', '\n')
    target2_n = target2.replace('\r\n', '\n')
    replacement2_n = replacement2.replace('\r\n', '\n')
    if target2_n in content_n:
        content = content_n.replace(target2_n, replacement2_n)
        print("Step 2: added client lock to _fetch_and_cache OK (normalized)")
    else:
        print("Step 2: NOT FOUND even after normalizing!")

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Done.")

"""Bounded clipboard presentation; never runs workspace commands."""
import asyncio
from dataclasses import dataclass

MAX_COPY_BYTES = 1024 * 1024
COPY_LABEL = "⧉ copy"


@dataclass(eq=False)
class ResponseCopy:
    text: str
    source: tuple[str, int] | None = None
    complete: bool = True
    feedback: str = COPY_LABEL
    copying: bool = False


async def response_text(client, target):
    if target.source is None:
        if not target.complete:
            raise ValueError("response source unavailable")
        return target.text
    turn, index = target.source
    after = 0
    chunks = []
    size = 0
    for _ in range(512):
        page = await client.call("turn/message/read", {"turnId": turn, "index": index, "after": after})
        text = page["text"]
        if not isinstance(text, str):
            raise ValueError("invalid response text")
        size += len(text.encode("utf-8"))
        if size > MAX_COPY_BYTES:
            raise ValueError("response too large")
        chunks.append(text)
        cursor = page["nextCursor"]
        if cursor is None:
            return "".join(chunks)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor <= after:
            raise ValueError("invalid response cursor")
        after = cursor
    raise ValueError("response too large")


async def write_clipboard(text, *, executable="/usr/bin/pbcopy"):
    payload = text.encode("utf-8")
    if len(payload) > MAX_COPY_BYTES:
        raise ValueError("response too large")
    process = await asyncio.create_subprocess_exec(executable, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LC_CTYPE": "en_US.UTF-8"})
    try:
        await asyncio.wait_for(process.communicate(payload), 2)
        if process.returncode:
            raise RuntimeError("clipboard unavailable")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()

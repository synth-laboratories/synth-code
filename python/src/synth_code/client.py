"""Bounded, serialized JSON-RPC transport. Rust remains the execution owner."""
import asyncio
import json
from pathlib import Path


class Client:
    def __init__(self, process: asyncio.subprocess.Process):
        self.process = process
        self.reader = process.stdout
        self.writer = process.stdin
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.poisoned = False

    @classmethod
    async def open(cls, executable: Path, store: Path, *, config=None, env_file=None):
        command = [str(executable), "stdio", str(store)]
        if config:
            command += ["--provider-config", str(config.resolve())]
        if env_file:
            command += ["--env-file", str(env_file.resolve())]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=65_536,
        )
        client = cls(process)
        try:
            client.info = await client.call("initialize", {})
            return client
        except BaseException:
            await client.close()
            raise

    @classmethod
    async def attach(cls, socket: Path):
        reader, writer = await asyncio.open_unix_connection(str(socket), limit=65_536)
        client = cls.__new__(cls)
        client.process, client.reader, client.writer = None, reader, writer
        client.lock, client.sequence, client.poisoned = asyncio.Lock(), 0, False
        try:
            client.info = await client.call("initialize", {})
            return client
        except BaseException:
            await client.close()
            raise

    async def call(self, method: str, params: dict):
        async with self.lock:
            if self.poisoned:
                raise ConnectionError("transport closed after an uncertain reply")
            self.sequence += 1
            body = json.dumps({"jsonrpc": "2.0", "id": self.sequence,
                               "method": method, "params": params}).encode() + b"\n"
            if len(body) > 65_536:
                raise ValueError("protocol frame too large")
            try:
                self.writer.write(body)
                await asyncio.wait_for(self.writer.drain(), 5)
                line = await asyncio.wait_for(self.reader.readline(), 5)
                if not line:
                    raise ConnectionError("runtime disconnected; outcome may be unknown")
                reply = json.loads(line)
                if reply.get("id") != self.sequence or reply.get("jsonrpc") != "2.0":
                    raise ConnectionError("protocol response identity mismatch")
            except (asyncio.CancelledError, TimeoutError, ConnectionError, ValueError, OSError):
                self.poisoned = True
                await self.close()
                raise
            if "error" in reply:
                raise RuntimeError(reply["error"]["message"])
            return reply["result"]

    async def close(self):
        if self.process is None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except OSError:
                pass
            return
        if self.process.returncode is not None:
            await self.process.wait()
            return
        if self.process.stdin:
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), 2)
        except TimeoutError:
            if self.process.returncode is None:
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

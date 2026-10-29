"""Resume a real pending edit and approve it through actual terminal input."""
import array
import asyncio
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path
from uuid import uuid4
import pyte
from synth_code.client import Client

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "target/debug/synth-code-runtime"
sys.path.insert(0, str(ROOT / "scripts"))
import sample_repo

class WorkspaceTerminalTests(unittest.TestCase):
    def test_resume_and_approve_a_pending_edit(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
            root = Path(temporary)
            workspace = sample_repo.materialize(root / "workspace")
            target = workspace / sample_repo.TARGET
            async def pending():
                client = await Client.open(RUNTIME, root / "store")
                try:
                    session = (await client.call("thread/start", {"mutationId": str(uuid4()), "workspace": str(workspace)}))["result"]["id"]
                    file = await client.call("workspace/read", {"threadId": session, "path": sample_repo.TARGET})
                    await client.call("workspace/proposeEdit", {"mutationId": str(uuid4()), "threadId": session,
                        "path": sample_repo.TARGET, "expectedSha256": file["version"]["sha256"], "content": sample_repo.edited(file["text"])})
                    return session
                finally:
                    await client.close()
            session = asyncio.run(pending())
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, array.array("H", [24, 80, 0, 0]))
            process = subprocess.Popen([sys.executable, "-m", "synth_code.app", "--stdio", "--runtime", str(RUNTIME),
                "--store", str(root / "store"), "--workspace", str(workspace), "--resume", session],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                env=os.environ | {"TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"})
            os.close(slave)
            screen = pyte.Screen(80, 24)
            stream = pyte.ByteStream(screen)
            raw = bytearray()
            def pump():
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        return
                    raw.extend(data)
                    stream.feed(data)
            def wait_for(text, line=False):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if (any(x.strip() == text for x in screen.display) if line else text in "\n".join(screen.display)):
                        return
                    pump()
                    if process.poll() is not None:
                        self.fail(f"exited {process.returncode}: {raw[-2000:]!r}")
                self.fail(f"missing {text!r}: {screen.display!r}")
            try:
                wait_for("Awaiting approval ·")
                wait_for(f"Edit · {sample_repo.TARGET}")
                self.assertEqual(sample_repo.changed(workspace), [])
                os.write(master, b"y")
                wait_for(f"• Edited {sample_repo.TARGET}")
                self.assertTrue(target.read_text().endswith(sample_repo.MARKER))
                self.assertEqual(sample_repo.changed(workspace), [sample_repo.TARGET])
                # The direct workspace commands are gone; the agent does file work.
                os.write(master, b"/read README.md\r")
                wait_for("Unknown command · /read")
                os.write(master, b"\x11")
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    pump()
                self.assertEqual(process.wait(timeout=1), 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                os.close(master)
                (ROOT / "artifacts" / f"pty-workspace-{uuid4()}.bin").write_bytes(raw)
            peer = subprocess.run([str(RUNTIME), "stdio", str(root / "store")], input=b"", capture_output=True, timeout=5)
            self.assertEqual(peer.returncode, 0, peer.stderr)

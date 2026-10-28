"""Real PTY + pyte observations against the Python app and Rust executable."""
import array
import fcntl
import os
import pty
import select
import sqlite3
import json
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from uuid import uuid4
from pathlib import Path
import pyte

ROOT = Path(__file__).resolve().parents[2]

class TerminalTests(unittest.TestCase):
    def test_approval_resize_denial_and_cleanup(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, array.array("H", [24, 80, 0, 0]))
            env = os.environ | {"TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1"}
            process = subprocess.Popen([
                sys.executable, "-m", "synth_code.app", "--fixture", "--runtime", str(ROOT / "target/debug/synth-code-runtime"),
                "--store", str(root / "store"), "--workspace", str(workspace),
            ], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
            os.close(slave)
            screen = pyte.Screen(80, 24)
            stream = pyte.ByteStream(screen)
            raw = bytearray()
            def wait_for(text, exact_line=False):
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if (screen.display[-5].strip() == text if exact_line else text in "\n".join(screen.display)):
                        return
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        data = os.read(master, 65536)
                        raw.extend(data)
                        stream.feed(data)
                    if process.poll() is not None:
                        self.fail(f"TUI exited {process.returncode}: {raw[-4000:]!r}")
                self.fail(f"missing {text!r}: {screen.display!r}; {raw[-4000:]!r}")
            try:
                wait_for("Ready")
                os.write(master, b"approved from real PTY\r")
                wait_for("Awaiting approval", exact_line=True)
                fcntl.ioctl(master, termios.TIOCSWINSZ, array.array("H", [12, 40, 0, 0]))
                screen.resize(12, 40)
                os.kill(process.pid, signal.SIGWINCH)
                os.write(master, b"\x1b[Z\r")  # focus previous (Allow), then activate
                wait_for("succeeded", exact_line=True)
                files = list(workspace.iterdir())
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].read_text(), "approved from real PTY")
                os.write(master, b"refused from real PTY\r")
                wait_for("Awaiting approval", exact_line=True)
                os.write(master, b"\r")  # Deny has initial focus
                wait_for("denied", exact_line=True)
                self.assertEqual(list(workspace.iterdir()), files)
                os.write(master, b"\x11")  # Ctrl+Q
                deadline = time.monotonic() + 5
                while process.poll() is None and time.monotonic() < deadline:
                    readable, _, _ = select.select([master], [], [], 0.1)
                    if readable:
                        try:
                            raw.extend(os.read(master, 65536))
                        except OSError:
                            break
                self.assertEqual(process.wait(timeout=1), 0)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                os.close(master)
                (ROOT / "artifacts" / f"pty-smoke-{uuid4()}.bin").write_bytes(raw)
            # Closing the UI must release the backend store lock.
            peer = subprocess.run([str(ROOT / "target/debug/synth-code-runtime"), "stdio", str(root / "store")],
                                  input=b"", capture_output=True, timeout=5)
            self.assertEqual(peer.returncode, 0, peer.stderr)

            with sqlite3.connect(f"file:{root / 'store/journal.sqlite'}?mode=ro",uri=True) as journal:
                commands=[json.loads(row[0])["payload"].get("mutation",{}).get("command",{}) for row in journal.execute("select body from events order by sequence")]
            approvals=[command["allow"] for command in commands if command.get("type")=="approve"]
            self.assertEqual(approvals,[True,False],"Journal must independently confirm the two exact decisions")

"""Drive the real Synth Code TUI in a PTY against a scripted provider and print screens.

Checks the mid-turn rules end to end: action rows, queued effort and message,
permissions applied mid-turn, Esc interrupt, queued message sent next.
"""
import array, asyncio, fcntl, json, os, pty, select, signal, subprocess, sys, tempfile, termios, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pyte

SC = Path(__file__).resolve().parents[1]
RUNTIME = SC / "target/debug/synth-code-runtime"
requests, release = [], threading.Event()
SCRIPT = [
    ("run_command", {"command": "printf hi > ran.txt", "timeout_seconds": 5}),
    ("edit_file", {"path": "note.txt", "expectedSha256": None, "content": "made by agent\n"}),
    "hang",
    "Got the queued note.",
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        step = SCRIPT[min(len(requests) - 1, len(SCRIPT) - 1)]
        if step == "hang":
            release.wait(30)
            return
        if isinstance(step, str):
            delta, finish = {"content": step}, "stop"
        else:
            delta = {"tool_calls": [{"index": 0, "id": f"c{len(requests)}", "type": "function",
                                     "function": {"name": step[0], "arguments": json.dumps(step[1])}}]}
            finish = "tool_calls"
        payload = ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
                   + '\n\ndata: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n\ndata: [DONE]\n\n').encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
root = Path(tempfile.mkdtemp(dir=SC / "artifacts"))
(root / "ws").mkdir()
(root / "store").mkdir(mode=0o700)
runtime_dir = SC / ".st"
runtime_dir.mkdir(exist_ok=True, mode=0o700)
config = root / "provider.json"
config.write_text(json.dumps(dict(endpoint=f"http://127.0.0.1:{server.server_port}/chat/completions",
                                  model="Beam-1.0-501B-A23B", effort="medium", env_file=None,
                                  max_output_tokens=1000, max_requests=20)))
cols, rows = 110, 34
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, array.array("H", [rows, cols, 0, 0]))
env = os.environ | {"TERM": "xterm-256color", "PROMPT_TOOLKIT_NO_CPR": "1", "PYTHONPATH": str(SC / "python/src")}
proc = subprocess.Popen([sys.executable, "-m", "synth_code.app", "--runtime", str(RUNTIME), "--store", str(root / "store"),
                         "--runtime-dir", str(runtime_dir), "--provider-config", str(config), "--workspace", str(root / "ws")],
                        stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
os.close(slave)
screen = pyte.Screen(cols, rows)
stream = pyte.ByteStream(screen)


def pump(seconds=0.3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            try:
                stream.feed(os.read(master, 65536))
            except OSError:
                return


def text():
    return "\n".join(line.rstrip() for line in screen.display)


def wait(needle, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(0.2)
        if needle in text():
            return True
    print(f"!! missing {needle!r}")
    return False


def send(data, settle=0.5):
    os.write(master, data.encode())
    pump(settle)


def snap(title):
    print(f"\n===== {title} =====")
    print("\n".join(line.rstrip() for line in screen.display if line.strip()))


results = {}
try:
    results["ready"] = wait("Ready", 40)
    send("/permissions yolo\r")
    results["yolo"] = wait("Permissions · yolo")
    send("Make the note and check it\r")
    results["ran_row"] = wait("• Ran printf hi > ran.txt")
    results["edit_row"] = wait("• Edited note.txt (+1 −0)")
    time.sleep(1)
    pump()
    send("/effort high\r")
    results["effort_queued"] = wait("Effort · high · queued")
    send("also try wood first\r")
    results["message_queued"] = wait("Message queued · sent when this turn ends")
    results["footer_queued"] = wait("queued: effort → high, 1 message")
    send("/permissions manual\r")
    results["perm_midturn"] = wait("applies from the agent's next action")
    send("/edit note.txt\r")
    results["edit_refused"] = wait("/edit unavailable while the agent is working")
    snap("mid-turn")
    send("\x1b", settle=1.5)
    results["interrupted"] = wait("Interrupted · send a message to continue")
    results["effort_applied"] = wait("Effort · high · applied (was queued)")
    results["queued_sent"] = wait("Queued message sent")
    results["second_turn"] = wait("Got the queued note.")
    snap("after Esc")
    results["files"] = (root / "ws/ran.txt").read_text() == "hi" and (root / "ws/note.txt").read_text() == "made by agent\n"
    results["second_prompt"] = requests[-1]["messages"][-1]["content"] if requests else None
    results["effort_sent"] = requests[-1].get("reasoning_effort") if requests else None
finally:
    release.set()
    send("\x11", settle=1)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    server.shutdown()
print("\n===== results =====")
print(json.dumps(results, indent=1))

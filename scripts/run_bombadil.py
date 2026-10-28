#!/usr/bin/env python3
"""Native Bombadil journey with independent file/journal oracles and hard bounds."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python/src"))
from synth_code.client import Client

VERSION = "0.7.10"
DIGESTS = {
    "bombadil-aarch64-darwin": "3d7866e2e8356390a0f2560853e3c775839f8c8ff648c9552c0105f7f78d1d72",
    "bombadil-aarch64-linux": "67ee0099c287aba90cfee37b317312678277f51a1b2986c49e339b1509509e73",
    "bombadil-x86_64-linux": "6f2664ca7d4067017b4efb26db5c29d6dcfecdc78131f96989d336f12420cc1b",
}

async def prepare(run, ready=False):
    workspace = run / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("before\n")
    if ready:
        process=await asyncio.create_subprocess_exec(str(ROOT/"target/debug/synth-code-runtime"),"stdio",str(run/"store"),"--fixture-crash-before-dispatch",stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,limit=65536)
        client=Client(process)
        await client.call("initialize",{})
    else:
        client = await Client.open(ROOT / "target/debug/synth-code-runtime", run / "store")
    try:
        thread = (await client.call("thread/start", {"mutationId": str(uuid4()), "workspace": str(workspace)}))["result"]["id"]
        file = await client.call("workspace/read", {"threadId": thread, "path": "sample.py"})
        result = await client.call("workspace/proposeEdit", {"mutationId": str(uuid4()), "threadId": thread,
            "path": "sample.py", "expectedSha256": file["version"]["sha256"], "content": "after\n"})
        if ready:
            operation=result["result"]
            try:
                await client.call("synth/approval/respond",{"mutationId":str(uuid4()),"operationId":operation["id"],"requestDigest":operation["request_digest"],"allow":True})
            except ConnectionError:
                assert client.process.returncode==85
            else:
                raise AssertionError("before-dispatch fault did not fire")
            assert (workspace/"sample.py").read_text()=="before\n"
        return thread, result["result"]["id"]
    finally:
        await client.close()

async def verify(run, thread, edit, explore=False):
    client = await Client.open(ROOT / "target/debug/synth-code-runtime", run / "store")
    try:
        page = await client.call("thread/read", {"threadId": thread})
        operations = list(page["operations"])
        while page["nextCursor"] is not None:
            page=await client.call("thread/read",{"threadId":thread,"after":page["nextCursor"],"atSequence":page["sequence"]})
            operations.extend(page["operations"])
        assert len(operations)>=2 if explore else len(operations)==2,operations
        assert next(op for op in operations if op["id"] == edit)["status"] == "succeeded"
        denied = next(op for op in operations if op["status"]=="denied")
        assert denied["dispatched_epoch"] is None
        assert (run / "workspace/sample.py").read_text() == "after\n"
        if explore:
            import sqlite3
            with sqlite3.connect(run/"store/journal.sqlite") as db:
                accepted=[json.loads(row[0])["payload"] for row in db.execute("SELECT body FROM events ORDER BY sequence")]
            approvals={e["mutation"]["command"]["operation"] for e in accepted if e["type"]=="accepted" and e["mutation"]["command"]["type"]=="approve" and e["mutation"]["command"]["allow"]}
            expected={"sample.py"}
            for op in operations:
                if op["id"]==edit:continue
                assert not op.get("edit") and not op.get("shell"),op
                witness=run/"workspace"/f"fixture-{op['id']}.txt"
                if op["status"]=="succeeded":assert witness.is_file(),op
                if witness.exists():
                    assert op["id"] in approvals and op["status"] in {"succeeded","unknown"},op
                    assert witness.read_text()==op["text"]
                    expected.add(witness.name)
                else:assert op["status"]!="succeeded",op
            assert {p.name for p in (run/"workspace").iterdir()}==expected
        else:
            assert sorted(p.name for p in (run / "workspace").iterdir()) == ["sample.py"]
        return {"operations":operations,"sequence":page["sequence"],"thread":thread}
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bombadil", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reproduce", type=Path)
    parser.add_argument("--inject-bad-expectation", action="store_true")
    parser.add_argument("--resume-ready", action="store_true")
    parser.add_argument("--explore", action="store_true")
    args = parser.parse_args()
    executable = args.bombadil.resolve()
    expected = DIGESTS.get(executable.name)
    if expected is None or hashlib.sha256(executable.read_bytes()).hexdigest() != expected:
        parser.error("Bombadil binary must match the pinned official v0.7.10 asset digest")
    launcher = Path(os.environ.get("SYNTH_BUILD_LAUNCHER", str(ROOT.parent / "synth-dev/local_dev/scripts/core_dev_build.py")))
    if not launcher.is_file() and "SYNTH_BUILD_LAUNCHER" not in os.environ:
        launcher=ROOT/"scripts/public_build_guard.py"
    if not launcher.is_file():
        parser.error("maintained process/admission launcher required")
    run = (args.output or ROOT / "artifacts/terminal" / str(uuid4())).resolve()
    if not run.is_relative_to(ROOT):
        parser.error("output must remain within this checkout")
    run.mkdir(parents=True, exist_ok=False)
    thread, edit = asyncio.run(prepare(run,args.resume_ready))
    specification = ROOT / "tests/terminal/specs" / ("explore.ts" if args.explore else "workspace.ts")
    command = [str(executable), "terminal", "test", "--specification", str(specification), "--columns", "80",
               "--rows", "24", "--scrollback-lines-max", "100", "--quiescence-timeout-ms", "200",
               "--time-limit", "30s" if args.explore else "20s", "--exit-on-violation", "--output-path", str(run / "trace")]
    if args.reproduce:
        command += ["--reproduce", str(args.reproduce.resolve())]
    command += ["--", sys.executable, "-m", "synth_code.app", "--fixture", "--runtime", str(ROOT / "target/debug/synth-code-runtime"),
                "--store", str(run / "store"), "--workspace", str(run / "workspace"), "--resume", thread]
    wrapped = [sys.executable, str(launcher), "--light", "--label", "synth-code-bombadil", "--", *command]
    env = {k: v for k, v in os.environ.items() if not any(secret in k.upper() for secret in ("TOKEN", "SECRET", "PASSWORD", "API_KEY"))}
    env.update(PYTHONPATH=str(ROOT / "python/src"), TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1")
    receipt = {"bombadil_version": VERSION, "bombadil_sha256": expected,
               "binary_sha256": hashlib.sha256((ROOT / "target/debug/synth-code-runtime").read_bytes()).hexdigest(),
               "specification_sha256": hashlib.sha256(specification.read_bytes()).hexdigest(),
               "thread": thread, "edit": edit, "command": command, "status": "running", "provider_calls": 0}
    receipt_path = run / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(run, flush=True)
    process = None
    try:
        with (run / "runner.log").open("w") as log:
            process = subprocess.Popen(wrapped, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 40
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise TimeoutError("terminal campaign watchdog expired")
                total = sum(p.stat().st_size for p in run.rglob("*") if p.is_file())
                if total > 100 * 1024**2:
                    raise RuntimeError("terminal campaign exceeded 100 MiB")
                time.sleep(0.1)
            receipt["exit_code"] = process.returncode
            if process.returncode:
                raise RuntimeError(f"Bombadil/launcher exited {process.returncode}")
        rows = [json.loads(line) for line in (run / "trace/trace.jsonl").read_text().splitlines()]
        assert len(rows) > 8, "insufficient terminal observations"
        assert not any(row["violations"] for row in rows), "terminal property violation"
        actions = [row["action"] for row in rows if row.get("action")]
        typed = [a["TypeText"]["text"] for a in actions if "TypeText" in a]
        assert "/read sample.py\r" in typed and "/search after\r" in typed and "\x11" in typed, "missing mandatory input milestones"
        resized = [a["Resize"]["size"] for a in actions if "Resize" in a]
        assert {"rows": 12, "columns": 40} in resized and {"rows": 24, "columns": 80} in resized, "missing resize milestones"
        states = [row["state"] for row in rows]
        assert any(s.get("exit_status") == {"code": 0, "signal": None} for s in states), "missing clean frontend exit"
        page = asyncio.run(verify(run, thread, edit,args.explore))
        (run / "oracle.json").write_text(json.dumps(page, indent=2) + "\n")
        receipt["cleanup_complete"] = True
        receipt["observations"] = len(rows)
        receipt["actions"] = len(actions)
        if args.inject_bad_expectation:
            assert (run / "workspace/sample.py").read_text() == "deliberately wrong", "injected oracle failure"
        receipt.update(status="passed", observations=len(rows), actions=len(actions), cleanup_complete=True)
    except BaseException as error:
        receipt.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print("Native terminal journey, durable receipts, file witness and cleanup passed", flush=True)

if __name__ == "__main__":
    main()

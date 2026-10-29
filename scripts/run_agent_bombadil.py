#!/usr/bin/env python3
"""Exercise the real agent TUI, SSE adapter, daemon and effects through Bombadil."""
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

from fixture_provider import Fixture
from run_bombadil import DIGESTS, ROOT
from synth_code.client import Client
from synth_code.daemon import address

async def start(socket, workspace):
    for _ in range(200):
        try:
            client=await Client.attach(socket)
            break
        except (FileNotFoundError,ConnectionRefusedError):
            await asyncio.sleep(0.025)
    else:
        raise RuntimeError("daemon startup timeout")
    try:
        return (await client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(workspace)}))["result"]["id"]
    finally:
        await client.close()

async def oracle(socket,thread,workspace):
    client=await Client.attach(socket)
    try:
        page=await client.call("turn/list",{"threadId":thread})
        assert len(page["turns"])==1 and page["nextCursor"] is None,page
        turn=await client.call("turn/read",{"turnId":page["turns"][0]["id"]})
        assert turn["phase"]=="completed",turn
        operations=[]
        for id in turn["operations"]:
            operations.append((await client.call("synth/operation/read",{"operationId":id}))["operation"])
        assert len(operations)==2 and all(o["status"]=="succeeded" for o in operations),operations
        assert (workspace/"sample.py").read_text()=="after\n"
        assert (workspace/"checked.txt").read_text()=="checked"
        return dict(turn=turn,operations=operations,daemon_survived_frontend_exit=True)
    finally:
        await client.close()

async def stop(socket):
    client=await Client.attach(socket)
    try:
        await client.call("synth/shutdown",{})
    finally:
        await client.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--bombadil",type=Path,required=True)
    parser.add_argument("--inject-bad-expectation",action="store_true")
    parser.add_argument("--reproduce",type=Path)
    parser.add_argument("--installed-executable",type=Path)
    args=parser.parse_args()
    bombadil=args.bombadil.resolve()
    assert hashlib.sha256(bombadil.read_bytes()).hexdigest()==DIGESTS[bombadil.name]
    run=ROOT/"artifacts/terminal-agent"/str(uuid4())
    workspace=run/"workspace"
    workspace.mkdir(parents=True)
    (workspace/"sample.py").write_text("before\n")
    runtime_dir=ROOT/".state/run"
    socket=address(run/"store",runtime_dir)
    fixture=Fixture()
    configuration=run/"provider.json"
    configuration.write_text(json.dumps(fixture.config()))
    runtime=ROOT/"target/debug/synth-code-runtime"
    if args.installed_executable:
        executable=args.installed_executable.resolve()
        environment=os.environ.copy();environment.pop("PYTHONPATH",None)
        runtime=Path(subprocess.check_output([str(executable.parent/"python"),"-c","from synth_code.cli import native_runtime;print(native_runtime())"],env=environment,text=True).strip())
    log=(run/"daemon.log").open("w")
    daemon=subprocess.Popen([str(runtime),"serve",str(run/"store"),"--socket",str(socket),"--provider-config",str(configuration)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=log,start_new_session=True)
    receipt={"status":"running","provider_calls_paid":0,"binary_sha256":hashlib.sha256(runtime.read_bytes()).hexdigest(),"bombadil_sha256":DIGESTS[bombadil.name]}
    process=None
    print(run,flush=True)
    try:
        thread=asyncio.run(start(socket,workspace))
        spec=ROOT/"tests/terminal/specs/agent.ts"
        receipt.update(thread=thread,specification_sha256=hashlib.sha256(spec.read_bytes()).hexdigest())
        command=[str(bombadil),"terminal","test","--specification",str(spec),"--columns","80","--rows","24","--scrollback-lines-max","100","--quiescence-timeout-ms","200","--time-limit","25s","--exit-on-violation","--output-path",str(run/"trace")]
        if args.reproduce:
            command += ["--reproduce",str(args.reproduce.resolve())]
        frontend=[str(args.installed_executable.resolve())] if args.installed_executable else [sys.executable,"-m","synth_code.app","--runtime",str(runtime)]
        command += ["--",*frontend,"--store",str(run/"store"),"--runtime-dir",str(runtime_dir),"--provider-config",str(configuration),"--resume",thread]
        launcher=Path(os.environ.get("SYNTH_BUILD_LAUNCHER",str(ROOT.parent/"synth-dev/local_dev/scripts/core_dev_build.py")))
        if not launcher.is_file() and "SYNTH_BUILD_LAUNCHER" not in os.environ:
            launcher=ROOT/"scripts/public_build_guard.py"
        wrapped=[sys.executable,str(launcher),"--light","--label","synth-code-agent-terminal","--",*command]
        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ("TOKEN","SECRET","PASSWORD","API_KEY"))}
        env.pop("PYTHONPATH",None)
        env.update(TERM="xterm-256color",PROMPT_TOOLKIT_NO_CPR="1",NO_COLOR="1")
        if not args.installed_executable:env["PYTHONPATH"]=str(ROOT/"python/src")
        with (run/"runner.log").open("w") as output:
            process=subprocess.Popen(wrapped,cwd=ROOT,env=env,stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
            deadline=time.monotonic()+40
            while process.poll() is None:
                if time.monotonic()>deadline:raise TimeoutError("terminal watchdog expired")
                if sum(p.stat().st_size for p in run.rglob("*") if p.is_file())>100*1024**2:raise RuntimeError("campaign exceeded 100 MiB")
                time.sleep(0.1)
        assert process.returncode==0,f"Bombadil exited {process.returncode}"
        rows=[json.loads(line) for line in (run/"trace/trace.jsonl").read_text().splitlines()]
        assert not any(r["violations"] for r in rows),"terminal invariant failed"
        assert any(r["state"].get("exit_status")=={"code":0,"signal":None} for r in rows),"clean frontend exit missing"
        actions=[r["action"] for r in rows if r.get("action")]
        typed=[a["TypeText"]["text"] for a in actions if "TypeText" in a]
        assert "Update sample.py and check it\r" in typed and typed.count("\x1b[Z\r")==2 and "\x11" in typed,"missing task/approval/detach milestones"
        sizes=[a["Resize"]["size"] for a in actions if "Resize" in a]
        assert {"rows":12,"columns":40} in sizes and {"rows":24,"columns":80} in sizes,"missing resize"
        assert daemon.poll() is None,"daemon must survive frontend detach"
        result=asyncio.run(oracle(socket,thread,workspace))
        assert len(fixture.requests)==5,"unexpected provider attempt count"  # 4 scripted + goal check
        (run/"oracle.json").write_text(json.dumps(result,indent=2)+'\n')
        (run/"fixture-requests.json").write_text(json.dumps(fixture.requests,indent=2)+'\n')
        if args.inject_bad_expectation:assert (workspace/"sample.py").read_text()=="wrong","injected independent oracle failure"
        receipt.update(status="passed",observations=len(rows),actions=len(actions),fixture_requests=len(fixture.requests))
    except BaseException as error:
        receipt.update(status="failed",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=5)
        if daemon.poll() is None:
            try:asyncio.run(stop(socket))
            except (OSError,RuntimeError):daemon.terminate()
            try:daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:daemon.kill();daemon.wait(timeout=5)
        fixture.close();log.close()
        probe=subprocess.run([str(runtime),"stdio",str(run/"store")],input=b"",capture_output=True,timeout=5)
        receipt["cleanup_complete"]=daemon.returncode==0 and probe.returncode==0 and not socket.exists()
        (run/"receipt.json").write_text(json.dumps(receipt,indent=2)+'\n')
        assert receipt["cleanup_complete"],"daemon/store cleanup failed"
    print("Native agent journey, detach, durable effects and cleanup passed",flush=True)

if __name__=="__main__":main()

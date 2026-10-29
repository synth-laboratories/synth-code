#!/usr/bin/env python3
"""Installed same-schema upgrade/rollback proof with an unpublished version fixture.
Both wheels must be built with `scripts/build.py build --release --test-hooks`: the
proof proposes an edit outside a turn and binds a local scripted provider."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

from run_bombadil import ROOT
from fixture_provider import Fixture
from synth_code.client import Client
from synth_code.daemon import address, connect

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--old-wheel",required=True,type=Path)
    parser.add_argument("--new-wheel",required=True,type=Path)
    args=parser.parse_args()
    run=ROOT/"artifacts/upgrade"/str(uuid4());run.mkdir(parents=True)
    workspace=run/"workspace";workspace.mkdir();(workspace/"sample.py").write_text("before\n")
    env=os.environ|{"UV_TOOL_DIR":str(run/"tools"),"UV_TOOL_BIN_DIR":str(run/"bin"),"UV_CACHE_DIR":str(ROOT/".cache/uv")}
    env.pop("PYTHONPATH",None)
    uv=shutil.which("uv")
    entry=run/"bin/synth-code"
    def install(wheel,expected=0):
        command=[uv,"tool","install","--force","--reinstall","--refresh","--no-build","--python",sys.executable,"--from",str(wheel.resolve()),"synth-code"]
        result=subprocess.run(command,cwd=ROOT,env=env,capture_output=True,text=True,timeout=60)
        with (run/"install.log").open("a") as log:log.write(result.stdout+result.stderr)
        assert (result.returncode==0)==(expected==0),result.stderr
    def native():
        python=entry.resolve().parent/"python"
        return Path(subprocess.check_output([str(python),"-c","from synth_code.cli import native_runtime;print(native_runtime())"],env=env,text=True).strip())
    def version():return subprocess.check_output([str(entry),"--version"],env=env,text=True).strip()
    fixture=Fixture();config=run/"provider.json";config.write_text(json.dumps(fixture.config()))
    runtime_dir=ROOT/".state/run";store=run/"store";socket=address(store,runtime_dir)
    daemon=None;client=None;receipt={"status":"running","provider_generation_calls":0,"scope":"same-schema compatibility with unpublished next-version fixture only"}
    print(run,flush=True)
    try:
        install(args.old_wheel)
        old_runtime=native();old_hash=hashlib.sha256(old_runtime.read_bytes()).hexdigest()
        client=await connect(old_runtime,store,config=config,runtime_dir=runtime_dir)
        old_status=await client.call("synth/status",{});old_pid=old_status["ownerPid"]
        session=(await client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(workspace)}))["result"]["id"]
        file=await client.call("workspace/read",{"threadId":session,"path":"sample.py"})
        operation=(await client.call("workspace/proposeEdit",{"mutationId":str(uuid4()),"threadId":session,"path":"sample.py","expectedSha256":file["version"]["sha256"],"content":"after\n"}))["result"]
        await client.close();client=None
        install(args.new_wheel)
        new_runtime=native();new_hash=hashlib.sha256(new_runtime.read_bytes()).hexdigest();assert new_hash!=old_hash
        assert "nightly" in version()
        try:
            unexpected=await connect(new_runtime,store,config=config,runtime_dir=runtime_dir)
        except RuntimeError as error:assert "different native bytes" in str(error)
        else:await unexpected.close();raise AssertionError("upgrade replaced active owner")
        client=await Client.attach(socket)
        assert (await client.call("synth/status",{}))["ownerPid"]==old_pid
        assert (workspace/"sample.py").read_text()=="before\n"
        await client.call("synth/shutdown",{});await client.close();client=None
        for _ in range(200):
            if not socket.exists():break
            await asyncio.sleep(0.05)
        assert not socket.exists(),"old owner failed to drain"
        client=await connect(new_runtime,store,config=config,runtime_dir=runtime_dir)
        op=(await client.call("synth/operation/read",{"operationId":operation["id"]}))["operation"]
        assert op["status"]=="awaiting_approval" and op["request_digest"]==operation["request_digest"]
        decision={"mutationId":str(uuid4()),"operationId":op["id"],"requestDigest":op["request_digest"],"allow":True}
        result=await client.call("synth/approval/respond",decision)
        assert result["operations"][0]["status"]=="succeeded"
        stamp=(workspace/"sample.py").stat().st_mtime_ns
        await client.call("synth/approval/respond",decision)
        assert (workspace/"sample.py").stat().st_mtime_ns==stamp
        await client.call("synth/shutdown",{});await client.close();client=None
        for _ in range(200):
            if not socket.exists():break
            await asyncio.sleep(0.05)
        assert not socket.exists()
        install(args.old_wheel)
        assert "0.0.1" in version() and "stable" in version()
        client=await connect(native(),store,config=config,runtime_dir=runtime_dir)
        await client.call("synth/approval/respond",decision)
        assert (workspace/"sample.py").stat().st_mtime_ns==stamp
        assert (workspace/"sample.py").read_text()=="after\n"
        assert (await client.call("synth/operation/read",{"operationId":op["id"]}))["operation"]["status"]=="succeeded"
        before=version();install(run/"missing.whl",expected=1);assert version()==before
        assert len(fixture.requests)==0
        receipt.update(status="passed",old_sha256=old_hash,new_sha256=new_hash,session=session,operation=op["id"],old_wheel_sha256=hashlib.sha256(args.old_wheel.read_bytes()).hexdigest(),fixture_wheel_sha256=hashlib.sha256(args.new_wheel.read_bytes()).hexdigest(),active_owner_refused=True,pending_approval_preserved=True,receipt_replayed_without_effect=True,rollback_same_schema=True,failed_update_preserved_install=True)
    except BaseException as error:
        receipt.update(status="failed",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        if client:
            try:await client.call("synth/shutdown",{})
            finally:await client.close()
        elif socket.exists():
            cleanup=await Client.attach(socket)
            try:await cleanup.call("synth/shutdown",{})
            finally:await cleanup.close()
        for _ in range(200):
            if not socket.exists():break
            await asyncio.sleep(0.05)
        fixture.close()
        receipt["cleanup_complete"]=not socket.exists()
        (run/"receipt.json").write_text(json.dumps(receipt,indent=2)+'\n')
        assert receipt["cleanup_complete"]
    print("Installed upgrade, owner refusal, replay, rollback and failed update passed",flush=True)

if __name__=="__main__":asyncio.run(main())

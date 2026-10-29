#!/usr/bin/env python3
"""Bounded real-provider coding smoke on the pinned sample repository. A real
bug is planted and committed; the model gets the bug report only. It may read,
edit and run commands freely inside the workspace (edits to tests or outside
the workspace are denied; commands that reach outside are recorded). The result is judged by the
project's own test suite and the diff, not by the exact edit."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4
from run_bombadil import ROOT
import sample_repo
from synth_code.client import Client

BUG_FILE="src/tomli/_re.py"
REPORT=("Bug report: datetimes with fractional seconds parse wrong. "
        "tomli.loads('t = 1979-05-27T00:32:00.5Z') gives microsecond=5, but .5 seconds is 500000 microseconds. "
        "Find and fix the bug in the library. Do not change the tests. "
        f"Run the project's test suite to confirm: {sample_repo.CHECK}")

def plant(workspace):
    path=workspace/BUG_FILE;text=path.read_text()
    assert text.count('micros_str.ljust(6, "0")')==2
    path.write_text(text.replace('micros_str.ljust(6, "0")','micros_str.rjust(6, "0")'))
    git=["git","-C",str(workspace),"-c","user.name=Synth Code smoke","-c","user.email=smoke@invalid"]
    subprocess.run(git+["commit","-q","-am","Format microseconds"],check=True)

def suite(workspace):
    return subprocess.run(sample_repo.CHECK,shell=True,cwd=workspace,capture_output=True,text=True,timeout=120)

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--runtime",type=Path,default=ROOT/"target/debug/synth-code-runtime")
    parser.add_argument("--env-file",required=True,type=Path)
    parser.add_argument("--model",default="Beam-1.0-501B-A23B")
    parser.add_argument("--max-requests",type=int,default=24)
    parser.add_argument("--max-output-tokens",type=int,default=8192)
    # Lowered, the runtime must compact mid-task and still finish it (SYN-5244 live check).
    parser.add_argument("--compact-request-bytes",type=int)
    args=parser.parse_args()
    assert 1<=args.max_requests<=40
    run=ROOT/"artifacts/provider"/str(uuid4())
    workspace=sample_repo.materialize(run/"workspace")
    plant(workspace)
    assert suite(workspace).returncode!=0,"planted bug did not fail the suite"
    config={"endpoint":"https://api.reflection.ai/openai/v1/chat/completions","model":args.model,"effort":"medium","env_file":str(args.env_file.resolve()),"max_output_tokens":args.max_output_tokens,"max_requests":args.max_requests}
    if args.compact_request_bytes:config["compact_request_bytes"]=args.compact_request_bytes
    file=run/"provider.json";file.write_text(json.dumps(config))
    receipt={"status":"running","runtime_sha256":hashlib.sha256(args.runtime.read_bytes()).hexdigest(),"requested_model":args.model,"maximum_requests":args.max_requests,"maximum_output_tokens":args.max_output_tokens*args.max_requests,"maximum_charge_usd":0,"billing_basis":"matched existing key, Reflection Special Access private beta / free daily allowance; artifacts/release/reflection-access.json","repository":{"bundle":sample_repo.BUNDLE.name,"commit":sample_repo.COMMIT,"planted":BUG_FILE},"task":REPORT,"provider_config_sha256":hashlib.sha256(file.read_bytes()).hexdigest(),"denied":[]}
    print(run,flush=True)
    client=await Client.open(args.runtime,run/"store",config=file)
    compactions={};deadline=time.monotonic()+600;started=time.monotonic();seen=set();last_activity=time.monotonic();previous=None
    try:
        session=(await client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(workspace)}))["result"]["id"]
        prompt=REPORT
        turn=(await client.call("turn/start",{"mutationId":str(uuid4()),"threadId":session,"prompt":prompt}))["id"]
        receipt.update(session=session,turn=turn)
        while True:
            view=await client.call("turn/read",{"turnId":turn})
            # turn/read shows the latest 8 messages, so compactions are recorded as they appear.
            for message in view["messages"]:
                if message.get("role")=="compaction":compactions[message["index"]]=len(message.get("text",""))
            key=(view["phase"],view["attempts"],tuple(view["operations"]),view["liveText"])
            if key!=previous:last_activity=time.monotonic();previous=key
            progress={"phase":view["phase"],"attempts":view["attempts"],"operations":len(view["operations"]),"latest_event_age_seconds":round(time.monotonic()-last_activity,1),"elapsed_seconds":round(time.monotonic()-started,1),"observed_monotonic":time.monotonic(),"models":view.get("models",[])}
            (run/"progress.json").write_text(json.dumps(progress)+'\n')
            print(json.dumps(progress),flush=True)
            for identifier in view["operations"]:
                op=(await client.call("synth/operation/read",{"operationId":identifier}))["operation"]
                if op["status"]=="awaiting_approval" and identifier not in seen:
                    seen.add(identifier)
                    reason=None
                    if op.get("outside"):receipt.setdefault("outside",[]).append({"operation":identifier,"paths":op["outside"]})
                    if op.get("edit") and op.get("outside"):reason=f"edits outside the workspace: {op['outside']}"
                    elif op.get("edit") and op["edit"]["path"].startswith("tests/"):reason="edits a test"
                    elif not (op.get("edit") or op.get("shell")):reason="unknown effect"
                    if reason:receipt["denied"].append({"operation":identifier,"reason":reason})
                    await client.call("synth/approval/respond",{"mutationId":str(uuid4()),"operationId":identifier,"requestDigest":op["request_digest"],"allow":reason is None})
            if view["phase"] not in {"running","waiting"}:
                receipt["view"]=view
                assert view["phase"]=="completed",view.get("error")
                break
            if time.monotonic()>deadline:raise TimeoutError("provider smoke watchdog expired")
            await asyncio.sleep(2)
        history=await client.call("thread/read",{"threadId":session})
        changed=sample_repo.changed(workspace)
        assert BUG_FILE in changed,changed
        assert not any(path.startswith("tests/") for path in changed),changed
        result=suite(workspace)
        assert result.returncode==0,result.stderr[-2000:]
        diff=subprocess.run(["git","-C",str(workspace),"diff"],capture_output=True,text=True).stdout
        (run/"oracle.json").write_text(json.dumps({"history":history,"changed":changed,"diff":diff,"suite_stderr":result.stderr[-4000:]},indent=2)+'\n')
        receipt["compactions"]=[{"index":index,"summary_preview_chars":chars} for index,chars in sorted(compactions.items())]
        if args.compact_request_bytes:
            assert compactions,"no compaction below the lowered request limit"
        receipt.update(status="passed",provider_requests=view["attempts"],observed_models=view.get("models"),usage=view["usage"],charge_usd=0,charge_final=True)
    except BaseException as error:
        receipt.update(status="failed",error=f"{type(error).__name__}: {error}")
        raise
    finally:
        try:
            if receipt.get("turn"):
                state=await client.call("turn/read",{"turnId":receipt["turn"]})
                receipt["view"]=state
                if state["phase"] in {"running","waiting"}:await client.call("turn/interrupt",{"turnId":receipt["turn"]})
        finally:
            await client.close()
            receipt["cleanup_complete"]=client.process.returncode==0
            (run/"receipt.json").write_text(json.dumps(receipt,indent=2)+'\n')
    print("Real Reflection coding smoke passed",flush=True)
if __name__=="__main__":asyncio.run(main())

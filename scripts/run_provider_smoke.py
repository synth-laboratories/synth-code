#!/usr/bin/env python3
"""Bounded real-provider coding smoke, with narrowly checked fixture approvals."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import sys
import time
from uuid import uuid4
from run_bombadil import ROOT
from synth_code.client import Client

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--runtime",type=Path,default=ROOT/"target/debug/synth-code-runtime")
    parser.add_argument("--env-file",required=True,type=Path)
    parser.add_argument("--model",default="Beam-1.0-501B-A23B")
    parser.add_argument("--max-requests",type=int,default=8)
    args=parser.parse_args()
    assert 1<=args.max_requests<=8
    run=ROOT/"artifacts/provider"/str(uuid4());workspace=run/"workspace";workspace.mkdir(parents=True)
    before="def add(a, b):\n    return a - b\n";after="def add(a, b):\n    return a + b\n"
    (workspace/"calc.py").write_text(before)
    (workspace/"test_calc.py").write_text("import unittest\nfrom calc import add\nclass Checks(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
    command=shlex.join([sys.executable,"-m","unittest","-v"])
    config={"endpoint":"https://api.reflection.ai/openai/v1/chat/completions","model":args.model,"effort":"medium","env_file":str(args.env_file.resolve()),"max_output_tokens":4096,"max_requests":args.max_requests}
    file=run/"provider.json";file.write_text(json.dumps(config))
    receipt={"status":"running","runtime_sha256":hashlib.sha256(args.runtime.read_bytes()).hexdigest(),"requested_model":args.model,"maximum_requests":args.max_requests,"maximum_output_tokens":4096*args.max_requests,"maximum_charge_usd":0,"billing_basis":"matched existing key, Reflection Special Access private beta / free daily allowance; artifacts/release/reflection-access.json","expected_command":command,"provider_config_sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
    print(run,flush=True)
    client=await Client.open(args.runtime,run/"store",config=file)
    deadline=time.monotonic()+240;started=time.monotonic();seen=set();last_activity=time.monotonic();previous=None
    try:
        session=(await client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(workspace)}))["result"]["id"]
        prompt=f"Fix calc.py's add function. First inspect calc.py and test_calc.py with read_file. Use edit_file to replace calc.py with exactly this UTF-8 content (including final newline):\n{after}\nThen request run_command with exactly this command and timeout_seconds 30: {command}\nDo not change tests, create other files or use other commands. Both effects need approval; wait for the tool results. Finish with a concise factual test result."
        turn=(await client.call("turn/start",{"mutationId":str(uuid4()),"threadId":session,"prompt":prompt}))["id"]
        receipt.update(session=session,turn=turn)
        while True:
            view=await client.call("turn/read",{"turnId":turn})
            key=(view["phase"],view["attempts"],tuple(view["operations"]),view["liveText"])
            if key!=previous:last_activity=time.monotonic();previous=key
            progress={"phase":view["phase"],"attempts":view["attempts"],"operations":len(view["operations"]),"latest_event_age_seconds":round(time.monotonic()-last_activity,1),"elapsed_seconds":round(time.monotonic()-started,1),"observed_monotonic":time.monotonic(),"models":view.get("models",[])}
            (run/"progress.json").write_text(json.dumps(progress)+'\n')
            print(json.dumps(progress),flush=True)
            for identifier in view["operations"]:
                op=(await client.call("synth/operation/read",{"operationId":identifier}))["operation"]
                if op["status"]=="awaiting_approval" and identifier not in seen:
                    seen.add(identifier)
                    if op.get("edit"):
                        edit=op["edit"]
                        allow=edit["path"]=="calc.py" and edit["before"]==before and edit["content"]==after
                    elif op.get("shell"):
                        shell=op["shell"]
                        allow=shell["command"]==command and shell["timeout_seconds"]<=30
                    else:allow=False
                    await client.call("synth/approval/respond",{"mutationId":str(uuid4()),"operationId":identifier,"requestDigest":op["request_digest"],"allow":allow})
                    if not allow:raise AssertionError("model requested an effect outside the exact smoke fixture")
            if view["phase"] not in {"running","waiting"}:
                receipt["view"]=view
                assert view["phase"]=="completed",view.get("error")
                break
            if time.monotonic()>deadline:raise TimeoutError("provider smoke watchdog expired")
            await asyncio.sleep(2)
        history=await client.call("thread/read",{"threadId":session});operations=history["operations"]
        assert len(operations)==2 and all(o["status"]=="succeeded" for o in operations),operations
        assert (workspace/"calc.py").read_text()==after
        check=next(o for o in operations if o.get("shell"))
        assert check["outcome"]["exit_code"]==0
        output=await client.call("synth/operation/output",{"operationId":check["id"],"stream":"stderr","after":0})
        assert "OK" in output["text"],output
        (run/"oracle.json").write_text(json.dumps({"history":history,"test_output":output},indent=2)+'\n')
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

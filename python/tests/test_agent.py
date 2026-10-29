"""Real socket daemon + scripted Reflection SSE; no paid/provider dependency."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from synth_code.client import Client

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "target/debug/synth-code-runtime"
STARTUP_TIMEOUT = 40

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "artifacts")
        self.process = self.provider = self.client = None
        self.addAsyncCleanup(self.cleanup_owned)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.txt").write_text("before\n")
        self.requests = []
        self.script = []
        # Requests answering the runtime's goal check; replied from check_script, else "Done.".
        self.checks = []
        self.check_script = []
        # Compaction requests; replied from summary_script, else a fixed summary.
        self.compactions = []
        self.summary_script = []
        self.provider = await asyncio.start_server(self.http, "127.0.0.1", 0)
        port = self.provider.sockets[0].getsockname()[1]
        self.config = self.root / "provider.json"
        self.config.write_text(json.dumps(dict(endpoint=f"http://127.0.0.1:{port}/chat/completions",
            model="Beam-1.0-501B-A23B", effort="medium", env_file=None,
            max_output_tokens=1000, max_requests=8)))
        private = self.root / "socket"
        private.mkdir(mode=0o700)
        self.socket = private / "agent.sock"
        await self.boot()
        self.client = await Client.attach(self.socket)
        result = await self.client.call("thread/start", {"mutationId":str(uuid4()),"workspace":str(self.workspace)})
        self.session = result["result"]["id"]

    async def boot(self):
        self.process = await asyncio.create_subprocess_exec(str(getattr(self, "runtime_path", RUNTIME)),"serve",str(self.root/"store"),
            "--socket",str(self.socket),"--provider-config",str(self.config),stderr=asyncio.subprocess.PIPE)
        try:
            async with asyncio.timeout(STARTUP_TIMEOUT):
                while True:
                    if self.socket.exists():
                        try:
                            c=await Client.attach(self.socket)
                            await c.close()
                            return
                        except (FileNotFoundError, ConnectionRefusedError):
                            pass
                    if self.process.returncode is not None:
                        raise AssertionError((await self.process.stderr.read()).decode())
                    await asyncio.sleep(0.025)
        except TimeoutError as error:
            raise AssertionError("daemon startup timeout") from error

    async def cleanup_owned(self):
        try:
            if self.client:
                await self.client.close()
        finally:
            try:
                if self.process:
                    if self.process.returncode is None:
                        self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 10)
                    except TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                    errors=(await self.process.stderr.read()).decode()
                    self.assertEqual(self.process.returncode, 0, errors)
            finally:
                if self.provider:
                    self.provider.close()
                    await self.provider.wait_closed()
                self.temp.cleanup()

    async def http(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(l.split(b":",1)[1] for l in header.split(b"\r\n") if l.lower().startswith(b"content-length:")))
            request = json.loads(await reader.readexactly(length))
            last = request["messages"][-1]
            if last["role"] == "user" and str(last.get("content","")).startswith("Synth Code check before finishing"):
                self.checks.append(request)
                action = self.check_script.pop(0) if self.check_script else "Done."
            elif last["role"] == "user" and str(last.get("content","")).startswith("Synth Code context checkpoint"):
                self.compactions.append(request)
                action = self.summary_script.pop(0) if self.summary_script else "SUMMARY: read big.txt; next step is to finish."
            else:
                self.requests.append(request)
                action = self.script.pop(0)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            if action == "hang":
                await writer.drain()
                await reader.read()
                return
            def event(delta, finish=None):
                return "data: " + json.dumps({"choices":[{"index":0,"delta":delta,"finish_reason":finish}]}) + "\n\n"
            payload = event({"reasoning_content":"PRIVATE_REASONING_SENTINEL"})
            if isinstance(action, tuple):
                name,args=action
                raw=json.dumps(args)
                payload += event({"tool_calls":[{"index":0,"id":"call-"+str(len(self.requests)),"type":"function","function":{"name":name,"arguments":raw[:3]}}]})
                payload += event({"tool_calls":[{"index":0,"function":{"arguments":raw[3:]}}]}, "tool_calls")
            else:
                payload += event({"content":action},"stop")
            payload += 'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":10}}\n\ndata: [DONE]\n\n'
            # Deliberately split JSON, UTF-8 and SSE records across HTTP chunks.
            data=payload.encode()
            for start in range(0,len(data),7):
                writer.write(data[start:start+7])
                await writer.drain()
                await asyncio.sleep(0)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def start(self):
        return await self.client.call("turn/start", {"mutationId":str(uuid4()), "threadId":self.session,"prompt":"Update note and check it"})

    async def wait_view(self, turn, predicate, *, polls=400):
        for _ in range(polls):
            view=await self.client.call("turn/read", {"turnId":turn})
            self.assertNotIn("PRIVATE_REASONING_SENTINEL",json.dumps(view))
            if predicate(view):
                return view
            if view["phase"] == "failed":
                raise AssertionError(view)
            await asyncio.sleep(0.01)
        raise AssertionError(view)

    async def approve(self, identifier, allow=True):
        op=(await self.client.call("synth/operation/read",{"operationId":identifier}))["operation"]
        return await self.client.call("synth/approval/respond",{"mutationId":str(uuid4()),"operationId":identifier,"requestDigest":op["request_digest"],"allow":allow})

    async def test_manual_command_approval_stays_active_and_ctrl_c_cancels(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        result=await self.client.call("workspace/proposeCommand", {
            "mutationId":str(uuid4()),"threadId":self.session,
            "request":{"command":"sleep 3; touch should-not-exist","timeout_seconds":5,"output_limit":1024},
        })
        identifier=result["result"]["id"]
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput(),no_color=True)
            await preview.restore()
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(0.05)
                input.send_text("\x1b[H\r")
                for _ in range(100):
                    if preview.active_operation:break
                    await asyncio.sleep(0.02)
                self.assertEqual(preview.active_operation,identifier)
                input.send_text("\x03")
                for _ in range(100):
                    if preview.active_operation is None:break
                    await asyncio.sleep(0.02)
                self.assertIsNone(preview.active_operation)
                self.assertFalse(task.done(),"Ctrl+C must cancel the command and retain the client")
                operation=(await self.client.call("synth/operation/read",{"operationId":identifier}))["operation"]
                self.assertIn(operation["status"],{"canceled","failed"})
                self.assertFalse((self.workspace/"should-not-exist").exists())
                input.send_text("\x11")
                await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_output_storage_refusal_is_unknown_and_reconcilable_without_restart(self):
        operation=(await self.client.call("workspace/proposeCommand", {
            "mutationId":str(uuid4()),"threadId":self.session,
            "request":{"command":"printf done >> witness","timeout_seconds":5,"output_limit":1024},
        }))["result"]
        with (self.root/"store/artifacts/capacity-fixture").open("wb") as file:
            file.truncate(512*1024*1024)
        approval={"mutationId":str(uuid4()),"operationId":operation["id"],"requestDigest":operation["request_digest"],"allow":True}
        await self.client.call("synth/approval/respond",approval)
        for _ in range(100):
            current=(await self.client.call("synth/operation/read",{"operationId":operation["id"]}))["operation"]
            if current["status"]=="unknown":break
            await asyncio.sleep(0.02)
        self.assertEqual(current["status"],"unknown")
        self.assertEqual((self.workspace/"witness").read_text(),"done")
        await self.client.call("synth/approval/respond",approval)
        await self.client.call("synth/operation/reconcile",{"mutationId":str(uuid4()),"operationId":operation["id"],"requestDigest":operation["request_digest"],"observation":"Inspected witness: done"})
        result=await self.client.call("synth/operation/run",{"operationId":operation["id"],"requestDigest":operation["request_digest"]})
        self.assertEqual(result["operations"][0]["status"],"reconciled")
        self.assertEqual((self.workspace/"witness").read_text(),"done")

    async def test_dispatch_capacity_refuses_then_manual_resume_keeps_cancel_owner(self):
        operations=[]
        for index in range(9):
            workspace=self.root/f"command-{index}"
            workspace.mkdir()
            session=(await self.client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(workspace)}))["result"]["id"]
            operation=(await self.client.call("workspace/proposeCommand",{"mutationId":str(uuid4()),"threadId":session,"request":{"command":"sleep 10; printf duplicate >> witness","timeout_seconds":20,"output_limit":1024}}))["result"]
            operations.append(operation)
            if index<8:await self.approve(operation["id"])
        ninth=operations[-1]
        with self.assertRaises(RuntimeError):await self.approve(ninth["id"])
        self.assertEqual((await self.client.call("synth/operation/read",{"operationId":ninth["id"]}))["operation"]["status"],"ready")
        for operation in operations[:8]:await self.client.call("synth/operation/cancel",{"operationId":operation["id"]})
        for _ in range(100):
            statuses=[(await self.client.call("synth/operation/read",{"operationId":op["id"]}))["operation"]["status"] for op in operations[:8]]
            if all(status!="dispatched" for status in statuses):break
            await asyncio.sleep(0.02)
        request={"operationId":ninth["id"],"requestDigest":ninth["request_digest"]}
        # Two distinct socket connections can replay the same dispatch request.
        peer=await Client.attach(self.socket)
        try:await asyncio.gather(self.client.call("synth/operation/run",request),peer.call("synth/operation/run",request))
        finally:await peer.close()
        self.assertEqual((await self.client.call("synth/operation/read",{"operationId":ninth["id"]}))["operation"]["status"],"dispatched")
        await self.client.call("synth/operation/cancel",{"operationId":ninth["id"]})
        for _ in range(100):
            current=(await self.client.call("synth/operation/read",{"operationId":ninth["id"]}))["operation"]
            if current["status"]!="dispatched":break
            await asyncio.sleep(0.02)
        self.assertEqual(current["status"],"failed")
        self.assertFalse((self.root/"command-8/witness").exists())

    async def test_read_edit_command_detach_and_continuation(self):
        import hashlib
        self.script=[("read_file",{"path":"note.txt"}),
            ("edit_file",{"path":"note.txt","expectedSha256":hashlib.sha256(b"before\n").hexdigest(),"content":"after\n"}),
            ("run_command",{"command":"cat note.txt; printf command-ok > checked.txt","timeout_seconds":5}),"Updated and checked."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:len(v["operations"])==1)
        self.assertEqual((self.workspace/"note.txt").read_text(),"before\n")
        await self.client.close()
        self.client=await Client.attach(self.socket)
        await self.approve(view["operations"][0])
        view=await self.wait_view(turn,lambda v:len(v["operations"])==2)
        self.assertEqual((self.workspace/"note.txt").read_text(),"after\n")
        self.assertFalse((self.workspace/"checked.txt").exists())
        await self.approve(view["operations"][1])
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual((self.workspace/"checked.txt").read_text(),"command-ok")
        self.assertEqual(len(self.requests),4)
        self.assertIn("PRIVATE_REASONING_SENTINEL",json.dumps(self.requests[-1]["messages"]))
        self.assertEqual(len(view["usage"]),5)  # four scripted requests plus the goal check
        self.assertEqual(view["messages"][-3]["text"],"Updated and checked.")

    async def test_interrupt_during_a_command_settles_it_and_leaves_the_workspace_usable(self):
        # Owner run 10-28: Esc during ./measure.sh left it Unknown and blocked the workspace.
        await self.set_permissions("yolo")
        self.script=[("run_command",{"command":"sleep 30","timeout_seconds":60})]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"wait"}))["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        operation=view["operations"][0]
        for _ in range(200):
            if (await self.client.call("synth/operation/read",{"operationId":operation}))["operation"]["status"]=="dispatched":break
            await asyncio.sleep(0.02)
        await self.client.call("turn/interrupt",{"turnId":turn})
        status=(await self.client.call("synth/operation/read",{"operationId":operation}))["operation"]["status"]
        self.assertIn(status,{"failed","canceled"})
        self.script=["Ready again."]
        again=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"next"}))["id"]
        await self.wait_view(again,lambda v:v["phase"]=="completed")

    async def test_replaced_runtime_binary_cancels_the_command_instead_of_blocking(self):
        # Owner run 10-28: the binary was swapped under a live daemon; the approved command
        # stayed Ready forever and every later action hit workspace_busy_or_uncertain.
        import shutil
        await self.client.close()
        self.process.terminate();await self.process.wait()
        self.runtime_path=self.root/"synth-code-runtime"
        shutil.copy2(RUNTIME,self.runtime_path)
        await self.boot()
        self.client=await Client.attach(self.socket)
        await self.set_permissions("yolo")
        replacement=self.root/"replacement-runtime"
        shutil.copy2(self.runtime_path,replacement)
        with open(replacement,"ab") as binary:binary.write(b"\0")
        replacement.replace(self.runtime_path)
        self.script=[("run_command",{"command":"printf ran > ran.txt","timeout_seconds":5}),"Stopped."]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"run it"}))["id"]
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        status=(await self.client.call("synth/operation/read",{"operationId":view["operations"][0]}))["operation"]["status"]
        self.assertEqual(status,"canceled")
        self.assertFalse((self.workspace/"ran.txt").exists())
        self.assertIn("native_runtime_changed",json.dumps(self.requests[1]["messages"][-1]))
        # The workspace stays usable: an edit (which needs no native re-exec) is admitted and runs.
        self.script=[("edit_file",{"path":"new.txt","expectedSha256":None,"content":"ok\n"}),"Done."]
        again=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"edit"}))["id"]
        await self.wait_view(again,lambda v:v["phase"]=="completed")
        self.assertEqual((self.workspace/"new.txt").read_text(),"ok\n")

    async def test_command_outside_the_workspace_is_marked_and_still_runs(self):
        await self.set_permissions("yolo")
        target=self.root/"outside.txt"
        self.script=[("run_command",{"command":f"printf out > {target} && printf in > inside.txt","timeout_seconds":5}),"Done."]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"write"}))["id"]
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        operation=(await self.client.call("synth/operation/read",{"operationId":view["operations"][0]}))["operation"]
        self.assertEqual((operation["status"],operation["outside"]),("succeeded",[str(target)]))
        self.assertEqual(target.read_text(),"out")
        self.assertEqual((self.workspace/"inside.txt").read_text(),"in")

    async def test_compact_replaces_history_with_user_messages_and_a_summary(self):
        empty=(await self.client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(self.workspace)}))["result"]["id"]
        with self.assertRaisesRegex(Exception,"nothing_to_compact"):
            await self.client.call("thread/compact",{"mutationId":str(uuid4()),"threadId":empty})
        self.script=["Hello there."]
        first=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"first task"}))["id"]
        await self.wait_view(first,lambda v:v["phase"]=="completed")
        compact=(await self.client.call("thread/compact",{"mutationId":str(uuid4()),"threadId":self.session}))["id"]
        view=await self.wait_view(compact,lambda v:v["phase"]=="completed")
        # The model saw the whole conversation and was asked for a summary, not an answer.
        sent=[m.get("content") for m in self.compactions[0]["messages"]]
        self.assertIn("first task",sent);self.assertIn("Hello there.",sent)
        self.assertEqual([m["role"] for m in view["messages"]],["assistant","compaction"])
        self.assertEqual(view["messages"][1]["text"],"SUMMARY: read big.txt; next step is to finish.")
        # The next turn starts from the user's messages and the summary only.
        self.script=["Continuing."]
        after=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"next"}))["id"]
        await self.wait_view(after,lambda v:v["phase"]=="completed")
        history=[(m["role"],m.get("content","")) for m in self.requests[-1]["messages"][1:]]
        self.assertEqual([role for role,_ in history],["user","user","user"])
        self.assertEqual(history[0][1],"first task")
        self.assertTrue(history[1][1].startswith("Synth Code compacted the earlier conversation."))
        self.assertTrue(history[1][1].endswith("SUMMARY: read big.txt; next step is to finish."))
        self.assertEqual(history[2][1],"next")

    async def test_a_large_request_compacts_mid_turn_and_the_turn_continues(self):
        config=json.loads(self.config.read_text());config["compact_request_bytes"]=10240
        await self.client.close();self.client=None
        self.process.terminate();await self.process.wait();self.process=None
        self.config.write_text(json.dumps(config));await self.boot()
        self.client=await Client.attach(self.socket)
        self.session=(await self.client.call("thread/start",{"mutationId":str(uuid4()),"workspace":str(self.workspace)}))["result"]["id"]
        (self.workspace/"big.txt").write_text("x"*12000)
        self.script=[("read_file",{"path":"big.txt"}),"Finished."]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"read big.txt"}))["id"]
        view=await self.wait_view(turn,lambda v:v["phase"]!="running" and v["phase"]!="waiting")
        self.assertEqual(view["phase"],"completed",view.get("error"))
        self.assertEqual(len(self.compactions),1)
        # After compaction the model continues from the prompt and summary, without the 12 KB tool reply.
        resumed=self.requests[1]["messages"][1:]
        self.assertEqual([m["role"] for m in resumed],["user","user"])
        self.assertEqual(resumed[0]["content"],"read big.txt")
        self.assertIn("SUMMARY: read big.txt",resumed[1]["content"])
        self.assertIn("compaction",[m["role"] for m in view["messages"]])

    async def test_final_answer_after_tool_work_gets_one_goal_check(self):
        # A chat with no tool work ends on its first answer.
        self.script=["Hello."]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"hi"}))["id"]
        await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual(self.checks,[])
        # Work, an early stop, a check that resumes work, then a check answered without tools.
        await self.set_permissions("yolo")
        self.script=[("run_command",{"command":"printf 1 > one.txt","timeout_seconds":5}),"Iteration 1 done.","Iteration 2 done."]
        self.check_script=[("run_command",{"command":"printf 2 > two.txt","timeout_seconds":5}),"Stopping rule met."]
        turn=(await self.client.call("turn/start",{"mutationId":str(uuid4()),"threadId":self.session,"prompt":"do 2 iterations"}))["id"]
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual((self.workspace/"one.txt").read_text()+(self.workspace/"two.txt").read_text(),"12")
        self.assertEqual(len(self.checks),2)  # first check resumed work; second was answered without tools
        check=self.checks[0]["messages"][-1]["content"]
        self.assertIn("no time, turn or budget limit",check)
        self.assertIn("name the stopping condition that is met and the evidence",check)
        roles=[m["role"] for m in view["messages"]]
        self.assertEqual(roles.count("continue"),2,roles)
        self.assertIn("stopping rule",next(m["text"] for m in view["messages"] if m["role"]=="continue"))

    async def test_denial_has_no_effect_and_model_receives_denial(self):
        self.script=[("run_command",{"command":"touch forbidden.txt","timeout_seconds":5}),"Respected the denial."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        await self.approve(view["operations"][0],False)
        await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertFalse((self.workspace/"forbidden.txt").exists())
        self.assertIn("denied",self.requests[-1]["messages"][-1]["content"])

    async def test_contaminated_tool_arguments_never_become_effects(self):
        # The real arguments Reflection returned in the SYN-5241 acceptance run.
        fixture=json.loads((Path(__file__).resolve().parents[2]/"crates/runtime/tests/fixtures/syn-5241-leaked-edit.json").read_text())
        leaked=fixture["arguments"]["content"]
        self.script=[("edit_file",{"path":"note.txt","expectedSha256":None,"content":leaked}),"Will retry cleanly."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual(view["operations"],[])
        self.assertEqual((self.workspace/"note.txt").read_text(),"before\n")
        result=self.requests[-1]["messages"][-1]["content"]
        self.assertIn("provider_tool_arguments_contaminated",result)
        self.assertIn("nothing was executed",result)

    async def test_escape_interrupts_and_queued_message_is_sent_next(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        self.script=["hang","Picked up the queued note."]
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput(),no_color=True)
            await preview.restore()
            task=asyncio.create_task(preview.run())
            try:
                await preview.admit("Update note and check it")
                first=preview.active_turn
                self.assertIsNotNone(first,preview.transcript.text[-600:])
                await self.wait_view(first,lambda v:v["attempts"]==1)
                await preview.admit("also try wood first")
                self.assertEqual(preview.queued_messages,["also try wood first"])
                input.send_text("\x1b")
                await self.wait_view(first,lambda v:v["phase"]=="interrupted")
                for _ in range(200):
                    if preview.active_turn not in (None,first):break
                    await asyncio.sleep(.025)
                second=preview.active_turn
                self.assertNotIn(second,(None,first))
                done=await self.wait_view(second,lambda v:v["phase"]=="completed")
                self.assertEqual(done["prompt"],"also try wood first")
                self.assertIn("Interrupted · send a message to continue",preview.transcript.text)
                self.assertIn("Queued message sent",preview.transcript.text)
                self.assertEqual(preview.queued_messages,[])
                input.send_text("\x11");await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,*preview.tasks,return_exceptions=True)

    async def test_file_tools_read_and_edit_outside_the_workspace(self):
        outside=self.root/"outside.txt"
        outside.write_text("far away\n")
        digest=__import__("hashlib").sha256(b"far away\n").hexdigest()
        self.script=[("read_file",{"path":str(outside)}),
                     ("edit_file",{"path":str(outside),"expectedSha256":digest,"content":"edited\n"}),"Done."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        self.assertIn("far away",self.requests[1]["messages"][-1]["content"])
        await self.approve(view["operations"][0],True)
        await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual(outside.read_text(),"edited\n")

    async def test_turn_read_reports_the_observed_model_stage(self):
        self.script=["hang"]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:(v.get("modelStage") or {}).get("stage")=="waiting")
        first=view["modelStage"]["quietMs"]
        await asyncio.sleep(0.3)
        later=(await self.client.call("turn/read",{"turnId":turn}))["modelStage"]
        self.assertEqual(later["stage"],"waiting")
        self.assertGreater(later["quietMs"],first)
        await self.client.call("turn/interrupt",{"turnId":turn})
        self.assertNotIn("modelStage",await self.client.call("turn/read",{"turnId":turn}))

    async def test_interrupt_fences_late_model_and_restart_does_not_retry(self):
        self.script=["hang"]
        turn=(await self.start())["id"]
        await self.wait_view(turn,lambda v:v["attempts"]==1)
        result=await self.client.call("turn/interrupt",{"turnId":turn})
        self.assertEqual(result["phase"],"interrupted")
        await self.client.close()
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(),10)
        self.assertEqual(self.process.returncode,0,(await self.process.stderr.read()).decode())
        await self.boot()
        self.client=await Client.attach(self.socket)
        result=await self.client.call("turn/read",{"turnId":turn})
        self.assertEqual(result["phase"],"interrupted")
        self.assertTrue(result["modelOutcomeUnknown"])
        await asyncio.sleep(0.1)
        self.assertLessEqual(len(self.requests),1)

    async def test_daemon_crash_during_command_is_unknown_and_never_repeated(self):
        self.script=[("run_command",{"command":"printf started > started.txt; sleep 2; touch delayed.txt","timeout_seconds":5})]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        operation=view["operations"][0]
        await self.approve(operation)
        for _ in range(200):
            if (self.workspace/"started.txt").exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue((self.workspace/"started.txt").exists())
        self.process.kill()
        await self.process.wait()
        await self.client.close()
        await self.boot()
        self.client=await Client.attach(self.socket)
        view=await self.client.call("turn/read",{"turnId":turn})
        self.assertEqual(view["phase"],"interrupted")
        op=(await self.client.call("synth/operation/read",{"operationId":operation}))["operation"]
        self.assertEqual(op["status"],"unknown")
        with self.assertRaisesRegex(RuntimeError,"workspace_busy_or_uncertain: operation [0-9a-f-]+ in session [0-9a-f-]+ is "):
            await self.start()
        await asyncio.sleep(2.1)
        self.assertFalse((self.workspace/"delayed.txt").exists())
        await self.client.call("synth/operation/reconcile",{"mutationId":str(uuid4()),"operationId":operation,
            "requestDigest":op["request_digest"],"observation":"Inspected files: started exists, delayed absent. Accept this uncertain command without repeating."})
        self.script=["Prior interrupted work inspected; no repeat."]
        next_turn=(await self.start())["id"]
        await self.wait_view(next_turn,lambda v:v["phase"]=="completed")
        messages=self.requests[-1]["messages"]
        self.assertIn("effects may be unknown",json.dumps(messages))
        self.assertFalse((self.workspace/"delayed.txt").exists())

    async def test_model_receives_full_bounded_command_output(self):
        self.script=[("run_command",{"command":"i=0; while [ \"$i\" -lt 1100 ]; do printf x; i=$((i+1)); done; printf TAIL_WITNESS","timeout_seconds":5}),"Checked the full output."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        operation=view["operations"][0]
        await self.approve(operation)
        await self.wait_view(turn,lambda v:v["phase"]=="completed")
        tool=json.loads(self.requests[-1]["messages"][-1]["content"])
        self.assertTrue(tool["command_output"]["stdout"].endswith("TAIL_WITNESS"))
        self.assertGreater(len(tool["command_output"]["stdout"]),384)
        page=await self.client.call("synth/operation/output",{"operationId":operation,"stream":"stdout","after":0})
        self.assertEqual(page["text"],tool["command_output"]["stdout"])
        self.assertIsNone(page["nextCursor"])

    async def test_session_settings_persist_and_turn_replay_keeps_original_profile(self):
        status=await self.client.call('synth/provider/status',{'threadId':self.session})
        self.assertEqual(status['effort'],'medium')
        selection=dict(status['selection'],effort='high')
        changed=await self.client.call('synth/provider/configure',{'mutationId':str(uuid4()),'threadId':self.session,'selection':selection,'expectedRevision':status['revision']})
        self.assertEqual(changed['status']['effort'],'high')
        other=(await self.client.call('thread/start',{'mutationId':str(uuid4()),'workspace':str(self.workspace)}))['result']['id']
        self.assertEqual((await self.client.call('synth/provider/status',{'threadId':other}))['effort'],'medium')
        self.script=['Done']
        request={'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Summarize the workspace'}
        turn=await self.client.call('turn/start',request)
        completed=await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        self.assertEqual(self.requests[0]['reasoning_effort'],'high')
        self.assertEqual(completed['selection']['effort'],'high')
        status=await self.client.call('synth/provider/status',{'threadId':self.session})
        await self.client.call('synth/provider/configure',{'mutationId':str(uuid4()),'threadId':self.session,'selection':dict(selection,effort='max'),'expectedRevision':status['revision']})
        replay=await self.client.call('turn/start',request)
        self.assertEqual(replay['selection']['effort'],'high')
        self.assertEqual(len(self.requests),1)
        await self.client.close();self.process.terminate();await asyncio.wait_for(self.process.wait(),5)
        await self.boot();self.client=await Client.attach(self.socket)
        self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['effort'],'max')
        self.assertEqual((await self.client.call('synth/provider/status',{}))['effort'],'medium')

    async def test_settings_reject_unsupported_stale_and_active_turn_updates(self):
        status=await self.client.call('synth/provider/status',{'threadId':self.session})
        for field,value in [('provider','other'),('model','other'),('effort','ultra'),('env_file','/unselected')]:
            with self.assertRaises(RuntimeError):
                await self.client.call('synth/provider/configure',{'mutationId':str(uuid4()),'threadId':self.session,'selection':dict(status['selection'],**{field:value}),'expectedRevision':status['revision']})
        valid={'mutationId':str(uuid4()),'threadId':self.session,'selection':dict(status['selection'],effort='low'),'expectedRevision':status['revision']}
        await self.client.call('synth/provider/configure',valid)
        with self.assertRaisesRegex(RuntimeError,'session_settings_conflict'):
            await self.client.call('synth/provider/configure',dict(valid,mutationId=str(uuid4()),selection=dict(status['selection'],effort='high')))
        self.script=['hang'];turn=await self.start()
        await self.wait_view(turn['id'],lambda v:v['attempts']==1)
        current=await self.client.call('synth/provider/status',{'threadId':self.session})
        with self.assertRaisesRegex(RuntimeError,'session_busy_or_uncertain'):
            await self.client.call('synth/provider/configure',dict(valid,mutationId=str(uuid4()),expectedRevision=current['revision']))
        await self.client.call('turn/interrupt',{'turnId':turn['id']})
        self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['effort'],'low')

    async def set_permissions(self, mode):
        status=await self.client.call('synth/permissions/status',{'threadId':self.session})
        return await self.client.call('synth/permissions/configure',{'mutationId':str(uuid4()),
            'threadId':self.session,'mode':mode,'expectedRevision':status['revision']})

    async def test_permissions_yolo_runs_model_edits_and_commands_without_approval(self):
        await self.set_permissions('yolo')
        before=(self.workspace/'note.txt').read_bytes()
        import hashlib
        self.script=[('edit_file',{'path':'note.txt','expectedSha256':hashlib.sha256(before).hexdigest(),'content':'after\n'}),
            ('run_command',{'command':'printf checked > checked','timeout_seconds':2}),'Finished']
        turn=await self.start()
        view=await self.wait_view(turn['id'],lambda v:v['phase'] in {'completed','failed'})
        self.assertEqual(view['phase'],'completed',view)
        self.assertEqual((self.workspace/'note.txt').read_text(),'after\n')
        self.assertEqual((self.workspace/'checked').read_text(),'checked')
        self.assertEqual(len(view['operations']),2)
        for identifier in view['operations']:
            op=(await self.client.call('synth/operation/read',{'operationId':identifier}))['operation']
            self.assertEqual(op['status'],'succeeded')

    async def test_permissions_auto_edit_still_requires_command_approval(self):
        await self.set_permissions('auto-edit')
        self.script=[('edit_file',{'path':'new.txt','expectedSha256':None,'content':'new\n'}),
            ('run_command',{'command':'touch forbidden','timeout_seconds':2}),'Denied command']
        turn=await self.start()
        view=await self.wait_view(turn['id'],lambda v:len(v['operations'])==2)
        self.assertEqual((self.workspace/'new.txt').read_text(),'new\n')
        op=(await self.client.call('synth/operation/read',{'operationId':view['operations'][1]}))['operation']
        self.assertEqual(op['status'],'awaiting_approval')
        await self.set_permissions('yolo')
        op=(await self.client.call('synth/operation/read',{'operationId':view['operations'][1]}))['operation']
        self.assertEqual(op['status'],'awaiting_approval')
        await self.approve(op['id'],False)
        await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        self.assertFalse((self.workspace/'forbidden').exists())

    async def test_permissions_direct_proposals_replay_and_restart_never_repeat(self):
        self.assertEqual((await self.client.call('synth/permissions/status',{'threadId':self.session}))['mode'],'manual')
        await self.set_permissions('yolo')
        edit={'mutationId':str(uuid4()),'threadId':self.session,'path':'new.txt','expectedSha256':None,'content':'new\n'}
        first=await self.client.call('workspace/proposeEdit',edit)
        self.assertEqual(first['result']['status'],'succeeded')
        (self.workspace/'new.txt').write_text('external change\n')
        replay=await self.client.call('workspace/proposeEdit',edit)
        self.assertTrue(replay['replayed'])
        self.assertEqual((self.workspace/'new.txt').read_text(),'external change\n')
        command={'mutationId':str(uuid4()),'threadId':self.session,'request':{
            'command':'printf once >> count','timeout_seconds':2,'output_limit':1024}}
        op=(await self.client.call('workspace/proposeCommand',command))['result']
        for _ in range(200):
            op=(await self.client.call('synth/operation/read',{'operationId':op['id']}))['operation']
            if op['status'] not in {'ready','dispatched'}:break
            await asyncio.sleep(.02)
        self.assertEqual(op['status'],'succeeded')
        await self.client.close();self.process.terminate();await asyncio.wait_for(self.process.wait(),5)
        await self.boot();self.client=await Client.attach(self.socket)
        self.assertEqual((await self.client.call('synth/permissions/status',{'threadId':self.session}))['mode'],'yolo')
        self.assertTrue((await self.client.call('workspace/proposeCommand',command))['replayed'])
        self.assertEqual((self.workspace/'count').read_text(),'once')
        other=(await self.client.call('thread/start',{'mutationId':str(uuid4()),'workspace':str(self.workspace)}))['result']['id']
        self.assertEqual((await self.client.call('synth/permissions/status',{'threadId':other}))['mode'],'manual')

    async def test_permissions_change_during_a_turn_governs_only_later_actions(self):
        self.script=[("run_command",{"command":"touch first","timeout_seconds":5}),
                     ("run_command",{"command":"touch second","timeout_seconds":5}),"Done."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        first=view["operations"][0]
        await self.set_permissions('yolo')
        self.assertEqual((await self.client.call('synth/operation/read',{'operationId':first}))['operation']['status'],'awaiting_approval')
        await self.approve(first,True)
        view=await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertEqual(len(view["operations"]),2)
        self.assertTrue((self.workspace/'second').exists())

    async def test_permissions_stale_turn_admission_refuses_but_exact_replay_survives_mode_change(self):
        await self.set_permissions('yolo')
        request={'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Summary','expectedPermissions':'manual'}
        with self.assertRaisesRegex(RuntimeError,'session_permissions_changed'):
            await self.client.call('turn/start',request)
        self.assertEqual(self.requests,[])
        self.script=['Done']
        request['expectedPermissions']='yolo'
        turn=await self.client.call('turn/start',request)
        self.assertEqual(turn['permissions'],'yolo')
        await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        await self.set_permissions('manual')
        replay=await self.client.call('turn/start',request)
        self.assertEqual(replay['id'],turn['id'])
        self.assertEqual(len(self.requests),1)

    async def test_activity_completion_restores_once_with_reported_session_tokens(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        self.script=['Done'];turn=await self.start()
        done=await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        self.assertGreaterEqual(done['activity']['durationMs'],0)
        self.assertIsNotNone(done['activity']['completedAt'])
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore();await preview.render_turn(done);await preview.render_turn(done)
            self.assertEqual(preview.transcript.text.count('Worked for '),1)
            footer=''.join(text for _,text in preview.footer_text())
            self.assertIn('20 in · 10 out',footer)
        await self.client.close();self.process.terminate();await asyncio.wait_for(self.process.wait(),5)
        await self.boot();self.client=await Client.attach(self.socket)
        restored=await self.client.call('turn/read',{'turnId':turn['id']})
        self.assertEqual(restored['activity'],done['activity'])
        pending=(await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),
            'threadId':self.session,'path':'unapproved.txt','expectedSha256':None,'content':'pending'}))['result']
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            self.assertEqual(preview.transcript.text.count('Worked for '),1)
            self.assertEqual(preview.pending['id'],pending['id'])
            self.assertIn('Awaiting approval',preview.status.text)
            self.assertFalse((self.workspace/'unapproved.txt').exists())

    async def test_activity_running_command_status_advances_without_new_events(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        await self.set_permissions('yolo')
        self.script=[('run_command',{'command':'sleep 3; printf done','timeout_seconds':6}),'Done']
        turn=await self.start()
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            try:
                for _ in range(200):
                    if 'Running command' in preview.status.text:break
                    await asyncio.sleep(.02)
                self.assertIn('timeout 6s',preview.status.text)
                self.assertIn('output arrives at exit',preview.status.text)
                first=preview.status.text;sequence=(await self.client.call('synth/status',{}))['sequence']
                await asyncio.sleep(1.1)
                self.assertNotEqual(first,preview.status.text)
                self.assertEqual((await self.client.call('synth/status',{}))['sequence'],sequence)
                await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
                for _ in range(100):
                    if not preview.active_turn:break
                    await asyncio.sleep(.02)
                self.assertEqual(preview.transcript.text.count('Worked for '),1)
            finally:
                for task in list(preview.tasks):task.cancel()
                await asyncio.gather(*preview.tasks,return_exceptions=True)

    async def test_activity_restore_unknown_effect_takes_priority_over_completed_history(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        self.script=['Done'];turn=await self.start()
        await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        await self.client.close();self.process.terminate();await asyncio.wait_for(self.process.wait(),5)
        await self.process.stderr.read()
        self.process=await asyncio.create_subprocess_exec(str(RUNTIME),'stdio',str(self.root/'store'),
            '--test-crash-after-effect','--provider-config',str(self.config),stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=65536)
        self.client=Client(self.process)
        await self.client.call('initialize',{})
        op=(await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),'threadId':self.session,
            'path':'unknown.txt','expectedSha256':None,'content':'first'}))['result']
        with self.assertRaises(ConnectionError):await self.approve(op['id'])
        await asyncio.wait_for(self.process.wait(),5)
        self.assertEqual(self.process.returncode,86)
        await self.process.stderr.read();await self.client.close()
        (self.workspace/'unknown.txt').write_text('external change')
        await self.boot();self.client=await Client.attach(self.socket)
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            self.assertEqual(preview.status.text,'Outcome unknown')
            self.assertEqual(preview.transcript.text.count('Worked for '),1)
            self.assertIsNone(preview.active_operation)
            self.assertIsNone(preview.pending)
            self.assertEqual((self.workspace/'unknown.txt').read_text(),'external change')

    async def test_copy_restored_public_response_does_not_mutate_pending_approval(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview,display_text
        reply='## Reply\n\n```python\nprint("hello")\n```\n'+'More text Ω\n'*500
        self.script=[reply];turn=await self.start()
        await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        op=(await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),'threadId':self.session,
            'path':'pending.txt','expectedSha256':None,'content':'pending'}))['result']
        copied=[]
        async def writer(text):copied.append(text)
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput(),clipboard_writer=writer)
            await p.restore();before=(await self.client.call('synth/status',{}))['sequence']
            target=next(iter(p.copy_targets.values()));task=asyncio.create_task(p.copy_response(target))
            try:
                for _ in range(100):
                    if copied:break
                    await asyncio.sleep(.01)
                self.assertEqual(copied,[display_text(reply)])
                self.assertNotIn('PRIVATE_REASONING_SENTINEL',copied[0])
                self.assertEqual(p.pending['id'],op['id'])
                self.assertTrue(p.application.layout.has_focus(p.composer))
                self.assertIn('Awaiting approval',p.status.text)
                self.assertEqual((await self.client.call('synth/status',{}))['sequence'],before)
                self.assertFalse((self.workspace/'pending.txt').exists())
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_host_discovery_is_visible_to_the_model_outside_a_small_workspace(self):
        self.script=[('host_info',{}),'Host tools discovered']
        before=(await self.client.call('synth/status',{}))['sequence']
        info=await self.client.call('synth/host/status',{'threadId':self.session})
        self.assertEqual(info['workspace'],str(self.workspace))
        self.assertTrue(info['hostCommands'])
        self.assertIn('docker',info['executables'])
        self.assertEqual((await self.client.call('synth/status',{}))['sequence'],before)
        turn=await self.start();done=await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
        self.assertEqual(done['operations'],[])
        tool=next(m for m in self.requests[1]['messages'] if m['role']=='tool')
        self.assertEqual(json.loads(tool['content'])['workspace'],str(self.workspace))
        self.assertIn('list_containers',[t['function']['name'] for t in self.requests[0]['tools']])
        self.assertIn('including outside the workspace',self.requests[0]['messages'][0]['content'])
        self.assertIn('full access to the user\'s files',self.requests[0]['messages'][0]['content'])
        self.assertIn('do not retry it or an equivalent command',self.requests[0]['messages'][0]['content'])
        self.assertIn('keep working until it is met',self.requests[0]['messages'][0]['content'])
        context=self.requests[0]['messages'][0]
        self.assertEqual(context['role'],'system')
        self.assertIn('Runtime observations at turn start',context['content'])
        self.assertIn(str(self.workspace),context['content'])
        self.assertIn('"hostCommands":true',context['content'])
        self.assertIn('"containerInventory":null',context['content'])
        self.assertEqual(self.requests[0]['messages'][-1]['role'],'user')
        self.assertEqual(self.requests[1]['messages'][-1]['role'],'tool')

    async def restart_with_request_limit(self, limit):
        await self.client.close();self.process.terminate();await asyncio.wait_for(self.process.wait(),5)
        config=json.loads(self.config.read_text())
        if limit is None:config.pop('max_requests',None)
        else:config['max_requests']=limit
        self.config.write_text(json.dumps(config))
        await self.boot();self.client=await Client.attach(self.socket)

    async def test_unlimited_request_count_passes_legacy_caps_and_still_interrupts(self):
        from unittest.mock import patch
        from synth_code.daemon import connect
        await self.restart_with_request_limit(None)
        self.assertIsNone((await self.client.call('synth/provider/status',{}))['maxRequests'])
        with patch('synth_code.daemon.address',return_value=self.socket):
            peer=await connect(RUNTIME,self.root/'store',config=self.config)
            await peer.close()
        self.script=[('read_file',{'path':'note.txt'})]*32+['Finished beyond the former limit.']
        request={'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Inspect the note repeatedly, then finish.'}
        turn=await self.client.call('turn/start',request)
        done=await self.wait_view(turn['id'],lambda v:v['phase']=='completed',polls=2000)
        self.assertEqual(done['attempts'],34)  # 33 scripted requests plus the goal check
        self.assertEqual(done['messages'][-3]['text'],'Finished beyond the former limit.')
        self.assertEqual(done['operations'],[])
        await self.client.call('turn/start',request)
        self.assertEqual(len(self.requests),33)
        self.script=['hang'];turn=await self.start()
        await self.wait_view(turn['id'],lambda v:v['attempts']==1)
        await self.client.call('turn/interrupt',{'turnId':turn['id']})
        stopped=await self.wait_view(turn['id'],lambda v:v['phase']=='interrupted')
        self.assertEqual(stopped['attempts'],1)

    async def test_explicit_request_cap_survives_upgrade_without_replaying_failed_turn(self):
        self.script=[('read_file',{'path':'note.txt'})]*9
        request={'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Bounded fixture'}
        turn=await self.client.call('turn/start',request)
        failed=await self.wait_view(turn['id'],lambda v:v['phase']=='failed')
        self.assertEqual(failed['error'],'turn_request_limit_reached')
        self.assertEqual(failed['attempts'],8)
        self.assertEqual(len(self.requests),8)
        await self.restart_with_request_limit(None)
        replay=await self.client.call('turn/start',request)
        self.assertEqual(replay['id'],turn['id'])
        restored=await self.client.call('turn/read',{'turnId':turn['id']})
        self.assertEqual(restored['phase'],'failed')
        self.assertEqual(len(self.requests),8)
        self.script=['New turn has no request limit.'];next_turn=await self.start()
        done=await self.wait_view(next_turn['id'],lambda v:v['phase']=='completed')
        self.assertEqual(done['messages'][-1]['text'],'New turn has no request limit.')

    async def test_peer_session_sync_catches_completed_turns_without_duplicates_or_draft_loss(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        await self.set_permissions('yolo')
        peer=await Client.attach(self.socket)
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore();p.composer.text='unsent draft';p.application.layout.focus(p.transcript)
            task=asyncio.create_task(p.run())
            try:
                p.settings_modal=True
                self.script=[('run_command',{'command':'printf peer-output','timeout_seconds':5}),'Peer task complete.']
                turn=await peer.call('turn/start',{'mutationId':str(uuid4()),'threadId':self.session,'prompt':'External task'})
                await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
                async with p.session_sync_lock:await p.sync_session_locked()
                self.assertNotIn('Peer task complete.',p.transcript.text)
                p.settings_modal=False
                async with p.session_sync_lock:await p.sync_session_locked()
                self.assertIn('Peer task complete.',p.transcript.text)
                self.assertIn('peer-output',p.details.text)
                self.assertEqual(p.composer.text,'unsent draft')
                self.assertTrue(p.application.layout.has_focus(p.transcript))
                sequence=(await self.client.call('synth/status',{}))['sequence']
                await asyncio.sleep(2.2)
                self.assertEqual((await self.client.call('synth/status',{}))['sequence'],sequence)
                self.assertEqual(p.transcript.text.count('External task'),1)
                self.assertEqual(p.transcript.text.count('Peer task complete.'),1)
                self.assertEqual(p.transcript.text.count('Worked for'),1)
                # Clear must remain clear after the next observer tick.
                await p.local_command('/clear','');await asyncio.sleep(2.2)
                self.assertEqual(p.transcript.text,'')
                # Catch up a peer's already-finished turn before a local admission.
                p.settings_modal=True;self.script=['Peer fast complete.']
                other=await peer.call('turn/start',{'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Second external task'})
                await self.wait_view(other['id'],lambda v:v['phase']=='completed')
                p.settings_modal=False;self.script=['Local task complete.'];await p.admit('Local task')
                for _ in range(300):
                    if 'Local task complete.' in p.transcript.text and p.active_turn is None:break
                    await asyncio.sleep(.02)
                self.assertLess(p.transcript.text.index('Peer fast complete.'),p.transcript.text.index('Local task complete.'))
                await asyncio.sleep(2.2)
                self.assertEqual(p.transcript.text.count('Local task complete.'),1)
            finally:
                input.send_text('\x11');await asyncio.wait_for(task,3);await peer.close()

    async def test_peer_session_sync_tracks_live_turn_and_external_approval(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        peer=await Client.attach(self.socket)
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore();p.composer.text='keep this draft';task=asyncio.create_task(p.run())
            try:
                # An open modal (the permissions picker) must survive a session sync.
                await p.admit('/permissions')
                self.assertTrue(p.settings_modal)
                self.script=[('run_command',{'command':'sleep 2; printf peer-checked','timeout_seconds':5}),'External task verified.']
                turn=await peer.call('turn/start',{'mutationId':str(uuid4()),'threadId':self.session,'prompt':'Live external task'})
                await self.wait_view(turn['id'],lambda v:v['phase']=='waiting')
                async with p.session_sync_lock:await p.sync_session_locked()
                self.assertIsNone(p.pending)
                self.assertTrue(p.settings_modal)
                p.settings_cancel()
                for _ in range(300):
                    if p.pending:break
                    await asyncio.sleep(.02)
                self.assertEqual(p.active_turn,turn['id'])
                self.assertIsNotNone(p.pending)
                self.assertTrue(p.application.layout.has_focus(p.composer))
                op=p.pending
                await peer.call('synth/approval/respond',{'mutationId':str(uuid4()),'operationId':op['id'],'requestDigest':op['request_digest'],'allow':True})
                for _ in range(100):
                    if p.pending is None:break
                    await asyncio.sleep(.02)
                self.assertIsNone(p.pending)
                self.assertEqual(p.composer.text,'keep this draft')
                for _ in range(300):
                    if 'External task verified.' in p.transcript.text and p.active_turn is None:break
                    await asyncio.sleep(.02)
                self.assertIn('External task verified.',p.transcript.text)
                self.assertIn('peer-checked',p.details.text)
                self.assertEqual(p.transcript.text.count('Live external task'),1)
                self.assertEqual(p.transcript.text.count('Worked for'),1)
            finally:
                input.send_text('\x11');await asyncio.wait_for(task,3);await peer.close()

    async def test_peer_session_sync_reconciles_standalone_approval_without_executing_it(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        op=(await self.client.call('workspace/proposeCommand',{'mutationId':str(uuid4()),'threadId':self.session,
            'request':{'command':'touch should-not-run','timeout_seconds':5,'output_limit':1024}}))['result']
        peer=await Client.attach(self.socket)
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore();task=asyncio.create_task(p.run())
            try:
                await asyncio.sleep(.1)
                self.assertEqual(p.pending['id'],op['id'])
                self.assertTrue(p.application.layout.has_focus(p.composer))
                await peer.call('synth/approval/respond',{'mutationId':str(uuid4()),'operationId':op['id'],'requestDigest':op['request_digest'],'allow':False})
                self.script=['Peer after denial complete.']
                turn=await peer.call('turn/start',{'mutationId':str(uuid4()),'threadId':self.session,'prompt':'After external denial'})
                for _ in range(300):
                    if 'Peer after denial complete.' in p.transcript.text and p.active_turn is None:break
                    await asyncio.sleep(.02)
                self.assertIn('Peer after denial complete.',p.transcript.text)
                self.assertIsNone(p.pending)
                self.assertFalse((self.workspace/'should-not-run').exists())
                operation=(await self.client.call('synth/operation/read',{'operationId':op['id']}))['operation']
                self.assertEqual(operation['status'],'denied')
                self.assertEqual((await self.client.call('turn/read',{'turnId':turn['id']}))['phase'],'completed')
            finally:
                input.send_text('\x11');await asyncio.wait_for(task,3);await peer.close()

    async def test_peer_session_sync_preserves_unknown_until_external_reconciliation(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore()
            self.script=['Completed during approval.'];turn=await self.start()
            await self.wait_view(turn['id'],lambda v:v['phase']=='completed')
            op=(await self.client.call('workspace/proposeCommand',{'mutationId':str(uuid4()),'threadId':self.session,
                'request':{'command':'printf probe >> witness','timeout_seconds':5,'output_limit':1024}}))['result']
            p.show_pending(op)
            capacity=self.root/'store/artifacts/capacity-fixture'
            with capacity.open('wb') as file:file.truncate(512*1024*1024)
            await self.approve(op['id'])
            for _ in range(300):
                current=(await self.client.call('synth/operation/read',{'operationId':op['id']}))['operation']
                if current['status']=='unknown':break
                await asyncio.sleep(.02)
            self.assertEqual(current['status'],'unknown')
            capacity.unlink()
            sequence=(await self.client.call('synth/status',{}))['sequence']
            async with p.session_sync_lock:
                await p.reconcile_peer_pending();await p.reconcile_peer_unknown();await p.sync_session_locked()
            self.assertEqual(p.status.text,'Outcome unknown')
            self.assertNotIn('Completed during approval.',p.transcript.text)
            self.assertEqual((await self.client.call('synth/status',{}))['sequence'],sequence)
            await self.client.call('synth/operation/reconcile',{'mutationId':str(uuid4()),'operationId':op['id'],
                'requestDigest':op['request_digest'],'observation':'Inspected witness: probe'})
            async with p.session_sync_lock:
                await p.reconcile_peer_unknown();await p.sync_session_locked()
            self.assertFalse(p.unresolved_operations)
            self.assertIn('Completed during approval.',p.transcript.text)
            self.assertEqual((self.workspace/'witness').read_text(),'probe')

    async def test_peer_session_watcher_cannot_adopt_a_successor_turn(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        reads=[]
        class Client:
            async def call(self,method,params):
                reads.append(params['turnId'])
                return {'id':params['turnId'],'phase':'completed'}
        with create_pipe_input() as input:
            p=Preview(Client(),'fixture',input=input,output=DummyOutput())
            p.active_turn='old'
            async def finished(view):p.active_turn='successor'
            p.project_turn=finished
            await p.watch_turn()
            self.assertEqual(reads,['old'])
            self.assertEqual(p.active_turn,'successor')

    async def test_peer_session_operation_watcher_cannot_adopt_a_successor(self):
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from synth_code.app import Preview
        reads=[]
        class Client:
            async def call(self,method,params):
                reads.append(params['operationId'])
                return {'operation':{'id':params['operationId'],'status':'succeeded','outcome':{}}}
        with create_pipe_input() as input:
            p=Preview(Client(),'fixture',input=input,output=DummyOutput())
            p.active_operation='old';task=asyncio.create_task(p.watch_operation())
            await asyncio.sleep(0)
            p.active_operation='successor'
            await task
            self.assertEqual(reads,['old'])
            self.assertEqual(p.active_operation,'successor')

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

class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "artifacts")
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "note.txt").write_text("before\n")
        self.requests = []
        self.script = []
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
        self.process = await asyncio.create_subprocess_exec(str(RUNTIME),"serve",str(self.root/"store"),
            "--socket",str(self.socket),"--provider-config",str(self.config),stderr=asyncio.subprocess.PIPE)
        for _ in range(200):
            if self.socket.exists():
                try:
                    c=await Client.attach(self.socket)
                    await c.close()
                    return
                except OSError:
                    pass
            if self.process.returncode is not None:
                raise AssertionError((await self.process.stderr.read()).decode())
            await asyncio.sleep(0.01)
        raise AssertionError("daemon startup timeout")

    async def asyncTearDown(self):
        await self.client.close()
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.wait(), 10)
        errors=(await self.process.stderr.read()).decode()
        self.assertEqual(self.process.returncode, 0, errors)
        self.provider.close()
        await self.provider.wait_closed()
        self.temp.cleanup()

    async def http(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = int(next(l.split(b":",1)[1] for l in header.split(b"\r\n") if l.lower().startswith(b"content-length:")))
            request = json.loads(await reader.readexactly(length))
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
            await writer.wait_closed()

    async def start(self):
        return await self.client.call("turn/start", {"mutationId":str(uuid4()), "threadId":self.session,"prompt":"Update note and check it"})

    async def wait_view(self, turn, predicate):
        for _ in range(400):
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
        self.assertEqual(len(view["usage"]),4)
        self.assertEqual(view["messages"][-1]["text"],"Updated and checked.")

    async def test_denial_has_no_effect_and_model_receives_denial(self):
        self.script=[("run_command",{"command":"touch forbidden.txt","timeout_seconds":5}),"Respected the denial."]
        turn=(await self.start())["id"]
        view=await self.wait_view(turn,lambda v:bool(v["operations"]))
        await self.approve(view["operations"][0],False)
        await self.wait_view(turn,lambda v:v["phase"]=="completed")
        self.assertFalse((self.workspace/"forbidden.txt").exists())
        self.assertIn("denied",self.requests[-1]["messages"][-1]["content"])

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
        with self.assertRaisesRegex(RuntimeError,"workspace_busy_or_uncertain"):
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

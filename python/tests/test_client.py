import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from synth_code.client import Client

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "target/debug/synth-code-runtime"

class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_backend_approval_and_receipt_replay(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            client = await Client.open(RUNTIME, root / "store")
            try:
                params = {"mutationId": str(uuid4()), "workspace": str(workspace)}
                first = await client.call("thread/start", params)
                again = await client.call("thread/start", params)
                self.assertEqual(first["result"], again["result"])
                self.assertTrue(again["replayed"])
                admitted = await client.call("fixture/admit", {
                    "mutationId": str(uuid4()), "threadId": first["result"]["id"],
                    "text": "python transport witness",
                })
                operation = admitted["result"]
                decision = {"mutationId": str(uuid4()), "operationId": operation["id"],
                            "requestDigest": operation["request_digest"], "allow": True}
                view = await client.call("synth/approval/respond", decision)
                self.assertEqual(view["operations"][0]["status"], "succeeded")
                witness = workspace / f"fixture-{operation['id']}.txt"
                before = witness.stat()
                await client.call("synth/approval/respond", decision)
                self.assertEqual(witness.read_text(), "python transport witness")
                self.assertEqual(witness.stat().st_mtime_ns, before.st_mtime_ns)
            finally:
                await client.close()
            self.assertIsNotNone(client.process.returncode)

    async def test_killed_runtime_poisoned_transport_does_not_retry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as temporary:
            client = await Client.open(RUNTIME, Path(temporary))
            client.process.kill()
            await client.process.wait()
            with self.assertRaises((ConnectionError, OSError)):
                await client.call("synth/status", {})
            with self.assertRaises(ConnectionError):
                await client.call("synth/status", {})
            await client.close()

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/"target/debug/synth-code-runtime"

class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def run_shell(self, root, command, timeout=5, limit=1024, disconnect=False):
        stat=root.stat()
        config={"session":{"id":"00000000-0000-0000-0000-000000000001","workspace":str(root),"device":stat.st_dev,"inode":stat.st_ino},"request":{"command":command,"timeout_seconds":timeout,"output_limit":limit}}
        process=await asyncio.create_subprocess_exec(str(RUNTIME),"supervise-command",stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env={"SYNTH_TEST_SECRET":"must-not-inherit"})
        try:
            process.stdin.write(json.dumps(config).encode()+b"\n")
            await process.stdin.drain()
            if disconnect:
                await asyncio.sleep(0.1)
                process.stdin.close()
            line=await asyncio.wait_for(process.stdout.readline(),8)
            process.stdin.close()
            await asyncio.wait_for(process.wait(),5)
            self.assertEqual(process.returncode,0,(await process.stderr.read()).decode())
            return json.loads(line)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def test_environment_output_and_exit_are_bounded(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"artifacts") as temporary:
            result=await self.run_shell(Path(temporary),'printf "%s" "${SYNTH_TEST_SECRET-unset}"; i=0; while [ "$i" -lt 3000 ]; do printf x; i=$((i+1)); done; printf err >&2; exit 7')
            self.assertEqual(result["exit_code"],7)
            self.assertTrue(result["stdout"].startswith("unset"))
            self.assertLessEqual(len(result["stdout"].encode()),512)
            self.assertTrue(result["truncated"])
            self.assertEqual(result["stderr"],"err")
            self.assertEqual(result["bytes"],3008)

    async def test_timeout_kills_process_group_before_delayed_effect(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"artifacts") as temporary:
            root=Path(temporary)
            result=await self.run_shell(root,'(sleep 2; touch escaped) & wait',timeout=1)
            self.assertTrue(result["timed_out"])
            await asyncio.sleep(1.2)
            self.assertFalse((root/"escaped").exists())

    async def test_gateway_eof_terminates_descendants(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"artifacts") as temporary:
            root=Path(temporary)
            result=await self.run_shell(root,'(sleep 1; touch escaped) & wait',disconnect=True)
            self.assertTrue(result["gateway_lost"])
            await asyncio.sleep(1.1)
            self.assertFalse((root/"escaped").exists())

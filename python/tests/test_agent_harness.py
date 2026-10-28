"""Readiness is bounded independently of runner scheduling; failed setup cleans up."""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_agent
import test_slash_commands


class AgentHarnessTests(unittest.IsolatedAsyncioTestCase):
    def delayed_runtime(self, root, delay, native):
        wrapper = root / 'delayed-runtime'
        wrapper.write_text(
            f'#!{sys.executable}\n'
            'import os, signal, sys, time\n'
            'signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n'
            f'time.sleep({delay})\n'
            f'os.execv({str(native)!r}, [{str(native)!r}, *sys.argv[1:]])\n'
        )
        wrapper.chmod(0o700)
        return wrapper

    async def test_cold_start_longer_than_two_seconds_reaches_real_daemon(self):
        with tempfile.TemporaryDirectory(dir=test_agent.ROOT / 'artifacts') as directory:
            runtime = self.delayed_runtime(Path(directory), 3, test_agent.RUNTIME)
            fixture = test_agent.AgentTests('runTest')
            with patch.object(test_agent, 'RUNTIME', runtime):
                try:
                    await fixture.asyncSetUp()
                    status = await fixture.client.call('synth/status', {})
                    self.assertEqual(status['ownerPid'], fixture.process.pid)
                finally:
                    await fixture.cleanup_owned()
            self.assertEqual(fixture.process.returncode, 0)
            self.assertFalse(fixture.root.exists())

    async def test_failed_startup_cleanup_reaps_process_and_closes_provider(self):
        harnesses = [
            (test_agent, test_agent.AgentTests, 'test_activity_running_command_status_advances_without_new_events'),
            (test_slash_commands, test_slash_commands.SlashCommandTests, 'test_host_command_reports_workspace_without_journal_mutation'),
        ]
        for module, case, method in harnesses:
            with self.subTest(harness=case.__name__), tempfile.TemporaryDirectory(dir=test_agent.ROOT / 'artifacts') as directory:
                runtime = self.delayed_runtime(Path(directory), 30, module.RUNTIME)
                fixture = case(method)
                result = unittest.TestResult()
                with patch.object(module, 'RUNTIME', runtime), patch.object(module, 'STARTUP_TIMEOUT', 3):
                    await asyncio.to_thread(fixture.run, result)
                self.assertEqual(len(result.errors) + len(result.failures), 1)
                error = (result.errors + result.failures)[0][1]
                self.assertTrue('daemon startup timeout' in error or 'TimeoutError' in error, error)
                self.assertEqual(fixture.process.returncode, 0)
                if module is test_agent:self.assertFalse(fixture.provider.is_serving())
                self.assertFalse(fixture.root.exists())

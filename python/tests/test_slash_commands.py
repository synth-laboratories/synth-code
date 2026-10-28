"""Keyboard slash commands against a real durable daemon and journal."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from synth_code.app import Preview
from synth_code.client import Client

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=Path(os.environ.get('SYNTH_TEST_RUNTIME',str(ROOT/'target/debug/synth-code-runtime')))

class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'artifacts')
        self.root=Path(self.temp.name)
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.socket=self.root/'agent.sock'
        self.process=await asyncio.create_subprocess_exec(str(RUNTIME),'serve',str(self.root/'store'),
            '--socket',str(self.socket),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        for _ in range(200):
            try:
                self.client=await Client.attach(self.socket);break
            except (FileNotFoundError,ConnectionRefusedError):await asyncio.sleep(.025)
        else: self.fail('owned daemon did not start')
        self.session=(await self.client.call('thread/start',{'mutationId':str(uuid4()),'workspace':str(self.workspace)}))['result']['id']

    async def asyncTearDown(self):
        await self.client.close()
        if self.process.returncode is None:
            self.process.terminate()
        await asyncio.wait_for(self.process.wait(),5)
        self.assertEqual(self.process.returncode,0,(await self.process.stderr.read()).decode())
        self.temp.cleanup()

    async def test_exit_and_quit_detach_with_pending_approval_without_mutation(self):
        operation=(await self.client.call('fixture/admit',{'mutationId':str(uuid4()),'threadId':self.session,'text':'unapproved effect'}))['result']
        before=await self.client.call('synth/status',{})
        for command in ('/exit','/quit'):
            with self.subTest(command=command),create_pipe_input() as input:
                p=Preview(self.client,self.session,input=input,output=DummyOutput())
                await p.restore()
                task=asyncio.create_task(p.run())
                try:
                    await asyncio.sleep(.05)
                    input.send_text('\t\t'+command+'\r')
                    await asyncio.wait_for(task,3)
                finally:
                    if not task.done():task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            # An independent connection proves UI exit did not terminate the owner.
            peer=await Client.attach(self.socket)
            try:
                after=await peer.call('synth/status',{})
                current=(await peer.call('synth/operation/read',{'operationId':operation['id']}))['operation']
                self.assertEqual(after['ownerPid'],before['ownerPid'])
                self.assertEqual(after['sequence'],before['sequence'])
                self.assertEqual(current['status'],'awaiting_approval')
                self.assertEqual(current['request_digest'],operation['request_digest'])
                self.assertEqual(list(self.workspace.iterdir()),[])
            finally:await peer.close()

    async def test_local_commands_preserve_journal_and_clear_does_not_hide_approval(self):
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore()
            before=await self.client.call('synth/status',{})
            await p.admit('/help')
            self.assertIn('/exit',p.transcript.text)
            self.assertIn('/resolve UUID OBSERVATION',p.transcript.text)
            await p.admit('/status')
            self.assertIn(self.session,p.transcript.text)
            await p.admit('/clear')
            self.assertEqual(p.transcript.text,'')
            operation=(await self.client.call('fixture/admit',{'mutationId':str(uuid4()),'threadId':self.session,'text':'pending'}))['result']
            p.show_pending(operation)
            decision={'mutationId':str(uuid4()),'operationId':operation['id'],'allow':False}
            p.decision=decision
            after_proposal=await self.client.call('synth/status',{})
            await p.admit('/clear')
            self.assertIn('Fixture ·',p.transcript.text)
            self.assertIs(p.decision,decision)
            self.assertEqual(p.pending['id'],operation['id'])
            self.assertEqual((await self.client.call('synth/status',{}))['sequence'],after_proposal['sequence'])
            self.assertGreater(after_proposal['sequence'],before['sequence'])
            self.assertEqual(list(self.workspace.iterdir()),[])

    async def test_stop_cancels_a_running_command_without_a_delayed_effect(self):
        operation=(await self.client.call('workspace/proposeCommand',{'mutationId':str(uuid4()),
            'threadId':self.session,'request':{'command':'sleep 30; touch forbidden',
                                             'timeout_seconds':60,'output_limit':1024}}))['result']
        await self.client.call('synth/approval/respond',{'mutationId':str(uuid4()),
            'operationId':operation['id'],'requestDigest':operation['request_digest'],'allow':True})
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            self.assertEqual(preview.active_operation,operation['id'])
            await preview.admit('/stop')
            for _ in range(100):
                if preview.active_operation is None:break
                await asyncio.sleep(.025)
            current=(await self.client.call('synth/operation/read',{'operationId':operation['id']}))['operation']
            self.assertIn(current['status'],{'canceled','failed'})
            self.assertFalse((self.workspace/'forbidden').exists())
            for task in list(preview.tasks):task.cancel()
            await asyncio.gather(*preview.tasks,return_exceptions=True)

    async def test_provider_commands_change_real_session_settings_and_validate_choices(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            await preview.admit('/effort high')
            self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['effort'],'high')
            self.assertIn('high',preview.provider_summary)
            await preview.admit('/provider reflection')
            await preview.admit('/model beam')
            await preview.admit('/model unsupported')
            self.assertIn('Unsupported model',preview.transcript.text)
            self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['model'],'Beam-1.0-501B-A23B')

    async def test_effort_picker_applies_and_escape_does_not_change_settings(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            await preview.admit('/effort')
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(.05)
                input.send_text('\x1b[B \t\r')
                for _ in range(100):
                    current=await self.client.call('synth/provider/status',{'threadId':self.session})
                    if current['effort']=='high':break
                    await asyncio.sleep(.025)
                self.assertEqual(current['effort'],'high')
                before=(await self.client.call('synth/status',{}))['sequence']
                await preview.admit('/provider')
                input.send_text('\x1b')
                for _ in range(100):
                    if not preview.settings_modal:break
                    await asyncio.sleep(.025)
                self.assertFalse(preview.settings_modal)
                self.assertEqual((await self.client.call('synth/status',{}))['sequence'],before)
                input.send_text('\x11');await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)

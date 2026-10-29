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
STARTUP_TIMEOUT=40

class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'artifacts')
        self.process=self.client=None
        self.addAsyncCleanup(self.cleanup_owned)
        self.root=Path(self.temp.name)
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.socket=self.root/'agent.sock'
        self.process=await asyncio.create_subprocess_exec(str(RUNTIME),'serve',str(self.root/'store'),
            '--socket',str(self.socket),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        async with asyncio.timeout(STARTUP_TIMEOUT):
            while True:
                if self.process.returncode is not None:
                    self.fail((await self.process.stderr.read()).decode())
                try:
                    self.client=await Client.attach(self.socket);break
                except (FileNotFoundError,ConnectionRefusedError):await asyncio.sleep(.025)
        self.session=(await self.client.call('thread/start',{'mutationId':str(uuid4()),'workspace':str(self.workspace)}))['result']['id']

    async def cleanup_owned(self):
        try:
            if self.client:await self.client.close()
        finally:
            try:
                if self.process:
                    if self.process.returncode is None:self.process.terminate()
                    try:await asyncio.wait_for(self.process.wait(),10)
                    except TimeoutError:
                        self.process.kill();await self.process.wait()
                    self.assertEqual(self.process.returncode,0,(await self.process.stderr.read()).decode())
            finally:self.temp.cleanup()

    async def test_exit_and_quit_detach_with_pending_approval_without_mutation(self):
        operation=(await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),'threadId':self.session,
            'path':'unapproved.txt','expectedSha256':None,'content':'unapproved effect'}))['result']
        before=await self.client.call('synth/status',{})
        for command in ('/exit','/quit'):
            with self.subTest(command=command),create_pipe_input() as input:
                p=Preview(self.client,self.session,input=input,output=DummyOutput())
                await p.restore()
                task=asyncio.create_task(p.run())
                try:
                    await asyncio.sleep(.05)
                    input.send_text('\t'+command+'\r')  # tab leaves the approval panel for the composer
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
            operation=(await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),'threadId':self.session,
                'path':'pending.txt','expectedSha256':None,'content':'pending'}))['result']
            p.show_pending(operation)
            decision={'mutationId':str(uuid4()),'operationId':operation['id'],'allow':False}
            p.decision=decision
            after_proposal=await self.client.call('synth/status',{})
            await p.admit('/clear')
            self.assertIn('Edit · pending.txt',p.transcript.text)
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

    async def test_settings_changed_during_a_turn_are_queued_then_applied(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            before=(await self.client.call('synth/provider/status',{'threadId':self.session}))['effort']
            self.assertEqual(preview.settings_apply,'compaction')
            preview.active_turn='running-turn'
            await preview.admit('/effort high')
            self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['effort'],before)
            self.assertIn('Effort · high · queued, applies at the next compaction; a running turn keeps its settings',preview.transcript.text)
            self.assertIn('queued: effort → high',preview.footer_text()[1][1])
            await preview.admit('/settings-apply turn-end')
            await preview.admit('/model beam')
            self.assertIn('Model · Beam 1.0 · queued, applies when this turn ends',preview.transcript.text)
            preview.active_turn=None
            await preview.apply_queued_settings()
            self.assertEqual((await self.client.call('synth/provider/status',{'threadId':self.session}))['effort'],'high')
            self.assertIn('Effort · high · applied (was queued)',preview.transcript.text)
            self.assertEqual(preview.queued_settings,{})
            self.assertNotIn('queued',preview.footer_text()[1][1])
            await preview.admit('/settings-apply sometimes')
            self.assertIn('Supported · compaction, turn-end',preview.transcript.text)

    async def test_commands_during_a_turn_run_queue_or_refuse_by_kind(self):
        (self.workspace/'note.txt').write_text('hello\n')
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            preview.active_turn='running-turn'
            await preview.admit('/host')
            self.assertIn(str(self.workspace),preview.transcript.text)
            await preview.admit('/permissions auto-edit')
            self.assertEqual((await self.client.call('synth/permissions/status',{'threadId':self.session}))['mode'],'auto-edit')
            self.assertIn("applies from the agent's next action",preview.transcript.text)
            await preview.admit('/compact')
            self.assertIn('/compact unavailable while the agent is working',preview.transcript.text)
            self.assertFalse(preview.settings_modal)
            await preview.admit('also try wood first')
            self.assertEqual(preview.queued_messages,['also try wood first'])
            queue=''.join(t for _,t in preview.queue_fragments())
            self.assertIn('Queued · 1 message',queue)
            self.assertIn('1. also try wood first',queue)
            self.assertNotIn('also try wood first',preview.transcript.text)
            # Up in the empty input pulls it back for editing; sending queues it again.
            binding=next(b for b in preview.application.key_bindings.bindings if b.keys==('up',) and b.handler.__name__=='edit_queued_')
            binding.handler(None)
            self.assertEqual(preview.composer.text,'also try wood first')
            self.assertEqual(preview.queued_messages,[])
            preview.composer.text=''
            await preview.admit('also try wood first')
            self.assertIn('queued: 1 message',preview.footer_text()[1][1])
            self.assertIn('esc interrupt',preview.footer_text()[1][1])
            for task in list(preview.tasks):task.cancel()
            await asyncio.gather(*preview.tasks,return_exceptions=True)

    # One plausible invocation per command. A new command without an entry here
    # fails this test, so its author must exercise its during-turn policy.
    SAMPLE_ARGUMENTS={'/help':'','/clear':'','/status':'','/stop':'','/details':'','/copy':'','/compact':'','/host':'',
        '/containers':'none-such','/effort':'high','/provider':'reflection','/model':'beam',
        '/permissions':'auto-edit','/settings-apply':'turn-end','/resume':str(uuid4()),'/output':str(uuid4()),'/resolve':str(uuid4())+' absent'}
    # Detaching exits the app; test_exit_and_quit_detach_with_pending_approval_without_mutation covers it.
    EXITS={'/exit','/quit'}

    async def test_every_command_follows_its_during_turn_policy(self):
        from synth_code.app import COMMANDS, DuringTurn
        self.assertEqual(set(COMMANDS),set(self.SAMPLE_ARGUMENTS)|self.EXITS,
                         'add a SAMPLE_ARGUMENTS entry for each new slash command')
        (self.workspace/'note.txt').write_text('hello\n')
        for name,spec in COMMANDS.items():
            self.assertIsInstance(spec.during_turn,DuringTurn,name)
            self.assertTrue(spec.why,name)
            if name in self.EXITS:
                continue
            with self.subTest(command=name), create_pipe_input() as input:
                preview=Preview(self.client,self.session,input=input,output=DummyOutput())
                await preview.restore()
                preview.active_turn='running-turn'
                before=len(preview.transcript.text)
                await preview.admit((name+' '+self.SAMPLE_ARGUMENTS[name]).strip())
                said=preview.transcript.text[before:]
                refused='unavailable while the agent is working' in said or 'Unknown command' in said
                self.assertEqual(refused,spec.during_turn is DuringTurn.REFUSE,said)
                self.assertNotIn('Settings unavailable · task active',said)
                self.assertNotIn('Permissions unavailable · task active',said)
                if spec.during_turn is DuringTurn.QUEUE:
                    self.assertIn('queued',said)
                    self.assertTrue(preview.queued_settings,name)
                self.assertFalse(preview.settings_modal,name)
                for task in list(preview.tasks):task.cancel()
                await asyncio.gather(*preview.tasks,return_exceptions=True)
            if name=='/permissions':
                await self.client.call('synth/permissions/configure',{'mutationId':str(uuid4()),'threadId':self.session,
                    'mode':'manual','expectedRevision':(await self.client.call('synth/permissions/status',{'threadId':self.session}))['revision']})

    def test_pasted_paths_are_messages_not_commands(self):
        from synth_code.app import split_command
        self.assertEqual(split_command('/Users/me/prompt.txt'),('','/Users/me/prompt.txt'))
        self.assertEqual(split_command('/effort high'),('/effort','high'))

    async def test_effort_picker_applies_and_escape_does_not_change_settings(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            await preview.admit('/effort')
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(.05)
                input.send_text('\x1b[B\r')
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

    async def test_picker_filters_as_you_type_and_keeps_the_draft(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            preview.composer.text='keep this draft'
            await preview.admit('/effort')
            self.assertTrue(preview.settings_modal)
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(.05)
                input.send_text('xh')
                for _ in range(100):
                    if preview.picker_filter.text=='xh':break
                    await asyncio.sleep(.025)
                heading,body=preview.picker_fragments()
                self.assertIn('1 of',''.join(t for _,t in heading))
                self.assertIn('› Xhigh',''.join(t for _,t in body))
                self.assertIn('enter select',preview.footer_text()[1][1])
                input.send_text('\r')
                for _ in range(100):
                    current=await self.client.call('synth/provider/status',{'threadId':self.session})
                    if current['effort']=='xhigh':break
                    await asyncio.sleep(.025)
                self.assertEqual(current['effort'],'xhigh')
                self.assertFalse(preview.settings_modal)
                self.assertEqual(preview.composer.text,'keep this draft')
                # Nothing matching: enter does nothing and the picker stays open.
                await preview.admit('/effort')
                input.send_text('zzz\r')
                await asyncio.sleep(.1)
                self.assertTrue(preview.settings_modal)
                self.assertIn('No matches',''.join(t for _,t in preview.picker_fragments()[1]))
                input.send_text('\x1b')
                for _ in range(100):
                    if not preview.settings_modal:break
                    await asyncio.sleep(.025)
                input.send_text('\x11');await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_resume_picker_switches_session_without_a_model_request(self):
        other=self.workspace/'other-project';other.mkdir()
        started=await self.client.call('thread/start',{'mutationId':str(uuid4()),'workspace':str(other)})
        target=started['result']['id']
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            await preview.admit('/resume '+self.session)
            self.assertIn('Already in this session',preview.transcript.text)
            await preview.admit('/resume')
            self.assertTrue(preview.settings_modal)
            labels=[choice[1] for choice in preview.picker['choices']]
            self.assertTrue(any('other-project · 0 turns' in label and '(no task yet)' in label for label in labels),labels)
            before=(await self.client.call('synth/status',{}))['sequence']
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(.05)
                input.send_text('other\r')
                await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            self.assertEqual(preview.resume_session,target)
            self.assertEqual((await self.client.call('synth/status',{}))['sequence'],before)
        with create_pipe_input() as input:
            # The launcher reopens on the chosen session.
            preview=Preview(self.client,target,input=input,output=DummyOutput())
            await preview.restore()
            self.assertEqual(preview.workspace_path,str(other))

    async def test_permissions_picker_and_commands_apply_to_real_runtime(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            self.assertEqual(preview.permissions,'manual')
            await preview.admit('/permissions yolo')
            self.assertEqual(preview.permissions,'yolo')
            self.assertIn('YOLO',''.join(text for _,text in preview.footer_text()))
            # The runtime applies yolo at once: an edit proposed now runs without approval.
            result=await self.client.call('workspace/proposeEdit',{'mutationId':str(uuid4()),'threadId':self.session,
                'path':'automatic.txt','expectedSha256':None,'content':'saved\n','expectedPermissions':preview.permissions})
            self.assertEqual(result['result']['status'],'succeeded')
            self.assertEqual((self.workspace/'automatic.txt').read_text(),'saved\n')
            await preview.admit('/permissions manual')
            await preview.admit('/permissions invalid')
            self.assertIn('Unsupported permissions',preview.transcript.text)
            await preview.admit('/permissions')
            task=asyncio.create_task(preview.run())
            try:
                await asyncio.sleep(.05)
                # Move to Auto-edit and select it.
                input.send_text('\x1b[B\r')
                for _ in range(100):
                    if preview.permissions=='auto-edit':break
                    await asyncio.sleep(.025)
                self.assertEqual(preview.permissions,'auto-edit')
                before=(await self.client.call('synth/status',{}))['sequence']
                await preview.admit('/permissions')
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

    async def test_permissions_stale_selection_and_pending_work_cannot_escalate(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            old=await self.client.call('synth/permissions/status',{'threadId':self.session})
            await preview.admit('/permissions auto-edit')
            await preview.configure_permissions('yolo',old)
            self.assertEqual(preview.permissions,'auto-edit')
            self.assertIn('session_permissions_conflict',preview.transcript.text)
            op=(await self.client.call('workspace/proposeCommand',{'mutationId':str(uuid4()),'threadId':self.session,
                'request':{'command':'touch forbidden','timeout_seconds':2,'output_limit':1024}}))['result']
            # Loosening while an action awaits approval applies to later actions only.
            preview.show_pending(op)
            decision={'mutationId':str(uuid4()),'operationId':op['id'],'allow':False}
            preview.decision=decision
            await preview.admit('/permissions yolo')
            self.assertEqual(preview.permissions,'yolo')
            self.assertIn('the pending approval still needs your decision',preview.transcript.text)
            self.assertIs(preview.decision,decision)
            self.assertEqual(preview.pending['id'],op['id'])
            current=(await self.client.call('synth/operation/read',{'operationId':op['id']}))['operation']
            self.assertEqual(current['status'],'awaiting_approval')
            self.assertFalse((self.workspace/'forbidden').exists())

    async def test_permissions_stale_footer_refuses_new_edit_and_refreshes_without_retry(self):
        with create_pipe_input() as input:
            preview=Preview(self.client,self.session,input=input,output=DummyOutput())
            await preview.restore()
            status=await self.client.call('synth/permissions/status',{'threadId':self.session})
            await self.client.call('synth/permissions/configure',{'mutationId':str(uuid4()),'threadId':self.session,
                'mode':'yolo','expectedRevision':status['revision']})
            # A task started from the stale manual footer is refused; the footer refreshes and nothing retries.
            await preview.admit('write not-approved.txt')
            self.assertEqual(preview.permissions,'yolo')
            self.assertIn('session_permissions_changed',preview.transcript.text)
            self.assertEqual((await self.client.call('turn/list',{'threadId':self.session}))['turns'],[])
            self.assertFalse((self.workspace/'not-approved.txt').exists())

    async def test_host_command_reports_workspace_without_journal_mutation(self):
        with create_pipe_input() as input:
            p=Preview(self.client,self.session,input=input,output=DummyOutput())
            await p.restore();before=(await self.client.call('synth/status',{}))['sequence']
            await p.admit('/host')
            self.assertIn(str(self.workspace),p.transcript.text)
            self.assertIn('docker ·',p.transcript.text)
            await p.admit('/containers bad\nquery')
            self.assertIn('invalid_container_query',p.transcript.text)
            self.assertEqual((await self.client.call('synth/status',{}))['sequence'],before)

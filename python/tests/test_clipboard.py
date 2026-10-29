import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from synth_code.app import Preview
from synth_code.clipboard import MAX_COPY_BYTES, ResponseCopy, response_text, write_clipboard

ROOT=Path(__file__).resolve().parents[2]

class ClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_pagination_copies_exact_source_and_refuses_bad_cursors(self):
        text='**Markdown**\n```python\nprint("hello")\n```\n'+'Ω'*9000
        class Client:
            async def call(self, method, params):
                self_method=method
                assert self_method=='turn/message/read'
                assert params['turnId']=='turn' and params['index']==3
                start=params['after'];end=min(len(text),start+2048)
                return {'text':text[start:end],'nextCursor':end if end<len(text) else None}
        self.assertEqual(await response_text(Client(),ResponseCopy('display clipped',('turn',3))),text)
        class Bad:
            async def call(self,*args):return {'text':'x','nextCursor':0}
        with self.assertRaises(ValueError):await response_text(Bad(),ResponseCopy('',('turn',0)))
        with self.assertRaises(ValueError):await response_text(None,ResponseCopy('clipped',complete=False))
        class Large:
            async def call(self,*args):return {'text':'x'*(MAX_COPY_BYTES+1),'nextCursor':None}
        with self.assertRaises(ValueError):await response_text(Large(),ResponseCopy('',('turn',0)))

    async def test_click_copies_only_selected_response_and_preserves_approval_and_focus(self):
        copied=[]
        async def writer(text):copied.append(text)
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput(),clipboard_writer=writer)
            p.append('**First**\n```python\nprint(1)\n```','assistant')
            p.append('user text with ⧉','user')
            p.append('Second','assistant')
            p.pending={'id':'pending','status':'awaiting_approval'};p.decision={'mutationId':'keep','allow':False}
            p.application.layout.focus(p.deny)
            row,(start,end,target)=next(iter(p.copy_actions.items()))
            def event(kind,x=start):return MouseEvent(Point(x,row),kind,MouseButton.LEFT,frozenset())
            p.copy_mouse(event(MouseEventType.MOUSE_DOWN));p.copy_mouse(event(MouseEventType.MOUSE_UP))
            await asyncio.sleep(.02)
            self.assertEqual(copied,['**First**\n```python\nprint(1)\n```'])
            self.assertTrue(p.application.layout.has_focus(p.deny))
            self.assertEqual(p.pending['id'],'pending');self.assertEqual(p.decision['mutationId'],'keep')
            self.assertIn('✓ copied',p.transcript.text)
            # A bare release, or a press beside the label, does not copy; a model-supplied symbol is not a button.
            start,end,_=p.copy_actions[row]
            count=len(copied);p.copy_mouse(event(MouseEventType.MOUSE_UP))
            p.copy_mouse(event(MouseEventType.MOUSE_DOWN,x=end+1));await asyncio.sleep(.01)
            self.assertEqual(len(copied),count)
            # The press alone copies, so a release that drifts off the label still works.
            p.copy_mouse(event(MouseEventType.MOUSE_DOWN,x=end-1));await asyncio.sleep(.02)
            self.assertEqual(len(copied),count+1)
            await p.admit('/copy');await asyncio.sleep(.02)
            self.assertEqual(copied[-1],'Second')
            self.assertEqual(len(p.copy_actions),2)
            for task in list(p.tasks):task.cancel()
            await asyncio.gather(*p.tasks,return_exceptions=True)

    async def test_copy_failure_clear_and_large_transcript_keep_state_bounded(self):
        async def failure(text):raise FileNotFoundError('no clipboard')
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput(),clipboard_writer=failure)
            for i in range(50):p.append(str(i)+'x'*4000,'assistant')
            self.assertLessEqual(len(p.transcript.text),100000)
            self.assertEqual(len(p.copy_targets),len(p.entries))
            target=next(iter(p.copy_targets.values()));p.status.text='Outcome unknown'
            task=asyncio.create_task(p.copy_response(target));await asyncio.sleep(.02)
            self.assertEqual(p.status.text,'Outcome unknown');self.assertIn('copy unavailable',p.transcript.text)
            await p.admit('/clear');self.assertEqual(p.copy_targets,{});self.assertEqual(p.copy_actions,{})
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_clipboard_helper_receives_stdin_without_shell_and_checks_failure(self):
        # Use an owned stand-in so tests never overwrite the owner's pasteboard.
        with tempfile.TemporaryDirectory(dir=ROOT/'artifacts') as name:
            root=Path(name);capture=root/'bytes';helper=root/'clipboard';helper.write_text('#!'+str(Path(os.sys.executable).resolve())+'\nimport sys\nfrom pathlib import Path\nPath('+repr(str(capture))+').write_bytes(sys.stdin.buffer.read())\n');helper.chmod(0o700)
            await write_clipboard('héllo\n```code```',executable=str(helper))
            self.assertEqual(capture.read_text(),'héllo\n```code```')
            helper.write_text('#!/bin/sh\nexit 1\n')
            with self.assertRaises(RuntimeError):await write_clipboard('text',executable=str(helper))
            with self.assertRaises(ValueError):await write_clipboard('x'*(MAX_COPY_BYTES+1),executable=str(helper))

    async def test_focused_keyboard_copy_preserves_composer_binding_and_focus(self):
        copied=[]
        async def writer(text):copied.append(text)
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput(),clipboard_writer=writer)
            p.append('Keyboard response','assistant')
            p.application.layout.focus(p.transcript)
            p.transcript.buffer.cursor_position=0
            task=asyncio.create_task(p.run())
            try:
                await asyncio.sleep(.05);input.send_text('\x19')
                for _ in range(100):
                    if copied:break
                    await asyncio.sleep(.01)
                self.assertEqual(copied,['Keyboard response'])
                self.assertTrue(p.application.layout.has_focus(p.transcript))
                p.application.layout.focus(p.composer);input.send_text('draft\x19')
                await asyncio.sleep(.03)
                self.assertEqual(copied,['Keyboard response'])
                self.assertEqual(p.composer.text,'draft')
                input.send_text('\x11');await asyncio.wait_for(task,3)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)

    async def test_cleared_response_cannot_write_a_delayed_clipboard_result(self):
        entered=asyncio.Event();release=asyncio.Event();copied=[]
        class Client:
            async def call(self,*args):
                entered.set();await release.wait();return {'text':'old response','nextCursor':None}
        async def writer(text):copied.append(text)
        with create_pipe_input() as input:
            p=Preview(Client(),'fixture',input=input,output=DummyOutput(),clipboard_writer=writer)
            p.append('old response','assistant',source=('turn',0))
            target=next(iter(p.copy_targets.values()));task=asyncio.create_task(p.copy_response(target))
            await entered.wait();await p.admit('/clear');release.set();await asyncio.wait_for(task,2)
            self.assertEqual(copied,[])
            self.assertFalse(target.copying)

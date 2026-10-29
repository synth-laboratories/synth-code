"""Adversarial model output must remain inert, bounded and renderable."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from prompt_toolkit.data_structures import Point
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType, MouseButton
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from synth_code.app import COMMANDS, Preview, activity_fragments, command_fragments
from synth_code.transcript import markdown_lines as markdown

def plain(lines):
    return '\n'.join(''.join(value for _, value in line) for line in lines)

class PresentationTests(unittest.TestCase):
    def test_title_footer_and_welcome_show_state_without_control_characters(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.workspace_name='repo\x1b]0;evil\x07'
            self.assertEqual(preview.terminal_title(),'✦ · Synth Code · repo]0;evil')
            preview.append('Fix the parser\x07 please','user')
            preview.active_turn='t'
            self.assertTrue(preview.terminal_title(1).startswith('⠙ · Fix the parser'))
            preview.pending={'id':'op'}
            self.assertTrue(preview.terminal_title().startswith('✋ awaiting approval'))
            preview.pending=None;preview.active_turn=None
            footer=lambda:''.join(t for _,t in preview.footer_text())
            self.assertNotIn('ctx',footer())
            preview.context_tokens=50_000
            self.assertIn('ctx 25%',footer())
            preview.context_tokens=20
            self.assertIn('ctx <1%',footer())
            self.assertIn('ctrl-c interrupt or clear',''.join(t for _,t in preview.header_fragments()))

    def test_ctrl_c_clears_input_and_never_exits_when_idle(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            binding=next(b for b in preview.application.key_bindings.bindings if b.keys==('c-c',))
            event=SimpleNamespace(app=preview.application)
            preview.composer.text='draft'
            binding.handler(event)
            self.assertEqual(preview.composer.text,'')
            binding.handler(event)
            self.assertIn('ctrl-d to exit',preview.status.text)
            self.assertFalse(preview.application.future)

    def test_action_rows_highlight_verb_command_and_counts(self):
        styles=dict((t,st) for st,t in command_fragments("cd runs && grep -n 'a b' x.py 2>&1 | tail -3"))
        self.assertEqual(styles["cd"],"class:program")
        self.assertEqual(styles["grep"],"class:program")
        self.assertEqual(styles["tail"],"class:program")
        self.assertEqual(styles["-n"],"class:flag")
        self.assertEqual(styles["'a b'"],"class:string")
        self.assertEqual(styles["&&"],"class:operator")
        self.assertEqual(styles["2>&1"],"class:operator")
        row=activity_fragments("• Edited policy.py (+40 −4) · outside workspace")
        self.assertEqual(''.join(t for _,t in row),"• Edited policy.py (+40 −4) · outside workspace")
        self.assertIn(("class:verb","Edited "),row)
        self.assertIn(("class:count-add","+40"),row)
        self.assertIn(("class:count-del","−4"),row)
        self.assertEqual(activity_fragments("✗ Failed · timeout")[0][0],"class:failed")
        # Fragments always reproduce the text exactly, whatever the command.
        for command in ["echo \"unterminated", "a'b", "x>>y", "(cd a; b) &", ""]:
            self.assertEqual(''.join(t for _,t in command_fragments(command)),command)

    def test_finished_actions_show_a_short_excerpt(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.operation_result({"id":"op","status":"succeeded","shell":{"command":"ls"},
                                      "outcome":{"exit_code":0,"stdout":"a\nb\nc\nd\ne\n"}})
            text=preview.transcript.text
            self.assertIn("• Ran ls\n  └ a\n    b\n    c\n    … +2 lines",text)
            preview.operation_result({"id":"ed","status":"succeeded","edit":{"path":"p.py","before":"x\n","content":"y\n"},"outcome":{}})
            self.assertIn("• Edited p.py (+1 −1)\n  └ -x\n    +y",preview.transcript.text)

    def test_action_cards_expand_inline_without_the_side_pane(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            self.assertFalse(preview.details_visible())
            output='\n'.join(f'line {i}' for i in range(40))
            preview.operation_result({'id':'op','status':'succeeded','shell':{'command':'make test'},
                                      'outcome':{'exit_code':0,'stdout':output}})
            text=preview.transcript.text
            self.assertIn('• Ran make test\n  └ line 0',text)
            self.assertIn('… +37 lines · ctrl-o or click to expand',text)
            self.assertNotIn('line 39',text)
            self.assertIn('ctrl-o expand',preview.footer_text()[1][1])
            # A click on the card heading expands that card in place.
            row=preview.transcript.text.splitlines().index('• Ran make test')
            preview.copy_mouse(MouseEvent(Point(3,row),MouseEventType.MOUSE_DOWN,MouseButton.LEFT,frozenset()))
            self.assertIn('line 39',preview.transcript.text)
            preview.copy_mouse(MouseEvent(Point(3,row),MouseEventType.MOUSE_DOWN,MouseButton.LEFT,frozenset()))
            self.assertNotIn('line 39',preview.transcript.text)
            # ctrl-o expands every card, then returns to the compact view.
            binding=next(b for b in preview.application.key_bindings.bindings
                         if b.keys==('c-o',) and b.handler.__name__=='transcript_full_')
            binding.handler(None)
            self.assertIn('line 39',preview.transcript.text)
            self.assertIn('ctrl-o compact',preview.footer_text()[1][1])
            binding.handler(None)
            self.assertNotIn('line 39',preview.transcript.text)
            # Failures open expanded with the whole diff or output on the card.
            preview.operation_result({'id':'bad','status':'failed','shell':{'command':'make check'},
                                      'outcome':{'exit_code':2,'stderr':'\n'.join(f'err {i}' for i in range(12))}})
            self.assertIn('err 11',preview.transcript.text)
            preview.operation_result({'id':'lost','status':'unknown','shell':{'command':'deploy'},'outcome':{}})
            row=preview.transcript.text.splitlines().index('✗ Unknown · deploy')
            self.assertTrue(all('warning' in style for style,_ in preview.transcript.lexer.rendered_lines[row]))

    def test_over_budget_items_keep_their_last_render_until_a_later_pass(self):
        async def scenario():
            with create_pipe_input() as input:
                preview=Preview(None,'fixture',input=input,output=DummyOutput())
                for i in range(5):
                    preview.operation_result({'id':f'op{i}','status':'succeeded','shell':{'command':f'step {i}'},
                                              'outcome':{'exit_code':0,'stdout':'\n'.join(f'out {i} {n}' for n in range(9))}})
                task=asyncio.create_task(preview.run())
                try:
                    while not preview.application.is_running:
                        await asyncio.sleep(.01)
                    # No budget: each pass renders one item; the rest show their previous render.
                    preview.render_budget=0
                    preview.full_transcript=True
                    preview.refresh_transcript()
                    self.assertIn('out 4 8',preview.transcript.text)
                    self.assertNotIn('out 0 8',preview.transcript.text)
                    for _ in range(100):
                        if 'out 0 8' in preview.transcript.text:break
                        await asyncio.sleep(.01)
                    self.assertTrue(all(f'out {i} 8' in preview.transcript.text for i in range(5)))
                    self.assertFalse(preview.render_pending)
                    preview.application.exit()
                    await asyncio.wait_for(task,3)
                finally:
                    if not task.done():task.cancel()
        asyncio.run(scenario())

    def test_agent_markdown_has_a_gutter_lists_and_highlighted_code(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.append('Done:\n\n1. read\n2. fixed\n\n```python\ndef f():\n    return 1\n```','assistant')
            lines=preview.transcript.text.splitlines()
            self.assertEqual(lines[0],'● Done:')
            self.assertIn('   1. read',lines)
            self.assertIn('   2. fixed',lines)
            row=next(i for i,line in enumerate(lines) if 'def f' in line)
            styles=[style for style,_ in preview.transcript.lexer.rendered_lines[row]]
            self.assertTrue(any('bg:#' in style for style in styles),styles)
            self.assertGreater(len({style for style in styles if style}),2,styles)

    def test_scrolling_up_holds_the_view_while_output_arrives(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            for i in range(60):
                preview.append(f'line {i}','assistant')
            window=preview.transcript.window
            rows=preview.transcript.text.count('\n')+1
            window.render_info=SimpleNamespace(content_height=rows,window_height=20)
            window.vertical_scroll=rows-20
            wheel=lambda kind:preview.copy_mouse(MouseEvent(Point(0,0),kind,MouseButton.NONE,frozenset()))
            wheel(MouseEventType.SCROLL_UP)
            self.assertEqual(window.vertical_scroll,rows-23)
            self.assertFalse(preview.following)
            view=preview.transcript.buffer.document.cursor_position_row
            self.assertTrue(rows-23<=view<rows-3)
            # New output does not pull the reader back down.
            preview.append('more','assistant')
            self.assertEqual(preview.transcript.buffer.document.cursor_position_row,view)
            self.assertIn('scrolled up',''.join(t for _,t in preview.footer_text()))
            preview.scroll_transcript(-preview.page_rows())
            self.assertEqual(window.vertical_scroll,rows-41)
            preview.scroll_transcript(10_000)
            self.assertTrue(preview.following)
            preview.append('latest','assistant')
            self.assertEqual(preview.transcript.buffer.document.cursor_position_row,
                             preview.transcript.text.rstrip().count('\n'))

    def test_table_like_text_and_code_keep_their_characters(self):
        # GFM reads this as a one-column table whose header is '---'; nothing is lost.
        self.assertIn('---', plain(markdown('|---|\n|---|')))
        self.assertIn('**literal**', plain(markdown('```python\nprint("**literal**")\n```')))

    def test_ragged_and_wide_tables_cannot_amplify_render_budget(self):
        cases = [
            '|a|b|\n|---|---|\n|' + 'x'*3000 + '|y|\n' + '|z|q|\n'*10000,
            '|'*20000+'\n|---|\n'+'|a|\n'*10000,
        ]
        for source in cases:
            self.assertLessEqual(len(plain(markdown(source))), 100000)
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            for _ in range(30):
                preview.append('|a|b|\n|---|---|\n|'+'x'*1000+'|b|\n'+'|z|q|\n'*100,'assistant')
            self.assertLessEqual(len(preview.transcript.text),100000)

    def test_unmatched_link_openers_do_not_stall_rendering_or_emit_controls(self):
        start=time.monotonic()
        self.assertEqual(plain(markdown('['*99000)).replace('\n',''), '['*99000)
        self.assertLess(time.monotonic()-start,2)
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.append('**text**\x1b[2J\u202e [link](https://example.test)','assistant')
            self.assertNotIn('\x1b',preview.transcript.text)
            self.assertNotIn('\u202e',preview.transcript.text)
            self.assertIn('https://example.test',preview.transcript.text)
            self.assertNotIn('**text**',preview.transcript.text)

    def test_successful_capped_output_keeps_the_partial_output_notice(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.operation_result({'id':'bounded-command','status':'succeeded',
                'outcome':{'exit_code':0,'truncated':True,'stdout':'partial'}})
            self.assertIn('output truncated',preview.transcript.text)
            self.assertIn('partial',preview.transcript.text)

    def test_routine_tool_successes_are_one_line_rows_with_output_in_details(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.workspace_path='/work/space'
            preview.append('Find the project','user')
            preview.operation_result({'id':'0','status':'succeeded',
                'shell':{'command':'cd /work/space && printf found\necho two'},'outcome':{'exit_code':0,'stdout':'found\x1b[2J'}})
            self.assertIn('• Ran printf found (+1 lines)',preview.transcript.text)
            self.assertNotIn('cd /work/space',preview.transcript.text)
            self.assertNotIn('found\n',preview.transcript.text.split('• Ran')[1])
            self.assertIn('found',preview.details.text)
            self.assertNotIn('\x1b',preview.details.text)
            preview.operation_result({'id':'edit','status':'succeeded',
                'edit':{'path':'note.txt','before':'before\nsame','content':'after\nsame\nmore'},'outcome':{}})
            self.assertIn('• Edited note.txt (+2 −1)',preview.transcript.text)
            self.assertIn('+after',preview.details.text)
            preview.operation_result({'id':'bad','status':'failed',
                'shell':{'command':'python3 -c boom'},'outcome':{'exit_code':1,'stderr':'Traceback'}})
            self.assertIn('✗ Failed · exit 1 · python3 -c boom',preview.transcript.text)
            self.assertNotIn('Command completed',preview.transcript.text)

    def test_tables_fit_the_width_and_wrap_inside_columns(self):
        from synth_code.transcript import markdown_lines as markdown
        from prompt_toolkit.utils import get_cwidth
        table=("| Iter | Hypothesis | Change | Mean score | Kept/Reverted |\n|---|---|---|---|---|\n"
               "| 0 | Starter noop | baseline | 0.0000 | baseline |\n"
               "| 1 | Policy never collects wood; move to nearest tree and chop | policy.py heuristic | 0.0455 | kept (committed) |")
        for width in (100,60):
            lines=["".join(t for _,t in row) for row in markdown(table,width=width)]
            self.assertTrue(all(get_cwidth(line.rstrip())<=width for line in lines),(width,lines))
            body="\n".join(lines)
            self.assertIn('kept (committed)' if width==100 else 'kept',body)
            starts=[line.index('0.0455') for line in lines if '0.0455' in line]
            means=[line.index('Mean') for line in lines if 'Mean' in line]
            self.assertEqual(starts,means,lines)  # numbers stay under their header

    def test_approval_docks_above_the_composer_with_deny_preselected(self):
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput())
            text=lambda:''.join(f[1] for f in p.approval_fragments())
            p.permissions='manual'
            p.pending={'id':'e','status':'awaiting_approval','edit':{'path':'policy.py','before':'a\n','content':'b\n'}}
            self.assertIn('Approval required · Edit policy.py (+1 −1)',text())
            self.assertIn(' Allow ',text())
            self.assertIn(' Allow edits this session ',text())
            self.assertIn(' Deny ',text())
            p.show_pending(p.pending)
            # Deny is preselected; the composer keeps focus.
            self.assertEqual(p.approval_options()[p.approval_choice][2],'deny')
            self.assertTrue(p.application.layout.has_focus(p.composer))
            p.permissions='auto-edit'
            p.pending={'id':'c','status':'awaiting_approval','shell':{'command':'\n'.join(f'echo {i}' for i in range(9)),'timeout_seconds':5}}
            self.assertIn('+4 lines · ctrl-o to expand',text())
            p.approval_expanded=True
            self.assertIn('echo 8',text())
            p.approval_expanded=False
            p.pending={'id':'c','status':'awaiting_approval','shell':{'command':'./measure.sh','timeout_seconds':5}}
            self.assertIn('$ ./measure.sh',text())
            self.assertIn(" Allow all this session (yolo) ",text())
            p.permissions='yolo'
            self.assertEqual([o[2] for o in p.approval_options()],['allow','deny'])
            p.pending={**p.pending,'status':'ready'}
            self.assertEqual([o[0] for o in p.approval_options()],['Run it now','Cancel'])

    def test_compaction_shows_one_row_with_the_summary_in_details(self):
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput())
            view={'id':'t','phase':'running','messages':[
                {'index':0,'role':'assistant','text':'','truncated':False},
                {'index':1,'role':'compaction','text':'SUMMARY: 3 of 6 done','truncated':False}]}
            asyncio.run(p.render_turn(view))
            self.assertIn('• Context compacted · earlier conversation summarized',p.transcript.text)
            self.assertNotIn('SUMMARY',p.transcript.text)
            self.assertIn('SUMMARY: 3 of 6 done',p.details.text)
            self.assertIn('/compact',COMMANDS)

    def test_actions_reaching_outside_the_workspace_are_marked_not_refused(self):
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput())
            text=lambda:''.join(f[1] for f in p.approval_fragments())
            p.pending={'id':'c','status':'awaiting_approval','shell':{'command':'./measure.sh','timeout_seconds':5}}
            self.assertNotIn('outside the workspace',text())
            p.pending={**p.pending,'shell':{'command':'cat ~/notes.md','timeout_seconds':5},'outside':['~/notes.md']}
            self.assertIn('⚠ Reaches outside the workspace: ~/notes.md',text())
            self.assertEqual(p.approval_options()[0][2],'allow')
            p.operation_result({'id':'o','status':'succeeded','outcome':{'exit_code':0},'outside':['/tmp/x'],
                                'shell':{'command':'cat /tmp/x','timeout_seconds':5}})
            self.assertIn('• Ran cat /tmp/x · outside workspace',p.transcript.text)
            self.assertIn('Outside the workspace · /tmp/x',p.details.text)

    def test_tool_failure_uncertainty_and_success_warnings_stay_visible(self):
        for status,outcome in [('failed',{'exit_code':1}),('unknown',{'reason':'outcome unknown'}),
                               ('denied',{}),('canceled',{}),('succeeded',{'exit_code':2}),
                               ('succeeded',{'gateway_lost':True}),('succeeded',{'error':'write failed'})]:
            with self.subTest(status=status,outcome=outcome),create_pipe_input() as input:
                preview=Preview(None,'fixture',input=input,output=DummyOutput())
                preview.operation_result({'id':'important','status':status,'outcome':outcome})
                self.assertTrue(preview.transcript.text)
                self.assertIn('important',preview.details.text)

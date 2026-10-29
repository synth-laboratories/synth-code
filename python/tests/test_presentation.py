"""Adversarial model output must remain inert, bounded and renderable."""
import asyncio
import time
import unittest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from synth_code.app import COMMANDS, Preview
from synth_code.presentation import markdown

def plain(lines):
    return '\n'.join(''.join(value for _, value in line) for line in lines)

class PresentationTests(unittest.TestCase):
    def test_malformed_table_is_literal_and_code_keeps_its_characters(self):
        self.assertEqual(plain(markdown('|---|\n|---|')), '|---|\n|---|')
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
        self.assertEqual(plain(markdown('['*99000)), '['*99000)
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
        from synth_code.presentation import markdown
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

    def test_approval_panel_asks_codex_style_with_numbered_choices(self):
        with create_pipe_input() as input:
            p=Preview(None,'fixture',input=input,output=DummyOutput())
            text=lambda:''.join(f[1] for f in p.approval_fragments())
            p.permissions='manual'
            p.pending={'id':'e','status':'awaiting_approval','edit':{'path':'policy.py','before':'a\n','content':'b\n'}}
            self.assertIn('Would you like to make the following edits?',text())
            self.assertIn('policy.py (+1 −1)',text())
            self.assertIn('› 1. Yes, proceed (y)',text())
            self.assertIn("2. Yes, and don't ask again for edits this session (a)",text())
            self.assertIn('3. No, and tell Synth Code what to do differently (esc)',text())
            p.permissions='auto-edit'
            p.pending={'id':'c','status':'awaiting_approval','shell':{'command':'./measure.sh','timeout_seconds':5}}
            self.assertIn('$ ./measure.sh',text())
            self.assertIn("(yolo) (a)",text())
            p.permissions='yolo'
            self.assertEqual([o[2] for o in p.approval_options()],['allow','deny'])
            p.pending={**p.pending,'status':'ready'}
            self.assertEqual([o[0] for o in p.approval_options()],['Yes, run it now','No, cancel it'])

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

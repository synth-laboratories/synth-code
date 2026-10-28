"""Adversarial model output must remain inert, bounded and renderable."""
import time
import unittest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from synth_code.app import Preview
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

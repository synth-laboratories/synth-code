import unittest
import asyncio
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.data_structures import Size
from synth_code.app import Preview
from datetime import datetime, timezone
from synth_code.activity import elapsed, summary, activity_line, usage

class ActivityTests(unittest.TestCase):
    def test_duration_and_completion_are_explicit_and_defensive(self):
        for ms,expected in [(0,'0s'),(67000,'1m 7s'),(3600000,'1h'),(4267000,'1h 11m 7s')]:
            self.assertEqual(elapsed(ms),expected)
        for unknown in [None,True,-1,'52',float('nan'),float('inf')]:
            self.assertIsNone(elapsed(unknown))
        self.assertIsNone(summary({'phase':'running'}))
        self.assertIn('duration unavailable',summary({'phase':'interrupted','activity':{'completedAt':None,'durationMs':None}}))
        self.assertIn('Worked for 52s',summary({'phase':'completed','activity':{'durationMs':52000,'completedAt':'2026-10-28T17:05:00Z'}}))
        self.assertNotIn('Worked',summary({'phase':'failed','activity':{'durationMs':52000}}))

    def test_live_labels_use_observed_phase_and_command_limits(self):
        now=datetime(2026,10,28,17,5,tzinfo=timezone.utc)
        view={'phase':'running','modelPending':True,'activity':{'durationMs':52000,'updatedAt':'2026-10-28T17:04:50Z'}}
        self.assertEqual(activity_line(view,now=now),'Thinking · 52s · last event 10s ago')
        self.assertTrue(activity_line(dict(view,liveText='Hi'),now=now).startswith('Responding'))
        future=dict(view,activity={'durationMs':None,'updatedAt':'2026-10-28T17:05:10Z'})
        self.assertIn('last event time unavailable',activity_line(future,now=now))
        op={'status':'dispatched','shell':{'command':'sleep 20\nprintf done','timeout_seconds':30}}
        line=activity_line(view,op,{'durationMs':5000},now=now)
        self.assertIn('Running command · 5s · timeout 30s · output buffered',line)
        self.assertEqual(activity_line(view,dict(op,status='succeeded'),{'durationMs':5000}),'Command completed · 5s')
        self.assertEqual(activity_line({'phase':'completed','activity':{'durationMs':5000}},dict(op,status='awaiting_approval')),'Completed · 5s')
        self.assertNotIn('last output',line)
        self.assertNotIn('\n',line)
        self.assertIn('Awaiting approval',activity_line(view,dict(op,status='awaiting_approval')))

    def test_tokens_do_not_invent_missing_usage(self):
        self.assertEqual(usage({'usage':[None]}),(0,0,False,True))
        self.assertEqual(usage({'usage':[{'prompt_tokens':4,'completion_tokens':2},None]}),(4,2,True,True))
        self.assertEqual(usage({'usage':[{'prompt_tokens':True,'completion_tokens':-1}]}),(0,0,False,True))

    def test_panel_owns_current_command_and_toggle_resize_preserve_status(self):
        class Output(DummyOutput):
            columns = 160
            def get_size(self): return Size(rows=40, columns=self.columns)
        output=Output()
        op={'id':'current-command','status':'dispatched','shell':{
            'command':'printf unique-command-preview','timeout_seconds':30}}
        view={'phase':'running'}
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=output)
            preview.show_running_operation(op)
            self.assertIn('unique-command-preview',preview.details.text)
            self.assertTrue(preview.details_visible())
            preview.status.project(view,op,{'durationMs':5000},show_preview=not preview.details_visible())
            self.assertEqual(preview.status.text,'Running command · 5s · timeout 30s · output buffered')
            preview.toggle_details()
            self.assertIn(op['shell']['command'],preview.status.text)
            preview.toggle_details()
            self.assertNotIn('unique-command-preview',preview.status.text)
            output.columns=80
            preview.resize_layout(preview.application)
            self.assertIn(op['shell']['command'],preview.status.text)
            output.columns=40
            preview.resize_layout(preview.application)
            self.assertIn('printf',preview.status.text[:40])
            output.columns=160
            preview.resize_layout(preview.application)
            self.assertNotIn('unique-command-preview',preview.status.text)
            for state in ['Outcome unknown','Disconnected','Canceled']:
                preview.status.text=state
                preview.toggle_details()
                preview.resize_layout(preview.application)
                self.assertEqual(preview.status.text,state)
            self.assertIsNone(preview.pending)
            self.assertTrue(preview.application.layout.has_focus(preview.composer))
            asyncio.run(preview.local_command('/clear',''))
            self.assertEqual(preview.details.text,'')
            preview.show_running_operation(op)
            self.assertIn('unique-command-preview',preview.details.text)

    def test_running_edit_title_escapes_untrusted_path_controls(self):
        with create_pipe_input() as input:
            preview=Preview(None,'fixture',input=input,output=DummyOutput())
            preview.show_running_operation({'id':'edit','status':'ready','edit':{
                'path':'owned-\u202ereversed.txt','before':'before','content':'after'}})
            self.assertNotIn('\u202e',preview.detail_title.text)
            self.assertIn('\\u202e',preview.detail_title.text)

    def test_partial_container_inventory_is_visible_even_without_matches(self):
        class Client:
            rows=[]
            async def call(self, method, params):
                return {'context':'fixture','containers':self.rows,'truncated':False,
                        'partial':True,'imageMetadataError':'unavailable'}
        client=Client()
        with create_pipe_input() as input:
            preview=Preview(client,'fixture',input=input,output=DummyOutput())
            for rows in [[],[{'id':'123','name':'owned-fixture','state':'created','image':'opaque-id','imageTags':[]}]]:
                client.rows=rows
                asyncio.run(preview.local_command('/containers','query'))
                self.assertIn('Partial inventory · image tags unavailable',preview.transcript.text)
                self.assertNotIn('No matching containers',preview.transcript.text)

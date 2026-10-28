import unittest
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

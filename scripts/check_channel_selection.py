#!/usr/bin/env python3
"""Verify stable/prerelease selection from retained, unpublished wheel fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--stable',required=True,type=Path)
parser.add_argument('--nightly',required=True,type=Path)
args=parser.parse_args()
run=ROOT/'artifacts/channel-selection'/str(uuid4());links=run/'wheels';links.mkdir(parents=True)
for wheel in [args.stable,args.nightly]:shutil.copyfile(wheel,links/wheel.name)
environment=os.environ|{'UV_CACHE_DIR':str(ROOT/'.cache/uv')}
receipt={'scope':'Local resolver selection only; no public registry or release publication','wheels':{w.name:hashlib.sha256(w.read_bytes()).hexdigest() for w in [args.stable,args.nightly]},'selections':[]}
for label,requirement,expected in [('stable','synth-code','0.0.1'),('nightly','synth-code==0.0.2.dev0','0.0.2.dev0')]:
    venv=run/label
    subprocess.run(['uv','venv','--python','3.12',str(venv)],env=environment,check=True,capture_output=True)
    python=venv/'bin/python'
    subprocess.run(['uv','pip','install','--python',str(python),'--no-deps','--no-index','--find-links',str(links),requirement],env=environment,check=True,capture_output=True)
    selected=subprocess.check_output([str(python),'-c',"from importlib.metadata import version; print(version('synth-code'))"],text=True).strip()
    assert selected==expected,(label,selected)
    display=subprocess.check_output([str(venv/'bin/synth-code'),'--version'],env=environment,text=True).strip()
    assert expected in display and label in display,display
    receipt['selections'].append({'requirement':requirement,'version':selected,'display':display})
receipt.update(status='passed',cleanup_complete=True,provider_requests=0)
(run/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(run)

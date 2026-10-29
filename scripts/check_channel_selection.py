#!/usr/bin/env python3
"""Verify stable/prerelease selection from retained, unpublished wheel fixtures."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile
from email.parser import BytesParser
from uuid import uuid4
from verify_publish import request

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--stable',required=True,type=Path)
parser.add_argument('--nightly',required=True,type=Path)
args=parser.parse_args()
run=ROOT/'artifacts/channel-selection'/str(uuid4());links=run/'wheels';links.mkdir(parents=True)
for wheel in [args.stable,args.nightly]:shutil.copyfile(wheel,links/wheel.name)
environment=os.environ|{'UV_CACHE_DIR':str(ROOT/'.cache/uv')}
receipt={'scope':'Local resolver selection only; no public registry or release publication','wheels':{w.name:hashlib.sha256(w.read_bytes()).hexdigest() for w in [args.stable,args.nightly]},'selections':[]}
def version(wheel,channel):
    with zipfile.ZipFile(wheel) as archive:
        names=[name for name in archive.namelist() if name.endswith('.dist-info/METADATA')]
        if len(names)!=1:raise ValueError('expected one wheel metadata file')
        metadata=BytesParser().parsebytes(archive.read(names[0]))
        if metadata['Name']!='synth-code':raise ValueError('unexpected wheel project')
        value=metadata['Version'];request('v'+value,channel,'0'*64)
        return value
stable=version(args.stable,'stable');nightly=version(args.nightly,'nightly')
for label,requirement,expected in [('stable','synth-code',stable),('nightly','synth-code=='+nightly,nightly)]:
    venv=run/label
    subprocess.run(['uv','venv','--python',sys.executable,str(venv)],env=environment,check=True,capture_output=True)
    python=venv/'bin/python'
    subprocess.run(['uv','pip','install','--python',str(python),'--no-deps','--no-index','--find-links',str(links),requirement],env=environment,check=True,capture_output=True)
    selected=subprocess.check_output([str(python),'-c',"from importlib.metadata import version; print(version('synth-code'))"],text=True).strip()
    assert selected==expected,(label,selected)
    display=subprocess.check_output([str(venv/'bin/synth-code'),'--version'],env=environment,text=True).strip()
    assert expected in display and label in display,display
    receipt['selections'].append({'requirement':requirement,'version':selected,'display':display})
receipt.update(status='passed',cleanup_complete=True,provider_requests=0)
(run/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n');print(run)

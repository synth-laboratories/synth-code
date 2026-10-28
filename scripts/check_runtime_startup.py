#!/usr/bin/env python3
"""Measure the native stdio handshake independently of the test runner."""
import json
from pathlib import Path
import queue
import subprocess
import threading
import time
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
run=ROOT/'artifacts/startup'/str(uuid4());run.mkdir(parents=True)
binary=ROOT/'target/debug/synth-code-runtime'
started=time.monotonic()
with (run/'stderr.log').open('w') as stderr:
    process=subprocess.Popen([str(binary),'stdio',str(run/'store')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True)
    results=queue.Queue()
    threading.Thread(target=lambda:results.put(process.stdout.readline()),daemon=True).start()
    try:
        process.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}})+'\n');process.stdin.flush()
        line=results.get(timeout=40)
        elapsed=time.monotonic()-started
        value=json.loads(line);assert value['result']['runtimeSha256']
        receipt={'binary_bytes':binary.stat().st_size,'handshake_seconds':elapsed,'pid':process.pid,'version':value['result']['version']}
        print(json.dumps(receipt),flush=True)
        (run/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
    finally:
        process.stdin.close()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()

#!/usr/bin/env python3
"""Portable public build admission; owner workspace uses its shared launcher."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
GiB=1024**3

def target_bytes(path):
    total=0
    if path.is_symlink():
        raise RuntimeError("explicit target must be an ordinary owned directory")
    for directory,_,files in os.walk(path,followlinks=False):
        for name in files:
            try:total+=(Path(directory)/name).lstat().st_size
            except FileNotFoundError:pass
    return total

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--label",required=True)
    parser.add_argument("--wait-seconds",type=float,default=60)
    parser.add_argument("--light",action="store_true")
    parser.add_argument("command",nargs=argparse.REMAINDER)
    args=parser.parse_args()
    command=args.command[1:] if args.command[:1]==["--"] else args.command
    if not command:parser.error("command required")
    if args.light:
        return subprocess.call(command,cwd=ROOT)
    if Path(command[0]).name!="mbx":parser.error("Rust commands must enter through mbx")
    if os.environ.get("RUSTC_WRAPPER"):
        parser.error("external compiler wrappers are not supported")
    target=ROOT/"target"
    if "--target-dir" in command:
        target=Path(command[command.index("--target-dir")+1]).resolve()
    if not target.is_relative_to(ROOT):parser.error("target must stay in this checkout")
    budget=int(os.environ.get("CORE_BUILD_TARGET_MAX_BYTES",8*GiB))
    target.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(os.environ.get("SYNTH_PUBLIC_ADMISSION_ROOT",str(Path.home()/".cache/synth-code-build-admission")))
    cache.mkdir(mode=0o700,parents=True,exist_ok=True)
    if cache.is_symlink() or cache.stat().st_mode&0o077:
        raise RuntimeError("admission directory must be private")
    locks=[];held=None;deadline=time.monotonic()+args.wait_seconds
    try:
        for index in range(2):
            descriptor=os.open(cache/f"lane-{index}.lock",os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            locks.append(descriptor)
        while held is None:
            for descriptor in locks:
                try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB);held=descriptor;break
                except BlockingIOError:pass
            if held is not None:break
            if time.monotonic()>=deadline:raise RuntimeError("build lanes are busy")
            time.sleep(0.2)
        def storage():
            if shutil.disk_usage(ROOT).free<20*GiB:raise RuntimeError("build disk floor: 20 GiB free required")
            if target_bytes(target)>budget:raise RuntimeError("owned target exceeds storage budget")
        storage()
        environment=os.environ|{"CARGO_TARGET_DIR":str(target),"MBX_TARGET_VIEWS":"0"}
        process=subprocess.Popen(command,cwd=ROOT,env=environment,start_new_session=True)
        def terminate(*_):
            if process.poll() is None:os.killpg(process.pid,signal.SIGTERM)
        previous={s:signal.signal(s,terminate) for s in [signal.SIGTERM,signal.SIGINT]}
        try:
            print(json.dumps({"schema":"synth-code.build-admission.v1","label":args.label,"pid":process.pid,"target":str(target),"max_bytes":budget}),flush=True)
            while process.poll() is None:
                try:storage()
                except BaseException:
                    terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
                    raise
                time.sleep(1)
            return process.returncode
        finally:
            for signum,handler in previous.items():signal.signal(signum,handler)
    finally:
        for descriptor in locks:os.close(descriptor)

if __name__=="__main__":raise SystemExit(main())

#!/usr/bin/env python3
"""Use the workspace's maintained build admission and mbx, with owned targets."""
import argparse
import hashlib
import json
import tomllib
from kernel_inputs import snapshot
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["build", "test", "clippy"])
parser.add_argument("--release", action="store_true")
# Test hooks (crash injection, local scripted providers, standalone proposals) are on
# for debug builds and off for release builds unless a qualification lane asks for them.
parser.add_argument("--test-hooks", action=argparse.BooleanOptionalAction, default=None)
args = parser.parse_args()
launcher = Path(os.environ.get("SYNTH_BUILD_LAUNCHER", str(ROOT.parent / "synth-dev/local_dev/scripts/core_dev_build.py")))
mbx = shutil.which("mbx") or str(Path.home() / ".cargo/bin/mbx")
if not launcher.is_file() and "SYNTH_BUILD_LAUNCHER" not in os.environ:
    launcher=ROOT/"scripts/public_build_guard.py"
if not launcher.is_file() or not Path(mbx).is_file():
    parser.error("maintained build launcher and mbx required; set SYNTH_BUILD_LAUNCHER")
command = ["python3", str(launcher), "--label", "synth-code-" + args.command, "--wait-seconds", "60", "--", mbx,
           args.command, "--locked", "--manifest-path", str(ROOT / "Cargo.toml"), "--target-dir", str(ROOT / "target"),
           "-p", "synth-code-runtime"]
if args.release:
    command += ["--release"]
if args.release is False and args.test_hooks is not False or args.test_hooks:
    command += ["--features", "test-hooks"]
if args.command == "clippy":
    command += ["--all-targets", "--", "-D", "warnings"]
inputs=snapshot()
result=subprocess.call(command,cwd=ROOT)
if result==0 and args.command in {"build","test"}:
    if snapshot()!=inputs:
        raise RuntimeError("Rust inputs changed during build; no source receipt written")
    binary=ROOT/"target"/("release" if args.release else "debug")/"synth-code-runtime"
    if binary.is_file():
        version=tomllib.loads((ROOT/"Cargo.toml").read_text())["workspace"]["package"]["version"]
        receipt={"schema":"synth-code.native-build.v1","version":version,"kernel_inputs":inputs,"binary_sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),"command":command}
        Path(str(binary)+".build.json").write_text(json.dumps(receipt,indent=2)+'\n')
raise SystemExit(result)

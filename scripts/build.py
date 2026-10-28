#!/usr/bin/env python3
"""Use the workspace's maintained build admission and mbx, with owned targets."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("command", choices=["build", "test", "clippy"])
parser.add_argument("--release", action="store_true")
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
if args.command == "clippy":
    command += ["--all-targets", "--", "-D", "warnings"]
raise SystemExit(subprocess.call(command, cwd=ROOT))

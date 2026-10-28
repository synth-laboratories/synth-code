#!/usr/bin/env python3
"""Explicit stable/nightly version mapping; never updates or publishes a channel."""
import argparse
from pathlib import Path
import re
import tomllib

ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--channel",required=True,choices=["stable","nightly"])
    parser.add_argument("--version")
    args=parser.parse_args()
    py=ROOT/"python/pyproject.toml";cargo=ROOT/"Cargo.toml"
    current=tomllib.loads(py.read_text())["project"]["version"]
    version=args.version or current
    match=re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:\.dev(0|[1-9]\d*))?",version)
    if not match or (args.channel=="nightly")!=(match[4] is not None):
        parser.error("stable requires X.Y.Z; nightly requires X.Y.Z.devN")
    native=version.replace(".dev","-dev.")
    old_native=tomllib.loads(cargo.read_text())["workspace"]["package"]["version"]
    if not args.version:
        if native!=old_native:parser.error("Python/Rust versions disagree")
        print(version,native,args.channel)
        return
    updates={}
    updates[py]=py.read_text().replace(f'version = "{current}"',f'version = "{version}"',1)
    updates[cargo]=cargo.read_text().replace(f'version = "{old_native}"',f'version = "{native}"',1)
    for path,name,old,new in [(ROOT/"Cargo.lock","synth-code-runtime",old_native,native),(ROOT/"python/uv.lock","synth-code",current,version)]:
        original=path.read_text()
        pattern=r'(\[\[package\]\]\nname = "'+re.escape(name)+r'"\nversion = ")'+re.escape(old)+r'(")'
        text,count=re.subn(pattern,lambda m:m[1]+new+m[2],original)
        if count!=1:raise RuntimeError(f"ambiguous lockfile package: {name}")
        updates[path]=text
    for path,text in updates.items():path.write_text(text)
    print(version,native,args.channel)
if __name__=="__main__":main()

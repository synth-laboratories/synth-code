#!/usr/bin/env python3
"""Download pinned official tooling into this checkout and verify release digests."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("tool",choices=["mbx","bombadil","uv","python"])
    args=parser.parse_args()
    record=json.loads((ROOT/"tools/pins.json").read_text())[args.tool]
    host=platform.system()+"-"+platform.machine()
    pin=record["platforms"].get(host)
    if pin is None:parser.error("unqualified tool platform")
    data=urllib.request.urlopen(pin["url"],timeout=60).read(32*1024*1024+1)
    if hashlib.sha256(data).hexdigest()!=pin["sha256"]:raise RuntimeError("tool digest mismatch")
    if pin.get("extract_tree"):
        target=ROOT/"artifacts/tools"/(args.tool+'-'+record["version"])
        target.mkdir(parents=True,exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data),mode="r:gz") as archive:
            archive.extractall(target,filter="data")
        print(target/"python/bin/python3")
        return
    target=ROOT/"artifacts/tools"/(args.tool+'-'+record["version"])/pin["member"]
    target.parent.mkdir(parents=True,exist_ok=True)
    if pin["archive"]:
        archive=tarfile.open(fileobj=io.BytesIO(data),mode="r:gz")
        members=[m for m in archive.getmembers() if Path(m.name).name==pin["member"] and m.isfile()]
        if len(members)!=1:raise RuntimeError("ambiguous tool archive")
        data=archive.extractfile(members[0]).read()
    target.write_bytes(data)
    target.chmod(0o755)
    print(target)
if __name__=="__main__":main()

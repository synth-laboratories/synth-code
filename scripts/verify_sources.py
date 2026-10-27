#!/usr/bin/env python3
"""Verify vendored public upstream bytes and retained license/provenance files."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
vendor = ROOT / "vendor/temper"
source = json.loads((vendor / "SOURCE.json").read_text())
assert source["revision"] == "118bb6f62354301cd76e2cd7c54ce0c97058a738"
assert source["repository"] == "https://github.com/nerdsane/temper"
for item in source["files"]:
    relative = item["path"]
    path = vendor / ("UPSTREAM-Cargo.toml" if relative == "Cargo.toml" else relative)
    assert path.is_file() and not path.is_symlink(), path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"], path
assert hashlib.sha256((vendor / "Cargo.toml").read_bytes()).hexdigest() == source["adapted_manifest_sha256"]
assert (vendor / "LICENSE-MIT").is_file() and (vendor / "LICENSE-APACHE").is_file()
print(f"Verified {len(source['files'])} pinned upstream files and reduced workspace manifest")

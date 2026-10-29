#!/usr/bin/env python3
"""Prove a release runtime carries no test hooks: no crash injection, no local
scripted provider, no standalone proposals and no fixture executor."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Strings that only exist when test hooks or the removed fixture paths are compiled in.
FORBIDDEN = [b"--test-crash-after-effect", b"--test-crash-before-dispatch", b"local-test-provider",
             b"workspace/proposeEdit", b"workspace/proposeCommand", b"synth/operation/run",
             b"fixture/admit", b"local-fixture", b"fixture_executor"]


def refused(binary, store, *flags):
    result = subprocess.run([str(binary), "stdio", str(store), *flags], input=b"", capture_output=True, timeout=30)
    return result.returncode != 0, result.stderr.decode(errors="replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path, nargs="?", default=ROOT / "target/release/synth-code-runtime")
    binary = parser.parse_args().binary
    data = binary.read_bytes()
    found = [marker.decode() for marker in FORBIDDEN if marker in data]
    assert not found, f"release runtime contains test-only paths: {found}"
    with tempfile.TemporaryDirectory(dir=ROOT / "artifacts") as name:
        root = Path(name)
        for flag in ("--test-crash-after-effect", "--test-crash-before-dispatch"):
            store = root / flag.strip("-")
            store.mkdir(mode=0o700)
            ok, stderr = refused(binary, store, flag)
            assert ok and "unknown option" in stderr, (flag, stderr)
        config = root / "provider.json"
        config.write_text(json.dumps({"endpoint": "http://127.0.0.1:9/chat/completions", "model": "Beam-1.0-501B-A23B",
                                      "effort": "medium", "env_file": None, "max_output_tokens": 1000, "max_requests": None}))
        store = root / "local-provider"
        store.mkdir(mode=0o700)
        ok, stderr = refused(binary, store, "--provider-config", str(config))
        assert ok and "unsupported_provider_endpoint" in stderr, stderr
    print(f"Release runtime has no test hooks: {binary}")


if __name__ == "__main__":
    main()

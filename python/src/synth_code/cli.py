"""Installed entry point, native asset verification and account commands."""
import hashlib
import json
import os
from pathlib import Path
import sys

VERSION="0.0.1"

def native_runtime():
    root=Path(__file__).parent / "native"
    manifest=json.loads((root/"manifest.json").read_text())
    binary=root/"synth-code-runtime"
    if manifest["version"] != VERSION or hashlib.sha256(binary.read_bytes()).hexdigest()!=manifest["sha256"]:
        raise RuntimeError("Installed runtime integrity check failed")
    return binary

def main():
    try:
        return run()
    except (RuntimeError,ValueError,OSError) as error:
        print(str(error),file=sys.stderr)
        return 1

def run():
    arguments=sys.argv[1:]
    if arguments == ["--version"]:
        print(f"Synth Code {VERSION} · stable candidate")
        return 0
    if arguments and arguments[0] in {"login","logout","auth","account"}:
        from .auth import main as account
        return account(arguments)
    from .app import main as application
    if "--runtime" not in arguments and "--help" not in arguments and "-h" not in arguments:
        arguments += ["--runtime",str(native_runtime())]
    if "--store" not in arguments:
        state_root=Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))
        arguments += ["--store",str(state_root/"synth-code"/"v1")]
    sys.argv=["synth-code",*arguments]
    application()
    return 0

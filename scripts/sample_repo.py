"""A small pinned real repository for tests and journeys: tomli 2.2.1 (MIT,
https://github.com/hukkin/tomli), vendored as a one-commit git bundle so runs
need no network. Test tooling only."""
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tests/repos/tomli-2.2.1.bundle"
COMMIT = "73c3d102eb81fe0d2b87f905df4f740f8878d8da"
# The file scripted agents edit, and the project's own test suite.
TARGET = "src/tomli/__init__.py"
MARKER = "# Checked by Synth Code.\n"
CHECK = "PYTHONPATH=src " + shlex.join([sys.executable, "-m", "unittest", "-q"])


def materialize(destination):
    """Clone the pinned commit into `destination` (which must not exist yet)."""
    destination = Path(destination)
    subprocess.run(["git", "-c", "advice.detachedHead=false", "clone", "-q", "--no-hardlinks", str(BUNDLE), str(destination)], check=True)
    head = subprocess.run(["git", "-C", str(destination), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()
    assert head == COMMIT, head
    return destination


def changed(workspace):
    """Paths that differ from the pinned commit, including untracked files."""
    out = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=all"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(line[3:] for line in out.splitlines())


def edited(text):
    return text + MARKER

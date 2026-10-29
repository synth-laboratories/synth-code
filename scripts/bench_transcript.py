"""Time transcript updates in a long session (SYN-5238 before/after measure).

Builds a session of agent replies and finished actions, then times one more
reply and a resize, which re-renders at a new width.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python/src"))
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from synth_code.app import Preview

REPLY = ("Checked **the parser**: `parse_float` rejects `1e5`.\n\n1. Read `src/tomli/_re.py`\n2. Ran the suite\n\n"
         "```python\ndef f(x):\n    return x + 1\n```\n\n| file | result |\n|---|---|\n| _re.py | ok |\n")


def session(preview, turns):
    for turn in range(turns):
        preview.append(f"Task {turn}", "user")
        preview.operation_result({"id": f"c{turn}", "status": "succeeded", "shell": {"command": "python -m unittest -q"},
                                  "outcome": {"exit_code": 0, "stdout": "\n".join(f"line {i}" for i in range(40))}})
        preview.operation_result({"id": f"e{turn}", "status": "succeeded", "outcome": {},
                                  "edit": {"path": "src/tomli/_re.py", "before": "a\nb\n" * 20, "content": "a\nc\n" * 20}})
        preview.append(REPLY, "assistant")


def main():
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    with create_pipe_input() as pipe:
        preview = Preview(None, "bench", input=pipe, output=DummyOutput())
        start = time.perf_counter()
        session(preview, turns)
        build = time.perf_counter() - start
        appends = []
        for _ in range(20):
            start = time.perf_counter()
            preview.append(REPLY, "assistant")
            appends.append(time.perf_counter() - start)
        start = time.perf_counter()
        preview.refresh_transcript()
        refresh = time.perf_counter() - start
        # A resize renders every retained item again at the new width.
        preview.application.output.get_size = lambda: Size(rows=40, columns=120)
        start = time.perf_counter()
        preview.refresh_transcript()
        resize = time.perf_counter() - start
    print(f"items={len(preview.entries)} chars={len(preview.transcript.text)} build={build:.2f}s "
          f"append_median={statistics.median(appends)*1000:.1f}ms append_max={max(appends)*1000:.1f}ms "
          f"refresh={refresh*1000:.1f}ms resize={resize*1000:.1f}ms")


if __name__ == "__main__":
    main()

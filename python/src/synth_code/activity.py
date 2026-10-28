"""Terminal activity labels from observed public runtime state."""
from datetime import datetime, timezone
import math


def elapsed(milliseconds):
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or not math.isfinite(milliseconds) or milliseconds < 0:
        return None
    seconds = int(milliseconds // 1000)
    parts = []
    for size, unit in ((3600, "h"), (60, "m"), (1, "s")):
        value, seconds = divmod(seconds, size)
        if value or (unit == "s" and not parts):
            parts.append(f"{value}{unit}")
    return " ".join(parts)


def stamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def summary(view):
    phase = view.get("phase")
    if phase in {"running", "waiting"}:
        return None
    timing = view.get("activity") or {}
    duration = elapsed(timing.get("durationMs"))
    labels = {"completed": "Worked for", "failed": "Failed after", "interrupted": "Interrupted after"}
    if phase not in labels:
        return None
    text = labels[phase] + " " + duration if duration else phase.title() + " · duration unavailable"
    finished = stamp(timing.get("completedAt"))
    if finished:
        text += " · " + finished.astimezone().strftime("%I:%M %p").lstrip("0")
    return text


def activity_line(view, operation=None, operation_activity=None, now=None, *, show_preview=True, compact=False):
    phase = view.get("phase")
    timing = view.get("activity") or {}
    if phase not in {"running", "waiting"}:
        duration = elapsed(timing.get("durationMs"))
        return str(phase or "Ready").title() + (" · " + duration if duration else "")
    if operation and operation.get("status") not in {"awaiting_approval", "ready", "dispatched"}:
        state = operation.get("status")
        label = {"succeeded": "Command completed" if operation.get("shell") else "Edit applied",
                 "failed": "Action failed", "unknown": "Outcome unknown", "denied": "Denied",
                 "canceled": "Canceled", "reconciled": "Reconciled"}.get(state, str(state or "Working"))
        duration = elapsed((operation_activity or {}).get("durationMs"))
        return label + (" · " + duration if duration else "")
    if operation and operation.get("status") in {"awaiting_approval", "ready", "dispatched"}:
        status = operation["status"]
        label = {"awaiting_approval": "Awaiting approval", "ready": "Approved · awaiting execution",
                 "dispatched": "Running command" if operation.get("shell") else "Applying edit"}[status]
        parts = ["Running" if compact and status == "dispatched" and operation.get("shell") else label]
        duration = elapsed((operation_activity or {}).get("durationMs"))
        if duration:
            parts.append(duration)
        if operation.get("shell"):
            parts += [f"timeout {operation['shell']['timeout_seconds']}s"]
            if status == "dispatched":
                parts.append("output buffered")
            if show_preview:
                preview = " ".join(operation["shell"]["command"].split())
                parts.insert(1 + bool(duration), preview) if compact else parts.append(preview)
        elif operation.get("edit") and show_preview:
            parts.append(operation["edit"]["path"])
        return " · ".join(parts)
    parts = ["Responding" if view.get("liveText") else "Thinking" if view.get("modelPending") else "Working"]
    duration = elapsed(timing.get("durationMs"))
    if duration:
        parts.append(duration)
    updated = stamp(timing.get("updatedAt"))
    if updated:
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
        parts.append("last event " + (elapsed(age * 1000) or "0s") + " ago" if age >= 0 else "last event time unavailable")
    return " · ".join(parts)


def usage(view):
    """Retain only reported nonnegative counters; missing rows remain partial."""
    rows = view.get("usage") or []
    totals = [0, 0]
    known = False
    partial = False
    for row in rows:
        for index, key in enumerate(("prompt_tokens", "completion_tokens")):
            value = row.get(key) if isinstance(row, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[index] += value
                known = True
            else:
                partial = True
    return (*totals, known, partial)

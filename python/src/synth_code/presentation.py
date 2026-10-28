"""Small, inert Markdown presentation for terminal conversation text."""
import re
from prompt_toolkit.utils import get_cwidth

INLINE = re.compile(r"(\*\*[^*\n]{1,2048}\*\*|`[^`\n]{1,2048}`|\[[^\[\]\n]{1,512}\]\([^()\s]{1,2048}\)|(?<!\*)\*[^*\n]{1,2048}\*(?!\*))")


def inline(text):
    result = []
    for part in INLINE.split(text):
        if part.startswith("**") and part.endswith("**"):
            result.append(("class:strong", part[2:-2]))
        elif part.startswith("`") and part.endswith("`"):
            result.append(("class:inline-code", part[1:-1]))
        elif part.startswith("[") and "](" in part and part.endswith(")"):
            label, target = part[1:-1].split("](", 1)
            result.append(("class:link", label + " (" + target + ")"))
        elif part.startswith("*") and part.endswith("*"):
            result.append(("class:emphasis", part[1:-1]))
        elif part:
            result.append(("", part))
    return result


def markdown(text, *, width=100, max_chars=100_000):
    """Return styled lines whose text is also the exact terminal document.

    Supports common headings, emphasis, code and pipe tables. Content is never
    evaluated, interpreted as terminal control, or opened as a link.
    """
    lines = text.splitlines()
    result = []
    marker = "… [display truncated]"
    remaining = max(0, min(max_chars, 100_000) - len(marker) - 1)
    truncated = False
    def emit(fragments):
        nonlocal remaining, truncated
        if remaining <= 0:
            truncated = True
            return
        if result:
            remaining -= 1
        row = []
        for style, value in fragments:
            if len(value) > remaining:
                row.append((style, value[:max(0, remaining)]))
                remaining = 0
                truncated = True
                break
            row.append((style, value))
            remaining -= len(value)
        result.append(row)
    code = False
    index = 0
    while index < len(lines) and remaining > 0:
        line = lines[index]
        if line.lstrip().startswith("```"):
            code = not code
            if code:
                language = line.lstrip()[3:].strip()
                if language:
                    emit([("class:muted", language)])
            index += 1
            continue
        if code:
            emit([("class:code", "  " + line)])
        elif line.strip().startswith("|") and index + 1 < len(lines) and re.fullmatch(r"[\s|:\-]+", lines[index + 1]):
            rows = []
            first = index
            while index < len(lines) and lines[index].strip().startswith("|"):
                current = lines[index]
                if not re.fullmatch(r"[\s|:\-]+", current):
                    rows.append([inline(cell.strip()) for cell in current.strip().strip("|").split("|")])
                index += 1
            count = max(map(len, rows), default=0)
            if not count or count > 16 or len(rows) * count > 10_000:
                for literal in lines[first:index]:
                    if remaining <= 0:
                        truncated = True
                        break
                    emit([("", literal)])
                continue
            widths = [min(max(1, min(48, min(width, 400) // count - 3)), max((get_cwidth("".join(t for _, t in row[col])) if col < len(row) else 0) for row in rows)) for col in range(count)]
            for row_index, row in enumerate(rows):
                if remaining <= 0:
                    truncated = True
                    break
                fragments = []
                for col in range(count):
                    cell = row[col] if col < len(row) else []
                    plain = "".join(t for _, t in cell)
                    fragments.extend([("class:table-header", plain)] if row_index == 0 else cell)
                    if col < count - 1:
                        fragments.append(("", " " * max(3, widths[col] - get_cwidth(plain) + 3)))
                emit(fragments)
                if row_index == 0:
                    emit([("class:separator", "─" * (sum(widths) + 3 * (count - 1)))])
            continue
        elif re.match(r"^#{1,6} ", line):
            emit([("class:heading", re.sub(r"^#{1,6} ", "", line))])
        else:
            if line.startswith("- "):
                line = "• " + line[2:]
            emit(inline(line))
        index += 1
    if truncated or index < len(lines):
        result.append([("class:muted", marker[:max_chars])])
    return result or [[]]

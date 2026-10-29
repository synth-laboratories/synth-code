"""Small, inert Markdown presentation for terminal conversation text."""
import re
import textwrap
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


def column_widths(rows, count, width):
    """Natural column widths, shrinking the widest column until the table fits."""
    natural = [min(400, max([get_cwidth("".join(t for _, t in row[col])) if col < len(row) else 0 for row in rows] + [1]))
               for col in range(count)]
    # Prefer not to split words (headers especially) unless nothing else fits.
    words = [min(20, max([get_cwidth(word) for row in rows if col < len(row)
                          for word in "".join(t for _, t in row[col]).split()] + [1])) for col in range(count)]
    available = max(count * 4, width - 3 * (count - 1))
    widths = list(natural)
    for floor in (words, [4] * count):
        while sum(widths) > available:
            candidates = [col for col in range(count) if widths[col] > floor[col]]
            if not candidates:
                break
            widest = max(candidates, key=widths.__getitem__)
            widths[widest] -= 1
    return widths


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
            widths = column_widths(rows, count, min(width, 400))
            for row_index, row in enumerate(rows):
                if remaining <= 0:
                    truncated = True
                    break
                cells = [row[col] if col < len(row) else [] for col in range(count)]
                plains = ["".join(t for _, t in cell) for cell in cells]
                wrapped = [textwrap.wrap(plain, widths[col], break_long_words=True, break_on_hyphens=False) or [""]
                           for col, plain in enumerate(plains)]
                for line_index in range(max(map(len, wrapped))):
                    fragments = []
                    for col in range(count):
                        piece = wrapped[col][line_index] if line_index < len(wrapped[col]) else ""
                        if row_index == 0:
                            fragments.append(("class:table-header", piece))
                        elif len(wrapped[col]) == 1:
                            if line_index == 0:
                                fragments.extend(cells[col])  # keeps inline styling when the cell fits
                        else:
                            fragments.append(("", piece))
                        if col < count - 1:
                            fragments.append(("", " " * (widths[col] - get_cwidth(piece) + 3)))
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

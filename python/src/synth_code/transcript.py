"""Typed transcript items rendered once per width into prompt_toolkit fragments.

Markdown rendering adapts Reflection Mirror's transcript presentation
(reflection-oss/mirror f9380337, mirror-cli/src/mirror/cli/rendering.py,
Apache-2.0): left-aligned headings, list numbers with their period, and code
blocks on a shared background, rendered by Rich and converted to fragments.
"""
import io
import re
from dataclasses import dataclass, field

from rich import markdown as rich_markdown
from rich.console import Console
from rich.segment import Segment
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

# Gutter glyphs, shared by every item so text lines up (Mirror's GUTTER_WIDTH).
USER_ICON, AGENT_ICON = "› ", "● "
GUTTER = "  "
# Expanded cards show at most this many detail lines; the rest is a note.
CARD_LINES = 400
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
THEME = Theme({
    "markdown.code": "#e6c07b", "markdown.item.bullet": "#979baa", "markdown.item.number": "#979baa",
    "markdown.link": "#7fb4ff underline", "markdown.link_url": "#7fb4ff", "markdown.block_quote": "#b8bcc8 italic",
    "markdown.h1": "bold #eeece8", "markdown.h2": "bold #eeece8", "markdown.h3": "bold #d7d9df",
    "markdown.hr": "#4b505c", "markdown.table.border": "#4b505c", "markdown.table.header": "bold",
    "quote.rail": "#6f7380",
})
CODE_BACKGROUND = "#1c1e24"


@dataclass(eq=False)
class Item:
    """One transcript entry. `kind` is user, agent, action, status or notice."""
    kind: str
    text: str
    detail: str | None = None
    preview: tuple | None = None
    # Failed and unknown actions are cards that open expanded.
    state: str = "ok"
    expanded: bool = False
    cache: dict = field(default_factory=dict)
    # Width-independent Markdown parse of an agent reply, made on first render.
    parsed: object = None

    @property
    def size(self):
        return len(self.text) + len(self.detail or "") + 2


class _Heading(rich_markdown.Heading):
    """Left-aligned bold headings without Rich's centred box."""
    def __rich_console__(self, console, options):
        self.text.justify = "left"
        self.text.stylize(console.get_style(f"markdown.{self.tag}"))
        yield self.text


class _ListItem(rich_markdown.ListItem):
    """Keep the source's period after ordered-list numbers."""
    def render_number(self, console, options, number, last_number):
        width = min(len(str(last_number)) + 3, max(0, options.max_width - 1))
        lines = console.render_lines(self.elements, options.update(width=max(1, options.max_width - width)),
                                     style=self.style)
        for index, line in enumerate(lines):
            label = f"{number}.".rjust(width - 1) + " " if index == 0 else " " * width
            yield Segment(label[:width], console.get_style("markdown.item.number"))
            yield from line
            yield Segment.line()


class _CodeBlock(rich_markdown.CodeBlock):
    """Highlighted code on one background; long lines wrap rather than clip."""
    def __rich_console__(self, console, options):
        yield Syntax(str(self.text).rstrip(), self.lexer_name, theme="monokai", word_wrap=True,
                     background_color=CODE_BACKGROUND, padding=(0, 1) if options.max_width >= 6 else 0)


class _Quote(rich_markdown.BlockQuote):
    """Quoted words beside a rail."""
    def __rich_console__(self, console, options):
        inset = min(2, max(0, options.max_width - 1))
        for line in console.render_lines(self.elements, options.update(width=max(1, options.max_width - inset)),
                                         style=self.style):
            yield Segment("▌ "[:inset], console.get_style("quote.rail"))
            yield from line
            yield Segment.line()


class _Markdown(rich_markdown.Markdown):
    elements = {**rich_markdown.Markdown.elements, "heading_open": _Heading, "list_item_open": _ListItem,
                "fence": _CodeBlock, "code_block": _CodeBlock, "blockquote_open": _Quote}

    def __rich_console__(self, console, options):
        # markdown-it drops input past maxNesting; show such text literally instead.
        if any(token.level >= 19 for token in self.parsed):
            yield Text(self.markup)
            return
        yield from super().__rich_console__(console, options)


def parse_markdown(text, *, max_chars=100_000):
    """Parse once; the result renders at any width."""
    return _Markdown(text[:max_chars], hyperlinks=False, code_theme="monokai")


_STYLES = {}


def _fragment_style(style, color):
    """A prompt_toolkit style string for a Rich style; colours only when enabled."""
    key = (style, color)
    if key not in _STYLES:
        parts = []
        if color and style.color and not style.color.is_default:
            parts.append(style.color.get_truecolor().hex)
        if color and style.bgcolor and not style.bgcolor.is_default:
            parts.append("bg:" + style.bgcolor.get_truecolor().hex)
        parts.extend(name for name in ("bold", "italic", "underline", "reverse", "strike") if getattr(style, name))
        _STYLES[key] = " ".join(parts)
    return _STYLES[key]


def markdown_lines(text, *, width=100, max_chars=100_000, color=True):
    """Render already escaped Markdown (text or a parse) to fragment lines no wider than `width`.

    The output is bounded by `max_chars` whatever padding tables add.
    """
    document = parse_markdown(text, max_chars=max_chars) if isinstance(text, str) else text
    console = Console(file=io.StringIO(), width=max(1, width), height=25, theme=THEME, legacy_windows=False,
                      color_system="truecolor")
    lines, used = [], 0
    for segments in console.render_lines(document, console.options.update(width=max(1, width)), pad=False):
        # Only colours and attributes survive; any other control is dropped.
        line = [(_fragment_style(segment.style, color) if segment.style else "", CONTROL.sub("", segment.text))
                for segment in segments if not segment.control and segment.text]
        # Trailing padding goes, except where a background (a code block) shows it.
        while line and "bg:" not in line[-1][0]:
            style, value = line[-1]
            if value.rstrip():
                line[-1] = (style, value.rstrip())
                break
            line.pop()
        if not line and lines and not lines[-1]:
            continue  # one blank line between blocks
        size = sum(len(value) for _, value in line) + 1
        if used + size > max_chars:
            break
        lines.append(line)
        used += size
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def gutter(lines, icon, style=""):
    """Prefix the first line with an icon and indent the rest to match."""
    return [[(style, icon if index == 0 else GUTTER)] + line for index, line in enumerate(lines)]

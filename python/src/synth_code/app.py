"""Local TUI projecting the durable Rust agent and explicit approvals."""
import argparse
import asyncio
import difflib
import os
import unicodedata
from collections import deque
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.completion import Completion, ConditionalCompleter, WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window, ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.processors import AfterInput
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Button, Dialog, Label, RadioList, TextArea
from prompt_toolkit.output import ColorDepth
from prompt_toolkit.mouse_events import MouseEventType, MouseButton

from .client import Client
from .presentation import markdown
from .activity import activity_line, summary, usage
from .clipboard import ResponseCopy, response_text, write_clipboard

try:
    VERSION = version("synth-code")
except PackageNotFoundError:
    VERSION = "source"

class DuringTurn(Enum):
    """What a slash command does while the agent is working."""
    RUN = "run now"
    QUEUE = "queued until the turn ends"
    REFUSE = "refused"


class SlashCommand(NamedTuple):
    syntax: str
    label: str
    # Required, so a new command cannot be registered without deciding this;
    # test_every_command_follows_its_during_turn_policy checks the behaviour.
    during_turn: DuringTurn
    why: str


RUN, QUEUE, REFUSE = DuringTurn.RUN, DuringTurn.QUEUE, DuringTurn.REFUSE
COMMANDS = {
    "/exit": SlashCommand("", "Detach", RUN, "detaching leaves the turn running in the daemon"),
    "/quit": SlashCommand("", "Detach", RUN, "same as /exit"),
    "/help": SlashCommand("", "Commands", RUN, "read-only"),
    "/clear": SlashCommand("", "Clear view", RUN, "view-only; refused while an approval is pending so it cannot be hidden"),
    "/status": SlashCommand("", "Session status", RUN, "read-only"),
    "/stop": SlashCommand("", "Interrupt task", RUN, "controls the running turn"),
    "/details": SlashCommand("", "Toggle operation pane", RUN, "view-only"),
    "/host": SlashCommand("", "Host tools and workspace", RUN, "read-only"),
    "/containers": SlashCommand("QUERY", "Find containers", RUN, "read-only"),
    "/effort": SlashCommand("LEVEL", "Reasoning effort", QUEUE, "affects the next model request"),
    "/provider": SlashCommand("NAME", "Provider", QUEUE, "affects the next model request"),
    "/model": SlashCommand("NAME", "Model", QUEUE, "affects the next model request"),
    "/permissions": SlashCommand("MODE", "Permissions", RUN,
                                 "governs the agent's next proposed action; tightening must not wait"),
    "/settings-apply": SlashCommand("WHEN", "When queued settings apply: turn-end or compaction", RUN, "client preference"),
    "/read": SlashCommand("PATH", "Read file", RUN, "read-only"),
    "/search": SlashCommand("QUERY", "Search workspace", RUN, "read-only"),
    "/edit": SlashCommand("PATH", "Propose edit", REFUSE, "a hand edit would race the agent's edits"),
    "/create": SlashCommand("PATH", "Propose file", REFUSE, "a hand edit would race the agent's edits"),
    "/output": SlashCommand("UUID", "Command output", RUN, "read-only"),
    "/resolve": SlashCommand("UUID OBSERVATION", "Reconcile unknown outcome", RUN, "the way to unblock an unknown outcome"),
}
LOCAL_COMMANDS = {"/exit", "/quit", "/help", "/clear", "/status", "/stop", "/details", "/effort", "/provider", "/model", "/permissions", "/host", "/containers", "/settings-apply"}
# When model/effort/provider changes made during a turn take effect. Synth Code has
# no context compaction yet, so "compaction" currently also applies at turn end.
SETTINGS_APPLY_CHOICES = ("compaction", "turn-end")
# Read commands handled outside the local dispatcher; they run without the busy guard.
READ_COMMANDS = {"/output", "/read", "/search"}
COMPLETIONS = list(COMMANDS) + ["/effort " + level for level in ("low", "medium", "high", "xhigh", "max")] + ["/provider reflection", "/model beam"] + ["/permissions " + mode for mode in ("manual", "auto-edit", "yolo")]


def split_command(text):
    parts = text.strip().split(maxsplit=1)
    if parts and parts[0].startswith("/") and "/" in parts[0][1:]:
        return "", text.strip()  # a pasted path such as /Users/me/prompt.txt is a message
    return (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")

def display_text(text):
    """Neutralize terminal and bidi controls; preserve ordinary text and layout."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}" for c in text)


class ConversationLexer(Lexer):
    """Style plain, already escaped text without interpreting markup or ANSI."""
    def lex_document(self, document):
        def line(index):
            text = document.lines[index]
            rendered = getattr(self, "rendered_lines", [])
            if index < len(rendered) and rendered[index] is not None and text == "".join(t for _, t in rendered[index]):
                return rendered[index]
            if text.startswith(("› ", "● ")):
                return [("class:role", text[:2]), ("", text[2:])]
            if text.startswith(("+++", "---", "@@")):
                return [("class:detail", text)]
            if text.startswith("+"):
                return [("class:added", text)]
            if text.startswith("-"):
                return [("class:removed", text)]
            return [("", text)]
        return line


class ActivityStatus(Label):
    """Manual status changes clear live projection; pane changes only reformat it."""
    def __init__(self, *args, columns=lambda: 120, **kwargs):
        self.columns = columns
        super().__init__(*args, **kwargs)

    @property
    def text(self):
        return self._text

    @text.setter
    def text(self, value):
        self._text = value
        self._activity = None

    def project(self, view, operation=None, timing=None, *, show_preview=True):
        self._activity = (view, operation, timing)
        self.refresh(show_preview=show_preview)

    def refresh(self, *, show_preview):
        if self._activity is not None:
            self._text = display_text(activity_line(*self._activity, show_preview=show_preview, compact=self.columns() < 60))


class Preview:
    def __init__(self, client, session, *, input=None, output=None, fixture=True, no_color=False, clipboard_writer=None):
        self.client, self.session = client, session
        self.fixture = fixture
        self.active_turn = None
        self.queued_settings = {}
        self.queued_messages = []
        self.settings_apply = "compaction"
        self.active_operation = None
        self.seen_turn_messages = {}
        self.seen_turn_summaries = set()
        self.seen_turn_prompts = set()
        self.seen_turn_errors = set()
        self.turn_cursor = 0
        self.session_sync_lock = asyncio.Lock()
        self.turn_usage = {}
        self.seen_operations = set()
        self.unresolved_operations = set()
        self.pending = None
        self.busy = False
        self.entries = deque()
        self.copy_targets = {}
        self.copy_actions = {}
        self.copy_ranges = []
        self.copy_pressed = None
        self.clipboard_writer = clipboard_writer or write_clipboard
        self.entry_chars = 0
        self.pane_enabled = True
        self._wide = False
        self._transcript_columns = None
        self.provider_summary = "Local fixture" if fixture else "Beam 1.0 · Reflection"
        self.credential_configured = None
        self.workspace_name = ""
        self.workspace_path = ""
        self.settings_modal = False
        self.settings_cancel = None
        self.permissions = "manual"
        self.transcript = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                   dont_extend_height=False)
        self.transcript_mouse = self.transcript.control.mouse_handler
        self.transcript.control.mouse_handler = self.copy_mouse
        self.streaming = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                  height=Dimension(min=1, max=6), dont_extend_height=True,
                                  focusable=False)
        self.composer = TextArea(height=Dimension(min=1, max=6), multiline=True,
                                 dont_extend_height=True, accept_handler=self.submit,
                                 get_line_prefix=lambda line, wrap: "› " if line == 0 and wrap == 0 else "  ",
                                 completer=ConditionalCompleter(
                                     WordCompleter(COMPLETIONS, sentence=True,
                                                   meta_dict={key: value.label for key, value in COMMANDS.items()}),
                                     Condition(lambda: self.composer.text.startswith("/"))),
                                 complete_while_typing=True,
                                 input_processors=[AfterInput(lambda: "" if self.composer.text else "Ask Synth Code to do anything",
                                                              style="class:placeholder")],
                                 style="class:composer")
        self.status = ActivityStatus("Ready", wrap_lines=False, columns=lambda: self.application.output.get_size().columns)
        self.running_detail = None
        self.tasks = set()
        self.decision = None
        self.allow = Button("Allow", handler=lambda: self.schedule(self.respond(True)))
        self.deny = Button("Deny", handler=lambda: self.schedule(self.respond(False)))
        keys = KeyBindings()

        @keys.add("c-q")
        def quit_(event):
            event.app.exit()

        @keys.add("c-c")
        def interrupt_(event):
            if self.active_turn:
                self.schedule(self.interrupt())
            elif self.active_operation:
                self.schedule(self.client.call("synth/operation/cancel", {"operationId":self.active_operation}))
            else:
                event.app.exit()

        @keys.add("escape", filter=Condition(lambda: self.settings_modal), eager=True)
        def cancel_settings_(event):
            self.settings_cancel()

        @keys.add("escape", filter=Condition(lambda: not self.settings_modal and bool(self.active_turn or self.active_operation)))
        def interrupt_turn_(event):
            if self.active_turn:
                self.schedule(self.interrupt())
            else:
                self.schedule(self.client.call("synth/operation/cancel", {"operationId":self.active_operation}))

        @keys.add("escape", "enter", filter=has_focus(self.composer))
        def newline_(event):
            event.current_buffer.insert_text("\n")

        @keys.add("enter", filter=has_focus(self.composer))
        def send_(event):
            event.current_buffer.validate_and_handle()

        @keys.add("tab")
        def next_(event):
            buffer = self.composer.buffer
            if (event.app.layout.has_focus(self.composer) and buffer.text.startswith("/")
                    and buffer.cursor_position == len(buffer.text)
                    and (buffer.complete_state or any(word.startswith(buffer.text) for word in COMPLETIONS))):
                if buffer.complete_state:
                    buffer.complete_next()
                else:
                    match = next((name for name in COMPLETIONS if name.startswith(buffer.text)), None)
                    if match:
                        buffer.apply_completion(Completion(match, start_position=-len(buffer.text)))
            else:
                event.app.layout.focus_next()

        @keys.add("s-tab")
        def previous_(event):
            if event.app.layout.has_focus(self.composer) and self.composer.buffer.complete_state:
                self.composer.buffer.complete_previous()
            else:
                event.app.layout.focus_previous()

        @keys.add("c-y", filter=has_focus(self.transcript))
        def copy_response_(event):
            row = self.transcript.buffer.document.cursor_position_row
            target = next((target for start, end, target in self.copy_ranges if start <= row <= end), None)
            if target is not None:
                self.schedule(self.copy_response(target))

        @keys.add("c-b")
        def toggle_details_(event):
            self.toggle_details()

        self.header = Window(FormattedTextControl(self.header_fragments), dont_extend_height=True)
        self.detail_title = Label("", style="class:pane-title")
        self.details = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                focusable=True, style="class:details")
        welcome = ConditionalContainer(HSplit([self.header, Window()]),
                                       filter=Condition(lambda: not self.transcript.text and not self.streaming.text))
        conversation = HSplit([
            welcome,
            ConditionalContainer(self.transcript, filter=Condition(lambda: bool(self.transcript.text))),
            ConditionalContainer(self.streaming, filter=Condition(lambda: bool(self.streaming.text) and not self.pending)),
        ])
        self.approvals = ConditionalContainer(
            Box(VSplit([self.allow, self.deny], padding=2), padding_top=0,
                padding_bottom=1, padding_left=2, padding_right=2, style="class:composer"),
            filter=Condition(lambda: self.pending is not None),
        )
        side = ConditionalContainer(VSplit([
            Window(width=1, char="│", style="class:separator"),
            Box(HSplit([self.detail_title, Window(height=1), self.details]),
                padding_top=1, padding_bottom=0, padding_left=2, padding_right=2,
                width=Dimension(weight=1)),
        ]), filter=Condition(self.details_visible))
        self.footer = Box(Label(self.footer_text, style="class:footer", wrap_lines=False),
                          padding_top=0, padding_bottom=0, padding_left=2, padding_right=2)
        self.main = HSplit([
            VSplit([Box(conversation, padding_top=1, padding_bottom=0,
                        padding_left=2, padding_right=2, width=Dimension(weight=1)), side]),
            Box(self.status, padding_top=0, padding_bottom=0, padding_left=2,
                padding_right=2, style="class:muted"),
            Box(self.composer, padding_top=1, padding_bottom=1, padding_left=2,
                padding_right=2, style="class:composer"),
            self.approvals,
            self.footer,
        ], style="class:app")
        self.main = FloatContainer(self.main, floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1)),
        ])
        monochrome = no_color or "NO_COLOR" in os.environ
        theme = {} if monochrome else {
            "": "bg:#24272e #d7d9df", "app": "bg:#24272e", "brand": "#d5a560 bold",
            "placeholder": "#6f7380", "key": "#d7d9df bold", "value": "#d7d9df",
            "role": "#d7d9df bold", "muted": "#979baa",
            "detail": "#979baa", "copy": "#979baa", "separator": "#626977",
            "composer": "bg:#2c2c2b #eeece8", "footer": "#979baa",
            "pane-title": "#d5a560 bold", "strong": "bold", "emphasis": "italic",
            "heading": "#d7d9df bold", "table-header": "#8faad1 bold",
            "inline-code": "bg:#30343c #dfd9ca", "code": "#bec7d9", "link": "#8faad1 underline",
            "added": "#85b8a6",
            "removed": "#d28f91", "button": "bg:#343536 #d7d9df",
            "button.focused": "bg:#b69a70 #181a1f bold",
            "completion-menu.completion": "bg:#30343c #d7d9df",
            "completion-menu.completion.current": "bg:#b69a70 #181a1f",
            "completion-menu.meta.completion": "bg:#30343c #979baa",
            "completion-menu.meta.completion.current": "bg:#b69a70 #181a1f",
            "dialog": "bg:#292c33 #d7d9df",
            "dialog.body": "bg:#292c33 #d7d9df",
            "dialog frame.label": "#d5a560 bold",
            "radio-selected": "bg:#383d45 #eeece8",
            "radio-checked": "#d5a560 bold",
        }
        self.application = Application(
            layout=Layout(self.main, focused_element=self.composer),
            key_bindings=keys, full_screen=True, mouse_support=True,
            input=input, output=output,
            style=Style.from_dict({"brand": "bold", "role": "bold",
                                   "detail": "italic", "strong": "bold", "heading": "bold",
                                   "emphasis": "italic", "table-header": "bold",
                                   "link": "underline", "button.focused": "reverse bold", **theme}),
            color_depth=ColorDepth.DEPTH_1_BIT if monochrome else ColorDepth.TRUE_COLOR,
            before_render=self.resize_layout,
        )

    def details_visible(self):
        application = getattr(self, "application", None)
        return bool(application and self.pane_enabled and self.details.text
                    and application.output.get_size().columns >= 120)

    def toggle_details(self):
        self.pane_enabled = not self.pane_enabled
        self.refresh_transcript()
        self.status.refresh(show_preview=not self.details_visible())
        if not self.details_visible() and self.application.layout.has_focus(self.details):
            self.application.layout.focus(self.deny if self.pending else self.composer)

    def workspace_label(self, width=None):
        home = str(Path.home())
        path = self.workspace_path
        if path == home or path.startswith(home + os.sep):
            path = "~" + path[len(home):]
        path = display_text(path or self.workspace_name)
        if width is not None and len(path) > width:
            path = "…" + path[-max(1, width - 1):]
        return path

    def columns(self):
        application = getattr(self, "application", None)
        return application.output.get_size().columns if application else 80

    def rows(self):
        application = getattr(self, "application", None)
        return application.output.get_size().rows if application else 24

    def header_fragments(self):
        return [("class:brand", "✦ Synth Code"), ("class:muted", f" (v{VERSION})\n"),
                ("class:muted", "  " + self.workspace_label(self.columns() - 8) + "\n"),
                ("class:muted", "provider: "), ("class:value", self.provider_summary + "\n"),
                ("class:muted", "permissions: "), ("class:key", self.permissions)]

    def footer_text(self):
        totals = list(self.turn_usage.values())
        token_text = ""
        if any(t[2] for t in totals):
            partial = any(t[3] for t in totals)
            token_text = "  ·  Σ " + ("≥" if partial else "") + f"{sum(t[0] for t in totals):,} in · {sum(t[1] for t in totals):,} out"
        if self.pending:
            hints = "tab switch · enter confirm"
        elif self.active_turn or self.active_operation:
            hints = "esc interrupt · enter queue message"
        else:
            hints = "/ for commands · alt+enter newline · ctrl-q quit"
        if self.details.text:
            hints += " · ctrl-b details"
        status = self.provider_summary + "  ·  " + self.permissions.upper() + "  ·  "
        room = self.columns() - 4 - len(status) - len(token_text)
        queued = "".join(f"  ·  {field} → {self.display_choice(value)} queued" for field, value in self.queued_settings.items())
        if self.queued_messages:
            count = len(self.queued_messages)
            queued += f"  ·  {count} message{'s' if count > 1 else ''} queued"
        room -= len(queued)
        line = status + self.workspace_label(max(12, room)) + token_text + queued
        # Short terminals keep the transcript; the hint line returns when there is room.
        if self.rows() < 16:
            return [("", line)]
        return [("", line + "\n"), ("class:muted", hints)]

    def resize_layout(self, application):
        wide = self.details_visible()
        columns = application.output.get_size().columns
        if self._wide != wide or self._transcript_columns != columns:
            self._wide = wide
            self._transcript_columns = columns
            if not wide and application.layout.has_focus(self.details):
                application.layout.focus(self.deny if self.pending else self.composer)
            self.refresh_transcript()
            self.status.refresh(show_preview=not self.details_visible())

    def refresh_transcript(self, *, follow=True):
        # Bound the rendered document as well as its source entries. Table
        # padding must not amplify a bounded provider message without limit.
        blocks = []
        remaining = 100_000
        application = getattr(self, "application", None)
        columns = application.output.get_size().columns if application else 80
        width = max(1, (columns // 2 if self.details_visible() else columns) - 4)
        old_cursor = self.transcript.buffer.cursor_position
        for entry in reversed(self.entries):
            text, detail, role = entry
            target = self.copy_targets.get(id(entry))
            copy_width = max(1, min(200, width - (1 if self.details_visible() else 0)))
            reserve = copy_width + 1 if target else 0
            if detail:
                if not self.details_visible():
                    text += "\n" + detail
                elif role != "tool-result":
                    text += "\n  … (+" + str(len(detail.splitlines())) + " lines)"
            if remaining <= reserve:
                break
            lines = (markdown(text, width=width, max_chars=remaining-reserve) if role == "assistant" else
                     [[("class:muted" if role == "status" else "", line)] for line in text[-remaining:].splitlines()])
            if target:
                label = target.feedback[:copy_width]
                lines.append([("class:copy", " " * (copy_width-len(label)) + label)])
            used = sum(len(value) for line in lines for _, value in line) + max(0, len(lines)-1)
            blocks.append((lines, target, copy_width))
            remaining -= used + 2
        rendered = []
        self.copy_actions = {}
        self.copy_ranges = []
        for lines, target, copy_width in reversed(blocks):
            if rendered:
                rendered.append([])
            start = len(rendered)
            rendered.extend(lines)
            if target:
                row = len(rendered)-1
                label = target.feedback[:copy_width]
                self.copy_actions[row] = (copy_width-len(label), copy_width, target)
                self.copy_ranges.append((start, row, target))
        # Lexer fragments and the plain document share exact character positions.
        self.transcript.lexer.rendered_lines = [line if any(style for style, _ in line) else None for line in rendered]
        self.transcript.text = "\n".join("".join(t for _, t in line) for line in rendered)
        self.transcript.buffer.cursor_position = (len(self.transcript.text.rstrip()) if follow
                                                  else min(old_cursor, len(self.transcript.text)))

    def copy_mouse(self, event):
        action = self.copy_actions.get(event.position.y)
        if (action and action[0] <= event.position.x < action[1]
                and event.button == MouseButton.LEFT):
            if event.event_type == MouseEventType.MOUSE_DOWN:
                self.copy_pressed = action[2]
                return None
            if event.event_type == MouseEventType.MOUSE_UP:
                target, self.copy_pressed = self.copy_pressed, None
                if target is action[2]:
                    self.schedule(self.copy_response(target))
                return None
        if event.event_type == MouseEventType.MOUSE_UP:
            self.copy_pressed = None
        return self.transcript_mouse(event)

    async def copy_response(self, target):
        if target.copying or not any(item is target for item in self.copy_targets.values()):
            return
        target.copying = True
        try:
            text = await response_text(self.client, target)
            if not any(item is target for item in self.copy_targets.values()):
                return
            await self.clipboard_writer(display_text(text))
            target.feedback = "✓ Copied"
        except Exception:
            target.feedback = "! Copy unavailable"
        finally:
            target.copying = False
        if not any(item is target for item in self.copy_targets.values()):
            return
        self.refresh_transcript(follow=False)
        self.application.invalidate()
        await asyncio.sleep(1.5)
        target.feedback = "⧉"
        if any(item is target for item in self.copy_targets.values()):
            self.refresh_transcript(follow=False)
            self.application.invalidate()

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def completed(done):
            self.tasks.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                self.status.text = "Error"
                self.append(str(error))
                self.application.invalidate()
        task.add_done_callback(completed)

    def submit(self, buffer):
        command, _ = split_command(buffer.text)
        if buffer.text.strip() and (command in LOCAL_COMMANDS or command in READ_COMMANDS
                                    or self.turn_running() or not self.busy):
            self.schedule(self.admit(buffer.text))
            return False
        return True

    async def local_command(self, command, argument):
        if command == "/containers":
            result = await self.client.call("synth/host/containers", {"threadId": self.session, "query": argument})
            rows = [f"{item['id'][:12]}  {item['name']}  {item['state']}\n  " + ", ".join(item['imageTags'] or [item['image']]) for item in result["containers"]]
            empty = "No name/image ID matches" if result.get("imageMetadataError") else "No matching containers"
            self.append("Docker · " + result["context"] + "\n" + ("\n".join(rows) or empty)
                        + ("\nInventory truncated" if result["truncated"] else "")
                        + ("\nPartial inventory · image tags unavailable" if result.get("imageMetadataError") else ""))
            return
        if command == "/host":
            if argument:
                self.append("Usage · /host")
                return
            result = await self.client.call("synth/host/status", {"threadId": self.session})
            tools = "\n".join(name + " · " + str(path or "unavailable") for name, path in result["executables"].items())
            self.append("Host · " + result["platform"] + "\nWorkspace · " + result["workspace"] + "\nHome · " + str(result["homeDirectory"] or "unavailable") + "\n" + tools)
            return
        if command == "/permissions":
            await self.permissions_command(argument)
            return
        if command in {"/effort", "/provider", "/model"}:
            await self.provider_command(command[1:], argument)
            return
        if command == "/settings-apply":
            value = argument.strip().casefold()
            if value not in SETTINGS_APPLY_CHOICES:
                self.append("Settings apply · " + self.settings_apply + "\nSupported · " + ", ".join(SETTINGS_APPLY_CHOICES))
                return
            self.settings_apply = value
            self.append("Settings apply · " + value)
            return
        if argument:
            self.append("Usage · " + command)
            return
        if command in {"/exit", "/quit"}:
            # Same detach semantics as Ctrl+Q; the daemon retains pending work.
            self.application.exit()
        elif command == "/help":
            self.append("Commands (while the agent works: · queued, × refused)\n" + "\n".join(
                f"{(name + (' ' + syntax if syntax else '')):28} {label}"
                + {RUN: "", QUEUE: " · queued", REFUSE: " ×"}[during_turn]
                for name, (syntax, label, during_turn, _) in COMMANDS.items()))
        elif command == "/clear":
            if self.pending:
                self.append("Clear unavailable · approval pending")
                return
            self.entries.clear()
            self.copy_targets.clear()
            self.copy_pressed = None
            self.entry_chars = 0
            self.streaming.text = ""
            self.details.text = ""
            self.running_detail = None
            self.detail_title.text = ""
            self.refresh_transcript()
            self.application.layout.focus(self.composer)
        elif command == "/status":
            status = await self.client.call("synth/status", {})
            self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
            if not self.fixture:
                self.show_provider(await self.client.call("synth/provider/status", {"threadId": self.session}))
            operation = self.pending["id"] if self.pending else self.active_operation
            self.append(f"Session · {self.session}\nWorkspace · {self.workspace_path}\n"
                        f"Provider · {self.provider_summary}\nPermissions · {self.permissions}\nDaemon · {status['ownerPid']}\n"
                        f"Turn · {self.active_turn or 'none'}\nOperation · {operation or 'none'}")
        elif command == "/stop":
            if self.active_turn:
                await self.interrupt()
            elif self.active_operation:
                await self.client.call("synth/operation/cancel", {"operationId": self.active_operation})
            elif self.busy:
                self.append("Task admission pending")
            else:
                self.append("No running task")
        elif command == "/details":
            self.toggle_details()

    def show_provider(self, status):
        self.credential_configured = status["credentialConfigured"]
        model = status.get("model", "Beam 1.0")
        model = "Beam 1.0" if model == "Beam-1.0-501B-A23B" else model
        self.provider_summary = display_text(model + " · " + status.get("effort", "medium") + " · Reflection")
        if status["credentialConfigured"] is False:
            self.provider_summary += " missing"

    async def provider_command(self, field, argument):
        if self.busy or self.settings_modal:
            self.append("Settings unavailable · menu open")
            return
        status = await self.client.call("synth/provider/status", {"threadId": self.session})
        self.show_provider(status)
        if "choices" not in status or "revision" not in status:
            self.append("Settings unavailable · runtime update required")
            return
        if self.busy or self.settings_modal:
            self.append("Settings unavailable · menu open")
            return
        choices = status["choices"][{"effort": "efforts", "provider": "providers", "model": "models"}[field]]
        if argument:
            value = argument.strip().casefold()
            if field == "model" and value.replace(" ", "") in {"beam", "beam1.0", "beam-1.0"}:
                value = "Beam-1.0-501B-A23B"
            value = next((choice for choice in choices if choice.casefold() == value.casefold()), None)
            if value is None:
                self.append("Unsupported " + field + " · " + argument + "\nSupported · " + ", ".join(choices))
                return
            await self.configure_provider(field, value, status)
            return
        labels = {"effort": "Reasoning effort", "provider": "Provider", "model": "Model"}
        self.settings_dialog(labels[field] + " · " + status[field],
            [(choice, "Beam 1.0" if choice == "Beam-1.0-501B-A23B" else choice.title()) for choice in choices],
            status[field], lambda value: self.configure_provider(field, value, status))

    def settings_dialog(self, title, choices, current, apply):
        values = RadioList(choices, default=current)
        def cancel():
            self.settings_modal = False
            self.settings_cancel = None
            self.application.layout.container = self.main
            self.application.layout.focus(self.composer)
        def select():
            value = values.current_value
            cancel()
            self.schedule(apply(value))
        dialog = Dialog(title=title, body=values,
                        buttons=[Button("Select", handler=select), Button("Cancel", handler=cancel)])
        self.settings_modal = True
        self.settings_cancel = cancel
        self.application.layout.container = FloatContainer(self.main, floats=[
            Float(content=dialog,
                  width=lambda: max(1, min(64, self.application.output.get_size().columns-4)),
                  height=lambda: min(12, self.application.output.get_size().rows),
                  z_index=200_000_000),
        ])
        self.application.layout.focus(values)

    async def permissions_command(self, argument):
        # The runtime reads the mode when each action is proposed, so a change
        # mid-turn applies from the agent's next action; tightening must not wait.
        if self.busy or self.settings_modal:
            self.append("Permissions unavailable · menu open")
            return
        status = await self.client.call("synth/permissions/status", {"threadId": self.session})
        self.permissions = status["mode"]
        if self.busy or self.settings_modal:
            self.append("Permissions unavailable · menu open")
            return
        if argument:
            value = argument.strip().casefold()
            if value not in status["choices"]:
                self.append("Unsupported permissions · " + argument + "\nSupported · " + ", ".join(status["choices"]))
                return
            await self.configure_permissions(value, status)
            return
        labels = {"manual": "Manual · ask for edits and commands",
                  "auto-edit": "Auto-edit · ask for commands", "yolo": "YOLO · allow edits and commands"}
        self.settings_dialog("Permissions · " + status["mode"],
            [(mode, labels[mode]) for mode in status["choices"]], status["mode"],
            lambda value: self.configure_permissions(value, status))

    async def configure_permissions(self, value, status):
        if self.busy:
            self.append("Permissions unavailable · settings change in progress")
            return
        self.busy = True
        try:
            result = await self.client.call("synth/permissions/configure", {
                "mutationId": str(uuid4()), "threadId": self.session,
                "mode": value, "expectedRevision": status["revision"],
            })
            self.permissions = result["status"]["mode"]
            note = ""
            if self.turn_running():
                note = " · applies from the agent's next action"
                if self.pending:
                    note += "; the pending approval still needs your decision"
            self.append("Permissions · " + self.permissions + note)
        except Exception as error:
            self.append(str(error))
            if not self.client.poisoned:
                self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        finally:
            self.busy = False
            self.application.invalidate()

    def turn_running(self):
        return bool(self.pending or self.active_turn or self.active_operation)

    def queue_setting(self, field, value):
        self.queued_settings[field] = value
        when = ("at the next compaction (none yet, so when this turn ends)" if self.settings_apply == "compaction"
                else "when this turn ends")
        self.append(f"{field.title()} · {self.display_choice(value)} · queued, applies {when}")
        self.application.invalidate()

    def display_choice(self, value):
        return "Beam 1.0" if value == "Beam-1.0-501B-A23B" else value

    async def after_turn(self):
        await self.apply_queued_settings()
        if self.queued_messages and not self.turn_running():
            text = self.queued_messages.pop(0)
            self.append("Queued message sent")
            try:
                await self.start_turn(text, echo=False)
            except Exception as error:
                self.append(str(error))

    async def apply_queued_settings(self):
        if not self.queued_settings or self.turn_running():
            return
        queued, self.queued_settings = self.queued_settings, {}
        status = await self.client.call("synth/provider/status", {"threadId": self.session})
        for field, value in queued.items():
            await self.configure_provider(field, value, status, queued=True)
            status = await self.client.call("synth/provider/status", {"threadId": self.session})

    async def configure_provider(self, field, value, status, *, queued=False):
        if self.turn_running():
            self.queue_setting(field, value)
            return
        if self.busy:
            self.append("Settings unavailable · task active or approval pending")
            return
        self.busy = True
        selection = dict(status["selection"])
        selection[field] = value
        try:
            result = await self.client.call("synth/provider/configure", {
                "mutationId": str(uuid4()), "threadId": self.session,
                "selection": selection, "expectedRevision": status["revision"],
            })
            self.show_provider(result["status"])
            self.append(field.title() + " · " + self.display_choice(result["status"][field]) + (" · applied (was queued)" if queued else ""))
        except Exception as error:
            self.append(str(error))
            if not self.client.poisoned:
                self.show_provider(await self.client.call("synth/provider/status", {"threadId": self.session}))
        finally:
            self.busy = False
            self.application.invalidate()

    def append(self, text, role=None, *, detail=None, detail_title="Operation", source=None):
        # Escape provider/file text before it reaches any terminal widget.
        prefix = "› " if role == "user" else ""
        text = prefix + display_text(text)
        detail = display_text(detail) if detail is not None else None
        complete = len(text) <= 100_000
        text = text[-100_000:]
        detail = detail[-(100_000-len(text)):] if detail and len(text) < 100_000 else None
        if not text and not detail:
            return
        entry = (text, detail, role)
        self.entries.append(entry)
        if role == "assistant":
            self.copy_targets[id(entry)] = ResponseCopy(text, source, complete)
        self.entry_chars += len(text) + len(detail or "") + 2
        while len(self.entries) > 1 and (self.entry_chars > 100_000 or len(self.entries) > 1000):
            old_entry = self.entries.popleft()
            old_text, old_detail, _ = old_entry
            self.copy_targets.pop(id(old_entry), None)
            self.entry_chars -= len(old_text) + len(old_detail or "") + 2
        if detail is not None:
            self.detail_title.text = display_text(detail_title)
            self.details.text = detail[-100_000:]
            self.details.buffer.cursor_position = 0
        self.refresh_transcript()

    def operation_result(self, operation):
        if operation["status"] == "unknown":
            self.unresolved_operations.add(operation["id"])
        elif operation["status"] == "reconciled":
            self.unresolved_operations.discard(operation["id"])
        outcome = operation.get("outcome") or {}
        summary = "Action · " + operation["status"]
        if operation.get("edit"):
            summary += " · " + operation["edit"]["path"]
        if "exit_code" in outcome:
            summary += " · exit " + str(outcome["exit_code"])
        if operation["status"] == "succeeded":
            summary = ("✓ Updated " + operation["edit"]["path"] if operation.get("edit")
                       else "✓ Command completed" + (" · exit " + str(outcome["exit_code"]) if "exit_code" in outcome else ""))
        for field, label in (("timed_out", "timeout"), ("truncated", "output truncated"), ("gateway_lost", "gateway lost")):
            if outcome.get(field):
                summary += " · " + label
        detail = "Operation · " + operation["id"]
        if operation.get("shell"):
            detail = operation["shell"]["command"] + "\n\n" + detail
        for stream in ("stdout", "stderr"):
            if outcome.get(stream):
                detail += "\n\n" + stream + "\n" + outcome[stream]
        if operation.get("edit"):
            edit = operation["edit"]
            diff = "\n".join(difflib.unified_diff((edit["before"] or "").splitlines(), edit["content"].splitlines(),
                              fromfile=edit["path"], tofile=edit["path"], lineterm=""))
            detail = diff + "\n\n" + detail
        title = ("Changes" if operation.get("edit") else "Command output") + " · " + operation["status"]
        routine_success = (operation["status"] == "succeeded" and outcome.get("exit_code") in {None, 0}
                           and not any(outcome.get(field) for field in
                                       ("timed_out", "truncated", "gateway_lost", "error", "reason")))
        if routine_success:
            self.detail_title.text = display_text(title)
            self.details.text = display_text(detail)[-100_000:]
            self.details.buffer.cursor_position = 0
            self.refresh_transcript()
            self.status.refresh(show_preview=not self.details_visible())
        else:
            self.append(summary, "tool-result", detail=detail, detail_title=title)
        for field in ("error", "reason"):
            if outcome.get(field):
                self.append(str(outcome[field]) + "\nOperation · " + operation["id"])

    async def admit(self, text):
        command, argument = split_command(text)
        spec = COMMANDS.get(command)
        if spec and spec.during_turn is REFUSE and self.turn_running():
            self.append(f"{command} unavailable while the agent is working · {spec.why} · press Esc to interrupt first")
            self.application.invalidate()
            return
        if command in LOCAL_COMMANDS:
            try:
                await self.local_command(command, argument)
            except Exception as error:
                self.append(str(error))
            self.application.invalidate()
            return
        if command in READ_COMMANDS:
            try:
                await self.read_command(command, argument)
            except Exception as error:
                self.append(str(error))
            self.application.invalidate()
            return
        if self.turn_running():
            if command == "/resolve":
                try:
                    await self.resolve_command(argument)
                except Exception as error:
                    self.append(str(error))
            elif command.startswith("/"):
                self.append("Unknown command · " + command + " · Commands: /help")
            else:
                self.queued_messages.append(text)
                self.append(text, "user")
                self.append("Message queued · sent when this turn ends")
            self.application.invalidate()
            return
        if self.busy or self.pending:
            return
        self.busy = True
        try:
            if command == "/resolve":
                await self.resolve_command(argument)
                return
            if command in {"/edit", "/create"}:
                before = await self.client.call("workspace/read", {"threadId": self.session, "path": argument}) if command == "/edit" else None
                self.edit_dialog(argument, before)
                return
            if command.startswith("/"):
                raise ValueError("Unknown command · " + command + " · Commands: /help")
            await self.start_turn(text)
        except Exception as error:
            self.status.text = "Error"
            self.append(str(error))
            if not self.client.poisoned and "session_permissions_changed" in str(error):
                self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        finally:
            self.busy = False
            self.application.invalidate()

    async def read_command(self, command, argument):
        if command == "/output":
            for stream in ("stdout", "stderr"):
                after = 0
                chunks = []
                while True:
                    page = await self.client.call("synth/operation/output", {
                        "operationId": argument, "stream": stream, "after": after,
                    })
                    chunks.append(page["text"])
                    after = page["nextCursor"]
                    if after is None:
                        break
                self.append(stream + "\n" + "".join(chunks))
            status = "Output"
        elif command == "/read":
            file = await self.client.call("workspace/read", {"threadId": self.session, "path": argument})
            self.append(file["path"] + "\n" + file["text"])
            status = "Read"
        else:
            result = await self.client.call("workspace/search", {"threadId": self.session, "query": argument})
            self.append("\n".join(f"{m['path']}:{m['line']} {m['text']}" for m in result["matches"])
                        or "No matches")
            status = f"{len(result['matches'])} matches" + (" · truncated" if result["truncated"] else "")
        # While the agent works the status line shows its activity instead.
        if not self.turn_running():
            self.status.text = status

    async def resolve_command(self, argument):
        identifier, _, observation = argument.partition(" ")
        op = (await self.client.call("synth/operation/read", {"operationId":identifier}))["operation"]
        await self.client.call("synth/operation/reconcile", {"mutationId":str(uuid4()), "operationId":identifier,
            "requestDigest":op["request_digest"], "observation":observation})
        self.append("Reconciled · " + identifier)

    async def start_turn(self, text, *, echo=True):
        if not self.fixture:
            async with self.session_sync_lock:
                await self.sync_session_locked(allow_busy=True)
                if self.active_turn or self.pending or self.active_operation:
                    raise ValueError("Task already active")
                result = await self.client.call("turn/start", {"mutationId":str(uuid4()), "threadId":self.session, "prompt":text, "expectedPermissions":self.permissions})
                if echo:
                    self.append(text, "user")
                self.seen_turn_prompts.add(result["id"])
                self.active_turn = result["id"]
                self.permissions = result["permissions"]
                if result.get("selection"):
                    self.show_provider({**result["selection"], "credentialConfigured": self.credential_configured})
                self.status.project(result, show_preview=not self.details_visible())
                self.schedule(self.watch_turn())
            return
        result = await self.client.call("fixture/admit", {
            "mutationId": str(uuid4()), "threadId": self.session, "text": text,
        })
        self.append(text, "user")
        self.show_pending(result["result"])

    def show_pending(self, operation):
        self.pending = operation
        self.decision = None
        ready = operation.get("status") == "ready"
        self.allow.text = "Resume" if ready else "Allow"
        self.deny.text = "Cancel" if ready else "Deny"
        edit = operation.get("edit")
        if edit:
            diff = "\n".join(difflib.unified_diff((edit["before"] or "").splitlines(), edit["content"].splitlines(), fromfile=edit["path"], tofile=edit["path"], lineterm=""))
            before = edit["before"] or ""
            diff += f"\n{len(before.encode())} → {len(edit['content'].encode())} bytes"
            if not edit["content"].endswith("\n"):
                diff += " · no final newline"
            self.append("Edit · " + edit["path"], detail=diff, detail_title="Changes · " + edit["path"])
        elif operation.get("shell"):
            shell = operation["shell"]
            self.append("Command · " + shell["command"], detail=f"Timeout · {shell['timeout_seconds']}s\nOperation · " + operation["id"], detail_title="Command")
        else:
            self.append("Fixture · fixture-" + operation["id"] + ".txt")
        self.status.text = "Approved · awaiting execution" if ready else "Awaiting approval"
        self.application.layout.focus(self.deny)

    def show_running_operation(self, operation):
        if operation["status"] not in {"ready", "dispatched"} or self.running_detail == (operation["id"], operation["status"]):
            return
        self.running_detail = (operation["id"], operation["status"])
        if operation.get("shell"):
            shell = operation["shell"]
            self.detail_title.text = "Command · " + ("running" if operation["status"] == "dispatched" else "approved")
            text = shell["command"] + f"\n\nTimeout · {shell['timeout_seconds']}s\nOperation · " + operation["id"]
        elif operation.get("edit"):
            edit = operation["edit"]
            self.detail_title.text = display_text("Changes · " + edit["path"])
            text = "\n".join(difflib.unified_diff((edit["before"] or "").splitlines(), edit["content"].splitlines(), fromfile=edit["path"], tofile=edit["path"], lineterm=""))
        else:
            return
        self.details.text = display_text(text)[-100_000:]
        self.details.buffer.cursor_position = 0

    def edit_dialog(self, path, before):
        editor = TextArea(text=before["text"] if before else "", multiline=True, scrollbar=True)
        def cancel():
            self.settings_modal = False
            self.settings_cancel = None
            self.application.layout.container = self.main
            self.application.layout.focus(self.composer)
        def propose():
            content = editor.text
            cancel()
            self.schedule(self.propose_edit(path, before, content))
        dialog = Dialog(title=display_text(path), body=editor, buttons=[Button("Propose", handler=propose), Button("Cancel", handler=cancel)])
        self.settings_modal = True
        self.settings_cancel = cancel
        self.application.layout.container = to_container(dialog)
        self.application.layout.focus(editor)

    async def propose_edit(self, path, before, content):
        if self.busy or self.pending:
            return
        self.busy = True
        try:
            result = await self.client.call("workspace/proposeEdit", {
                "mutationId": str(uuid4()), "threadId": self.session, "path": path,
                "expectedSha256": before["version"]["sha256"] if before else None, "content": content,
                "expectedPermissions": self.permissions,
            })
            self.permissions = result["permissions"]
            operation = result["result"]
            if operation["status"] == "awaiting_approval" or operation["status"] == "ready":
                self.show_pending(operation)
            elif operation["status"] == "dispatched":
                self.active_operation = operation["id"]
                self.schedule(self.watch_operation())
            else:
                self.operation_result(operation)
                self.status.text = operation["status"]
        except Exception as error:
            self.status.text = "Error"
            self.append(str(error))
            if not self.client.poisoned and "session_permissions_changed" in str(error):
                self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        finally:
            self.busy = False
            self.application.invalidate()

    async def restore(self):
        self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        if not self.fixture:
            provider = await self.client.call("synth/provider/status", {"threadId": self.session})
            self.show_provider(provider)
        view = await self.client.call("thread/read", {"threadId": self.session})
        if not view["sessions"]:
            raise ValueError("Session not found")
        workspace = view["sessions"][0]["workspace"]
        self.workspace_path = workspace
        self.workspace_name = display_text(Path(workspace).name or workspace)
        unresolved_operation = None
        operations = list(view["operations"])
        while view.get("nextCursor") is not None:
            view = await self.client.call("thread/read", {"threadId": self.session, "after": view["nextCursor"], "atSequence": view["sequence"]})
            operations.extend(view["operations"])
        for operation in operations:
            if operation["status"] == "unknown":
                unresolved_operation = operation
                self.unresolved_operations.add(operation["id"])
            if operation["status"] in {"succeeded", "failed", "unknown", "denied", "canceled", "reconciled"}:
                self.operation_result(operation)
                self.seen_operations.add(operation["id"])
            if operation["status"] == "awaiting_approval" or (operation["status"] == "ready" and not operation.get("turn")):
                self.show_pending(operation)
            elif operation["status"] in {"unknown", "ready", "dispatched"}:
                self.status.text = operation["status"]
                if operation["status"] == "dispatched" and not operation.get("turn"):
                    self.active_operation = operation["id"]
                    self.schedule(self.watch_operation())

        if not self.fixture:
            after = 0
            while True:
                page = await self.client.call("turn/list", {"threadId": self.session, "after":after})
                for turn in page["turns"]:
                    view = await self.client.call("turn/read", {"turnId":turn["id"]})
                    self.append(view["prompt"], "user")
                    self.seen_turn_prompts.add(view["id"])
                    self.turn_cursor = max(self.turn_cursor, turn["ordinal"])
                    await self.render_turn(view)
                    if view["phase"] in {"running", "waiting"}:
                        self.active_turn = view["id"]
                after = page["nextCursor"]
                if after is None:
                    break
            if self.active_turn:
                self.schedule(self.watch_turn())
            elif self.pending:
                # A standalone proposal can coexist with completed turn history.
                # Its approval state takes precedence over historical summaries.
                self.status.project({"phase": "waiting"}, self.pending, show_preview=not self.details_visible())
            elif unresolved_operation:
                self.status.project({"phase": "waiting"}, unresolved_operation, show_preview=not self.details_visible())

    async def render_turn(self, view, current_operation=None, operation_activity=None):
        self.turn_usage[view["id"]] = usage(view)
        if self.active_turn == view["id"] and "permissions" in view:
            self.permissions = view["permissions"]
        if view.get("selection") and self.active_turn == view["id"]:
            self.show_provider({**view["selection"], "credentialConfigured": self.credential_configured})
        seen = self.seen_turn_messages.get(view["id"], 0)
        for message in view["messages"]:
            if message["index"] >= seen and message["text"]:
                if message["truncated"]:
                    after = 0
                    chunks = []
                    while True:
                        page = await self.client.call("turn/message/read", {"turnId":view["id"], "index":message["index"], "after":after})
                        chunks.append(page["text"])
                        after = page["nextCursor"]
                        if after is None:
                            break
                    self.append("".join(chunks), "assistant", source=(view["id"], message["index"]))
                else:
                    self.append(message["text"], "assistant", source=(view["id"], message["index"]))
        self.seen_turn_messages[view["id"]] = max([m["index"]+1 for m in view["messages"]], default=seen)
        text = summary(view)
        if text and view["id"] not in self.seen_turn_summaries:
            self.append(text, "status")
            self.seen_turn_summaries.add(view["id"])
        self.status.project(view, current_operation or self.pending, operation_activity, show_preview=not self.details_visible())
        # Provisional output has its own view; only durable messages enter history.
        live = view.get("liveText") or ""
        self.streaming.text = "● " + display_text(live) if live else ""
        self.streaming.buffer.cursor_position = len(self.streaming.text)

    async def project_turn(self, view):
        current_operation = None
        uncertain_operation = None
        operation_activity = None
        for identifier in view["operations"]:
            detail = await self.client.call("synth/operation/read", {"operationId":identifier})
            operation = detail["operation"]
            if (self.pending and self.pending["id"] == identifier and not self.busy
                    and operation["status"] != "awaiting_approval"):
                self.pending = None
                self.decision = None
                if self.application.layout.has_focus(self.allow) or self.application.layout.has_focus(self.deny):
                    self.application.layout.focus(self.composer)
            if operation["status"] == "unknown":
                uncertain_operation = operation
                self.unresolved_operations.add(identifier)
            if operation["status"] in {"awaiting_approval", "ready", "dispatched"}:
                current_operation, operation_activity = operation, detail.get("activity")
            if operation["status"] == "awaiting_approval" and not self.pending:
                self.show_pending(operation)
            elif operation["status"] in {"succeeded", "failed", "unknown", "denied", "canceled"} and identifier not in self.seen_operations:
                self.seen_operations.add(identifier)
                self.operation_result(operation)
        if current_operation:
            self.show_running_operation(current_operation)
        await self.render_turn(view, current_operation, operation_activity)
        if uncertain_operation:
            self.status.text = "Outcome unknown"
        if view["phase"] not in {"running", "waiting"}:
            if view.get("error") and view["id"] not in self.seen_turn_errors:
                self.append(view["error"])
                self.seen_turn_errors.add(view["id"])
            self.active_turn = None
            self.pending = None
            if self.application.layout.has_focus(self.allow) or self.application.layout.has_focus(self.deny):
                self.application.layout.focus(self.composer)
            if (self.queued_settings or self.queued_messages) and not self.active_operation:
                self.schedule(self.after_turn())
        self.application.invalidate()

    def session_sync_available(self, *, allow_busy=False):
        return not (self.fixture or self.active_turn or self.active_operation or self.pending
                    or self.unresolved_operations or self.settings_modal or (self.busy and not allow_busy))

    async def sync_session_locked(self, *, allow_busy=False):
        while self.session_sync_available(allow_busy=allow_busy):
            page = await self.client.call("turn/list", {"threadId":self.session, "after":self.turn_cursor})
            if not self.session_sync_available(allow_busy=allow_busy):
                return
            for turn in page["turns"]:
                if turn["id"] not in self.seen_turn_prompts:
                    view = await self.client.call("turn/read", {"turnId":turn["id"]})
                    if not self.session_sync_available(allow_busy=allow_busy):
                        return
                    self.append(view["prompt"], "user")
                    self.seen_turn_prompts.add(view["id"])
                    self.active_turn = view["id"]
                    await self.project_turn(view)
                    if self.active_turn:
                        self.schedule(self.watch_turn())
                        self.turn_cursor = turn["ordinal"]
                        return
                self.turn_cursor = turn["ordinal"]
            if page["nextCursor"] is None:
                return

    async def watch_session(self):
        try:
            while True:
                async with self.session_sync_lock:
                    await self.reconcile_peer_pending()
                    await self.reconcile_peer_unknown()
                    await self.sync_session_locked()
                await asyncio.sleep(2)
        except Exception as error:
            self.status.text = "Disconnected"
            self.append(str(error))
            self.application.invalidate()

    async def reconcile_peer_pending(self):
        if (not self.pending or self.busy or self.settings_modal or self.active_turn
                or self.active_operation):
            return
        identifier = self.pending["id"]
        detail = await self.client.call("synth/operation/read", {"operationId":identifier})
        if (not self.pending or self.pending["id"] != identifier or self.busy or self.settings_modal
                or self.active_turn or self.active_operation):
            return
        operation = detail["operation"]
        if operation["status"] in {"awaiting_approval", "ready"}:
            if operation["status"] != self.pending["status"]:
                self.show_pending(operation)
            return
        self.pending = None
        self.decision = None
        if operation["status"] == "unknown":
            self.unresolved_operations.add(identifier)
        if self.application.layout.has_focus(self.allow) or self.application.layout.has_focus(self.deny):
            self.application.layout.focus(self.composer)
        if operation["status"] == "dispatched":
            self.active_operation = identifier
            self.schedule(self.watch_operation())
        elif identifier not in self.seen_operations:
            self.seen_operations.add(identifier)
            self.operation_result(operation)
        self.status.project({"phase":"running"}, operation, detail.get("activity"), show_preview=not self.details_visible())
        self.application.invalidate()

    async def reconcile_peer_unknown(self):
        if not self.unresolved_operations or self.busy or self.settings_modal or self.active_turn or self.active_operation:
            return
        for identifier in tuple(self.unresolved_operations):
            detail = await self.client.call("synth/operation/read", {"operationId":identifier})
            if self.busy or self.settings_modal or self.active_turn or self.active_operation:
                return
            operation = detail["operation"]
            if operation["status"] != "unknown":
                self.unresolved_operations.discard(identifier)
                if identifier not in self.seen_operations or operation["status"] == "reconciled":
                    self.seen_operations.add(identifier)
                    self.operation_result(operation)
        self.status.text = "Outcome unknown" if self.unresolved_operations else "Reconciled"
        self.application.invalidate()

    async def watch_turn(self):
        watched_turn = self.active_turn
        try:
            while watched_turn and self.active_turn == watched_turn:
                view = await self.client.call("turn/read", {"turnId":watched_turn})
                if self.active_turn != watched_turn:
                    return
                await self.project_turn(view)
                if view["phase"] not in {"running", "waiting"}:
                    return
                await asyncio.sleep(0.15)
        except Exception as error:
            if self.active_turn == watched_turn:
                self.status.text = "Disconnected"
                self.append(str(error))
                self.active_turn = None
                self.application.invalidate()

    async def interrupt(self):
        if not self.active_turn:
            return
        try:
            await self.client.call("turn/interrupt", {"turnId":self.active_turn})
            self.append("Interrupted · send a message to continue" + (
                f" · {len(self.queued_messages)} queued message(s) will be sent" if self.queued_messages else ""))
        except Exception as error:
            self.append(str(error))

    async def watch_operation(self):
        watched_operation = self.active_operation
        try:
            while watched_operation and self.active_operation == watched_operation:
                detail = await self.client.call("synth/operation/read", {"operationId":watched_operation})
                if self.active_operation != watched_operation:
                    return
                operation = detail["operation"]
                self.show_running_operation(operation)
                self.status.project({"phase": "running"}, operation, detail.get("activity"), show_preview=not self.details_visible())
                if operation["status"] == "ready":
                    self.active_operation = None
                    self.show_pending(operation)
                    self.application.invalidate()
                    return
                elif operation["status"] != "dispatched":
                    self.operation_result(operation)
                    self.active_operation = None
                    self.application.invalidate()
                    return
                self.application.invalidate()
                await asyncio.sleep(0.15)
        except Exception as error:
            if self.active_operation == watched_operation:
                self.active_operation = None
                self.status.text = "Disconnected"
                self.append(str(error))
                self.application.invalidate()

    async def respond(self, allow):
        if self.busy or not self.pending:
            return
        self.busy = True
        operation = self.pending
        try:
            if operation.get("status") == "ready":
                view = await self.client.call("synth/operation/run" if allow else "synth/operation/cancel", {
                    "operationId": operation["id"], "requestDigest": operation["request_digest"],
                })
                item = next(op for op in view["operations"] if op["id"] == operation["id"])
                self.status.text = item["status"]
                if item["status"] not in {"ready", "dispatched"}:
                    self.operation_result(item)
                self.pending = None
                self.allow.text, self.deny.text = "Allow", "Deny"
                if item["status"] in {"ready", "dispatched"}:
                    self.active_operation = item["id"]
                    self.schedule(self.watch_operation())
                self.application.layout.focus(self.composer)
                return
            if self.decision is None:
                self.decision = {
                    "mutationId": str(uuid4()), "operationId": operation["id"],
                    "requestDigest": operation["request_digest"], "allow": allow,
                }
            if self.decision["allow"] != allow:
                raise RuntimeError("Prior decision outcome unavailable")
            view = await self.client.call("synth/approval/respond", self.decision)
            item = next(op for op in view["operations"] if op["id"] == operation["id"])
            self.status.text = item["status"]
            if item["status"] not in {"ready", "dispatched"}:
                self.operation_result(item)
            self.pending = None
            if item["status"] in {"ready", "dispatched"} and not operation.get("turn"):
                self.active_operation = item["id"]
                self.schedule(self.watch_operation())
            self.application.layout.focus(self.composer)
        except Exception as error:
            self.status.text = "Outcome unavailable"
            self.append(str(error))
            if not self.client.poisoned:
                try:
                    current = (await self.client.call("synth/operation/read", {"operationId":operation["id"]}))["operation"]
                    if current["status"] == "ready":
                        self.show_pending(current)
                except Exception:
                    pass
        finally:
            self.busy = False
            self.application.invalidate()

    async def run(self):
        try:
            if not self.fixture:
                self.schedule(self.watch_session())
            await self.application.run_async()
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)


async def launch(args):
    if args.status or args.stop:
        import json
        from .daemon import address
        try:
            client = await Client.attach(address(args.store.resolve(), args.runtime_dir))
        except (FileNotFoundError, ConnectionRefusedError):
            print(json.dumps({"state":"not_running"}))
            return
        try:
            print(json.dumps(await client.call("synth/shutdown" if args.stop else "synth/status", {})))
        finally:
            await client.close()
        if args.stop:
            socket = address(args.store.resolve(), args.runtime_dir)
            for _ in range(200):
                if not socket.exists():
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Daemon is still draining")
        return
    if args.stdio or args.fixture:
        client = await Client.open(args.runtime.resolve(), args.store.resolve(), config=args.provider_config, env_file=args.env_file)
    else:
        from .daemon import connect
        client = await connect(args.runtime.resolve(), args.store.resolve(), config=args.provider_config, env_file=args.env_file, runtime_dir=args.runtime_dir)
    try:
        if args.list_sessions:
            after = None
            while True:
                page = await client.call("thread/list", {} if after is None else {"after": after})
                for item in page["sessions"]:
                    print(item["id"], display_text(item["workspace"]))
                after = page["nextCursor"]
                if after is None:
                    return
        if args.resume:
            session = args.resume
        else:
            result = await client.call("thread/start", {
                "mutationId": str(uuid4()), "workspace": str(args.workspace.resolve()),
            })
            session = result["result"]["id"]
        preview = Preview(client, session, fixture=args.fixture, no_color=args.no_color)
        await preview.restore()
        await preview.run()
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser(epilog="Account commands: login | auth status | account limits | logout")
    parser.add_argument("--runtime", required=True, type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--stdio", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fixture", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--resume")
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    diagnostic = parser.add_mutually_exclusive_group()
    diagnostic.add_argument("--status", action="store_true")
    diagnostic.add_argument("--stop", action="store_true")
    asyncio.run(launch(parser.parse_args()))


if __name__ == "__main__":
    main()

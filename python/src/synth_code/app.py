"""Local TUI projecting the durable Rust agent and explicit approvals."""
import argparse
import asyncio
import difflib
import os
import unicodedata
from collections import deque
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.completion import Completion, ConditionalCompleter, WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window, ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.containers import WindowAlign
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Box, Button, Dialog, Label, RadioList, TextArea
from prompt_toolkit.output import ColorDepth

from .client import Client
from .presentation import markdown

COMMANDS = {
    "/exit": ("", "Detach"), "/quit": ("", "Detach"),
    "/help": ("", "Commands"), "/clear": ("", "Clear view"),
    "/status": ("", "Session status"), "/stop": ("", "Interrupt task"),
    "/details": ("", "Toggle operation pane"),
    "/effort": ("LEVEL", "Reasoning effort"), "/provider": ("NAME", "Provider"),
    "/model": ("NAME", "Model"),
    "/permissions": ("MODE", "Permissions"),
    "/read": ("PATH", "Read file"), "/search": ("QUERY", "Search workspace"),
    "/edit": ("PATH", "Propose edit"), "/create": ("PATH", "Propose file"),
    "/output": ("UUID", "Command output"),
    "/resolve": ("UUID OBSERVATION", "Reconcile unknown outcome"),
}
LOCAL_COMMANDS = {"/exit", "/quit", "/help", "/clear", "/status", "/stop", "/details", "/effort", "/provider", "/model", "/permissions"}
COMPLETIONS = list(COMMANDS) + ["/effort " + level for level in ("low", "medium", "high", "xhigh", "max")] + ["/provider reflection", "/model beam"] + ["/permissions " + mode for mode in ("manual", "auto-edit", "yolo")]


def split_command(text):
    parts = text.strip().split(maxsplit=1)
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


class Preview:
    def __init__(self, client, session, *, input=None, output=None, fixture=True, no_color=False):
        self.client, self.session = client, session
        self.fixture = fixture
        self.active_turn = None
        self.active_operation = None
        self.seen_turn_messages = {}
        self.seen_operations = set()
        self.pending = None
        self.busy = False
        self.entries = deque()
        self.entry_chars = 0
        self.pane_enabled = True
        self._wide = False
        self.provider_summary = "Local fixture" if fixture else "Beam 1.0 · Reflection"
        self.credential_configured = None
        self.workspace_name = ""
        self.workspace_path = ""
        self.settings_modal = False
        self.settings_cancel = None
        self.permissions = "manual"
        self.transcript = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                   dont_extend_height=False)
        self.streaming = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                  height=Dimension(min=1, max=6), dont_extend_height=True,
                                  focusable=False)
        self.composer = TextArea(height=Dimension(min=1, max=6), multiline=True,
                                 dont_extend_height=True, accept_handler=self.submit,
                                 get_line_prefix=lambda line, wrap: "› " if line == 0 and wrap == 0 else "  ",
                                 completer=ConditionalCompleter(
                                     WordCompleter(COMPLETIONS, sentence=True,
                                                   meta_dict={key: value[1] for key, value in COMMANDS.items()}),
                                     Condition(lambda: self.composer.text.startswith("/"))),
                                 complete_while_typing=True,
                                 style="class:composer")
        self.status = Label("Ready")
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

        @keys.add("c-b")
        def toggle_details_(event):
            self.toggle_details()

        self.identity = Label("✦ Synth Code", style="class:brand", align=WindowAlign.CENTER)
        self.context = Label("", style="class:muted", align=WindowAlign.CENTER)
        self.detail_title = Label("", style="class:pane-title")
        self.details = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                focusable=True, style="class:details")
        welcome = ConditionalContainer(HSplit([
            Window(), self.identity, self.context, Window(),
        ]), filter=Condition(lambda: not self.transcript.text and not self.streaming.text))
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
        self.footer = Label(self.footer_text, style="class:footer", wrap_lines=False)
        self.main = HSplit([
            VSplit([Box(conversation, padding_top=1, padding_bottom=0,
                        padding_left=2, padding_right=2, width=Dimension(weight=1)), side]),
            Box(self.status, padding_top=0, padding_bottom=0, padding_left=2,
                padding_right=2, style="class:muted"),
            Box(self.composer, padding_top=1, padding_bottom=1, padding_left=2,
                padding_right=2, style="class:composer"),
            self.approvals,
            self.footer,
        ])
        self.main = FloatContainer(self.main, floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=8, scroll_offset=1)),
        ])
        monochrome = no_color or "NO_COLOR" in os.environ
        theme = {} if monochrome else {
            "": "bg:#24272e #d7d9df", "brand": "#d5a560 bold",
            "role": "#d7d9df bold", "muted": "#979baa",
            "detail": "#979baa", "separator": "#626977",
            "composer": "bg:#2c2c2b #eeece8", "footer": "bg:#292927 #aaa397",
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
        if not self.details_visible() and self.application.layout.has_focus(self.details):
            self.application.layout.focus(self.deny if self.pending else self.composer)

    def footer_text(self):
        return [("class:brand", "  Synth Code"), ("", "  ·  " + self.provider_summary
                + "  ·  " + self.permissions.upper() + "  ·  " + self.workspace_name)]

    def resize_layout(self, application):
        wide = self.details_visible()
        if self._wide != wide:
            self._wide = wide
            if not wide and application.layout.has_focus(self.details):
                application.layout.focus(self.deny if self.pending else self.composer)
            self.refresh_transcript()

    def refresh_transcript(self):
        # Bound the rendered document as well as its source entries. Table
        # padding must not amplify a bounded provider message without limit.
        blocks = []
        remaining = 100_000
        application = getattr(self, "application", None)
        columns = application.output.get_size().columns if application else 80
        width = max(1, (columns // 2 if self.details_visible() else columns) - 4)
        for text, detail, role in reversed(self.entries):
            if detail:
                if not self.details_visible():
                    text += "\n" + detail
                elif role != "tool-result":
                    text += "\n  … (+" + str(len(detail.splitlines())) + " lines)"
            if remaining <= 0:
                break
            lines = (markdown(text, width=width, max_chars=remaining) if role == "assistant" else
                     [[("", line)] for line in text[-remaining:].splitlines()])
            used = sum(len(value) for line in lines for _, value in line) + max(0, len(lines)-1)
            blocks.append(lines)
            remaining -= used + 2
        rendered = []
        for lines in reversed(blocks):
            if rendered:
                rendered.append([])
            rendered.extend(lines)
        # Lexer fragments and the plain document share exact character positions.
        self.transcript.lexer.rendered_lines = [line if any(style for style, _ in line) else None for line in rendered]
        self.transcript.text = "\n".join("".join(t for _, t in line) for line in rendered)
        self.transcript.buffer.cursor_position = len(self.transcript.text.rstrip())

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
        available = not self.busy and not self.pending and not self.active_turn and not self.active_operation
        if buffer.text.strip() and (command in LOCAL_COMMANDS or available):
            self.schedule(self.admit(buffer.text))
            return False
        return True

    async def local_command(self, command, argument):
        if command == "/permissions":
            await self.permissions_command(argument)
            return
        if command in {"/effort", "/provider", "/model"}:
            await self.provider_command(command[1:], argument)
            return
        if argument:
            self.append("Usage · " + command)
            return
        if command in {"/exit", "/quit"}:
            # Same detach semantics as Ctrl+Q; the daemon retains pending work.
            self.application.exit()
        elif command == "/help":
            self.append("Commands\n" + "\n".join(
                f"{(name + (' ' + syntax if syntax else '')):28} {label}"
                for name, (syntax, label) in COMMANDS.items()))
        elif command == "/clear":
            if self.pending:
                self.append("Clear unavailable · approval pending")
                return
            self.entries.clear()
            self.entry_chars = 0
            self.streaming.text = ""
            self.details.text = ""
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
        self.context.text = self.workspace_name + "  ·  " + self.provider_summary

    async def provider_command(self, field, argument):
        if self.busy or self.pending or self.active_turn or self.active_operation:
            self.append("Settings unavailable · task active or approval pending")
            return
        status = await self.client.call("synth/provider/status", {"threadId": self.session})
        self.show_provider(status)
        if "choices" not in status or "revision" not in status:
            self.append("Settings unavailable · runtime update required")
            return
        if self.busy or self.pending or self.active_turn or self.active_operation or self.settings_modal:
            self.append("Settings unavailable · task active or menu open")
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
        if self.busy or self.pending or self.active_turn or self.active_operation or self.settings_modal:
            self.append("Permissions unavailable · task active or menu open")
            return
        status = await self.client.call("synth/permissions/status", {"threadId": self.session})
        self.permissions = status["mode"]
        # A peer or watcher can admit work while the status read is in flight.
        if self.busy or self.pending or self.active_turn or self.active_operation or self.settings_modal:
            self.append("Permissions unavailable · task active or menu open")
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
        if self.busy or self.pending or self.active_turn or self.active_operation:
            self.append("Permissions unavailable · task active or approval pending")
            return
        self.busy = True
        try:
            result = await self.client.call("synth/permissions/configure", {
                "mutationId": str(uuid4()), "threadId": self.session,
                "mode": value, "expectedRevision": status["revision"],
            })
            self.permissions = result["status"]["mode"]
            self.append("Permissions · " + self.permissions)
        except Exception as error:
            self.append(str(error))
            if not self.client.poisoned:
                self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        finally:
            self.busy = False
            self.application.invalidate()

    async def configure_provider(self, field, value, status):
        if self.busy or self.pending or self.active_turn or self.active_operation:
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
            self.append(field.title() + " · " + result["status"][field])
        except Exception as error:
            self.append(str(error))
            if not self.client.poisoned:
                self.show_provider(await self.client.call("synth/provider/status", {"threadId": self.session}))
        finally:
            self.busy = False
            self.application.invalidate()

    def append(self, text, role=None, *, detail=None, detail_title="Operation"):
        # Escape provider/file text before it reaches any terminal widget.
        prefix = "› " if role == "user" else ""
        text = prefix + display_text(text)
        detail = display_text(detail) if detail is not None else None
        text = text[-100_000:]
        detail = detail[-(100_000-len(text)):] if detail and len(text) < 100_000 else None
        if not text and not detail:
            return
        self.entries.append((text, detail, role))
        self.entry_chars += len(text) + len(detail or "") + 2
        while len(self.entries) > 1 and (self.entry_chars > 100_000 or len(self.entries) > 1000):
            old_text, old_detail, _ = self.entries.popleft()
            self.entry_chars -= len(old_text) + len(old_detail or "") + 2
        if detail is not None:
            self.detail_title.text = display_text(detail_title)
            self.details.text = detail[-100_000:]
            self.details.buffer.cursor_position = 0
        self.refresh_transcript()

    def operation_result(self, operation):
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
        self.append(summary, "tool-result", detail=detail,
                    detail_title=("Changes" if operation.get("edit") else "Command output") + " · " + operation["status"])
        for field in ("error", "reason"):
            if outcome.get(field):
                self.append(str(outcome[field]) + "\nOperation · " + operation["id"])

    async def admit(self, text):
        command, argument = split_command(text)
        if command in LOCAL_COMMANDS:
            try:
                await self.local_command(command, argument)
            except Exception as error:
                self.append(str(error))
            self.application.invalidate()
            return
        if self.busy or self.pending:
            return
        self.busy = True
        try:
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
                self.status.text = "Output"
                return
            if command == "/resolve":
                identifier, _, observation = argument.partition(" ")
                op = (await self.client.call("synth/operation/read", {"operationId":identifier}))["operation"]
                await self.client.call("synth/operation/reconcile", {"mutationId":str(uuid4()), "operationId":identifier,
                    "requestDigest":op["request_digest"], "observation":observation})
                self.append("Reconciled · " + identifier)
                self.status.text = "Ready"
                return
            if command == "/read":
                file = await self.client.call("workspace/read", {"threadId": self.session, "path": argument})
                self.append(file["path"] + "\n" + file["text"])
                self.status.text = "Read"
                return
            if command == "/search":
                result = await self.client.call("workspace/search", {"threadId": self.session, "query": argument})
                self.append("\n".join(f"{m['path']}:{m['line']} {m['text']}" for m in result["matches"]))
                self.status.text = f"{len(result['matches'])} matches" + (" · truncated" if result["truncated"] else "")
                return
            if command in {"/edit", "/create"}:
                before = await self.client.call("workspace/read", {"threadId": self.session, "path": argument}) if command == "/edit" else None
                self.edit_dialog(argument, before)
                return
            if command.startswith("/"):
                raise ValueError("Unknown command · " + command + " · Commands: /help")
            if not self.fixture:
                result = await self.client.call("turn/start", {"mutationId":str(uuid4()), "threadId":self.session, "prompt":text, "expectedPermissions":self.permissions})
                self.append(text, "user")
                self.active_turn = result["id"]
                self.permissions = result["permissions"]
                if result.get("selection"):
                    self.show_provider({**result["selection"], "credentialConfigured": self.credential_configured})
                self.status.text = result["phase"]
                self.schedule(self.watch_turn())
                return
            result = await self.client.call("fixture/admit", {
                "mutationId": str(uuid4()), "threadId": self.session, "text": text,
            })
            self.append(text, "user")
            self.show_pending(result["result"])
        except Exception as error:
            self.status.text = "Error"
            self.append(str(error))
            if not self.client.poisoned and "session_permissions_changed" in str(error):
                self.permissions = (await self.client.call("synth/permissions/status", {"threadId": self.session}))["mode"]
        finally:
            self.busy = False
            self.application.invalidate()

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

    def edit_dialog(self, path, before):
        editor = TextArea(text=before["text"] if before else "", multiline=True, scrollbar=True)
        def cancel():
            self.application.layout.container = self.main
            self.application.layout.focus(self.composer)
        def propose():
            content = editor.text
            cancel()
            self.schedule(self.propose_edit(path, before, content))
        dialog = Dialog(title=display_text(path), body=editor, buttons=[Button("Propose", handler=propose), Button("Cancel", handler=cancel)])
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
        self.context.text = display_text(Path(workspace).name or workspace) + "  ·  " + self.provider_summary
        operations = list(view["operations"])
        while view.get("nextCursor") is not None:
            view = await self.client.call("thread/read", {"threadId": self.session, "after": view["nextCursor"], "atSequence": view["sequence"]})
            operations.extend(view["operations"])
        for operation in operations:
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
                    await self.render_turn(view)
                    if view["phase"] in {"running", "waiting"}:
                        self.active_turn = view["id"]
                after = page["nextCursor"]
                if after is None:
                    break
            if self.active_turn:
                self.schedule(self.watch_turn())

    async def render_turn(self, view):
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
                    self.append("".join(chunks), "assistant")
                else:
                    self.append(message["text"], "assistant")
        self.seen_turn_messages[view["id"]] = max([m["index"]+1 for m in view["messages"]], default=seen)
        self.status.text = ("Approved · awaiting execution" if self.pending.get("status")=="ready" else "Awaiting approval") if self.pending else view["phase"]
        # Provisional output has its own view; only durable messages enter history.
        live = view.get("liveText") or ""
        self.streaming.text = "● " + display_text(live) if live else ""
        self.streaming.buffer.cursor_position = len(self.streaming.text)

    async def watch_turn(self):
        try:
            while self.active_turn:
                view = await self.client.call("turn/read", {"turnId":self.active_turn})
                for identifier in view["operations"]:
                    detail = await self.client.call("synth/operation/read", {"operationId":identifier})
                    operation = detail["operation"]
                    if operation["status"] == "awaiting_approval" and not self.pending:
                        self.show_pending(operation)
                    elif operation["status"] in {"succeeded", "failed", "unknown", "denied", "canceled"} and identifier not in self.seen_operations:
                        self.seen_operations.add(identifier)
                        self.operation_result(operation)
                await self.render_turn(view)
                if view["phase"] not in {"running", "waiting"}:
                    if view.get("error"):
                        self.append(view["error"])
                    self.active_turn = None
                    self.pending = None
                    self.application.layout.focus(self.composer)
                self.application.invalidate()
                await asyncio.sleep(0.15)
        except Exception as error:
            self.status.text = "Disconnected"
            self.append(str(error))
            self.active_turn = None
            self.application.invalidate()

    async def interrupt(self):
        try:
            await self.client.call("turn/interrupt", {"turnId":self.active_turn})
        except Exception as error:
            self.append(str(error))

    async def watch_operation(self):
        try:
            while self.active_operation:
                operation = (await self.client.call("synth/operation/read", {"operationId":self.active_operation}))["operation"]
                self.status.text = operation["status"]
                if operation["status"] == "ready":
                    self.active_operation = None
                    self.show_pending(operation)
                elif operation["status"] != "dispatched":
                    self.operation_result(operation)
                    self.active_operation = None
                self.application.invalidate()
                await asyncio.sleep(0.15)
        except Exception as error:
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

"""Local TUI projecting the durable Rust agent and explicit approvals."""
import argparse
import asyncio
import difflib
import os
import unicodedata
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window, ConditionalContainer
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Button, Dialog, Label, TextArea
from prompt_toolkit.output import ColorDepth

from .client import Client

def display_text(text):
    """Neutralize terminal and bidi controls; preserve ordinary text and layout."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}" for c in text)


class ConversationLexer(Lexer):
    """Style plain, already escaped text without interpreting markup or ANSI."""
    def lex_document(self, document):
        def line(index):
            text = document.lines[index]
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
        self.transcript = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                   dont_extend_height=True)
        self.streaming = TextArea(text="", read_only=True, lexer=ConversationLexer(),
                                  height=Dimension(min=1, max=6), dont_extend_height=True,
                                  focusable=False)
        self.composer = TextArea(height=Dimension(min=1, max=6), multiline=True,
                                 dont_extend_height=True, accept_handler=self.submit,
                                 get_line_prefix=lambda line, wrap: "› " if line == 0 and wrap == 0 else "  ")
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

        @keys.add("escape", "enter", filter=has_focus(self.composer))
        def newline_(event):
            event.current_buffer.insert_text("\n")

        @keys.add("enter", filter=has_focus(self.composer))
        def send_(event):
            event.current_buffer.validate_and_handle()

        @keys.add("tab")
        def next_(event):
            event.app.layout.focus_next()

        @keys.add("s-tab")
        def previous_(event):
            event.app.layout.focus_previous()

        self.identity = Label([("class:brand", "Synth Code"), ("", "  ·  Beam 1.0")])
        self.context = Label("", style="class:detail")
        self.approvals = ConditionalContainer(
            VSplit([self.allow, self.deny], padding=1),
            filter=Condition(lambda: self.pending is not None),
        )
        self.main = HSplit([
            self.identity, self.context, Window(height=1),
            ConditionalContainer(self.transcript, filter=Condition(lambda: bool(self.transcript.text))),
            ConditionalContainer(self.streaming, filter=Condition(lambda: bool(self.streaming.text) and not self.pending)),
            Window(height=1, char="─", style="class:separator"),
            self.composer,
            Window(height=1, char="─", style="class:separator"),
            self.status, self.approvals,
        ])
        monochrome = no_color or "NO_COLOR" in os.environ
        self.application = Application(
            layout=Layout(self.main, focused_element=self.composer),
            key_bindings=keys, full_screen=False, mouse_support=True,
            input=input, output=output,
            style=Style.from_dict({
                "brand": "bold", "role": "bold", "detail": "italic",
                "separator": "", "button.focused": "reverse bold",
                **({} if monochrome else {"added": "ansigreen", "removed": "ansired"}),
            }),
            color_depth=ColorDepth.DEPTH_1_BIT if monochrome else None,
        )

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
        if not self.busy and not self.pending and not self.active_turn and not self.active_operation and buffer.text.strip():
            self.schedule(self.admit(buffer.text))
            return False
        return True

    def append(self, text, role=None):
        # Plain TextArea content is rendered as text, never as terminal commands.
        prefix = {"user": "› ", "assistant": "● "}.get(role, "")
        self.transcript.text = (self.transcript.text + prefix + display_text(text) + "\n\n")[-100_000:]
        self.transcript.buffer.cursor_position = len(self.transcript.text.rstrip())

    def operation_result(self, operation):
        outcome = operation.get("outcome") or {}
        summary = "Action · " + operation["status"]
        if operation.get("edit"):
            summary += " · " + operation["edit"]["path"]
        if "exit_code" in outcome:
            summary += " · exit " + str(outcome["exit_code"])
        for field, label in (("timed_out", "timeout"), ("truncated", "output truncated"), ("gateway_lost", "gateway lost")):
            if outcome.get(field):
                summary += " · " + label
        self.append(summary + "\nOperation · " + operation["id"])
        for field in ("error", "reason"):
            if outcome.get(field):
                self.append(str(outcome[field]))
        for stream in ("stdout", "stderr"):
            if outcome.get(stream):
                self.append(stream + "\n" + outcome[stream])

    async def admit(self, text):
        if self.busy or self.pending:
            return
        self.busy = True
        try:
            command, _, argument = text.partition(" ")
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
                raise ValueError("Unknown command")
            if not self.fixture:
                result = await self.client.call("turn/start", {"mutationId":str(uuid4()), "threadId":self.session, "prompt":text})
                self.append(text, "user")
                self.active_turn = result["id"]
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
            self.append("Edit · " + edit["path"] + "\n" + diff)
        elif operation.get("shell"):
            shell = operation["shell"]
            self.append("Command · " + shell["command"] + f"\nTimeout · {shell['timeout_seconds']}s")
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
            })
            self.show_pending(result["result"])
        except Exception as error:
            self.status.text = "Error"
            self.append(str(error))
        finally:
            self.busy = False
            self.application.invalidate()

    async def restore(self):
        if not self.fixture:
            provider = await self.client.call("synth/provider/status", {})
            if not provider["credentialConfigured"]:
                self.identity.text = [("class:brand", "Synth Code"), ("", "  ·  Beam 1.0  ·  Reflection missing")]
        view = await self.client.call("thread/read", {"threadId": self.session})
        if not view["sessions"]:
            raise ValueError("Session not found")
        workspace = view["sessions"][0]["workspace"]
        self.context.text = display_text(Path(workspace).name or workspace)
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
                else:
                    self.append("Action · " + item["status"])
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
            else:
                self.append("Action · " + item["status"])
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

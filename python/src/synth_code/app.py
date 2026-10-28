"""Local TUI projecting the durable Rust agent and explicit approvals."""
import argparse
import asyncio
import difflib
import unicodedata
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import has_focus
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.widgets import Button, Dialog, Frame, Label, TextArea

from .client import Client

def display_text(text):
    """Neutralize terminal and bidi controls; preserve ordinary text and layout."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}" for c in text)


class Preview:
    def __init__(self, client, session, *, input=None, output=None, fixture=True):
        self.client, self.session = client, session
        self.fixture = fixture
        self.active_turn = None
        self.seen_turn_messages = {}
        self.seen_operations = set()
        self.pending = None
        self.busy = False
        self.transcript = TextArea(text="", read_only=True, scrollbar=True)
        self.composer = TextArea(height=3, multiline=False, accept_handler=self.submit)
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
            else:
                event.app.exit()

        @keys.add("escape", "enter", filter=has_focus(self.composer))
        def newline_(event):
            event.current_buffer.insert_text("\n")

        @keys.add("tab")
        def next_(event):
            event.app.layout.focus_next()

        @keys.add("s-tab")
        def previous_(event):
            event.app.layout.focus_previous()

        self.identity = Label("Synth Code")
        self.main = HSplit([
                self.identity,
                Frame(self.transcript, "Session"), self.status,
                VSplit([self.allow, self.deny]), Frame(self.composer, "Task"),
            ])
        self.application = Application(
            layout=Layout(self.main, focused_element=self.composer),
            key_bindings=keys, full_screen=True, mouse_support=True,
            input=input, output=output,
        )

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def submit(self, buffer):
        if not self.busy and not self.pending and not self.active_turn and buffer.text.strip():
            self.schedule(self.admit(buffer.text))
            return False
        return True

    def append(self, text):
        # Plain TextArea content is rendered as text, never as terminal commands.
        self.transcript.text = (self.transcript.text + display_text(text) + "\n")[-100_000:]
        self.transcript.buffer.cursor_position = len(self.transcript.text)

    async def admit(self, text):
        if self.busy or self.pending:
            return
        self.busy = True
        try:
            command, _, argument = text.partition(" ")
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
                self.append(text)
                self.active_turn = result["id"]
                self.status.text = result["phase"]
                self.schedule(self.watch_turn())
                return
            result = await self.client.call("fixture/admit", {
                "mutationId": str(uuid4()), "threadId": self.session, "text": text,
            })
            self.append(text)
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
        self.status.text = "Awaiting approval"
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
            self.identity.text = "Synth Code · Beam 1.0 · Reflection " + ("configured" if provider["credentialConfigured"] else "missing")
        view = await self.client.call("thread/read", {"threadId": self.session})
        if not view["sessions"]:
            raise ValueError("Session not found")
        self.append("Session · " + self.session)
        operations = list(view["operations"])
        while view.get("nextCursor") is not None:
            view = await self.client.call("thread/read", {"threadId": self.session, "after": view["nextCursor"], "atSequence": view["sequence"]})
            operations.extend(view["operations"])
        for operation in operations:
            self.append(operation["text"] + " · " + operation["status"])
            if operation["status"] == "awaiting_approval":
                self.show_pending(operation)
            elif operation["status"] in {"unknown", "ready", "dispatched"}:
                self.status.text = operation["status"]

        if not self.fixture:
            after = 0
            while True:
                page = await self.client.call("turn/list", {"threadId": self.session, "after":after})
                for turn in page["turns"]:
                    view = await self.client.call("turn/read", {"turnId":turn["id"]})
                    self.append(view["prompt"])
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
                    self.append("".join(chunks))
                else:
                    self.append(message["text"])
        self.seen_turn_messages[view["id"]] = max([m["index"]+1 for m in view["messages"]], default=seen)
        self.status.text = "Awaiting approval" if self.pending else view["phase"]
        # Streamed text is provisional until its durable completion arrives.
        if view.get("liveText"):
            self.status.text = display_text(view["liveText"][-300:]).replace("\n", " ")

    async def watch_turn(self):
        try:
            while self.active_turn:
                view = await self.client.call("turn/read", {"turnId":self.active_turn})
                await self.render_turn(view)
                for identifier in view["operations"]:
                    detail = await self.client.call("synth/operation/read", {"operationId":identifier})
                    operation = detail["operation"]
                    if operation["status"] == "awaiting_approval" and not self.pending:
                        self.show_pending(operation)
                    elif operation["status"] in {"succeeded", "failed", "unknown", "denied", "canceled"} and identifier not in self.seen_operations:
                        self.seen_operations.add(identifier)
                        self.append(operation["status"] + (" · " + str(operation["outcome"]) if operation.get("outcome") else ""))
                if view["phase"] not in {"running", "waiting"}:
                    if view.get("error"):
                        self.append(view["error"])
                    self.active_turn = None
                    self.pending = None
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

    async def respond(self, allow):
        if self.busy or not self.pending:
            return
        self.busy = True
        operation = self.pending
        try:
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
            self.append(item["status"])
            self.pending = None
            self.application.layout.focus(self.composer)
        except Exception as error:
            self.status.text = "Outcome unavailable"
            self.append(str(error))
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
        return
    if args.stdio or args.fixture:
        client = await Client.open(args.runtime.resolve(), args.store.resolve())
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
        preview = Preview(client, session, fixture=args.fixture)
        await preview.restore()
        await preview.run()
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--stdio", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    parser.add_argument("--provider-config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--resume")
    parser.add_argument("--list-sessions", action="store_true")
    diagnostic = parser.add_mutually_exclusive_group()
    diagnostic.add_argument("--status", action="store_true")
    diagnostic.add_argument("--stop", action="store_true")
    asyncio.run(launch(parser.parse_args()))


if __name__ == "__main__":
    main()

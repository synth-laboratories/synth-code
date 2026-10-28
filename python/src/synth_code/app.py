"""Offline fixture UI; provider, shell and detachable daemon are later slices."""
import argparse
import asyncio
import difflib
import unicodedata
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.widgets import Button, Dialog, Frame, Label, TextArea

from .client import Client

def display_text(text):
    """Neutralize terminal and bidi controls; preserve ordinary text and layout."""
    return "".join(c if c in "\n\t" or not unicodedata.category(c).startswith("C") else f"\\u{ord(c):04x}" for c in text)


class Preview:
    def __init__(self, client, session, *, input=None, output=None):
        self.client, self.session = client, session
        self.pending = None
        self.busy = False
        self.transcript = TextArea(text="", read_only=True, scrollbar=True)
        self.composer = TextArea(height=3, multiline=False, accept_handler=self.submit)
        self.status = Label("Offline workspace")
        self.tasks = set()
        self.decision = None
        self.allow = Button("Allow", handler=lambda: self.schedule(self.respond(True)))
        self.deny = Button("Deny", handler=lambda: self.schedule(self.respond(False)))
        keys = KeyBindings()

        @keys.add("c-q")
        @keys.add("c-c")
        def quit_(event):
            event.app.exit()

        @keys.add("tab")
        def next_(event):
            event.app.layout.focus_next()

        @keys.add("s-tab")
        def previous_(event):
            event.app.layout.focus_previous()

        self.main = HSplit([
                Label("Synth Code · foundation preview"),
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
        if not self.busy and not self.pending and buffer.text.strip():
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
    client = await Client.open(args.runtime.resolve(), args.store.resolve())
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
        preview = Preview(client, session)
        await preview.restore()
        await preview.run()
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--resume")
    parser.add_argument("--list-sessions", action="store_true")
    asyncio.run(launch(parser.parse_args()))


if __name__ == "__main__":
    main()

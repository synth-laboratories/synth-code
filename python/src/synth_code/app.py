"""Offline fixture UI; provider, shell and detachable daemon are later slices."""
import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from prompt_toolkit import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit
from prompt_toolkit.widgets import Button, Frame, Label, TextArea

from .client import Client


class Preview:
    def __init__(self, client, session, *, input=None, output=None):
        self.client, self.session = client, session
        self.pending = None
        self.busy = False
        self.transcript = TextArea(text="", read_only=True, scrollbar=True)
        self.composer = TextArea(height=3, multiline=False, accept_handler=self.submit)
        self.status = Label("Offline fixture")
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

        self.application = Application(
            layout=Layout(HSplit([
                Label("Synth Code · foundation preview"),
                Frame(self.transcript, "Session"), self.status,
                VSplit([self.allow, self.deny]), Frame(self.composer, "Fixture text"),
            ]), focused_element=self.composer),
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
        self.transcript.text += text + "\n"
        self.transcript.buffer.cursor_position = len(self.transcript.text)

    async def admit(self, text):
        if self.busy or self.pending:
            return
        self.busy = True
        try:
            result = await self.client.call("fixture/admit", {
                "mutationId": str(uuid4()), "threadId": self.session, "text": text,
            })
            self.pending = result["result"]
            self.decision = None
            self.append(text)
            self.append("Awaiting approval · fixture-" + self.pending["id"] + ".txt")
            self.status.text = "Awaiting approval"
            self.application.layout.focus(self.deny)
        except Exception as error:
            self.status.text = "Error"
            self.append(str(error))
        finally:
            self.busy = False
            self.application.invalidate()

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
        session = await client.call("thread/start", {
            "mutationId": str(uuid4()), "workspace": str(args.workspace.resolve()),
        })
        await Preview(client, session["result"]["id"]).run()
    finally:
        await client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    asyncio.run(launch(parser.parse_args()))


if __name__ == "__main__":
    main()

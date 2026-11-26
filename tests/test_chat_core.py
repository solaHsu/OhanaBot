import asyncio
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from bot.core import ChatCore, DispatchError, Forward, SQLiteStore


class ChatCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiuser_membership_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.sqlite3"
            with closing(SQLiteStore(path)) as store:
                core = ChatCore(store)
                conversation = core.open(["alice", "bob", "alice"])
                self.assertEqual(conversation.participants, ("alice", "bob"))
                first = core.post(conversation.id, "alice", "team update")

                async def echo(turn):
                    self.assertEqual(turn.history[0], first)
                    return turn.message.text.upper()

                core.register("echo", echo)
                with self.assertRaises(DispatchError):
                    await core.submit(conversation.id, "eve", "echo", "private")
                reply = await core.submit(conversation.id, "bob", "echo", "received")

            with closing(SQLiteStore(path)) as store:
                core = ChatCore(store)
                with self.assertRaises(DispatchError):
                    core.history(conversation.id, "eve")
                history = core.history(conversation.id, "alice")
                self.assertEqual([message.sender for message in history], ["alice", "bob", "echo"])
                self.assertEqual(history[-1], reply)
                self.assertEqual(reply.in_reply_to, history[-2].id)

    async def test_nested_parallel_dispatch_isolated_and_partial_failure(self):
        with closing(SQLiteStore()) as store:
            core = ChatCore(store, max_depth=2)
            parent = core.open(["alice", "bob"])
            sibling_started = asyncio.Event()
            nested_ids = []

            async def leaf(turn):
                too_deep, = await turn.forward(Forward("leaf", "another level"))
                self.assertIn("Maximum conversation depth", too_deep.error)
                return "nested result"

            async def branch(turn):
                await asyncio.wait_for(sibling_started.wait(), timeout=1)
                self.assertEqual([message.text for message in turn.history], ["shared excerpt"])
                self.assertEqual(turn.conversation.participants, ("alice",))
                self.assertEqual(turn.conversation.parent_id, parent.id)
                nested, = await turn.forward(Forward("leaf", "specific question"))
                self.assertEqual(store.get(nested.conversation_id).parent_message_id, turn.message.id)
                nested_ids.append(nested.conversation_id)
                return nested.reply.text

            async def unavailable(turn):
                sibling_started.set()
                raise DispatchError("source unavailable")

            async def cancelled(turn):
                raise asyncio.CancelledError

            async def root(turn):
                results = await turn.forward(
                    Forward("branch", "shared excerpt"), Forward("unavailable", "separate request"),
                    Forward("cancelled", "try this too"),
                )
                self.assertEqual([result.target for result in results], ["branch", "unavailable", "cancelled"])
                self.assertEqual(results[1].error, "source unavailable")
                self.assertEqual(results[2].error, "Handler cancelled")
                child = store.get(results[0].conversation_id)
                self.assertEqual(child.parent_message_id, turn.message.id)
                with self.assertRaises(DispatchError):
                    core.history(child.id, "bob")
                return results[0].reply.text

            core.register("root", root)
            core.register("branch", branch)
            core.register("leaf", leaf)
            core.register("unavailable", unavailable)
            core.register("cancelled", cancelled)
            core.post(parent.id, "bob", "parent history")
            result = await core.submit(parent.id, "alice", "root", "parent question")
            self.assertEqual(result.text, "nested result")
            self.assertEqual(len(core.history(parent.id, "alice")), 3)
            resumed = await core.submit(nested_ids[0], "alice", "leaf", "follow-up")
            self.assertEqual(resumed.text, "nested result")

    async def test_cancelling_parent_propagates_to_children(self):
        with closing(SQLiteStore()) as store:
            core = ChatCore(store)
            parent = core.open(["alice"])
            started = asyncio.Event()
            stopped = asyncio.Event()

            async def child(turn):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

            async def root(turn):
                await turn.forward(Forward("child", "work"))
                return "unreachable"

            core.register("root", root)
            core.register("child", child)
            task = asyncio.create_task(core.submit(parent.id, "alice", "root", "start"))
            await asyncio.wait_for(started.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(stopped.is_set())

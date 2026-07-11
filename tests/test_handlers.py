"""Tests for the NO_REPLY sentinel handling in handlers.py's shared turn lifecycle
(`_run_and_deliver`): a scheduled firing (`scheduled=True`) whose reply is exactly NO_REPLY
sends nothing and deletes its progress bubble; every other case — including NO_REPLY on a real
user turn — behaves exactly as it did before this feature. `run_turn` and `send_message` are
mocked (no network, no claude subprocess, no bot token); `_clear_outbox`/`_flush_outbox` run
for real against a tempdir since they're pure filesystem I/O."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent import handlers
from agent.config import Settings


def _settings(home: Path) -> Settings:
    settings = Settings(
        token="t", owner_id=1, agent_name="Agent", claude_bin="claude",
        turn_timeout=1800, allowed_groups=frozenset(), home=home,
    )
    settings.run_dir.mkdir(exist_ok=True)
    settings.outbox_dir.mkdir(exist_ok=True)
    return settings


def _context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(delete_message=AsyncMock()))


class RunAndDeliverNoReply(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.context = _context()

    def tearDown(self):
        self._tmp.cleanup()

    async def test_scheduled_no_reply_suppresses_message_and_deletes_bubble(self):
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("NO_REPLY", 555))), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertTrue(ok)
        send_message.assert_not_called()
        self.context.bot.delete_message.assert_awaited_once_with(chat_id=42, message_id=555)

    async def test_scheduled_normal_reply_sends_and_never_deletes(self):
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("all clear", 555))), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertTrue(ok)
        send_message.assert_called_once_with("t", 42, "all clear")
        self.context.bot.delete_message.assert_not_awaited()

    async def test_user_turn_ignores_sentinel_and_sends_it_verbatim(self):
        # NO_REPLY is only special on the scheduled path (scheduled=True). A real user turn
        # (the default) must behave EXACTLY as it did before this feature — always send whatever
        # claude replied, even if that happens to be the literal sentinel.
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("NO_REPLY", 555))), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings)
        self.assertTrue(ok)
        send_message.assert_called_once_with("t", 42, "NO_REPLY")
        self.context.bot.delete_message.assert_not_awaited()

    async def test_no_bubble_message_id_skips_delete_call(self):
        # A turn with zero tool calls never sends a bubble — message_id is None; deleting
        # "nothing" must be a no-op, not an error.
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("NO_REPLY", None))), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertTrue(ok)
        send_message.assert_not_called()
        self.context.bot.delete_message.assert_not_awaited()

    async def test_bubble_delete_failure_is_swallowed_turn_still_ok(self):
        self.context.bot.delete_message.side_effect = Exception("message to delete not found")
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("NO_REPLY", 555))), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertTrue(ok)  # the swallowed delete failure must not flip the turn to "failed"
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()

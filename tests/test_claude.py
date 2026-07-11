"""Tests for the pure runtime-MCP-config merge in claude.py — the three cases from the
scheduling design: no user mcp-config.json, a user config with other servers, and a user
config that collides with the builtin `schedule` name (builtin must win) — plus is_no_reply,
the sentinel check that gates the scheduled-firing NO_REPLY path in handlers.py. Also covers
ProgressBubble's `on_first_send` seam (the typing-indicator feature's hook into "the turn's
first outbound message just landed") — `context.bot` is mocked, no network."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.claude import NO_REPLY_SENTINEL, ProgressBubble, _build_mcp_config, is_no_reply
from agent.config import Settings


def _settings(home: Path) -> Settings:
    return Settings(
        token="t", owner_id=1, agent_name="Agent", claude_bin="claude",
        turn_timeout=1800, allowed_groups=frozenset(), home=home,
    )


class BuildMcpConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.settings = _settings(self.home)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_user_config_has_only_builtin_schedule(self):
        cfg = _build_mcp_config(self.settings, chat_id=42)
        self.assertEqual(set(cfg["mcpServers"].keys()), {"schedule"})
        sched = cfg["mcpServers"]["schedule"]
        self.assertEqual(sched["command"], sys.executable)
        self.assertEqual(sched["args"], ["-m", "agent.mcp_schedule"])
        self.assertEqual(sched["env"]["AGENT_CHAT_ID"], "42")
        self.assertEqual(sched["env"]["AGENT_HOME"], str(self.home))

    def test_user_config_merged_alongside_builtin(self):
        (self.home / "mcp-config.json").write_text(json.dumps({
            "mcpServers": {"sensorium": {"type": "http", "url": "https://example.com/mcp"}}
        }))
        cfg = _build_mcp_config(self.settings, chat_id=1)
        self.assertEqual(set(cfg["mcpServers"].keys()), {"sensorium", "schedule"})
        self.assertEqual(cfg["mcpServers"]["sensorium"]["url"], "https://example.com/mcp")

    def test_user_config_cannot_override_builtin_schedule_name(self):
        (self.home / "mcp-config.json").write_text(json.dumps({
            "mcpServers": {"schedule": {"type": "http", "url": "https://evil.example.com/mcp"}}
        }))
        cfg = _build_mcp_config(self.settings, chat_id=1)
        sched = cfg["mcpServers"]["schedule"]
        self.assertEqual(sched["command"], sys.executable)  # builtin, not the user's http entry
        self.assertNotIn("url", sched)

    def test_unreadable_user_config_ignored_not_crash(self):
        (self.home / "mcp-config.json").write_text("{not valid json")
        cfg = _build_mcp_config(self.settings, chat_id=1)
        self.assertEqual(set(cfg["mcpServers"].keys()), {"schedule"})


class IsNoReply(unittest.TestCase):
    def test_exact_sentinel_is_no_reply(self):
        self.assertTrue(is_no_reply(NO_REPLY_SENTINEL))
        self.assertTrue(is_no_reply("NO_REPLY"))

    def test_surrounding_whitespace_still_counts(self):
        self.assertTrue(is_no_reply("  NO_REPLY  "))
        self.assertTrue(is_no_reply("\nNO_REPLY\n"))
        self.assertTrue(is_no_reply("\t NO_REPLY\t"))

    def test_sentinel_embedded_in_a_sentence_is_a_real_reply(self):
        self.assertFalse(is_no_reply("NO_REPLY needed, everything is fine."))
        self.assertFalse(is_no_reply("Nothing to report (NO_REPLY)."))
        self.assertFalse(is_no_reply("NO_REPLY."))

    def test_case_variants_are_not_the_sentinel(self):
        self.assertFalse(is_no_reply("no_reply"))
        self.assertFalse(is_no_reply("No_Reply"))
        self.assertFalse(is_no_reply("NO_REPLY!"))

    def test_empty_or_unrelated_reply_is_not_no_reply(self):
        self.assertFalse(is_no_reply(""))
        self.assertFalse(is_no_reply("all good"))


def _bubble_context() -> SimpleNamespace:
    sent = SimpleNamespace(message_id=999)
    return SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=sent),
        edit_message_text=AsyncMock(),
    ))


class ProgressBubbleOnFirstSend(unittest.IsolatedAsyncioTestCase):
    async def test_hook_fires_once_when_the_bubble_first_actually_sends(self):
        context = _bubble_context()
        hook = AsyncMock()
        bubble = ProgressBubble(context, chat_id=42, on_first_send=hook)
        await bubble.add("📖 first step")
        hook.assert_awaited_once_with()

    async def test_hook_not_called_again_on_later_edits(self):
        context = _bubble_context()
        hook = AsyncMock()
        bubble = ProgressBubble(context, chat_id=42, on_first_send=hook)
        await bubble.add("📖 first step")  # first real send -> hook fires
        # A second step, flushed with force=True to bypass the 3s throttle (this is now an
        # EDIT of the same message, not a new send — message_id is already set).
        bubble._steps.append("⚡️ second step")
        await bubble._flush(force=True)
        context.bot.edit_message_text.assert_awaited_once()  # the edit really did happen
        hook.assert_awaited_once_with()  # still exactly once, not once per edit

    async def test_no_hook_is_safe(self):
        context = _bubble_context()
        bubble = ProgressBubble(context, chat_id=42)  # on_first_send defaults to None
        await bubble.add("📖 first step")  # must not raise

    async def test_hook_failure_does_not_break_the_bubble(self):
        context = _bubble_context()
        hook = AsyncMock(side_effect=Exception("typing indicator stop blew up"))
        bubble = ProgressBubble(context, chat_id=42, on_first_send=hook)
        await bubble.add("📖 first step")  # must not raise despite the hook failing
        self.assertEqual(bubble.message_id, 999)  # the actual bubble send still landed

    async def test_hook_not_fired_when_send_itself_fails(self):
        context = _bubble_context()
        context.bot.send_message.side_effect = Exception("network hiccup")
        hook = AsyncMock()
        bubble = ProgressBubble(context, chat_id=42, on_first_send=hook)
        await bubble.add("📖 first step")
        hook.assert_not_awaited()  # no real "first send" happened, so no signal


if __name__ == "__main__":
    unittest.main()

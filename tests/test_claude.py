"""Tests for the pure runtime-MCP-config merge in claude.py — the three cases from the
scheduling design: no user mcp-config.json, a user config with other servers, and a user
config that collides with the builtin `schedule` name (builtin must win) — plus is_no_reply,
the sentinel check that gates the scheduled-firing NO_REPLY path in handlers.py."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent.claude import NO_REPLY_SENTINEL, _build_mcp_config, is_no_reply
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


if __name__ == "__main__":
    unittest.main()

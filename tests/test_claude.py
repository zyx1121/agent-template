"""Tests for the pure runtime-MCP-config merge in claude.py — the three cases from the
scheduling design: no user mcp-config.json, a user config with other servers, and a user
config that collides with the builtin `schedule` name (builtin must win)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent.claude import _build_mcp_config
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


if __name__ == "__main__":
    unittest.main()

"""Tests for the pure env-parsing helpers in config. These must not touch os.environ, so they
run without a populated .env."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.config import load_settings, parse_group_ids, safe_name


class ParseGroupIds(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_group_ids(""), set())

    def test_negative_ids(self):
        self.assertEqual(parse_group_ids("-100,-200"), {-100, -200})

    def test_whitespace_tolerated(self):
        self.assertEqual(parse_group_ids(" -100 , -200 "), {-100, -200})

    def test_non_integer_skipped(self):
        self.assertEqual(parse_group_ids("abc,-100"), {-100})

    def test_non_negative_rejected(self):
        # a positive/zero id would be a DM, not a group — dropping it prevents bypassing the gate
        self.assertEqual(parse_group_ids("100,-100"), {-100})
        self.assertEqual(parse_group_ids("0"), set())


class SafeName(unittest.TestCase):
    def test_plain_name_kept(self):
        self.assertEqual(safe_name("hello.txt", "fb"), "hello.txt")

    def test_none_uses_fallback(self):
        self.assertEqual(safe_name(None, "fb"), "fb")

    def test_empty_uses_fallback(self):
        self.assertEqual(safe_name("", "fb"), "fb")

    def test_special_chars_replaced(self):
        self.assertEqual(safe_name("a/b:c*.txt", "fb"), "a_b_c_.txt")

    def test_truncated_to_120(self):
        self.assertEqual(len(safe_name("x" * 200, "fb")), 120)


class LoadSettingsHome(unittest.TestCase):
    """Regression test for a real deployed bug: `settings.home` used to derive from
    `Path(__file__).resolve().parents[N]`, which only lands on the repo root for an editable
    install. `deploy/install.sh` runs `uv sync --no-editable` — under that install, `__file__`
    resolves into `.venv/lib/pythonX/site-packages/agent/`, so home silently pointed inside
    the venv: SOUL.md and mcp-config.json never loaded (found nonexistent, no error), and
    session files went to a path `uv sync --reinstall` wipes on every redeploy. This test
    pins the actual contract instead: home = cwd (or AGENT_HOME), regardless of where the
    `agent` package's own source happens to live on disk."""

    def test_home_follows_cwd_not_package_location(self):
        cwd_before = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"TELEGRAM_BOT_TOKEN": "t", "OWNER_USER_ID": "1"},
                    clear=False,
                ):
                    os.environ.pop("AGENT_HOME", None)
                    settings = load_settings()
            finally:
                os.chdir(cwd_before)
            self.assertEqual(settings.home, Path(tmp).resolve())

    def test_agent_home_env_var_overrides_cwd(self):
        cwd_before = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as override:
            os.chdir(tmp)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"TELEGRAM_BOT_TOKEN": "t", "OWNER_USER_ID": "1", "AGENT_HOME": override},
                    clear=False,
                ):
                    settings = load_settings()
            finally:
                os.chdir(cwd_before)
            self.assertEqual(settings.home, Path(override))


if __name__ == "__main__":
    unittest.main()

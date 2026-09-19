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
import os
from unittest.mock import AsyncMock, patch

from agent.claude import (
    ERR_AUTH,
    ERR_GENERIC,
    ERR_TRANSIENT,
    ERR_USAGE_LIMIT,
    NO_REPLY_SENTINEL,
    ClaudeTurnError,
    ProgressBubble,
    _build_mcp_config,
    _classify_error,
    _result_message,
    is_no_reply,
    purge_stale_mcp_runtime,
    run_turn,
    write_mcp_runtime,
)
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

    def test_padded_reply_ending_with_bare_sentinel_is_suppressed(self):
        # Models occasionally pad a summary sentence despite the prompt — a trailing bare
        # sentinel still means "do not deliver" (the leak that motivated this: 2026-07-12).
        self.assertTrue(is_no_reply("No new mail, no account errors. Nothing worth reporting. NO_REPLY"))
        self.assertTrue(is_no_reply("Nothing to report.\nNO_REPLY"))
        self.assertTrue(is_no_reply("All quiet.\n\n  NO_REPLY  "))

    def test_trailing_sentinel_needs_a_word_boundary(self):
        self.assertFalse(is_no_reply("Set FOO_NO_REPLY"))
        self.assertFalse(is_no_reply("status is OKNO_REPLY"))
        self.assertFalse(is_no_reply("counted 3NO_REPLY"))

    def test_sentinel_followed_by_text_or_punctuation_still_delivers(self):
        self.assertFalse(is_no_reply("NO_REPLY — nothing to report today."))
        self.assertFalse(is_no_reply("Nothing to report. NO_REPLY."))


def _bubble_context() -> SimpleNamespace:
    sent = SimpleNamespace(message_id=999)
    return SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=sent),
        edit_message_text=AsyncMock(),
    ))


class ResultMessageAndClassify(unittest.TestCase):
    """The failure-reason extraction + categorization that fixes 'claude exited 1' hiding the
    real cause. `_result_message` takes the stream-json result event's own text first (that's
    where a limit / auth / api error reports), stderr / a bare exit line only as fallbacks.
    `_classify_error` buckets by api_error_status (live-confirmed: 429 limit, 401 auth) with
    message-text fallbacks."""

    def test_result_event_text_preferred_over_stderr(self):
        ev = {"result": "You've hit your session limit · resets 1:50pm (Asia/Taipei)"}
        self.assertEqual(
            _result_message(ev, "some stderr noise", 1),
            "You've hit your session limit · resets 1:50pm (Asia/Taipei)")

    def test_falls_back_to_stderr_when_no_result_text(self):
        self.assertEqual(_result_message({"result": ""}, "boom on stderr", 1), "boom on stderr")

    def test_falls_back_to_bare_exit_line_when_nothing_else(self):
        self.assertEqual(_result_message(None, "", 1), "claude exited 1")

    def test_classify_usage_limit_by_429_or_text(self):
        self.assertEqual(_classify_error({"api_error_status": 429}, "anything"), ERR_USAGE_LIMIT)
        self.assertEqual(_classify_error(None, "You've hit your session limit · resets 1:50pm"), ERR_USAGE_LIMIT)
        self.assertEqual(_classify_error(None, "usage limit reached"), ERR_USAGE_LIMIT)

    def test_classify_auth_by_401_403_or_text(self):
        self.assertEqual(_classify_error({"api_error_status": 401}, "Failed to authenticate. API Error: 401 Invalid bearer token"), ERR_AUTH)
        self.assertEqual(_classify_error({"api_error_status": 403}, "forbidden"), ERR_AUTH)
        self.assertEqual(_classify_error(None, "Not logged in · Please run /login"), ERR_AUTH)

    def test_classify_transient_by_5xx_529_or_text(self):
        for s in (408, 500, 502, 503, 504, 529):
            self.assertEqual(_classify_error({"api_error_status": s}, "x"), ERR_TRANSIENT, f"status {s}")
        self.assertEqual(_classify_error(None, "Overloaded"), ERR_TRANSIENT)
        self.assertEqual(_classify_error(None, "connection reset by peer"), ERR_TRANSIENT)

    def test_classify_generic_when_nothing_matches(self):
        self.assertEqual(_classify_error(None, "claude exited 1"), ERR_GENERIC)
        self.assertEqual(_classify_error({"api_error_status": 400}, "bad request"), ERR_GENERIC)

    def test_429_wins_over_transient_text(self):
        # a 429 whose message also mentions a timeout must stay usage_limit, not transient
        self.assertEqual(_classify_error({"api_error_status": 429}, "rate limit; connection slow"), ERR_USAGE_LIMIT)

    def test_error_carries_message_and_category(self):
        e = ClaudeTurnError("hit session limit", category=ERR_USAGE_LIMIT)
        self.assertEqual(e.message, "hit session limit")
        self.assertEqual(e.category, ERR_USAGE_LIMIT)
        self.assertTrue(e.is_usage_limit)
        self.assertIn("hit session limit", str(e))
        self.assertFalse(ClaudeTurnError("x", category=ERR_AUTH).is_usage_limit)


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


class McpRuntimeConfigFile(unittest.TestCase):
    """The merged `--mcp-config` file a turn hands claude carries every server's headers,
    which on the kitbash deploy target includes the bearer kitbashd issued to this Process.
    It must never be readable by anyone else and must never outlive the turn, least of all in
    run/, which is the directory a deploy keeps (a bind mount of the member's Files there)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.settings = _settings(self.home)
        self.settings.run_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_it_is_written_0600_outside_run_dir(self):
        path = write_mcp_runtime(self.settings, chat_id=42)
        self.addCleanup(path.unlink, True)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(self.settings.run_dir, path.parents)
        self.assertEqual(json.loads(path.read_text()),
                         _build_mcp_config(self.settings, chat_id=42))

    def test_stale_configs_from_an_older_version_are_purged(self):
        legacy = self.settings.run_dir / "mcp-runtime-42.json"
        legacy.write_text('{"mcpServers": {"kitbash": {"headers": {"Authorization": "Bearer x"}}}}')
        keep = self.settings.run_dir / "session-42"
        keep.write_text("abc")
        removed = purge_stale_mcp_runtime(self.settings)
        self.assertEqual(removed, [legacy])
        self.assertFalse(legacy.exists())
        self.assertTrue(keep.exists())

    def test_purge_is_quiet_when_there_is_nothing_to_purge(self):
        self.assertEqual(purge_stale_mcp_runtime(self.settings), [])


class _FakeClaudeProcess:
    """Just enough of an asyncio subprocess for run_turn: one result event, empty stderr,
    exit 0."""

    def __init__(self, session_id="sess-1", reply="done"):
        line = json.dumps({"type": "result", "result": reply, "session_id": session_id,
                           "is_error": False}).encode()
        self._lines = [line + b"\n"]
        self.stdout = self
        self.stderr = SimpleNamespace(read=AsyncMock(return_value=b""))

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
        return gen()

    async def wait(self):
        return 0

    def kill(self):  # pragma: no cover - only reached on cancellation
        pass


class McpRuntimeConfigLifecycle(unittest.IsolatedAsyncioTestCase):
    BEARER = "CANARYbearerFromKitbashd"

    async def test_a_turn_deletes_its_config_and_leaves_no_bearer_in_run_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings = _settings(home)
            settings.run_dir.mkdir(exist_ok=True)
            # what the kitbash entrypoint writes at every start
            settings.mcp_config_file.write_text(json.dumps({"mcpServers": {"kitbash": {
                "type": "http", "url": "http://host.containers.internal:4318/mcp",
                "headers": {"Authorization": f"Bearer {self.BEARER}"}}}}))
            seen = {}

            async def fake_exec(*cmd, **kwargs):
                path = Path(cmd[cmd.index("--mcp-config") + 1])
                seen["path"] = path
                seen["mode"] = os.stat(path).st_mode & 0o777
                seen["content"] = path.read_text()
                return _FakeClaudeProcess()

            with patch("asyncio.create_subprocess_exec", fake_exec):
                reply, _ = await run_turn("hi", 42, _bubble_context(), settings)

            self.assertEqual(reply, "done")
            # the bearer really was in the file claude was pointed at, written 0600,
            self.assertIn(self.BEARER, seen["content"])
            self.assertEqual(seen["mode"], 0o600)
            # and the file is gone now the turn is over,
            self.assertFalse(seen["path"].exists())
            # and nothing the deploy keeps mentions it.
            for path in settings.run_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(self.BEARER, path.read_text(errors="replace"), str(path))

    async def test_the_config_is_deleted_even_when_the_turn_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings = _settings(home)
            settings.run_dir.mkdir(exist_ok=True)
            seen = {}

            async def fake_exec(*cmd, **kwargs):
                seen["path"] = Path(cmd[cmd.index("--mcp-config") + 1])
                raise OSError("claude is not installed")

            with patch("asyncio.create_subprocess_exec", fake_exec):
                with self.assertRaises(OSError):
                    await run_turn("hi", 42, _bubble_context(), settings)
            self.assertFalse(seen["path"].exists())


if __name__ == "__main__":
    unittest.main()

"""Tests for the NO_REPLY sentinel handling in handlers.py's shared turn lifecycle
(`_run_and_deliver`): a scheduled firing (`scheduled=True`) whose reply is exactly NO_REPLY
sends nothing and deletes its progress bubble; every other case — including NO_REPLY on a real
user turn — behaves exactly as it did before this feature. `run_turn` and `send_message` are
mocked (no network, no claude subprocess, no bot token); `_clear_outbox`/`_flush_outbox` run
for real against a tempdir since they're pure filesystem I/O.

Also covers the typing-indicator wiring: `_run_and_deliver`'s `on_output_start` hook (spec 2's
"stop the moment the first outbound message appears, including the zero-tool-call case where
the final reply itself is that first message") and `_serve_turn`'s full integration (spec 1:
immediate typing on a real message; spec 3: a scheduled/non-message turn never touches typing
at all)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatAction

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
    return SimpleNamespace(bot=SimpleNamespace(
        delete_message=AsyncMock(), send_chat_action=AsyncMock()))


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


class RunAndDeliverClaudeTurnError(unittest.IsolatedAsyncioTestCase):
    """ClaudeTurnError handling — the fix for a failed turn surfacing the useless 'claude
    exited 1'. A usage/session limit is an expected transient condition: a calm 🕐 notice on a
    real user turn, and total silence on a scheduled firing (a monitoring tick must not repeat
    the same limit notice every run until it resets). A non-limit error still surfaces as a
    ⚠️ failure — but now carrying claude's real message, not a bare exit code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.context = _context()

    def tearDown(self):
        self._tmp.cleanup()

    async def test_usage_limit_on_user_turn_sends_calm_notice(self):
        err = handlers.ClaudeTurnError("You've hit your session limit · resets 1:50pm", is_usage_limit=True)
        with patch("agent.handlers.run_turn", new=AsyncMock(side_effect=err)), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings)
        self.assertFalse(ok)
        send_message.assert_called_once()
        sent = send_message.call_args.args[2]
        self.assertIn("session limit", sent)
        self.assertNotIn("claude failed", sent)  # not framed as a crash

    async def test_usage_limit_on_scheduled_turn_is_suppressed(self):
        err = handlers.ClaudeTurnError("You've hit your session limit · resets 1:50pm", is_usage_limit=True)
        with patch("agent.handlers.run_turn", new=AsyncMock(side_effect=err)), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertFalse(ok)
        send_message.assert_not_called()  # a triage tick must not spam the limit notice
        # run_turn raised before returning a bubble id, so there's nothing to delete — and a
        # 429 fails before any tool step, so no progress bubble was ever sent anyway.
        self.context.bot.delete_message.assert_not_awaited()

    async def test_non_limit_error_surfaces_real_message_not_bare_exit_code(self):
        err = handlers.ClaudeTurnError("boom: something specific broke", is_usage_limit=False)
        with patch("agent.handlers.run_turn", new=AsyncMock(side_effect=err)), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings)
        self.assertFalse(ok)
        sent = send_message.call_args.args[2]
        self.assertIn("claude failed", sent)
        self.assertIn("boom: something specific broke", sent)

    async def test_non_limit_error_on_scheduled_turn_still_notifies(self):
        # a genuine error (not a usage limit) is worth surfacing even on a scheduled tick —
        # suppression is only for the expected/transient limit case.
        err = handlers.ClaudeTurnError("real breakage", is_usage_limit=False)
        with patch("agent.handlers.run_turn", new=AsyncMock(side_effect=err)), \
             patch("agent.handlers.send_message") as send_message:
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.assertFalse(ok)
        send_message.assert_called_once()


class RunAndDeliverOnOutputStart(unittest.IsolatedAsyncioTestCase):
    """`on_output_start` — the generic turn-lifecycle hook the typing indicator is wired
    through (spec 2). `_run_and_deliver` has no idea it's "typing"; it just forwards the hook to
    `run_turn` (as `on_first_send`) and fires it once more itself before the final send, in case
    the bubble never fired it (e.g. a turn with zero tool calls — the reply IS the first output,
    per spec 2's "whichever comes first")."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.context = _context()

    def tearDown(self):
        self._tmp.cleanup()

    async def test_hook_forwarded_to_run_turn_as_on_first_send(self):
        hook = AsyncMock()
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("hi", 555))) as run_turn, \
             patch("agent.handlers.send_message"):
            await handlers._run_and_deliver("prompt", 42, self.context, self.settings, on_output_start=hook)
        self.assertIs(run_turn.await_args.kwargs["on_first_send"], hook)

    async def test_no_hook_forwards_none_and_never_crashes(self):
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("hi", 555))) as run_turn, \
             patch("agent.handlers.send_message"):
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings)
        self.assertTrue(ok)
        self.assertIsNone(run_turn.await_args.kwargs["on_first_send"])

    async def test_hook_called_before_final_send_when_bubble_never_fired_it(self):
        # run_turn is mocked, so it never actually calls the hook itself (that's ProgressBubble's
        # job, covered in test_claude.py) — this proves _run_and_deliver's OWN fallback call
        # happens, and happens before send_message, which is exactly the "zero tool calls: the
        # reply is the first outbound message" case from spec 2.
        order = []
        hook = AsyncMock(side_effect=lambda: order.append("stop"))
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("hi", None))), \
             patch("agent.handlers.send_message", side_effect=lambda *a: order.append("send")):
            await handlers._run_and_deliver("prompt", 42, self.context, self.settings, on_output_start=hook)
        self.assertEqual(order, ["stop", "send"])

    async def test_hook_still_called_on_claude_failure(self):
        # A claude-failure reply is still an outbound message — the typing indicator must stop
        # for it too, not just the happy path.
        hook = AsyncMock()
        with patch("agent.handlers.run_turn", new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("agent.handlers.send_message"):
            ok = await handlers._run_and_deliver("prompt", 42, self.context, self.settings, on_output_start=hook)
        self.assertFalse(ok)
        hook.assert_awaited_once_with()


class ServeTurnTypingIndicator(unittest.IsolatedAsyncioTestCase):
    """`_serve_turn` — the real message path — wires TypingIndicator end to end: immediate
    typing on arrival (spec 1), stopped via `on_output_start` once `run_turn` returns (spec 2)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.context = _context()
        self.context.bot_data = {"settings": self.settings}
        self.msg = SimpleNamespace(set_reaction=AsyncMock())
        self.chat = SimpleNamespace(id=42)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_real_message_gets_immediate_typing(self):
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("hi", None))), \
             patch("agent.handlers.send_message"):
            await handlers._serve_turn(self.msg, self.chat, self.context, "hello")
        self.context.bot.send_chat_action.assert_any_await(chat_id=42, action=ChatAction.TYPING)

    async def test_typing_stops_by_the_time_the_turn_finishes(self):
        # No way to observe "stopped" directly from the outside except: the keep-alive interval
        # (4s) never gets a chance to fire in this fast mocked turn, so exactly one
        # send_chat_action call (the immediate one) is expected — anything more would mean the
        # indicator kept running past the turn's end.
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("hi", None))), \
             patch("agent.handlers.send_message"):
            await handlers._serve_turn(self.msg, self.chat, self.context, "hello")
        self.assertEqual(self.context.bot.send_chat_action.await_count, 1)


class ScheduledTurnNeverTypes(unittest.IsolatedAsyncioTestCase):
    """Spec 3: a schedule firing (or any non-Telegram-triggered turn) never sends a typing
    signal — nobody's watching a chat waiting on it. `_schedule_tick` never constructs a
    TypingIndicator or passes `on_output_start`, so `_run_and_deliver(scheduled=True)` called the
    way `_schedule_tick` calls it (no `on_output_start`) must never touch send_chat_action."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = _settings(Path(self._tmp.name))
        self.context = _context()

    def tearDown(self):
        self._tmp.cleanup()

    async def test_scheduled_run_and_deliver_never_sends_typing(self):
        with patch("agent.handlers.run_turn", new=AsyncMock(return_value=("all clear", 555))), \
             patch("agent.handlers.send_message"):
            await handlers._run_and_deliver("prompt", 42, self.context, self.settings, scheduled=True)
        self.context.bot.send_chat_action.assert_not_awaited()


class ReplyContextTest(unittest.TestCase):
    """`_reply_context` — turns a Telegram reply/quote on the incoming message into a prompt
    prefix so claude knows which earlier message the user is pointing at. Pure function; fakes
    are SimpleNamespace mirroring the telegram.Message fields it reads."""

    BOT_ID = 999

    def _msg(self, *, reply_text=None, reply_caption=None, reply_from_id=None, quote_text=None):
        reply = None
        if reply_text is not None or reply_caption is not None or reply_from_id is not None:
            reply = SimpleNamespace(
                text=reply_text, caption=reply_caption,
                from_user=SimpleNamespace(id=reply_from_id) if reply_from_id is not None else None)
        quote = SimpleNamespace(text=quote_text) if quote_text is not None else None
        return SimpleNamespace(reply_to_message=reply, quote=quote)

    def test_not_a_reply_returns_empty(self):
        self.assertEqual(handlers._reply_context(self._msg(), self.BOT_ID), "")

    def test_reply_to_bot_message_labels_it_as_ours_and_includes_text(self):
        out = handlers._reply_context(
            self._msg(reply_text="mail uid 496: 老師問 meeting 時間", reply_from_id=self.BOT_ID), self.BOT_ID)
        self.assertIn("your own earlier message", out)
        self.assertIn("uid 496", out)
        self.assertTrue(out.endswith("\n\n"))  # trailing blank line so it prepends cleanly

    def test_reply_to_someone_elses_message(self):
        out = handlers._reply_context(
            self._msg(reply_text="hi", reply_from_id=12345), self.BOT_ID)
        self.assertIn("an earlier message", out)
        self.assertNotIn("your own", out)

    def test_manual_quote_fragment_is_preferred_over_full_message(self):
        out = handlers._reply_context(
            self._msg(reply_text="line A\nline B (uid 7)\nline C", reply_from_id=self.BOT_ID,
                      quote_text="line B (uid 7)"), self.BOT_ID)
        self.assertIn("quoted part", out)
        self.assertIn("line B (uid 7)", out)
        self.assertNotIn("line A", out)

    def test_caption_used_when_replied_to_message_has_no_text(self):
        out = handlers._reply_context(
            self._msg(reply_caption="photo caption here", reply_from_id=self.BOT_ID), self.BOT_ID)
        self.assertIn("photo caption here", out)

    def test_long_quoted_text_is_truncated(self):
        out = handlers._reply_context(
            self._msg(reply_text="x" * 5000, reply_from_id=self.BOT_ID), self.BOT_ID)
        self.assertIn("…(truncated)", out)
        self.assertLess(len(out), 5000)


if __name__ == "__main__":
    unittest.main()

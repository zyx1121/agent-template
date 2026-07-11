"""Tests for `TypingIndicator` (handlers.py) in isolation: the immediate first send (spec 1),
the ~_INTERVAL keep-alive rhythm (spec 2), and cosmetic failure-swallowing (spec 4). Spec 3
(no typing indicator for a scheduled/non-Telegram-triggered turn) and the "stop the moment the
turn's first outbound message appears" wiring (spec 2's other half) are covered in
test_handlers.py, since those are about how `_serve_turn`/`_run_and_deliver` USE this class, not
about the class itself. `context.bot.send_chat_action` is mocked throughout — no network, no
bot token."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.constants import ChatAction

from agent.handlers import TypingIndicator


def _context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(send_chat_action=AsyncMock()))


class TypingIndicatorStart(unittest.IsolatedAsyncioTestCase):
    async def test_start_sends_typing_immediately(self):
        # Spec 1: a real incoming message must get a typing signal right away, not after the
        # first keep-alive interval elapses.
        context = _context()
        typing = TypingIndicator(context, chat_id=42)
        await typing.start()
        context.bot.send_chat_action.assert_awaited_once_with(chat_id=42, action=ChatAction.TYPING)
        await typing.stop()


class TypingIndicatorKeepAlive(unittest.IsolatedAsyncioTestCase):
    async def test_keep_alive_resends_at_interval(self):
        # Spec 2 (rhythm half): re-sent roughly every _INTERVAL seconds while running.
        context = _context()
        typing = TypingIndicator(context, chat_id=42)
        with patch.object(TypingIndicator, "_INTERVAL", 0.02):
            await typing.start()  # 1 immediate send
            await asyncio.sleep(0.07)  # long enough for >= 2 more keep-alive sends
            await typing.stop()
        self.assertGreaterEqual(context.bot.send_chat_action.await_count, 3)

    async def test_stop_permanently_ends_the_keep_alive_loop(self):
        # Spec 2 (stop half): once stopped, no further typing signals — even if the caller
        # keeps waiting around afterward. This is what keeps the indicator from "reappearing"
        # after the bubble/reply already showed up.
        context = _context()
        typing = TypingIndicator(context, chat_id=42)
        with patch.object(TypingIndicator, "_INTERVAL", 0.02):
            await typing.start()
            await asyncio.sleep(0.03)
            await typing.stop()
            count_at_stop = context.bot.send_chat_action.await_count
            await asyncio.sleep(0.07)  # several more intervals' worth of "waiting"
        self.assertEqual(context.bot.send_chat_action.await_count, count_at_stop)

    async def test_stop_is_idempotent(self):
        context = _context()
        typing = TypingIndicator(context, chat_id=42)
        await typing.start()
        await typing.stop()
        await typing.stop()  # must not raise / not double-cancel

    async def test_stop_before_start_is_a_safe_noop(self):
        context = _context()
        typing = TypingIndicator(context, chat_id=42)
        await typing.stop()
        context.bot.send_chat_action.assert_not_awaited()


class TypingIndicatorFailureSwallowed(unittest.IsolatedAsyncioTestCase):
    async def test_send_chat_action_failure_is_swallowed(self):
        # Spec 4: any Telegram API failure here is cosmetic — logged, never raised, never
        # allowed to affect the turn.
        context = _context()
        context.bot.send_chat_action.side_effect = Exception("Forbidden: bot was blocked")
        typing = TypingIndicator(context, chat_id=42)
        await typing.start()  # must not raise despite the immediate send failing
        await typing.stop()


if __name__ == "__main__":
    unittest.main()

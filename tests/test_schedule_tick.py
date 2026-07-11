"""Regression test for the `once` schedule catch-up multi-fire bug in `_schedule_tick`
(handlers.py): a stalled tick's catch-up walks every pending minute since the last tick
against the SAME in-memory `schedules` snapshot fetched once at the top of the function. A
`once` schedule whose cron matches more than one of those pending minutes (e.g. catch-up after
the process was blocked for a few minutes, with an every-minute cron) used to fire once PER
MATCHING MINUTE instead of once per tick — its on-disk self-delete (`remove_schedule`) doesn't
retroactively update that snapshot, so later pending minutes in the same tick still see it.
`list_schedules`/`remove_schedule`/`_run_and_deliver` are mocked (no filesystem, no claude
subprocess); `datetime.now()` is pinned so the pending-minute math is deterministic."""
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agent import handlers
from agent.config import Settings

_FIXED_NOW = datetime(2026, 1, 1, 12, 5, 0)


def _settings() -> Settings:
    return Settings(
        token="t", owner_id=1, agent_name="Agent", claude_bin="claude",
        turn_timeout=1800, allowed_groups=frozenset(), home=Path("/nonexistent"),
    )


def _context(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(bot_data={"settings": settings})


class ScheduleTickOnceCatchup(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._saved_last_minute = handlers._last_minute

    def tearDown(self):
        handlers._last_minute = self._saved_last_minute

    async def test_once_schedule_fires_at_most_once_across_catchup_minutes(self):
        settings = _settings()
        context = _context(settings)
        # Tick stalled for 3 minutes: pending = [now-2, now-1, now] — 3 distinct pending
        # minutes in this one _schedule_tick call.
        handlers._last_minute = _FIXED_NOW - timedelta(minutes=3)

        sched = {
            "id": "abc123", "cron": "* * * * *", "prompt": "p", "chat_id": 42,
            "note": "", "enabled": True, "once": True,
        }
        with patch("agent.handlers.datetime") as mock_datetime, \
             patch("agent.handlers.list_schedules", return_value=[sched]), \
             patch("agent.handlers.remove_schedule") as remove_schedule, \
             patch("agent.handlers._run_and_deliver", new=AsyncMock(return_value=True)) as run_and_deliver:
            mock_datetime.now.return_value = _FIXED_NOW
            await handlers._schedule_tick(context)

        # cron="* * * * *" matches all 3 pending minutes — pre-fix this fired 3 times (once
        # per pending minute) because the in-memory `schedules` snapshot never sees its own
        # on-disk removal mid-tick. A `once` schedule must fire at most once per tick.
        self.assertEqual(run_and_deliver.await_count, 1)
        remove_schedule.assert_called_once_with(settings.schedules_file, "abc123")


if __name__ == "__main__":
    unittest.main()

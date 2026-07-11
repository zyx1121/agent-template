"""Tests for the schedules.json read/write layer: add/edit/remove/list, atomic write, and the
once-flag round-trip. Uses a tempdir path — no network, no bot token, no real lock contention
(fcntl.flock is exercised for real, just not across processes)."""
import json
import tempfile
import unittest
from pathlib import Path

from agent.schedule_store import add_schedule, edit_schedule, list_schedules, remove_schedule


class ScheduleStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "run" / "schedules.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_on_missing_file_is_empty(self):
        self.assertEqual(list_schedules(self.path), [])

    def test_add_then_list(self):
        sched = add_schedule(self.path, cron="0 8 * * *", prompt="good morning", chat_id=123, note="daily")
        self.assertEqual(len(sched["id"]), 6)
        self.assertTrue(sched["enabled"])
        self.assertFalse(sched["once"])
        self.assertIn("created_at", sched)
        listed = list_schedules(self.path)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["id"], sched["id"])
        self.assertEqual(listed[0]["chat_id"], 123)

    def test_add_multiple_gets_distinct_ids(self):
        a = add_schedule(self.path, cron="* * * * *", prompt="a", chat_id=1)
        b = add_schedule(self.path, cron="* * * * *", prompt="b", chat_id=1)
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(len(list_schedules(self.path)), 2)

    def test_once_flag_round_trips(self):
        sched = add_schedule(self.path, cron="* * * * *", prompt="once", chat_id=1, once=True)
        self.assertTrue(sched["once"])
        listed = list_schedules(self.path)
        self.assertTrue(listed[0]["once"])

    def test_edit_updates_only_given_fields(self):
        sched = add_schedule(self.path, cron="0 8 * * *", prompt="orig", chat_id=1, note="n")
        updated = edit_schedule(self.path, sched["id"], enabled=False)
        self.assertIsNotNone(updated)
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["cron"], "0 8 * * *")  # untouched
        self.assertEqual(updated["prompt"], "orig")  # untouched

    def test_edit_multiple_fields(self):
        sched = add_schedule(self.path, cron="0 8 * * *", prompt="orig", chat_id=1)
        updated = edit_schedule(self.path, sched["id"], cron="0 9 * * *", prompt="new", once=True)
        self.assertEqual(updated["cron"], "0 9 * * *")
        self.assertEqual(updated["prompt"], "new")
        self.assertTrue(updated["once"])

    def test_edit_missing_id_returns_none(self):
        self.assertIsNone(edit_schedule(self.path, "ffffff", enabled=False))

    def test_edit_ignores_unknown_kwargs(self):
        sched = add_schedule(self.path, cron="* * * * *", prompt="p", chat_id=1)
        updated = edit_schedule(self.path, sched["id"], chat_id=999)  # not in _EDITABLE_FIELDS
        self.assertEqual(updated["chat_id"], 1)  # unchanged

    def test_remove_existing(self):
        sched = add_schedule(self.path, cron="* * * * *", prompt="p", chat_id=1)
        self.assertTrue(remove_schedule(self.path, sched["id"]))
        self.assertEqual(list_schedules(self.path), [])

    def test_remove_missing_returns_false(self):
        self.assertFalse(remove_schedule(self.path, "ffffff"))

    def test_write_is_atomic_no_leftover_tmp_file(self):
        add_schedule(self.path, cron="* * * * *", prompt="p", chat_id=1)
        leftovers = list(self.path.parent.glob(".schedules-*.tmp"))
        self.assertEqual(leftovers, [])
        # the file on disk parses as valid JSON with the expected shape
        data = json.loads(self.path.read_text())
        self.assertIn("schedules", data)
        self.assertEqual(len(data["schedules"]), 1)

    def test_corrupt_file_treated_as_empty_not_crash(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not valid json")
        self.assertEqual(list_schedules(self.path), [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the dependency-free 5-field cron matcher — pure functions, no filesystem, no
network."""
import unittest
from datetime import datetime

from agent.cron import cron_matches, validate_cron


class ValidateCron(unittest.TestCase):
    def test_valid_wildcard_everything(self):
        self.assertIsNone(validate_cron("* * * * *"))

    def test_valid_step(self):
        self.assertIsNone(validate_cron("*/5 * * * *"))

    def test_valid_range_and_step(self):
        self.assertIsNone(validate_cron("0-30/10 8 * * *"))

    def test_valid_list(self):
        self.assertIsNone(validate_cron("0,15,30,45 * * * *"))

    def test_valid_weekday_range(self):
        self.assertIsNone(validate_cron("0 8 * * 1-5"))

    def test_wrong_field_count(self):
        self.assertIsNotNone(validate_cron("* * * *"))

    def test_out_of_range_value(self):
        err = validate_cron("60 * * * *")
        self.assertIsNotNone(err)
        self.assertIn("minute", err)

    def test_bad_range_order(self):
        self.assertIsNotNone(validate_cron("* * * * 5-1"))

    def test_non_numeric_field(self):
        self.assertIsNotNone(validate_cron("abc * * * *"))

    def test_zero_step_rejected(self):
        self.assertIsNotNone(validate_cron("*/0 * * * *"))

    def test_empty_field_rejected(self):
        self.assertIsNotNone(validate_cron("  * * * *"))

    def test_month_out_of_range(self):
        self.assertIsNotNone(validate_cron("* * * 13 *"))

    def test_dow_7_is_valid(self):
        self.assertIsNone(validate_cron("* * * * 7"))

    def test_dow_8_is_invalid(self):
        self.assertIsNotNone(validate_cron("* * * * 8"))


class CronMatches(unittest.TestCase):
    def test_every_minute(self):
        self.assertTrue(cron_matches("* * * * *", datetime(2026, 7, 11, 13, 47)))

    def test_step_every_5_minutes_hit(self):
        self.assertTrue(cron_matches("*/5 * * * *", datetime(2026, 7, 11, 13, 45)))

    def test_step_every_5_minutes_miss(self):
        self.assertFalse(cron_matches("*/5 * * * *", datetime(2026, 7, 11, 13, 47)))

    def test_daily_8am_hit(self):
        self.assertTrue(cron_matches("0 8 * * *", datetime(2026, 7, 11, 8, 0)))

    def test_daily_8am_wrong_hour_miss(self):
        self.assertFalse(cron_matches("0 8 * * *", datetime(2026, 7, 11, 9, 0)))

    def test_daily_8am_wrong_minute_miss(self):
        self.assertFalse(cron_matches("0 8 * * *", datetime(2026, 7, 11, 8, 1)))

    def test_weekday_8am_hits_monday(self):
        # 2026-07-13 is a Monday
        self.assertTrue(cron_matches("0 8 * * 1-5", datetime(2026, 7, 13, 8, 0)))

    def test_weekday_8am_misses_sunday(self):
        # 2026-07-12 is a Sunday
        self.assertFalse(cron_matches("0 8 * * 1-5", datetime(2026, 7, 12, 8, 0)))

    def test_dow_0_and_7_both_mean_sunday(self):
        sunday = datetime(2026, 7, 12, 8, 0)
        self.assertTrue(cron_matches("0 8 * * 0", sunday))
        self.assertTrue(cron_matches("0 8 * * 7", sunday))

    def test_dom_and_dow_both_restricted_is_or_dom_hit(self):
        # dom=1 (1st of month) OR dow=1 (Monday) — 2026-07-01 is a Wednesday, so this only
        # matches via the dom side, proving OR (an AND would reject it).
        self.assertTrue(cron_matches("0 8 1 * 1", datetime(2026, 7, 1, 8, 0)))

    def test_dom_and_dow_both_restricted_is_or_dow_hit(self):
        # 2026-07-13 is a Monday but not the 1st — matches via the dow side only.
        self.assertTrue(cron_matches("0 8 1 * 1", datetime(2026, 7, 13, 8, 0)))

    def test_dom_and_dow_both_restricted_neither_hits(self):
        # 2026-07-14 is a Tuesday and not the 1st — neither side matches.
        self.assertFalse(cron_matches("0 8 1 * 1", datetime(2026, 7, 14, 8, 0)))

    def test_month_field_restricts(self):
        self.assertFalse(cron_matches("0 8 * 12 *", datetime(2026, 7, 11, 8, 0)))
        self.assertTrue(cron_matches("0 8 * 12 *", datetime(2026, 12, 11, 8, 0)))

    def test_invalid_expression_raises(self):
        with self.assertRaises(ValueError):
            cron_matches("not a cron", datetime(2026, 7, 11, 8, 0))


if __name__ == "__main__":
    unittest.main()

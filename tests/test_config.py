"""Tests for the pure env-parsing helpers in config. These must not touch os.environ, so they
run without a populated .env."""
import unittest

from agent.config import parse_group_ids, safe_name


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


if __name__ == "__main__":
    unittest.main()

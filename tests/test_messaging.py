"""Characterization tests for the markdown→HTML rendering and chunking — the fiddly pure
functions that must not drift. Golden values captured from the pre-refactor behaviour."""
import unittest

from agent.messaging import _chunks, md_to_html


class MdToHtml(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(md_to_html("**hi** there"), "<b>hi</b> there")

    def test_inline_code(self):
        self.assertEqual(md_to_html("run `foo bar` now"), "run <code>foo bar</code> now")

    def test_fenced_block(self):
        self.assertEqual(md_to_html("```python\nx = 1\n```"), "<pre>x = 1\n</pre>")

    def test_header_to_bold(self):
        self.assertEqual(md_to_html("# Title\nbody"), "<b>Title</b>\nbody")

    def test_bullets_to_dot(self):
        self.assertEqual(md_to_html("- one\n* two"), "• one\n• two")

    def test_html_chars_escaped(self):
        self.assertEqual(md_to_html("a < b & c > d"), "a &lt; b &amp; c &gt; d")

    def test_bold_survives_astral_emoji(self):
        # parse must not miscount offsets around astral chars
        self.assertEqual(md_to_html("**bold** 🎉 tail"), "<b>bold</b> 🎉 tail")

    def test_nul_stripped(self):
        self.assertEqual(md_to_html("a\x00b"), "ab")

    def test_prose_regex_never_touches_code(self):
        # ** inside inline code must stay literal, not become <b>
        self.assertEqual(md_to_html("`**not bold**`"), "<code>**not bold**</code>")


class Chunks(unittest.TestCase):
    def test_short_stays_single(self):
        self.assertEqual(_chunks("a\nb\nc"), ["a\nb\nc"])

    def test_long_line_hard_split(self):
        self.assertEqual(_chunks("x" * 40, size=10), ["x" * 10] * 4)

    def test_fence_straddling_split_is_reclosed(self):
        straddle = "```\n" + "\n".join(str(i) for i in range(200)) + "\n```"
        ch = _chunks(straddle, size=120)
        self.assertGreater(len(ch), 1)
        self.assertTrue(ch[0].rstrip().endswith("```"))
        self.assertTrue(ch[1].lstrip().startswith("```"))


if __name__ == "__main__":
    unittest.main()

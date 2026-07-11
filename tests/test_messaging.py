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

    def test_italic_single_star(self):
        self.assertEqual(md_to_html("*hi* there"), "<i>hi</i> there")

    def test_italic_underscore(self):
        self.assertEqual(md_to_html("_hi_ there"), "<i>hi</i> there")

    def test_italic_underscore_does_not_eat_identifiers(self):
        # snake_case must not be read as an underscore-italic delimiter pair
        self.assertEqual(md_to_html("a_b_c stays literal"), "a_b_c stays literal")

    def test_table_ascii_golden(self):
        md = "| a | bb |\n|---|---|\n| 1 | 2 |"
        self.assertEqual(md_to_html(md), "<pre>a | bb\n--+---\n1 | 2 </pre>")

    def test_table_cjk_columns_stay_aligned(self):
        # east-asian-width chars are 2 columns wide — a naive len()-based pad would misalign
        md = "| repo | 可見度 |\n|---|---|\n| ai.winlab.tw | public |\n| skills | 好 |"
        out = md_to_html(md)
        body = out[len("<pre>"):-len("</pre>")]
        lines = body.split("\n")
        self.assertEqual(len(lines), 4)  # header, separator, 2 data rows
        sep_col = lines[1].index("+")
        for ln in (lines[0], lines[2], lines[3]):
            self.assertEqual(ln[sep_col], "|")

    def test_table_inside_code_fence_not_mistaken_for_table(self):
        # a ``` block containing `|`-heavy text must stay a plain code block, not be
        # re-parsed as a GFM table (the fence stash must run first)
        md = "```\n| not | a | table |\n|---|---|---|\n```"
        self.assertEqual(md_to_html(md), "<pre>| not | a | table |\n|---|---|---|\n</pre>")

    def test_pipe_text_without_separator_row_untouched(self):
        self.assertEqual(md_to_html("a | b"), "a | b")


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

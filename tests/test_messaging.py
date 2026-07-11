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

    def test_table_becomes_bullet_list(self):
        md = "| repo | visibility | last updated |\n|---|---|---|\n| example.com | Public | 2026-07-10 |"
        self.assertEqual(
            md_to_html(md),
            "• example.com — visibility: Public · last updated: 2026-07-10",
        )

    def test_table_row_empty_cells_dropped(self):
        md = "| repo | visibility | note |\n|---|---|---|\n| skills | private |  |"
        self.assertEqual(md_to_html(md), "• skills — visibility: private")

    def test_table_long_cell_has_no_column_to_misalign(self):
        # this is the actual failure mode that motivated switching away from a <pre> grid:
        # Telegram mobile hard-wraps a long <pre> line instead of scrolling it, breaking the
        # whole grid's alignment. A bullet line just wraps like normal text — nothing to break.
        md = "| Skill | purpose |\n|---|---|\n| sync-reports | sync weekly reports from Google Drive to example.com |"
        self.assertEqual(
            md_to_html(md),
            "• sync-reports — purpose: sync weekly reports from Google Drive to example.com",
        )

    def test_table_cell_markdown_still_renders(self):
        # unlike the old <pre>-stash approach, a list line is NOT stashed inert — bold/code
        # inside a cell still goes through the normal passes below.
        md = "| repo | visibility |\n|---|---|\n| `example.com` | **Public** |"
        self.assertEqual(
            md_to_html(md),
            "• <code>example.com</code> — visibility: <b>Public</b>",
        )

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

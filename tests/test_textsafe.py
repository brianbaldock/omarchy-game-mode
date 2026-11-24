"""Text from other programs is made safe before the window shows it."""

import unittest

from gaminghome.textsafe import clean_text


class CleanTextTest(unittest.TestCase):
    def test_an_ordinary_text_is_kept(self):
        self.assertEqual(clean_text("org.example.Game", 32), "org.example.Game")
        self.assertEqual(clean_text("Steam Big Picture", 32), "Steam Big Picture")

    def test_control_characters_and_text_direction_tricks_are_removed(self):
        self.assertEqual(clean_text("Pad\x00\x07\x1b[31m", 32), "Pad[31m")
        self.assertEqual(clean_text("Pa\u202ed\u2066x\u2069\u200f\ufeff\u200b", 32), "Padx")
        self.assertEqual(clean_text("\ud800Pad\ue000", 32), "Pad")

    def test_line_breaks_and_runs_of_white_space_become_one_space(self):
        self.assertEqual(clean_text("  My \n\t Pad\u2028  Two\u00a0 ", 32), "My Pad Two")

    def test_a_text_is_cut_to_the_limit_and_the_cut_leaves_no_trailing_space(self):
        self.assertEqual(clean_text("x" * 500, 32), "x" * 32)
        self.assertEqual(clean_text("abc def", 4), "abc")
        self.assertEqual(clean_text("abc", 0), "")
        self.assertEqual(clean_text("abc", -3), "")

    def test_writing_that_needs_zero_width_joiners_and_symbols_survives(self):
        self.assertEqual(clean_text("می\u200cخواهم", 32), "می\u200cخواهم")
        self.assertEqual(clean_text("🎮", 32), "🎮")
        self.assertEqual(clean_text("ヘッドホン", 32), "ヘッドホン")
        self.assertEqual(clean_text("👩\u200d💻", 32), "👩\u200d💻")

    def test_every_other_format_character_is_removed(self):
        hidden = ("\U000e0001\U000e0041\U000e007f"     # tags
                  "\u00ad"                              # a soft hyphen
                  "\ufff9\ufffa\ufffb"                  # an interlinear annotation
                  "\u206a\u206b\u206c\u206d\u206e\u206f"
                  "\u180e\u2061\u2064\u0600\U0001bca0")
        self.assertEqual(clean_text("Pa" + hidden + "d", 32), "Pad")

    def test_at_most_three_combining_marks_stay_in_a_row(self):
        self.assertEqual(clean_text("e" + "\u0301" * 50 + "x", 32), "e\u0301\u0301\u0301x")
        self.assertEqual(clean_text("a" + "\u20dd" * 5, 32), "a\u20dd\u20dd\u20dd")         # enclosing ones too
        self.assertEqual(clean_text("Vie\u0302\u0323t", 32), "Vie\u0302\u0323t")            # written with two
        self.assertEqual(clean_text("e\u0301\u0301\u0301\u200b\u0301 e\u0301", 32), "e\u0301\u0301\u0301 e\u0301")

    def test_anything_that_is_not_a_string_gives_nothing(self):
        for value in (None, 7, b"abc", ["a"]):
            with self.subTest(value=value):
                self.assertEqual(clean_text(value, 32), "")


if __name__ == "__main__":
    unittest.main()

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

    def test_anything_that_is_not_a_string_gives_nothing(self):
        for value in (None, 7, b"abc", ["a"]):
            with self.subTest(value=value):
                self.assertEqual(clean_text(value, 32), "")


if __name__ == "__main__":
    unittest.main()

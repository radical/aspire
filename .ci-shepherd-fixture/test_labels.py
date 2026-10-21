"""Fork-only repair fixture; normalization failures are intentional."""

import unittest

from labels import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_normalizes_surrounding_whitespace_and_case(self):
        for text, expected in [
            ("  hello  ", "hello"),
            ("HELLO", "hello"),
            ("  MiXeD  ", "mixed"),
            ("\tADMIN\n", "admin"),
            (" \t\n ", ""),
        ]:
            with self.subTest(text=text):
                self.assertEqual(normalize_label(text), expected)

    def test_preserves_normalized_content_and_internal_whitespace(self):
        for text in ["", "hello", "two  words", "two\twords", "a_b-42!"]:
            with self.subTest(text=text):
                self.assertEqual(normalize_label(text), text)

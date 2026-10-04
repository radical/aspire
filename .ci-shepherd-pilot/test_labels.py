import unittest

from labels import normalize_label


class LabelNormalizationTests(unittest.TestCase):
    def test_trims_surrounding_whitespace_and_lowercases(self):
        for value, expected in (
            ("HELLO", "hello"),
            (" Hello ", "hello"),
            ("\tHeLLo\n", "hello"),
            ("MiXeD", "mixed"),
            (" ADMIN ", "admin"),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_label(value), expected)

    def test_preserves_internal_whitespace_and_punctuation(self):
        for value, expected in (
            (" Two  Words ", "two  words"),
            (" Two\tWords ", "two\twords"),
            (" A_B-42! ", "a_b-42!"),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_label(value), expected)


if __name__ == "__main__":
    unittest.main()

import unittest

from normalize import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_strips_and_lowercases(self) -> None:
        self.assertEqual(normalize_label("  NO-MERGE  "), "no-merge")

    def test_preserves_embedded_spaces(self) -> None:
        self.assertEqual(normalize_label("  No Merge Label  "), "no merge label")

    def test_replaces_each_embedded_tab_with_one_space(self) -> None:
        self.assertEqual(normalize_label("A\tB"), "a b")
        self.assertEqual(normalize_label("  A\t\tB  C  "), "a  b  c")


if __name__ == "__main__":
    unittest.main()

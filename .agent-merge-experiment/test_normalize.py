import unittest

from normalize import normalize_label


class NormalizeLabelTests(unittest.TestCase):
    def test_strips_and_lowercases(self) -> None:
        self.assertEqual(normalize_label("  NO-MERGE  "), "no-merge")

    def test_preserves_embedded_spaces(self) -> None:
        self.assertEqual(normalize_label("  No Merge Label  "), "no merge label")


if __name__ == "__main__":
    unittest.main()

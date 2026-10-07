import unittest

from fixture import canonical_service, normalize_label, parse_retry_count


class FixtureTests(unittest.TestCase):
    def test_known_alias_and_unknown_service(self):
        self.assertEqual("frontend", canonical_service("WEB"))
        self.assertEqual("database", canonical_service("DATABASE"))

    def test_label_lowercase_and_outer_whitespace(self):
        self.assertEqual("hello  world", normalize_label("  HELLO  WORLD  "))
        self.assertEqual("", normalize_label("   "))

    def test_retry_count(self):
        self.assertEqual(0, parse_retry_count("0"))
        self.assertEqual(12, parse_retry_count("012"))
        for value in ("", "-1", "+1", "1.5", "one", "1_000", " 1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_retry_count(value)


if __name__ == "__main__":
    unittest.main()

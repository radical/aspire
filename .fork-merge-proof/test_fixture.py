import unittest

from fixture import canonical_service


class FixtureTests(unittest.TestCase):
    def test_gateway_alias(self):
        self.assertEqual("backend", canonical_service("GATEWAY"))

    def test_aliases_ignore_outer_whitespace_and_case(self):
        self.assertEqual("frontend", canonical_service("  WEB  "))
        self.assertEqual("backend", canonical_service("\tAPI\n"))

    def test_unknown_services_remain_normalized(self):
        self.assertEqual("database", canonical_service(" DATABASE "))
        self.assertEqual("", canonical_service(" \t\n"))


if __name__ == "__main__":
    unittest.main()

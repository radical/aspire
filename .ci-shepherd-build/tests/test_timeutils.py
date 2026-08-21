from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ci_shepherd.timeutils import format_utc_z, parse_aware_iso8601


class ParseAwareIso8601Tests(unittest.TestCase):
    def test_zulu_timestamp_is_parsed_as_utc(self) -> None:
        self.assertEqual(
            datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc),
            parse_aware_iso8601("2026-08-19T16:00:00Z", "collectedAt"),
        )

    def test_offset_timestamp_is_converted_to_utc(self) -> None:
        self.assertEqual(
            datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc),
            parse_aware_iso8601("2026-08-19T18:00:00+02:00", "collectedAt"),
        )

    def test_naive_timestamp_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "collectedAt must be a timezone-aware"):
            parse_aware_iso8601("2026-08-19T16:00:00", "collectedAt")

    def test_non_timestamp_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "collectedAt must be a timezone-aware"):
            parse_aware_iso8601("yesterday", "collectedAt")

    def test_missing_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "collectedAt must be a nonempty string"):
            parse_aware_iso8601(None, "collectedAt")


class FormatUtcZTests(unittest.TestCase):
    def test_utc_datetime_renders_with_z_suffix(self) -> None:
        self.assertEqual(
            "2026-08-19T16:00:00Z",
            format_utc_z(datetime(2026, 8, 19, 16, 0, tzinfo=timezone.utc)),
        )

    def test_offset_datetime_is_normalized_to_utc(self) -> None:
        parsed = parse_aware_iso8601("2026-08-19T18:00:00+02:00", "observedAt")

        self.assertEqual("2026-08-19T16:00:00Z", format_utc_z(parsed))


if __name__ == "__main__":
    unittest.main()

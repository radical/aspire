from __future__ import annotations

from io import BytesIO
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from ci_shepherd import trx
from ci_shepherd.trx import parse_test_results_archive


def trx_archive(*results: tuple[str, str, str]) -> bytes:
    definitions = []
    executions = []
    for index, (test_name, outcome, display_name) in enumerate(results, start=1):
        class_name, method_name = test_name.rsplit(".", 1)
        definitions.append(
            f"""
    <UnitTest id="{index}" name="{display_name}">
      <TestMethod className="{class_name}" name="{method_name}" />
    </UnitTest>"""
        )
        executions.append(
            f'<UnitTestResult testId="{index}" testName="{display_name}" outcome="{outcome}" />'
        )
    trx = f"""<?xml version="1.0" encoding="utf-8"?>
<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
  <TestDefinitions>{''.join(definitions)}</TestDefinitions>
  <Results>{''.join(executions)}</Results>
</TestRun>"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "windows-latest/testresults/Hosting-1_net10.0_20260830120000.trx",
            trx,
        )
    return stream.getvalue()


class TrxTests(unittest.TestCase):
    def test_returns_canonical_method_outcomes(self) -> None:
        content = trx_archive(
            (
                "Namespace.Type.FlakyTheory",
                "Passed",
                "Namespace.Type.FlakyTheory(value: 1)",
            ),
            (
                "Namespace.Type.FlakyTheory",
                "Failed",
                "Namespace.Type.FlakyTheory(value: 2)",
            ),
            (
                "Namespace.Type.PassingTest",
                "Passed",
                "Namespace.Type.PassingTest",
            ),
        )

        self.assertEqual(
            [
                {
                    "lane": "Hosting-1",
                    "os": "windows-latest",
                    "testName": "Namespace.Type.FlakyTheory",
                    "outcome": "Failed",
                },
                {
                    "lane": "Hosting-1",
                    "os": "windows-latest",
                    "testName": "Namespace.Type.PassingTest",
                    "outcome": "Passed",
                },
            ],
            parse_test_results_archive(content),
        )

    def test_rejects_xml_entities(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/testresults/Tests_net10.0_now.trx",
                b'<!DOCTYPE x [<!ENTITY y "z">]><TestRun />',
            )

        with self.assertRaisesRegex(ValueError, "XML declarations"):
            parse_test_results_archive(stream.getvalue())

    def test_rejects_invalid_or_missing_trx_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid ZIP"):
            parse_test_results_archive(b"not a zip")

        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr("readme.txt", "no results")

        with self.assertRaisesRegex(ValueError, "TRX file count"):
            parse_test_results_archive(stream.getvalue())

    def test_rejects_trx_files_outside_the_repository_convention(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "unexpected.trx",
                "<TestRun />",
            )

        with self.assertRaisesRegex(ValueError, "repository convention"):
            parse_test_results_archive(stream.getvalue())

    def test_rejects_archive_entry_and_size_limits(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/Tests_net10.0_now.trx",
                "<TestRun />",
            )
            archive.writestr("extra.txt", "x")

        with patch.object(trx, "MAX_ARCHIVE_ENTRIES", 1):
            with self.assertRaisesRegex(ValueError, "too many entries"):
                parse_test_results_archive(stream.getvalue())

        with patch.object(trx, "MAX_TRX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "oversized TRX"):
                parse_test_results_archive(stream.getvalue())

        with patch.object(trx, "MAX_TOTAL_TRX_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "too much TRX"):
                parse_test_results_archive(stream.getvalue())

    def test_rejects_malformed_or_ambiguous_trx(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/Tests_net10.0_now.trx",
                "<TestRun>",
            )

        with self.assertRaisesRegex(ValueError, "malformed TRX"):
            parse_test_results_archive(stream.getvalue())

        ambiguous = b"""<TestRun>
  <TestDefinitions>
    <UnitTest id="1">
      <TestMethod className="Tests.One" name="Test" />
    </UnitTest>
    <UnitTest id="1">
      <TestMethod className="Tests.Two" name="Test" />
    </UnitTest>
  </TestDefinitions>
</TestRun>"""
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/Tests_net10.0_now.trx",
                ambiguous,
            )

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            parse_test_results_archive(stream.getvalue())


if __name__ == "__main__":
    unittest.main()

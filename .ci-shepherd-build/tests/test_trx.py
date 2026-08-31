from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath
import re
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from ci_shepherd import trx
from ci_shepherd.trx import parse_test_results_archive


TRX_NAME = re.compile(r"^(?P<lane>.+)_net[^_]+_[^/]+\.trx$", re.IGNORECASE)


def parse(content: bytes) -> list[dict[str, str]]:
    def identify(path_text: str) -> tuple[str, str] | None:
        path = PurePosixPath(path_text)
        match = TRX_NAME.fullmatch(path.name)
        if len(path.parts) < 2 or match is None:
            return None
        return match.group("lane"), path.parts[0]

    return parse_test_results_archive(content, identify_trx=identify)


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
            parse(content),
        )

    def test_cumulative_retry_artifact_cannot_turn_a_failure_into_a_pass(self) -> None:
        failed_archive = trx_archive(
            ("Namespace.Type.Flaky", "Failed", "Namespace.Type.Flaky")
        )
        passed_archive = trx_archive(
            ("Namespace.Type.Flaky", "Passed", "Namespace.Type.Flaky")
        )
        stream = BytesIO()
        with (
            ZipFile(stream, "w", ZIP_DEFLATED) as combined,
            ZipFile(BytesIO(failed_archive)) as failed,
            ZipFile(BytesIO(passed_archive)) as passed,
        ):
            combined.writestr(
                "windows-latest/testresults/Hosting-1_net10.0_first.trx",
                failed.read(failed.namelist()[0]),
            )
            combined.writestr(
                "windows-latest/testresults/Hosting-1_net10.0_second.trx",
                passed.read(passed.namelist()[0]),
            )

        self.assertEqual(
            [
                {
                    "lane": "Hosting-1",
                    "os": "windows-latest",
                    "testName": "Namespace.Type.Flaky",
                    "outcome": "Failed",
                }
            ],
            parse(stream.getvalue()),
        )

    def test_rejects_xml_entities(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/testresults/Tests_net10.0_now.trx",
                b'<!DOCTYPE x [<!ENTITY y "z">]><TestRun />',
            )

        with self.assertRaisesRegex(ValueError, "XML declarations"):
            parse(stream.getvalue())

    def test_rejects_invalid_or_missing_trx_data(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid ZIP"):
            parse(b"not a zip")

        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr("readme.txt", "no results")

        with self.assertRaisesRegex(ValueError, "TRX file count"):
            parse(stream.getvalue())

    def test_rejects_trx_files_outside_the_repository_convention(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "unexpected.trx",
                "<TestRun />",
            )

        with self.assertRaisesRegex(ValueError, "repository convention"):
            parse(stream.getvalue())

    def test_rejects_partial_trx_matches_instead_of_losing_evidence(self) -> None:
        valid_archive = trx_archive(
            ("Namespace.Type.Flaky", "Failed", "Namespace.Type.Flaky")
        )
        with ZipFile(BytesIO(valid_archive)) as valid:
            payload = valid.read(valid.namelist()[0])
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "windows-latest/testresults/Hosting-1_net10.0_now.trx",
                payload,
            )
            archive.writestr("unexpected.trx", payload)

        with self.assertRaisesRegex(ValueError, "does not match"):
            parse(stream.getvalue())

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
                parse(stream.getvalue())

        with patch.object(trx, "MAX_TRX_FILE_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "oversized TRX"):
                parse(stream.getvalue())

        with patch.object(trx, "MAX_TOTAL_TRX_BYTES", 5):
            with self.assertRaisesRegex(ValueError, "too much TRX"):
                parse(stream.getvalue())

    def test_rejects_malformed_or_ambiguous_trx(self) -> None:
        stream = BytesIO()
        with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
            archive.writestr(
                "ubuntu-latest/Tests_net10.0_now.trx",
                "<TestRun>",
            )

        with self.assertRaisesRegex(ValueError, "malformed TRX"):
            parse(stream.getvalue())

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
            parse(stream.getvalue())


if __name__ == "__main__":
    unittest.main()

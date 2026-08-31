from __future__ import annotations

from collections import defaultdict
from io import BytesIO
from pathlib import PurePosixPath
from typing import Callable
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


MAX_ARCHIVE_ENTRIES = 500
MAX_TRX_FILES = 200
MAX_TRX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_TRX_BYTES = 100 * 1024 * 1024

def parse_test_results_archive(
    content: bytes,
    *,
    identify_trx: Callable[[str], tuple[str, str] | None],
) -> list[dict[str, str]]:
    try:
        archive = ZipFile(BytesIO(content))
    except BadZipFile as exc:
        raise ValueError("Test-results artifact is not a valid ZIP archive.") from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("Test-results artifact contains too many entries.")
        trx_entries = [
            entry
            for entry in entries
            if not entry.is_dir() and entry.filename.lower().endswith(".trx")
        ]
        if not trx_entries or len(trx_entries) > MAX_TRX_FILES:
            raise ValueError("Test-results artifact has an invalid TRX file count.")
        if any(entry.file_size > MAX_TRX_FILE_BYTES for entry in trx_entries):
            raise ValueError("Test-results artifact contains an oversized TRX file.")
        if sum(entry.file_size for entry in trx_entries) > MAX_TOTAL_TRX_BYTES:
            raise ValueError("Test-results artifact contains too much TRX data.")

        outcomes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        matched_trx_count = 0
        for entry in trx_entries:
            path = PurePosixPath(entry.filename)
            identity = identify_trx(path.as_posix())
            if identity is None:
                raise ValueError(
                    "Test-results artifact contains a TRX file that does not "
                    "match the repository convention."
                )
            matched_trx_count += 1
            try:
                with archive.open(entry) as stream:
                    payload = stream.read(MAX_TRX_FILE_BYTES + 1)
            except BadZipFile as exc:
                raise ValueError(
                    "Test-results artifact contains a corrupt TRX entry."
                ) from exc
            if len(payload) > MAX_TRX_FILE_BYTES:
                raise ValueError("Test-results artifact contains an oversized TRX file.")
            if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                raise ValueError("Test-results artifact contains XML declarations.")
            try:
                root = ElementTree.fromstring(payload)
            except ElementTree.ParseError as exc:
                raise ValueError("Test-results artifact contains malformed TRX.") from exc

            definitions: dict[str, str] = {}
            for unit_test in _elements(root, "UnitTest"):
                test_id = unit_test.get("id")
                test_method = next(_children(unit_test, "TestMethod"), None)
                if test_id is None or test_method is None:
                    continue
                class_name = test_method.get("className")
                method_name = test_method.get("name")
                if not class_name or not method_name:
                    continue
                canonical_name = f"{class_name}.{method_name}"
                existing = definitions.get(test_id)
                if existing is not None and existing != canonical_name:
                    raise ValueError("TRX test identifiers are ambiguous.")
                definitions[test_id] = canonical_name

            lane, os_name = identity
            for result in _elements(root, "UnitTestResult"):
                outcome = result.get("outcome")
                test_name = definitions.get(result.get("testId", ""))
                if outcome not in {"Failed", "Passed"} or test_name is None:
                    continue
                outcomes[(lane, os_name, test_name)].add(outcome)
        if matched_trx_count == 0:
            raise ValueError(
                "Test-results artifact has no TRX files matching the repository convention."
            )

    return [
        {
            "lane": lane,
            "os": os_name,
            "testName": test_name,
            # One failing theory row means the method failed even if its other
            # data rows passed in the same attempt.
            "outcome": "Failed" if "Failed" in values else "Passed",
        }
        for (lane, os_name, test_name), values in sorted(outcomes.items())
    ]


def _elements(root: ElementTree.Element, name: str):
    return (
        element
        for element in root.iter()
        if _local_name(element.tag) == name
    )


def _children(root: ElementTree.Element, name: str):
    return (
        element
        for element in root
        if _local_name(element.tag) == name
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

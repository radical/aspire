#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any

from ci_shepherd.adaptive import AdaptiveEnricher
from ci_shepherd.github import GitHubClient
from ci_shepherd.models import (
    ValidationError,
    stable_json,
    validate_evidence_requests,
    validate_snapshot,
)


def _load_json(path: Path, description: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"Unable to read valid {description} JSON from {path}: {exc}") from exc


def _prepare_private_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)


def _write_private(path: Path, content: str) -> None:
    _prepare_private_path(path)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _ensure_distinct_artifacts(
    input_path: Path,
    requests_path: Path,
    output_path: Path,
    errors_path: Path,
    audit_path: Path,
) -> None:
    if _paths_alias(output_path, input_path):
        raise ValidationError("Expansion output path must differ from the input path.")

    for description, path in (
        ("errors", errors_path),
        ("audit", audit_path),
    ):
        if _paths_alias(path, input_path) or _paths_alias(path, requests_path):
            raise ValidationError(
                f"Expansion {description} path must not overwrite an input artifact."
            )
    if _paths_alias(errors_path, output_path) or _paths_alias(audit_path, output_path):
        raise ValidationError("Expansion output, errors, and audit paths must be distinct.")
    if _paths_alias(errors_path, audit_path):
        raise ValidationError("Expansion errors and audit paths must be distinct.")


def _paths_alias(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def expand_files(
    input_path: Path,
    requests_path: Path,
    output_path: Path,
    errors_path: Path,
    *,
    checkout: Path | None,
    audit_path: Path,
) -> Path:
    _ensure_distinct_artifacts(
        input_path,
        requests_path,
        output_path,
        errors_path,
        audit_path,
    )
    snapshot = _load_json(input_path, "snapshot")
    request_document = _load_json(requests_path, "evidence request")
    validate_snapshot(snapshot)
    validate_evidence_requests(snapshot, request_document)

    for path in (output_path, errors_path, audit_path):
        _prepare_private_path(path)
    audit_path.touch(mode=0o600, exist_ok=True)
    audit_path.chmod(0o600)

    client = GitHubClient(
        runner=subprocess.run,
        popen_factory=subprocess.Popen,
        sleep=time.sleep,
        now=lambda: datetime.now(UTC),
        audit_path=audit_path,
    )
    enricher = AdaptiveEnricher(
        client,
        now=lambda: datetime.now(UTC),
        checkout=checkout.resolve() if checkout is not None else None,
    )
    expanded = enricher.expand(snapshot, request_document)
    validate_snapshot(expanded)
    _write_private(errors_path, stable_json(enricher.errors))
    _write_private(output_path, stable_json(expanded))
    return output_path.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Expand a CI shepherd snapshot with bounded read-only evidence."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--errors", type=Path, required=True)
    parser.add_argument("--checkout", type=Path)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        output_path = expand_files(
            args.input,
            args.requests,
            args.output,
            args.errors,
            checkout=args.checkout,
            audit_path=args.audit,
        )
    finally:
        os.umask(old_umask)
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

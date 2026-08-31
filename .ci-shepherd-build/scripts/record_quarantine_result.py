#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

from ci_shepherd.github import GitHubClient
from ci_shepherd.models import stable_json
from ci_shepherd.quarantine_result import (
    record_quarantine_worker_result,
    validate_quarantine_worker_result,
)
from quarantine_session import _load_request


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and record one typed quarantine worker result."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--mutation-result", type=Path)
    parser.add_argument("--commit-validation", type=Path)
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()

    request = _load_request(args.request, args.state_dir, args.batch_id)
    result = json.loads(args.result.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Quarantine worker result must be a JSON object.")
    validated = validate_quarantine_worker_result(request, result)
    pull_request_document = None
    pull_request_files = None
    pull_request_reviews = None
    mutation_result = None
    commit_validation = None
    if validated["outcome"] in {"pull-request-open", "completed"}:
        if args.mutation_result is None or args.commit_validation is None:
            raise ValueError(
                "Successful quarantine results require --mutation-result and "
                "--commit-validation."
            )
        mutation_result = json.loads(
            args.mutation_result.read_text(encoding="utf-8")
        )
        if not isinstance(mutation_result, dict):
            raise ValueError("Quarantine mutation result must be a JSON object.")
        commit_validation = json.loads(
            args.commit_validation.read_text(encoding="utf-8")
        )
        if not isinstance(commit_validation, dict):
            raise ValueError("Quarantine commit validation must be a JSON object.")
        pull_request = validated["pullRequest"]
        if not isinstance(pull_request, dict):
            raise ValueError("Validated pull request is missing.")
        repository = str(validated["repository"])
        url = str(pull_request["url"])
        prefix = f"https://github.com/{repository}/pull/"
        if not url.startswith(prefix) or not url[len(prefix):].isdigit():
            raise ValueError("Pull request URL does not match the worker repository.")
        number = int(url[len(prefix):])
        client = GitHubClient(
            runner=subprocess.run,
            popen_factory=subprocess.Popen,
            sleep=time.sleep,
            now=lambda: datetime.now(UTC),
            audit_path=args.audit,
        )
        pull_request_document = client.get(
            f"/repos/{quote(repository, safe='/')}/pulls/{number}"
        )
        if not isinstance(pull_request_document, dict):
            raise ValueError("GitHub returned an invalid pull request document.")
        changed_files = pull_request_document.get("changed_files")
        if (
            not isinstance(changed_files, int)
            or isinstance(changed_files, bool)
            or changed_files < 1
            or changed_files > 100
        ):
            raise ValueError(
                "Quarantine pull requests must change between 1 and 100 files."
            )
        pull_request_files = client.get(
            f"/repos/{quote(repository, safe='/')}/pulls/{number}/files?per_page=100"
        )
        if not isinstance(pull_request_files, list):
            raise ValueError("GitHub returned an invalid pull request file list.")
        if validated["outcome"] == "completed":
            pull_request_reviews = client.get_pages(
                f"/repos/{quote(repository, safe='/')}/pulls/{number}/reviews"
            )
            if not isinstance(pull_request_reviews, list):
                raise ValueError("GitHub returned an invalid pull request review list.")

    event = record_quarantine_worker_result(
        state_directory=args.state_dir,
        request=request,
        result=validated,
        recorded_at=args.recorded_at,
        pull_request_document=pull_request_document,
        pull_request_files=pull_request_files,
        pull_request_reviews=pull_request_reviews,
        mutation_result=mutation_result,
        commit_validation=commit_validation,
    )
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

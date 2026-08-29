#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.quarantine import (
    read_quarantine_session_events,
    record_quarantine_session_event,
    select_quarantine_session_request,
)


def _load_request(
    path: Path,
    state_directory: Path,
    batch_id: str | None,
) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Quarantine session plan must be an object.")
    proposal = document.get("proposal")
    if isinstance(proposal, dict) and (
        batch_id is None or proposal.get("batchId") == batch_id
    ):
        return proposal

    repository = document.get("repository")
    if not isinstance(repository, str) or not repository:
        raise ValueError("Quarantine session plan has no repository.")
    open_batch_ids = document.get("openBatchIds")
    if not isinstance(open_batch_ids, list):
        open_batch_ids = []
    candidate_batch_ids = (
        [batch_id]
        if isinstance(batch_id, str) and batch_id
        else [
            value
            for value in (document.get("activeBatchId"), *open_batch_ids)
            if isinstance(value, str) and value
        ]
    )
    candidate_batch_ids = list(dict.fromkeys(candidate_batch_ids))
    if len(candidate_batch_ids) != 1:
        raise ValueError(
            "Select exactly one existing quarantine batch with --batch-id."
        )
    selected_batch_id = candidate_batch_ids[0]
    latest = next(
        (
            event
            for event in reversed(read_quarantine_session_events(state_directory))
            if str(event.get("repository", "")).casefold()
            == repository.casefold()
            and event.get("batchId") == selected_batch_id
        ),
        None,
    )
    if latest is None:
        raise ValueError(f"Quarantine batch {selected_batch_id} was not found.")
    return latest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record the lifecycle of one approved quarantine worktree session."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument(
        "--status",
        choices=("started", "pull-request-open", "completed", "failed"),
        required=True,
    )
    parser.add_argument("--recorded-at", required=True)
    parser.add_argument("--session-id")
    parser.add_argument("--batch-id")
    parser.add_argument(
        "--test-name",
        help="Start a bounded trial containing only this exact proposed test.",
    )
    parser.add_argument("--pull-request-url")
    parser.add_argument("--completed-test", action="append")
    args = parser.parse_args()
    if args.test_name is not None and args.status != "started":
        parser.error("--test-name is valid only with --status started")

    old_umask = os.umask(0o077)
    try:
        request = _load_request(args.request, args.state_dir, args.batch_id)
        if args.test_name is not None:
            request = select_quarantine_session_request(request, args.test_name)
        session_id = args.session_id or request.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(
                "session-id is required when the selected request has no recorded session."
            )
        event = record_quarantine_session_event(
            args.state_dir,
            request,
            status=args.status,
            recorded_at=args.recorded_at,
            session_id=session_id,
            pull_request_url=args.pull_request_url,
            completed_test_names=args.completed_test,
        )
    finally:
        os.umask(old_umask)
    print(stable_json(event), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

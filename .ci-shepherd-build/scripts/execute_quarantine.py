#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path

from ci_shepherd.models import stable_json
from ci_shepherd.quarantine import record_quarantine_session_event
from ci_shepherd.quarantine_authorization import authorize_quarantine_start
from ci_shepherd.quarantine_mutation import (
    execute_quarantine_mutation,
    write_quarantine_validation,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Execute and validate one exactly authorized quarantine mutation batch."
        )
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old_umask = os.umask(0o077)
    try:
        now = datetime.now(timezone.utc)
        authorized = authorize_quarantine_start(
            request_path=args.request,
            authorization_path=args.authorization,
            state_dir=args.state_dir,
            batch_id=args.batch_id,
            now=now,
        )
        record_quarantine_session_event(
            args.state_dir,
            authorized.request,
            status="started",
            recorded_at=now.isoformat().replace("+00:00", "Z"),
            session_id=args.session_id,
            authorization_grant_id=authorized.grant_id,
        )
        try:
            result = execute_quarantine_mutation(
                authorized.request,
                args.checkout,
            )
            write_quarantine_validation(args.output, result)
        except (OSError, ValueError) as error:
            record_quarantine_session_event(
                args.state_dir,
                authorized.request,
                status="failed",
                recorded_at=(
                    datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                session_id=args.session_id,
                failure_reason=str(error),
            )
            raise
    finally:
        os.umask(old_umask)

    print(stable_json(result), end="")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())

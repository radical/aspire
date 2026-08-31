from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ci_shepherd.quarantine_authorization import (
    create_quarantine_grant,
    write_quarantine_grant,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create an exact short-lived grant for one quarantine batch."
    )
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lifetime-minutes", type=int, default=60)
    parser.add_argument("--test-name")
    parser.add_argument(
        "--purpose",
        choices=("start", "publication"),
        default="start",
    )
    args = parser.parse_args()
    grant = create_quarantine_grant(
        request_path=args.request,
        state_dir=args.state_dir,
        checkout=args.checkout,
        session_id=args.session_id,
        batch_id=args.batch_id,
        issued_at=datetime.now(timezone.utc),
        lifetime=timedelta(minutes=args.lifetime_minutes),
        test_name=args.test_name,
        grant_type=f"quarantine-{args.purpose}",
    )
    write_quarantine_grant(args.output, grant)
    print(json.dumps(grant, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

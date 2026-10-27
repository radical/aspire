"""Deliberately unresponsive fork-only child for cancellation/recovery trials."""

import json
import os
import signal
import time


if os.environ.get("GITHUB_REPOSITORY") != "radical/aspire":
    raise SystemExit("Fault fixture is restricted to the test fork")
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({
    "fixture": "deliberately-hung-worker-not-a-test",
    "run_id": os.environ["GITHUB_RUN_ID"],
    "source_sha": os.environ["GITHUB_SHA"],
    "pid": os.getpid(),
}), flush=True)
time.sleep(600)
raise SystemExit("Cancellation fixture unexpectedly exhausted its own sleep")

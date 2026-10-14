"""Standalone worker result encoder, embedded in the cloud prompt."""

import base64
import json
import os
from pathlib import Path
import sys


def unique_object(pairs):
    value = {}
    for key, entry in pairs:
        if key in value:
            raise ValueError("duplicate worker result key")
        value[key] = entry
    return value


def reject_constant(value):
    raise ValueError("nonfinite worker result value")


def main():
    source, output = map(Path, sys.argv[1:])
    value = json.loads(source.read_text(encoding="utf-8"),
                       object_pairs_hook=unique_object, parse_constant=reject_constant)
    value["worker_id"] = os.environ.get("COPILOT_AGENT_SESSION_ID") or "HOST_VERIFIED_SESSION_ID"
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > 16000:
        raise ValueError("worker result exceeds 16000 UTF-8 bytes")
    encoded = base64.b64encode(raw).decode("ascii")
    if json.loads(base64.b64decode(encoded, validate=True)) != value:
        raise ValueError("worker result round-trip failed")
    output.write_text(encoded + "\n", encoding="ascii")


if __name__ == "__main__":
    main()

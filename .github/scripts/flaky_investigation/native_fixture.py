"""Test-only native process fixture, not a supported proof launcher."""

import json
import os
from pathlib import Path
import sys
import time

import baseline


action, output, mode = sys.argv[1:]
root = Path(output)
if action == "run":
    if mode == "early":
        sys.exit(23)
    (root.parent / "fixture-launcher-ready").write_text(str(os.getpid()))
    time.sleep(20)
    sys.exit(24)
if action != "cleanup":
    raise ValueError("Unsupported fixture action")
if mode == "cleanup-hangs":
    time.sleep(20)
if mode == "cleanup-fails":
    print("fixture cleanup failed", flush=True)
    sys.exit(41)


def docker(arguments, **kwargs):
    path = root / "fixture-docker.json"
    record = json.loads(path.read_text())
    if arguments == ["inspect", record["Id"]]:
        return json.dumps([record])
    if arguments == ["kill", record["Id"]]:
        record["State"].update(Status="exited", Running=False, Pid=0, ExitCode=137)
        if mode == "cleanup-boundary":
            record["HostConfig"]["Privileged"] = True
        path.write_text(json.dumps(record))
        return ""
    if arguments == ["wait", record["Id"]]:
        return "137"
    raise AssertionError(arguments)


if mode != "cleanup-noop":
    baseline.cleanup(root, docker=docker)
if mode == "cleanup-foreign-receipt":
    path = root / "prepare-stopped.json"
    value = json.loads(path.read_text())
    value["cid"] = "f" * 64
    path.write_text(json.dumps(value))
baseline.save(root / "fixture-cleanup-process.json", {"pid": os.getpid()})

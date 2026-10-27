"""Unwired fork-only launcher-interruption experiment; never test-success evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

import baseline
from contracts import require


BASELINE_BLOB = "6b16127d2cc9fe31e0c1cf48bb0606a793fe7aa7"
READY_SECONDS = 900
POLL_SECONDS = .2
CLEANUP_SECONDS = 180
REAP_SECONDS = 5
# Baseline image output is bounded at 4 MiB before JSON escaping (up to 6x).
IMAGE_RECEIPT_BYTES = 32 * 1024 * 1024
PROTECTED = ("experiment.json", "image-receipt.json", "prepare-plan.json",
             "prepare.cid", "prepare-start-intent.json")
ERRORS = (ValueError, OSError, TimeoutError, subprocess.SubprocessError, KeyError, TypeError)


def verify_module():
    module = Path(baseline.__file__).resolve()
    content = baseline.read_regular(module, 65536)
    # Git blob identity is the frozen authority pin, not a mutable version label.
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    require(blob == BASELINE_BLOB, "The exact frozen v4 baseline module is required")
    return module


def protected(root):
    return {name: baseline.read_regular(
                root / name, IMAGE_RECEIPT_BYTES if name == "image-receipt.json" else 65536).decode()
            for name in PROTECTED if (root / name).exists()}


def preparation(root, identity):
    plan = json.loads(baseline.read_regular(root / "prepare-plan.json", 16384))
    image = json.loads(baseline.read_regular(root / "image-receipt.json", IMAGE_RECEIPT_BYTES))
    require(image["sdk"] == baseline.SDK and image["sdk_size"] == baseline.SDK_SIZE
            and image["sdk_sha512"] == baseline.SDK_SHA512 and image["base"] == baseline.BASE,
            "Unsupported preparation SDK/image receipt")
    require(plan["work"] == str(root / "work") and plan["image"] == image["id"],
            "Preparation must use the exact baseline work/image")
    for path in (root, root / "work"):
        owner = path.lstat()
        require(stat.S_ISDIR(owner.st_mode) and (owner.st_uid, owner.st_gid) == identity,
                "Protected output/work owner differs from the native host identity")
    require(root.lstat().st_mode & 0o077 == 0, "Output receipts must remain protected")
    stage = baseline.Stage(root, root / "work", "prepare", image["id"], plan["command"],
                           network=plan["network"], docker=baseline.docker)
    record = stage.inspect()
    stage.boundary(record)
    intent = json.loads(baseline.read_regular(root / "prepare-start-intent.json", 1024))
    require(intent == {"cid": record["Id"]}, "Missing or foreign preparation start intent")
    require(not (root / "prepare-stopped.json").exists()
            and not (root / "test-plan.json").exists(), "Preparation already completed")
    return stage, record


def reap_owned(child):
    """Abort disposal is not interruption evidence and never targets a PID input."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=REAP_SECONDS)


def run(output, archive, receipts):
    baseline.authorize()
    identity = baseline.host_identity()
    module = verify_module()
    require(output.is_absolute() and receipts.is_absolute() and archive.is_absolute(),
            "Absolute fixed experiment paths are required")
    require(not output.exists() and not output.is_symlink(),
            "Use a fresh baseline output directory")
    require(not receipts.is_relative_to(output) and not output.is_relative_to(receipts),
            "Proof receipts must be outside the baseline work/control mount")
    receipts.mkdir(mode=0o700)
    result = {"status": "error", "accepted_test_counts": None,
              "source": baseline.SOURCE, "sdk": baseline.SDK, "baseline_blob": BASELINE_BLOB,
              "launcher_pid": None, "launcher_returncode": None,
              "shell_equivalent_returncode": None, "early_launcher_returncode": None,
              "cleanup_pid": None, "cleanup_returncode": None, "error": None,
              "claim": "Owned launcher interruption only; no test-success, runner-loss, "
                       "job-timeout cleanup, candidate or publication claim"}
    launcher = None
    try:
        with (receipts / "launcher.log").open("xb") as log:
            launcher = subprocess.Popen(
                [sys.executable, "-B", str(module), "run", "--sdk-archive", str(archive),
                 "--output", str(output)], stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True)
            result["launcher_pid"] = launcher.pid
            deadline = time.monotonic() + READY_SECONDS
            while True:
                code = launcher.poll()
                if code is not None:
                    result["early_launcher_returncode"] = code
                    raise ValueError(f"Owned launcher exited before interruption readiness: {code}")
                if all((output / name).exists() for name in PROTECTED[1:]):
                    stage, before = preparation(output, identity)
                    if before["State"]["Running"] is True:
                        require(before["State"]["Status"] == "running"
                                and before["State"]["Pid"] > 0,
                                "Inconsistent preparation Running state")
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Bounded preparation readiness was never observed")
                time.sleep(POLL_SECONDS)
            result["before"] = before
            original = protected(output)
            baseline.save(receipts / "protected-before.json", original)
            baseline.save(receipts / "before.json", before)
            require(launcher.poll() is None, "Owned launcher exited before SIGKILL")
            # Only this Popen handle is signalled; no PID arguments, ps parsing,
            # name matching, process-group kill or container mutation is used here.
            launcher.kill()
            code = launcher.wait(timeout=REAP_SECONDS)
            result["launcher_returncode"] = code
            result["shell_equivalent_returncode"] = 128 - code if code < 0 else code
            require(code == -signal.SIGKILL, "Launcher did not retain native SIGKILL exit")
            after_death = stage.inspect()
            result["after_launcher_death"] = after_death
            baseline.save(receipts / "after-launcher-death.json", after_death)
            stage.boundary(after_death)
            require(after_death["Id"] == before["Id"]
                    and after_death["State"]["Running"] is True
                    and after_death["State"]["Status"] == "running"
                    and after_death["State"]["Pid"] > 0,
                    "Exact preparation container was not still active after launcher death")
            require(protected(output) == original,
                    "Protected preparation identity changed during interruption")
            with (receipts / "cleanup.log").open("xb") as cleanup_log:
                cleanup = subprocess.Popen(
                    [sys.executable, "-B", str(module), "cleanup", "--output", str(output)],
                    stdout=cleanup_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    start_new_session=True)
                result["cleanup_pid"] = cleanup.pid
                try:
                    cleanup_code = cleanup.wait(timeout=CLEANUP_SECONDS)
                    result["cleanup_returncode"] = cleanup_code
                    if cleanup_code:
                        raise subprocess.CalledProcessError(cleanup_code, cleanup.args)
                finally:
                    reap_owned(cleanup)
                    result["final_cleanup_returncode"] = cleanup.returncode
            after = stage.inspect()
            result["after_cleanup"] = after
            baseline.save(receipts / "after-cleanup.json", after)
            stage.boundary(after)
            require(after["Id"] == before["Id"]
                    and after["State"]["Running"] is False
                    and after["State"]["Pid"] == 0,
                    "Exact owned preparation container is not confirmed stopped")
            require(protected(output) == original,
                    "Protected preparation identity changed during cleanup")
            stopped = json.loads(baseline.read_regular(output / "prepare-stopped.json", 1024))
            require(stopped == {"cid": before["Id"], "stopped": True,
                                "exit_code": after["State"]["ExitCode"]},
                    "Cleanup receipt does not confirm the exact owned container stop")
            result["status"] = "owned-launcher-interruption-cleanup-confirmed"
    except ERRORS as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            if launcher is not None:
                reap_owned(launcher)
                result["final_launcher_returncode"] = launcher.returncode
        finally:
            baseline.save(receipts / "interruption.json", result)
    return result


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    baseline.authorize()
    require(Path(baseline.__file__).resolve() == Path(__file__).resolve().with_name("baseline.py"),
            "Install the reviewed helper beside the fixed baseline module")
    runner = Path(os.environ["RUNNER_TEMP"])
    require(runner.is_absolute() and runner.resolve() == runner,
            "Use the trusted absolute RUNNER_TEMP without symlink aliases")
    run(runner / "aspire-baseline-interruption-v1", runner / "baseline-sdk.tar.gz",
        runner / "aspire-baseline-interruption-proof-v1")


if __name__ == "__main__":
    main()

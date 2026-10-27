"""Disposable Linux boundary probe; never reports candidate files as test passes."""

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid


IMAGE = "mcr.microsoft.com/dotnet/sdk@sha256:9c7b6b263d9d4c2e4718e16dae6d75cd8f4b5ccf206f1e7d48a98eb429981b03"


def docker(*args, timeout=120, check=True):
    return subprocess.run(
        ["docker", *args], text=True, capture_output=True, timeout=timeout, check=check,
    )


def import_outputs(source, destination):
    """Import bounded regular files after execution stops, without dereferencing links."""
    if not stat.S_ISDIR(source.lstat().st_mode):
        raise ValueError("Output root must be a non-symlink directory")

    def traversal_error(error):
        raise error

    count = 0
    size = 0
    for current, directories, files in os.walk(
            source, followlinks=False, onerror=traversal_error):
        for name in directories:
            path = Path(current) / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError("Non-directory output entry")
        for name in files:
            path = Path(current) / name
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Output must be a single-link regular file")
                count += 1
                size += info.st_size
                if count > 32 or size > 2 * 1024 * 1024:
                    raise ValueError("Output exceeds bounded import")
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    data = stream.read(info.st_size + 1)
                if len(data) != info.st_size:
                    raise ValueError("Output changed during import")
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            finally:
                os.close(descriptor)
    return count


class Container:
    def __init__(self, workspace, label):
        self.label = label
        self.identity = docker(
            "create", "--init", "--label", f"aspire-boundary-proof={label}",
            "--user", "65534:65534", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", "none",
            "--read-only", "--pids-limit", "64", "--memory", "256m", "--cpus", "1",
            "--log-driver", "none",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
            "--mount", f"type=bind,src={workspace},dst=/work",
            "--workdir", "/work", IMAGE, "/bin/bash", "/work/candidate.sh",
        ).stdout.strip()
        if len(self.identity) != 64 or any(c not in "0123456789abcdef" for c in self.identity):
            raise ValueError("Container creation did not return an exact ID")

    def inspect(self):
        document = json.loads(docker("inspect", self.identity).stdout)[0]
        if document["Id"] != self.identity or document["Config"]["Labels"].get(
                "aspire-boundary-proof") != self.label:
            raise ValueError("Container ownership changed")
        return document

    def stop(self):
        document = self.inspect()
        if document["State"]["Running"]:
            docker("kill", self.identity)
        docker("wait", self.identity, timeout=30)
        document = self.inspect()
        if document["State"]["Running"] or document["State"]["Pid"] != 0:
            raise RuntimeError("Owned container termination is unconfirmed")
        return document

    def remove(self):
        self.stop()
        docker("rm", self.identity)

    def execute(self, seconds):
        started = time.monotonic()
        process = subprocess.Popen(
            ["docker", "start", "--attach", self.identity],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        buffers = [bytearray(), bytearray()]
        lock = threading.Lock()
        limit = threading.Event()

        def capture(stream, target):
            while chunk := stream.read(65536):
                with lock:
                    remaining = max(0, 1024 * 1024 - sum(map(len, buffers)))
                    target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limit.set()

        readers = [threading.Thread(target=capture, args=(stream, buffer), daemon=True)
                   for stream, buffer in zip([process.stdout, process.stderr], buffers)]
        for reader in readers:
            reader.start()
        timed_out = False
        deadline = time.monotonic() + seconds
        while process.poll() is None and not limit.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.05)
        document = self.stop()
        process.wait(timeout=30)
        for reader in readers:
            reader.join(timeout=30)
            if reader.is_alive():
                raise RuntimeError("Container output capture did not finish")
        return {"timed_out": timed_out, "exit_code": document["State"]["ExitCode"],
                "container_id": self.identity, "image": IMAGE,
                "image_id": document["Image"],
                "candidate_invocation": document["Config"]["Cmd"],
                "source_sha": os.environ.get("GITHUB_SHA"),
                "repository": os.environ.get("GITHUB_REPOSITORY"),
                "elapsed_seconds": time.monotonic() - started,
                "timeout_seconds": seconds, "attach_exit_code": process.returncode,
                "container_stopped": True, "output_limited": limit.is_set(),
                "stdout": buffers[0].decode("utf-8", errors="replace"),
                "stderr": buffers[1].decode("utf-8", errors="replace")}


def candidate(root, text):
    workspace = root / uuid.uuid4().hex
    workspace.mkdir(mode=0o777)
    workspace.chmod(0o777)
    (workspace / "candidate.sh").write_text(text)
    (workspace / "candidate.sh").chmod(0o644)
    output = workspace / "output"
    output.mkdir(mode=0o777)
    output.chmod(0o777)
    return workspace


def main(output):
    output.mkdir(parents=True, exist_ok=False)
    docker("version")
    docker("pull", IMAGE, timeout=300)
    label = uuid.uuid4().hex
    receipts = []
    with tempfile.TemporaryDirectory(prefix="aspire-boundary-") as directory:
        root = Path(directory)
        protected = root / "trusted"
        protected.mkdir()
        sentinel = protected / "receipt.json"
        sentinel.write_text('{"trusted":"host-only"}\n')
        original = sentinel.read_bytes()
        os.environ["ASPIRE_BOUNDARY_HOST_SECRET"] = "proof-only-not-a-credential"
        script = f"""set -eu
test "$(id -u)" = 65534
test ! -e /var/run/docker.sock
test ! -e /trusted
test -z "${{GH_TOKEN:-}}"
test -z "${{GITHUB_TOKEN:-}}"
test -z "${{GITHUB_ENV:-}}"
test -z "${{ASPIRE_BOUNDARY_HOST_SECRET:-}}"
if cat '{sentinel}' >/dev/null 2>&1; then exit 31; fi
if printf tampered >'{sentinel}' 2>/dev/null; then exit 32; fi
printf '<TestRun><ResultSummary outcome="Passed"/></TestRun>' > output/fabricated.trx
printf 'host-paths-and-credentials-inaccessible\\n'
"""
        workspace = candidate(root, script)
        container = Container(workspace, label)
        try:
            observation = container.execute(30)
            if (observation["exit_code"] != 0 or observation["timed_out"]
                    or observation["output_limited"]):
                raise RuntimeError(f"Isolation attack probe failed: {observation}")
            if sentinel.read_bytes() != original:
                raise RuntimeError("Protected host receipt changed")
            imported = output / "untrusted-candidate-output"
            imported.mkdir()
            count = import_outputs(workspace / "output", imported)
            if count != 1:
                raise ValueError("Fabricated output not retained as untrusted raw bytes")
            receipts.append({
                "scenario": "host-tampering-and-fabricated-report",
                "image": IMAGE, **observation, "imported_untrusted_files": count,
                "accepted_test_counts": None, "requires_independent_source_review": True,
            })
        finally:
            container.remove()

        workspace = candidate(
            root, f"set -eu\nrmdir output\nln -s '{protected}' output\n")
        container = Container(workspace, label)
        try:
            observation = container.execute(30)
            if observation["exit_code"] != 0:
                raise RuntimeError("Output-root replacement attack did not execute")
            imported = output / "root-link-must-not-be-imported"
            try:
                import_outputs(workspace / "output", imported)
            except ValueError as error:
                if "root" not in str(error):
                    raise
            else:
                raise RuntimeError("Output root dereferenced protected host files")
            if imported.exists():
                raise RuntimeError("Rejected output root left imported host files")
            receipts.append({"scenario": "output-root-symlink", **observation,
                             "rejected_before_import": True})
        finally:
            container.remove()

        workspace = candidate(root, "yes output-flood\n")
        container = Container(workspace, label)
        try:
            observation = container.execute(10)
            if not observation["output_limited"] or not observation["container_stopped"]:
                raise RuntimeError("Output flood was not bounded and stopped")
            receipts.append({
                "scenario": "output-flood",
                **{key: value for key, value in observation.items()
                   if key not in ("stdout", "stderr")},
                "captured_bytes": len(observation["stdout"].encode()) + len(
                    observation["stderr"].encode()),
            })
        finally:
            container.remove()

        # A detached descendant must die with the container even if the Docker
        # attach client is interrupted. This is not a test execution or pass.
        workspace = candidate(root, "setsid sleep 300 &\nsleep 300\n")
        container = Container(workspace, label)
        try:
            observation = container.execute(2)
            if not observation["timed_out"] or not observation["container_stopped"]:
                raise RuntimeError("Hung container was not timed out and confirmed stopped")
            receipts.append({"scenario": "hung-process-tree", **observation})
        finally:
            container.remove()

        workspace = candidate(root, "sleep 300\n")
        launcher = subprocess.Popen(
            [sys.executable, __file__, "--launch", str(workspace), "--label", label],
            stdout=subprocess.PIPE, text=True,
        )
        identity = launcher.stdout.readline().strip()
        launcher.kill()
        launcher.wait(timeout=30)
        container = Container.__new__(Container)
        container.identity, container.label = identity, label
        try:
            if not container.inspect()["State"]["Running"]:
                raise RuntimeError("Interruption probe did not start")
            recovery = subprocess.run(
                [sys.executable, __file__, "--cleanup", container.identity, "--label", label],
                text=True, capture_output=True, timeout=60, check=True,
            )
            if container.inspect()["State"]["Running"]:
                raise RuntimeError("Fresh cleanup process left owned work running")
            receipts.append({"scenario": "fresh-process-cleanup",
                             "launcher_killed": launcher.returncode < 0,
                             "container_stopped": True, "receipt": json.loads(recovery.stdout)})
        finally:
            container.remove()
        os.environ.pop("ASPIRE_BOUNDARY_HOST_SECRET")
    (output / "host-receipts.json").write_text(json.dumps(receipts, indent=2) + "\n")
    print(json.dumps(receipts, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup")
    parser.add_argument("--launch", type=Path)
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.launch:
        instance = Container(args.launch, args.label)
        docker("start", instance.identity)
        print(instance.identity, flush=True)
        time.sleep(300)
    elif args.cleanup:
        instance = Container.__new__(Container)
        instance.identity, instance.label = args.cleanup, args.label
        document = instance.stop()
        print(json.dumps({"owned_container": instance.identity, "stopped": True,
                          "image": IMAGE, "image_id": document["Image"],
                          "candidate_invocation": document["Config"]["Cmd"],
                          "source_sha": os.environ.get("GITHUB_SHA"),
                          "repository": os.environ.get("GITHUB_REPOSITORY")}))
    elif args.output:
        main(args.output)
    else:
        parser.error("--output or --cleanup is required")

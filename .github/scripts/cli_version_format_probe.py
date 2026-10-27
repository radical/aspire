"""One credential-free --help command; not a provider or inference test."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tarfile
import time
import uuid

ASSET_URL = "https://github.com/github/copilot-cli/releases/download/v1.0.85/copilot-linux-x64.tar.gz"
ASSET_SHA = "6f235cea897645eef67e8f480e36a15b5d9c9f7a6165ebc9b6b1c28afc8bcdda"
ASSET_BYTES = 97257547
BINARY_SHA = "d928a8f4620cf1047ada093b15b26dc3943b963d31f427b081ba1fafa586bfb0"
BINARY_BYTES = 166464320
IMAGE = "ghcr.io/github/gh-aw-firewall/agent:0.28.20@sha256:1bacef0f405d77999d01d8c00cb731222531b3f43ef9dc8312c513d6d9038bb1"
IMAGE_REPO_DIGEST = IMAGE.replace(":0.28.20@", "@")
LABEL = "io.github.cli-help-format.owner"
SOCKET = Path("/var/run/docker.sock")
CURL = "/usr/bin/curl"
LIMIT = 16384
VERSION_SECONDS = 60
# Allow the pinned CLI's 13,521-byte help output to fit within one bounded stream.
VERSION_SUFFIX_BYTES = 16384
VERSION_READ_LIMIT = 64 * 1024 * 1024
COMMAND = ["-i", "PATH=/usr/bin:/bin", "HOME=/home/awfuser", "TMPDIR=/tmp",
           "LANG=C.UTF-8", "/package/copilot", "--help"]
TMPFS = {"/tmp": "rw,nosuid,nodev,size=32m,mode=1777",
         # The pinned CLI extracts its ~157 MiB package cache under HOME before parsing options.
         # Its native addon needs executable mappings; override Docker's default noexec here only.
         # https://docs.docker.com/engine/storage/tmpfs/#options-for---tmpfs
         "/home/awfuser": "rw,exec,nosuid,nodev,size=256m,mode=0700,uid=1000,gid=1000"}


class Failure(Exception):
    pass


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise Failure("unsafe-directory")
    path.mkdir(mode=0o700)


def capture(argv, environment, seconds=15, limit=LIMIT, suffix_bytes=None, read_limit=None,
            cancellation=None):
    """Bound both pipes together; kill only this command's owned process group."""
    if cancellation is not None and cancellation():
        raise Failure("controller-terminated")
    process = subprocess.Popen(argv, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    selector = selectors.DefaultSelector()
    buffers = [bytearray(), bytearray()]
    observed = [0, 0]
    if suffix_bytes is not None:
        assert suffix_bytes > 0 and read_limit is not None and read_limit > 0
    for index, stream in enumerate([process.stdout, process.stderr]):
        selector.register(stream, selectors.EVENT_READ, index)
    deadline = time.monotonic() + seconds
    termination = "completed"
    normal_completion = False
    try:
        while selector.get_map() or process.poll() is None:
            if cancellation is not None and cancellation():
                termination = "cancelled"
                break
            if time.monotonic() >= deadline:
                termination = "timeout"
                break
            for key, _ in selector.select(0.02):
                size = min(65536, read_limit - sum(observed) + 1) if suffix_bytes is not None else 65536
                block = os.read(key.fileobj.fileno(), size)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                observed[key.data] += len(block)
                if suffix_bytes is not None:
                    # Long single-line diagnostics can put the actual error at the end.
                    # Retain only each stream's suffix; never accumulate the full output.
                    buffers[key.data].extend(block)
                    del buffers[key.data][:-suffix_bytes]
                    if sum(observed) > read_limit:
                        termination = "read-cap"
                        break
                else:
                    remaining = limit - sum(map(len, buffers))
                    buffers[key.data].extend(block[:remaining])
                    if len(block) > remaining:
                        termination = "output-cap"
                        break
            if termination != "completed":
                break
        if cancellation is not None and cancellation():
            termination = "cancelled"
        normal_completion = termination == "completed"
    finally:
        # Reaping releases the PID even if a signal interrupts completion bookkeeping.
        # Do not signal a numeric group ID after it can have been reused.
        if not normal_completion and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=3)
        selector.close()
        process.stdout.close()
        process.stderr.close()
    return {"termination": termination, "exit": process.returncode,
            "stdout": bytes(buffers[0]), "stderr": bytes(buffers[1]),
            "observed_bytes": dict(zip(["stdout", "stderr"], observed)),
            "suffix_offsets": dict(zip(["stdout", "stderr"],
                                      [seen - len(value) for seen, value in zip(observed, buffers)]))
            if suffix_bytes is not None else None,
            "truncated": any(seen > len(value) for seen, value in zip(observed, buffers))}


def checked(argv, environment, seconds=15, cancellation=None):
    result = capture(argv, environment, seconds, 262144, cancellation=cancellation)
    if result["termination"] == "cancelled":
        raise Failure("controller-terminated")
    if result["termination"] != "completed":
        raise Failure("control-" + result["termination"])
    if result["exit"] != 0:
        raise Failure("control-nonzero")
    return result["stdout"]


def package(archive, payload):
    info = archive.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_size != ASSET_BYTES
            or digest(archive) != ASSET_SHA):
        raise Failure("package-identity")
    payload.mkdir(mode=0o700)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if (len(members) != 1 or members[0].name != "copilot"
                or not members[0].isreg() or members[0].size != BINARY_BYTES):
            raise Failure("package-members")
        with source.extractfile(members[0]) as stream, (payload / "copilot").open("xb") as target:
            while block := stream.read(1024 * 1024):
                target.write(block)
    binary = payload / "copilot"
    if digest(binary) != BINARY_SHA:
        raise Failure("binary-identity")
    with binary.open("rb") as stream:
        header = stream.read(20)
    if header[:6] != b"\x7fELF\x02\x01" or int.from_bytes(header[18:20], "little") != 62:
        raise Failure("binary-platform")
    binary.chmod(0o555)
    payload.chmod(0o555)
    return {"package_sha256": ASSET_SHA, "package_bytes": info.st_size,
            "file": "copilot", "binary_sha256": BINARY_SHA, "binary_bytes": binary.stat().st_size,
            "elf": "ELF64 little endian EM_X86_64"}


class Docker:
    def __init__(self, executable, state, environment, cancellation):
        self.command = [str(executable), "--host", "unix://" + str(SOCKET),
                        "--config", str(state / "docker-config")]
        self.environment = environment
        self.nonce = uuid.uuid4().hex
        self.name = "cli-help-format-" + self.nonce
        self.cid = None
        self.image_id = None
        self.attempted = False
        self.cancellation = cancellation
        self.cleaning = False

    def call(self, *args, seconds=15):
        return checked([*self.command, *args], self.environment, seconds,
                       cancellation=None if self.cleaning else self.cancellation)

    def matches(self):
        raw = self.call("container", "ls", "--all", "--no-trunc", "--filter",
                        "name=^/" + self.name + "$", "--format", "{{.ID}}")
        ids = raw.decode("ascii").splitlines()
        if len(ids) > 1 or any(not re.fullmatch("[0-9a-f]{64}", value) for value in ids):
            raise Failure("ownership-query")
        return ids

    def owned(self, cid):
        value = json.loads(self.call("container", "inspect", cid))
        if not isinstance(value, list) or len(value) != 1:
            raise Failure("ownership-inspect")
        item = value[0]
        if (item["Id"] != cid or item["Name"] != "/" + self.name
                or item["Config"]["Labels"].get(LABEL) != self.nonce
                or item["Config"]["Image"] != IMAGE or item["Image"] != self.image_id
                or (self.cid is not None and cid != self.cid)):
            raise Failure("ownership-mismatch")
        return item

    def cleanup(self):
        self.cleaning = True
        if not self.attempted:
            return "not-created-no-create-attempt"
        ids = self.matches()
        if not ids:
            return "absence-confirmed"
        self.owned(ids[0])
        self.call("container", "rm", "--force", ids[0])
        if self.matches():
            raise Failure("cleanup-still-present")
        return "owned-container-removed-absence-confirmed"

    def create(self, payload):
        args = ["container", "create", "--name", self.name, "--label", LABEL + "=" + self.nonce,
                "--platform", "linux/amd64", "--pull", "never", "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--user", "1000:1000", "--workdir", "/tmp", "--ipc", "private",
                "--memory", "2g", "--memory-swap", "2g", "--cpus", "2", "--pids-limit", "256",
                "--shm-size", "16m", "--log-driver", "none", "--no-healthcheck",
                "--mount", f"type=bind,src={payload},dst=/package,readonly,bind-propagation=rprivate"]
        for path, options in TMPFS.items():
            args.extend(["--tmpfs", path + ":" + options])
        args.extend(["--entrypoint", "/usr/bin/env", IMAGE, *COMMAND])
        self.attempted = True
        raw = self.call(*args)
        cid = raw.decode("ascii").strip()
        if not re.fullmatch("[0-9a-f]{64}", cid):
            raise Failure("create-identity")
        self.cid = cid
        return self.owned(cid)


def isolated(item, payload):
    host = item["HostConfig"]
    config = item["Config"]
    binds = [value for value in item["Mounts"] if value["Type"] == "bind"]
    expected = (len(binds) == 1 and binds[0]["Source"] == str(payload)
                and binds[0]["Destination"] == "/package" and binds[0]["RW"] is False
                and binds[0]["Propagation"] == "rprivate")
    other_mounts = all(value["Type"] == "bind" or (
        value["Type"] == "tmpfs" and value["Destination"] in TMPFS) for value in item["Mounts"])
    if not (expected and other_mounts and host["NetworkMode"] == "none"
            and set(item["NetworkSettings"]["Networks"]) == {"none"}
            and host["ReadonlyRootfs"] is True and not host.get("Privileged")
            and not host.get("CapAdd") and host["CapDrop"] == ["ALL"]
            and host["SecurityOpt"] == ["no-new-privileges:true"]
            and not host.get("Binds") and not host.get("Devices")
            and host.get("PidMode", "") == "" and host["IpcMode"] == "private"
            and host["Memory"] == 2147483648 and host["MemorySwap"] == 2147483648
            and host["NanoCpus"] == 2000000000 and host["PidsLimit"] == 256
            and host["ShmSize"] == 16777216
            and host["LogConfig"]["Type"] == "none" and host["Tmpfs"] == TMPFS
            and config["Entrypoint"] == ["/usr/bin/env"] and config["Cmd"] == COMMAND
            and config["User"] == "1000:1000" and config["WorkingDir"] == "/tmp"
            and config["Healthcheck"]["Test"] == ["NONE"]
            and not config.get("OpenStdin") and not config.get("Tty")):
        raise Failure("container-isolation")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--docker", type=Path, default=Path("/usr/bin/docker"))
    args = parser.parse_args(argv)
    os.umask(0o077)
    ready_parent = args.ready_file.parent
    ready_info = ready_parent.stat()
    if (args.ready_file.resolve() != args.ready_file or not stat.S_ISDIR(ready_info.st_mode)
            or ready_info.st_uid != os.getuid()
            or stat.S_IMODE(ready_info.st_mode) != 0o700 or args.ready_file.exists()):
        raise Failure("unsafe-ready-boundary")
    directory(args.output)
    receipt = {"profile": "credential-free-linux-cli-help-tail-v2",
               "status": "rejected", "stage": "setup", "reason": "not-completed",
               "controller_exit_status": 70, "literal_command_exit_status": None,
               "capture_transport_exit_status": None, "capture_termination": "not-started",
               "cleanup": "not-created-no-create-attempt", "output_eligible": False,
               "inference_authorized": False, "provider_policy_budget_medium": "UNOBSERVED",
               "release_id": 389603482, "asset_id": 567012597, "asset_url": ASSET_URL,
               "expected_package_sha256": ASSET_SHA, "expected_package_bytes": ASSET_BYTES,
               "expected_binary_sha256": BINARY_SHA, "expected_binary_bytes": BINARY_BYTES,
               "verified_package": None, "required_platform": "linux/amd64",
               "verified_image_platform": None, "image_ref": IMAGE, "image_id": None,
               "command": ["/usr/bin/env", *COMMAND],
               "stream_scope": "last bounded suffix of each credential-free Docker attach stream, including possible client diagnostics; not necessarily complete CLI output",
               "output_byte_limit_combined": 2 * VERSION_SUFFIX_BYTES,
               "suffix_byte_limit_per_stream": VERSION_SUFFIX_BYTES,
               "observed_read_limit": VERSION_READ_LIMIT,
               "capture_truncated": None,
               "command_seconds": VERSION_SECONDS}
    docker = None
    captured = None
    cancellation_pending = False

    def interrupted(signum, frame):
        # Raising here can interrupt waitpid after reaping but before status bookkeeping.
        nonlocal cancellation_pending
        cancellation_pending = True

    def cancellation_requested():
        return cancellation_pending

    previous_signals = {value: signal.signal(value, interrupted) for value in [signal.SIGTERM, signal.SIGINT]}
    try:
        directory(args.state)
        for name in ["home", "docker-config"]:
            directory(args.state / name)
        environment = {"HOME": str(args.state / "home"), "PATH": "/usr/bin:/bin",
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        try:
            socket_mode = SOCKET.stat().st_mode
        except FileNotFoundError:
            raise Failure("unix-socket-required") from None
        if not stat.S_ISSOCK(socket_mode):
            raise Failure("unix-socket-required")
        docker = Docker(args.docker, args.state, environment, cancellation_requested)
        receipt["stage"] = "ownership-preflight"
        if docker.matches():
            raise Failure("container-name-preexists")
        receipt["stage"] = "package"
        archive = args.archive or args.state / "release.tar.gz"
        if args.archive is None:
            checked([CURL, "--disable", "--fail", "--location", "--silent", "--show-error",
                     "--proto", "=https", "--proto-redir", "=https",
                     "--max-time", "170", "--max-filesize", str(ASSET_BYTES),
                     "--output", str(archive), ASSET_URL], environment, 180,
                    cancellation=cancellation_requested)
        payload = args.state / "payload"
        receipt["verified_package"] = package(archive, payload)
        receipt["stage"] = "image"
        docker.call("image", "pull", "--quiet", "--platform", "linux/amd64", IMAGE, seconds=180)
        image, = json.loads(docker.call("image", "inspect", IMAGE))
        if (image["Os"] != "linux" or image["Architecture"] != "amd64"
                or IMAGE_REPO_DIGEST not in image["RepoDigests"] or image["Config"].get("Volumes")
                or not re.fullmatch("sha256:[0-9a-f]{64}", image["Id"])):
            raise Failure("image-identity")
        docker.image_id = image["Id"]
        receipt["image_id"] = image["Id"]
        receipt["verified_image_platform"] = "linux/amd64"
        receipt["stage"] = "container-create"
        item = docker.create(payload)
        receipt["container_id"] = docker.cid
        isolated(item, payload)
        receipt["stage"] = "literal-help-command"
        receipt["capture_termination"] = "unobserved-after-interruption"
        captured = capture([*docker.command, "container", "start", "--attach", docker.cid],
                           environment, VERSION_SECONDS, suffix_bytes=VERSION_SUFFIX_BYTES,
                           read_limit=VERSION_READ_LIMIT, cancellation=cancellation_requested)
        receipt.update(capture_termination=captured["termination"],
                       capture_transport_exit_status=captured["exit"],
                       captured_bytes={name: len(captured[name]) for name in ["stdout", "stderr"]},
                       observed_bytes=captured["observed_bytes"],
                       suffix_offsets=captured["suffix_offsets"],
                       capture_truncated=captured["truncated"])
        if captured["termination"] != "completed":
            if captured["termination"] == "cancelled":
                raise Failure("controller-terminated")
            receipt["controller_exit_status"] = 124 if captured["termination"] == "timeout" else 125
            raise Failure(captured["termination"])
        # Docker attach status is not substituted for the container command status.
        raw = docker.call("container", "wait", docker.cid).decode("ascii").strip()
        item = docker.owned(docker.cid)
        state = item["State"]
        if (not re.fullmatch(r"\d{1,3}", raw) or not 0 <= int(raw) <= 255
                or state["Status"] != "exited" or state["Running"] is not False
                or state["ExitCode"] != int(raw) or state["Error"]
                or not state["StartedAt"] or state["StartedAt"].startswith("0001-")
                or not state["FinishedAt"] or state["FinishedAt"].startswith("0001-")):
            raise Failure("command-exit-unobserved")
        status = int(raw)
        receipt["literal_command_exit_status"] = status
        if captured["exit"] not in (0, status):
            raise Failure("attach-transport-mismatch")
        receipt.update(status="help-command-completed", reason="exit-zero" if status == 0 else "cli-nonzero",
                       controller_exit_status=status)
    except (Failure, OSError, ValueError, KeyError, tarfile.TarError) as error:
        receipt["reason"] = str(error) if isinstance(error, Failure) else "setup-or-control-failed"
    finally:
        if docker is not None:
            try:
                receipt["cleanup"] = docker.cleanup()
            except (Failure, OSError, ValueError, KeyError):
                receipt["cleanup"] = "failed-or-unconfirmed"
                receipt["controller_exit_status"] = 70
        if cancellation_pending:
            receipt.update(status="rejected", reason="controller-terminated",
                           controller_exit_status=70)
        eligible = (receipt["status"] == "help-command-completed"
                    and receipt["cleanup"] == "owned-container-removed-absence-confirmed")
        if not eligible and receipt["status"] == "help-command-completed":
            receipt.update(status="rejected", reason="cleanup-not-confirmed", controller_exit_status=70)
        if eligible:
            for name in ["stdout", "stderr"]:
                path = args.output / (name + ".tail.log")
                with path.open("xb") as stream:
                    stream.write(captured[name])
            receipt["output_eligible"] = True
            receipt["files"] = {name: {"bytes": path.stat().st_size, "sha256": digest(path)}
                                for name, path in ((p.name, p) for p in args.output.iterdir())}
        (args.output / "receipt.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        # The host step may upload only after this fresh private completion marker.
        # A pre-existing/unsafe output directory never reaches this point.
        with args.ready_file.open("xb") as stream:
            stream.write(b"credential-free-artifact-complete\n")
        for value, handler in previous_signals.items():
            signal.signal(value, handler)
    return receipt["controller_exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())

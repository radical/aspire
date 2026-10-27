"""Unwired, fork-only Linux experiment. Never accepts or authorizes candidates."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import stat
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from contracts import require


SOURCE = "934fbbbe0942da5025abdfefa93edca1ad9ec633"
SDK = "11.0.100-rc.1.26425.128"
SDK_SIZE = 207623687
SDK_SHA512 = (
    "608518577aab9db33db92ebd74f0fc9bfbcc7b3d473619f4731aca2edba80af6"
    "c571a38d5be6994862fc4679f6f3d13db767712fcb6f70a7683974a18824a0cc"
)
BASE = "mcr.microsoft.com/dotnet/sdk@sha256:9c7b6b263d9d4c2e4718e16dae6d75cd8f4b5ccf206f1e7d48a98eb429981b03"
PROJECT = "tests/Aspire.Hosting.Tests/Aspire.Hosting.Tests.csproj"
CLASS = "Aspire.Hosting.Tests.SlimTestProgramTests"
METHOD = "TestProjectStartsAndStopsCleanly"
LABEL = "aspire.baseline-experiment"
IMAGE = re.compile(r"sha256:[0-9a-f]{64}")
CID = re.compile(r"[0-9a-f]{64}")
OWNERSHIP_CHECK = (
    "if ! test -O . || ! test -G . || ! test -O eng/common/dotnet-install.sh "
    "|| ! test -G eng/common/dotnet-install.sh; then\n"
    "printf '%s\\n' 'Unsupported work/source ownership mapping for non-root UID/GID' >&2\n"
    "exit 1\nfi\n"
)


def host_identity():
    uid, gid = os.getuid(), os.getgid()
    require(0 < uid <= 2147483647 and 0 < gid <= 2147483647
            and uid == os.geteuid() and gid == os.getegid(),
            "A matching real/effective non-root host UID/GID identity is required")
    return uid, gid


def authorize():
    require(os.environ.get("GITHUB_REPOSITORY") == "radical/aspire",
            "Experimental baseline execution requires radical/aspire")
    require(platform.system() == "Linux" and platform.machine() == "x86_64",
            "Only Linux x64 is supported; no platform or SDK fallback")
    host_identity()


def command(arguments, timeout=30, limit=1048576, env=None):
    """Bound combined output without leaving a blocked pipe-reader thread."""
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=env)
    output = bytearray()
    finished = False
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                selector.register(stream, selectors.EVENT_READ)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Command exceeded its finite deadline")
                for key, _ in selector.select(min(remaining, .2)):
                    data = os.read(key.fileobj.fileno(), min(8192, limit - len(output) + 1))
                    if not data:
                        selector.unregister(key.fileobj)
                    output.extend(data)
                    require(len(output) <= limit, "Command output exceeded its byte limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Command exceeded its finite deadline")
            process.wait(timeout=remaining)
            finished = True
        text = output.decode("utf-8", errors="replace")
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, arguments, output=text)
        return text
    finally:
        # Also terminate an inherited pipe holder when the launcher has exited.
        if not finished:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()
        process.stderr.close()


def docker(arguments, **kwargs):
    return command(["docker", *arguments], **kwargs)


def save(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_regular(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= maximum,
                "Only bounded, single-link regular files are importable")
        value = stream.read(maximum + 1)
        require(len(value) <= maximum, "File exceeded import byte limit")
        return value


class Stage:
    def __init__(self, root, work, name, image, arguments, *, network, docker=docker):
        require(name in {"prepare", "test"} and network == ("bridge" if name == "prepare" else "none"),
                "Unsupported experimental execution stage")
        require(IMAGE.fullmatch(image), "An immutable image ID is required")
        require(work.is_absolute() and not re.search(r"[,\n\r]", str(work)), "Invalid dedicated mount path")
        self.root, self.work, self.name, self.image = root, work, name, image
        self.arguments, self.network, self.docker = arguments, network, docker
        self.uid, self.gid = host_identity()

    def create(self):
        owner = self.work.lstat()
        require(stat.S_ISDIR(owner.st_mode) and (owner.st_uid, owner.st_gid) == (self.uid, self.gid),
                "Dedicated work owner must match the non-root execution identity")
        path = self.root / f"{self.name}-plan.json"
        require(not path.exists(), "A stage cannot be dispatched twice")
        nonce = uuid.uuid4().hex
        save(path, {
            "source": SOURCE, "image": self.image, "nonce": nonce,
            "network": self.network, "work": str(self.work), "command": self.arguments,
            "uid": self.uid, "gid": self.gid,
            "memory_bytes": 6442450944, "cpus": 2, "pids": 512,
            "accepted_test_counts": None,
        })
        self.docker([
            "create", "--cidfile", str(self.root / f"{self.name}.cid"),
            "--label", f"{LABEL}={nonce}", "--label", f"{LABEL}.source={SOURCE}",
            "--init", "--user", f"{self.uid}:{self.gid}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--network", self.network,
            "--read-only", "--pids-limit", "512", "--memory", "6g", "--memory-swap", "6g", "--cpus", "2",
            "--log-driver", "none", "--tmpfs", "/tmp:rw,nosuid,nodev,size=536870912",
            "--mount", f"type=bind,src={self.work},dst=/work",
            "--workdir", "/work/source",
            "--env", "HOME=/work/home", "--env", "DOTNET_CLI_HOME=/work/home",
            "--env", "NUGET_PACKAGES=/work/packages",
            # The SDK image inherits skip; schema generation needs adjacent package XML.
            # Set normal extraction before the first restore into the fresh private cache.
            # https://learn.microsoft.com/en-us/nuget/reference/cli-reference/cli-ref-environment-variables
            "--env", "NUGET_XMLDOC_MODE=none",
            "--env", "DOTNET_CLI_TELEMETRY_OPTOUT=1", "--env", "DOTNET_NOLOGO=1",
            "--env", "SkipDefaultTestFilters=true",
            "--env", "ASPIRE__TEST__DCPLOGBASEPATH=/work/dcp-logs",
            self.image, *self.arguments,
        ])
        descriptor = os.open(self.root / f"{self.name}.cid", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def inspect(self):
        plan = json.loads(read_regular(self.root / f"{self.name}-plan.json", 16384))
        cid = read_regular(self.root / f"{self.name}.cid", 65).decode().strip()
        require(CID.fullmatch(cid), "Invalid protected container identity")
        records = json.loads(self.docker(["inspect", cid]))
        require(isinstance(records, list) and len(records) == 1, "Missing exact container identity")
        record = records[0]
        require(record["Id"] == cid and record["Image"] == self.image == plan["image"]
                and plan["source"] == SOURCE and plan["work"] == str(self.work)
                and (plan.get("uid"), plan.get("gid")) == (self.uid, self.gid)
                and record["Config"]["User"] == f"{self.uid}:{self.gid}"
                and record["Config"]["Labels"].get(LABEL) == plan["nonce"]
                and record["Config"]["Labels"].get(LABEL + ".source") == SOURCE,
                "Container identity does not match the protected ownership plan")
        return record

    def boundary(self, record):
        config = record["HostConfig"]
        require(config["ReadonlyRootfs"] is True and config["Privileged"] is False
                and config["NetworkMode"] == self.network
                and config["CapDrop"] == ["ALL"]
                and "no-new-privileges" in config["SecurityOpt"]
                and config["Memory"] == config["MemorySwap"] == 6442450944
                and config["PidsLimit"] == 512
                and config["NanoCpus"] == 2000000000 and config["LogConfig"]["Type"] == "none",
                "Container execution boundary differs from the protected plan")
        mounts = record["Mounts"]
        require(len(mounts) == 1 and mounts[0]["Type"] == "bind"
                and mounts[0]["Source"] == str(self.work) and mounts[0]["Destination"] == "/work"
                and mounts[0]["RW"] is True, "Unexpected container mount")
        # Inspect the merged container Config.Env, not just the requested overrides.
        # Docker reports entries such as "NUGET_XMLDOC_MODE=none"; duplicates or
        # a bare "NUGET_XMLDOC_MODE" cannot establish an unambiguous extraction mode.
        environment = record["Config"].get("Env")
        require(isinstance(environment, list) and all(isinstance(item, str) for item in environment)
                and [item for item in environment if item.split("=", 1)[0] == "NUGET_XMLDOC_MODE"]
                == ["NUGET_XMLDOC_MODE=none"],
                "Container XML documentation mode must be exactly NUGET_XMLDOC_MODE=none")

    def stop(self):
        record = self.inspect()
        if record["State"]["Running"]:
            self.docker(["kill", record["Id"]])
            self.docker(["wait", record["Id"]], timeout=30)
        record = self.inspect()
        require(record["State"]["Running"] is False and record["State"]["Pid"] == 0,
                "Whole container is not confirmed stopped")
        receipt = {"cid": record["Id"], "stopped": True, "exit_code": record["State"]["ExitCode"]}
        path = self.root / f"{self.name}-stopped.json"
        if not path.exists():
            save(path, receipt)
        return receipt

    def execute(self, timeout):
        if not (self.root / f"{self.name}-plan.json").exists():
            self.create()
        record = self.inspect()
        self.boundary(record)
        print(f"{self.name}: NUGET_XMLDOC_MODE=none", flush=True)
        require(record["State"]["Status"] == "created", "A started container cannot be executed again")
        require(not (self.root / f"{self.name}-start-intent.json").exists(),
                "A start intent cannot be retried after interruption")
        save(self.root / f"{self.name}-start-intent.json", {"cid": record["Id"]})
        output = ""
        try:
            try:
                output = self.docker(["start", "--attach", record["Id"]],
                                     timeout=timeout, limit=16 * 1024 * 1024)
                require(self.inspect()["State"]["Status"] == "exited",
                        "Attach returned before command completion")
            except subprocess.CalledProcessError as error:
                # docker start --attach propagates the container command's exit.
                output = error.output
                state = self.inspect()["State"]
                require(state["Status"] == "exited" and state["ExitCode"] == error.returncode,
                        "Container start or attach failed independently of command execution")
        finally:
            stopped = self.stop()
        save(self.root / f"{self.name}-console.json", {"combined_output": output})
        return stopped


def cleanup(root, *, docker=docker):
    require(root.is_dir() and not root.is_symlink(), "Invalid protected receipt directory")
    for name in ("prepare", "test"):
        plan_path = root / f"{name}-plan.json"
        if plan_path.exists():
            plan = json.loads(read_regular(plan_path, 16384))
            Stage(root, Path(plan["work"]), name, plan["image"], plan["command"],
                  network=plan["network"], docker=docker).stop()


def import_outputs(root, destination, *, stopped, byte_limit=16 * 1024 * 1024):
    require(stopped, "Output import requires confirmed whole-container stop")
    require(stat.S_ISDIR(root.lstat().st_mode), "Output root must be a regular directory")
    files = {}
    directory_count = 0
    def failure(error):
        raise error
    for directory, directories, names in os.walk(root, followlinks=False, onerror=failure):
        directory_count += len(directories)
        require(directory_count <= 128, "Too many output directories")
        for name in directories:
            require(stat.S_ISDIR((Path(directory) / name).lstat().st_mode),
                    "Output directories cannot be symlinks")
        for name in names:
            require(len(files) < 32, "Too many output files")
            path = Path(directory) / name
            require(len(str(path.relative_to(root))) <= 1024, "Output path exceeded its limit")
            try:
                content = read_regular(path, byte_limit - sum(map(len, files.values())))
            except OSError as error:
                raise ValueError("Unsafe output file") from error
            files[str(path.relative_to(root))] = content
    destination.mkdir()
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return files


def observations(root, exit_code):
    reports = list(root.rglob("*.trx"))
    require(len(reports) == 1, "Exactly one original report is required")
    content = read_regular(reports[0], 16 * 1024 * 1024)
    require(b"<!DOCTYPE" not in content.upper() and b"<!ENTITY" not in content.upper(),
            "TRX entities are forbidden")
    tree = ET.fromstring(content)
    local = lambda node: node.tag.split("}")[-1]
    definitions = {}
    for node in tree.iter():
        if local(node) == "UnitTest":
            method = next((child for child in node if local(child) == "TestMethod"), None)
            require(node.get("id") and node.get("id") not in definitions, "Colliding TRX definitions")
            definitions[node.get("id")] = None if method is None else (
                method.get("className"), method.get("name"))
    outcomes, seen = [], set()
    for node in tree.iter():
        if local(node) == "UnitTestResult":
            require(definitions.get(node.get("testId")) in {(CLASS, METHOD), (CLASS, CLASS + "." + METHOD)},
                    "A foreign test executed")
            require(node.get("outcome") in {"Passed", "Failed"}, "Selected test did not execute")
            key = (node.get("executionId"), node.get("testId"))
            require(all(key) and key not in seen, "Duplicate original execution")
            seen.add(key)
            outcomes.append(node.get("outcome"))
    require(outcomes, "Zero tests are not execution evidence")
    require(exit_code == (2 if "Failed" in outcomes else 0), "Test process indicates infrastructure failure")
    return {"untrusted_outcomes": outcomes, "accepted_test_counts": None,
            "claim": "Imported test bytes are not assertion-authenticity or repair evidence"}


def source(root):
    host_identity()
    work = root / "work"
    work.mkdir(mode=0o700)
    work.chmod(0o700)
    checkout = work / "source"
    env = {"PATH": os.defpath, "HOME": str(root), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    git = ["git", "--no-pager", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null"]
    command([*git, "init", "--quiet", str(checkout)], env=env)
    command([*git, "-C", str(checkout), "fetch", "--quiet", "--depth=1", "--no-tags",
             "https://github.com/radical/aspire.git", SOURCE], env=env, timeout=180)
    command([*git, "-C", str(checkout), "checkout", "--quiet", "--detach", SOURCE], env=env)
    require(command([*git, "-C", str(checkout), "rev-parse", "HEAD"], env=env).strip() == SOURCE,
            "Unexpected baseline checkout")
    config = json.loads(read_regular(checkout / "global.json", 32768))
    require(config["sdk"]["version"] == SDK and config["tools"]["dotnet"] == SDK,
            "Unsupported baseline SDK; no fallback")
    # Match the checkout owner instead of widening write bits: Arcade chmods its
    # tracked installer, which requires ownership even when bytes are writable.
    # https://man7.org/linux/man-pages/man2/chmod.2.html
    return work


def image(root, archive):
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
    context = root / "image"
    context.mkdir()
    with os.fdopen(descriptor, "rb") as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
                and os.fstat(stream.fileno()).st_size == SDK_SIZE, "SDK archive size/type mismatch")
        digest = hashlib.sha512()
        with (context / "sdk.tar.gz").open("xb") as target:
            copied = 0
            while chunk := stream.read(1024 * 1024):
                copied += len(chunk)
                require(copied <= SDK_SIZE, "SDK archive grew while being read")
                digest.update(chunk)
                target.write(chunk)
        require(digest.hexdigest() == SDK_SHA512, "SDK vendor checksum mismatch")
    recipe = (f"FROM {BASE}\nCOPY sdk.tar.gz /tmp/sdk.tar.gz\n"
              "RUN tar -xzf /tmp/sdk.tar.gz -C /usr/share/dotnet && rm /tmp/sdk.tar.gz\n")
    (context / "Dockerfile").write_text(recipe)
    tag = "aspire-trusted-baseline:" + uuid.uuid4().hex
    log = docker(["build", "--network", "none", "--tag", tag, str(context)],
                 timeout=600, limit=4 * 1024 * 1024)
    records = json.loads(docker(["image", "inspect", tag]))
    require(len(records) == 1 and IMAGE.fullmatch(records[0]["Id"])
            and records[0]["Os"] == "linux" and records[0]["Architecture"] == "amd64",
            "Unsupported image platform or mutable identity")
    save(root / "image-receipt.json", {
        "id": records[0]["Id"], "base": BASE, "sdk": SDK, "sdk_sha512": SDK_SHA512,
        "sdk_size": SDK_SIZE, "recipe": recipe, "build_output": log,
    })
    return records[0]["Id"]


def run(root, archive):
    authorize()
    require(root.is_absolute(), "An absolute output directory is required")
    root = root.resolve()
    require(root.is_absolute() and not root.exists(), "Use a fresh absolute protected output directory")
    # The source/control checkout must never accidentally become the writable mount.
    control = Path(__file__).resolve().parents[3]
    require(not root.is_relative_to(control), "Output must be outside the control checkout")
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    save(root / "experiment.json", {
        "source": SOURCE, "repository": "radical/aspire", "sdk": SDK,
        "project": PROJECT, "method": CLASS + "." + METHOD,
        "claim": "Trusted baseline only; no candidate or publication authorization",
        "accepted_test_counts": None,
    })
    work = source(root)
    immutable_image = image(root, archive)
    prepare = [
        "bash", "-euc",
        OWNERSHIP_CHECK + "mkdir -p /work/home /work/packages /work/results /work/dcp-logs .dotnet\n"
        "cp -a /usr/share/dotnet/. .dotnet/\n"
        f'test "$(./dotnet.sh --version)" = "{SDK}"\n'
        # Arcade consumes this path from its SDK tools/Build.proj, not this cwd.
        f'./build.sh -restore -build -projects "$PWD/{PROJECT}" '
        "/p:InstallBrowsersForPlaywright=false /p:RunQuarantinedTests=true\n"
        "./dotnet.sh --list-runtimes\n",
    ]
    prepared = Stage(root, work, "prepare", immutable_image, prepare, network="bridge").execute(1800)
    require(prepared["exit_code"] == 0, "Trusted-baseline restore/build failed; no test stage started")
    test = [
        "bash", "-euc", OWNERSHIP_CHECK + f'test "$(./dotnet.sh --version)" = "{SDK}"\n'
        f"./dotnet.sh test --project {PROJECT} --no-launch-profile --no-restore --no-build "
        "--results-directory /work/results -- --timeout 8m "
        "--report-trx --report-trx-filename original.trx "
        f"--filter-method {CLASS}.{METHOD} --filter-not-trait outerloop=true\n",
    ]
    tested = Stage(root, work, "test", immutable_image, test, network="none").execute(600)
    import_outputs(work / "results", root / "imported", stopped=tested["stopped"])
    result = observations(root / "imported", tested["exit_code"])
    save(root / "observations.json", result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    execute = subcommands.add_parser("run")
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--sdk-archive", type=Path, required=True)
    stop = subcommands.add_parser("cleanup")
    stop.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    authorize()
    if arguments.action == "cleanup":
        cleanup(arguments.output)
    else:
        run(arguments.output, arguments.sdk_archive)


if __name__ == "__main__":
    main()

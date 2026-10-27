"""Credential-free startup profile; never a model/permission/readiness probe."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time

AGENT = "synthetic-credential-free-gateway-startup-v1"
MAX_BYTES = 262144
SECONDS = 600


def new_directory(path):
    path.mkdir(mode=0o700)
    if path.resolve() != path or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("directory boundary")


def clean_environment(state, runtime, workspace, toolcache, executables):
    home = state / "home"
    new_directory(home)
    scratch = state / "scratch"
    new_directory(scratch)
    return {
        "PATH": os.pathsep.join(dict.fromkeys([str(Path(executables[3]).parent)] +
                                            [str(Path(p).parent) for p in executables] +
                                            ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])),
        "HOME": str(home), "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "RUNNER_TEMP": str(runtime), "GITHUB_WORKSPACE": str(workspace),
        "RUNNER_TOOL_CACHE": str(toolcache), "GH_HOST": "github.com",
        "GH_AW_COMPILED_VERSION": "v0.89.17", "GH_AW_ENGINE": "copilot",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "", "OTEL_EXPORTER_OTLP_HEADERS": "",
        "GH_AW_OTLP_ENDPOINTS": "", "GITHUB_AW_OTEL_TRACE_ID": "0123456789abcdef0123456789abcdef",
        "GITHUB_AW_OTEL_PARENT_SPAN_ID": "0123456789abcdef", "GITHUB_OUTPUT": str(state / "native-outputs"),
        "CF_PYTHON": executables[0], "CF_NODE": executables[1],
        "CF_HELPER": str(Path(__file__).resolve()),
        "CF_STATE": str(state),
        "GITHUB_PATH": str(state / "native-path"), "GITHUB_ENV": str(state / "native-env"),
    }


def identity(file):
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or \
            stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("capture boundary")
    return info.st_dev, info.st_ino


def capture(command, environment, state, output, byte_limit=MAX_BYTES, seconds=SECONDS):
    """Execute producer and launcher with one clean environment; no parent interpolation."""
    new_directory(output)
    streams = []
    identities = {}
    for name in ["stdout.log", "stderr.log"]:
        file = output / name
        fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        streams.append(os.fdopen(fd, "wb", buffering=0))
        identities[name] = identity(file)
    process = subprocess.Popen(command, env=environment, cwd=environment["GITHUB_WORKSPACE"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, streams[0])
    selector.register(process.stderr, selectors.EVENT_READ, streams[1])
    start = time.monotonic()
    size = 0
    reason = "completed"
    try:
        while selector.get_map() or process.poll() is None:
            if time.monotonic() - start >= seconds:
                reason = "timeout"
                break
            for key, _ in selector.select(0.02):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = byte_limit - size
                key.data.write(chunk[:remaining])
                size += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    reason = "output_cap"
                    break
            if reason != "completed":
                break
        original_status = process.poll()
    finally:
        # This owns the producer/launcher group, not native's detached Docker
        # client. Production must separately complete fixed-name Docker cleanup.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        for stream in streams:
            os.fsync(stream.fileno())
            stream.close()
    for name in identities:
        if identity(output / name) != identities[name]:
            raise ValueError("capture identity")
    status = original_status if reason == "completed" else (124 if reason == "timeout" else 125)
    if status is None:
        raise ValueError("missing status")
    receipt = {
        "profile": "credential_free_static_empty_mcp_startup",
        "output_directory": str(output), "container_cleanup": "not_yet_confirmed",
        "synthetic_agent_id": AGENT, "inference_authorized": False,
        "termination": reason, "boundary_process_status": status,
        "underlying_process_status_before_forced_cleanup": original_status,
        "gateway_docker_exit_cause_readiness": "UNOBSERVED",
        "bytes": size, "byte_limit": byte_limit, "time_limit_seconds": seconds,
        "files": {name: {"bytes": (output / name).stat().st_size,
                         "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()}
                  for name in identities},
    }
    fd = os.open(output / "receipt.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return status


def eligible(output):
    if output.resolve() != output or set(p.name for p in output.iterdir()) != \
            {"stdout.log", "stderr.log", "receipt.json"}:
        raise ValueError("output set")
    info = output.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("output owner")
    receipt = json.loads(bounded_read(output / "receipt.json", 8192))
    if receipt["profile"] != "credential_free_static_empty_mcp_startup" or receipt["inference_authorized"] is not False or \
            receipt["output_directory"] != str(output):
        raise ValueError("receipt profile")
    for name in ["stdout.log", "stderr.log"]:
        data = bounded_read(output / name, MAX_BYTES)
        if receipt["files"][name] != \
                {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}:
            raise ValueError("receipt binding")
    return receipt


def bounded_read(file, maximum):
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError("bounded input") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or \
                before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > maximum:
            raise ValueError("bounded input")
        chunks = []
        size = 0
        while size <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if size > maximum or size != after.st_size or signature(before) != signature(after) or \
                identity(file) != (before.st_dev, before.st_ino):
            raise ValueError("bounded input")
        return b"".join(chunks)
    finally:
        os.close(fd)


def socket_environment(parent):
    import re
    host = parent.get("DOCKER_HOST", "")
    if parent.get("DOCKER_CONTEXT") or parent.get("DOCKER_CONFIG"):
        raise ValueError("explicit Docker context/config")
    config = Path(parent["HOME"]) / ".docker/config.json"
    if config.exists() and json.loads(config.read_text()).get("currentContext", "default") not in ("", "default") and not host:
        raise ValueError("non-default context")
    if host and not re.fullmatch(r"unix:///[A-Za-z0-9._/-]+", host):
        raise ValueError("non-unix daemon")
    endpoint = host.removeprefix("unix://") if host else "/var/run/docker.sock"
    socket_path = parent.get("GH_AW_DOCKER_SOCK_PATH") or endpoint
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", socket_path):
        raise ValueError("socket path")
    info = Path(socket_path).stat()
    endpoint_info = Path(endpoint).stat()
    if not stat.S_ISSOCK(info.st_mode) or not stat.S_ISSOCK(endpoint_info.st_mode) or \
            (info.st_dev, info.st_ino) != (endpoint_info.st_dev, endpoint_info.st_ino):
        raise ValueError("socket type")
    gid = parent.get("GH_AW_DOCKER_SOCK_GID", str(info.st_gid))
    if not gid.isdecimal() or int(gid) != info.st_gid:
        raise ValueError("socket group")
    result = {"DOCKER_HOST": host or "unix://" + endpoint,
              "GH_AW_DOCKER_SOCK_PATH": socket_path, "GH_AW_DOCKER_SOCK_GID": gid}
    if parent.get("XDG_RUNTIME_DIR"):
        directory = Path(parent["XDG_RUNTIME_DIR"])
        info = directory.lstat()
        if not directory.is_absolute() or directory.resolve() != directory or \
                not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("runtime directory")
        result["XDG_RUNTIME_DIR"] = str(directory)
    return result


def policy_environment(parent):
    # Pinned getOTLPIfMissingMode trims/lowercases warn/ignore and defaults to
    # error. Preserve absence; reject unknown supplied values, never select a
    # new effective policy for the diagnostic.
    if "GH_AW_OTLP_IF_MISSING" not in parent:
        return {}
    value = parent["GH_AW_OTLP_IF_MISSING"].strip().lower()
    if value not in {"error", "warn", "ignore"}:
        raise ValueError("missing OTLP policy")
    return {"GH_AW_OTLP_IF_MISSING": value}


def cleanup_container(docker, environment, state):
    def call(arguments):
        result = subprocess.run([docker, *arguments], env=environment, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=15)
        if result.returncode or len(result.stdout) > 4096 or len(result.stderr) > 4096:
            raise ValueError("cleanup transport")
        return result.stdout.decode().strip()
    if not (state / "preflight-ok").is_file():
        raise ValueError("no ownership preflight")
    found = call(["ps", "-a", "--filter", "name=^/awmg-mcpg$", "--format", "{{.ID}}"])
    if found:
        actual = call(["inspect", "--format", "{{.Id}} {{.Image}} {{.Name}}", "awmg-mcpg"]).split()
        expected = (state / "image-id").read_text().strip()
        if len(actual) != 3 or actual[1] != expected or actual[2] != "/awmg-mcpg":
            raise ValueError("container identity")
        call(["rm", "-f", actual[0]])
    if call(["ps", "-a", "--filter", "name=^/awmg-mcpg$", "--format", "{{.ID}}"]):
        raise ValueError("container remains")


def produce():
    # All interpolation is performed only after entering the clean child env.
    json.dump({"mcpServers": {}, "gateway": {
        "port": 8080, "domain": "awmg-mcpg", "agentId": AGENT,
        "payloadDir": "/tmp/gh-aw/mcp-payloads", "startupTimeout": 120,
        "opentelemetry": {"endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                          "traceId": os.environ.get("GITHUB_AW_OTEL_TRACE_ID", ""),
                          "spanId": os.environ.get("GITHUB_AW_OTEL_PARENT_SPAN_ID", "")}}}, sys.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--produce", action="store_true")
    parser.add_argument("--eligible", action="store_true")
    args = parser.parse_args()
    runtime = Path(os.environ["RUNNER_TEMP"]).resolve()
    output = runtime / "credential-free-gateway-startup"
    if args.produce:
        produce()
        return 0
    if args.eligible:
        eligible(output)
        return 0
    state = runtime / "credential-free-gateway-state"
    new_directory(state)
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    executables = [sys.executable, *[shutil.which(name) for name in ["node", "bash", "docker"]]]
    if any(not item or not Path(item).is_absolute() for item in executables):
        raise ValueError("executables")
    environment = clean_environment(state, runtime, workspace, Path(os.environ["RUNNER_TOOL_CACHE"]), executables)
    environment.update(socket_environment(os.environ))
    environment.update(policy_environment(os.environ))
    for name in ["native-outputs", "native-env", "native-path"]:
        (state / name).touch(mode=0o600)
    script = Path(__file__).with_name("credential_free_startup.sh")
    cleanup_status = "not_started_no_owned_container"
    try:
        status = capture([executables[2], str(script)], environment, state, output)
    finally:
        if (state / "preflight-ok").exists():
            try:
                cleanup_container(executables[3], environment, state)
                cleanup_status = "confirmed_absent"
            except BaseException:
                cleanup_status = "failed_or_unknown"
    receipt_file = output / "receipt.json"
    identity(receipt_file)
    receipt = json.loads(receipt_file.read_text())
    receipt["container_cleanup"] = cleanup_status
    receipt["controller_status"] = status if status != 0 or cleanup_status != "failed_or_unknown" else 126
    fd = os.open(receipt_file, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    eligible(output)
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write("eligible=true\n")
    return receipt["controller_status"]


if __name__ == "__main__":
    try:
        result = main()
    except BaseException:
        # Do not publish arbitrary exceptions or claim eligible partial files.
        print("credential_free_boundary_failed", file=sys.stderr)
        result = 126
    sys.exit(result if result >= 0 else min(255, 128 - result))

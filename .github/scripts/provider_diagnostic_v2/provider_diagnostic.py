"""Owned provider metadata only. No native model RPC, auth, or inference."""

import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time


MODEL = "gpt-6.1-sol"
MAX_BYTES = 1024 * 1024
IMAGE = ("ghcr.io/github/gh-aw-firewall/api-proxy:0.28.20"
         "@sha256:30ab6d3261dd95364281fa0b52ab78450e1c01aa074eccc3a8d4f04b12a6560b")
FIELDS = ("input", "output", "cachedInput", "cacheWrite")
PROXY_FAILURE_CODES = frozenset({
    "proxy-inspect-failed", "proxy-logs-failed", "proxy-discovery-failed",
    "proxy-listing-invalid", "proxy-identities-ambiguous",
    "proxy-container-mismatch", "proxy-mount-mismatch",
    "proxy-identity-changed", "proxy-cleanup-transport-failed",
    "proxy-observer-failed", "proxy-record-invalid",
})


class Rejected(Exception):
    def __init__(self, reason, *, failure_code=None):
        super().__init__(reason)
        self.failure_code = failure_code


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=pairs)


def clean_path(path):
    if not path.is_absolute() or ".." in path.parts:
        raise Rejected("snapshot-unsafe")
    for parent in (*reversed(path.parents), path):
        try:
            if stat.S_ISLNK(parent.lstat().st_mode):
                raise Rejected("snapshot-unsafe")
        except FileNotFoundError:
            continue


def read_regular(path, uid, limit=MAX_BYTES):
    clean_path(path)
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Walk anchored directory descriptors so a replaced parent cannot
        # redirect this read between the symlink check and the file open.
        for component in path.parts[1:-1]:
            next_directory = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory,
            )
            os.close(directory)
            directory = next_directory
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
    except Exception:
        os.close(directory)
        raise
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                or info.st_nlink != 1 or info.st_size > limit):
            raise Rejected("snapshot-unsafe")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (len(raw) > limit or info.st_ino != current.st_ino
                or info.st_dev != current.st_dev or
                (info.st_size, info.st_mtime_ns, info.st_ctime_ns) !=
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise Rejected("snapshot-unsafe")
        return bytes(raw), info
    finally:
        os.close(fd)
        os.close(directory)


def write_new(path, value):
    clean_path(path)
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")


def identity():
    values = {key: os.environ.get(env, "") for key, env in (
        ("run_id", "GITHUB_RUN_ID"), ("attempt", "GITHUB_RUN_ATTEMPT"),
        ("job", "GITHUB_JOB"), ("commit", "GITHUB_SHA"),
    )}
    if (not values["run_id"].isdigit() or not values["attempt"].isdigit()
            or values["job"] != "provider_diagnostic"
            or not re.fullmatch("[0-9a-f]{40}", values["commit"])):
        raise Rejected("intent-mismatch")
    return values


def prepare(args):
    clean_path(args.logs)
    # Never remove a snapshot or log directory to manufacture a fresh run.
    if os.path.lexists(args.logs / "models.json"):
        raise Rejected("snapshot-preexisting")
    if args.logs.exists() and any(args.logs.iterdir()):
        raise Rejected("logs-preexisting")
    write_new(args.state, {
        **identity(), "started_ns": time.time_ns(), "uid": os.geteuid(),
        "gid": os.getegid(), "logs": str(args.logs),
    })


def load_intent(args):
    try:
        raw, _ = read_regular(args.state, os.geteuid(), 16384)
        state = decode(raw)
        if not isinstance(state, dict):
            raise ValueError()
        if any(state.get(k) != v for k, v in identity().items()):
            raise ValueError()
        if (state.get("uid") != os.geteuid() or state.get("gid") != os.getegid()
                or state.get("logs") != str(args.logs)
                or type(state.get("started_ns")) is not int):
            raise ValueError()
        return state
    except (OSError, ValueError, TypeError):
        raise Rejected("intent-mismatch") from None


def timestamp(value, start, end):
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        ns = int(parsed.timestamp() * 1_000_000_000)
        if not start <= ns <= end:
            raise ValueError()
        return ns
    except (ValueError, OverflowError):
        raise Rejected("snapshot-stale") from None


def pricing(snapshot, start, end):
    try:
        if not isinstance(snapshot, dict):
            raise Rejected("snapshot-malformed")
        timestamp(snapshot.get("timestamp"), start, end)
        providers = snapshot.get("providers")
        if not isinstance(providers, dict):
            raise Rejected("snapshot-malformed")
        provider = providers.get("copilot")
        if not isinstance(provider, dict) or provider.get("configured") is not True:
            raise Rejected("selected-model-unknown")
        models = provider.get("models")
        if (not isinstance(models, list) or not all(isinstance(m, str) for m in models)
                or models.count(MODEL) != 1):
            raise Rejected("selected-model-unknown")
        metadata = provider.get("model_metadata")
        if not isinstance(metadata, list):
            raise Rejected("selected-model-unknown")
        exact = [m for m in metadata if isinstance(m, dict) and m.get("id") == MODEL]
        if len(exact) != 1 or exact[0].get("source") != "provider":
            raise Rejected("selected-model-unknown")
        timestamp(exact[0].get("observed_at"), start, end)
        value = exact[0].get("pricing")
        if (not isinstance(value, dict) or "default" not in value
                or set(value) - {"default", "long_context"}):
            raise Rejected("pricing-unknown")
        normalized = {}
        # AWF getRuntimeCatalogSnapshot emits pricing.default and optional
        # pricing.long_context, each with USD/million input/output/cachedInput/
        # cacheWrite and an optional positive threshold. Sol has no bundled
        # rate; missing fields must not be completed with invented prices.
        for name, tier in value.items():
            if (not isinstance(tier, dict)
                    or set(tier) - {*FIELDS, "threshold"}):
                raise Rejected("pricing-unknown")
            for key in FIELDS:
                number = tier.get(key)
                if (type(number) not in (float, int) or number < 0
                        or not math.isfinite(number)):
                    raise Rejected("pricing-unknown")
            if "threshold" in tier:
                number = tier["threshold"]
                if (type(number) not in (float, int) or number <= 0
                        or not math.isfinite(number)):
                    raise Rejected("pricing-unknown")
            normalized[name] = {key: tier[key] for key in (*FIELDS, "threshold")
                                if key in tier}
        return normalized
    except (TypeError, OverflowError):
        raise Rejected("pricing-unknown") from None


def bounded_command(command, limit=MAX_BYTES, seconds=3):
    """Only trusted host Docker metadata/log transport; discard all stderr."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    raw = bytearray()
    deadline = time.monotonic() + seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise Rejected("proxy-transport-unknown")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > limit:
                    raise Rejected("proxy-transport-unknown")
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise Rejected("proxy-transport-unknown")
        return bytes(raw)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def inspect_proxy(docker, state):
    # Docker ps takes options, not container operands. Its name filter is a
    # substring match, so compare the returned name and full ID ourselves.
    # https://docs.docker.com/reference/cli/docker/container/ls/#filter
    listing = bounded_command([
        docker, "ps", "--all", "--no-trunc", "--filter", "name=awf-api-proxy",
        "--format", "{{.ID}} {{.Names}}",
    ], 65536)
    identities = []
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2 or not re.fullmatch(rb"[0-9a-f]{64}", fields[0]):
            raise Rejected("proxy-transport-unknown", failure_code="proxy-listing-invalid")
        if fields[1] == b"awf-api-proxy":
            identities.append(fields[0].decode("ascii"))
    if not identities:
        return None
    if len(identities) != 1:
        raise Rejected("proxy-unattested", failure_code="proxy-identities-ambiguous")
    container_id = identities[0]
    # Docker's Go template selects no Env/Labels/auth or arbitrary account data.
    raw = bounded_command([
        docker, "inspect", "--format",
        '{"id":{{json .Id}},"image_id":{{json .Image}},'
        '"image_ref":{{json .Config.Image}},"user":{{json .Config.User}},'
        '"created":{{json .Created}},"mounts":{{json .Mounts}}}',
        container_id,
    ], 65536)
    data = decode(raw)
    if (data.get("id") != container_id
            or not re.fullmatch("sha256:[0-9a-f]{64}", data.get("image_id", ""))
            or data.get("image_ref") != IMAGE
            or data.get("user") != f'{state["uid"]}:{state["gid"]}'):
        raise Rejected("proxy-unattested", failure_code="proxy-container-mismatch")
    mounts = [m for m in data["mounts"] if m.get("Destination") == "/var/log/api-proxy"]
    if (len(mounts) != 1 or mounts[0].get("Type") != "bind"
            or mounts[0].get("Source") != state["logs"]
            or mounts[0].get("RW") is not True):
        raise Rejected("proxy-unattested", failure_code="proxy-mount-mismatch")
    created = timestamp(data["created"], state["started_ns"], time.time_ns())
    return {
        "id": data["id"], "image_id": data["image_id"], "image_ref": IMAGE,
        "host_logs": state["logs"], "container_logs": "/var/log/api-proxy",
        "uid": state["uid"], "gid": state["gid"], "created_ns": created,
    }


def proxy_absent(docker, container_id):
    # Require a successful list request covering all states for the observed
    # full CID. Nonzero/timeout/malformed transport is unknown, never absence.
    # https://docs.docker.com/reference/cli/docker/container/ls/
    raw = bounded_command([
        docker, "ps", "--all", "--no-trunc", "--filter", f"id={container_id}",
        "--format", "{{.ID}}",
    ], 65536)
    if not raw.strip():
        return True
    if raw.strip() == container_id.encode("ascii"):
        return False
    raise Rejected("proxy-transport-unknown")


def discovery(raw, start, end):
    success = False
    failure = False
    if raw is None:
        return False
    # Pinned logging.js emits JSONL: {"timestamp": "...", "event":
    # "model_validation", "requested_model": "gpt-6.1-sol",
    # "resolved_via": "direct", ...}. Ignore messages and all catalog contents.
    for line in raw.splitlines():
        if not line.strip():
            continue
        event = decode(line)
        timestamp(event.get("timestamp"), start, end)
        if event.get("event") in {
            "model_fetch_error", "model_validation_skipped",
            "model_unavailable_at_startup", "key_validation_error",
        }:
            failure = True
        if (event.get("event") == "model_validation"
                and event.get("requested_model") == MODEL
                and event.get("resolved_via") == "direct"):
            success = True
    return success and not failure


def watch(args):
    state = load_intent(args)
    write_new(args.state.with_suffix(".ready"), {"pid": os.getpid()})
    deadline = time.monotonic() + args.watch_seconds
    proxy = None
    observed = False
    seen = set()
    reason = None
    while time.monotonic() < deadline:
        phase = "proxy-inspect-failed"
        try:
            current = inspect_proxy(args.docker, state)
            if current is not None:
                seen.add(current["id"])
                if len(seen) != 1:
                    raise Rejected("proxy-unattested", failure_code="proxy-identity-changed")
                proxy = current
                phase = "proxy-logs-failed"
                raw = bounded_command([args.docker, "logs", current["id"]])
                if raw is not None:
                    phase = "proxy-discovery-failed"
                    observed = discovery(raw, state["started_ns"], time.time_ns())
        except Rejected as error:
            reason = reason or error.failure_code or phase
        except Exception:
            reason = reason or phase
        if args.state.with_suffix(".exit").exists():
            break
        time.sleep(0.2)
    try:
        exit_raw, _ = read_regular(args.state.with_suffix(".exit"), state["uid"], 16)
        if not re.fullmatch(rb"[0-9]{1,3}\n?", exit_raw):
            raise ValueError()
        exit_code = int(exit_raw)
    except Exception:
        exit_code = None
    removed = False
    try:
        if proxy is not None:
            removed = proxy_absent(args.docker, proxy["id"])
    except Exception:
        reason = reason or "proxy-cleanup-transport-failed"
    write_new(args.state.with_suffix(".observed.json"), {
        **state, "proxy": proxy, "proxy_observations": len(seen),
        "discovery_direct_success": observed, "observer_reason": reason,
        "awf_exit": exit_code, "completed_ns": time.time_ns(),
        "proxy_absent_after_return": removed,
    })


def finish(args):
    state = load_intent(args)
    deadline = time.monotonic() + args.wait_seconds
    observed_path = args.state.with_suffix(".observed.json")
    while not observed_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if not observed_path.exists():
        raise Rejected("awf-not-complete")
    raw, _ = read_regular(observed_path, os.geteuid(), 16384)
    observed = decode(raw)
    if any(observed.get(key) != value for key, value in state.items()):
        raise Rejected("intent-mismatch")
    state = observed
    end = state.get("completed_ns")
    if (state.get("awf_exit") != 0 or type(end) is not int
            or not state["started_ns"] <= end <= time.time_ns()):
        raise Rejected("awf-not-complete")
    # Only fixed codes leave the private observer state, never exception text
    # or Docker/provider output, even if an observed-state field is replaced.
    observer_reason = state.get("observer_reason")
    failure_code = None
    if observer_reason:
        failure_code = (observer_reason if isinstance(observer_reason, str)
                        and observer_reason in PROXY_FAILURE_CODES
                        else "proxy-observer-failed")
    if state.get("proxy_absent_after_return") is not True:
        raise Rejected("proxy-cleanup-unobserved", failure_code=failure_code)
    proxy = state.get("proxy")
    if (not isinstance(proxy, dict) or state.get("observer_reason")
            or state.get("proxy_observations") != 1
            or proxy.get("image_ref") != IMAGE
            or proxy.get("uid") != state["uid"] or proxy.get("gid") != state["gid"]
            or proxy.get("host_logs") != str(args.logs)
            or proxy.get("container_logs") != "/var/log/api-proxy"
            or not re.fullmatch("[0-9a-f]{64}", proxy.get("id", ""))
            or not re.fullmatch("sha256:[0-9a-f]{64}", proxy.get("image_id", ""))
            or not state["started_ns"] <= proxy.get("created_ns", 0) <= end):
        raise Rejected("proxy-unattested", failure_code=failure_code or "proxy-record-invalid")
    path = args.logs / "models.json"
    try:
        raw, info = read_regular(path, state["uid"])
    except FileNotFoundError:
        raise Rejected("snapshot-missing") from None
    except OSError:
        raise Rejected("snapshot-unsafe") from None
    if not state["started_ns"] <= info.st_mtime_ns <= end:
        raise Rejected("snapshot-stale")
    try:
        snapshot = decode(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise Rejected("snapshot-malformed") from None
    value = pricing(snapshot, state["started_ns"], end)
    if state.get("discovery_direct_success") is not True:
        raise Rejected("discovery-unobserved")
    version_path = args.version_file or args.state.with_suffix(".version")
    version_raw, _ = read_regular(version_path, state["uid"], 256)
    # The pinned release prints a terminal period followed by an update hint.
    if version_raw.strip() not in (
        b"1.0.85",
        b"GitHub Copilot CLI 1.0.85",
        b"GitHub Copilot CLI 1.0.85.\nRun 'copilot update' to check for updates.",
    ):
        raise Rejected("cli-version-unobserved")
    return {
        "selected_sol": {"model": MODEL, "configured_provider": "copilot",
                         "exact_membership": True, "discovery": "direct-success",
                         "pricing_complete": True, "pricing": value,
                         "pricing_unit": "USD-per-million-tokens"},
        "provenance": {
            **identity(), "started_ns": state["started_ns"], "completed_ns": end,
            "proxy": proxy, "snapshot_path": str(path),
            "snapshot": {"device": info.st_dev, "inode": info.st_ino,
                         "uid": info.st_uid, "gid": info.st_gid,
                         "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
                         "bytes": info.st_size},
            "awf_exit": 0, "cli_version": "1.0.85",
            "awf_version_pin": "0.28.20", "compiler_version_pin": "0.89.17",
            "mutable_mount_not_authenticated": True,
            "whole_workflow_completion": "requires-independent-authentication",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "watch", "finish"))
    parser.add_argument("--logs", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=float, default=20)
    parser.add_argument("--watch-seconds", type=float, default=840)
    parser.add_argument("--docker", default="/usr/bin/docker")
    parser.add_argument("--version-file", type=Path)
    args = parser.parse_args()
    result = {
        "status": "rejected", "model": MODEL, "selected_effort": "medium",
        "inference_authorized": False, "medium_transport": "unobserved",
        "medium_application": "unobserved", "native_policy": "unobserved",
        "budget_guard_acceptance": "unobserved", "inference": "unobserved",
    }
    try:
        if args.operation == "prepare":
            prepare(args)
            return 0
        if args.operation == "watch":
            if not 0 <= args.watch_seconds <= 840:
                raise Rejected("invalid-bound")
            watch(args)
            return 0
        if not 0 <= args.wait_seconds <= 30:
            raise Rejected("invalid-bound")
        result.update(finish(args))
        result["status"] = "provider-metadata-observed"
    except Rejected as error:
        result["reason"] = str(error)
        if error.failure_code in PROXY_FAILURE_CODES:
            result["failure_code"] = error.failure_code
    except Exception:
        result["reason"] = "metadata-unknown"
    if args.operation == "watch":
        return 1
    # Fixed allowlisted fields only. Never print raw exceptions, snapshot/log
    # fragments, arbitrary provider strings or Docker/engine stderr.
    try:
        write_new(args.receipt, result)
    except Exception:
        return 1
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        try:
            with open(output, "a", encoding="utf-8") as stream:
                stream.write("receipt_written=true\n")
        except OSError:
            return 1
    return 0 if result["status"] == "provider-metadata-observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

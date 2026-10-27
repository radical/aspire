"""Emit fixed startup metadata, never raw private capture contents."""

import argparse
import json
import os
from pathlib import Path
import stat


LIMIT = 1024 * 1024
SHELL_STAGES = {
    "prepare", "socket_resolution", "launcher_preparation",
    "launcher_pipeline", "complete",
}
# Exact fixed messages from the pinned setup/js/start_mcp_gateway.cjs.
# https://github.com/github/gh-aw-actions/blob/f3b81cdb3070066a47faa9bdf440f4f1dc78b1f0/setup/js/start_mcp_gateway.cjs
STAGES = [
    ("=== MCP Gateway Startup ===", "validation"),
    ("Reading MCP configuration from stdin...", "input_config"),
    ("Starting gateway container...", "spawn"),
    ("=== Health Check Progress ===", "health"),
    ("Reading gateway output configuration...", "read_output"),
    ("Converting gateway configuration to agent format...", "conversion"),
    ("Checking MCP server functionality...", "server_check"),
    ("Saving MCP CLI manifest...", "manifest"),
    ("Cleaning up gateway configuration file...", "config_cleanup"),
]
REASONS = {
    "ERROR: Gateway process stdin is not available": "stdin_unavailable",
    "ERROR: Failed to start gateway container": "spawn_failed",
    "ERROR: Gateway process exited immediately after start": "process_exited_early",
    "ERROR: Gateway failed to become ready": "health_failed",
    "ERROR: Gateway did not write output configuration": "output_missing",
    "ERROR: Gateway returned an error payload instead of configuration": "output_rejected",
    "ERROR: MCP server checks failed - no servers could be connected": "server_check_failed",
}


def read_capture(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_size > LIMIT:
            raise ValueError
        data = bytearray()
        while len(data) <= LIMIT:
            chunk = os.read(descriptor, min(65536, LIMIT + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if len(data) > LIMIT or identity(before) != identity(after):
            raise ValueError
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ValueError
        return text
    finally:
        os.close(descriptor)


def normalize(text, status):
    # shim.cjs:24-28 emits "[info] <message>\n" / "[error] <message>\n".
    # Match complete lines only. Captured data is mutable, not authenticated
    # control flow: these codes describe observed markers, not a proven cause.
    markers = {message: (index, stage) for index, (message, stage) in enumerate(STAGES)}
    last = -1
    node_stage = "unknown"
    errors = []
    ambiguous = False
    for line in text.splitlines():
        if line.startswith("[info] ") and line[7:] in markers:
            index, stage = markers[line[7:]]
            if index < last:
                ambiguous = True
            last, node_stage = index, stage
        elif line.startswith("[error] "):
            errors.append(REASONS.get(line[8:], "unknown"))
    if ambiguous:
        return "unknown", "unknown"
    reason = "exit_zero" if status == 0 else errors[0] if len(errors) == 1 else "unknown"
    return node_stage, reason


class PrivateArguments(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError


def main():
    record = {
        "boundary": "gateway_startup", "stage": "unknown",
        "last_logged_node_stage": "unknown", "reason": "unknown", "status": 1,
        "capture": "unknown", "inference_authorized": False,
    }
    result = 3
    try:
        parser = PrivateArguments(add_help=False)
        parser.add_argument("--log", required=True)
        parser.add_argument("--stage", required=True)
        parser.add_argument("--status", required=True, type=int)
        args = parser.parse_args()
        if not 0 <= args.status <= 255:
            raise ValueError
        record["stage"] = args.stage if args.stage in SHELL_STAGES else "unknown"
        record["status"] = args.status
        text = read_capture(Path(args.log))
        node_stage, reason = normalize(text, args.status)
        # A shell preparation failure must not be relabeled as Node activity.
        if record["stage"] not in {"launcher_pipeline", "complete"}:
            node_stage, reason = "unknown", "unknown"
        record.update(last_logged_node_stage=node_stage, reason=reason,
                      capture="bounded_regular_file")
        result = 0
    except (OSError, ValueError, UnicodeError):
        pass
    print(json.dumps(record, separators=(",", ":")))
    return result


if __name__ == "__main__":
    raise SystemExit(main())

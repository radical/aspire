"""Fresh restricted Copilot launcher and host-evidence validation."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import round as contracts


LOCAL_TOOLS = frozenset()
HOSTED_TOOLS = frozenset({"safeoutputs-submit_decision"})
PROVIDER_KEYS = frozenset({
    "COPILOT_PROVIDER_BASE_URL", "COPILOT_PROVIDER_TYPE", "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN", "COPILOT_PROVIDER_WIRE_API", "COPILOT_PROVIDER_MODEL_ID",
    "COPILOT_PROVIDER_WIRE_MODEL", "COPILOT_MODEL",
})
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
COPILOT_VERSION = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:-(?P<prerelease>\d+(?:\.[A-Za-z0-9-]+)*))?$"
)
MINIMUM_COPILOT_VERSION = (1, 0, 92, 3)


def copilot_version_supported(value):
    if not isinstance(value, str):
        return False
    value = value.removeprefix("GitHub Copilot CLI ").rstrip(".")
    match = COPILOT_VERSION.fullmatch(value)
    if match is None:
        return False

    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    minimum = MINIMUM_COPILOT_VERSION[:3]
    if version != minimum:
        return version > minimum

    prerelease = match.group("prerelease")
    return prerelease is None or int(prerelease.split(".", 1)[0]) >= MINIMUM_COPILOT_VERSION[3]


def render(template, **values):
    if set(PLACEHOLDER.findall(template)) != set(values):
        raise ValueError("prompt substitutions do not match the template")
    remainder = PLACEHOLDER.sub("", template)
    if "{{" in remainder or "}}" in remainder:
        raise ValueError("invalid prompt placeholder")
    if not all(isinstance(value, str) for value in values.values()):
        raise ValueError("prompt substitutions must be strings")
    return PLACEHOLDER.sub(lambda match: values[match.group(1)], template)


def child_environment(directory, provider):
    if set(provider) - PROVIDER_KEYS:
        raise ValueError("reasoning environment contains unauthorized credentials or configuration")
    home = Path(directory).resolve() / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    return {
        "PATH": os.defpath, "HOME": str(home), "COPILOT_HOME": str(home / ".copilot"),
        "XDG_CONFIG_HOME": str(home), "CI": "true", "GH_AW_HARNESS_MAX_RETRIES": "0",
        **provider,
    }


def jsonl(text):
    return [contracts.loads(line) for line in text.splitlines() if line.strip()]


def wire_reports(debug):
    reports = []
    # The CLI logs a multiline host-side request:
    # [DEBUG] [rust:model_wire] Wire request: {"copilotToolsFingerprint":
    #   {"count": 1, "deferred": 0, "tools": ["safeoutputs-submit_decision:ce1231c6"]}, ...}
    # The suffix is a schema fingerprint, not part of the tool name.
    marker = "[DEBUG] [rust:model_wire] Wire request: "
    for match in re.finditer(re.escape(marker), debug):
        try:
            request, _ = json.JSONDecoder().raw_decode(debug[match.end():])
        except (json.JSONDecodeError, RecursionError) as error:
            raise ValueError("malformed host model request report") from error
        if not isinstance(request, dict):
            raise ValueError("invalid host model request report")
        fingerprint = request.get("copilotToolsFingerprint")
        if not isinstance(fingerprint, dict) or not isinstance(fingerprint.get("tools"), list):
            raise ValueError("missing host effective tool fingerprint")
        reports.append(fingerprint)
    return reports


def effective_tools(debug):
    reports = []
    for fingerprint in wire_reports(debug):
        names = []
        for value in fingerprint["tools"]:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+:[0-9a-f]+", value):
                raise ValueError("invalid host effective tool fingerprint")
            names.append(value.rsplit(":", 1)[0])
        if type(fingerprint.get("count")) is not int or fingerprint["count"] != len(names):
            raise ValueError("inconsistent host effective tool count")
        if fingerprint.get("deferred") != 0 or len(names) != len(set(names)):
            raise ValueError("deferred or duplicate effective tools are not supported")
        reports.append(frozenset(names))
    return reports


def validate(events, session_id, *, debug="", hosted=False):
    allowed = HOSTED_TOOLS if hosted else LOCAL_TOOLS
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ValueError("invalid host event")
        if event["type"] != "result" and not isinstance(event.get("data"), dict):
            raise ValueError("missing host event data")
    starts = [event.get("data") for event in events if event["type"] == "session.start"]
    if len(starts) != 1 or not isinstance(starts[0], dict) or starts[0].get("sessionId") != session_id:
        raise ValueError("missing or mismatched fresh host session")
    if starts[0].get("alreadyInUse") is not False:
        raise ValueError("host session is not proven fresh")
    if not copilot_version_supported(starts[0].get("copilotVersion")):
        raise ValueError("unsupported Copilot host report version")
    results = [event for event in events if event["type"] == "result"]
    if (len(results) != 1 or results[0].get("sessionId") != session_id
            or type(results[0].get("exitCode")) is not int or results[0]["exitCode"] != 0):
        raise ValueError("missing or unsuccessful host process result")
    reports = effective_tools(debug)
    if not reports:
        raise ValueError("missing host effective tool report")
    if any(report != allowed for report in reports):
        raise ValueError("unauthorized effective tool grants")
    infos = [event.get("data", {}).get("message", "") for event in events if event["type"] == "session.info"]
    if not all(isinstance(info, str) for info in infos):
        raise ValueError("invalid host tool configuration report")
    if any(re.search(r"\b(unrestricted|all tools enabled)\b", info, re.IGNORECASE) for info in infos):
        raise ValueError("unrestricted host tool configuration")
    for event in events:
        if event["type"] != "assistant.message":
            continue
        requests = event["data"].get("toolRequests", [])
        if not isinstance(requests, list):
            raise ValueError("invalid host tool request report")
        for request in requests:
            if not isinstance(request, dict) or request.get("name") not in allowed:
                raise ValueError("unauthorized tool request")
    calls = [event.get("data") for event in events if event["type"] == "tool.execution_start"]
    for call in calls:
        if not isinstance(call, dict) or call.get("toolName") not in allowed:
            raise ValueError("unauthorized tool call")
        if not isinstance(call.get("arguments"), dict):
            raise ValueError("invalid tool call arguments")
    completions = [event.get("data", {}) for event in events if event["type"] == "tool.execution_complete"]
    if not hosted and completions:
        raise ValueError("unauthorized tool completion")
    messages = [
        event["data"]["content"] for event in events
        if event["type"] == "assistant.message"
        and not event.get("data", {}).get("toolRequests")
        and event.get("data", {}).get("content")
    ]
    if len(messages) != 1:
        raise ValueError("expected exactly one final decision")
    decision = contracts.loads(messages[0])
    if hosted:
        if len(calls) != 1:
            raise ValueError("expected exactly one safe-output decision call")
        if not isinstance(calls[0].get("toolCallId"), str) or not calls[0]["toolCallId"]:
            raise ValueError("missing host decision call identity")
        arguments = contracts.exact(calls[0].get("arguments"), {"decision"}, "decision call")
        if contracts.loads(arguments["decision"]) != decision:
            raise ValueError("safe-output call and final decision disagree")
        if len(completions) != 1 or completions[0].get("success") is not True:
            raise ValueError("safe-output decision transport did not complete")
        if completions[0].get("toolCallId") != calls[0].get("toolCallId"):
            raise ValueError("safe-output completion belongs to another call")
    return decision, {
        "schemaVersion": 1, "sessionId": session_id,
        "copilotVersion": starts[0]["copilotVersion"],
        "effectiveTools": sorted(reports[0]), "toolCalls": [call["toolName"] for call in calls],
        "processExitCode": 0, "effects": [],
    }


def validate_evidence(evidence, session_id, *, hosted=False):
    contracts.exact(evidence, {"schemaVersion", "sessionId", "events", "debug"}, "host evidence")
    if type(evidence["schemaVersion"]) is not int or evidence["schemaVersion"] != 1:
        raise ValueError("unsupported evidence schemaVersion")
    if not isinstance(evidence["events"], list) or not isinstance(evidence["debug"], str):
        raise ValueError("invalid host evidence")
    if evidence["sessionId"] != session_id:
        raise ValueError("mismatched host evidence session")
    return validate(evidence["events"], session_id, debug=evidence["debug"], hosted=hosted)


def validate_reconciliation_evidence(packet, decision, run, evidence):
    if not isinstance(evidence, dict) or not isinstance(evidence.get("sessionId"), str):
        raise ValueError("live reconciliation requires fresh host evidence")
    observed, report = validate_evidence(evidence, evidence["sessionId"], hosted=True)
    if observed != decision:
        raise ValueError("reconciliation decision differs from the actual host final decision")
    contracts.validate_reconciliation_decision(packet, observed, run)
    return report


def collect(session_root, logs, outcome, output):
    if outcome != "success":
        raise ValueError("host engine step did not succeed")
    session_files = list(Path(session_root).glob("*/events.jsonl"))
    if len(session_files) != 1:
        raise ValueError("expected exactly one fresh native session")
    session_file = session_files[0]
    session_id = session_file.parent.name
    events = [event for event in jsonl(session_file.read_text(encoding="utf-8")) if event["type"] in {
        "session.start", "assistant.message", "tool.execution_start", "tool.execution_complete", "session.info",
    }]
    # The exit result is host step evidence; it is never an agent-authored field.
    events.append({"type": "result", "sessionId": session_id, "exitCode": 0})
    debug = "\n".join(path.read_text(encoding="utf-8") for path in sorted(Path(logs).glob("*.log")))
    # Persist only the host's tool fingerprints, not prompts or authentication diagnostics.
    reports = wire_reports(debug)
    debug = "\n".join("[DEBUG] [rust:model_wire] Wire request: " + json.dumps({
        "copilotToolsFingerprint": report,
    }) for report in reports)
    evidence = {"schemaVersion": 1, "sessionId": session_id, "events": events, "debug": debug}
    contracts.write_json(output, evidence)
    return evidence


def execute(directory, packet, session_id, executable, *, process=None, provider_env=None):
    directory = Path(directory).resolve()
    directory.mkdir(mode=0o700)
    contracts.write_json(directory / "packet.json", packet)
    if provider_env is None:
        provider_env = {key: os.environ[key] for key in PROVIDER_KEYS if os.environ.get(key)}
        if not provider_env.get("COPILOT_PROVIDER_BASE_URL"):
            raise ValueError("no scoped inference provider configured; user GitHub tokens are not accepted")
    env = child_environment(directory, provider_env)
    executable = shutil.which(executable) or str(Path(executable).resolve())
    prompt = render((Path(__file__).parent / "prompts" / "round.md").read_text(), packet=json.dumps(packet))
    (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
    argv = [
        executable, "--no-auto-update", "--no-custom-instructions", "--disable-builtin-mcps",
        "--no-remote", "--no-remote-export", "--no-ask-user", "--disallow-temp-dir",
        "--available-tools", "--deny-tool", "view", "shell", "write", "task",
        "--max-ai-credits", "5", "--log-level", "all", "--log-dir", str(directory / "logs"),
        "--silent", "--output-format", "json", "--session-id", session_id,
        "--usage-output-file", str(directory / "usage.json"),
        "--secret-env-vars", "COPILOT_PROVIDER_API_KEY", "COPILOT_PROVIDER_BEARER_TOKEN",
        "-C", str(directory), "--prompt", prompt,
    ]
    try:
        result = (process or subprocess.run)(
            argv, cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False, timeout=600,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("fresh Copilot process exceeded the ten-minute timeout") from error
    for name, text in [("copilot.jsonl", result.stdout), ("stderr.txt", result.stderr)]:
        (directory / name).write_text(text, encoding="utf-8")
    if result.returncode != 0:
        raise ValueError(f"fresh Copilot process exited with status {result.returncode}; see agent/stderr.txt")
    events = jsonl(result.stdout)
    session_file = Path(env["COPILOT_HOME"]) / "session-state" / session_id / "events.jsonl"
    if session_file.exists():
        persisted = jsonl(session_file.read_text(encoding="utf-8"))
        # session.start is persisted but not streamed by Copilot 1.0.92-3.
        events = [event for event in persisted if event["type"] == "session.start"] + events
    debug = "\n".join(path.read_text(encoding="utf-8") for path in sorted((directory / "logs").glob("*.log")))
    return validate(events, session_id, debug=debug)

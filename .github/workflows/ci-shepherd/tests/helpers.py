import json
from pathlib import Path
import re
import shutil
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class WorkspaceTest:
    def setUp(self):
        self.work = Path("artifacts/ci-shepherd/tests") / str(uuid.uuid4())
        self.work.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.work)


def decision_for(packet):
    return {**packet, "outcome": "wait"}


def host_events(session_id, decision, calls=()):
    return [
        {"type": "session.start", "data": {"sessionId": session_id, "alreadyInUse": False, "copilotVersion": "1.0.92-3"}},
        {"type": "session.info", "data": {"message": "Disabled tools: bash, create, edit, apply_patch, task"}},
        *[{"type": "tool.execution_start", "data": call} for call in calls],
        *[{"type": "tool.execution_complete", "data": {"toolCallId": call.get("toolCallId"), "success": True}} for call in calls],
        {"type": "assistant.message", "data": {"toolRequests": [], "content": json.dumps(decision)}},
        {"type": "result", "sessionId": session_id, "exitCode": 0},
    ]


def wire_report(tools=()):
    return "2026-10-04T01:15:02.872Z [DEBUG] [rust:model_wire] Wire request: " + json.dumps({
        "copilotToolsFingerprint": {"count": len(tools), "deferred": 0, "tools": [name + ":ce1231c6" for name in tools]}
    }, indent=2)


def jsonl(events):
    return "\n".join(json.dumps(event) for event in events)


def compiled_step(name):
    workflow = Path(__file__).resolve().parents[2] / "ci-shepherd.lock.yml"
    for block in workflow.read_text().split("\n      - "):
        lines = ("        " + block).splitlines()
        if not any(line.strip() == "name: " + name for line in lines):
            continue
        environment = {}
        command = None
        for index, line in enumerate(lines):
            if line == "        env:":
                for entry in lines[index + 1:]:
                    if not entry.startswith("          "):
                        break
                    key, value = entry.strip().split(":", 1)
                    value = value.strip()
                    environment[key] = "" if value in {"''", '""'} else value
            if line.startswith("        run: "):
                value = line.removeprefix("        run: ")
                if value == "|":
                    body = []
                    for entry in lines[index + 1:]:
                        if entry and not entry.startswith("          "):
                            break
                        body.append(entry[10:])
                    command = "\n".join(body)
                else:
                    command = json.loads(value) if value.startswith('"') else value
        if command is None:
            raise ValueError("compiled step has no command")
        return {"env": environment, "run": command}
    raise ValueError("compiled step not found")


def compiled_environment(step, workspace, *, inputs=None):
    inputs = {} if inputs is None else inputs
    context = {
        "github.workspace": str(Path(workspace).resolve()),
        "github.token": "fixture-inference-token",
        "runner.temp": str(Path(workspace).resolve() / "runner"),
        "steps.agentic_execution.outcome": "success",
        "''": "",
        "inputs.mode || 'transport-proof'": inputs.get("mode", "transport-proof"),
        "inputs.resume_prepared && 'true' || 'false'": "true" if inputs.get("resume_prepared", False) else "false",
    }
    return {
        key: re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: context.get(match[1], "fixture-context"), value)
        for key, value in step["env"].items()
    }


def fixture_executable(directory):
    directory = Path(directory)
    source = Path(__file__).parent
    shutil.copyfile(source / "helpers.py", directory / "helpers.py")
    executable = directory / "fixture-copilot"
    executable.write_text(f"#!{sys.executable}\n" + (source / "fixture_process.py").read_text())
    executable.chmod(0o700)
    return str(executable.resolve())


class FakeProcess:
    def __init__(self, transform=lambda events: events, returncode=0, tools=(), report=True, output=None):
        self.transform = transform
        self.returncode = returncode
        self.launches = []
        self.tools = tools
        self.report = report
        self.output = output

    def __call__(self, argv, **kwargs):
        from subprocess import CompletedProcess
        self.launches.append((argv, kwargs))
        session = argv[argv.index("--session-id") + 1]
        packet = json.loads((Path(kwargs["cwd"]) / "packet.json").read_text())
        events = host_events(session, decision_for(packet))
        logs = Path(kwargs["cwd"]) / "logs"
        logs.mkdir()
        if self.report:
            (logs / "process-fixture.log").write_text(wire_report(self.tools))
        output = jsonl(self.transform(events)) if self.output is None else self.output
        return CompletedProcess(argv, self.returncode, output, "")


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 4, tzinfo=timezone.utc)
        self.readings = []

    def __call__(self):
        if self.readings:
            self.now = self.readings.pop(0)
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def subject(kind="pr", number=7):
    return {"repository": "owner/repo", "kind": kind, "number": number}


def observation(kind="pr"):
    root = subject(kind)
    return {
        "schemaVersion": 1, "root": root,
        "complete": {key: True for key in (
            "subjects", "feedback", "workers", "workersArchived", "workersUnarchived",
            "pullRequests", "history", "comments", "jobs",
        )},
        "subjects": [{
            "subject": root, "nodeId": "NODE7", "state": "open", "managed": True,
            "labels": ["shepherd-adopted"], "revision": "a" * 40 if kind == "pr" else "issue-revision-1",
            "feedback": [{"id": "review-1", "revision": "2026-10-04T00:00:00Z", "state": "open"}],
        }],
        "workers": [], "managedPullRequests": [7] if kind == "pr" else [],
        "history": {"recordIds": [], "publicationAttempts": [], "associatedOperationIds": []},
        "comments": [],
        "jobs": [{
            "subject": root, "headSha": "a" * 40, "runId": 10, "jobId": 20,
            "logicalJob": "tests / linux", "transient": True, "state": "completed",
        }] if kind == "pr" else [],
    }


RECONCILIATION_RUN = {
    "repository": "owner/repo", "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40,
}


def reconciliation_decision(packet, action="repair-pr", arguments=None):
    value = {
        "schemaVersion": 1, "subject": deepcopy(packet["subject"]), "basis": deepcopy(packet["basis"]),
        "action": action, "reason": "Use the current host observations.",
        "evidenceIds": [packet["evidence"][0]["id"]],
    }
    if action != "wait":
        defaults = {
            "repair-pr": {"feedbackIds": ["review-1"]}, "assign-issue": {},
            "adopt-pr": {"pullRequestNumber": 8},
            "rerun-transient": {"runId": 10, "jobId": 20, "logicalJob": "tests / linux"},
            "checkpoint": {},
        }
        value["arguments"] = deepcopy(defaults[action] if arguments is None else arguments)
    return value


def reconciliation_evidence(decision):
    session = str(uuid.uuid4())
    call = {
        "toolCallId": "decision-call", "toolName": "safeoutputs-submit_decision",
        "arguments": {"decision": json.dumps(decision)},
    }
    return {"schemaVersion": 1, "sessionId": session, "events": host_events(session, decision, [call]),
            "debug": wire_report(["safeoutputs-submit_decision"])}


class FakeGitHub:
    """Closed host snapshots; history is independent of comment visibility."""
    write_enabled = True
    actor = {"id": 100, "login": "shepherd[bot]"}

    def __init__(self, snapshot=None):
        import receipts
        self.snapshot = deepcopy(snapshot or observation())
        _, record = receipts.read_record(self.snapshot, self.actor)
        self.scope = receipts.TrialScope(self.snapshot["root"], None if record is None else receipts.trial_tuple(record))
        self.writes = []
        self.effects = []
        self.refreshes = 0
        self.before_refresh = None
        self.loss = None
        self.effect_loss = False
        self.effect_visible = False
        self.reject_effect = False

    def refresh(self, root):
        self.refreshes += 1
        if self.before_refresh:
            self.before_refresh(self, self.refreshes)
        return deepcopy(self.snapshot)

    def publish_status(self, root, body, comment_id, guard):
        from github import LostResponse
        guard()
        self.writes.append(("create" if comment_id is None else "edit", comment_id, body))
        record = body.split("<!-- ci-shepherd:root:v1 -->\n", 1)[1]
        trial_id = json.loads(record)["trialId"]
        self.snapshot["history"]["publicationAttempts"] = [trial_id]
        if self.loss == "create-before":
            self.loss = None
            raise LostResponse("status creation response unavailable")
        if comment_id is None:
            comment_id = 501
            self.snapshot["comments"].append({"id": comment_id, "user": deepcopy(self.actor), "body": body})
            self.snapshot["history"]["recordIds"].append(comment_id)
        else:
            next(comment for comment in self.snapshot["comments"] if comment["id"] == comment_id)["body"] = body
        if self.loss in {"create-after", "edit-after"}:
            self.loss = None
            raise LostResponse("status publication response unavailable")
        return {"id": comment_id}

    def execute(self, operation):
        from github import LostResponse, RejectedEffect
        self.effects.append(deepcopy(operation))
        if self.reject_effect:
            raise RejectedEffect("remote service established no effect")
        result = {"id": f"task-{len(self.snapshot['workers']) + 1}", "kind": "worker"} if operation["action"] in {"repair-pr", "assign-issue"} else {
            "id": "effect-1", "kind": "effect",
        }
        if self.effect_loss:
            if self.effect_visible:
                self.snapshot["workers"].append({
                    "id": result["id"], "state": "queued", "root": deepcopy(self.snapshot["root"]),
                    "operationId": operation["id"],
                })
            raise LostResponse("effect result unavailable")
        if result["kind"] == "worker":
            self.snapshot["workers"].append({
                "id": result["id"], "state": "queued", "root": deepcopy(self.snapshot["root"]),
                "operationId": operation["id"],
            })
        return result

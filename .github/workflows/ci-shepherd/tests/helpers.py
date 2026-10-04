import json
from pathlib import Path
import re
import shutil
import sys
import uuid

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


def compiled_environment(step, workspace):
    context = {
        "github.workspace": str(Path(workspace).resolve()),
        "github.token": "fixture-inference-token",
        "runner.temp": str(Path(workspace).resolve() / "runner"),
        "steps.agentic_execution.outcome": "success",
        "''": "",
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

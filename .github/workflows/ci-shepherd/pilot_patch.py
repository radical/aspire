"""A narrow trusted local profile; no executable commands come from decisions."""

import ast
import base64
from copy import deepcopy
import difflib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from github import LostResponse, Response
import pilot_github
import round as contracts

SOURCE = ".ci-shepherd-pilot/labels.py"
TEST = ".ci-shepherd-pilot/test_labels.py"
PROFILE = "python-labels-v1"
VALIDATION_ARGV = ["python3", "-B", "-m", "unittest", "discover", "-s", ".ci-shepherd-pilot", "-p", "test_*.py", "-v"]
IMAGE = "python:3.13-slim"
MAX_SOURCE = 16000


def pure_function(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError("local proposal is invalid Python") from error
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("local scope is one pure normalization function")
    function = tree.body[0]
    if (function.name != "normalize_label" or function.decorator_list
            or len(function.args.args) != 1 or function.args.defaults
            or function.args.kwonlyargs or function.args.posonlyargs or function.args.vararg or function.args.kwarg
            or len(function.body) != 1 or not isinstance(function.body[0], ast.Return)):
        raise ValueError("unsupported pure function shape")
    for annotation in (function.args.args[0].annotation, function.returns):
        if annotation is not None and not (isinstance(annotation, ast.Name) and annotation.id == "str"):
            raise ValueError("only inert str annotations are supported")
    parameter = function.args.args[0].arg
    # Only the parameter and chained zero-argument normalization methods are
    # eligible. No imports, globals or monkey-patching of the test runner.
    expression = function.body[0].value
    while isinstance(expression, ast.Call):
        if (expression.args or expression.keywords or not isinstance(expression.func, ast.Attribute)
                or expression.func.attr not in {"strip", "lower", "casefold", "upper"}):
            raise ValueError("unsupported normalization expression")
        expression = expression.func.value
    if not isinstance(expression, ast.Name) or expression.id != parameter:
        raise ValueError("normalization must return the input or its string normalization")
    return ast.dump(function.args), None if function.returns is None else ast.dump(function.returns)


def validate(proposal):
    contracts.exact(proposal, {"profile", "head", "operation", "files", "replacement"}, "local proposal")
    if proposal["profile"] != PROFILE or not re.fullmatch(r"[0-9a-f]{40}", proposal["head"]):
        raise ValueError("unsupported profile/head")
    if not isinstance(proposal["operation"], str) or not proposal["operation"]:
        raise ValueError("operation identity required")
    contracts.exact(proposal["files"], {SOURCE, TEST}, "profile files")
    for value in [*proposal["files"].values(), proposal["replacement"]]:
        if not isinstance(value, str) or not value or len(value.encode()) > MAX_SOURCE or "\x00" in value:
            raise ValueError("profile source bound exceeded")
    if pure_function(proposal["files"][SOURCE]) != pure_function(proposal["replacement"]):
        raise ValueError("local repair must preserve the function signature")
    if proposal["files"][SOURCE] == proposal["replacement"]:
        raise ValueError("empty local repair")
    changed = sum(line.startswith(("+", "-")) for line in difflib.unified_diff(
        proposal["files"][SOURCE].splitlines(), proposal["replacement"].splitlines()))
    if changed > 64:
        raise ValueError("local diff bound exceeded")
    return proposal


def source_context(api, observation):
    """Read exact-head files without checking out or executing PR code."""
    if observation["kind"] != "pr":
        return None
    files = api.api.pages(f"{pilot_github.PREFIX}/pulls/{observation['number']}/files")
    if not files or any(value["filename"] != SOURCE or value["status"] != "modified" for value in files):
        return None
    values = {}
    for path in (SOURCE, TEST):
        value = api.api.get(f"{pilot_github.PREFIX}/contents/{path}?ref={observation['head']}")
        if (value.get("type") != "file" or value.get("path") != path or value.get("encoding") != "base64"
                or type(value.get("size")) is not int or not 0 < value["size"] <= MAX_SOURCE):
            return None
        try:
            decoded = base64.b64decode(value["content"], validate=False).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("invalid exact-head source encoding") from error
        if len(decoded.encode()) != value["size"]:
            raise ValueError("exact-head source size mismatch")
        values[path] = decoded
    try:
        pure_function(values[SOURCE])
    except ValueError:
        return None
    return {"profile": PROFILE, "files": values}


def run_validation(proposal, directory, *, process=subprocess.run):
    validate(proposal)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pilot-validation-", dir=directory) as temporary:
        work = Path(temporary)
        work.chmod(0o755)
        (work / ".ci-shepherd-pilot").mkdir(mode=0o755)
        for name, content in proposal["files"].items():
            target = work / name
            target.write_text(proposal["replacement"] if name == SOURCE else content, encoding="utf-8")
            target.chmod(0o444)
        container = "ci-shepherd-validation-" + uuid.uuid4().hex
        argv = ["docker", "run", "--rm", "--name", container, "--network=none", "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=64", "--memory=128m", "--cpus=1",
                "--user=65534:65534", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
                "--mount", f"type=bind,source={work},target=/work,readonly", "--workdir=/work",
                IMAGE, *VALIDATION_ARGV]
        try:
            completed = process(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
                                timeout=90, env={"PATH": os.defpath})
            output = (completed.stdout + completed.stderr)[-4000:]
            exit_code = completed.returncode
        except subprocess.TimeoutExpired:
            # Killing the docker client alone does not stop its daemon-owned
            # container. Clean up only the uniquely named container we created.
            removed = process(["docker", "rm", "--force", container], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, check=False, timeout=15, env={"PATH": os.defpath})
            if removed.returncode != 0:
                raise ValueError("timed-out validation container cleanup failed")
            output, exit_code = "Isolated validation exceeded 90 seconds.", 124
    return {"schemaVersion": 1, "proposal": deepcopy(proposal), "argv": list(VALIDATION_ARGV),
            "exitCode": exit_code, "output": output}


def verify_evidence(proposal, evidence):
    validate(proposal)
    contracts.exact(evidence, {"schemaVersion", "proposal", "argv", "exitCode", "output"}, "validation evidence")
    if evidence["schemaVersion"] != 1 or evidence["proposal"] != proposal or evidence["argv"] != VALIDATION_ARGV:
        raise ValueError("validation does not bind exact patch bytes, head and trusted profile")
    if type(evidence["exitCode"]) is not int or evidence["exitCode"] != 0:
        raise ValueError("isolated validation failed")


def publish(api, chain, observation, proposal, evidence):
    verify_evidence(proposal, evidence)
    if proposal["head"] != observation["head"]:
        raise ValueError("validation head changed")
    operation = next(value for value in chain["operations"] if value["id"] == proposal["operation"])
    if operation["state"] != "reserved" or operation["lane"] != "local":
        raise ValueError("local publication reservation unavailable/replayed")
    context = source_context(api, observation)
    if context is None or context["files"] != proposal["files"]:
        raise ValueError("exact-head profile/source changed")
    api.guard(chain, observation)
    operation["state"] = "sent"
    api.persist()

    def create(kind, body):
        api.guard(chain, observation)
        response = api.transport("POST", pilot_github.PREFIX + "/git/" + kind, body)
        if not isinstance(response, Response) or response.status != 201 or not re.fullmatch(
            r"[0-9a-f]{40}", response.payload.get("sha", "")):
            raise LostResponse("immutable Git object creation outcome unknown; no retry")
        return response.payload["sha"]

    commit = api.api.get(pilot_github.PREFIX + "/git/commits/" + proposal["head"])
    blob = create("blobs", {"content": proposal["replacement"], "encoding": "utf-8"})
    tree = create("trees", {"base_tree": commit["tree"]["sha"], "tree": [
        {"path": SOURCE, "mode": "100644", "type": "blob", "sha": blob}]})
    new_head = create("commits", {"tree": tree, "parents": [proposal["head"]],
                                 "message": "Fix label normalization\n\n"
                                 "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>"})
    ref = api.api.get(pilot_github.PREFIX + "/git/ref/heads/" + observation["headRef"])
    if ref["object"]["sha"] != proposal["head"]:
        raise ValueError("branch head changed before non-forced publication")
    api.guard(chain, observation)
    response = api.transport("PATCH", pilot_github.PREFIX + "/git/refs/heads/" + observation["headRef"],
                             {"sha": new_head, "force": False})
    if not isinstance(response, Response) or response.status != 200 or response.payload.get("object", {}).get("sha") != new_head:
        raise LostResponse("branch publication unknown; no retry")
    operation["state"] = "completed"
    api.persist()
    return new_head

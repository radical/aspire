"""Operator-approved GitHub effects, separated from fresh packet-only reasoning."""

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
from urllib.parse import quote
import uuid

import local
import prompt_templates
import round as contracts


MAX_API_BYTES = 2 * 1024 * 1024
MAX_ACTIONS = 10
MAX_PAGES = 20
REPOSITORY_KEYS = {"name", "id", "nodeId"}
REQUEST_KEYS = {"schemaVersion", "requestId", "approved", "actor", "repository", "actions"}
PACKET_KEYS = {"schemaVersion", "kind", "packetId", "request", "bindings"}
# Same closing-reference grammar as work_item_execution.mapped_pr. Both final
# body and the complete chronological compare inventory must be inspected.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s*:?\s*"
    r"(?:[\w.-]+/[\w.-]+#\d+|#\d+|https://github\.com/[\w.-]+/[\w.-]+/issues/\d+)", re.I)
DEFAULT_STATE = Path.home() / ".local/state/shepherd-standalone"


class ApiError(ValueError):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"GitHub request rejected (HTTP {status or 'unknown'}); no credential fallback")


def bounded_command(argv, *, input, timeout, **unused):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = [bytearray(), bytearray()]
    outgoing = memoryview((input or "").encode("utf-8"))
    deadline = time.monotonic() + timeout
    try:
        # Drain both streams while writing stdin: pipe buffers must not create
        # an unbounded wait before the timeout can be enforced.
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            if outgoing:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
            else:
                process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _ in selector.select(remaining):
                    if key.data is None:
                        count = os.write(key.fileobj.fileno(), outgoing[:4096])
                        outgoing = outgoing[count:]
                        if not outgoing:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    buffer = buffers[key.data]
                    if len(buffer) + len(chunk) > (MAX_API_BYTES if key.data == 0 else 65536):
                        raise ValueError("GitHub command output exceeded bound")
                    buffer.extend(chunk)
            process.wait(timeout=max(0, deadline - time.monotonic()))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
    return subprocess.CompletedProcess(argv, process.returncode, *(bytes(b).decode("utf-8") for b in buffers))


class GhTransport:
    def __init__(self, *, process=bounded_command):
        self.process = process

    def call(self, method, endpoint, payload=None):
        if method not in {"GET", "POST", "PATCH"} or not re.fullmatch(r"[A-Za-z0-9_./:%?=&+-]+", endpoint):
            raise ValueError("invalid bounded GitHub transport request")
        argv = ["gh", "api", "--hostname", "github.com", "--method", method, endpoint]
        if payload is not None:
            argv += ["--input", "-"]
        # gh honors the selected environment credential before saved credentials.
        # One failed call is authoritative; never retry with another credential.
        result = self.process(
            argv, input=None if payload is None else json.dumps(payload, allow_nan=False),
            capture_output=True, text=True, check=False, timeout=45)
        if result.returncode:
            match = re.search(r"\bHTTP ([0-9]{3})\b", result.stderr)
            raise ApiError(int(match[1]) if match else None)
        return contracts.loads(result.stdout, max_bytes=MAX_API_BYTES)


def text(value, label, pattern=None, limit=65536):
    if not isinstance(value, str) or not value or len(value.encode()) > limit or "\0" in value:
        raise ValueError(f"invalid {label}")
    if pattern and re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {label}")
    return value


def positive(value, label):
    if type(value) is not int or value <= 0:
        raise ValueError(f"invalid {label}")


def repository(value):
    contracts.exact(value, REPOSITORY_KEYS, "repository identity")
    text(value["name"], "repository name", r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", 200)
    positive(value["id"], "repository id")
    text(value["nodeId"], "repository node", limit=200)


def marker(request_id, action_id):
    return f"<!-- shepherd-standalone:{request_id}:{action_id} -->"


def validate_request(value):
    contracts.exact(value, REQUEST_KEYS, "trusted request")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1 or value["approved"] is not True:
        raise ValueError("request requires schemaVersion 1 and explicit approval of final bytes")
    text(value["requestId"], "request id", r"[0-9a-f-]{36}")
    if str(uuid.UUID(value["requestId"])) != value["requestId"]:
        raise ValueError("requestId must be a canonical UUID")
    contracts.exact(value["actor"], {"login", "id"}, "actor")
    positive(value["actor"]["id"], "actor id")
    text(value["actor"]["login"], "actor login", r"[A-Za-z0-9_-]+", 100)
    repository(value["repository"])
    if not isinstance(value["actions"], list) or not 1 <= len(value["actions"]) <= MAX_ACTIONS:
        raise ValueError("request requires one to ten actions")
    identities = set()
    for action in value["actions"]:
        if not isinstance(action, dict):
            raise ValueError("invalid action")
        kind = action.get("kind")
        if kind == "draft-pr":
            keys = {"id", "kind", "headRepository", "base", "head", "expectedBaseSha",
                    "expectedHeadSha", "title", "body", "draft"}
        elif kind in {"issue-comment", "pr-comment"}:
            keys = {"id", "kind", "subject", "body"}
        elif kind == "close-issue":
            keys = {"id", "kind", "subject", "reason"}
        else:
            raise ValueError("unsupported action kind")
        contracts.exact(action, keys, "approved action")
        text(action["id"], "action id", r"[a-z][a-z0-9-]{0,63}")
        if action["id"] in identities:
            raise ValueError("duplicate action id")
        identities.add(action["id"])
        if kind == "draft-pr":
            repository(action["headRepository"])
            for key in ("base", "head"):
                ref = text(action[key], "explicit branch", r"[A-Za-z0-9_./+-]+", 200)
                if (ref.startswith("/") or ref.endswith(("/", ".")) or "//" in ref or ".." in ref or
                        any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))):
                    raise ValueError("invalid branch")
            for key in ("expectedBaseSha", "expectedHeadSha"):
                text(action[key], "expected SHA", r"[0-9a-f]{40}")
            text(action["title"], "final title", limit=256)
            if "\n" in action["title"] or action["draft"] is not True:
                raise ValueError("only explicit draft PRs are supported")
        else:
            subject = contracts.exact(
                action["subject"], {"number", "nodeId", "revision"} |
                ({"headSha"} if kind == "pr-comment" else set()), "subject")
            positive(subject["number"], "subject number")
            text(subject["nodeId"], "subject node", limit=200)
            text(subject["revision"], "subject revision", limit=100)
            if kind == "pr-comment":
                text(subject["headSha"], "subject head SHA", r"[0-9a-f]{40}")
            if kind == "close-issue" and action["reason"] not in {"completed", "not_planned"}:
                raise ValueError("issue close reason must be completed or not_planned")
        if "body" in action:
            body = text(action["body"], "approved final body")
            if body.count(marker(value["requestId"], action["id"])) != 1:
                raise ValueError("final body requires exactly one stable operation marker before approval")
            if kind != "draft-pr" and not body.startswith("[automated] "):
                raise ValueError("approved comment body must already have the automated prefix")
            if kind == "draft-pr" and CLOSING.search(body):
                raise ValueError("closing PR reference is not authorized")
    # Updating the same subject twice makes the second child's basis stale.
    subjects = [(a["subject"]["number"]) for a in value["actions"] if "subject" in a]
    if len(subjects) != len(set(subjects)):
        raise ValueError("multiple actions for the same subject require separate requests")
    return value


def secure_path(path, *, directory=False, create=False):
    path = Path(os.path.abspath(path))
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise ValueError("symlink operator paths are unsupported")
        if ancestor.exists() and ancestor.stat().st_mode & 0o022:
            raise ValueError("operator path has a group/world-writable ancestor")
    if create:
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.exists():
        raise ValueError("missing operator state; initialize only for a genuinely new request after reconciliation")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("operator file/directory must be private and owned by the current user")
    if directory != path.is_dir() or not directory and (not path.is_file() or info.st_nlink != 1):
        raise ValueError("invalid operator path type")
    return path


def read_request(path):
    return validate_request(contracts.read_json(secure_path(path)))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(packet):
    # Security integrity binding, not a content-addressing optimization. The
    # operator journal is trusted; this does not authenticate an operator's origin.
    return hashlib.sha256(canonical(packet).encode()).hexdigest()


def durable_json(path, value, *, replace=False):
    path = Path(path)
    secure_path(path.parent, directory=True)
    target = path.with_name(path.name + ".next") if replace else path
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if replace:
        os.replace(target, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def request_lock(root, request_id):
    root = secure_path(root, directory=True)
    # Reuse the controller's nonblocking POSIX single-host authority lock.
    with local.authority_lock(root, request_id):
        yield


def load_state(root, request):
    folder = secure_path(Path(root) / request["requestId"], directory=True)
    saved = contracts.read_json(secure_path(folder / "request.json"))
    if saved != request:
        raise ValueError("trusted request changed; existing request identity cannot be reused")
    packet = contracts.read_json(secure_path(folder / "packet.json"))
    journal = contracts.read_json(secure_path(folder / "journal.json"))
    contracts.exact(packet, PACKET_KEYS, "canonical packet")
    contracts.exact(journal, {"schemaVersion", "packetId", "packetDigest", "actions"}, "journal")
    if (packet["schemaVersion"] != 1 or packet["kind"] != "standalone-actions" or
            journal["schemaVersion"] != 1 or packet["request"] != request or
            journal["packetId"] != packet["packetId"] or journal["packetDigest"] != digest(packet)):
        raise ValueError("canonical packet integrity mismatch")
    ids = {a["id"] for a in request["actions"]}
    contracts.exact(packet["bindings"], ids, "packet action bindings")
    contracts.exact(journal["actions"], ids, "journal action bindings")
    for entry in journal["actions"].values():
        contracts.exact(entry, {"status", "receipt", "error"}, "action journal")
        if entry["status"] not in {"pending", "sending", "confirmed", "uncertain"}:
            raise ValueError("invalid dispatch state")
    return folder, packet, journal


def require_identity(api, request):
    actor = api.call("GET", "user")
    if not isinstance(actor, dict) or any(actor.get(k) != v for k, v in request["actor"].items()):
        raise ValueError("authenticated actor mismatch")
    return require_repository(api, request["repository"], writable=True)


def require_repository(api, expected, *, writable=False):
    observed = api.call("GET", "repos/" + expected["name"])
    if (not isinstance(observed, dict) or observed.get("full_name") != expected["name"] or
            observed.get("id") != expected["id"] or observed.get("node_id") != expected["nodeId"]):
        raise ValueError("repository identity mismatch")
    permissions = observed.get("permissions")
    if writable and permissions is not None:
        if not isinstance(permissions, dict) or type(permissions.get("push")) is not bool:
            raise ValueError("malformed repository permissions")
        if permissions["push"] is not True:
            raise ValueError("known repository write privilege is missing")
    # Missing permissions (e.g. fine-grained grants) is unknown, not proof of
    # write access. The exact mutation endpoint can still reject the credential.
    return observed


def inventory(api, endpoint):
    entries, seen = [], set()
    separator = "&" if "?" in endpoint else "?"
    for page in range(1, MAX_PAGES + 1):
        values = api.call("GET", f"{endpoint}{separator}per_page=100&page={page}")
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("invalid bounded inventory")
        for entry in values:
            if not isinstance(entry, dict):
                raise ValueError("invalid inventory entry")
            positive(entry.get("id"), "inventory id")
            if entry["id"] in seen:
                raise ValueError("duplicate inventory identity")
            seen.add(entry["id"])
            entries.append(entry)
        if len(values) < 100:
            return entries
    raise ValueError("inventory exceeds complete bounded pagination")


def markdown_structure(content):
    lines, fence = [], None
    for line in content.splitlines(keepends=True):
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) +
                            r",}[ \t]*(?:\r?\n)?", line):
                fence = None
            lines.append("\n")
            continue
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            fence = match[1]
            lines.append("\n")
        else:
            lines.append(line)
    if fence is not None:
        raise ValueError("unclosed Markdown fence in PR template/body")
    return re.findall(
        r"(?m)<!--[\s\S]*?-->|^ {0,3}#{1,6}[ \t]+[^\n]+|^[ \t]*[-*][ \t]+\[[ xX]\][^\n]*"
        r"|^ {0,3}\S[^\n]*\n {0,3}(?:=+|-+)[ \t]*(?=\n|$)",
        "".join(lines))


def templates(api, repo, base_sha, final_body):
    prefix = f"repos/{repo}/contents/"
    candidates = []
    for path in (".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE"):
        try:
            item = api.call("GET", prefix + path + "?ref=" + base_sha)
        except ApiError as error:
            if error.status != 404:
                raise
            continue
        if path.endswith("TEMPLATE"):
            if not isinstance(item, list) or len(item) > 20:
                raise ValueError("invalid template directory")
            for entry in item:
                if not isinstance(entry, dict) or entry.get("type") != "file":
                    raise ValueError("unsupported template directory entry")
                candidate = entry.get("path")
                if not isinstance(candidate, str) or not re.fullmatch(
                        r"\.github/PULL_REQUEST_TEMPLATE/[A-Za-z0-9_.-]+\.md", candidate):
                    raise ValueError("unsupported template path")
                candidates.append(candidate)
        else:
            candidates.append(path)
    if len(candidates) > 1:
        raise ValueError("multiple PR templates require human selection; unsupported in this proof")
    if not candidates:
        return None
    path = candidates[0]
    item = api.call("GET", prefix + path + "?ref=" + base_sha)
    if (not isinstance(item, dict) or item.get("type") != "file" or
            item.get("path") != path or item.get("encoding") != "base64"):
        raise ValueError("unsupported PR template content")
    text(item.get("sha"), "template blob SHA", r"[0-9a-f]{40}")
    try:
        content = base64.b64decode(item["content"], validate=False).decode("utf-8")
    except (KeyError, ValueError, UnicodeError) as error:
        raise ValueError("invalid PR template encoding") from error
    text(content, "template content")
    # Preserve the ordered Markdown headings, complete HTML comments and
    # checklist lines. Example: "## Testing\n<!-- guidance -->\n- [ ] Tested".
    # Prose placeholders may be replaced before approval, never afterwards.
    def checklist_key(line):
        return re.sub(r"^([ \t]*[-*][ \t]+)\[[ xX]\]", r"\1[]", line)

    remaining = iter(markdown_structure(final_body))
    for element in markdown_structure(content):
        if not any(checklist_key(candidate) == checklist_key(element) for candidate in remaining):
            raise ValueError("approved PR body does not preserve template structure and order")
    return {"path": path, "sha": item["sha"], "content": content}


def ref(api, repo, branch, expected):
    item = api.call("GET", f"repos/{repo}/git/ref/heads/" + quote(branch, safe=""))
    object_value = item.get("object") if isinstance(item, dict) else None
    if (not isinstance(item, dict) or item.get("ref") != "refs/heads/" + branch or
            not isinstance(object_value, dict) or object_value.get("type") != "commit" or
            object_value.get("sha") != expected):
        raise ValueError("requested pushed ref changed or is unavailable")


def commits(api, request, action):
    repo = request["repository"]["name"]
    base_owner = repo.split("/")[0]
    head_owner = action["headRepository"]["name"].split("/")[0]
    # Cross-network refs and SHAs are supported; explicit pagination is required
    # because the unpaged compare endpoint truncates at 250 commits.
    # https://docs.github.com/en/rest/commits/commits#compare-two-commits
    pair = f"{base_owner}:{action['expectedBaseSha']}...{head_owner}:{action['expectedHeadSha']}"
    endpoint = f"repos/{repo}/compare/" + quote(pair, safe=":.")
    complete, total, seen = [], None, set()
    for page in range(1, 11):
        response = api.call("GET", f"{endpoint}?per_page=100&page={page}")
        if not isinstance(response, dict):
            raise ValueError("invalid compare response")
        count = response.get("total_commits")
        if (type(count) is not int or not 1 <= count <= 1000 or
                total is not None and count != total or
                response.get("base_commit", {}).get("sha") != action["expectedBaseSha"] or
                response.get("status") not in {"ahead", "diverged"}):
            raise ValueError("empty, changed or unbounded commit inventory")
        total = count
        if page == 1 and not response.get("files"):
            raise ValueError("empty PR diff")
        values = response.get("commits")
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise ValueError("incomplete commit inventory")
        for entry in values:
            sha = text(entry.get("sha"), "commit SHA", r"[0-9a-f]{40}")
            message = text(entry.get("commit", {}).get("message"), "commit message")
            if sha in seen:
                raise ValueError("duplicate commit SHA")
            if CLOSING.search(message):
                raise ValueError("closing commit reference is not authorized")
            seen.add(sha)
            complete.append({"sha": sha, "message": message})
        if len(complete) == total:
            if complete[-1]["sha"] != action["expectedHeadSha"]:
                raise ValueError("commit inventory head mismatch")
            return complete
        if len(values) < 100 or len(complete) > total:
            raise ValueError("incomplete commit inventory")
    raise ValueError("commit inventory exceeds bound")


def observe(api, request, action):
    repo = request["repository"]["name"]
    prefix = "repos/" + repo
    operation = marker(request["requestId"], action["id"])
    if action["kind"] == "draft-pr":
        require_repository(api, action["headRepository"])
        template = templates(api, repo, action["expectedBaseSha"], action["body"])
        compare = commits(api, request, action)
        head = action["headRepository"]["name"].split("/")[0] + ":" + action["head"]
        pulls = inventory(api, prefix + "/pulls?state=all&head=" + quote(head, safe=":"))
        if any(operation in (p.get("body") or "") or
               p.get("state") == "open" and p.get("base", {}).get("ref") == action["base"] for p in pulls):
            raise ValueError("existing PR requires human reconciliation")
        # Read refs last: these remain client guards, not a server-side SHA CAS.
        ref(api, repo, action["base"], action["expectedBaseSha"])
        ref(api, action["headRepository"]["name"], action["head"], action["expectedHeadSha"])
        return {"template": template, "commits": compare}
    expected = action["subject"]
    number = expected["number"]
    issue = api.call("GET", prefix + f"/issues/{number}")
    if (not isinstance(issue, dict) or issue.get("number") != number or
            issue.get("url") != f"https://api.github.com/{prefix}/issues/{number}" or
            issue.get("repository_url") != f"https://api.github.com/{prefix}" or issue.get("state") != "open"):
        raise ValueError("open subject identity mismatch")
    is_pr = "pull_request" in issue
    if is_pr != (action["kind"] == "pr-comment"):
        raise ValueError("issue/PR type mismatch; PR closure is never supported")
    comments = inventory(api, prefix + f"/issues/{number}/comments")
    if any(operation in (c.get("body") or "") for c in comments):
        raise ValueError("existing operation marker requires human reconciliation")
    subject = api.call("GET", prefix + (f"/pulls/{number}" if is_pr else f"/issues/{number}"))
    if (subject.get("number") != number or subject.get("node_id") != expected["nodeId"] or
            subject.get("updated_at") != expected["revision"] or subject.get("state") != "open" or
            subject.get("url") != f"https://api.github.com/{prefix}/{'pulls' if is_pr else 'issues'}/{number}"):
        raise ValueError("subject revision or node changed")
    if not is_pr and "pull_request" in subject:
        raise ValueError("fresh issue became a PR; closure is forbidden")
    if is_pr and subject.get("head", {}).get("sha") != expected["headSha"]:
        raise ValueError("PR head changed")
    return {"nodeId": subject["node_id"], "revision": subject["updated_at"],
            "headSha": expected.get("headSha"), "title": subject.get("title"), "body": subject.get("body")}


def output_packet(output, packet):
    output = Path(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    contracts.write_json(output / "preview.json", {"requestId": packet["request"]["requestId"],
                                                 "actions": packet["request"]["actions"]})
    contracts.write_json(output / "packet.json", packet)
    (output / "prompt.txt").write_text(
        prompt_templates.load("standalone-actions", packet=canonical(packet)), encoding="utf-8")
    return packet


def prepare(request_path, state_root, output, *, initialize=False, api=None):
    request = read_request(request_path)
    root = secure_path(state_root, directory=True, create=initialize)
    api = api or GhTransport()
    with request_lock(root, request["requestId"]):
        folder = root / request["requestId"]
        if folder.exists():
            _, packet, journal = load_state(root, request)
            require_identity(api, request)
            for action in request["actions"]:
                if journal["actions"][action["id"]]["status"] == "pending":
                    if observe(api, request, action) != packet["bindings"][action["id"]]:
                        raise ValueError("prepared action basis changed")
        else:
            if not initialize:
                raise ValueError("missing state; explicit initialize requires human reconciliation of any prior effects")
            require_identity(api, request)
            packet = {"schemaVersion": 1, "kind": "standalone-actions", "packetId": str(uuid.uuid4()),
                      "request": request,
                      "bindings": {a["id"]: observe(api, request, a) for a in request["actions"]}}
            contracts.loads(canonical(packet))
            folder.mkdir(mode=0o700)
            durable_json(folder / "request.json", request)
            durable_json(folder / "packet.json", packet)
            journal = {"schemaVersion": 1, "packetId": packet["packetId"], "packetDigest": digest(packet),
                       "actions": {a["id"]: {"status": "pending", "receipt": None, "error": None}
                                   for a in request["actions"]}}
            durable_json(folder / "journal.json", journal)
        return output_packet(output, packet)


def validate_decision(packet, decision):
    contracts.exact(decision, {"schemaVersion", "packetId", "select", "defer"}, "decision")
    if (type(decision["schemaVersion"]) is not int or decision["schemaVersion"] != 1 or
            decision["packetId"] != packet["packetId"]):
        raise ValueError("foreign or unsupported decision")
    for key in ("select", "defer"):
        if not isinstance(decision[key], list) or not all(isinstance(x, str) for x in decision[key]):
            raise ValueError("invalid action decision list")
    values = decision["select"] + decision["defer"]
    ids = {a["id"] for a in packet["request"]["actions"]}
    if len(values) != len(set(values)) or set(values) != ids:
        raise ValueError("duplicate, foreign or missing action decision")
    return decision


def reason(request_path, state_root, output, *, executable="copilot", process=subprocess.run,
           model="gpt-6.1-sol", effort="medium"):
    request = read_request(request_path)
    with request_lock(state_root, request["requestId"]):
        _, packet, _ = load_state(state_root, request)
    output = Path(output).absolute()
    state = Path(state_root).absolute()
    for path in [output, *output.parents]:
        if path.is_symlink():
            raise ValueError("symlink child directories are unsupported")
    if (state.is_relative_to(output) or output.is_relative_to(state) or
            Path(request_path).absolute().is_relative_to(output)):
        raise ValueError("operator state/request must be outside the child writable directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = local.execute(
        output, packet, local.selected_token(actor=request["actor"]["login"]), process=process, executable=executable,
        prompt_text=prompt_templates.load("standalone-actions", packet=canonical(packet)),
        model=model, effort=effort, isolation_root=output / "child")
    import reasoning
    decision, report = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
    validate_decision(packet, decision)
    contracts.write_json(output / "decision.json", decision)
    contracts.write_json(output / "host-report.json", report)
    return decision


def payload(request, action):
    prefix = "repos/" + request["repository"]["name"]
    if action["kind"] == "draft-pr":
        head_repo = action["headRepository"]["name"]
        return "POST", prefix + "/pulls", {
            "title": action["title"], "body": action["body"], "base": action["base"],
            "head": head_repo.split("/")[0] + ":" + action["head"],
            "head_repo": head_repo.split("/")[1], "draft": True}
    number = action["subject"]["number"]
    if action["kind"] == "close-issue":
        return "PATCH", prefix + f"/issues/{number}", {"state": "closed", "state_reason": action["reason"]}
    return "POST", prefix + f"/issues/{number}/comments", {"body": action["body"]}


def verify_receipt(api, request, action, response):
    if not isinstance(response, dict):
        raise ValueError("unknown mutation response")
    positive(response.get("id"), "response id")
    text(response.get("node_id"), "response node")
    repo = request["repository"]["name"]
    prefix = f"https://api.github.com/repos/{repo}"
    kind = action["kind"]
    if kind == "close-issue":
        expected_url = prefix + f"/issues/{action['subject']['number']}"
    elif kind == "draft-pr":
        positive(response.get("number"), "created PR number")
        expected_url = prefix + f"/pulls/{response['number']}"
    else:
        expected_url = prefix + f"/issues/comments/{response['id']}"
    if response.get("url") != expected_url:
        raise ValueError("response repository/target mismatch")

    def check(value):
        if (not isinstance(value, dict) or value.get("id") != response["id"] or
                value.get("node_id") != response["node_id"] or value.get("url") != expected_url):
            raise ValueError("receipt identity mismatch")
        if kind == "close-issue":
            if (value.get("node_id") != action["subject"]["nodeId"] or "pull_request" in value or
                    value.get("number") != action["subject"]["number"] or
                    value.get("repository_url") != prefix or
                    value.get("state") != "closed" or value.get("state_reason") != action["reason"]):
                raise ValueError("issue closure receipt mismatch")
            # The PATCH response does not identify the actor who closed it.
            # The fresh closed_by field supplies that evidence.
            if value.get("closed_by") != request["actor"] and (
                    not isinstance(value.get("closed_by"), dict) or
                    any(value["closed_by"].get(k) != v for k, v in request["actor"].items())):
                raise ValueError("issue closure actor mismatch")
            return
        if any(value.get("user", {}).get(k) != v for k, v in request["actor"].items()):
            raise ValueError("receipt actor mismatch")
        if value.get("body") != action["body"]:
            raise ValueError("receipt final body mismatch")
        if kind == "draft-pr":
            if (value.get("title") != action["title"] or value.get("draft") is not True or
                    value.get("state") != "open"):
                raise ValueError("created PR payload mismatch")
            for key, expected_repo, expected_ref, expected_sha in (
                    ("base", request["repository"], action["base"], action["expectedBaseSha"]),
                    ("head", action["headRepository"], action["head"], action["expectedHeadSha"])):
                observed = value.get(key, {})
                mapping = observed.get("repo", {})
                if (observed.get("ref") != expected_ref or observed.get("sha") != expected_sha or
                        mapping.get("id") != expected_repo["id"] or
                        mapping.get("node_id") != expected_repo["nodeId"] or
                        mapping.get("full_name") != expected_repo["name"]):
                    raise ValueError("created PR head/base mismatch; attention required")
        elif value.get("issue_url") != prefix + f"/issues/{action['subject']['number']}":
            raise ValueError("comment target mismatch")

    check(response)
    fresh = api.call("GET", expected_url.removeprefix("https://api.github.com/"))
    check(fresh)
    return {"id": response["id"], "nodeId": response["node_id"], "url": expected_url}


def apply(request_path, state_root, decision, *, api=None):
    request = read_request(request_path)
    api = api or GhTransport()
    with request_lock(state_root, request["requestId"]):
        folder, packet, journal = load_state(state_root, request)
        validate_decision(packet, decision)
        require_identity(api, request)
        results = {}
        for action in request["actions"]:
            identity = action["id"]
            entry = journal["actions"][identity]
            if entry["status"] != "pending":
                results[identity] = {"status": entry["status"], "receipt": entry["receipt"]}
                continue
            if identity in decision["defer"]:
                results[identity] = {"status": "deferred"}
                continue
            try:
                require_identity(api, request)
                if observe(api, request, action) != packet["bindings"][identity]:
                    raise ValueError("action preparation is stale")
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                results[identity] = {"status": "failed", "error": "fresh action guards failed; no dispatch"}
                continue
            entry.update(status="sending", error=None)
            # Persist and fsync BEFORE crossing the write boundary. A crash here
            # leaves sending forever; neither reprepare nor replay may resend.
            durable_json(folder / "journal.json", journal, replace=True)
            method, endpoint, outgoing = payload(request, action)
            try:
                response = api.call(method, endpoint, outgoing)
                receipt = verify_receipt(api, request, action, response)
                entry.update(status="confirmed", receipt=receipt)
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                entry.update(status="uncertain", error="dispatch response or verification uncertain; human reconciliation required")
            durable_json(folder / "journal.json", journal, replace=True)
            results[identity] = dict(entry)
        return {"schemaVersion": 1, "packetId": packet["packetId"],
                "ok": all(v["status"] in {"confirmed", "deferred"} for v in results.values()), "actions": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "reason", "apply"):
        command = commands.add_parser(name)
        command.add_argument("--request", type=Path, required=True)
        command.add_argument("--state-root", type=Path, default=DEFAULT_STATE)
        if name != "apply":
            command.add_argument("--workdir", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--initialize", action="store_true")
        elif name == "reason":
            command.add_argument("--copilot", default="copilot")
            command.add_argument("--model", default="gpt-6.1-sol")
            command.add_argument("--reasoning-effort", default="medium", choices=["medium", "high"])
        else:
            command.add_argument("--decision", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.request, args.state_root, args.workdir, initialize=args.initialize)
            print(canonical({"packetId": result["packetId"], "outcome": "prepared; no effects"}))
        elif args.command == "reason":
            result = reason(args.request, args.state_root, args.workdir, executable=args.copilot,
                            model=args.model, effort=args.reasoning_effort)
            print(canonical(result))
        else:
            result = apply(args.request, args.state_root, contracts.read_json(args.decision))
            print(canonical(result))
            return 0 if result["ok"] else 1
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        # Remote errors/output can contain credentials or hostile terminal text.
        # The trusted journal and fixed statuses carry the actionable evidence.
        print("Standalone Shepherd failed closed; inspect operator inputs/state, reconcile uncertain dispatches.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

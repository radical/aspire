"""Fork-only pilot adapter using the legacy transport, identity and paging core."""

from copy import deepcopy
from datetime import timedelta
import json
import hashlib
import re
import sys
from urllib.parse import urlparse

from github import IncompleteInventory, LostResponse, Response
import live
import issue_pr
import pilot_state as state
import round as contracts
import pilot_binding as bindings

REPOSITORY = live.REPOSITORY
PREFIX = "repos/" + REPOSITORY
CORRELATION = "ci-shepherd-pilot: "


class PresentationUncertain(ValueError):
    pass


class PilotTransport(live.HTTPTransport):
    def __init__(self, token, *, write=False, binding=bindings.FORK, tracker=None, authority=None):
        super().__init__(token, write=write)
        self.binding = binding
        self.task_repository = binding.repository
        self.tracker, self.authority = tracker, authority

    def validate_endpoint(self, method, endpoint, body):
        path = urlparse(endpoint)
        if path.scheme or path.netloc or path.fragment or any(part in {".", ".."} for part in path.path.split("/")):
            raise ValueError("invalid pilot endpoint")
        if self.binding == bindings.UPSTREAM:
            target = "repos/" + self.binding.repository
            if method == "GET" and (path.path == target or re.fullmatch(
                    re.escape(target) + r"/(?:issues/20722(?:/comments)?|issues/comments/[1-9][0-9]*"
                    r"|pulls/20722(?:/(?:comments|reviews|files))?|commits/[0-9a-f]{40}/(?:check-runs|status))",
                    path.path) or re.fullmatch(
                        r"agents/repos/microsoft/aspire/tasks(?:/[A-Za-z0-9_-]+)?", path.path)):
                if body is not None:
                    raise ValueError("GET body forbidden")
                return
            if method == "POST" and path.path == "agents/repos/microsoft/aspire/tasks" and self.write:
                if (not isinstance(body, dict) or set(body) != {"prompt", "base_ref", "head_ref", "create_pull_request"}
                        or not isinstance(body["prompt"], str) or not body["prompt"] or len(body["prompt"].encode()) > 20000
                        or body["base_ref"] != "main"
                        or body["head_ref"] != "copilot/restrict-workflows-to-microsoft-aspire"
                        or body["create_pull_request"] is not False):
                    raise ValueError("upstream trial task body mismatch")
                return
            if method in {"POST", "PATCH"} and path.path.startswith(target + "/"):
                raise ValueError("upstream host publication is not authorized")
            controller_reads = {"user", "users/radical", PREFIX,
                                f"{PREFIX}/issues/{self.tracker}", f"{PREFIX}/issues/{self.tracker}/comments"}
            if method == "GET" and path.path in controller_reads and body is None:
                return
            if (method == "PATCH" and self.write and self.authority is not None
                    and path.path == f"{PREFIX}/issues/comments/{self.authority}"
                    and isinstance(body, dict) and set(body) == {"body"}
                    and len(body["body"].encode()) <= state.MAX_BODY
                    and state.parse(body["body"])["repository"] == self.binding.repository):
                return
            raise ValueError("upstream trial endpoint is not allowed")
        prefix = re.escape(PREFIX)
        reads = (
            r"user|users/radical|" + prefix +
            r"(?:|/issues(?:/[1-9][0-9]*(?:/comments)?)?|/issues/comments/[1-9][0-9]*"
            r"|/pulls(?:/[1-9][0-9]*(?:/(?:comments|reviews|files))?)?"
            r"|/commits/[0-9a-f]{40}(?:/(?:check-runs|status))?"
            r"|/contents/\.ci-shepherd-pilot/(?:labels|test_labels)\.py"
            r"|/git/(?:commits/[0-9a-f]{40}|ref/heads/[A-Za-z0-9_./-]+))"
            r"|agents/repos/" + re.escape(REPOSITORY) + r"/tasks(?:/[A-Za-z0-9_-]+)?"
        )
        if method == "GET":
            if body is not None or not re.fullmatch(reads, path.path):
                raise ValueError("pilot read endpoint is not allowed")
            return
        if not self.write or not isinstance(body, dict):
            raise ValueError("hosted pilot writer required")
        if method == "POST" and path.path == f"agents/repos/{REPOSITORY}/tasks":
            if (not {"prompt", "base_ref", "create_pull_request"} <= body.keys()
                    or set(body) - {"prompt", "base_ref", "head_ref", "create_pull_request"}
                    or type(body["create_pull_request"]) is not bool):
                raise ValueError("invalid task body")
            return
        if method == "POST" and re.fullmatch(prefix + r"/issues/[1-9][0-9]*/labels", path.path):
            if body != {"labels": ["shepherd-adopted"]} or path.path.endswith("/121/labels"):
                raise ValueError("only verified pilot child adoption allowed")
            return
        if method in {"POST", "PATCH"} and re.fullmatch(
            prefix + (r"/issues/[1-9][0-9]*/comments" if method == "POST" else r"/issues/comments/[1-9][0-9]*"),
            path.path,
        ):
            if set(body) != {"body"} or not body["body"].startswith("[automated] ") or len(body["body"].encode()) > state.MAX_BODY:
                raise ValueError("invalid host comment")
            return
        if method == "POST" and path.path in {PREFIX + "/git/" + kind for kind in ("blobs", "trees", "commits")}:
            return
        if method == "PATCH" and re.fullmatch(prefix + r"/git/refs/heads/[A-Za-z0-9_./-]+", path.path):
            if set(body) != {"sha", "force"} or body["force"] is not False or not re.fullmatch(r"[0-9a-f]{40}", body["sha"]):
                raise ValueError("only non-forced exact commit publication allowed")
            return
        raise ValueError("pilot write endpoint is not allowed")


def managed(item):
    names = [label["name"] for label in item["labels"]]
    return item["state"] == "open" and "shepherd-adopted" in names and "shepherd-hands-off" not in names


def fingerprint(observation):
    return json.dumps({"number": observation["number"], "node": observation["node"], "head": observation["head"],
                       "description": observation["description"],
                       "feedback": [item["id"] for item in observation["feedback"]]}, sort_keys=True, separators=(",", ":"))


class PilotGitHub:
    def __init__(self, transport, tracker, authority_id, tracker_node, *, write=False, binding=bindings.FORK):
        if binding not in {bindings.FORK, bindings.UPSTREAM}:
            raise ValueError("closed pilot binding required")
        self.binding = binding
        self.repository, self.repository_id = binding.repository, binding.repository_id
        self.prefix = "repos/" + self.repository
        # The approved upstream target currently has 751 active/1357 archived
        # tasks. Keep fork bounds unchanged; 20 pages/lane bounds this trial.
        self.transport, self.api = transport, live.API(
            transport, max_pages=20 if binding == bindings.UPSTREAM else 10)
        self.tracker, self.authority_id, self.write = tracker, authority_id, write
        if not isinstance(tracker_node, str) or not tracker_node:
            raise ValueError("pinned tracker node required")
        self.tracker_node = tracker_node
        for value in (tracker, authority_id):
            if type(value) is not int or value <= 0:
                raise ValueError("explicit tracker and authority comment IDs required")
        if tracker == 121 or authority_id == 5976480777:
            raise ValueError("legacy authority cannot be the pilot tracker")
        actor, identity = self.api.get("user"), self.api.get("users/radical")
        if any(value.get("id") != 1472 or value.get("login") != "radical" for value in (actor, identity)):
            raise ValueError("selected pilot operator identity mismatch")
        self.actor = {"id": 1472, "login": "radical"}
        repository = self.api.get(self.prefix)
        if (repository["id"] != self.repository_id or repository["full_name"] != self.repository
                or repository["default_branch"] != "main"):
            raise ValueError("pilot target identity/default branch mismatch")
        if binding == bindings.UPSTREAM:
            controller = self.api.get(PREFIX)
            if controller.get("id") != live.REPOSITORY_ID or controller.get("full_name") != REPOSITORY:
                raise ValueError("controller repository mismatch")
            if tracker == 122 or authority_id == 5982545145:
                raise ValueError("upstream requires separate controller authority")
        self.ledger = None
        self.expected = None
        self.tasks = {}
        self.external_slots = 0
        self.packet_time = None
        self.high_water = None
        self.clock = live.clock

    def owned(self, comment):
        return comment.get("user", {}).get("id") == self.actor["id"] and comment["user"].get("login") == self.actor["login"]

    def read_authority(self):
        tracker = self.api.get(f"{PREFIX}/issues/{self.tracker}")
        if (tracker["number"] != self.tracker or tracker["node_id"] != self.tracker_node
                or tracker["state"] != "open" or "pull_request" in tracker):
            raise ValueError("pilot tracker must be an open issue")
        comments = self.api.pages(f"{PREFIX}/issues/{self.tracker}/comments")
        candidates = [comment for comment in comments if self.owned(comment) and state.MARKER in comment.get("body", "")]
        if len(candidates) != 1 or candidates[0]["id"] != self.authority_id:
            raise ValueError("pilot authority missing/ambiguous/replaced; no initialization")
        observed = state.parse(candidates[0]["body"])
        if observed["repository"] != self.repository:
            raise ValueError("authority target namespace mismatch")
        if self.expected is None:
            self.expected = deepcopy(observed)
            self.ledger = deepcopy(observed)
        return observed

    def authority_guard(self):
        if self.read_authority() != self.expected:
            raise ValueError("repository authority changed")

    def persist(self):
        if not self.write or self.ledger is None:
            raise ValueError("authenticated hosted writer required")
        body = state.render(self.ledger)
        if self.ledger == self.expected:
            return
        self.authority_guard()
        try:
            response = self.transport("PATCH", f"{PREFIX}/issues/comments/{self.authority_id}", {"body": body})
            if not isinstance(response, Response) or response.status != 200 or response.payload.get("id") != self.authority_id:
                raise LostResponse("ledger write result unknown")
        except LostResponse:
            if self.read_authority() != self.ledger:
                raise ValueError("authority publication uncertain; no retry") from None
        self.expected = deepcopy(self.ledger)

    def mapping(self, number):
        if self.binding.subject is not None and number != self.binding.subject:
            raise ValueError("upstream trial subject mismatch")
        value = self.api.get(f"{self.prefix}/pulls/{number}")
        if (value["number"] != number or value["base"]["repo"]["id"] != self.repository_id
                or value["head"]["repo"]["id"] != self.repository_id
                or value["base"]["repo"]["full_name"] != self.repository or value["head"]["repo"]["full_name"] != self.repository
                or value["base"]["ref"] != "main" or not re.fullmatch(r"[0-9a-f]{40}", value["head"]["sha"])
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", value["head"]["ref"])):
            raise ValueError("PR fork/head/base identity mismatch")
        if self.binding == bindings.UPSTREAM and value["head"]["ref"] != "copilot/restrict-workflows-to-microsoft-aspire":
            raise ValueError("upstream trial branch mismatch")
        return value

    def observe(self, chain):
        number = chain["child"] or chain["origin"]
        kind = "pr" if chain["child"] is not None else chain["kind"]
        value = self.mapping(number) if kind == "pr" else self.api.get(f"{self.prefix}/issues/{number}")
        node = chain["childNode"] or chain["node"]
        if value["number"] != number or value["node_id"] != node:
            raise ValueError("subject identity changed")
        active = managed(value)
        if chain["child"] is not None:
            origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
            active = active and origin["node_id"] == chain["node"] and managed(origin)
        feedback = []
        endpoints = [(f"{self.prefix}/issues/{number}/comments", "comment")]
        if kind == "pr":
            endpoints.append((f"{self.prefix}/pulls/{number}/comments", "review-comment"))
        for endpoint, prefix in endpoints if active else []:
            for comment in self.api.pages(endpoint):
                if comment["id"] == chain["statusId"] and self.owned(comment) and state.STATUS_MARKER in comment.get("body", ""):
                    continue
                identity = f"{prefix}:{comment['id']}:{comment['updated_at']}"
                if identity not in chain["dispositions"]:
                    feedback.append({"id": identity, "body": comment["body"][:2000], "url": comment.get("html_url", "")})
                    for key in ("path", "line", "start_line", "original_line", "side", "commit_id"):
                        if key in comment:
                            feedback[-1][key] = comment[key]
        # Issue comments (including our status) change updated_at. Bind the
        # complete title/body cryptographically instead, without copying those
        # untrusted bodies into the authority ledger.
        description = hashlib.sha256(
            json.dumps([value.get("title"), value.get("body")], ensure_ascii=True).encode()).hexdigest()
        head = value["head"]["sha"] if kind == "pr" else description
        ready, pending_ci, checks, reviews = False, False, [], []
        if kind == "pr" and active:
            checks = self.api.pages(f"{self.prefix}/commits/{head}/check-runs", key="check_runs",
                                    require_total_count=True)
            statuses = self.api.get(f"{self.prefix}/commits/{head}/status")
            if not isinstance(statuses.get("statuses"), list) or len(statuses["statuses"]) >= 100:
                raise IncompleteInventory("combined status inventory incomplete")
            latest_statuses = {}
            for status in statuses["statuses"]:
                latest_statuses.setdefault(status["context"], status)
            # check-runs defaults to filter=latest; every reported check must bind this head.
            for check in checks:
                if check["head_sha"] != head:
                    raise ValueError("check run belongs to old head")
                if check["status"] != "completed":
                    pending_ci = True
                elif check["conclusion"] in {"failure", "timed_out", "action_required"}:
                    identity = f"check:{check['id']}:{head}:{check['conclusion']}"
                    if identity not in chain["dispositions"]:
                        feedback.append({"id": identity, "body": check["name"] + ": " + check["conclusion"],
                                         "url": check["html_url"]})
            for status in latest_statuses.values():
                pending_ci |= status["state"] == "pending"
                if status["state"] in {"failure", "error"}:
                    identity = f"status:{status['id']}:{head}:{status['state']}"
                    if identity not in chain["dispositions"]:
                        feedback.append({"id": identity, "body": status["context"] + ": " + status["state"],
                                         "url": status.get("target_url", "")})
            reviews = self.api.pages(f"{self.prefix}/pulls/{number}/reviews")
            latest = {}
            for review in reviews:
                if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    latest[review["user"]["id"]] = review
                if review["state"] == "CHANGES_REQUESTED" and review.get("body"):
                    identity = f"review:{review['id']}:{review['submitted_at']}"
                    if identity not in chain["dispositions"]:
                        feedback.append({"id": identity, "body": review["body"][:2000], "url": review.get("html_url", "")})
            requested = {reviewer["id"] for reviewer in value["requested_reviewers"]}
            approved = any(review["state"] == "APPROVED" and review["commit_id"] == head and reviewer not in requested
                           for reviewer, review in latest.items())
            ci_green = bool(checks or latest_statuses) and not pending_ci and all(
                check["status"] == "completed" and check["conclusion"] in {"success", "neutral", "skipped"}
                for check in checks
            ) and all(status["state"] == "success" for status in latest_statuses.values())
            ready = ci_green and approved and not requested and not value.get("requested_teams") and not value["draft"] and value["mergeable"] is True and not any(
                review["state"] == "CHANGES_REQUESTED" for review in latest.values())
        attention = "Feedback batch exceeds 30 items; human attention required." if len(feedback) > 30 else None
        if attention is not None:
            # Do not send a truncated repair batch or persist its bodies. This
            # item's visible wait must not stop other chains in the cheap sweep.
            feedback = []
        initial_due = not chain["operations"] or (
            chain["operations"][-1]["state"] in {"failed", "no-send"} and chain["operations"][-1]["taskId"] is None)
        return {"number": number, "kind": kind, "node": node, "head": head, "description": description, "managed": active,
                "state": value["state"], "feedback": sorted(feedback, key=lambda item: item["id"]), "ready": ready,
                "attention": attention, "pendingCI": pending_ci, "actionable": active and attention is None and (
                    bool(feedback) if kind == "pr" else initial_due),
                "title": value.get("title", "")[:300], "body": (value.get("body") or "")[:2000],
                "url": value.get("html_url", ""), "headRef": value["head"]["ref"] if kind == "pr" else None}

    def guard(self, chain, observation, *, effect=True):
        if effect:
            repository = self.api.get(self.prefix)
            if repository.get("id") != self.repository_id or repository.get("full_name") != self.repository:
                raise ValueError("target repository changed")
        fresh = self.observe(chain)
        if not fresh["managed"]:
            raise ValueError("subject management removed or hands-off")
        if effect and fresh["attention"] is not None:
            raise ValueError(fresh["attention"])
        if fingerprint(fresh) != fingerprint(observation):
            raise ValueError("subject basis changed")
        self.authority_guard()
        if effect and (chain["state"] != "open" or state.chain_spend(chain) > state.CHAIN_ALLOWANCE or state.repository_spend(
                self.ledger, self.clock()) > state.REPOSITORY_ALLOWANCE):
            raise ValueError("chain/repository authority or credit allowance exhausted")
        if effect and self.packet_time is not None:
            now = self.clock()
            if not self.packet_time <= now < self.packet_time + timedelta(minutes=10) or (
                    self.high_water is not None and now < self.high_water):
                raise ValueError("packet expired or host clock rolled backwards")
            self.high_water = now
        return fresh

    def inventory(self):
        tasks = {}
        for archived in ("false", "true"):
            for task in self.api.pages(f"agents/repos/{self.repository}/tasks", key="tasks",
                                       query={"is_archived": archived}, optional_total_count=True,
                                       total_count_key="total_archived_count" if archived == "true" else "total_active_count"):
                if task["id"] in tasks:
                    raise IncompleteInventory("task present in both archive lanes")
                tasks[task["id"]] = task
        self.tasks = tasks
        known = {operation["taskId"] for chain in self.ledger["chains"] for operation in chain["operations"]}
        self.external_slots = sum(task["id"] not in known and task["state"] not in state.TERMINAL
                                  for task in tasks.values())

    def task_detail(self, task_id, chain, operation):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("invalid task id")
        task = self.api.get(f"agents/repos/{self.repository}/tasks/{task_id}")
        if (task["id"] != task_id or task["repository"]["id"] != self.repository_id
                or task["creator"]["id"] != self.actor["id"] or not isinstance(task.get("sessions"), list)
                or task.get("session_count") != len(task["sessions"]) or not task["sessions"]
                or not isinstance(task.get("artifacts"), list)):
            raise IncompleteInventory("task/session/artifact source identity unavailable")
        correlations = []
        nano, billed = 0, True
        expected = {"chain": chain["id"], "operation": operation["id"], "origin": chain["origin"]}
        for session in task["sessions"]:
            if session["task_id"] != task_id or session["repository"]["id"] != self.repository_id:
                raise ValueError("session source mismatch")
            if self.binding == bindings.UPSTREAM and (
                    session.get("user", {}).get("id") != self.actor["id"]
                    or session.get("base_ref") != "main"
                    or session.get("head_ref") != "copilot/restrict-workflows-to-microsoft-aspire"):
                raise ValueError("upstream session actor/branch mismatch")
            lines = [line[len(CORRELATION):] for line in session["prompt"].splitlines() if line.startswith(CORRELATION)]
            if len(lines) != 1 or contracts.loads(lines[0]) != expected:
                raise ValueError("session operation correlation mismatch")
            correlations.append(session["id"])
            usage = session.get("usage")
            if usage is None or usage.get("type") != "ai_credits":
                billed = False
            else:
                nano += state.amount(usage["amount"])
        if len(correlations) != len(set(correlations)):
            raise ValueError("duplicate task sessions")
        return task, nano / 1e9 if billed else None

    def admission_slots(self, head_ref):
        if self.binding == bindings.FORK:
            return self.external_slots + state.worker_slots(self.ledger)
        known = {operation["taskId"] for chain in self.ledger["chains"] for operation in chain["operations"]}
        target_id = self.mapping(self.binding.subject)["id"]
        unbound = 0
        queued_pending = 0
        inspected = 0
        for task in self.tasks.values():
            if task["id"] in known or task["state"] in state.TERMINAL:
                continue
            inspected += 1
            if inspected > 128:
                raise ValueError("upstream foreign-task association inspection bound exceeded")
            # Unrelated upstream work does not occupy this authority's cap, but
            # every live foreign task must prove it is not on the target branch.
            detail = self.api.get(f"agents/repos/{self.repository}/tasks/{task['id']}")
            sessions = detail.get("sessions")
            queued_stub = (task["state"] == detail.get("state") == "queued"
                           and type(detail.get("session_count")) is int and detail["session_count"] == 1
                           and sessions == [] and detail.get("artifacts") == [])
            if (detail.get("id") != task["id"] or detail.get("repository", {}).get("id") != self.repository_id
                    or not isinstance(sessions, list) or type(detail.get("session_count")) is not int
                    or detail["session_count"] != len(sessions) and not queued_stub
                    or not isinstance(detail.get("artifacts"), list)):
                raise ValueError("foreign task branch association unavailable")
            if not sessions:
                if detail["artifacts"]:
                    raise ValueError("session-free foreign task artifact association unavailable")
                # A queued foreign task can explicitly have no session yet.
                # This proves only current unbound state, never future inactivity.
                # Live queued stub: session_count=1, sessions=[], artifacts=[].
                # Accept that observed pending shape only when both states agree;
                # managed tasks still require fully correlated sessions.
                unbound += 1
                queued_pending += queued_stub
                continue
            heads = []
            for session in sessions:
                if (session.get("task_id") != task["id"]
                        or session.get("repository", {}).get("id") != self.repository_id
                        or not isinstance(session.get("head_ref"), str)
                        or not isinstance(session.get("base_ref"), str)
                        or bool(session["head_ref"]) != bool(session["base_ref"])):
                    raise ValueError("foreign session branch association unavailable")
                heads.append(session["head_ref"])
            for artifact in detail["artifacts"]:
                if artifact.get("provider") != "github" or not isinstance(artifact.get("data"), dict):
                    raise ValueError("foreign task artifact association unavailable")
                if artifact.get("type") == "branch":
                    branch = artifact["data"].get("head_ref")
                    if not isinstance(branch, str) or not branch:
                        raise ValueError("foreign task branch artifact unavailable")
                    heads.append(branch)
                elif artifact.get("type") == "pull":
                    if type(artifact["data"].get("id")) is not int:
                        raise ValueError("foreign task PR artifact unavailable")
                    if artifact["data"]["id"] == target_id:
                        raise ValueError("active task already owns target PR")
                else:
                    raise ValueError("foreign task artifact association unavailable")
            if head_ref in heads:
                raise ValueError("active task already owns target branch")
            unbound += all(not head for head in heads) and not detail["artifacts"]
        if unbound:
            print(f"CI Shepherd upstream admission observed {unbound} explicitly unbound foreign tasks; "
                  "no current branch/PR association, not proof of future inactivity.", file=sys.stderr)
        if queued_pending:
            print(f"CI Shepherd upstream admission observed {queued_pending} queued pending-association placeholders "
                  "(session_count=1, sessions=[], artifacts=[]); not proof of eventual inactivity.", file=sys.stderr)
        return state.worker_slots(self.ledger)

    def reconcile_workers(self):
        for chain in self.ledger["chains"]:
            for operation in chain["operations"]:
                if operation["taskId"] is None:
                    continue
                if operation["taskId"] not in self.tasks:
                    operation["workerState"] = "unknown"
                    operation["state"] = "waiting"
                    operation["workerReserved"] = max(
                        operation["workerReserved"], max(0, state.CHAIN_ALLOWANCE - state.chain_spend(chain)))
                    continue
                inventory = self.tasks[operation["taskId"]]
                version = {key: inventory.get(key) for key in ("state", "session_count", "updated_at")}
                if (operation["workerState"] in state.TERMINAL and operation["workerActual"] is not None
                        and operation["workerReserved"] == 0
                        and operation["workerVersion"] == version and all(version[key] is not None for key in version)):
                    continue
                # Fresh task state always overrides a terminal cached receipt.
                # A resumed/new session holds capacity before detail validation.
                operation["workerState"] = inventory["state"]
                if inventory["state"] not in state.TERMINAL:
                    operation["state"] = "waiting"
                    operation["workerReserved"] = max(
                        operation["workerReserved"], max(0, state.CHAIN_ALLOWANCE - state.chain_spend(chain)))
                try:
                    task, usage = self.task_detail(operation["taskId"], chain, operation)
                    if task["state"] != inventory["state"] or (
                            inventory.get("session_count") is not None and task["session_count"] != inventory["session_count"]):
                        raise ValueError("fresh task inventory/detail changed; observe again")
                except (ValueError, KeyError) as error:
                    operation.update(workerState="unknown", state="waiting")
                    operation["workerReserved"] = max(
                        operation["workerReserved"], max(0, state.CHAIN_ALLOWANCE - state.chain_spend(chain)))
                    print(f"CI Shepherd task {operation['taskId']} needs human verification: {error}", file=sys.stderr)
                    continue
                operation["workerState"] = task["state"]
                operation["workerVersion"] = {key: task.get(key) for key in ("state", "session_count", "updated_at")}
                if usage is not None:
                    if operation["workerActual"] is not None and usage < operation["workerActual"]:
                        raise ValueError("task billing moved backwards")
                    if usage != operation["workerActual"]:
                        operation.update(workerActual=usage, workerAt=issue_pr.stamp(self.clock()))
                    operation["workerReserved"] = 0
                if task["state"] not in state.TERMINAL or usage is None:
                    operation["workerReserved"] = max(
                        operation["workerReserved"], max(0, state.CHAIN_ALLOWANCE - state.chain_spend(chain)))
                if task["state"] in state.TERMINAL:
                    state.finish(operation, "completed" if task["state"] == "completed" else "failed")
                    if task["state"] == "completed":
                        basis = contracts.loads(operation["identity"].split(":round:", 1)[0])
                        # A completed worker is not proof of comment resolution.
                        # Preserve explicit human disposition rather than blindly
                        # starting another worker for the same feedback IDs.
                        for identity in basis["feedback"]:
                            chain["dispositions"].setdefault(identity, "needs-human")
                    if chain["kind"] == "issue" and chain["child"] is None and task["state"] == "completed":
                        self.adopt_artifact(task, chain, operation)

    def adopt_artifact(self, task, chain, operation):
        pulls = [artifact["data"] for artifact in task["artifacts"]
                 if artifact.get("provider") == "github" and artifact.get("type") == "pull"]
        branches = [artifact["data"] for artifact in task["artifacts"]
                    if artifact.get("provider") == "github" and artifact.get("type") == "branch"]
        if len(pulls) != 1 or len(branches) != 1:
            chain["state"] = "human"
            return
        candidates = [pr for pr in self.api.pages(f"{self.prefix}/pulls", query={"state": "open"})
                      if pr["id"] == pulls[0]["id"]]
        if len(candidates) != 1:
            raise ValueError("task PR artifact mapping unavailable")
        pr = self.mapping(candidates[0]["number"])
        if (branches[0] != {"head_ref": pr["head"]["ref"], "base_ref": pr["base"]["ref"]}
                or pulls[0].get("global_id") not in {None, "", pr["node_id"]}
                or not all(session["head_ref"] == pr["head"]["ref"] and session["base_ref"] == "main"
                           for session in task["sessions"])):
            raise ValueError("task/session/branch/PR artifact mismatch")
        ref = self.api.get(f"{self.prefix}/git/ref/heads/{pr['head']['ref']}")
        if ref.get("ref") != "refs/heads/" + pr["head"]["ref"] or ref.get("object", {}).get("sha") != pr["head"]["sha"]:
            raise ValueError("independent child branch mapping mismatch")
        # Mapping preserves the parent's counters; a missing artifact never
        # creates another budget or authorizes a guessed pull request.
        state.bind_child(self.ledger, chain, pr["number"], pr["node_id"])
        chain["childAdoption"] = "reserved"
        self.persist()
        self.adopt_child(chain)

    def adopt_child(self, chain):
        if chain["childAdoption"] == "confirmed":
            return
        pr = self.mapping(chain["child"])
        origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
        if origin["node_id"] != chain["node"] or pr["node_id"] != chain["childNode"]:
            raise ValueError("origin/child adoption authority changed")
        if not managed(origin) or pr["state"] != "open" or "shepherd-hands-off" in [label["name"] for label in pr["labels"]]:
            chain["state"] = "hands-off"
            return
        if managed(pr):
            chain["childAdoption"] = "confirmed"
            self.persist()
            return
        if chain["childAdoption"] in {"sent", "uncertain"}:
            chain["state"] = "hands-off"
            return
        chain["childAdoption"] = "sent"
        self.persist()
        pr = self.mapping(chain["child"])
        origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
        if (not managed(origin) or origin["node_id"] != chain["node"] or pr["node_id"] != chain["childNode"]
                or pr["state"] != "open" or "shepherd-hands-off" in [label["name"] for label in pr["labels"]]):
            raise ValueError("child adoption management changed before send")
        self.authority_guard()
        # Only independently mapped task artifacts can enter this fixed label
        # writer; a model cannot supply labels, subjects or an arbitrary body.
        response = self.transport("POST", f"{self.prefix}/issues/{chain['child']}/labels", {"labels": ["shepherd-adopted"]})
        if not isinstance(response, Response) or response.status != 200 or not isinstance(response.payload, list) or not any(
                label.get("name") == "shepherd-adopted" for label in response.payload):
            chain["childAdoption"] = "uncertain"
            self.persist()
            raise LostResponse("child adoption result unknown; no retry")
        chain["childAdoption"] = "confirmed"
        self.persist()

    def sweep(self):
        if self.ledger is None:
            raise ValueError("authenticated authority must be read before discovery")
        self.inventory()
        self.reconcile_workers()
        for chain in self.ledger["chains"]:
            if chain["child"] is not None and chain["childAdoption"] in {"reserved", "sent", "uncertain"}:
                self.adopt_child(chain)
        intake = ([self.mapping(self.binding.subject)] if self.binding.subject is not None else
                  self.api.pages(f"{self.prefix}/issues", query={"state": "open", "labels": "shepherd-adopted"}))
        for candidate in intake:
            number = candidate["number"]
            if number in {121, self.tracker} or not managed(candidate) or state.find_chain(self.ledger, number) is not None:
                continue
            state.adopt(self.ledger, number, "pr" if self.binding.subject is not None or "pull_request" in candidate else "issue", candidate["node_id"])
        observations = {}
        for chain in self.ledger["chains"]:
            observed = self.observe(chain)
            observations[observed["number"]] = observed
            if not observed["managed"]:
                chain["state"] = "closed" if observed["state"] == "closed" else "hands-off"
            elif chain["state"] in {"closed", "hands-off"}:
                chain["state"] = "open"
        return observations

    def status(self, chain, observation, now):
        operation = chain["operations"][-1] if chain["operations"] else None
        lane = operation["lane"] if operation else ("cloud" if chain["escalated"] else "local")
        actual = sum(sum(op[key] or 0 for key in ("nativeActual", "workerActual")) for op in chain["operations"])
        reserved = state.chain_spend(chain) - actual
        dispositions = {value: list(chain["dispositions"].values()).count(value)
                        for value in ("addressed", "declined", "needs-human")}
        if chain["state"] != "open":
            blocker = "Human handoff / adoption removed; no new item writes."
        elif observation["attention"] is not None:
            blocker = observation["attention"] + " No inference."
        elif operation is not None and operation["workerState"] == "waiting_for_user":
            blocker = "Worker needs human input; open the task. No inference."
        elif state.pending(chain):
            blocker = "Tracked work / uncertain send; observe only, never retry."
        elif (chain["kind"] == "issue" and chain["child"] is None and operation is not None
              and operation["state"] == "completed" and operation["taskId"] is not None):
            blocker = "Completed task has no verified child PR; human handoff required."
        elif chain["rounds"] >= 10 or state.chain_spend(chain) >= state.CHAIN_ALLOWANCE:
            blocker = "Lifetime allowance exhausted; human attention required."
        elif observation["ready"]:
            blocker = "Current-head checks and approval verified; human merge required."
        elif observation["pendingCI"]:
            blocker = "Waiting for current-head CI; no inference."
        elif not observation["actionable"]:
            blocker = "Waiting for human review / supported new feedback; no inference."
        else:
            blocker = "Bounded repair batch due."
        task = "" if not operation or not operation["taskId"] else (
            f"\nTask: https://github.com/{self.repository}/agents/tasks/{operation['taskId']}")
        last = "" if operation is None else f" Last action: {operation['state']}."
        return (f"[automated] CI Shepherd - {lane}\n\nLocal attempts: {chain['localAttempts']}/2; "
                f"action rounds: {chain['rounds']}/10.\nActual credits: {actual:g}; outstanding reservation: {reserved:g}. "
                "Unknown billing retains its reservation; these are not hard billing caps.\n\n"
                f"Feedback dispositions: {dispositions['addressed']} addressed, {dispositions['declined']} declined, "
                f"{dispositions['needs-human']} needs-human.\n{blocker}{last}\nEvidence: {observation['url']}{task}\n"
                f"{state.STATUS_MARKER}\nChain: {chain['id']}")

    def publish_status(self, chain, observation, now):
        if self.binding == bindings.UPSTREAM:
            # Upstream host writes are limited to the single task request.
            # The canonical fork authority remains the trial's status surface.
            return
        if not self.write or not observation["managed"] or chain["state"] not in {"open", "human"}:
            return
        body = self.status(chain, observation, now)
        number = observation["number"]
        if chain["statusPending"]:
            candidates = [comment for comment in self.api.pages(f"{self.prefix}/issues/{number}/comments")
                          if self.owned(comment) and state.STATUS_MARKER in comment.get("body", "")
                          and comment["body"].endswith("Chain: " + chain["id"])]
            if len(candidates) != 1:
                raise PresentationUncertain("presentation creation uncertain; needs-human; no retry")
            chain.update(statusId=candidates[0]["id"], statusPending=False)
            self.persist()
        if chain["statusId"] is not None:
            comment = self.api.get(f"{self.prefix}/issues/comments/{chain['statusId']}")
            if not self.owned(comment) or state.STATUS_MARKER not in comment["body"]:
                raise ValueError("owned presentation comment missing/replaced")
            if comment["body"] == body:
                return
        self.guard(chain, observation, effect=False)
        if chain["statusId"] is None:
            chain["statusPending"] = True
            self.persist()
            self.guard(chain, observation, effect=False)
        method, endpoint = ("POST", f"{self.prefix}/issues/{number}/comments") if chain["statusId"] is None else (
            "PATCH", f"{self.prefix}/issues/comments/{chain['statusId']}")
        response = self.transport(method, endpoint, {"body": body})
        if not isinstance(response, Response) or response.status != (201 if method == "POST" else 200):
            raise LostResponse("presentation outcome unknown; no retry")
        if not isinstance(response.payload, dict) or type(response.payload.get("id")) is not int or response.payload["id"] <= 0:
            raise LostResponse("presentation response identity unknown; no retry")
        chain.update(statusId=response.payload["id"], statusPending=False)
        self.persist()

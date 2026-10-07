"""Fork-only pilot adapter using the legacy transport, identity and paging core."""

from copy import deepcopy
from datetime import timedelta
import json
import hashlib
import re
import sys
from urllib.parse import parse_qs, urlparse

from github import IncompleteInventory, LostResponse, Response
import live
import issue_pr
import pilot_state as state
import round as contracts
import pilot_binding as bindings
import pilot_history as history
import pilot_reminders as reminders
import pilot_results as results
import pilot_feedback as review_feedback
import pilot_authors as authors
import pilot_reviews as copilot_reviews

REPOSITORY = live.REPOSITORY
PREFIX = "repos/" + REPOSITORY
CORRELATION = "ci-shepherd-pilot: "
WORKER_STATES = state.TERMINAL | {"queued", "in_progress", "idle", "waiting_for_user"}


def validate_graphql(value, binding):
    if isinstance(value, dict) and value.get("query") in (review_feedback.QUERY, review_feedback.COMMENTS_QUERY):
        review_feedback.validate_request(value, binding)
    else:
        history.validate_request(value, binding)


class PresentationUncertain(ValueError):
    pass


class AuthorityUncertain(ValueError):
    pass


class PilotTransport(live.HTTPTransport):
    def __init__(self, token, *, write=False, binding=bindings.FORK, tracker=None, authority=None):
        if binding not in {bindings.FORK, bindings.UPSTREAM, bindings.UPSTREAM_ALL}:
            raise ValueError("closed pilot transport binding required")
        super().__init__(token, write=write)
        self.binding = binding
        self.task_repository = binding.repository
        self.tracker, self.authority = tracker, authority

    def is_read(self, method, endpoint, body):
        if method == "POST" and endpoint == "graphql":
            validate_graphql(body, self.binding)
            return True
        return super().is_read(method, endpoint, body)

    def validate_endpoint(self, method, endpoint, body):
        path = urlparse(endpoint)
        if path.scheme or path.netloc or path.fragment or any(part in {".", ".."} for part in path.path.split("/")):
            raise ValueError("invalid pilot endpoint")
        if method == "POST" and endpoint == "graphql":
            validate_graphql(body, self.binding)
            return
        if method == "GET" and re.fullmatch(
                re.escape(f"repos/{self.binding.repository}") + r"/check-runs/[1-9][0-9]*/annotations", path.path):
            parameters = parse_qs(path.query, strict_parsing=True)
            if (body is not None or set(parameters) != {"page", "per_page"}
                    or parameters["per_page"] != ["100"] or len(parameters["page"]) != 1
                    or not re.fullmatch(r"[1-9][0-9]*", parameters["page"][0])
                    or int(parameters["page"][0]) > 10):
                raise ValueError("only bounded check annotation GETs are allowed")
            return
        if method == "GET" and path.path == f"repos/{self.binding.repository}/actions/runs" and body is None:
            reminders.validate_runs_endpoint(path, self.binding)
            return
        if (method == "POST" and self.write and not path.query
                and re.fullmatch(re.escape(f"repos/{self.binding.repository}/pulls/")
                                 + ("20722" if self.binding == bindings.UPSTREAM else r"[1-9][0-9]*")
                                 + r"/requested_reviewers", path.path)):
            if body != {"reviewers": [authors.REVIEWER]} or path.path.endswith("/121/requested_reviewers"):
                raise ValueError("only fixed Copilot reviewer request allowed")
            return
        if self.binding != bindings.FORK:
            target = "repos/" + self.binding.repository
            subject = str(self.binding.subject) if self.binding.subject is not None else r"[1-9][0-9]*"
            if method == "GET" and (path.path == target or re.fullmatch(
                    re.escape(target) + r"/(?:issues/" + subject + r"(?:/comments)?|issues/comments/[1-9][0-9]*"
                    r"|pulls/" + subject + r"(?:/(?:comments|reviews|files))?|commits/[0-9a-f]{40}/(?:check-runs|status))",
                    path.path) or re.fullmatch(
                        r"agents/repos/microsoft/aspire/tasks/[A-Za-z0-9_-]+", path.path)
                    or self.binding == bindings.UPSTREAM_ALL and path.path == target + "/issues"):
                if body is not None:
                    raise ValueError("GET body forbidden")
                if path.path == target + "/issues":
                    parameters = parse_qs(path.query, strict_parsing=True)
                    if (set(parameters) != {"state", "labels", "page", "per_page"}
                            or parameters["state"] != ["open"] or parameters["labels"] != ["shepherd-adopted"]
                            or parameters["per_page"] != ["100"] or len(parameters["page"]) != 1
                            or not re.fullmatch(r"[1-9][0-9]*", parameters["page"][0])
                            or int(parameters["page"][0]) > 10):
                        raise ValueError("only bounded adopted upstream intake allowed")
                return
            if method == "POST" and path.path == "agents/repos/microsoft/aspire/tasks" and self.write:
                if (not isinstance(body, dict) or set(body) != {"prompt", "base_ref", "head_ref", "create_pull_request"}
                        or not isinstance(body["prompt"], str) or not body["prompt"] or len(body["prompt"].encode()) > 20000
                        or body["base_ref"] != "main"
                        or not isinstance(body["head_ref"], str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", body["head_ref"])
                        or self.binding == bindings.UPSTREAM and body["head_ref"] != "copilot/restrict-workflows-to-microsoft-aspire"
                        or body["create_pull_request"] is not False):
                    raise ValueError("upstream trial task body mismatch")
                return
            if (method == "POST" and self.write and not path.query
                    and re.fullmatch(re.escape(target) + "/issues/" + subject + "/comments", path.path)
                    and isinstance(body, dict) and set(body) == {"body"}
                    and (reminders.valid_body(body["body"], self.binding.repository, int(path.path.split("/")[-2]))
                         or results.valid_report(body["body"], self.binding.repository, int(path.path.split("/")[-2])))):
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
            r"|agents/repos/" + re.escape(REPOSITORY) + r"/tasks/[A-Za-z0-9_-]+"
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


def native_handoff(chain):
    """Identify a native human stop, never a completed taskless timed wait."""
    if not chain["operations"]:
        return False
    operation = chain["operations"][-1]
    return (operation["state"] == "completed" and operation["taskId"] is None
            and operation["sessionId"] is not None and "wait" not in operation)


def wait_state(chain, observation, now):
    if not chain["operations"]:
        return None
    operation = chain["operations"][-1]
    if "wait" not in operation:
        return None
    basis = contracts.loads(operation["identity"].rsplit(":round:", 1)[0])
    basis.pop("workerEvidence", None)
    basis.pop("workerResults", None)
    if json.dumps(basis, sort_keys=True, separators=(",", ":")) != fingerprint(observation, worker_evidence=False):
        return "superseded"
    return "waiting" if now < state.validate_wait(operation["wait"]) else "due"


def fingerprint(observation, *, worker_evidence=True):
    value = {"number": observation["number"], "node": observation["node"], "head": observation["head"],
             "headRef": observation["headRef"],
             "ciEvidence": observation["ciEvidence"], "feedbackEvidence": observation["feedbackEvidence"],
             "description": observation["description"],
             "feedback": [item["id"] for item in observation["feedback"]]}
    if worker_evidence:
        value["workerEvidence"] = observation["workerEvidence"]
    if worker_evidence and observation.get("workerResults"):
        # Descriptive snippets can be bounded in both prompts. Bind the receipt's
        # identities/version instead, so fresh settlement rejects changed results.
        value["workerResults"] = [{**{key: result[key] for key in (
            "operation", "taskId", "state", "sessionCount", "sessionIds", "updatedAt")},
            **({"resultSettlement": {key: result["resultSettlement"][key] for key in (
                "version", "status", "summary", "reason") if key in result["resultSettlement"]}}
               if "resultSettlement" in result else {})}
            for result in observation["workerResults"]]
    review = observation.get("copilotReview")
    if review is not None and (review["requested"] or review["inProgress"] or review["receipts"]):
        # Bind external review activity, not our mutable send bookkeeping.
        # A new request or withdrawn result invalidates a native repair packet.
        value["reviewEvidence"] = {key: review[key] for key in ("requested", "inProgress", "receipts")}
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class PilotGitHub:
    inline_repairs = True
    result_collector = None
    publish_results = False

    @property
    def result_capable(self):
        collector = self.result_collector
        return collector is not None and getattr(collector, "capable", True)

    def __init__(self, transport, tracker, authority_id, tracker_node, *, write=False, binding=bindings.FORK):
        if binding not in {bindings.FORK, bindings.UPSTREAM, bindings.UPSTREAM_ALL}:
            raise ValueError("closed pilot binding required")
        self.binding = binding
        self.repository, self.repository_id = binding.repository, binding.repository_id
        self.prefix = "repos/" + self.repository
        self.transport, self.api = transport, live.API(transport, repository_id=self.repository_id)
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
        if binding != bindings.FORK:
            controller = self.api.get(PREFIX)
            if controller.get("id") != live.REPOSITORY_ID or controller.get("full_name") != REPOSITORY:
                raise ValueError("controller repository mismatch")
            if tracker == 122 or authority_id == 5982545145:
                raise ValueError("upstream requires separate controller authority")
        self.ledger = None
        self.expected = None
        self.packet_time = None
        self.high_water = None
        self.clock = live.clock
        self.reminder_delay = 60
        self.worker_results = {}
        self.worker_result_heads = {}
        self.worker_result_versions = {}
        self.worker_revisions = {}
        self.admission_reasons = {}

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
        if self.binding == bindings.UPSTREAM and any(
                chain["origin"] != self.binding.subject for chain in observed["chains"]):
            raise ValueError("upstream trial subject mismatch")
        if self.expected is None:
            self.expected = deepcopy(observed)
            self.ledger = deepcopy(observed)
        return observed

    def authority_guard(self):
        if self.read_authority() != self.expected:
            raise ValueError("repository authority changed")

    def adoption_effect_guard(self):
        """Guard label writes without blocking billing reconciliation."""
        self.authority_guard()

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
            try:
                if self.read_authority() != self.ledger:
                    raise AuthorityUncertain("authority publication uncertain; no retry")
            except (ValueError, KeyError, TypeError) as error:
                raise AuthorityUncertain("authority publication uncertain; no retry") from error
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
        reviews, review_inventory_complete = [], True
        review_attention = None
        if kind == "pr" and (active or chain.get("reviews")):
            try:
                reviews = self.api.pages(f"{self.prefix}/pulls/{number}/reviews")
            except IncompleteInventory as error:
                review_inventory_complete = False
                review_attention = "PR review inventory unavailable/incomplete."
                print(f"CI Shepherd #{number} review inventory unknown: {error}", file=sys.stderr)
        origin_managed, hands_off = None, None
        if chain["child"] is not None:
            origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
            origin_managed = origin["node_id"] == chain["node"] and managed(origin)
            hands_off = "shepherd-hands-off" in [label["name"] for label in value["labels"]]
            active = active and origin_managed
        feedback, feedback_attention, feedback_evidence = [], review_attention, []
        feedback_revisions = {}
        endpoints = [(f"{self.prefix}/issues/{number}/comments", "comment")]
        if kind == "pr":
            endpoints.append((f"{self.prefix}/pulls/{number}/comments", "review-comment"))
        for endpoint, prefix in endpoints if active else []:
            resolved_comments = set()
            if prefix == "review-comment":
                try:
                    comments = self.api.pages(endpoint)
                    comments = [comment for comment in comments if authors.feedback(comment.get("user"))]
                    if comments:
                        resolved_comments = review_feedback.resolved(
                            self.transport, self.binding, number, node, value["head"]["sha"], comments)
                except IncompleteInventory:
                    feedback_attention = "Review-thread resolution unavailable/incomplete."
                    comments = []
            else:
                comments = self.api.pages(endpoint)
            for comment in comments:
                if not authors.feedback(comment.get("user")):
                    continue
                if comment["id"] in resolved_comments:
                    continue
                if comment["id"] == chain["statusId"] and self.owned(comment) and state.STATUS_MARKER in comment.get("body", ""):
                    continue
                if self.owned(comment) and reminders.valid_body(comment.get("body"), self.repository, number):
                    continue
                if results.owned_report(self, chain, comment):
                    continue
                feedback_evidence.append(comment)
                identity = f"{prefix}:{comment['id']}:{comment['updated_at']}"
                if results.eligible(chain, identity, self.worker_results):
                    feedback.append({"id": identity, "body": comment["body"][:2000], "url": comment.get("html_url", "")})
                    feedback_revisions[identity] = hashlib.sha256(json.dumps(
                        [comment["body"], {key: comment.get(key) for key in (
                            "path", "line", "start_line", "original_line", "side", "commit_id")}],
                        sort_keys=True).encode()).hexdigest()
                    for key in ("path", "line", "start_line", "original_line", "side", "commit_id"):
                        if key in comment:
                            feedback[-1][key] = comment[key]
        # Issue comments (including our status) change updated_at. Bind the
        # complete title/body cryptographically instead, without copying those
        # untrusted bodies into the authority ledger.
        description = hashlib.sha256(
            json.dumps([value.get("title"), value.get("body")], ensure_ascii=True).encode()).hexdigest()
        head = value["head"]["sha"] if kind == "pr" else description
        ready, pending_ci, ci_green, checks = False, False, False, []
        diagnostics, ci_wait = [], None
        statuses = None
        workflow = {"approval": None, "pending": False, "green": True, "attention": None}
        if kind == "pr" and active:
            workflow = reminders.workflow_evidence(self, head)
            pending_ci = workflow["pending"]
            checks = self.api.pages(f"{self.prefix}/commits/{head}/check-runs", key="check_runs",
                                    require_total_count=True)
            statuses = self.api.get(f"{self.prefix}/commits/{head}/status")
            if not isinstance(statuses.get("statuses"), list) or len(statuses["statuses"]) >= 100:
                raise IncompleteInventory("combined status inventory incomplete")
            latest_statuses = {}
            for status in statuses["statuses"]:
                latest_statuses.setdefault(status["context"], status)
            # Validate the entire named-repository, filter=latest connection
            # before deriving any annotation IDs. Never follow output URLs.
            for check in checks:
                issue_pr.positive(check["id"], "check run")
                if check["head_sha"] != head:
                    raise ValueError("check run belongs to old head")
            diagnostics = self.check_diagnostics(checks)
            by_id = {item["checkId"]: item for item in diagnostics}
            failures = [check for check in checks if check["status"] == "completed"
                        and check["conclusion"] not in {"success", "neutral", "skipped"}]
            # Classification uses raw inventory, not disposition-filtered feedback.
            # A NOTICE about scarcity is not evidence that a failing job is infra.
            infra_only = bool(failures) and all(
                check["conclusion"] == "cancelled" or check["conclusion"] in {"failure", "timed_out"}
                and by_id.get(check["id"], {}).get("infrastructure") is True
                for check in failures)
            status_failure = any(status["state"] in {"failure", "error"} for status in latest_statuses.values())
            workflow_failures = workflow.get("failures", [])
            if (infra_only and not status_failure and not workflow["attention"]
                    and all(conclusion == "cancelled" or type(suite) is int and any(
                        check.get("check_suite", {}).get("id") == suite for check in failures)
                            for suite, conclusion in workflow_failures)):
                ci_wait = "Infrastructure/cancellation-only CI; wait or rerun required, no code repair."
            elif not failures and workflow_failures and all(
                    conclusion == "cancelled" for _, conclusion in workflow_failures) and not status_failure:
                ci_wait = "Cancellation-only CI; rerun required, outage not established."
            for check in checks:
                if check["status"] != "completed":
                    pending_ci = True
                elif check["conclusion"] not in {"success", "neutral", "skipped"} and ci_wait is None:
                    if check["conclusion"] == "action_required" and workflow["approval"] is not None:
                        continue
                    identity = f"check:{check['id']}:{head}:{check['conclusion']}"
                    if results.eligible(chain, identity, self.worker_results):
                        feedback.append({"id": identity, "body": check["name"] + ": " + check["conclusion"],
                                         "url": check["html_url"]})
            if ci_wait is None:
                for run in workflow.get("failedRuns", []):
                    if workflow["approval"] is not None and run["conclusion"] == "action_required":
                        continue
                    if type(run["suite"]) is int and any(
                            check.get("check_suite", {}).get("id") == run["suite"] for check in failures):
                        continue
                    # A terminal workflow can fail before exposing any jobs.
                    # Its verified run is investigation evidence, not a cause.
                    identity = f"workflow:{run['id']}:{head}:{run['conclusion']}"
                    if results.eligible(chain, identity, self.worker_results):
                        feedback.append({"id": identity, "body": "Workflow: " + run["conclusion"] + "; cause unknown",
                                         "url": run["url"]})
                        # The same run ID can execute again without exposing jobs.
                        # Bind only this verified run's attempt, not aggregate
                        # workflow metadata that unrelated/cosmetic changes alter.
                        feedback_revisions[identity] = str(run["runAttempt"])
            for status in latest_statuses.values():
                pending_ci |= status["state"] == "pending"
                if status["state"] in {"failure", "error"}:
                    identity = f"status:{status['id']}:{head}:{status['state']}"
                    if results.eligible(chain, identity, self.worker_results):
                        feedback.append({"id": identity, "body": status["context"] + ": " + status["state"],
                                         "url": status.get("target_url", "")})
            latest = {}
            for review in reviews:
                if review["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    user = review.get("user")
                    if not isinstance(user, dict) or type(user.get("id")) is not int or user["id"] <= 0:
                        feedback_attention = "PR review author evidence unavailable/incomplete."
                    else:
                        latest[user["id"]] = review
                if (review["state"] in {"CHANGES_REQUESTED", "COMMENTED"} and review.get("body")
                        and authors.feedback(review.get("user"))):
                    # REST reviews have submitted_at, but no edit timestamp.
                    # Bind the complete body so a later edit cannot reuse a
                    # declined disposition or authorize a stale worker packet.
                    version = hashlib.sha256(review["body"].encode()).hexdigest()
                    identity = f"review:{review['id']}:{review['submitted_at']}:{version}"
                    if results.eligible(chain, identity, self.worker_results):
                        feedback.append({"id": identity, "body": review["body"][:2000], "url": review.get("html_url", "")})
                        feedback_revisions[identity] = version
            requested = {reviewer["id"] for reviewer in value["requested_reviewers"]}
            approved = any(review["state"] == "APPROVED"
                           and review["user"]["id"] not in {authors.REVIEWER_ID, authors.WORKER_ID}
                           and review["user"].get("type") != "Bot"
                           and review["commit_id"] == head and reviewer not in requested
                           for reviewer, review in latest.items())
            ci_green = workflow["green"] and bool(checks or latest_statuses) and not pending_ci and all(
                check["status"] == "completed" and check["conclusion"] in {"success", "neutral", "skipped"}
                for check in checks
            ) and all(status["state"] == "success" for status in latest_statuses.values())
            ready = ci_green and approved and not requested and not value.get("requested_teams") and not value["draft"] and value["mergeable"] is True and not any(
                review["state"] == "CHANGES_REQUESTED" for review in latest.values())
        attention = feedback_attention
        if attention is not None:
            # Do not send a truncated repair batch or persist its bodies. This
            # item's visible wait must not stop other chains in the cheap sweep.
            feedback = []
        last_operation = chain["operations"][-1] if chain["operations"] else None
        # Retry only from this sweep's verified saved-task receipt, never a
        # persisted status alone. No reported artifacts is not proof that no
        # PR exists elsewhere; existing human stops still block selection.
        last_receipt = self.worker_results.get(last_operation["id"]) if last_operation is not None else None
        worker_failed_without_artifact = (
            kind == "issue" and chain["child"] is None and last_operation is not None
            and last_operation["taskId"] is not None and last_operation["state"] == "failed"
            and last_operation["workerState"] in {"failed", "timed_out", "cancelled"}
            and last_receipt is not None and last_receipt["taskId"] == last_operation["taskId"]
            and last_receipt["artifactState"] == "not-reported")
        initial_due = not chain["operations"] or (
            last_operation["state"] in {"failed", "no-send"} and last_operation["taskId"] is None
        ) or worker_failed_without_artifact
        work_history = None
        if kind == "pr":
            try:
                work_history = history.read(self.transport, self.binding, number, node)
            except IncompleteInventory:
                work_history = {"complete": False, "events": []}
        # Security bindings cover untrusted raw inventories before any prompt
        # truncation. Same-ID output edits must invalidate prepared decisions.
        ci_revision = hashlib.sha256(json.dumps(
            [checks, statuses, workflow, diagnostics], sort_keys=True, ensure_ascii=True,
            allow_nan=False).encode()).hexdigest()
        feedback_revision = hashlib.sha256(json.dumps(
            [feedback_evidence, reviews], sort_keys=True, ensure_ascii=True,
            allow_nan=False).encode()).hexdigest()
        observed = {"number": number, "kind": kind, "node": node, "head": head, "description": description, "managed": active,
                "ciEvidence": ci_revision, "feedbackEvidence": feedback_revision,
                "feedbackRevisions": {item["id"]: feedback_revisions[item["id"]] for item in feedback
                                      if item["id"] in feedback_revisions},
                "originManaged": origin_managed, "handsOff": hands_off,
                "state": value["state"], "feedback": sorted(feedback, key=lambda item: item["id"]), "ready": ready,
                "attention": attention, "pendingCI": pending_ci,
                "ciWait": ci_wait, "reviewOnly": ci_wait is not None, "diagnostics": diagnostics,
                "approval": workflow["approval"], "workflowAttention": workflow["attention"],
                "actionable": active and not pending_ci and attention is None and workflow["attention"] is None and workflow["approval"] is None and (
                    bool(feedback) if kind == "pr" else initial_due or bool(feedback)),
                "title": value.get("title", "")[:300], "body": (value.get("body") or "")[:2000],
                "url": value.get("html_url", ""), "headRef": value["head"]["ref"] if kind == "pr" else None,
                "workHistory": work_history}
        observed["workerResults"] = results.context(chain, self.worker_results, observed, self.worker_result_versions)
        observed["workerEvidence"] = [{"operation": operation["id"], "revision": self.worker_revisions[operation["id"]]}
                                      for operation in chain["operations"] if operation["id"] in self.worker_revisions]
        if kind == "pr":
            try:
                if not review_inventory_complete:
                    raise IncompleteInventory("PR review inventory unavailable/incomplete.")
                observed["copilotReview"] = copilot_reviews.evidence(chain, value, reviews, ci_green)
            except IncompleteInventory as error:
                observed["attention"] = "Copilot review inventory unavailable/incomplete."
                observed["copilotReview"] = {
                    "state": "unavailable", "green": False, "draft": value["draft"],
                    "receipts": [], "requested": False, "inProgress": False}
                print(f"CI Shepherd #{number} Copilot review evidence unknown: {error}", file=sys.stderr)
            observed["ready"] &= observed["attention"] is None
            observed["actionable"] &= observed["attention"] is None and observed["copilotReview"]["state"] not in {
                "waiting", "uncertain", "blocked", "limit", "unavailable"}
        results.gate(chain, observed)
        if len(observed["feedback"]) > 30:
            # Saved attempt holds are not a repair batch. Cap only actionable
            # items, after binding the complete raw inventories for freshness.
            attention = observed["attention"] or "Feedback batch exceeds 30 items; human attention required."
            observed.update(attention=attention, feedback=[], actionable=False, ready=False)
        observed["feedbackRevisions"] = {
            item["id"]: feedback_revisions[item["id"]] for item in observed["feedback"]
            if item["id"] in feedback_revisions}
        timed = wait_state(chain, observed, self.clock())
        if timed == "waiting":
            observed["actionable"] = False
        elif timed in {"due", "superseded"} and kind == "issue":
            observed["actionable"] = (active and not pending_ci and attention is None
                                     and workflow["attention"] is None and workflow["approval"] is None)
        results.gate(chain, observed)
        return observed

    def check_diagnostics(self, checks):
        remaining_pages, remaining_bytes = 10, 32000
        result = []

        def bounded_get(method, endpoint, body):
            nonlocal remaining_pages
            if remaining_pages <= 0:
                raise IncompleteInventory("annotation request budget exhausted")
            remaining_pages -= 1
            return self.transport(method, endpoint, body)

        for check in checks:
            if check["status"] != "completed" or check["conclusion"] in {"success", "neutral", "skipped", "cancelled"}:
                continue
            item = {"checkId": check["id"], "complete": False, "infrastructure": False}
            try:
                output = check.get("output")
                if not isinstance(output, dict) or type(output.get("annotations_count")) is not int or output["annotations_count"] < 0:
                    raise IncompleteInventory("annotation count unavailable")
                for key in ("title", "summary", "text"):
                    if output.get(key) is not None and not isinstance(output[key], str):
                        raise IncompleteInventory("malformed check output")
                item["outputRevision"] = hashlib.sha256(json.dumps(
                    [output.get(key) for key in ("title", "summary", "text")],
                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()
                # REST returns [{path, annotation_level, title, message,
                # raw_details, ...}], with nullable text and NO id.
                # https://docs.github.com/en/rest/checks/runs#list-check-run-annotations
                if remaining_pages <= 0 or remaining_bytes <= 0:
                    raise IncompleteInventory("aggregate annotation budget exhausted")
                annotations = live.API(bounded_get, repository_id=self.repository_id).pages(
                    f"{self.prefix}/check-runs/{check['id']}/annotations", identity_key=None,
                    max_pages=remaining_pages, max_bytes=remaining_bytes)
                if len(annotations) != output["annotations_count"]:
                    raise IncompleteInventory("annotation count contradicts complete pages")
                remaining_bytes -= len(json.dumps(annotations, ensure_ascii=True).encode())
                item["revision"] = hashlib.sha256(json.dumps(
                    annotations, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
                failures = []
                snippets = []
                for annotation in annotations:
                    if not isinstance(annotation, dict):
                        raise IncompleteInventory("malformed annotation")
                    if (not isinstance(annotation.get("path"), str)
                            or not isinstance(annotation.get("blob_href"), str)
                            or type(annotation.get("start_line")) is not int or annotation["start_line"] <= 0
                            or type(annotation.get("end_line")) is not int or annotation["end_line"] < annotation["start_line"]
                            or any(key not in annotation or annotation[key] is not None and (
                                type(annotation[key]) is not int or annotation[key] <= 0)
                                   for key in ("start_column", "end_column"))):
                        raise IncompleteInventory("annotation location missing/malformed")
                    for key in ("title", "message", "raw_details"):
                        if key not in annotation or annotation[key] is not None and not isinstance(annotation[key], str):
                            raise IncompleteInventory("annotation text missing/malformed")
                    if "annotation_level" not in annotation or annotation["annotation_level"] not in {"notice", "warning", "failure", None}:
                        raise IncompleteInventory("annotation level malformed")
                    if annotation.get("annotation_level") == "failure":
                        failures.append(annotation)
                    snippets.append({"path": annotation["path"][:500],
                                     "start_line": annotation["start_line"], "end_line": annotation["end_line"],
                                     "annotation_level": annotation["annotation_level"],
                                     **{key: annotation[key][:500] if annotation[key] is not None else None
                                        for key in ("title", "message", "raw_details")}})
                # Match the failure-level GitHub runner error, not job names or
                # warnings: "The hosted runner lost communication with the server."
                # Some runner errors instead start with "The runner <name> ...".
                infrastructure = (bool(failures) and all(annotation.get("annotation_level") is not None
                                                        for annotation in annotations) and all(re.search(
                    r"\bThe (?:hosted )?runner\b[^\n]*\blost communication with the server\b",
                    " ".join(annotation.get(key) or "" for key in ("title", "message", "raw_details")),
                    re.IGNORECASE) for annotation in failures))
                item.update(complete=True, infrastructure=infrastructure,
                            output={key: (output.get(key) or "")[:1000] for key in ("title", "summary", "text")},
                            annotations=snippets)
            except (IncompleteInventory, ValueError, KeyError, TypeError) as error:
                item["unknown"] = str(error)
                print(f"CI Shepherd check {check['id']} diagnostics unknown: {error}", file=sys.stderr)
            result.append(item)
        return result

    def guard(self, chain, observation, *, effect=True, require_managed=True):
        if not require_managed and effect:
            # Only pending-adoption notifications may inspect an unmanaged child.
            raise ValueError("require_managed=False is only valid for a non-effect notification read")
        if effect:
            repository = self.api.get(self.prefix)
            if repository.get("id") != self.repository_id or repository.get("full_name") != self.repository:
                raise ValueError("target repository changed")
        fresh = self.observe(chain)
        if require_managed:
            if not fresh["managed"]:
                raise ValueError("subject management removed or hands-off")
        else:
            # The fingerprint excludes management/closure. Derive the narrow
            # notification exception from this read to catch intervening takeover.
            pending_adoption = (chain["child"] is not None and fresh["number"] == chain["child"]
                                 and fresh["state"] == "open" and fresh["originManaged"] is True
                                 and fresh["handsOff"] is False
                                 and chain["childAdoption"] in {"sent", "uncertain"})
            if not pending_adoption:
                raise ValueError("subject management removed or hands-off")
        if effect and fresh["attention"] is not None:
            raise ValueError(fresh["attention"])
        if effect and fresh["workflowAttention"] is not None:
            raise ValueError(fresh["workflowAttention"] + " No repair.")
        if effect and fresh["approval"] is not None:
            raise ValueError("current-head workflows require human approval; no repair")
        if effect and (fresh["pendingCI"] or fresh.get("ciWait") is not None and not fresh["actionable"]):
            raise ValueError("current-head CI wait; no repair")
        # Notifications independently refresh and match the concrete blocker;
        # they may start with no cached worker receipt after a process restart.
        # Paid decisions always bind the complete raw worker evidence.
        if fingerprint(fresh, worker_evidence=effect) != fingerprint(observation, worker_evidence=effect):
            raise ValueError("subject basis changed")
        self.authority_guard()
        if effect and chain["rounds"] > self.binding.round_limit:
            raise ValueError("lifetime action round limit exceeded")
        if effect and (chain["state"] != "open" or state.chain_spend(chain) > state.chain_allowance(self.ledger) or state.repository_spend(
                self.ledger, self.clock()) > state.REPOSITORY_ALLOWANCE):
            raise ValueError("chain/repository authority or credit allowance exhausted")
        if effect and self.packet_time is not None:
            now = self.clock()
            if not self.packet_time <= now < self.packet_time + timedelta(minutes=10) or (
                    self.high_water is not None and now < self.high_water):
                raise ValueError("packet expired or host clock rolled backwards")
            self.high_water = now
        return fresh

    def task_detail(self, task_id, chain, operation):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("invalid task id")
        task = self.api.get(f"agents/repos/{self.repository}/tasks/{task_id}")
        return self.verify_task(task, task_id, chain, operation)

    def verify_task(self, task, task_id, chain, operation):
        if (task["id"] != task_id or task["repository"]["id"] != self.repository_id
                or task["creator"]["id"] != self.actor["id"] or not isinstance(task.get("sessions"), list)
                or type(task.get("session_count")) is not int
                or task["session_count"] != len(task["sessions"]) or not task["sessions"]
                or not isinstance(task.get("artifacts"), list)
                or task["state"] not in WORKER_STATES):
            raise IncompleteInventory("task/session/artifact source identity unavailable")
        if task.get("updated_at") is not None:
            issue_pr.timestamp(task["updated_at"])
        number = chain["child"] or chain["origin"]
        pr = self.mapping(number) if chain["child"] is not None or chain["kind"] == "pr" else None
        branch = pr["head"]["ref"] if pr is not None else None
        correlations = []
        nano, billed = 0, True
        expected = {"chain": chain["id"], "operation": operation["id"], "origin": chain["origin"]}
        for session in task["sessions"]:
            if session["task_id"] != task_id or session["repository"]["id"] != self.repository_id:
                raise ValueError("session source mismatch")
            session_state = session.get("state")
            if not isinstance(session_state, str) or session_state not in WORKER_STATES:
                raise ValueError("session state unavailable or invalid")
            # Historical terminal outcomes can differ from the aggregate.
            # A terminal task with any live session is conflicting evidence,
            # never permission to release a worker slot or its reservation.
            if task["state"] in state.TERMINAL and session_state not in state.TERMINAL:
                raise ValueError("terminal task has a nonterminal session")
            issue_pr.text(session["id"], "task session ID")
            if (session.get("user", {}).get("id") != self.actor["id"]
                    or session.get("base_ref") != "main"
                    or not isinstance(session.get("head_ref"), str) or not session["head_ref"]
                    or branch is not None and session["head_ref"] != branch):
                raise ValueError("session actor/branch mismatch")
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
        receipt = results.summarize(task, operation, pr)
        self.worker_result_heads[operation["id"]] = pr["head"]["sha"] if pr is not None else None
        self.worker_result_versions[operation["id"]] = results.task_version(task)
        self.worker_revisions[operation["id"]] = hashlib.sha256(json.dumps(
            task, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()
        if task["state"] in state.TERMINAL:
            self.worker_results[operation["id"]] = receipt
        return task, state.amount(nano / 1e9) if billed else None

    def admission_slots(self, head_ref):
        return state.worker_slots(self.ledger)

    def reconcile_workers(self, *, adopt_children=True, acquisition=True):
        details = {}
        self.worker_results = {}
        self.worker_result_heads = {}
        self.worker_result_versions = {}
        self.worker_revisions = {}
        for chain in self.ledger["chains"]:
            for operation in chain["operations"]:
                task_id = operation["taskId"]
                if task_id is None:
                    continue
                try:
                    # One fresh direct GET per saved ID, even for an unchanged
                    # completed receipt. No catalog or foreign-task inspection.
                    if task_id not in details:
                        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
                            raise ValueError("invalid task ID")
                        try:
                            details[task_id] = self.api.get(f"agents/repos/{self.repository}/tasks/{task_id}")
                        except IncompleteInventory as error:
                            details[task_id] = error
                    if isinstance(details[task_id], IncompleteInventory):
                        raise details[task_id]
                    task, usage = self.verify_task(details[task_id], task_id, chain, operation)
                    if usage is not None and operation["workerActual"] is not None and usage < operation["workerActual"]:
                        raise ValueError("task billing moved backwards")
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
                    self.worker_results.pop(operation["id"], None)
                    self.worker_revisions.pop(operation["id"], None)
                    operation.update(workerState="unknown", state="waiting")
                    if not operation["workerReserved"]:
                        operation["workerReserved"] = state.new_worker_reservation(self.ledger, chain, self.clock())
                    print(f"CI Shepherd task {operation['taskId']} needs human verification: {error}", file=sys.stderr)
                    continue
                operation["workerState"] = task["state"]
                operation["workerVersion"] = {key: task.get(key) for key in ("state", "session_count", "updated_at")}
                if usage is not None:
                    if usage != operation["workerActual"]:
                        operation.update(workerActual=usage, workerAt=issue_pr.stamp(self.clock()))
                    operation["workerReserved"] = 0
                if task["state"] not in state.TERMINAL or usage is None:
                    # Retain an existing unknown hold exactly: aging other
                    # spending is neither billing evidence nor a new admission.
                    # A resumed previously billed task needs a fresh hold.
                    if not operation["workerReserved"]:
                        operation["workerReserved"] = state.new_worker_reservation(self.ledger, chain, self.clock())
                if task["state"] not in state.TERMINAL:
                    operation["state"] = "waiting"
                else:
                    state.finish(operation, "completed" if task["state"] == "completed" else "failed")
                    if acquisition:
                        results.settle(self, chain, operation, task)
                    # Failed workers can still report a child PR.
                    if (acquisition and chain["kind"] == "issue" and chain["child"] is None and adopt_children and self.write
                            and operation["workerState"] in state.TERMINAL):
                        # Artifact verification can fail independently of a verified billing receipt.
                        self.persist()
                        self.adopt_artifact(task, chain, operation)
        if acquisition:
            results.reconcile_publications(self)

    def adopt_artifact(self, task, chain, operation):
        pulls = [artifact["data"] for artifact in task["artifacts"]
                 if artifact.get("provider") == "github" and artifact.get("type") == "pull"]
        branches = [artifact["data"] for artifact in task["artifacts"]
                    if artifact.get("provider") == "github" and artifact.get("type") == "branch"]
        if not pulls and not branches and task["state"] != "completed":
            # Failures with no reported artifacts can continue within the existing
            # budget. Completion without a mappable child still needs a human.
            return
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

    def _settle_child_adoption(self, chain, hands_off, managed_child):
        """Settle one origin/child observation; False permits a first label send."""
        if hands_off:
            chain["state"] = "hands-off"
            return True
        if managed_child:
            # Confirmation may clear adoption uncertainty, not a newer native stop.
            if (chain["state"] == "human" and chain["childAdoption"] in {"sent", "uncertain"}
                    and not native_handoff(chain)):
                chain["state"] = "open"
            chain["childAdoption"] = "confirmed"
            self.persist()
            return True
        if chain["childAdoption"] in {"sent", "uncertain"}:
            # An unconfirmed controller write is not evidence of human takeover.
            chain["state"] = "human"
            return True
        return False

    def adopt_child(self, chain, observed=None):
        if chain["childAdoption"] == "confirmed":
            return
        if observed is not None:
            # Settle from the same observation used to classify this sweep.
            hands_off = not observed["originManaged"] or observed["state"] != "open" or observed["handsOff"]
            self._settle_child_adoption(chain, hands_off, observed["managed"])
            return
        pr = self.mapping(chain["child"])
        origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
        if origin["node_id"] != chain["node"] or pr["node_id"] != chain["childNode"]:
            raise ValueError("origin/child adoption authority changed")
        hands_off = not managed(origin) or pr["state"] != "open" or "shepherd-hands-off" in [label["name"] for label in pr["labels"]]
        if self._settle_child_adoption(chain, hands_off, managed(pr)):
            return
        chain["childAdoption"] = "sent"
        self.persist()
        pr = self.mapping(chain["child"])
        origin = self.api.get(f"{self.prefix}/issues/{chain['origin']}")
        if (not managed(origin) or origin["node_id"] != chain["node"] or pr["node_id"] != chain["childNode"]
                or pr["state"] != "open" or "shepherd-hands-off" in [label["name"] for label in pr["labels"]]):
            raise ValueError("child adoption management changed before send")
        self.adoption_effect_guard()
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
        self.reconcile_workers()
        for chain in self.ledger["chains"]:
            # Never retry an uncertain send; settle it from observe() below.
            if self.write and chain["child"] is not None and chain["childAdoption"] == "reserved":
                self.adopt_child(chain)
        intake = ([self.mapping(self.binding.subject)] if self.binding.subject is not None else
                  self.api.pages(f"{self.prefix}/issues", query={"state": "open", "labels": "shepherd-adopted"}))
        for candidate in intake:
            number = candidate["number"]
            if number in {121, self.tracker} or not managed(candidate) or state.find_chain(self.ledger, number) is not None:
                continue
            if self.binding == bindings.UPSTREAM_ALL:
                if "pull_request" not in candidate:
                    print(f"CI Shepherd upstream issue #{number} skipped; PR-only intake.", file=sys.stderr)
                    continue
                verified = self.mapping(number)
                if verified["node_id"] != candidate["node_id"] or not managed(verified):
                    raise ValueError("upstream intake identity/management changed")
            state.adopt(self.ledger, number, "pr" if self.binding.subject is not None or "pull_request" in candidate else "issue", candidate["node_id"])
        observations = {}
        for chain in self.ledger["chains"]:
            observed = self.observe(chain)
            observations[observed["number"]] = observed
            if self.write and chain["child"] is not None and chain["childAdoption"] in {"sent", "uncertain"}:
                # Adoption settlement and chain classification must share one read.
                self.adopt_child(chain, observed)
            resolving_ambiguous_adoption = (
                chain["state"] == "human" and chain["child"] is not None
                and chain["childAdoption"] in {"sent", "uncertain"})
            if not observed["managed"] and not resolving_ambiguous_adoption:
                chain["state"] = "closed" if observed["state"] == "closed" else "hands-off"
            elif chain["state"] in {"closed", "hands-off"}:
                chain["state"] = "open"
        return observations

    def next_action(self, chain, observation):
        operation = chain["operations"][-1] if chain["operations"] else None
        unbilled = state.worker_billing_pending(chain)
        credit_blocked = (state.chain_spend(chain) + state.NATIVE_RESERVE > state.chain_allowance(self.ledger)
                          or state.repository_spend(self.ledger, self.clock()) + state.NATIVE_RESERVE
                          > state.REPOSITORY_ALLOWANCE)
        if chain["state"] == "hands-off":
            blocker = "Adoption removed or hands-off label applied; no new repairs."
        elif chain["state"] == "closed":
            blocker = "Origin or child closed; no new repairs."
        elif chain["state"] == "human":
            blocker = "Human handoff; no new repairs."
        elif observation["workflowAttention"] is not None:
            blocker = observation["workflowAttention"] + " No inference."
        elif observation["approval"] is not None:
            blocker = "Current-head workflows require human approval; no inference."
        elif observation["attention"] is not None:
            blocker = observation["attention"] + " No inference."
        elif operation is not None and operation["workerState"] == "waiting_for_user":
            blocker = "Worker needs human input; open the task. No inference."
        elif observation.get("copilotReview", {}).get("state") in {"waiting", "uncertain"}:
            blocker = "Waiting for Copilot review / uncertain request; observe only, never retry."
        elif observation.get("copilotReview", {}).get("state") in {"blocked", "limit"}:
            blocker = "Copilot review request rejected/unverifiable or lifetime limit reached; human attention required."
        elif state.pending(chain):
            blocker = "Tracked work / uncertain send; observe only, never retry."
        elif wait_state(chain, observation, self.clock()) == "waiting":
            timed = operation["wait"]
            blocker = f"Deferred until {timed['until']}: {timed['reason']} No inference."
        elif unbilled and credit_blocked:
            blocker = "Tracked worker finished; billing unavailable, reservation retained. No new paid repair."
            if chain["rounds"] >= self.binding.round_limit:
                blocker += f" Lifetime action round limit ({self.binding.round_limit}) also reached."
        elif chain["rounds"] >= self.binding.round_limit:
            blocker = f"Lifetime action round limit ({self.binding.round_limit}) reached; human attention required."
        elif chain["id"] in self.admission_reasons:
            blocker = self.admission_reasons[chain["id"]]
        elif credit_blocked:
            blocker = ("Lifetime allowance exhausted; human attention required."
                       if state.chain_spend(chain) >= state.chain_allowance(self.ledger)
                       else "Credit headroom cannot cover native admission; no inference.")
        elif (chain["kind"] == "issue" and chain["child"] is None and operation is not None
              and operation["state"] == "completed" and operation["taskId"] is not None):
            blocker = "Completed task has no verified child PR; human handoff required."
        elif observation["ready"]:
            blocker = "Current-head checks and approval verified; human merge required."
        elif observation["pendingCI"]:
            blocker = "Waiting for current-head CI; no inference."
        elif observation.get("ciWait") is not None and not observation["actionable"]:
            blocker = observation["ciWait"] + " No inference."
        elif not observation["actionable"]:
            blocker = ("Matching saved worker attempt held; substantive new evidence required. No inference."
                       if observation.get("attemptHold") else
                       "Copilot review request due; no inference." if observation.get("copilotReview", {}).get("state") == "due"
                       and observation["copilotReview"]["green"] and not observation["copilotReview"]["draft"]
                       else "Waiting for human review / supported new feedback; no inference.")
        elif observation.get("reviewOnly"):
            blocker = "Bounded review-only repair batch due; CI still requires wait/rerun."
        else:
            blocker = "Bounded repair batch due."
        if unbilled and not credit_blocked:
            blocker += " Finished worker billing unavailable; reservation retained."
        if any(record["actual"] is None for record in chain.get("reviews", [])):
            blocker += " Copilot review billing unavailable; admission reservation retained."
        return blocker

    def status(self, chain, observation, now):
        operation = chain["operations"][-1] if chain["operations"] else None
        lane = operation["lane"] if operation else (
            "cloud" if chain["escalated"] or self.binding != bindings.FORK else "local")
        actual = sum(sum(op[key] or 0 for key in ("nativeActual", "workerActual")) for op in chain["operations"])
        reserved = state.chain_spend(chain) - actual
        dispositions = {value: list(chain["dispositions"].values()).count(value)
                        for value in ("addressed", "declined", "needs-human")}
        blocker = self.next_action(chain, observation)
        task = "" if not operation or not operation["taskId"] else (
            f"\nTask: https://github.com/{self.repository}/tasks/{operation['taskId']}")
        last = "" if operation is None else f" Last action: {operation['state']}."
        rechecking = sum(chain["dispositions"].get(item["id"]) == "needs-human" for item in observation["feedback"])
        reevaluation = (f"\nRe-evaluating {rechecking} legacy completion entries; no verified resolution or human blocker."
                        if rechecking else "")
        return (f"[automated] CI Shepherd - {lane}\n\nLocal attempts: {chain['localAttempts']}/2; "
                f"action rounds: {chain['rounds']}/{self.binding.round_limit}.\nActual credits: {actual:g}; outstanding reservation: {reserved:g}. "
                "Unknown billing retains its reservation; these are not hard billing caps.\n\n"
                f"Recorded feedback dispositions: {dispositions['addressed']} addressed, {dispositions['declined']} declined, "
                f"{dispositions['needs-human']} needs-human.{reevaluation}\n{blocker}{last}\nEvidence: {observation['url']}{task}\n"
                f"{state.STATUS_MARKER}\nChain: {chain['id']}")

    def log_status(self, chain, observation, now):
        operation = chain["operations"][-1] if chain["operations"] else None
        task = operation["taskId"] if operation else None
        worker = (operation["workerState"] or operation["state"]) if operation else "not started"
        unknown = any(op["nativeActual"] is None or op["workerReserved"] > 0 or (
            op["taskId"] is not None and op["workerActual"] is None)
                      for op in chain["operations"]) or any(
            record["actual"] is None for record in chain.get("reviews", []))
        actual = sum(sum(op[key] or 0 for key in ("nativeActual", "workerActual")) for op in chain["operations"])
        reserved = state.chain_spend(chain) - actual
        summary = (f"CI Shepherd {self.repository} {observation['kind']} #{observation['number']} "
                   f"head {observation['head']}\nTracked task: {task or 'no saved ID'}; state: {worker}. "
                   f"Action rounds: {chain['rounds']}/{self.binding.round_limit}.\nActual credits: {actual:g}; outstanding reservation: {reserved:g}; "
                   f"billing: {'unknown amounts remain reserved' if unknown else 'known reported amounts'}.\n"
                   f"Next action: {self.next_action(chain, observation)}")
        if observation.get("workHistory") is not None:
            summary += "\n" + history.describe(observation["workHistory"])
        if observation["workflowAttention"] is not None:
            summary += "\n" + observation["workflowAttention"]
        print(summary)

    def publish_status(self, chain, observation, now):
        if self.binding != bindings.FORK:
            # Upstream comments are limited to fixed delayed human reminders.
            # Plain hosted logs remain available when the native job skips.
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

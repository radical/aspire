"""Item-bound issue reporting; deliberately excludes tasks, Git writes and PRs."""

from copy import deepcopy
import re
from urllib.parse import parse_qs, urlparse

from github import LostResponse, Response
import live
import local
import pilot_binding as bindings
import pilot_github
import pilot_state as state
import pilot_storage as storage
import work_items as items


REPOSITORY_IDS = {"radical/aspire": live.REPOSITORY_ID, "microsoft/aspire": bindings.UPSTREAM.repository_id}


class WorkItemTransport(pilot_github.PilotTransport):
    def __init__(self, token, tracker, authority, control, *, binding=bindings.FORK):
        super().__init__(token, write=True, tracker=tracker, authority=authority, binding=binding)
        self.tracker, self.authority = tracker, authority
        self.control = deepcopy(items.validate_control(control))

    def validate_endpoint(self, method, endpoint, body):
        path = urlparse(endpoint)
        if path.scheme or path.netloc or path.fragment or any(part in {".", ".."} for part in path.path.split("/")):
            raise ValueError("invalid work-item endpoint")
        if self.memory_capability(method, endpoint, body):
            return
        issue = self.control["issue"]
        issue_path = f"repos/{issue['repository']}/issues/{issue['number']}"
        prefix = "repos/radical/aspire"
        reads = {"user", "users/radical", prefix, f"{prefix}/issues/{self.tracker}",
                 f"{prefix}/issues/{self.tracker}/comments", f"repos/{issue['repository']}",
                 issue_path, issue_path + "/comments"}
        if method == "GET" and path.path in reads and body is None:
            if path.query:
                query = parse_qs(path.query, strict_parsing=True)
                if (not path.path.endswith("/comments") or set(query) != {"page", "per_page"}
                        or query["per_page"] != ["100"] or len(query["page"]) != 1
                        or not re.fullmatch(r"[1-9][0-9]*", query["page"][0]) or int(query["page"][0]) > 10):
                    raise ValueError("invalid bounded comment pagination")
            return
        if (method == "PATCH" and not path.query and path.path == f"{prefix}/issues/comments/{self.authority}"
                and isinstance(body, dict) and set(body) == {"body"}
                and state.parse(body["body"])["repository"] == "radical/aspire"):
            return
        if (method == "POST" and not path.query and path.path == issue_path + "/comments"
                and self.control["authority"]["issue_comment"] and self.control["action"] != "pause"
                and isinstance(body, dict) and set(body) == {"body"} and isinstance(body["body"], str)
                and len(body["body"].encode()) <= 14000
                and body["body"].startswith("[automated] CI Shepherd work-item result; human direction required.")
                and f"<!-- ci-shepherd:work-item:{self.control['id']}:" in body["body"]):
            return
        raise ValueError("work-item endpoint/effect is not authorized")


class WorkItemGitHub(pilot_github.PilotGitHub):
    def __init__(self, token, tracker, authority, node, *, revision, control, transport=None):
        self.token, self.revision = token, revision
        self.control = deepcopy(items.validate_control(control))
        transport = transport if transport is not None else WorkItemTransport(token, tracker, authority, control)
        super().__init__(transport, tracker, authority, node, write=True, binding=bindings.FORK)
        storage.install_local_policy(self)

    def enabled(self):
        value = local.metadata("repos/radical/aspire/actions/variables/CI_SHEPHERD_ENABLE", self.token)
        if value.get("name") != "CI_SHEPHERD_ENABLE" or value.get("value") not in {"true", "false"}:
            raise ValueError("Shepherd enable configuration unavailable")
        return value["value"] == "true"

    def authority_guard(self):
        local.require_idle_actions(self.token)
        super().authority_guard()

    def verify_issue(self, issue):
        if issue != self.control["issue"]:
            raise ValueError("linked issue identity changed")
        prefix = "repos/" + issue["repository"]
        repository = self.api.get(prefix)
        if (repository.get("id") != REPOSITORY_IDS[issue["repository"]]
                or repository.get("full_name") != issue["repository"]):
            raise ValueError("linked repository identity mismatch")
        value = self.api.get(f"{prefix}/issues/{issue['number']}")
        if (value.get("number") != issue["number"] or value.get("node_id") != issue["node_id"]
                or value.get("state") != "open" or "pull_request" in value):
            raise ValueError("linked tracker must be the exact open issue")
        if any(label.get("name") == "shepherd-hands-off" for label in value.get("labels", [])):
            raise ValueError("linked issue is hands-off")

    def guard_work_item(self, control):
        if control["issue"] != self.control["issue"] or control["id"] != self.control["id"]:
            raise ValueError("item binding changed")
        local.require_source(self.revision)
        if not self.enabled():
            raise ValueError("Shepherd disabled; no new work-item effects")
        self.authority_guard()
        self.verify_issue(control["issue"])
        self.control = deepcopy(control)
        if isinstance(self.transport, WorkItemTransport):
            self.transport.control = deepcopy(control)

    def issue_comments(self, issue):
        self.verify_issue(issue)
        # Pagination aliases must be validated against the linked repository,
        # not the controller fork when the tracker lives upstream.
        api = live.API(self.transport, repository_id=REPOSITORY_IDS[issue["repository"]])
        comments = api.pages(f"repos/{issue['repository']}/issues/{issue['number']}/comments")
        return [{"id": comment["id"], "body": comment["body"], "owned": self.owned(comment)}
                for comment in comments]

    def post_comment(self, issue, body, before_send):
        import work_item_receiver as receiver
        if issue != self.control["issue"]:
            raise ValueError("exact linked issue required for reporting")
        if not receiver.comment_authorized(self, self.control):
            raise ValueError("explicit issue-comment permission required")
        latest = self.read_authority()
        record = next((entry for entry in latest.get("workItems", []) if entry["id"] == self.control["id"]), None)
        if (record is None or record["control"] != self.control or record["comment"] is None
                or not record["assignments"]
                or record["comment"]["assignment_id"] != record["assignments"][-1]["id"]
                or record["comment"]["delivery"] != "delivering" or record["comment"]["body"] != body):
            raise ValueError("canonical exact comment send intent required")
        self.guard_work_item(self.control)
        before_send()
        if not receiver.comment_authorized(self, self.control):
            raise ValueError("explicit issue-comment permission revoked")
        response = self.transport("POST", f"repos/{issue['repository']}/issues/{issue['number']}/comments",
                                  {"body": body})
        if (not isinstance(response, Response) or response.status != 201 or not isinstance(response.payload, dict)
                or type(response.payload.get("id")) is not int or response.payload["id"] <= 0
                or response.payload.get("body") != body or not self.owned(response.payload)):
            raise LostResponse("issue-comment response uncertain; never blindly replay")
        return response.payload["id"]


    def replace_comment(self, issue, identity, previous, body, before_send):
        import work_item_receiver as receiver
        if issue != self.control["issue"] or not receiver.comment_authorized(self, self.control):
            raise ValueError("exact authorized issue required for replacement")
        latest = self.read_authority()
        record = next((entry for entry in latest.get("workItems", [])
                       if entry["id"] == self.control["id"]), None)
        comment = record["comment"] if record is not None else None
        if (record is None or record["control"] != self.control or comment is None
                or comment["assignment_id"] != record["assignments"][-1]["id"]
                or comment.get("replacement_id") != identity or comment["delivery"] != "delivering"
                or comment["body"] != body):
            raise ValueError("canonical exact replacement intent required")
        self.guard_work_item(self.control)
        before_send()
        matches = [entry for entry in self.issue_comments(issue) if entry["id"] == identity]
        if (len(matches) != 1 or not matches[0]["owned"] or matches[0]["body"] != previous
                or not receiver.comment_authorized(self, self.control)):
            raise ValueError("report changed or permission revoked before replacement")
        # GitHub comment PATCH has no conditional body update. Recheck ownership
        # and exact old bytes immediately before sending; concurrent human edits
        # after this read remain an API limitation, not an atomic-CAS guarantee.
        response = self.transport("PATCH", f"repos/{issue['repository']}/issues/comments/{identity}",
                                  {"body": body})
        if (not isinstance(response, Response) or response.status != 200
                or not isinstance(response.payload, dict) or response.payload.get("id") != identity
                or response.payload.get("body") != body or not self.owned(response.payload)):
            raise LostResponse("issue-report replacement uncertain; never blindly replay")
        return identity


class CloudWorkItemTransport(pilot_github.PilotTransport):
    """A task-creation capability separate from legacy upstream repair effects."""

    def __init__(self, token, tracker, authority, control):
        self.control = deepcopy(items.validate_control(control))
        self.approved_request = None
        binding = bindings.FORK if control["issue"]["repository"] == "radical/aspire" else bindings.UPSTREAM_ALL
        super().__init__(token, write=True, binding=binding, tracker=tracker, authority=authority)
        self.task_repository = binding.repository

    def validate_endpoint(self, method, endpoint, body):
        if self.memory_capability(method, endpoint, body):
            return
        target = self.binding.repository
        if method == "POST" and endpoint == f"agents/repos/{target}/tasks":
            if self.approved_request is None or body != self.approved_request:
                raise ValueError("exact canonical cloud send intent required")
            return
        if method != "GET" and not (
                method == "PATCH" and endpoint == f"repos/radical/aspire/issues/comments/{self.authority}"):
            raise ValueError("cloud adapter cannot publish comments, labels, PRs or ownership changes")
        super().validate_endpoint(method, endpoint, body)


class FollowupTransport(WorkItemTransport):
    """Exact saved-PR reads only; no task POST, PR import or general reads."""

    def __init__(self, token, tracker, authority, control):
        binding = bindings.FORK if control["issue"]["repository"] == "radical/aspire" else bindings.UPSTREAM_ALL
        super().__init__(token, tracker, authority, control, binding=binding)
        self.task_id = None
        self.saved_pull = None
        self.tracker_search = None
        self.tracker_issues = set()
        self.tracker_runs = set()
        self.read_only = False

    def validate_endpoint(self, method, endpoint, body):
        if self.memory_capability(method, endpoint, body):
            return
        if self.read_only and method != "GET":
            raise ValueError("read-only follow-up transport prohibits every GitHub mutation")
        if method == "PATCH":
            if (endpoint != f"repos/radical/aspire/issues/comments/{self.authority}"
                    or not isinstance(body, dict) or set(body) != {"body"}):
                raise ValueError("follow-up authority target namespace mismatch")
            ledger = state.parse(body["body"])
            bound = [item for item in ledger.get("workItems", []) if item["id"] == self.control["id"]]
            if (ledger["repository"] != self.control["issue"]["repository"]
                    or len(bound) != 1 or bound[0]["control"] != self.control):
                raise ValueError("follow-up authority target/control mismatch")
            return
        issue = self.control["issue"]
        occurrence = self.control["occurrence"]
        reads = set()
        if self.task_id is not None:
            items.identifier(self.task_id, "follow-up task")
            reads.add(f"agents/repos/{issue['repository']}/tasks/{self.task_id}")
        if self.saved_pull is not None:
            items.issue_pr.positive(self.saved_pull, "saved follow-up PR")
            reads.add(f"repos/{issue['repository']}/pulls/{self.saved_pull}")
        if occurrence is not None:
            prefix = f"repos/{occurrence['repository']}/actions"
            reads.update({
                f"{prefix}/runs/{occurrence['run_id']}/attempts/{occurrence['run_attempt']}",
                f"{prefix}/jobs/{occurrence['job_id']}",
            })
        if method == "GET" and endpoint in reads and body is None:
            return
        if method == "GET" and body is None and (
                endpoint == self.tracker_search or endpoint in self.tracker_issues
                or endpoint in self.tracker_runs):
            return
        super().validate_endpoint(method, endpoint, body)


class FollowupGitHub(WorkItemGitHub):
    def __init__(self, token, tracker, authority, node, *, revision, control, transport=None):
        transport = transport if transport is not None else FollowupTransport(token, tracker, authority, control)
        binding = bindings.FORK if control["issue"]["repository"] == "radical/aspire" else bindings.UPSTREAM_ALL
        self.token, self.revision = token, revision
        self.control = deepcopy(items.validate_control(control))
        pilot_github.PilotGitHub.__init__(
            self, transport, tracker, authority, node, write=True, binding=binding)
        storage.install_local_policy(self)


class ReadOnlyFollowupGitHub(FollowupGitHub):
    """Simulated receipts on disk; remote authority stays the freshness anchor."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transport.read_only = True
        self.remote_expected = None
        self.simulated = None
        self.write = False
        self.memory_authorized = False

    def read_authority(self):
        observed = super().read_authority()
        if self.remote_expected is None:
            self.remote_expected = deepcopy(observed)
        if observed != self.remote_expected:
            raise ValueError("repository authority changed during read-only collection")
        return deepcopy(self.simulated if self.simulated is not None else observed)

    def authority_guard(self):
        local.require_idle_actions(self.token)
        if super().read_authority() != self.remote_expected or self.observed_revision != self.storage_revision:
            raise ValueError("repository authority changed during read-only collection")

    def persist(self):
        storage.size(self, self.ledger)
        storage.validate_candidate(self, self.ledger)
        self.authority_guard()
        self.simulated = deepcopy(self.ledger)
        self.expected = deepcopy(self.ledger)


class CloudWorkItemGitHub(WorkItemGitHub):
    def __init__(self, token, tracker, authority, node, *, revision, control, transport=None):
        self.token, self.revision = token, revision
        self.control = deepcopy(items.validate_control(control))
        binding = bindings.FORK if control["issue"]["repository"] == "radical/aspire" else bindings.UPSTREAM_ALL
        transport = transport if transport is not None else CloudWorkItemTransport(token, tracker, authority, control)
        pilot_github.PilotGitHub.__init__(
            self, transport, tracker, authority, node, write=True, binding=binding)
        storage.install_local_policy(self)

    def start_task(self, body, before_send):
        import work_item_execution as execution
        latest = self.read_authority()
        record = next((item for item in latest.get("workItems", []) if item["id"] == self.control["id"]), None)
        assignment = record["assignments"][-1] if record and record["assignments"] else None
        if (record is None or record["control"] != self.control or assignment is None
                or assignment.get("execution", {}).get("state") != "sending"
                or assignment["execution"]["task_id"] is not None
                or body != execution.request(self, record, assignment)):
            raise ValueError("canonical cloud send intent mismatch")
        self.guard_work_item(self.control)
        before_send()
        if isinstance(self.transport, pilot_github.PilotTransport):
            self.transport.approved_request = deepcopy(body)
        try:
            response = self.transport("POST", f"agents/repos/{self.repository}/tasks", body)
        finally:
            if isinstance(self.transport, pilot_github.PilotTransport):
                self.transport.approved_request = None
        if (not isinstance(response, Response) or response.status != 201
                or not isinstance(response.payload, dict)):
            raise LostResponse("cloud task response uncertain")
        identity = response.payload.get("id")
        try:
            items.identifier(identity, "returned cloud task")
        except ValueError as error:
            raise LostResponse("cloud task response identity uncertain") from error
        return identity

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
import work_items as items


REPOSITORY_IDS = {"radical/aspire": live.REPOSITORY_ID, "microsoft/aspire": bindings.UPSTREAM.repository_id}


class WorkItemTransport(live.HTTPTransport):
    def __init__(self, token, tracker, authority, control):
        super().__init__(token, write=True)
        self.tracker, self.authority = tracker, authority
        self.control = deepcopy(items.validate_control(control))

    def validate_endpoint(self, method, endpoint, body):
        path = urlparse(endpoint)
        if path.scheme or path.netloc or path.fragment or any(part in {".", ".."} for part in path.path.split("/")):
            raise ValueError("invalid work-item endpoint")
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
        if not self.control["authority"]["issue_comment"] or self.control["action"] == "pause":
            raise ValueError("explicit issue-comment permission required")
        latest = self.read_authority()
        record = next((entry for entry in latest.get("workItems", []) if entry["id"] == self.control["id"]), None)
        if (record is None or record["control"] != self.control or record["comment"] is None
                or record["comment"]["delivery"] != "delivering" or record["comment"]["body"] != body):
            raise ValueError("canonical exact comment send intent required")
        self.guard_work_item(self.control)
        before_send()
        response = self.transport("POST", f"repos/{issue['repository']}/issues/{issue['number']}/comments",
                                  {"body": body})
        if (not isinstance(response, Response) or response.status != 201 or not isinstance(response.payload, dict)
                or type(response.payload.get("id")) is not int or response.payload["id"] <= 0
                or response.payload.get("body") != body or not self.owned(response.payload)):
            raise LostResponse("issue-comment response uncertain; never blindly replay")
        return response.payload["id"]

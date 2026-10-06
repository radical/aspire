from copy import deepcopy
import unittest

from helpers import FakeClock
from github import Response, LostResponse, IncompleteInventory
import pilot_github as pilot
import pilot_state as state
import pilot_feedback as feedback
import json


ACTOR = {"id": 1472, "login": "radical"}


def pr(number=7):
    repository = {"id": 746880239, "full_name": "radical/aspire"}
    return {"id": 1000 + number, "number": number, "node_id": "NODE" + str(number), "state": "open",
            "labels": [{"name": "shepherd-adopted"}], "draft": False, "mergeable": True,
            "requested_reviewers": [], "base": {"repo": repository, "ref": "main"},
            "head": {"repo": repository, "ref": "fix-" + str(number), "sha": "a" * 40},
            "title": "Repair fixture", "body": "Broken normalization", "html_url": "https://github.com/radical/aspire/pull/" + str(number)}


class Transport:
    def __init__(self):
        self.ledger = state.new_ledger()
        self.comments = [{"id": 500, "user": ACTOR, "body": state.render(self.ledger)}]
        self.values = {
            "user": ACTOR, "users/radical": ACTOR,
            "repos/radical/aspire": {"id": 746880239, "full_name": "radical/aspire", "default_branch": "main"},
            "repos/radical/aspire/issues/99": {"id": 999, "number": 99, "node_id": "TRACKER99", "state": "open", "labels": []},
            "repos/radical/aspire/issues/99/comments": self.comments,
        }
        self.writes = []
        self.reads = []
        self.history = []
        self.resolved_reviews = set()
        self.label_write_confirms = True  # controls whether a /labels POST echoes the label back

    def __call__(self, method, endpoint, body):
        if method == "POST" and endpoint == "graphql":
            self.reads.append((method, endpoint, body))
            variables = body["variables"]
            repository = variables["owner"] + "/" + variables["name"]
            value = self.values[f"repos/{repository}/pulls/{variables['number']}"]
            if body["query"] == feedback.QUERY:
                comments = self.values.get(f"repos/{repository}/pulls/{variables['number']}/comments", [])
                start = 0 if variables["after"] is None else int(variables["after"].removeprefix("threads:"))
                page = comments[start:start + 100]
                return Response({"data": {
                    "repository": {"databaseId": value["base"]["repo"]["id"], "nameWithOwner": repository,
                                   "pullRequest": {"id": value["node_id"], "number": value["number"],
                                                   "headRefOid": value["head"]["sha"], "reviewThreads": {
                        "nodes": [{"id": "THREAD" + str(comment["id"]),
                                   "isResolved": comment["id"] in self.resolved_reviews,
                                   "pullRequest": {"id": value["node_id"]}, "comments": {
                                       "nodes": [{"id": comment.get("node_id") or "COMMENT" + str(comment["id"]),
                                                  "fullDatabaseId": str(comment["id"])}],
                                       "pageInfo": {"hasNextPage": False, "endCursor": "comment-end"}}}
                                  for comment in page],
                        "pageInfo": {"hasNextPage": start + 100 < len(comments),
                                     "endCursor": "threads:" + str(start + len(page)) if page else None}}}}}}, {})
            return Response({"data": {"repository": {
                "databaseId": value["base"]["repo"]["id"], "nameWithOwner": repository,
                "pullRequest": {"id": value["node_id"], "number": value["number"], "timelineItems": {
                    "nodes": deepcopy(self.history), "pageInfo": {
                        "hasNextPage": False, "endCursor": "last" if self.history else None}}}}}}, {})
        if method != "GET":
            self.writes.append((method, endpoint, body))
            if endpoint.endswith("/comments/500"):
                self.comments[0]["body"] = body["body"]
                return Response(deepcopy(self.comments[0]), {}, 200)
            if method == "POST" and endpoint.endswith("/labels"):
                payload = body["labels"] if self.label_write_confirms else []
                return Response([{"name": name} for name in payload], {}, 200)
            if method == "POST" and endpoint.endswith("/requested_reviewers"):
                value = self.values[endpoint.removesuffix("/requested_reviewers")]
                value["requested_reviewers"].append(
                    {"id": 175728472, "login": "Copilot", "type": "Bot"})
                return Response(deepcopy(value), {}, 201)
            raise LostResponse("unknown write")
        path = endpoint.split("?")[0]
        self.reads.append((method, endpoint, body))
        if path in self.values:
            value = deepcopy(self.values[path])
            if "/pulls/" in path and path.endswith("/comments"):
                for comment in value:
                    comment.setdefault("node_id", "COMMENT" + str(comment["id"]))
            if isinstance(value, dict) and "/issues/" in path and "number" in value:
                value.setdefault("updated_at", "2026-10-04T00:00:00Z")
            return Response(value, {})
        if path.endswith("/tasks"):
            return Response({"tasks": []}, {})
        if path.endswith("/check-runs"):
            return Response({"check_runs": [], "total_count": 0}, {})
        if path.endswith("/actions/runs"):
            return Response({"workflow_runs": [], "total_count": 0}, {})
        if path.endswith("/status"):
            return Response({"statuses": [], "state": "pending"}, {})
        return Response(deepcopy(self.values.get(path, [])), {})


class PilotGitHubTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.api = pilot.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)

    def test_actor_owned_authority_is_required_and_unambiguous(self):
        self.assertEqual(state.new_ledger(), self.api.read_authority())
        self.transport.comments.append(deepcopy(self.transport.comments[0]))
        self.transport.comments[-1]["id"] = 501
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.api.read_authority()
        self.transport.comments[:] = []
        with self.assertRaises(ValueError):
            self.api.read_authority()

    def test_identity_reverified_and_upstream_never_selected(self):
        self.transport.values["user"] = {"id": 1, "login": "radical"}
        with self.assertRaises(ValueError):
            pilot.PilotGitHub(self.transport, 99, 500, "TRACKER99")
        transport = pilot.PilotTransport("test", write=True)
        for method, endpoint, body in [
            ("POST", "repos/microsoft/aspire/issues/1/comments", {"body": "no"}),
            ("POST", "repos/radical/aspire/pulls/7/merge", {}),
            ("PATCH", "repos/radical/aspire/git/refs/heads/fix-7", {"sha": "a" * 40, "force": True}),
        ]:
            with self.assertRaises(ValueError):
                transport.validate_endpoint(method, endpoint, body)

    def test_intake_two_prs_and_issue_preserves_legacy_observation_only(self):
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Bug", "body": "Broken",
                 "html_url": "https://github.com/radical/aspire/issues/8"}
        self.transport.values["repos/radical/aspire/issues"] = [dict(pr(7), pull_request={}), issue,
                                                              dict(pr(9), pull_request={}),
                                                              dict(pr(121), pull_request={})]
        for number in (7, 9):
            self.transport.values[f"repos/radical/aspire/pulls/{number}"] = pr(number)
        self.transport.values["repos/radical/aspire/issues/8"] = issue
        self.api.read_authority()
        observations = self.api.sweep()
        self.assertEqual({7, 8, 9}, set(observations))
        self.assertEqual(3, len(self.api.ledger["chains"]))
        self.assertEqual([], self.transport.writes)

    def test_owned_status_only_excluded_other_operator_feedback_preserved(self):
        value = pr()
        self.transport.values["repos/radical/aspire/pulls/7"] = value
        self.transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": 601, "user": ACTOR, "body": "[automated] " + state.STATUS_MARKER, "updated_at": "2026-10-04T00:00:00Z"},
            {"id": 602, "user": ACTOR, "body": "Please fix whitespace", "updated_at": "2026-10-04T00:00:00Z"}]
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        chain["statusId"] = 601
        observed = self.api.observe(chain)
        self.assertEqual(["comment:602:2026-10-04T00:00:00Z"], [item["id"] for item in observed["feedback"]])
        chain["dispositions"][observed["feedback"][0]["id"]] = "addressed"
        self.assertEqual([], self.api.observe(chain)["feedback"])

    def test_guard_rejects_stale_head_takeover_and_authority_replacement(self):
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        observed = self.api.observe(chain)
        self.api.persist()
        self.transport.values["repos/radical/aspire/pulls/7"]["head"]["sha"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "basis"):
            self.api.guard(chain, observed)
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"].append({"name": "shepherd-hands-off"})
        with self.assertRaisesRegex(ValueError, "management"):
            self.api.guard(chain, observed)

    def test_pending_ci_and_draft_are_not_merge_ready(self):
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        self.assertFalse(self.api.observe(chain)["ready"])
        self.transport.values["repos/radical/aspire/pulls/7"]["draft"] = True
        self.assertFalse(self.api.observe(chain)["ready"])

    def test_current_head_approval_and_no_requested_reviewer_required(self):
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.transport.values["repos/radical/aspire/pulls/7/reviews"] = [{
            "id": 30, "user": {"id": 10, "login": "reviewer"}, "state": "APPROVED", "commit_id": "a" * 40,
            "body": "", "submitted_at": "2026-10-04T00:00:00Z"}]
        self.transport.values["repos/radical/aspire/commits/" + "a" * 40 + "/status"] = {
            "statuses": [{"id": 20, "context": "test", "state": "success"}], "state": "success"}
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        self.assertTrue(self.api.observe(chain)["ready"])
        self.transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"] = [{"id": 10}]
        self.assertFalse(self.api.observe(chain)["ready"])

    def issue_worker(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        clock = FakeClock()
        operation = state.reserve(self.api.ledger, chain, json.dumps({
            "number": 8, "node": "NODE8", "head": "NODE8", "feedback": []}) + ":round:1", clock(), local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(self.api.ledger, chain, operation, clock())
        state.sent(operation)
        operation["taskId"] = "TASK1"
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        self.transport.values["repos/radical/aspire/pulls"] = [child]
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        self.transport.values["repos/radical/aspire/git/ref/heads/fix-9"] = {
            "ref": "refs/heads/fix-9", "object": {"sha": "a" * 40}}
        task = {"id": "TASK1", "state": "completed", "repository": {"id": 746880239}, "creator": {"id": 1472},
                "session_count": 1, "sessions": [{"id": "SESSION1", "task_id": "TASK1", "repository": {"id": 746880239},
                    "user": {"id": 1472}, "state": "completed",
                    "prompt": pilot.CORRELATION + json.dumps({
                        "chain": chain["id"], "operation": operation["id"], "origin": 8}),
                    "head_ref": "fix-9", "base_ref": "main", "usage": {"type": "ai_credits", "amount": 1500000000}}],
                "artifacts": [{"provider": "github", "type": "pull", "data": {"id": 1009, "global_id": ""}},
                              {"provider": "github", "type": "branch", "data": {"head_ref": "fix-9", "base_ref": "main"}}]}
        self.transport.values["agents/repos/radical/aspire/tasks/TASK1"] = task
        self.api.persist()
        return chain, operation, task

    def test_task_session_artifact_rest_and_branch_mapping_preserve_parent_budget(self):
        chain, operation, task = self.issue_worker()
        self.api.reconcile_workers()
        self.assertEqual((9, 1, 0), (chain["child"], chain["rounds"], chain["localAttempts"]))
        self.assertEqual(3.5, state.chain_spend(chain))
        self.assertEqual(1, len(self.api.ledger["chains"]))
        self.assertEqual("confirmed", chain["childAdoption"])

    def test_ambiguous_child_adoption_write_is_honest_handoff_not_hands_off(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []  # our own adoption label write could not be confirmed
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        self.api.persist()
        self.api.adopt_child(chain)
        # Distinct from genuine human/hands-off removal: the child/history
        # stay bound, the chain is not silently relabeled as removed, and a
        # later confirmation that the label did land can still recover to
        # "confirmed" instead of being stuck.
        self.assertEqual("human", chain["state"])
        self.assertEqual(9, chain["child"])
        self.assertEqual(child["node_id"], chain["childNode"])
        observed = self.api.observe(chain)
        self.assertEqual("Human handoff; no new repairs.", self.api.next_action(chain, observed))
        # Immediate child status requires confirmed management; reminders use the origin.
        self.assertFalse(observed["managed"])
        self.transport.writes.clear()
        self.api.publish_status(chain, observed, FakeClock()())
        self.assertEqual([], self.transport.writes)

    def test_late_artifact_adoption_does_not_clear_an_unrelated_native_handoff(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        clock = FakeClock()
        operation = state.reserve(self.api.ledger, chain, json.dumps({
            "number": 8, "node": "NODE8", "head": "NODE8", "feedback": []}) + ":round:1", clock(), local=True)
        operation["sessionId"] = "SESSION-HANDOFF"
        state.finish(operation, "completed")
        chain["state"] = "human"  # a genuine, unrelated native handoff
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)  # an older saved task's artifact catches up late, already labeled
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        self.api.persist()
        self.api.adopt_child(chain)
        self.assertEqual("confirmed", chain["childAdoption"])
        self.assertEqual("human", chain["state"],
                          "a genuine native handoff must survive an unrelated late child-adoption confirmation")

    def test_recheck_confirms_child_once_the_adoption_label_is_actually_present(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []  # first read: write could not be confirmed yet
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        self.api.persist()
        self.api.adopt_child(chain)
        # Must actually reach the controller-generated honest-handoff stop
        # first, or a later "open" assertion would prove nothing about reopening.
        self.assertEqual("human", chain["state"])
        self.transport.values["repos/radical/aspire/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        self.api.adopt_child(chain)
        self.assertEqual("confirmed", chain["childAdoption"])
        self.assertEqual("open", chain["state"])

    def test_first_ambiguous_send_persists_uncertain_before_raising_and_restart_recovers_honest_handoff(self):
        # The first attempt at the real label POST is where a genuinely lost
        # response can occur; the write must be durably marked uncertain
        # before that failure surfaces, so a restarted sweep never re-sends
        # and still reaches the same honest "human" stop, not a silent retry.
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "reserved"
        self.api.persist()
        self.transport.label_write_confirms = False
        self.transport.writes.clear()
        with self.assertRaisesRegex(LostResponse, "child adoption result unknown"):
            self.api.adopt_child(chain)
        self.assertEqual("uncertain", chain["childAdoption"])
        label_writes = [write for write in self.transport.writes if write[1].endswith("/labels")]
        self.assertEqual([("POST", "repos/radical/aspire/issues/9/labels", {"labels": ["shepherd-adopted"]})],
                          label_writes)
        self.transport.writes.clear()
        self.api.sweep()
        self.assertEqual("human", chain["state"])
        self.assertEqual("uncertain", chain["childAdoption"])
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/labels")],
                          "a restarted sweep must not re-send an ambiguous label write")

    def test_ambiguous_adoption_human_state_survives_a_real_sweep_without_being_clobbered(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []  # a prior run's adoption write could not be confirmed
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        self.api.persist()
        self.api.sweep()
        self.assertEqual("human", chain["state"])
        self.assertEqual("uncertain", chain["childAdoption"])
        # The same generic unmanaged-child reclassification that real restarts
        # run on every sweep must not immediately stomp this back to
        # "hands-off" just because the child still reads unmanaged.
        self.api.sweep()
        self.assertEqual("human", chain["state"])
        self.assertEqual(9, chain["child"])

    def test_ambiguous_adoption_reopens_via_a_real_sweep_once_label_is_confirmed(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        self.api.persist()
        self.api.sweep()
        self.assertEqual("human", chain["state"])
        self.transport.values["repos/radical/aspire/pulls/9"]["labels"] = [{"name": "shepherd-adopted"}]
        self.api.sweep()
        self.assertEqual("open", chain["state"])
        self.assertEqual("confirmed", chain["childAdoption"])

    def test_pending_adoption_recheck_during_sweep_fetches_child_and_origin_exactly_once(self):
        # The recheck and the reclassification it feeds must share a single
        # fresh read of the child PR and origin issue; a second independent
        # fetch could read a different, later state than the first (a human
        # editing labels mid-sweep) and the two reads could then disagree
        # with each other about whether the adoption is still ambiguous.
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = []
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        self.api.persist()
        self.transport.reads.clear()
        self.api.sweep()
        self.assertEqual("human", chain["state"])
        child_pulls_reads = [read for read in self.transport.reads if read[1] == "repos/radical/aspire/pulls/9"]
        origin_issue_reads = [read for read in self.transport.reads
                               if read[1].split("?")[0] in {"repos/radical/aspire/issues/8"}]
        self.assertEqual(1, len(child_pulls_reads),
                          "recheck must reuse observe()'s read instead of an independent second fetch")
        self.assertEqual(1, len(origin_issue_reads),
                          "recheck must reuse observe()'s read instead of an independent second fetch")

    def test_pending_adoption_recheck_reflects_hands_off_from_the_same_fresh_read_not_a_stale_decision(self):
        # A stale decision (computed before the recheck's own fresh read) is
        # exactly the shape of bug the single-read fix prevents: this proves
        # the reclassification actually uses the SAME observation the
        # recheck consulted, not an earlier cached one.
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = [{"name": "shepherd-hands-off"}]  # genuinely hands-off by the time of this sweep
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "uncertain"
        chain["state"] = "human"
        self.api.persist()
        self.api.sweep()
        # A stale resolving_ambiguous_adoption computed before this fresh read
        # would have preserved "human"; the fresh read shows genuine hands-off
        # and must win.
        self.assertEqual("hands-off", chain["state"])

    def test_genuine_hands_off_label_still_suppresses_status_and_child_binding_unchanged(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        self.transport.values["repos/radical/aspire/issues/8"] = {
            "number": 8, "node_id": "NODE8", "state": "open", "labels": [{"name": "shepherd-adopted"}]}
        child = pr(9)
        child["labels"] = [{"name": "shepherd-hands-off"}]
        self.transport.values["repos/radical/aspire/pulls/9"] = child
        state.bind_child(self.api.ledger, chain, 9, child["node_id"])
        chain["childAdoption"] = "sent"
        self.api.persist()
        self.api.adopt_child(chain)
        self.assertEqual("hands-off", chain["state"])
        observed = self.api.observe(chain)
        self.transport.writes.clear()
        self.api.publish_status(chain, observed, FakeClock()())
        self.assertEqual([], self.transport.writes, "real hands-off must still suppress status writes")
        self.assertEqual("Adoption removed or hands-off label applied; no new repairs.",
                          self.api.next_action(chain, observed))

    def test_missing_pr_artifact_is_honest_human_handoff(self):
        chain, operation, task = self.issue_worker()
        task["artifacts"] = []
        self.api.reconcile_workers()
        self.assertIsNone(chain["child"])
        self.assertEqual("human", chain["state"])
        self.assertEqual(1, chain["rounds"])

    def test_cancelled_worker_with_no_artifact_becomes_due_again_preserving_history(self):
        chain, operation, task = self.issue_worker()
        task["state"] = "cancelled"
        task["sessions"][0]["state"] = "cancelled"
        task["artifacts"] = []
        self.api.reconcile_workers()
        self.assertIsNone(chain["child"])
        self.assertEqual("open", chain["state"])
        self.assertEqual(1, len(chain["operations"]))
        self.assertEqual("TASK1", operation["taskId"])
        self.assertEqual("failed", operation["state"])
        self.assertEqual("cancelled", operation["workerState"])
        observation = self.api.observe(chain)
        self.assertTrue(observation["actionable"])
        self.assertEqual("Bounded repair batch due.", self.api.next_action(chain, observation))

    def test_failed_worker_with_an_artifact_still_requires_human_not_retry(self):
        chain, operation, task = self.issue_worker()
        task["state"] = "failed"
        task["sessions"][0]["state"] = "failed"
        task["artifacts"] = [task["artifacts"][0]]  # pull artifact only, no matching branch
        self.api.reconcile_workers()
        self.assertIsNone(chain["child"])
        self.assertEqual("human", chain["state"])
        observation = self.api.observe(chain)
        self.assertEqual("Human handoff; no new repairs.", self.api.next_action(chain, observation))

    def test_wrong_session_or_branch_artifact_never_adopts_child(self):
        for change in ("session", "branch"):
            with self.subTest(change=change):
                self.setUp()
                chain, operation, task = self.issue_worker()
                if change == "session":
                    task["sessions"][0]["task_id"] = "OTHER"
                else:
                    self.transport.values["repos/radical/aspire/git/ref/heads/fix-9"]["object"]["sha"] = "b" * 40
                if change == "branch":
                    with self.assertRaises(ValueError):
                        self.api.reconcile_workers()
                else:
                    self.api.reconcile_workers()
                    self.assertEqual("unknown", operation["workerState"])
                    self.assertTrue(state.pending(chain))
                self.assertIsNone(chain["child"])

    def test_unknown_worker_billing_stays_reserved_after_terminal_and_rolling_window(self):
        chain, operation, task = self.issue_worker()
        task["sessions"][0]["usage"] = None
        self.api.reconcile_workers()
        clock = FakeClock()
        clock.advance(days=2)
        self.assertEqual(498, state.repository_spend(self.api.ledger, clock()))
        self.assertEqual(0, state.worker_slots(self.api.ledger))

    def test_removed_adoption_stops_new_item_writes_not_other_chain_observation(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"] = []
        observed = self.api.observe(chain)
        self.api.publish_status(chain, observed, FakeClock()())
        self.assertEqual([], self.transport.writes)

    def test_packet_clock_rollback_is_rejected_after_fresh_basis_reads(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        observed = self.api.observe(chain)
        self.api.persist()
        clock = FakeClock()
        self.api.clock = clock
        self.api.packet_time = clock()
        self.api.guard(chain, observed)
        clock.advance(seconds=-1)
        with self.assertRaisesRegex(ValueError, "clock"):
            self.api.guard(chain, observed)

    def test_issue_status_timestamp_is_not_source_revision_but_body_edits_are(self):
        self.api.read_authority()
        chain = state.adopt(self.api.ledger, 8, "issue", "NODE8")
        issue = {"number": 8, "node_id": "NODE8", "state": "open", "title": "Bug", "body": "Actual defect",
                 "labels": [{"name": "shepherd-adopted"}], "updated_at": "2026-10-04T00:00:00Z"}
        self.transport.values["repos/radical/aspire/issues/8"] = issue
        observed = self.api.observe(chain)
        self.api.persist()
        issue["updated_at"] = "2026-10-04T01:00:00Z"
        self.api.guard(chain, observed)
        issue["body"] = "A different defect"
        with self.assertRaisesRegex(ValueError, "basis"):
            self.api.guard(chain, observed)

    def test_empty_saved_ids_read_no_task_catalog_or_detail(self):
        self.api.read_authority()
        self.transport.reads.clear()
        self.api.reconcile_workers()
        self.assertEqual([], self.transport.reads)

    def test_foreign_catalog_is_never_requested_or_counted(self):
        self.api.read_authority()
        original = self.transport.__call__

        def no_catalog(method, endpoint, body):
            if "/tasks" in endpoint:
                self.fail("foreign catalog/detail request")
            return original(method, endpoint, body)

        self.api.api.transport = no_catalog
        self.api.reconcile_workers()
        self.assertEqual(0, self.api.admission_slots("fix-7"))

    def test_completed_receipt_always_refreshes_its_saved_task(self):
        chain, operation, task = self.issue_worker()
        task["updated_at"] = "2026-10-04T00:00:00Z"
        self.api.reconcile_workers()
        self.transport.reads.clear()
        self.api.reconcile_workers()
        self.assertEqual(["agents/repos/radical/aspire/tasks/TASK1"],
                         [endpoint for _, endpoint, _ in self.transport.reads if "/tasks" in endpoint])
        self.assertEqual("completed", operation["state"])

    def test_primary_optional_counts_validate_present_values_without_requiring_them(self):
        from live import API
        for count in (None, -1, True, "0", 1):
            with self.subTest(count=count), self.assertRaises(IncompleteInventory):
                API(lambda *_: Response({"tasks": [], "total_active_count": count}, {})).pages(
                    "agents/repos/radical/aspire/tasks", key="tasks", optional_total_count=True,
                    total_count_key="total_active_count")
        with self.assertRaises(IncompleteInventory):
            API(lambda *_: Response({"tasks": []}, {})).pages(
                "agents/repos/radical/aspire/tasks", key="tasks", require_total_count=True)

    def test_verified_repository_id_alias_collects_pages_using_pinned_named_requests(self):
        from live import API
        for repository, repository_id in (("radical/aspire", 746880239), ("microsoft/aspire", 696529789)):
            with self.subTest(repository=repository):
                path = f"repos/{repository}/commits/{'a' * 40}/check-runs"
                alias = f"repositories/{repository_id}/commits/{'a' * 40}/check-runs"
                calls = []

                def pages(_method, endpoint, _body):
                    calls.append(endpoint)
                    first = endpoint.endswith("&page=1")
                    return Response({"check_runs": [{"id": 1 if first else 2}], "total_count": 2},
                                    {"Link": f'<https://api.github.com/{alias}?per_page=100&page=2>; rel="next"'}
                                    if first else {})

                result = API(pages, repository_id=repository_id).pages(
                    path, key="check_runs", require_total_count=True)
                self.assertEqual([{"id": 1}, {"id": 2}], result)
                self.assertEqual([f"{path}?per_page=100&page=1", f"{path}?per_page=100&page=2"], calls)

    def test_repository_id_alias_requires_verified_identity_and_exact_page_basis(self):
        from live import API
        path = f"repos/microsoft/aspire/commits/{'a' * 40}/check-runs"
        alias = f"https://api.github.com/repositories/696529789/commits/{'a' * 40}/check-runs?per_page=100&page=2"
        for repository_id, url in (
                (None, alias),
                (746880239, alias),
                (696529789, alias.replace("api.github.com", "example.com")),
                (696529789, alias.replace("a" * 40, "b" * 40)),
                (696529789, alias.replace("/check-runs", "/status")),
                (696529789, alias.replace("page=2", "page=3")),
                (696529789, alias.replace("per_page=100", "per_page=99")),
                (696529789, alias + "&filter=all")):
            with self.subTest(repository_id=repository_id, url=url), self.assertRaises(IncompleteInventory):
                API(lambda *_: Response({"check_runs": [{"id": 1}], "total_count": 2},
                                        {"Link": f'<{url}>; rel="next"'}),
                    repository_id=repository_id).pages(path, key="check_runs", require_total_count=True)

    def test_primary_countless_link_pagination_completeness_and_fail_closed_boundaries(self):
        from live import API
        path = "agents/repos/radical/aspire/tasks"
        next_link = f'<https://api.github.com/{path}?is_archived=false&per_page=100&page=2>; rel="next"'
        calls = []

        def pages(_method, endpoint, _body):
            calls.append(endpoint)
            if endpoint.endswith("&page=1"):
                return Response({"tasks": [{"id": "one"}]}, {"Link": next_link})
            return Response({"tasks": [{"id": "two"}]}, {})

        result = API(pages).pages(path, key="tasks", query={"is_archived": "false"}, optional_total_count=True)
        self.assertEqual([{"id": "one"}, {"id": "two"}], result)
        self.assertEqual(2, len(calls))
        for headers, values in (
                ({}, [{"id": str(index)} for index in range(100)]),
                ({"Link": next_link + ", " + next_link}, []),
                ({"Link": next_link.replace('rel="next"', 'rel="last"')}, []),
                ({"Link": next_link.replace("is_archived=false", "is_archived=true")}, [])):
            with self.subTest(headers=headers), self.assertRaises(IncompleteInventory):
                API(lambda *_: Response({"tasks": values}, headers)).pages(
                    path, key="tasks", query={"is_archived": "false"}, optional_total_count=True)
        with self.assertRaises(IncompleteInventory):
            API(pages, max_pages=1).pages(
                path, key="tasks", query={"is_archived": "false"}, optional_total_count=True)

    def test_primary_optional_count_change_and_duplicate_pages_fail_closed(self):
        from live import API
        path = "agents/repos/radical/aspire/tasks"
        link = f'<https://api.github.com/{path}?per_page=100&page=2>; rel="next"'
        for changed_count in (True, False):
            def pages(_method, endpoint, _body):
                first = endpoint.endswith("&page=1")
                return Response({"tasks": [{"id": "one" if first or not changed_count else "two"}],
                                 "total_active_count": 2 if first else 3 if changed_count else 2},
                                {"Link": link} if first else {})
            with self.subTest(changed_count=changed_count), self.assertRaises(ValueError):
                API(pages).pages(path, key="tasks", optional_total_count=True, total_count_key="total_active_count")

    def test_primary_links_omitting_archive_filter_keep_each_requested_lane_pinned(self):
        from live import API
        path = "agents/repos/radical/aspire/tasks"
        for archived in ("false", "true"):
            calls = []

            def pages(_method, endpoint, _body):
                calls.append(endpoint)
                first = endpoint.endswith("&page=1")
                number = 1 if first else 2
                url = f"https://api.github.com/{path}?page={number}&per_page=100"
                first_url = f"https://api.github.com/{path}?page=1&per_page=100"
                link = f'<{first_url}>; rel="first", <{url}>; rel="last"'
                if first:
                    link = f'<https://api.github.com/{path}?page=2&per_page=100>; rel="next"'
                return Response({"tasks": [{"id": f"{archived}-{number}"}]}, {"Link": link})

            with self.subTest(archived=archived):
                result = API(pages).pages(path, key="tasks", query={"is_archived": archived},
                                         optional_total_count=True)
                self.assertEqual(2, len(result))
                self.assertEqual([
                    f"{path}?is_archived={archived}&per_page=100&page=1",
                    f"{path}?is_archived={archived}&per_page=100&page=2"], calls)

    def test_primary_first_last_self_link_proves_complete_single_page(self):
        from live import API
        path = "agents/repos/radical/aspire/tasks"
        url = f"https://api.github.com/{path}?page=1&per_page=100"
        for size in (0, 4, 100):
            tasks = [{"id": str(index)} for index in range(size)]
            with self.subTest(size=size):
                result = API(lambda *_: Response({"tasks": tasks}, {
                    "Link": f'<{url}>; rel="first", <{url}>; rel="last"'})).pages(
                        path, key="tasks", query={"is_archived": "false"}, optional_total_count=True)
                self.assertEqual(tasks, result)

    def test_resumed_settled_worker_holds_capacity_and_additional_usage(self):
        chain, operation, task = self.issue_worker()
        self.api.reconcile_workers()
        self.assertEqual(0, state.worker_slots(self.api.ledger))
        self.assertEqual(3.5, state.chain_spend(chain))
        task["state"] = "in_progress"
        task["sessions"].append({**task["sessions"][0], "id": "SESSION2", "state": "in_progress", "usage": None})
        task["session_count"] = 2
        self.api.reconcile_workers()
        self.assertEqual("waiting", operation["state"])
        self.assertTrue(state.pending(chain))
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        self.assertEqual(500, state.chain_spend(chain))
        with self.assertRaisesRegex(ValueError, "pending"):
            state.reserve(self.api.ledger, chain, "new", FakeClock()(), local=False)
        task["sessions"][1]["usage"] = {"type": "ai_credits", "amount": 2000000000}
        self.api.reconcile_workers()
        self.assertEqual(3.5, operation["workerActual"])
        self.assertEqual(500, state.chain_spend(chain))
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        task["state"] = "completed"
        task["sessions"][1]["state"] = "completed"
        self.api.reconcile_workers()
        self.assertEqual(5.5, state.chain_spend(chain))
        self.assertEqual(0, state.worker_slots(self.api.ledger))

    def test_unverifiable_resumed_session_never_refunds_known_spend_or_slot(self):
        chain, operation, task = self.issue_worker()
        self.api.reconcile_workers()
        task["state"] = "in_progress"
        task["sessions"].append({**task["sessions"][0], "id": "SESSION2", "state": "in_progress", "prompt": "Human follow-up"})
        task["session_count"] = 2
        self.api.reconcile_workers()
        self.assertEqual("unknown", operation["workerState"])
        self.assertEqual(1.5, operation["workerActual"])
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        self.assertTrue(state.pending(chain))

    def test_disappearing_previously_terminal_worker_holds_unknown_capacity(self):
        chain, operation, task = self.issue_worker()
        self.api.reconcile_workers()
        del self.transport.values["agents/repos/radical/aspire/tasks/TASK1"]
        self.api.reconcile_workers()
        self.assertEqual("unknown", operation["workerState"])
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        self.assertEqual(500, state.chain_spend(chain))

    def test_new_terminal_session_without_usage_retains_additional_credit_reservation(self):
        chain, operation, task = self.issue_worker()
        task["updated_at"] = "2026-10-04T00:00:00Z"
        self.api.reconcile_workers()
        task["sessions"].append({**task["sessions"][0], "id": "SESSION2", "usage": None})
        task["session_count"] = 2
        self.api.reconcile_workers()
        self.assertEqual(1.5, operation["workerActual"])
        self.assertEqual(500, state.chain_spend(chain))
        self.assertEqual(0, state.worker_slots(self.api.ledger))
        task["sessions"][1]["usage"] = {"type": "ai_credits", "amount": 2000000000}
        self.api.reconcile_workers()
        self.assertEqual(3.5, operation["workerActual"])
        self.assertEqual(5.5, state.chain_spend(chain))


if __name__ == "__main__":
    unittest.main()

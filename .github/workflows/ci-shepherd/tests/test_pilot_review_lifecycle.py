"""Public restarted review/repair sweeps against closed external wire fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import unittest

from helpers import FakeClock, reconciliation_evidence
from github import Response
from test_pilot_github import pr
from test_pilot_lifecycle import LifecycleTransport, PREFIX, RUN, TASKS, decision
import pilot
import pilot_github as github
import pilot_state as state


HUMAN = {"id": 1472, "login": "radical", "type": "User"}
REVIEWER = {"id": 175728472, "login": "Copilot", "type": "Bot"}
WORKER = {"id": 198982749, "login": "Copilot", "type": "Bot"}


class ReviewLifecycleTransport(LifecycleTransport):
    def __init__(self):
        super().__init__()
        self.review_boundaries = []

    def __call__(self, method, endpoint, body):
        if method == "POST" and endpoint.endswith("/requested_reviewers"):
            self.writes.append((method, endpoint, deepcopy(body)))
            self.review_boundaries.append(state.parse(self.comments[0]["body"]))
            value = self.values[endpoint.removesuffix("/requested_reviewers")]
            value["requested_reviewers"] = [deepcopy(REVIEWER)]
            return Response(deepcopy(value), {}, 201)
        return super().__call__(method, endpoint, body)


class ReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.transport = ReviewLifecycleTransport()
        self.transport.values[PREFIX + "/issues"] = [dict(pr(), pull_request={})]
        self.transport.values[PREFIX + "/pulls/7"] = pr()
        self.green("a" * 40)

    def fresh(self):
        api = github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)
        from helpers import result_capable
        result_capable(api)
        api.clock = self.clock
        return api

    def ledger(self):
        return state.parse(self.transport.comments[0]["body"])

    def chain(self):
        self.assertEqual(1, len(self.ledger()["chains"]))
        return self.ledger()["chains"][0]

    def sweep(self):
        api = self.fresh()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            packet = pilot.prepare(api, RUN, self.clock(), present=False)
        self.assertEqual(api.ledger, self.ledger())
        return packet

    def settle(self, packet, task_id):
        api = self.fresh()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, self.clock())
        self.assertEqual({"outcome": "waiting", "taskId": task_id}, result)
        self.assertEqual(api.ledger, self.ledger())

    def green(self, head):
        self.transport.values[PREFIX + "/commits/" + head + "/check-runs"] = {
            "total_count": 1, "check_runs": [{
                "id": 45 if head == "a" * 40 else 46, "head_sha": head, "status": "completed",
                "conclusion": "success", "name": "Tests", "html_url": "https://github.com/radical/aspire/pull/7"}]}

    def review(self, number, head, identity, *, user=REVIEWER, body="", outcome="COMMENTED"):
        self.clock.advance(minutes=1)
        value = self.transport.values[PREFIX + f"/pulls/{number}"]
        if user == REVIEWER:
            value["requested_reviewers"] = []
        review = {"id": identity, "user": deepcopy(user), "commit_id": head, "state": outcome, "body": body,
                  "submitted_at": self.clock().isoformat().replace("+00:00", "Z")}
        self.transport.values.setdefault(PREFIX + f"/pulls/{number}/reviews", []).append(review)
        return review

    def finish_task(self, identity, *, head=None, number=7):
        task = self.transport.values[TASKS + "/" + identity]
        task["state"] = task["sessions"][0]["state"] = "completed"
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 1500000000}
        if head is not None:
            self.transport.values[PREFIX + f"/pulls/{number}"]["head"]["sha"] = head
            self.green(head)
        return task

    def review_writes(self):
        return [write for write in self.transport.writes if write[1].endswith("/requested_reviewers")]

    def tasks(self):
        return [write for write in self.transport.writes if write[1] == TASKS]

    def repair_and_rereview(self, number, head, task_id):
        self.review(number, head, 60)
        self.transport.values[PREFIX + f"/pulls/{number}/comments"] = [{
            "id": 70, "node_id": "RC70", "user": REVIEWER, "body": "Handle empty input",
            "updated_at": "2026-10-04T00:02:00Z", "path": "src/parser.py", "line": 12,
            "side": "RIGHT", "commit_id": head}]
        packet = self.sweep()
        self.assertIsNotNone(packet)
        self.assertEqual((number, head), (packet["observation"]["number"], packet["observation"]["head"]))
        self.assertEqual(["Handle empty input"], [item["body"] for item in packet["observation"]["feedback"]])
        self.assertEqual(1, len(packet["observation"]["feedback"]))
        self.assertEqual("review-comment:70:2026-10-04T00:02:00Z",
                         packet["observation"]["feedback"][0]["id"])
        self.assertEqual("completed", packet["observation"]["copilotReview"]["state"])
        self.assertFalse(packet["observation"]["ready"])
        old_request = deepcopy(self.chain()["reviews"][0])
        self.assertEqual((60, None, 30), tuple(old_request[key] for key in ("reviewId", "actual", "reserved")))
        old_rounds = self.chain()["rounds"]
        self.settle(packet, task_id)
        self.assertIsNone(self.sweep())
        self.finish_task(task_id, head="b" * 40, number=number)
        self.transport.resolved_reviews = {70}
        self.assertIsNone(self.sweep())
        chain = self.chain()
        self.assertEqual(old_rounds, chain["rounds"])
        self.assertEqual([head, "b" * 40], [record["head"] for record in chain["reviews"]])
        self.assertEqual((None, 30, "waiting"),
                         tuple(chain["reviews"][1][key] for key in ("actual", "reserved", "state")))
        self.assertEqual(2, len(self.review_writes()))
        self.assertEqual([
            ("POST", PREFIX + f"/pulls/{number}/requested_reviewers",
             {"reviewers": ["copilot-pull-request-reviewer[bot]"]})] * 2, self.review_writes())
        self.assertEqual(old_request, chain["reviews"][0])
        boundary = self.transport.review_boundaries[-1]["chains"][0]
        self.assertEqual(old_rounds, boundary["rounds"])
        self.assertEqual(("sent", None, 30), tuple(boundary["reviews"][-1][key] for key in (
            "state", "actual", "reserved")))
        self.assertEqual(packet["operation"], chain["operations"][-1]["id"])
        self.assertEqual(2, chain["operations"][-1]["nativeActual"])
        self.assertEqual(1.5, chain["operations"][-1]["workerActual"])
        snapshot = deepcopy(chain)
        self.clock.advance(minutes=2)
        self.assertIsNone(self.sweep())
        self.assertEqual(snapshot, self.chain())
        return packet

    def test_current_head_objection_repairs_then_pushed_green_head_requests_rereview(self):
        self.assertIsNone(self.sweep())
        chain_id = self.chain()["id"]
        self.assertEqual((0, 30), (self.chain()["rounds"], state.chain_spend(self.chain())))
        packet = self.repair_and_rereview(7, "a" * 40, "TASK1")
        self.assertEqual(chain_id, packet["chain"])
        self.assertEqual((1, 63.5, 1), (self.chain()["rounds"], state.chain_spend(self.chain()), len(self.tasks())))
        self.review(7, "b" * 40, 62)
        self.assertIsNone(self.sweep())
        self.assertEqual((1, 63.5), (self.chain()["rounds"], state.chain_spend(self.chain())))
        self.assertEqual([60, 62], [record["reviewId"] for record in self.chain()["reviews"]])

    def test_verified_issue_child_uses_same_review_repair_chain_and_lifetime_cost(self):
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Parser bug", "body": "Empty input fails"}
        self.transport.values[PREFIX + "/issues"] = [issue]
        self.transport.values[PREFIX + "/issues/8"] = issue
        initial = self.sweep()
        self.settle(initial, "TASK1")
        child = pr(9)
        self.transport.values[PREFIX + "/pulls"] = [child]
        self.transport.values[PREFIX + "/pulls/9"] = child
        self.transport.values[PREFIX + "/issues"].append(dict(child, pull_request={}))
        self.transport.values[PREFIX + "/git/ref/heads/fix-9"] = {
            "ref": "refs/heads/fix-9", "object": {"sha": "a" * 40}}
        task = self.finish_task("TASK1")
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": 1009, "global_id": "NODE9"}},
            {"provider": "github", "type": "branch", "data": {"head_ref": "fix-9", "base_ref": "main"}}]
        self.assertIsNone(self.sweep())
        original_operation = deepcopy(self.chain()["operations"][0])
        self.assertEqual((initial["chain"], 8, 9, 1, 33.5),
                         (self.chain()["id"], self.chain()["origin"], self.chain()["child"], self.chain()["rounds"],
                          state.chain_spend(self.chain())))
        packet = self.repair_and_rereview(9, "a" * 40, "TASK2")
        self.assertEqual(initial["chain"], packet["chain"])
        self.assertEqual(original_operation, self.chain()["operations"][0])
        self.assertEqual((2, 67, 2), (self.chain()["rounds"], state.chain_spend(self.chain()), len(self.tasks())))
        self.assertTrue(self.tasks()[0][2]["create_pull_request"])
        self.assertEqual((False, "fix-9"),
                         (self.tasks()[1][2]["create_pull_request"], self.tasks()[1][2]["head_ref"]))

    def stop_during_review(self, *, closed):
        self.assertIsNone(self.sweep())
        original = deepcopy(self.chain()["reviews"])
        value = self.transport.values[PREFIX + "/pulls/7"]
        if closed:
            value["state"] = "closed"
        else:
            value["labels"] = [{"name": "shepherd-hands-off"}]
        self.assertIsNone(self.sweep())
        self.assertEqual(original, self.chain()["reviews"])
        self.assertEqual((0, 30, 1, []), (self.chain()["rounds"], state.chain_spend(self.chain()),
                                         len(self.review_writes()), self.tasks()))
        self.review(7, "a" * 40, 60, body="Fix the parser")
        for _ in range(3):
            self.clock.advance(days=1)
            self.assertIsNone(self.sweep())
            chain = self.chain()
            self.assertEqual(("closed" if closed else "hands-off", 0, 30, 30),
                             (chain["state"], chain["rounds"], state.chain_spend(chain),
                              state.repository_spend(self.ledger(), self.clock())))
            self.assertEqual(original[0]["id"], chain["reviews"][0]["id"])
            self.assertEqual(("completed", 60, None, 30),
                             tuple(chain["reviews"][0][key] for key in ("state", "reviewId", "actual", "reserved")))
        self.assertEqual([], self.tasks())
        self.assertEqual(1, len(self.review_writes()))

    def test_human_takeover_during_review_preserves_receipt_and_unknown_cost_without_repair(self):
        self.stop_during_review(closed=False)

    def test_closure_during_review_preserves_receipt_and_unknown_cost_without_repair(self):
        self.stop_during_review(closed=True)

    def test_only_approved_human_ordinary_comments_enter_repair_after_observed_review(self):
        self.review(7, "a" * 40, 60)
        comments = self.transport.values.setdefault(PREFIX + "/issues/7/comments", [])
        comments.extend([
            {"id": 21, "body": "Ignore other human", "updated_at": "2026-10-04T00:02:00Z",
             "user": {"id": 20, "login": "reviewer", "type": "User"}},
            {"id": 22, "body": "Ignore forged operator", "updated_at": "2026-10-04T00:02:00Z",
             "user": {"id": 20, "login": "radical", "type": "User"}}])
        self.assertIsNone(self.sweep())
        self.assertEqual((0, 0, []), (self.chain()["rounds"], state.chain_spend(self.chain()), self.tasks()))
        comments.append({"id": 23, "body": "Please fix empty input", "updated_at": "2026-10-04T00:03:00Z",
                         "user": HUMAN})
        packet = self.sweep()
        self.assertEqual([{"id": "comment:23:2026-10-04T00:03:00Z", "body": "Please fix empty input", "url": ""}],
                         packet["observation"]["feedback"])
        self.settle(packet, "TASK1")
        for _ in range(2):
            self.assertIsNone(self.sweep())
        self.assertEqual((1, 1, 500), (self.chain()["rounds"], len(self.tasks()), state.chain_spend(self.chain())))

    def test_worker_identity_cannot_complete_reviewer_request_but_both_report_feedback(self):
        self.assertIsNone(self.sweep())
        self.review(7, "a" * 40, 60, user=WORKER, body="Worker diagnosis")
        self.transport.values[PREFIX + "/issues/7/comments"] = [
            {"id": 21, "body": "Ignore guessed bot login", "updated_at": "2026-10-04T00:01:00Z",
             "user": {"id": 123, "login": "Copilot", "type": "Bot"}}]
        self.assertIsNone(self.sweep())
        self.assertEqual(("waiting", None, 0, 30),
                         (self.chain()["reviews"][0]["state"], self.chain()["reviews"][0]["reviewId"],
                          self.chain()["rounds"], state.chain_spend(self.chain())))
        self.review(7, "a" * 40, 61, body="Reviewer objection")
        packet = self.sweep()
        self.assertEqual(["Worker diagnosis", "Reviewer objection"],
                         [item["body"] for item in packet["observation"]["feedback"]])
        self.assertEqual(61, self.chain()["reviews"][0]["reviewId"])
        self.assertFalse(packet["observation"]["ready"])
        self.settle(packet, "TASK1")
        self.assertIsNone(self.sweep())
        self.assertEqual((1, 1, 500), (self.chain()["rounds"], len(self.tasks()), state.chain_spend(self.chain())))
        self.assertEqual(1, len(self.review_writes()))


if __name__ == "__main__":
    unittest.main()

from contextlib import redirect_stdout
from copy import deepcopy
import io
import unittest
from urllib.error import HTTPError

from github import IncompleteInventory, LostResponse, RejectedEffect, Response
from helpers import FakeClock, reconciliation_evidence
import pilot
import pilot_github as github
import pilot_state as state
from test_pilot import RUN
from test_pilot_github import pr
from test_pilot_lifecycle import LifecycleTransport, TASKS, decision


BOT = {"id": 175728472, "login": "Copilot", "type": "Bot"}


class ReviewTransport(LifecycleTransport):
    def __init__(self):
        super().__init__()
        self.boundaries = []
        self.on_boundary = None
        self.receipt = None

    def __call__(self, method, endpoint, body):
        if method == "POST" and endpoint.endswith("/requested_reviewers"):
            self.writes.append((method, endpoint, deepcopy(body)))
            self.boundaries.append(state.parse(self.comments[0]["body"]))
            value = self.values[endpoint.removesuffix("/requested_reviewers")]
            value["requested_reviewers"].append(BOT)
            if self.receipt is not None:
                return self.receipt(value)
            return Response(deepcopy(value), {}, 201)
        response = super().__call__(method, endpoint, body)
        if (self.on_boundary is not None and method == "PATCH" and endpoint.endswith("/comments/500")
                and state.parse(body["body"])["chains"][0].get("reviews", [{}])[-1].get("state") == "sent"):
            callback, self.on_boundary = self.on_boundary, None
            callback()
        return response


class ReviewTests(unittest.TestCase):
    def api(self):
        transport = ReviewTransport()
        transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        transport.values["repos/radical/aspire/commits/" + "a" * 40 + "/check-runs"] = {
            "total_count": 1, "check_runs": [
                {"id": 45, "head_sha": "a" * 40, "status": "completed", "conclusion": "success",
                 "name": "Tests", "html_url": "https://github.com/radical/aspire/pull/7"}]}
        return transport, FakeClock()

    def prepare(self, transport, clock):
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = clock
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, clock(), present=False)
        self.assertIsNone(packet)
        return api

    def test_green_pr_requests_once_and_fresh_sweeps_wait_without_native_rounds(self):
        transport, clock = self.api()
        api = self.prepare(transport, clock)
        requests = [write for write in transport.writes if write[1].endswith("/requested_reviewers")]
        self.assertEqual([("POST", "repos/radical/aspire/pulls/7/requested_reviewers",
                           {"reviewers": ["copilot-pull-request-reviewer[bot]"]})], requests)
        chain = api.ledger["chains"][0]
        self.assertEqual(0, chain["rounds"])
        self.assertEqual(30, state.chain_spend(chain))
        self.assertEqual("sent", transport.boundaries[0]["chains"][0]["reviews"][0]["state"])
        before = deepcopy(chain)
        for _ in range(2):
            api = self.prepare(transport, clock)
            self.assertEqual(before, api.ledger["chains"][0])
            self.assertIn("Waiting for Copilot review", api.next_action(
                api.ledger["chains"][0], api.observe(api.ledger["chains"][0])))
        self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/requested_reviewers")]))

    def test_published_review_completes_request_but_does_not_refund_unknown_usage_or_count_as_human_approval(self):
        transport, clock = self.api()
        self.prepare(transport, clock)
        transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"] = []
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [
            {"id": 60, "user": BOT, "commit_id": "a" * 40, "state": "APPROVED", "body": "",
             "submitted_at": "2026-10-04T00:01:00Z"}]
        clock.advance(minutes=1)
        api = self.prepare(transport, clock)
        chain = api.ledger["chains"][0]
        self.assertEqual(("completed", 60, 30, None), tuple(chain["reviews"][0][key]
                         for key in ("state", "reviewId", "reserved", "actual")))
        self.assertFalse(api.observe(chain)["ready"])
        clock.advance(days=2)
        api = self.prepare(transport, clock)
        self.assertEqual((30, 30), (state.chain_spend(api.ledger["chains"][0]),
                                    state.repository_spend(api.ledger, clock())))

    def test_external_review_request_between_guards_prevents_duplicate_post(self):
        transport, clock = self.api()
        transport.on_boundary = lambda: transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"].append(BOT)
        api = self.prepare(transport, clock)
        self.assertEqual([], transport.boundaries)
        record = api.ledger["chains"][0]["reviews"][0]
        self.assertEqual(("no-send", 0, 0), tuple(record[key] for key in ("state", "actual", "reserved")))
        self.prepare(transport, clock)
        self.assertEqual([], transport.boundaries)

    def test_documented_review_http_rejection_is_distinct_from_uncertain_failure_without_retry(self):
        class Reject:
            def __init__(self, code):
                self.code, self.calls = code, 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, self.code, "fixture", {}, None)

        for code in (403, 422, 429, 503):
            with self.subTest(code=code):
                transport = github.PilotTransport("test", write=True)
                opener = Reject(code)
                transport.opener = opener
                expected = RejectedEffect if code in {403, 422} else LostResponse
                with self.assertRaises(expected):
                    transport("POST", "repos/radical/aspire/pulls/7/requested_reviewers",
                              {"reviewers": ["copilot-pull-request-reviewer[bot]"]})
                self.assertEqual(1, opener.calls)

    def test_crash_after_saved_send_boundary_before_post_never_retries_or_refunds(self):
        transport, clock = self.api()

        def crash():
            raise SystemExit("process stopped before POST")

        transport.on_boundary = crash
        with self.assertRaises(SystemExit):
            self.prepare(transport, clock)
        body = transport.comments[0]["body"]
        for _ in range(2):
            clock.advance(days=1)
            api = self.prepare(transport, clock)
            chain = api.ledger["chains"][0]
            self.assertEqual(body, transport.comments[0]["body"])
            self.assertEqual(("sent", None, 30), tuple(chain["reviews"][0][key]
                             for key in ("state", "actual", "reserved")))
            self.assertEqual((0, 30, 30, []), (chain["rounds"], state.chain_spend(chain),
                                             state.repository_spend(api.ledger, clock()), transport.boundaries))
            self.assertEqual("uncertain", api.observe(chain)["copilotReview"]["state"])

    def test_malformed_success_receipt_remains_uncertain_without_duplicate_or_zero_usage(self):
        transport, clock = self.api()
        transport.receipt = lambda value: Response({**deepcopy(value), "head": None}, {}, 201)
        api = self.prepare(transport, clock)
        chain = api.ledger["chains"][0]
        self.assertEqual(("uncertain", None, 30), tuple(chain["reviews"][0][key]
                         for key in ("state", "actual", "reserved")))
        before = deepcopy(chain)
        for _ in range(2):
            self.assertEqual(before, self.prepare(transport, clock).ledger["chains"][0])
        self.assertEqual(1, len(transport.boundaries))

    def test_existing_unsubmitted_copilot_review_waits_without_starting_another_request(self):
        transport, clock = self.api()
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [
            {"id": 60, "user": BOT, "commit_id": "a" * 40, "state": "PENDING", "body": "",
             "submitted_at": None}]
        api = self.prepare(transport, clock)
        self.assertEqual([], transport.boundaries)
        self.assertEqual((0, 0), (api.ledger["chains"][0]["rounds"], state.chain_spend(api.ledger["chains"][0])))
        self.assertEqual("waiting", api.observe(api.ledger["chains"][0])["copilotReview"]["state"])

    def test_ten_unknown_review_reservations_do_not_consume_native_rounds_or_all_repair_headroom(self):
        transport, clock = self.api()
        for index in range(10):
            head = format(index + 1, "040x")
            transport.values["repos/radical/aspire/pulls/7"]["head"]["sha"] = head
            transport.values["repos/radical/aspire/commits/" + head + "/check-runs"] = {
                "total_count": 1, "check_runs": [
                    {"id": index + 45, "head_sha": head, "status": "completed", "conclusion": "success",
                     "name": "Tests", "html_url": "https://github.com/radical/aspire/pull/7"}]}
            self.prepare(transport, clock)
            clock.advance(minutes=1)
            transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"] = []
            transport.values.setdefault("repos/radical/aspire/pulls/7/reviews", []).append(
                {"id": index + 60, "user": BOT, "commit_id": head, "state": "COMMENTED", "body": "",
                 "submitted_at": clock().isoformat().replace("+00:00", "Z")})
            api = self.prepare(transport, clock)
        chain = api.ledger["chains"][0]
        self.assertEqual((10, 0, 300), (len(transport.boundaries), chain["rounds"], state.chain_spend(chain)))
        transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": 20, "body": "Please fix the empty input", "user": {"id": 1472, "login": "radical"},
             "updated_at": clock().isoformat().replace("+00:00", "Z")}]
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, clock(), present=False)
        self.assertIsNotNone(packet)
        chain = api.ledger["chains"][0]
        self.assertEqual((1, 330, 10), (chain["rounds"], state.chain_spend(chain), len(transport.boundaries)))

    def test_saved_worker_resuming_after_review_send_boundary_prevents_post_and_retains_full_worker_hold(self):
        transport, clock = self.api()
        transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": 20, "body": "Repair empty input", "user": {"id": 1472, "login": "radical"},
             "updated_at": "2026-10-04T00:00:00Z"}]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = clock
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, clock(), present=False)
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, clock())
        self.assertEqual("TASK1", result["taskId"])
        task = transport.values[TASKS + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = "completed"
        task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": 1500000000}
        transport.values["repos/radical/aspire/issues/7/comments"] = []

        def resume():
            task["state"] = task["sessions"][0]["state"] = "queued"
            task["updated_at"] = "2026-10-04T00:01:00Z"

        transport.on_boundary = resume
        api = self.prepare(transport, clock)
        chain = api.ledger["chains"][0]
        self.assertEqual([], transport.boundaries)
        self.assertEqual(("no-send", 0, 0), tuple(chain["reviews"][0][key] for key in ("state", "actual", "reserved")))
        self.assertEqual(("TASK1", "queued", 1, 500), (chain["operations"][0]["taskId"],
                         chain["operations"][0]["workerState"], chain["rounds"], state.chain_spend(chain)))

    def test_one_review_request_per_sweep_does_not_starve_the_next_due_pr(self):
        transport, clock = self.api()
        transport.values["repos/radical/aspire/issues"].append(dict(pr(9), pull_request={}))
        transport.values["repos/radical/aspire/pulls/9"] = pr(9)
        api = self.prepare(transport, clock)
        self.assertEqual(1, len(transport.boundaries))
        self.assertEqual([7], [chain["origin"] for chain in api.ledger["chains"] if chain.get("reviews")])
        api = self.prepare(transport, clock)
        self.assertEqual(2, len(transport.boundaries))
        self.assertEqual([7, 9], [chain["origin"] for chain in api.ledger["chains"] if chain.get("reviews")])
        self.assertEqual(60, state.repository_spend(api.ledger, clock()))

    def test_unavailable_review_inventory_pauses_only_affected_chain_and_keeps_other_review_due(self):
        class MissingReviews(ReviewTransport):
            def __call__(self, method, endpoint, body):
                if method == "GET" and endpoint.startswith("repos/radical/aspire/pulls/7/reviews"):
                    raise IncompleteInventory("review inventory unavailable")
                return super().__call__(method, endpoint, body)

        values, clock = self.api()
        transport = MissingReviews()
        transport.values.update(values.values)
        transport.values["repos/radical/aspire/issues/99/comments"] = transport.comments
        transport.values["repos/radical/aspire/issues"].append(dict(pr(9), pull_request={}))
        transport.values["repos/radical/aspire/pulls/9"] = pr(9)
        api = self.prepare(transport, clock)
        chain = api.ledger["chains"][0]
        observed = api.observe(chain)
        self.assertEqual((False, False, "unavailable"),
                         (observed["ready"], observed["actionable"], observed["copilotReview"]["state"]))
        self.assertIn("review inventory", observed["attention"].lower())
        self.assertNotIn("reviews", chain)
        self.assertEqual([9], [item["origin"] for item in api.ledger["chains"] if item.get("reviews")])

    def test_copilot_review_rerequest_after_native_preparation_invalidates_worker_dispatch(self):
        transport, clock = self.api()
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [
            {"id": 60, "user": BOT, "state": "COMMENTED", "commit_id": "a" * 40, "body": "",
             "submitted_at": "2026-10-04T00:00:00Z"}]
        transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": 20, "body": "Repair empty input", "user": {"id": 1472, "login": "radical"},
             "updated_at": "2026-10-04T00:00:00Z"}]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = clock
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, clock(), present=False)
            transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"] = [BOT]
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), 2, clock())
        self.assertEqual("failed", result["outcome"])
        self.assertEqual([], [write for write in transport.writes if write[1] == TASKS])
        self.assertEqual((1, 2, 0), (api.ledger["chains"][0]["rounds"],
                         api.ledger["chains"][0]["operations"][0]["nativeActual"],
                         api.ledger["chains"][0]["operations"][0]["workerReserved"]))

    def test_accepted_review_disappearing_without_a_result_requires_confirmation_not_retry(self):
        transport, clock = self.api()
        self.prepare(transport, clock)
        transport.values["repos/radical/aspire/pulls/7"]["requested_reviewers"] = []
        for _ in range(2):
            api = self.prepare(transport, clock)
            chain = api.ledger["chains"][0]
            self.assertEqual("uncertain", api.observe(chain)["copilotReview"]["state"])
            self.assertEqual(("waiting", None, 30), tuple(chain["reviews"][0][key]
                             for key in ("state", "actual", "reserved")))
        self.assertEqual(1, len(transport.boundaries))

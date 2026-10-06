from contextlib import redirect_stdout
from copy import deepcopy
import io
import unittest
from urllib.parse import parse_qs, urlparse

from github import Response
from helpers import reconciliation_evidence
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_feedback as feedback
import pilot_state as state
import test_pilot_tracked_only as fixtures


class ReviewFeedbackTests(unittest.TestCase):
    def api(self, binding=bindings.FORK):
        fixture = fixtures.TrackedOnlyTests()
        api, transport = fixture.api(binding)
        number = binding.subject or 7
        transport.values[f"{api.prefix}/issues/{number}/comments"] = []
        transport.values[f"{api.prefix}/pulls/{number}/comments"] = [{
            "id": 31, "node_id": "COMMENT31", "body": "Fix normalization",
            "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 20},
            "path": "normalization.py", "line": 12}]
        resolved = set()
        original = api.transport

        def read(method, endpoint, body):
            if method == "POST" and endpoint == "graphql" and "ids" in body["variables"]:
                transport.reads.append((method, endpoint, deepcopy(body)))
                value = transport.values[f"{api.prefix}/pulls/{number}"]
                comments = {comment["node_id"]: comment
                            for comment in transport.values[f"{api.prefix}/pulls/{number}/comments"]}
                return Response({"data": {
                    "repository": {"databaseId": api.repository_id, "nameWithOwner": api.repository,
                                   "pullRequest": {"id": value["node_id"], "number": number,
                                                   "headRefOid": value["head"]["sha"]}},
                    "nodes": [{"id": identity, "fullDatabaseId": str(comments[identity]["id"]),
                               "thread": {"id": "THREAD" + identity,
                                          "isResolved": comments[identity]["id"] in resolved,
                                          "pullRequest": {"id": value["node_id"]}}}
                              for identity in body["variables"]["ids"]]}}, {})
            return original(method, endpoint, body)

        api.transport = api.api.transport = read
        return fixture, api, transport, resolved

    def test_resolved_feedback_after_worker_completion_does_not_reserve_another_round(self):
        fixture, api, transport, resolved = self.api()
        chain, _, _ = fixture.seed_worker(api, transport, completed=True)
        before = deepcopy(api.ledger)
        resolved.add(31)
        for _ in range(2):
            fresh = fixture.fresh(api)
            with redirect_stdout(io.StringIO()):
                packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
            self.assertIsNone(packet)
            self.assertEqual(before, fresh.ledger)
            self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])
            api = fresh
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])
        self.assertEqual("open", chain["state"])

    def test_reopened_thread_and_new_identical_comment_each_become_fresh_work(self):
        for change in ("reopened", "new-comment"):
            with self.subTest(change=change):
                fixture, api, transport, resolved = self.api()
                fixture.seed_worker(api, transport, completed=True)
                before = deepcopy(api.ledger)
                resolved.add(31)
                with redirect_stdout(io.StringIO()):
                    self.assertIsNone(pilot.prepare(fixture.fresh(api), fixtures.RUN, api.clock(), present=False))
                if change == "reopened":
                    resolved.remove(31)
                    expected = 31
                else:
                    comments = transport.values[f"{api.prefix}/pulls/7/comments"]
                    comments.append({**comments[0], "id": 32, "node_id": "COMMENT32"})
                    expected = 32
                fresh = fixture.fresh(api)
                with redirect_stdout(io.StringIO()):
                    packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
                self.assertEqual([f"review-comment:{expected}:2026-10-04T00:00:00Z"],
                                 [item["id"] for item in packet["observation"]["feedback"]])
                self.assertEqual({}, fresh.ledger["chains"][0]["dispositions"])
                self.assertEqual(before["chains"][0]["operations"], fresh.ledger["chains"][0]["operations"][:-1])
                self.assertEqual(2, fresh.ledger["chains"][0]["rounds"])

    def test_unresolved_feedback_survives_terminal_worker_outcomes_in_both_profiles(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            for outcome in ("completed", "failed", "cancelled", "timed_out"):
                with self.subTest(binding=binding.name, outcome=outcome):
                    fixture, api, transport, _ = self.api(binding)
                    _, _, task = fixture.seed_worker(api, transport, completed=True)
                    task["state"] = task["sessions"][0]["state"] = outcome
                    if outcome != "completed":
                        task["sessions"][0]["error"] = {"message": "Could not repair normalization"}
                    fresh = fixture.fresh(api)
                    with redirect_stdout(io.StringIO()):
                        packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
                    self.assertEqual(["review-comment:31:2026-10-04T00:00:00Z"],
                                     [item["id"] for item in packet["observation"]["feedback"]])
                    receipt = packet["observation"]["workerResults"][0]
                    self.assertEqual(outcome, receipt["state"])
                    self.assertFalse(receipt["narrativeAvailable"])
                    if outcome != "completed":
                        self.assertEqual("Could not repair normalization", receipt["errors"][0]["message"])

    def test_resolution_changed_after_prepare_prevents_worker_send_but_keeps_native_billing(self):
        fixture, api, transport, resolved = self.api()
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, fixtures.RUN, api.clock(), present=False)
        resolved.add(31)
        fresh = fixture.fresh(api)
        result = pilot.settle(fresh, packet, reconciliation_evidence(fixtures.decision(packet)), 2, api.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertIn("basis changed", result["error"])
        operation = fresh.ledger["chains"][0]["operations"][0]
        self.assertEqual((2, 0, 0, None), (
            operation["nativeActual"], operation["nativeReserved"], operation["workerReserved"], operation["taskId"]))
        self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_unreadable_thread_pauses_only_that_chain_without_reserving_or_guessing_resolution(self):
        fixture, api, transport, _ = self.api()
        fixture.seed_worker(api, transport, completed=True)
        original = api.transport

        def unavailable(method, endpoint, body):
            if method == "POST" and endpoint == "graphql" and "ids" in body["variables"]:
                return Response({"errors": [{"message": "PRIVATE_SOURCE_SENTINEL"}]}, {})
            return original(method, endpoint, body)

        before = deepcopy(api.ledger)
        fresh = fixture.fresh(api, unavailable)
        log = io.StringIO()
        with redirect_stdout(log):
            self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
        self.assertEqual(before, fresh.ledger)
        self.assertIn("Review-thread resolution unavailable/incomplete", log.getvalue())
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", log.getvalue())
        transport.values[f"{api.prefix}/pulls/9"] = fixtures.pr(9)
        transport.values[f"{api.prefix}/issues/9/comments"] = [{
            "id": 40, "body": "Fix independent work", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 20}}]
        transport.values[f"{api.prefix}/issues"].append(dict(fixtures.pr(9), pull_request={}))
        fresh = fixture.fresh(api, unavailable)
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
        self.assertEqual(9, packet["observation"]["number"])
        self.assertEqual(before["chains"][0], fresh.ledger["chains"][0])

    def test_unreadable_review_comment_inventory_pauses_only_the_chain(self):
        fixture, api, transport, _ = self.api()
        fixture.seed_worker(api, transport, completed=True)
        before = deepcopy(api.ledger)
        original = api.transport

        def unavailable(method, endpoint, body):
            if method == "GET" and endpoint.startswith(f"{api.prefix}/pulls/7/comments?"):
                return Response({"message": "PRIVATE_SOURCE_SENTINEL"}, {}, 403)
            return original(method, endpoint, body)

        fresh = fixture.fresh(api, unavailable)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
        self.assertEqual(before, fresh.ledger)
        self.assertIn("Review-thread resolution unavailable/incomplete",
                      fresh.observe(fresh.ledger["chains"][0])["attention"])

    def test_issue_child_uses_the_same_resolution_filter_without_replacing_its_chain(self):
        from test_pilot_github import PilotGitHubTests
        fixture = PilotGitHubTests()
        fixture.setUp()
        api, transport = fixture.api, fixture.transport
        chain, _, _ = fixture.issue_worker()
        api.clock = fixtures.FakeClock()
        api.reconcile_workers()
        api.adopt_child(chain)
        transport.values[f"{api.prefix}/issues/9/comments"] = []
        transport.values[f"{api.prefix}/pulls/9/comments"] = [{
            "id": 31, "body": "Fix normalization", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 20}}]
        api.persist()
        transport.resolved_reviews.add(31)
        before = deepcopy(api.ledger)
        fresh = fixtures.TrackedOnlyTests().fresh(api)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
        self.assertEqual(before, fresh.ledger)
        transport.resolved_reviews.remove(31)
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
        self.assertEqual(chain["id"], packet["chain"])
        self.assertEqual((9, "pr"), (packet["observation"]["number"], packet["observation"]["kind"]))
        self.assertEqual(2, fresh.ledger["chains"][0]["rounds"])

    def test_missing_foreign_partial_or_stale_resolution_evidence_never_authorizes_work(self):
        cases = ("http", "graphql", "repository", "repository-name", "pr", "number", "head",
                 "null-node", "missing-node", "duplicate-node", "foreign-node", "database-id",
                 "database-id-type", "null-thread", "thread-id", "thread-pr", "unknown-state",
                 "missing-rest-node", "duplicate-rest-node")
        for change in cases:
            with self.subTest(change=change):
                fixture, api, transport, _ = self.api()
                fixture.seed_worker(api, transport, completed=True)
                before = deepcopy(api.ledger)
                original = api.transport

                def malformed(method, endpoint, body):
                    response = original(method, endpoint, body)
                    if method != "POST" or endpoint != "graphql" or "ids" not in body["variables"]:
                        return response
                    payload = deepcopy(response.payload)
                    root = payload["data"]["repository"]
                    nodes = payload["data"]["nodes"]
                    if change == "http":
                        return Response(payload, {}, 403)
                    if change == "graphql":
                        payload["errors"] = [{"message": "Incomplete field"}]
                    elif change == "repository":
                        root["databaseId"] = 1
                    elif change == "repository-name":
                        root["nameWithOwner"] = "microsoft/aspire"
                    elif change == "pr":
                        root["pullRequest"]["id"] = "FOREIGN"
                    elif change == "number":
                        root["pullRequest"]["number"] = 9
                    elif change == "head":
                        root["pullRequest"]["headRefOid"] = "b" * 40
                    elif change == "null-node":
                        nodes[0] = None
                    elif change == "missing-node":
                        nodes.clear()
                    elif change == "duplicate-node":
                        nodes.append(deepcopy(nodes[0]))
                    elif change == "foreign-node":
                        nodes[0]["id"] = "FOREIGN"
                    elif change == "database-id":
                        nodes[0]["fullDatabaseId"] = "32"
                    elif change == "database-id-type":
                        nodes[0]["fullDatabaseId"] = True
                    elif change == "null-thread":
                        nodes[0]["thread"] = None
                    elif change == "thread-id":
                        nodes[0]["thread"]["id"] = ""
                    elif change == "thread-pr":
                        nodes[0]["thread"]["pullRequest"]["id"] = "FOREIGN"
                    elif change == "unknown-state":
                        nodes[0]["thread"]["isResolved"] = None
                    return Response(payload, {})

                comments = transport.values[f"{api.prefix}/pulls/7/comments"]
                if change == "missing-rest-node":
                    comments[0]["node_id"] = None
                elif change == "duplicate-rest-node":
                    comments.append({**comments[0], "id": 32})
                fresh = fixture.fresh(api, malformed)
                with redirect_stdout(io.StringIO()):
                    self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
                self.assertEqual(before, fresh.ledger)
                observed = fresh.observe(fresh.ledger["chains"][0])
                self.assertFalse(observed["actionable"])
                self.assertIn("Review-thread resolution unavailable/incomplete", observed["attention"])
                self.assertEqual([], observed["feedback"])
                self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_paginated_comments_use_bounded_complete_batches_and_filter_before_the_feedback_limit(self):
        fixture, api, transport, resolved = self.api()
        comment = transport.values[f"{api.prefix}/pulls/7/comments"][0]
        comments = [{**comment, "id": number, "node_id": f"COMMENT{number}"} for number in range(1, 102)]
        transport.values[f"{api.prefix}/pulls/7/comments"] = comments
        resolved.update(range(1, 101))
        original = api.transport

        def paginated(method, endpoint, body):
            if method == "GET" and endpoint.startswith(f"{api.prefix}/pulls/7/comments?"):
                page = int(parse_qs(urlparse(endpoint).query)["page"][0])
                link = {"Link": f'<https://api.github.com/{api.prefix}/pulls/7/comments?per_page=100&page=2>; rel="next"'}
                return Response(comments[(page - 1) * 100:page * 100], link if page == 1 else {})
            return original(method, endpoint, body)

        fresh = fixture.fresh(api, paginated)
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False)
        self.assertEqual(["review-comment:101:2026-10-04T00:00:00Z"],
                         [item["id"] for item in packet["observation"]["feedback"]])
        batches = [body["variables"]["ids"] for method, endpoint, body in transport.reads
                   if method == "POST" and endpoint == "graphql" and "ids" in body["variables"]]
        self.assertEqual({100, 1}, {len(batch) for batch in batches})
        self.assertEqual({comment["node_id"] for comment in comments}, {identity for batch in batches for identity in batch})
        self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])

    def test_sealed_resolution_query_is_read_only_and_keeps_closed_subject_bindings(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                transport = github.PilotTransport("fixture-token", binding=binding)
                request = feedback.body(binding.repository, binding.subject or 7, ["COMMENT31"])
                transport.validate_endpoint("POST", "graphql", request)
                self.assertTrue(transport.is_read("POST", "graphql", request))
                changes = []
                for field, value in (("owner", "other"), ("name", "other"), ("number", 121),
                                     ("number", True), ("ids", []), ("ids", ["COMMENT31"] * 2),
                                     ("ids", [f"COMMENT{index}" for index in range(101)])):
                    changed = deepcopy(request)
                    changed["variables"][field] = value
                    changes.append(changed)
                changed = deepcopy(request)
                changed["query"] = "mutation { addComment }"
                changes.append(changed)
                changed = deepcopy(request)
                changed["variables"]["extra"] = "foreign input"
                changes.append(changed)
                if binding.subject is not None:
                    changed = deepcopy(request)
                    changed["variables"]["number"] = 7
                    changes.append(changed)
                for changed in changes:
                    with self.assertRaises(ValueError):
                        transport.validate_endpoint("POST", "graphql", changed)

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
    def test_portable_schema_resolves_legacy_feedback_without_another_paid_round(self):
        fixture, api, transport, _ = self.api(bindings.UPSTREAM)
        chain, _, _ = fixture.seed_worker(api, transport, completed=True)
        before = deepcopy(api.ledger)
        writes_before = deepcopy(transport.writes)
        original = api.transport
        value = transport.values[f"{api.prefix}/pulls/20722"]

        def portable_schema(method, endpoint, body):
            if method != "POST" or endpoint != "graphql":
                return original(method, endpoint, body)
            if "ids" in body["variables"]:
                return Response({"errors": [{
                    "extensions": {"code": "undefinedField"},
                    "message": "Field 'thread' doesn't exist on type 'PullRequestReviewComment'"}]}, {})
            if "after" in body["variables"]:
                return Response({"data": {"repository": {
                    "databaseId": api.repository_id, "nameWithOwner": api.repository,
                    "pullRequest": {
                        "id": value["node_id"], "number": 20722, "headRefOid": value["head"]["sha"],
                        "reviewThreads": {"nodes": [{
                            "id": "THREAD31", "isResolved": True,
                            "pullRequest": {"id": value["node_id"]},
                            "comments": {"nodes": [{"id": "COMMENT31", "fullDatabaseId": "31"}],
                                         "pageInfo": {"hasNextPage": False, "endCursor": "COMMENT-END"}}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": "THREAD-END"}}}}}}, {})
            return original(method, endpoint, body)

        fresh = fixture.fresh(api, portable_schema)
        observed = fresh.observe(chain)
        self.assertIsNone(observed["attention"])
        self.assertEqual([], observed["feedback"])
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
        self.assertEqual(before, fresh.ledger)
        self.assertEqual(writes_before, transport.writes)

    def api(self, binding=bindings.FORK):
        fixture = fixtures.TrackedOnlyTests()
        api, transport = fixture.api(binding)
        number = binding.subject or 7
        transport.values[f"{api.prefix}/issues/{number}/comments"] = []
        transport.values[f"{api.prefix}/pulls/{number}/comments"] = [{
            "id": 31, "node_id": "COMMENT31", "body": "Fix normalization",
            "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"},
            "path": "normalization.py", "line": 12}]
        return fixture, api, transport, transport.resolved_reviews

    def test_nested_pages_match_rest_comments_and_reject_incomplete_or_changed_evidence(self):
        for change in ("complete", "head", "thread", "membership", "resolution", "missing", "duplicate",
                       "repeated comment cursor", "repeated thread cursor"):
            with self.subTest(change=change):
                fixture, api, transport, _ = self.api()
                fixture.seed_worker(api, transport, completed=True)
                writes_before = deepcopy(transport.writes)
                comments = transport.values[f"{api.prefix}/pulls/7/comments"]
                comments += [{**comments[0], "id": number, "node_id": f"COMMENT{number}"} for number in (32, 33)]
                value = transport.values[f"{api.prefix}/pulls/7"]
                original, requests = api.transport, []

                def read(method, endpoint, body):
                    if endpoint != "graphql" or body["query"] not in (feedback.QUERY, feedback.COMMENTS_QUERY):
                        return original(method, endpoint, body)
                    requests.append(deepcopy(body["variables"]))
                    root = {"databaseId": api.repository_id, "nameWithOwner": api.repository,
                            "pullRequest": {"id": value["node_id"], "number": 7, "headRefOid": value["head"]["sha"]}}
                    continuation = body["query"] == feedback.COMMENTS_QUERY
                    second_thread = not continuation and body["variables"]["after"] is not None
                    number = 32 if continuation else 33 if second_thread else 31
                    thread = {"id": "THREAD33" if second_thread else "THREAD31", "isResolved": not second_thread,
                              "pullRequest": {"id": value["node_id"]}, "comments": {
                                  "nodes": [{"id": f"COMMENT{number}", "fullDatabaseId": str(number)}],
                                  "pageInfo": {"hasNextPage": not continuation and not second_thread,
                                               "endCursor": "comment-next" if not continuation and not second_thread
                                               else "comment-end"}}}
                    if continuation:
                        if change == "head":
                            root["pullRequest"]["headRefOid"] = "b" * 40
                        if change == "thread":
                            thread["id"] = "FOREIGN"
                        if change == "membership":
                            thread["pullRequest"]["id"] = "FOREIGN"
                        if change == "resolution":
                            thread["isResolved"] = False
                        if change == "missing":
                            thread["comments"] = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
                        if change == "duplicate":
                            thread["comments"]["nodes"][0] = {"id": "COMMENT31", "fullDatabaseId": "31"}
                        if change == "repeated comment cursor":
                            thread["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": "comment-next"}
                        return Response({"data": {"repository": root, "node": thread}}, {})
                    cursor = "thread-next" if not second_thread or change == "repeated thread cursor" else "thread-end"
                    root["pullRequest"]["reviewThreads"] = {
                        "nodes": [thread], "pageInfo": {"hasNextPage": not second_thread, "endCursor": cursor}}
                    return Response({"data": {"repository": root}}, {})

                api.transport = api.api.transport = read
                fresh = fixture.fresh(api)
                fresh.read_authority()
                chain = fresh.ledger["chains"][0]
                before = deepcopy(fresh.ledger)
                observed = fresh.observe(chain)
                if change == "complete":
                    self.assertIsNone(observed["attention"])
                    self.assertEqual(["review-comment:33:2026-10-04T00:00:00Z"],
                                     [item["id"] for item in observed["feedback"]])
                    self.assertEqual([{"owner": "radical", "name": "aspire", "number": 7, "after": None},
                                      {"owner": "radical", "name": "aspire", "number": 7,
                                       "after": "comment-next", "thread": "THREAD31"},
                                      {"owner": "radical", "name": "aspire", "number": 7, "after": "thread-next"}],
                                     requests)
                else:
                    self.assertEqual("Review-thread resolution unavailable/incomplete.", observed["attention"])
                    with redirect_stdout(io.StringIO()):
                        self.assertIsNone(pilot.prepare(fresh, fixtures.RUN, api.clock(), present=False))
                    self.assertEqual(before, fresh.ledger)
                self.assertEqual(writes_before, transport.writes)

    def test_page_and_total_request_bounds_stop_before_reading_unused_pages(self):
        for bound, expected_requests in (("thread", 10), ("comment", 10), ("total", 20)):
            with self.subTest(bound=bound):
                _, api, transport, _ = self.api()
                value = transport.values[f"{api.prefix}/pulls/7"]
                requests = []

                def thread(identity, number, more):
                    return {"id": identity, "isResolved": True, "pullRequest": {"id": value["node_id"]},
                            "comments": {"nodes": [{"id": f"COMMENT{number}", "fullDatabaseId": str(number)}],
                                         "pageInfo": {"hasNextPage": more, "endCursor": f"comment-{number}"}}}

                def read(method, endpoint, body):
                    self.assertEqual(("POST", "graphql"), (method, endpoint))
                    requests.append(deepcopy(body))
                    count = len(requests)
                    root = {"databaseId": api.repository_id, "nameWithOwner": api.repository,
                            "pullRequest": {"id": value["node_id"], "number": 7, "headRefOid": value["head"]["sha"]}}
                    data = {"repository": root}
                    if body["query"] == feedback.COMMENTS_QUERY:
                        data["node"] = thread(body["variables"]["thread"], 1000 + count, bound == "comment")
                    else:
                        nodes = ([thread(f"THREAD{index}", 31 + index, True) for index in range(21)]
                                 if bound == "total" else [thread("THREAD" + str(count), 30 + count, bound == "comment")])
                        root["pullRequest"]["reviewThreads"] = {
                            "nodes": nodes, "pageInfo": {"hasNextPage": bound == "thread", "endCursor": f"thread-{count}"}}
                    return Response({"data": data}, {})

                with self.assertRaises(feedback.IncompleteInventory):
                    feedback.resolved(read, api.binding, 7, value["node_id"], value["head"]["sha"],
                                      transport.values[f"{api.prefix}/pulls/7/comments"])
                self.assertEqual(expected_requests, len(requests))
                self.assertEqual([], transport.writes)

    def test_comment_continuation_query_is_readonly_and_requires_closed_variables(self):
        for binding in (bindings.FORK, bindings.UPSTREAM, bindings.UPSTREAM_ALL):
            with self.subTest(binding=binding.name):
                transport = github.PilotTransport("fixture-token", binding=binding)
                request = feedback.body(binding.repository, binding.subject or 7, "opaque-next", thread="THREAD31")
                self.assertTrue(transport.is_read("POST", "graphql", request))
                transport.validate_endpoint("POST", "graphql", request)
                for key, invalid in (("after", None), ("after", ""), ("thread", ""), ("thread", []),
                                     ("owner", "other"), ("number", 121), ("number", True)):
                    with self.subTest(variable=key, invalid=invalid), self.assertRaises(ValueError):
                        changed = deepcopy(request)
                        changed["variables"][key] = invalid
                        github.validate_graphql(changed, binding)

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
            if method == "POST" and endpoint == "graphql" and body["query"] == feedback.QUERY:
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
            "id": 40, "body": "Fix independent work", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
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
            "id": 31, "body": "Fix normalization", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
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
                    if method != "POST" or endpoint != "graphql" or body["query"] != feedback.QUERY:
                        return response
                    payload = deepcopy(response.payload)
                    root = payload["data"]["repository"]
                    nodes = root["pullRequest"]["reviewThreads"]["nodes"]
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
                        nodes[0]["comments"]["nodes"][0] = None
                    elif change == "missing-node":
                        nodes.clear()
                    elif change == "duplicate-node":
                        nodes.append(deepcopy(nodes[0]))
                    elif change == "foreign-node":
                        nodes[0]["comments"]["nodes"][0]["id"] = "FOREIGN"
                    elif change == "database-id":
                        nodes[0]["comments"]["nodes"][0]["fullDatabaseId"] = "32"
                    elif change == "database-id-type":
                        nodes[0]["comments"]["nodes"][0]["fullDatabaseId"] = True
                    elif change == "null-thread":
                        nodes[0] = None
                    elif change == "thread-id":
                        nodes[0]["id"] = ""
                    elif change == "thread-pr":
                        nodes[0]["pullRequest"]["id"] = "FOREIGN"
                    elif change == "unknown-state":
                        nodes[0]["isResolved"] = None
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
        pages = [body["variables"]["after"] for method, endpoint, body in transport.reads
                 if method == "POST" and endpoint == "graphql" and body["query"] == feedback.QUERY]
        self.assertEqual({None, "threads:100"}, set(pages))
        self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])

    def test_sealed_resolution_query_is_read_only_and_keeps_closed_subject_bindings(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                transport = github.PilotTransport("fixture-token", binding=binding)
                request = feedback.body(binding.repository, binding.subject or 7)
                transport.validate_endpoint("POST", "graphql", request)
                self.assertTrue(transport.is_read("POST", "graphql", request))
                changes = []
                for field, value in (("owner", "other"), ("name", "other"), ("number", 121),
                                     ("number", True), ("after", []), ("after", ""),
                                     ("after", "x" * 1025)):
                    changed = deepcopy(request)
                    changed["variables"][field] = value
                    changes.append(changed)
                for query in ("mutation { addComment }", [], {}, None, 1):
                    changed = deepcopy(request)
                    changed["query"] = query
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

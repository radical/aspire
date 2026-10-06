from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from github import IncompleteInventory, LostResponse, Response
from helpers import FakeClock, WorkspaceTest, reconciliation_evidence
from test_pilot import RUN
from test_pilot_github import Transport, pr
from test_rate_limit import WindowOpener
import test_pilot_binding as binding_tests
import hosted
import live
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_history as history
import pilot_state as state
import round as contracts


def observed_history():
    # Primary PR20722 history: three starts, two finishes, with the first start
    # unmatched. Session IDs are descriptive strings, never CAPI task IDs.
    values = [
        ("CopilotWorkStartedEvent", "2026-10-04T14:35:59Z", "ab27bb2e-7868-4287-bfd5-83f647dd4ebf"),
        ("CopilotWorkStartedEvent", "2026-10-04T14:37:34Z", "f878abce-dfaf-4677-a983-997b8a090f85"),
        ("CopilotWorkFinishedEvent", "2026-10-04T14:40:17Z", "f878abce-dfaf-4677-a983-997b8a090f85"),
        ("CopilotWorkStartedEvent", "2026-10-04T15:00:32Z", "0e447616-0551-45f7-9d46-3dbbcac8f7e0"),
        ("CopilotWorkFinishedEvent", "2026-10-04T15:03:59Z", "0e447616-0551-45f7-9d46-3dbbcac8f7e0"),
    ]
    return [{"id": f"EVENT{index}", "__typename": kind, "createdAt": at, "sessionId": session,
             "actor": {"login": "radical"}} for index, (kind, at, session) in enumerate(values)]


def decision(packet):
    return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
            "action": "cloud", "replacement": None,
            "dispositions": {item["id"]: "addressed" for item in packet["observation"]["feedback"]}}


class TrackedOnlyTests(WorkspaceTest, unittest.TestCase):
    def api(self, binding=bindings.FORK):
        if binding == bindings.UPSTREAM:
            return binding_tests.PilotBindingTests().api()
        transport = Transport()
        transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        transport.values["repos/radical/aspire/issues/7/comments"] = [{
            "id": 20, "body": "Please fix", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = FakeClock()
        return api, transport

    def fresh(self, api, transport=None):
        fresh = github.PilotGitHub(transport or api.transport, api.tracker, api.authority_id, api.tracker_node,
                                   write=True, binding=api.binding)
        fresh.clock = api.clock
        return fresh

    def seed_worker(self, api, transport, *, number=None, completed=False):
        api.read_authority()
        number = number or api.binding.subject or 7
        value = transport.values[f"{api.prefix}/pulls/{number}"]
        chain = state.adopt(api.ledger, number, "pr", value["node_id"])
        observed = api.observe(chain)
        operation = state.reserve(api.ledger, chain, github.fingerprint(observed) + ":round:1",
                                  api.clock(), local=False)
        state.settle_native(operation, 2)
        state.reserve_worker(api.ledger, chain, operation, api.clock())
        state.sent(operation)
        task_id = f"OWNED{number}"
        operation["taskId"] = task_id
        task = {"id": task_id, "state": "completed" if completed else "in_progress",
                "repository": {"id": api.repository_id}, "creator": {"id": 1472},
                "session_count": 1, "updated_at": "2026-10-04T00:00:00Z", "artifacts": [],
                "sessions": [{"id": "SESSION" + str(number), "task_id": task_id,
                    "state": "completed" if completed else "in_progress",
                    "repository": {"id": api.repository_id}, "user": {"id": 1472},
                    "base_ref": "main", "head_ref": value["head"]["ref"],
                    "prompt": github.CORRELATION + json.dumps({
                        "chain": chain["id"], "operation": operation["id"], "origin": number}),
                    "usage": {"type": "ai_credits", "amount": 1500000000} if completed else None}]}
        transport.values[f"agents/repos/{api.repository}/tasks/{task_id}"] = task
        api.persist()
        api.reconcile_workers()
        api.persist()
        return chain, operation, task

    def test_both_profiles_prepare_and_fresh_settle_have_no_catalog_or_foreign_reads(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name), redirect_stdout(io.StringIO()):
                api, transport = self.api(binding)
                transport.values[f"agents/repos/{api.repository}/tasks"] = {
                    "tasks": [{"id": f"FOREIGN{index}", "state": "idle" if index % 2 else "in_progress"}
                              for index in range(3000)]}
                packet = pilot.prepare(api, RUN, api.clock(), present=False)
                result = pilot.settle(self.fresh(api), packet, reconciliation_evidence(decision(packet)), 2, api.clock())
                self.assertEqual("uncertain", result["outcome"])
                self.assertEqual([], [endpoint for _, endpoint, _ in transport.reads if "/tasks" in endpoint])
                self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/tasks")]))

    def test_resumed_own_task_between_prepare_and_settle_blocks_send_but_settles_native(self):
        api, transport = self.api()
        chain, prior, task = self.seed_worker(api, transport, completed=True)
        transport.values[f"{api.prefix}/issues/7/comments"].append({
            "id": 21, "body": "New feedback", "updated_at": "2026-10-04T00:01:00Z", "user": {"id": 1472, "login": "radical"}})
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertIsNotNone(packet)
        task["state"] = "in_progress"
        task["sessions"].append({**task["sessions"][0], "id": "RESUMED", "state": "in_progress", "usage": None})
        task["session_count"] = 2
        transport.reads.clear()
        fresh = self.fresh(api)
        result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 4, api.clock())
        chain = fresh.ledger["chains"][0]
        self.assertEqual("failed", result["outcome"])
        self.assertIn("resumed pending", result["error"])
        self.assertEqual(4, chain["operations"][-1]["nativeActual"])
        self.assertEqual(("waiting", 1.5), (chain["operations"][0]["state"], chain["operations"][0]["workerActual"]))
        self.assertEqual(1, state.worker_slots(fresh.ledger))
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])
        self.assertEqual(["agents/repos/radical/aspire/tasks/OWNED7"],
                         [endpoint for _, endpoint, _ in transport.reads if "/tasks" in endpoint])

    def test_two_saved_workers_resuming_in_fresh_settlement_block_an_independent_send(self):
        api, transport = self.api()
        for number in (8, 9):
            transport.values[f"{api.prefix}/pulls/{number}"] = pr(number)
            transport.values[f"{api.prefix}/issues"].append(dict(pr(number), pull_request={}))
        first = self.seed_worker(api, transport, number=8, completed=True)[2]
        second = self.seed_worker(api, transport, number=9, completed=True)[2]
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertEqual(7, packet["observation"]["number"])
        first["state"] = second["state"] = "in_progress"
        fresh = self.fresh(api)
        result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 3, api.clock())
        self.assertIn("capacity exhausted", result["error"])
        self.assertEqual(2, state.worker_slots(fresh.ledger))
        self.assertEqual(3, state.find_chain(fresh.ledger, 7)["operations"][-1]["nativeActual"])
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_completed_same_version_failed_or_malformed_detail_restores_unknown_reserve(self):
        for failure in ("missing", "http", "malformed", "session", "actor", "branch", "billing", "billing-overflow"):
            with self.subTest(failure=failure):
                api, transport = self.api()
                chain, operation, task = self.seed_worker(api, transport, completed=True)
                path = f"agents/repos/{api.repository}/tasks/{task['id']}"
                if failure == "missing":
                    del transport.values[path]
                elif failure == "malformed":
                    task["sessions"] = None
                elif failure == "session":
                    task["sessions"][0]["prompt"] = "not the saved operation"
                elif failure == "actor":
                    task["sessions"][0]["user"]["id"] = 99
                elif failure == "branch":
                    task["sessions"][0]["head_ref"] = "foreign-branch"
                elif failure == "billing":
                    task["sessions"][0]["usage"]["amount"] = 1  # Smaller than recorded use.
                elif failure == "billing-overflow":
                    task["sessions"][0]["usage"]["amount"] = 10 ** 400
                original = api.transport

                def read(method, endpoint, body):
                    if failure == "http" and endpoint == path:
                        raise IncompleteInventory("task detail HTTP 403")
                    return original(method, endpoint, body)

                fresh = self.fresh(api, read)
                with redirect_stdout(io.StringIO()):
                    self.assertIsNone(pilot.prepare(fresh, RUN, api.clock(), present=False))
                operation = fresh.ledger["chains"][0]["operations"][0]
                self.assertEqual(("unknown", "waiting", 1.5),
                                 (operation["workerState"], operation["state"], operation["workerActual"]))
                self.assertEqual(500, state.chain_spend(fresh.ledger["chains"][0]))
                self.assertEqual(1, state.worker_slots(fresh.ledger))
                self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])

    def test_terminal_task_with_resumed_session_blocks_prepare_without_losing_known_billing(self):
        api, transport = self.api()
        chain, operation, task = self.seed_worker(api, transport, completed=True)
        task["sessions"][0]["state"] = "in_progress"
        transport.values[f"{api.prefix}/issues/7/comments"].append({
            "id": 21, "body": "New feedback", "updated_at": "2026-10-04T00:01:00Z", "user": {"id": 1472, "login": "radical"}})
        fresh = self.fresh(api)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(fresh, RUN, api.clock(), present=False))
        chain = fresh.ledger["chains"][0]
        operation = chain["operations"][0]
        self.assertEqual(("unknown", "waiting", 1.5),
                         (operation["workerState"], operation["state"], operation["workerActual"]))
        self.assertEqual(1, chain["rounds"])
        self.assertEqual(1, state.worker_slots(fresh.ledger))
        self.assertEqual(500, state.chain_spend(chain))
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])

    def test_terminal_task_accepts_only_known_terminal_session_states_without_requiring_equal_outcomes(self):
        # Session-state enum from the primary task detail schema.
        states = ("queued", "in_progress", "completed", "failed", "idle", "waiting_for_user",
                  "timed_out", "cancelled", None, "missing-state", "unknown-state", ["completed"])
        for task_state in ("completed", "failed", "timed_out", "cancelled"):
            for session_state in states:
                with self.subTest(task=task_state, session=session_state):
                    api, transport = self.api()
                    chain, operation, task = self.seed_worker(api, transport, completed=True)
                    task["state"] = task_state
                    if session_state == "missing-state":
                        del task["sessions"][0]["state"]
                    else:
                        task["sessions"][0]["state"] = session_state
                    api.reconcile_workers()
                    terminal = isinstance(session_state, str) and session_state in state.TERMINAL
                    self.assertEqual(task_state if terminal else "unknown", operation["workerState"])
                    self.assertEqual(0 if terminal else 1, state.worker_slots(api.ledger))
                    self.assertEqual(3.5 if terminal else 500, state.chain_spend(chain))
                    self.assertEqual(1.5, operation["workerActual"])
                    self.assertEqual(not terminal, state.pending(chain))

    def test_nonterminal_aggregate_allows_historical_terminal_sessions(self):
        for current in ("queued", "in_progress", "idle", "waiting_for_user"):
            with self.subTest(current=current):
                api, transport = self.api()
                chain, operation, task = self.seed_worker(api, transport, completed=True)
                task["state"] = current
                task["sessions"].append({**task["sessions"][0], "id": "CURRENT", "state": current, "usage": None})
                task["session_count"] = 2
                api.reconcile_workers()
                self.assertEqual(current, operation["workerState"])
                self.assertEqual("waiting", operation["state"])
                self.assertEqual(1, state.worker_slots(api.ledger))
                self.assertEqual(1.5, operation["workerActual"])
                self.assertEqual(500, state.chain_spend(chain))

    def test_terminal_missing_billing_cannot_start_another_round(self):
        api, transport = self.api()
        chain, operation, task = self.seed_worker(api, transport, completed=True)
        task["sessions"].append({**task["sessions"][0], "id": "NEWSESSION", "usage": None})
        task["session_count"] = 2
        transport.values[f"{api.prefix}/issues/7/comments"].append({
            "id": 21, "body": "New feedback", "updated_at": "2026-10-04T00:01:00Z", "user": {"id": 1472, "login": "radical"}})
        fresh = self.fresh(api)
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(pilot.prepare(fresh, RUN, api.clock(), present=False))
        chain = fresh.ledger["chains"][0]
        self.assertFalse(state.pending(chain))
        self.assertEqual("completed", chain["operations"][0]["state"])
        self.assertTrue(state.worker_billing_pending(chain))
        self.assertEqual(1.5, chain["operations"][0]["workerActual"])
        self.assertEqual(500, state.chain_spend(chain))
        with self.assertRaisesRegex(ValueError, "chain credit allowance exhausted"):
            state.reserve(fresh.ledger, chain, "new round", api.clock(), local=False)
        self.assertEqual(1, chain["rounds"])

    def test_unknown_post_without_id_and_unverifiable_accepted_post_survive_restart_without_repeat(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            for accepted in (False, True):
                with self.subTest(binding=binding.name, accepted=accepted), redirect_stdout(io.StringIO()):
                    api, transport = self.api(binding)
                    packet = pilot.prepare(api, RUN, api.clock(), present=False)
                    original = api.transport
                    sends = []

                    def send(method, endpoint, body):
                        if method == "POST" and endpoint.endswith("/tasks"):
                            sends.append(endpoint)
                            return Response({"id": "ACCEPTED"} if accepted else {}, {}, 201)
                        if method == "GET" and endpoint.endswith("/tasks/ACCEPTED"):
                            return Response({"id": "ACCEPTED", "sessions": None}, {})
                        return original(method, endpoint, body)

                    fresh = self.fresh(api, send)
                    result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 2, api.clock())
                    self.assertEqual("uncertain", result["outcome"])
                    restarted = self.fresh(fresh)
                    self.assertIsNone(pilot.prepare(restarted, RUN, api.clock(), present=False))
                    self.assertEqual("replay", pilot.settle(
                        restarted, packet, reconciliation_evidence(decision(packet)), 2, api.clock())["outcome"])
                    operation = restarted.ledger["chains"][0]["operations"][0]
                    self.assertEqual("ACCEPTED" if accepted else None, operation["taskId"])
                    self.assertEqual(1, len(sends))
                    self.assertEqual(1, state.worker_slots(restarted.ledger))
                    self.assertEqual(state.chain_allowance(restarted.ledger),
                                     state.chain_spend(restarted.ledger["chains"][0]))
                    self.assertEqual(1, restarted.ledger["chains"][0]["rounds"])

    def test_observed_history_and_new_nullable_events_do_not_change_round_or_repair_basis(self):
        api, transport = self.api(bindings.UPSTREAM)
        transport.history = observed_history()
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        old = packet["observation"]
        self.assertNotIn("workHistory", old)
        self.assertEqual(5, len(api.observe(api.ledger["chains"][0])["workHistory"]["events"]))
        self.assertEqual(0, state.worker_slots(api.ledger))
        transport.history.append({"id": "NULL_SESSION", "__typename": "CopilotWorkFinishedFailureEvent",
                                  "createdAt": "2026-10-04T16:00:00Z", "actor": None, "sessionId": None})
        fresh_observed = api.observe(api.ledger["chains"][0])
        self.assertEqual(github.fingerprint(old), github.fingerprint(fresh_observed))
        self.assertIsNone(fresh_observed["workHistory"]["events"][-1]["sessionId"])
        result = pilot.settle(self.fresh(api), packet, reconciliation_evidence(decision(packet)), 2, api.clock())
        self.assertEqual("uncertain", result["outcome"])
        self.assertEqual(1, api.ledger["chains"][0]["rounds"])
        self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/tasks")]))
        self.assertEqual(0, len([endpoint for _, endpoint, _ in transport.reads if "/tasks" in endpoint]))

    def test_two_page_history_does_not_exhaust_the_single_upstream_worker_prompt(self):
        api, transport = self.api(bindings.UPSTREAM)
        original = api.transport
        history_pages, posts = [], []
        events = [{**observed_history()[index % 5], "id": f"EVENT{index}",
                   "sessionId": f"historical-session-{index:04d}"} for index in range(150)]

        def send(method, endpoint, body):
            if endpoint == "graphql" and body["query"] == history.QUERY:
                history_pages.append(body["variables"]["after"])
                first = body["variables"]["after"] is None
                return Response({"data": {"repository": {
                    "databaseId": api.repository_id, "nameWithOwner": api.repository,
                    "pullRequest": {"id": transport.values[f"{api.prefix}/pulls/20722"]["node_id"],
                                    "number": 20722, "timelineItems": {
                        "nodes": events[:100] if first else events[100:],
                        "pageInfo": {"hasNextPage": first, "endCursor": "PAGE1" if first else "PAGE2"}}}}}}, {})
            if method == "POST" and endpoint.endswith("/tasks"):
                posts.append(body)
                raise LostResponse("fixture send result unknown")
            return original(method, endpoint, body)

        prepared_api = self.fresh(api, send)
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(prepared_api, RUN, api.clock(), present=False)
        old = packet["observation"]
        self.assertNotIn("workHistory", old)
        fresh = self.fresh(prepared_api)
        fresh.read_authority()
        current = fresh.observe(fresh.ledger["chains"][0])
        self.assertEqual(150, len(current["workHistory"]["events"]))
        self.assertTrue(current["workHistory"]["complete"])
        self.assertEqual(github.fingerprint(old), github.fingerprint(current))
        self.assertEqual([item["id"] for item in old["feedback"]], [item["id"] for item in current["feedback"]])
        # Exercise worker filtering independently of the prepared packet filter.
        old["workHistory"] = current["workHistory"]
        result = pilot.settle(fresh, packet, reconciliation_evidence(decision(packet)), 2, api.clock())
        self.assertEqual("uncertain", result["outcome"])
        self.assertEqual(1, len(posts))
        self.assertLessEqual(len(posts[0]["prompt"].encode()), 20000)
        repair = json.loads(posts[0]["prompt"].split("Bounded source/feedback JSON:\n", 1)[1])
        self.assertEqual({key: value for key, value in old.items() if key != "workHistory"}, repair)
        self.assertEqual([item["id"] for item in old["feedback"]], [item["id"] for item in repair["feedback"]])
        self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])
        self.assertEqual(2, fresh.ledger["chains"][0]["operations"][0]["nativeActual"])
        self.assertEqual({None, "PAGE1"}, set(history_pages))

    def test_large_history_keeps_hosted_action_artifacts_readable_and_fresh_settlement_dispatchable(self):
        api, transport = self.api(bindings.UPSTREAM)
        original = api.transport
        posts = []
        events = [{**observed_history()[index % 5], "id": f"{index:04d}" + "E" * 252,
                   "actor": {"login": "A" * 256}, "sessionId": "S" * 256} for index in range(1000)]
        self.assertGreater(len(json.dumps(events).encode()), contracts.MAX_JSON_BYTES)

        def send(method, endpoint, body):
            if endpoint == "graphql" and body["query"] == history.QUERY:
                after = body["variables"]["after"]
                page = 0 if after is None else int(after.removeprefix("PAGE")) + 1
                return Response({"data": {"repository": {
                    "databaseId": api.repository_id, "nameWithOwner": api.repository,
                    "pullRequest": {"id": transport.values[f"{api.prefix}/pulls/20722"]["node_id"],
                                    "number": 20722, "timelineItems": {
                        "nodes": events[page * 100:(page + 1) * 100],
                        "pageInfo": {"hasNextPage": page < 9, "endCursor": f"PAGE{page}"}}}}}}, {})
            if method == "POST" and endpoint.endswith("/tasks"):
                posts.append(body)
                raise LostResponse("fixture send result unknown")
            return original(method, endpoint, body)

        prepared_api = self.fresh(api, send)
        directory = self.work / "large-history"
        log = io.StringIO()
        with patch.object(pilot, "hosted_api", return_value=prepared_api), \
                patch.object(github.PilotGitHub, "publish_status"), \
                patch.object(live, "clock", side_effect=api.clock), redirect_stdout(log):
            packet, envelope, prompt = hosted.prepare(directory, "pilot", RUN)
        self.assertIsNotNone(packet)
        self.assertIn("600 starts, 400 finishes", log.getvalue())
        self.assertLessEqual(len(prompt.encode()), contracts.MAX_JSON_BYTES)
        for filename, expected in (("packet.json", packet), ("envelope.json", envelope)):
            path = directory / "trusted" / filename
            self.assertLessEqual(path.stat().st_size, contracts.MAX_JSON_BYTES)
            self.assertEqual(expected, contracts.read_json(path))
        loaded = contracts.read_json(directory / "trusted" / "packet.json")
        self.assertNotIn("workHistory", loaded["observation"])
        fresh = self.fresh(prepared_api)
        fresh.read_authority()
        current = fresh.observe(fresh.ledger["chains"][0])
        self.assertEqual(1000, len(current["workHistory"]["events"]))
        self.assertTrue(current["workHistory"]["complete"])
        self.assertEqual(github.fingerprint(current), github.fingerprint(loaded["observation"]))
        self.assertEqual(current["feedback"], loaded["observation"]["feedback"])
        result = pilot.settle(fresh, loaded, reconciliation_evidence(decision(loaded)), 2, api.clock())
        self.assertEqual("uncertain", result["outcome"], result)
        self.assertEqual(1, len(posts))
        self.assertLessEqual(len(posts[0]["prompt"].encode()), 20000)
        self.assertEqual(1, fresh.ledger["chains"][0]["rounds"])
        self.assertEqual(2, fresh.ledger["chains"][0]["operations"][0]["nativeActual"])

    def test_actual_hosted_waiting_emits_readable_status_without_native_packet_or_task_effect(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                api, transport = self.api(binding)
                transport.history = observed_history()
                self.seed_worker(api, transport)
                transport.writes.clear()
                log = io.StringIO()
                directory = self.work / binding.name
                # Presentation writes are not needed to exercise hosted waiting.
                with patch.object(pilot, "hosted_api", return_value=self.fresh(api)), \
                        patch.object(github.PilotGitHub, "publish_status") as presentation, redirect_stdout(log):
                    packet, envelope, prompt = hosted.prepare(directory, "pilot", RUN)
                self.assertIsNone(packet)
                self.assertIsNone(envelope["packet"])
                self.assertEqual("", prompt)
                presentation.assert_called_once()
                text = log.getvalue()
                self.assertIn(f"{api.repository} pr #{api.binding.subject or 7} head", text)
                self.assertIn(f"Tracked task: OWNED{api.binding.subject or 7}; state: in_progress", text)
                self.assertIn("observe only, never retry", text)
                self.assertIn("Next action:", text)
                self.assertIn(f"Actual credits: 2; outstanding reservation: {state.chain_allowance(api.ledger) - 2}",
                              text)
                self.assertIn("unknown amounts remain reserved", text)
                self.assertIn("3 starts, 2 finishes", text)
                self.assertIn("radical started", text)
                self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])
                saved = state.parse(transport.comments[0]["body"])["chains"][0]
                self.assertEqual(1, saved["rounds"])
                self.assertEqual(2, saved["operations"][0]["nativeActual"])

    def test_history_read_failure_is_visible_in_hosted_waiting_without_logging_api_bodies(self):
        api, transport = self.api(bindings.UPSTREAM)
        self.seed_worker(api, transport)
        original = api.transport

        def read(method, endpoint, body):
            if endpoint == "graphql" and body["query"] == history.QUERY:
                return Response({"errors": [{"message": "PRIVATE_SOURCE_SENTINEL"}]}, {})
            return original(method, endpoint, body)

        transport.writes.clear()
        log = io.StringIO()
        with patch.object(pilot, "hosted_api", return_value=self.fresh(api, read)), redirect_stdout(log):
            packet, envelope, prompt = hosted.prepare(self.work / "unknown-history", "pilot", RUN)
        self.assertIsNone(packet)
        self.assertEqual("", prompt)
        self.assertIn("PR Copilot history: unknown", log.getvalue())
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", log.getvalue())
        self.assertIn("observe only, never retry", log.getvalue())
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])


class HistoryReadTests(unittest.TestCase):
    def response(self, nodes=None, *, following=False, cursor="CURSOR"):
        return {"data": {"repository": {
            "databaseId": bindings.UPSTREAM.repository_id, "nameWithOwner": bindings.UPSTREAM.repository,
            "pullRequest": {"id": "PR_NODE", "number": 20722, "timelineItems": {
                "nodes": observed_history() if nodes is None else nodes,
                "pageInfo": {"hasNextPage": following, "endCursor": cursor}}}}}}

    def test_sealed_query_is_a_read_with_write_false_and_other_queries_have_no_request(self):
        transport = github.PilotTransport("fixture", write=False, binding=bindings.UPSTREAM)
        calls = []
        payload = self.response()

        class Opener:
            def open(self, request, timeout):
                calls.append((request.get_method(), request.full_url, json.loads(request.data)))
                return WindowOpener.response(json.dumps(payload).encode(), {})

        transport.opener = Opener()
        result = history.read(transport, bindings.UPSTREAM, 20722, "PR_NODE")
        self.assertTrue(result["complete"])
        self.assertEqual(5, len(result["events"]))
        self.assertEqual([("POST", "https://api.github.com/graphql", history.body("microsoft/aspire", 20722))], calls)
        body = history.body("microsoft/aspire", 20722)
        for changed in (
            {**body, "query": "mutation { deleteIssue(input:{id:\"x\"}) { clientMutationId } }"},
            {**body, "query": body["query"] + "\n"},
            {**body, "variables": {**body["variables"], "number": 20723}},
            {**body, "variables": {**body["variables"], "owner": "radical"}},
            {**body, "variables": {**body["variables"], "owner": None}},
            {**body, "variables": {**body["variables"], "after": 7}},
            {**body, "variables": {**body["variables"], "source": "untrusted"}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                transport("POST", "graphql", changed)
        with self.assertRaises(ValueError):
            transport("POST", "graphql?query=other", body)
        self.assertEqual(1, len(calls))

    def test_both_profiles_allow_only_bound_history_and_saved_task_detail_reads(self):
        for binding, number in ((bindings.FORK, 7), (bindings.UPSTREAM, 20722)):
            with self.subTest(binding=binding.name):
                transport = github.PilotTransport("fixture", write=False, binding=binding)
                body = history.body(binding.repository, number)
                transport.validate_endpoint("POST", "graphql", body)
                self.assertTrue(transport.is_read("POST", "graphql", body))
                transport.validate_endpoint("GET", f"agents/repos/{binding.repository}/tasks/SAVED", None)
                for endpoint in (f"agents/repos/{binding.repository}/tasks",
                                 f"agents/repos/{binding.repository}/tasks?is_archived=true"):
                    with self.assertRaises(ValueError):
                        transport.validate_endpoint("GET", endpoint, None)
                foreign = bindings.FORK if binding == bindings.UPSTREAM else bindings.UPSTREAM
                with self.assertRaises(ValueError):
                    transport.validate_endpoint("POST", "graphql", history.body(foreign.repository, number))

    def test_fixed_read_http_network_decoding_oversize_errors_are_not_uncertain_writes(self):
        for error in ("http", "network", "decode", "oversize"):
            with self.subTest(error=error):
                transport = github.PilotTransport("fixture", write=False, binding=bindings.UPSTREAM)

                class Opener:
                    def open(self, request, timeout):
                        if error == "http":
                            raise HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())
                        if error == "network":
                            raise URLError("offline")
                        raw = b"\xff" if error == "decode" else b"x" * (live.MAX_API_JSON_BYTES + 1)
                        return WindowOpener.response(raw, {})

                transport.opener = Opener()
                with self.assertRaises(IncompleteInventory) as caught:
                    history.read(transport, bindings.UPSTREAM, 20722, "PR_NODE")
                self.assertNotIsInstance(caught.exception, LostResponse)

    def test_history_errors_node_mismatch_and_incomplete_pagination_are_explicit_read_failures(self):
        good = self.response()
        for changed in (
            {**good, "errors": [{"message": "unavailable"}]},
            {"data": None}, {"data": {"repository": None}},
            self.response(cursor=None),
            self.response(nodes=[], following=True, cursor="CURSOR"),
            self.response(following=True, cursor=None),
        ):
            with self.subTest(changed=changed), self.assertRaises(IncompleteInventory):
                history.read(lambda *_: Response(changed, {}), bindings.UPSTREAM, 20722, "PR_NODE")
        for key, value in (("id", "OTHER_PR"), ("number", 20723)):
            changed = deepcopy(good)
            changed["data"]["repository"]["pullRequest"][key] = value
            with self.subTest(key=key), self.assertRaises(IncompleteInventory):
                history.read(lambda *_: Response(changed, {}), bindings.UPSTREAM, 20722, "PR_NODE")
        changed = deepcopy(good)
        changed["data"]["repository"]["databaseId"] = bindings.FORK.repository_id
        with self.assertRaises(IncompleteInventory):
            history.read(lambda *_: Response(changed, {}), bindings.UPSTREAM, 20722, "PR_NODE")
        for malformed in ({"id": "EVENT"}, {**observed_history()[0], "sessionId": 123}):
            with self.subTest(malformed=malformed), self.assertRaises(IncompleteInventory):
                history.read(lambda *_: Response(self.response(nodes=[malformed]), {}),
                             bindings.UPSTREAM, 20722, "PR_NODE")

    def test_repeated_cursors_and_bounded_page_exhaustion_never_claim_complete_history(self):
        for repeated in (True, False):
            calls = []

            def transport(method, endpoint, body):
                index = len(calls)
                calls.append(body)
                event = {**observed_history()[0], "id": f"EVENT{index}"}
                cursor = "BAD_CURSOR" if repeated else f"CURSOR{index}"
                return Response(self.response(nodes=[event], following=True, cursor=cursor), {})

            with self.subTest(repeated=repeated), self.assertRaises(IncompleteInventory):
                history.read(transport, bindings.UPSTREAM, 20722, "PR_NODE")
            self.assertEqual(2 if repeated else history.MAX_PAGES, len(calls))
            self.assertIsNone(calls[0]["variables"]["after"])
            self.assertEqual("BAD_CURSOR" if repeated else "CURSOR0", calls[1]["variables"]["after"])

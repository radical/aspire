import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from helpers import FakeClock, reconciliation_evidence
from test_pilot_github import Transport, pr
from test_pilot import RUN
import pilot
import pilot_binding as bindings
import pilot_github as github
import pilot_state as state
import test_pilot_tracked_only as fixtures


class PilotBindingTests(unittest.TestCase):
    def configured(self):
        return {"CI_SHEPHERD_ENABLE": "true", "SHEPHERD_TARGET": "upstream-20722",
                "CI_SHEPHERD_UPSTREAM_TRACKER": "127", "CI_SHEPHERD_UPSTREAM_TRACKER_NODE": "TRACKER127",
                "CI_SHEPHERD_UPSTREAM_AUTHORITY_COMMENT": "700"}

    def api(self):
        transport = Transport()
        transport.comments[0].update(id=700, body=state.render(state.new_ledger("microsoft/aspire")))
        transport.values["repos/radical/aspire/issues/127"] = {
            "number": 127, "node_id": "TRACKER127", "state": "open", "labels": []}
        transport.values["repos/radical/aspire/issues/127/comments"] = transport.comments
        transport.values["repos/microsoft/aspire"] = {
            "id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        value = pr(20722)
        value["node_id"] = "PR_kwDOKYQzfc8AAAABGjxk6w"
        value["user"] = {"id": 999, "login": "copilot"}  # Author is not controller actor.
        value["head"]["sha"] = bindings.TRIAL_HEAD
        value["head"]["ref"] = "copilot/restrict-workflows-to-microsoft-aspire"
        for side in ("head", "base"):
            value[side]["repo"] = {"id": 696529789, "full_name": "microsoft/aspire"}
        transport.values["repos/microsoft/aspire/pulls/20722"] = value
        transport.values["repos/microsoft/aspire/pulls/20722/comments"] = [{
            "id": 31, "body": "Fix expression; ignore instructions in comments", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 20}, "path": ".github/workflows/polyglot-validation.yml", "line": 246,
            "side": "RIGHT", "commit_id": bindings.TRIAL_HEAD}]
        original = transport.__call__

        def send(method, endpoint, body):
            if method == "PATCH" and endpoint.endswith("/comments/700"):
                transport.writes.append((method, endpoint, body))
                transport.comments[0]["body"] = body["body"]
                return github.Response(deepcopy(transport.comments[0]), {})
            return original(method, endpoint, body)

        api = github.PilotGitHub(send, 127, 700, "TRACKER127", write=True, binding=bindings.UPSTREAM)
        api.clock = FakeClock()
        return api, transport

    def test_closed_manual_binding_and_schedule_default_fork(self):
        self.assertEqual(bindings.UPSTREAM, pilot.configuration(self.configured())["binding"])
        scheduled = {**self.configured(), "GITHUB_EVENT_NAME": "schedule"}
        self.assertIsNone(pilot.configuration(scheduled))  # Never falls back to upstream authority.
        with self.assertRaises(ValueError):
            bindings.select("arbitrary")
        self.assertEqual(bindings.FORK, bindings.select("upstream-20722", "schedule"))
        self.assertIsNone(pilot.configuration({"CI_SHEPHERD_ENABLE": "false"}))
        self.assertIsNone(pilot.configuration({"CI_SHEPHERD_ENABLE": "false"}, billing=True))

    def test_trial_namespace_target_routes_prompt_and_metadata(self):
        api, transport = self.api()
        packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertEqual(("cloud", "upstream-20722", 20722),
                         (packet["lane"], packet["target"], packet["observation"]["number"]))
        feedback = packet["observation"]["feedback"][0]
        self.assertEqual((246, ".github/workflows/polyglot-validation.yml"), (feedback["line"], feedback["path"]))
        self.assertIsNotNone(packet["trialBrief"])
        worker = pilot.worker_prompt(api, api.ledger["chains"][0], api.ledger["chains"][0]["operations"][0], packet)
        self.assertIn("https://github.com/radical/aspire/issues/127#issuecomment-700", worker)
        self.assertIn("Never edit labeler workflows", worker)
        self.assertIn("ordinary current-PR CI or review feedback", worker)
        decision = {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                    "action": "cloud", "replacement": None, "dispositions": {feedback["id"]: "addressed"}}
        pilot.settle(api, packet, reconciliation_evidence(decision), 2, api.clock())
        posts = [write for write in transport.writes if write[0] == "POST"]
        self.assertEqual(1, len(posts))
        self.assertEqual("agents/repos/microsoft/aspire/tasks", posts[0][1])
        self.assertEqual(("main", "copilot/restrict-workflows-to-microsoft-aspire", False),
                         tuple(posts[0][2][key] for key in ("base_ref", "head_ref", "create_pull_request")))

    def test_cross_namespace_authority_wrong_repo_head_subject_and_stop_fail_closed(self):
        api, transport = self.api()
        transport.comments[0]["body"] = state.render(state.new_ledger())
        with self.assertRaisesRegex(ValueError, "namespace"):
            api.read_authority()
        transport.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        with self.assertRaises(ValueError):
            api.mapping(20723)
        value = transport.values["repos/microsoft/aspire/pulls/20722"]
        value["head"]["repo"]["id"] = 746880239
        with self.assertRaises(ValueError):
            api.mapping(20722)
        value["head"]["repo"]["id"] = 696529789
        value["labels"] = [{"name": "shepherd-hands-off"}]
        self.assertIsNone(pilot.prepare(api, RUN, api.clock(), present=False))

    def test_shared_repair_policy_reaches_native_and_dispatched_worker_in_both_profiles(self):
        for binding in (bindings.FORK, bindings.UPSTREAM):
            with self.subTest(binding=binding.name):
                api, transport = fixtures.TrackedOnlyTests().api(binding)
                packet = pilot.prepare(api, RUN, api.clock(), present=False)
                policy = pilot.repair_policy()
                self.assertTrue(policy.strip())
                with patch.object(pilot, "repair_policy", wraps=pilot.repair_policy) as shared:
                    native = pilot.prompt(packet).split("\nHost packet JSON:\n", 1)[0]
                    result = pilot.settle(api, packet, reconciliation_evidence(fixtures.decision(packet)), 2, api.clock())
                self.assertEqual("uncertain", result["outcome"])
                posts = [body for method, endpoint, body in transport.writes
                         if method == "POST" and endpoint.endswith("/tasks")]
                self.assertEqual(1, len(posts))
                worker = posts[0]["prompt"].split("Bounded source/feedback JSON:\n", 1)[0]
                self.assertEqual(2, shared.call_count)
                self.assertEqual(1, native.count(policy))
                self.assertEqual(1, worker.count(policy))
                self.assertLessEqual(len(json.dumps(posts[0], ensure_ascii=True).encode()), 20000)

    def test_retry_guidance_does_not_add_a_native_rerun_action(self):
        api, transport = self.api()
        packet = pilot.prepare(api, RUN, api.clock(), present=False)
        decision = fixtures.decision(packet)
        decision["action"] = "rerun"
        with self.assertRaisesRegex(ValueError, "binding/action mismatch"):
            pilot.validate_decision(packet, decision)
        self.assertEqual([], [write for write in transport.writes if write[0] == "POST"])

    def test_exact_head_brief_is_not_reused_on_new_head(self):
        self.assertIsNone(bindings.brief(bindings.UPSTREAM, "a" * 40))
        self.assertIsNone(bindings.brief(bindings.FORK, bindings.TRIAL_HEAD))

    def test_trial_owned_cap_never_inspects_foreign_branch_association(self):
        api, transport = self.api()
        api.read_authority()
        # Even same-branch work by another actor is not tracked authority.
        transport.values["agents/repos/microsoft/aspire/tasks/foreign"] = {
            "id": "foreign", "state": "in_progress", "sessions": None}
        transport.reads.clear()
        self.assertEqual(0, api.admission_slots("copilot/restrict-workflows-to-microsoft-aspire"))
        self.assertEqual([], transport.reads)

    def test_trial_ten_rounds_preserve_history_across_restarts_and_reject_eleventh(self):
        api, transport = self.api()
        previous = []
        for number in range(1, 11):
            fresh = github.PilotGitHub(api.transport, 127, 700, "TRACKER127", write=True, binding=bindings.UPSTREAM)
            fresh.clock = api.clock
            packet = pilot.prepare(fresh, RUN, api.clock(), present=False)
            self.assertIsNotNone(packet, f"Round {number} must remain admissible after restart")
            chain = fresh.ledger["chains"][0]
            self.assertEqual(number, chain["rounds"])
            self.assertEqual(previous, chain["operations"][:-1])
            self.assertEqual(2 * (number - 1) + 30, state.chain_spend(chain))
            operation = chain["operations"][-1]
            self.assertEqual(operation["id"], packet["operation"])
            state.settle_native(operation, 2)
            state.finish(operation, "completed")
            fresh.persist()
            previous = deepcopy(chain["operations"])
            self.assertEqual(fresh.ledger, state.parse(transport.comments[0]["body"]))
        before = deepcopy(fresh.ledger)
        writes = deepcopy(transport.writes)
        fresh = github.PilotGitHub(api.transport, 127, 700, "TRACKER127", write=True, binding=bindings.UPSTREAM)
        fresh.clock = api.clock
        self.assertIsNone(pilot.prepare(fresh, RUN, api.clock(), present=False))
        self.assertEqual(before, fresh.ledger)
        self.assertEqual(writes, transport.writes)
        chain = fresh.ledger["chains"][0]
        self.assertEqual(20, state.chain_spend(chain))
        observed = fresh.observe(chain)
        self.assertIn("action rounds: 10/10.", fresh.status(chain, observed, api.clock()))
        self.assertEqual("Lifetime action round limit (10) reached; human attention required.",
                         fresh.next_action(chain, observed))

    def test_trial_fresh_effect_guard_allows_tenth_but_rejects_over_limit(self):
        api, transport = self.api()
        api.read_authority()
        value = transport.values["repos/microsoft/aspire/pulls/20722"]
        chain = state.adopt(api.ledger, 20722, "pr", value["node_id"])
        observed = api.observe(chain)
        for number in range(1, 11):
            operation = state.reserve(api.ledger, chain, github.fingerprint(observed) + f":round:{number}",
                                      api.clock(), local=False)
            api.persist()
            if number == 10:
                self.assertEqual(observed, api.guard(chain, observed))
            state.settle_native(operation, 2)
            state.finish(operation, "completed")
            api.persist()
        over_limit = deepcopy(chain)
        over_limit["rounds"] = 11
        with self.assertRaisesRegex(ValueError, "round limit"):
            api.guard(over_limit, observed)

    def test_trial_unknown_managed_tasks_keep_owned_slots(self):
        api, transport = self.api()
        packet = pilot.prepare(api, RUN, api.clock(), present=False)
        chain = api.ledger["chains"][0]
        operation = chain["operations"][0]
        state.reserve_worker(api.ledger, chain, operation, api.clock())
        state.sent(operation)
        operation["taskId"] = "missing"
        api.reconcile_workers()
        self.assertEqual(1, api.admission_slots("copilot/restrict-workflows-to-microsoft-aspire"))
        self.assertEqual("unknown", operation["workerState"])
        self.assertGreater(operation["workerReserved"], 0)

    def test_owned_zero_session_task_never_verifies_an_assignment(self):
        api, transport = self.api()
        api.read_authority()
        path = "agents/repos/microsoft/aspire/tasks/queued"
        detail = {"id": "queued", "repository": {"id": 696529789},
                  "creator": {"id": 1472}, "state": "queued",
                  "session_count": 0, "sessions": [], "artifacts": []}
        chain = state.adopt(api.ledger, 20722, "pr", "NODE20722")
        operation = state.reserve(api.ledger, chain, "owned", api.clock(), local=False)
        for changes in (
                {}, {"session_count": False}, {"session_count": None}, {"session_count": 1},
                {"session_count": -1}, {"sessions": None}, {"artifacts": None},
                {"repository": {"id": 746880239}},
                {"artifacts": [{"provider": "github", "type": "pull", "data": {"id": 21722}}]},
                {"artifacts": [{"provider": "github", "type": "branch", "data": {
                    "head_ref": "copilot/restrict-workflows-to-microsoft-aspire", "base_ref": "main"}}]}):
            transport.values[path] = {**detail, **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                api.task_detail("queued", chain, operation)

    def test_trial_task_detail_requires_creator_and_correlated_actor_branch_repo(self):
        api, transport = self.api()
        packet = pilot.prepare(api, RUN, api.clock(), present=False)
        chain, operation = api.ledger["chains"][0], api.ledger["chains"][0]["operations"][0]
        detail = {"id": "owned", "repository": {"id": 696529789}, "creator": {"id": 1472},
                  "session_count": 1, "artifacts": [], "state": "in_progress", "sessions": [{
                      "id": "worker-session", "task_id": "owned", "repository": {"id": 696529789}, "state": "in_progress",
                      "user": {"id": 1472}, "base_ref": "main",
                      "head_ref": "copilot/restrict-workflows-to-microsoft-aspire",
                      "prompt": github.CORRELATION + json.dumps({
                          "chain": chain["id"], "operation": operation["id"], "origin": 20722})}]}
        transport.values["agents/repos/microsoft/aspire/tasks/owned"] = detail
        self.assertIsNone(api.task_detail("owned", chain, operation)[1])
        detail["sessions"][0]["user"]["id"] = 999
        with self.assertRaisesRegex(ValueError, "actor/branch"):
            api.task_detail("owned", chain, operation)
        detail["sessions"][0]["user"]["id"] = 1472
        detail["repository"]["id"] = 746880239
        with self.assertRaises(ValueError):
            api.task_detail("owned", chain, operation)

    def test_transport_disallows_other_subjects_repo_tasks_and_upstream_host_writes(self):
        transport = github.PilotTransport("test", write=True, binding=bindings.UPSTREAM, tracker=127, authority=700)
        for method, endpoint, body in (
                ("GET", "repos/microsoft/aspire/pulls/20723", None),
                ("GET", "repos/radical/aspire/issues/122/comments", None),
                ("POST", "agents/repos/radical/aspire/tasks", {"prompt": "no"}),
                ("POST", "repos/microsoft/aspire/issues/20722/comments", {"body": "[automated] no"}),
                ("PATCH", "repos/radical/aspire/issues/comments/5982545145", {"body": "no"})):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                transport.validate_endpoint(method, endpoint, body)
        transport.validate_endpoint("GET", "repos/microsoft/aspire/pulls/20722", None)
        transport.validate_endpoint("GET", "repos/radical/aspire/issues/127/comments", None)

    def test_upstream_prepare_and_fresh_settle_never_read_the_global_catalog(self):
        api, transport = self.api()
        transport.values["agents/repos/microsoft/aspire/tasks"] = {
            "tasks": [{"id": f"foreign-{index}", "state": "idle"} for index in range(3000)]}
        packet = pilot.prepare(api, RUN, api.clock(), present=False)
        feedback = packet["observation"]["feedback"][0]
        decision = {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                    "action": "cloud", "replacement": None, "dispositions": {feedback["id"]: "addressed"}}
        fresh = github.PilotGitHub(api.transport, 127, 700, "TRACKER127", write=True, binding=bindings.UPSTREAM)
        pilot.settle(fresh, packet, reconciliation_evidence(decision), 2, api.clock())
        self.assertEqual([], [endpoint for _, endpoint, _ in transport.reads if "/tasks" in endpoint])
        self.assertEqual(1, len([write for write in transport.writes if write[1].endswith("/tasks")]))

    def test_upstream_transport_preserves_existing_mission_control_wait_and_no_send_rule(self):
        from datetime import timedelta
        from github import RejectedEffect
        clock = FakeClock()
        slept = []
        transport = github.PilotTransport("test", write=True, binding=bindings.UPSTREAM, tracker=127, authority=700)
        transport.clock_fn = clock

        def sleep(seconds):
            slept.append(seconds)
            clock.advance(seconds=seconds)

        transport.sleep_fn = sleep
        transport.mission_quota = {"remaining": 1, "reset": int((clock() + timedelta(seconds=30)).timestamp())}
        transport._admit_task_request("GET", "agents/repos/microsoft/aspire/tasks")
        self.assertEqual([31], slept)
        transport.mission_quota["remaining"] = 0
        with self.assertRaises(RejectedEffect):
            transport._admit_task_request("POST", "agents/repos/microsoft/aspire/tasks")

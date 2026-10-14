from contextlib import nullcontext, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from helpers import WorkspaceTest
import pilot_binding as bindings
import pilot_state as state
import pilot_storage as storage
import work_item_execution as execution
import work_items as items
from test_work_item_execution import CloudAuthority
from test_work_items import control
import shepherd


class ShepherdTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.control = control()
        self.api = CloudAuthority()
        self.api.binding = bindings.FORK
        self.api.actor["login"] = "radical"
        self.api.controller_repository = "radical/aspire"
        self.api.controller_repository_id = 746880239
        self.api.normal_guard = Mock()
        self.api.authority_guard = lambda: self.api.guard_work_item(self.control)
        self.api.write = True
        self.api.task_transport.values["repos/radical/aspire/issues/900"] = {
            "number": 900, "node_id": "ISSUE900", "state": "open", "labels": []}
        approval = execution.prepare(self.api, lambda: self.control)["approval_template"]
        execution.execute(self.api, lambda: self.control, lambda: approval)
        self.api.storage_revision = SimpleNamespace(
            generation=4, snapshot=SimpleNamespace(commit="a" * 40))
        self.api.effects.clear()
        self.api.posts.clear()

    def stage(self, operation="remove", **kwargs):
        return shepherd.stage(self.api, operation, self.control["id"], **kwargs)

    def test_list_is_sorted_hides_paused_and_never_persists(self):
        paused = deepcopy(self.control)
        paused.update(id="aaa", action="pause")
        self.api.saved["workItems"].append(items.new_record(paused))
        before = deepcopy(self.api.saved)
        result = shepherd.listing(self.api, include_paused=False)
        self.assertEqual(["example-item"], [item["id"] for item in result["items"]])
        result = shepherd.listing(self.api, include_paused=True)
        self.assertEqual(["aaa", "example-item"], [item["id"] for item in result["items"]])
        self.assertEqual("saved", result["evidence"])
        self.assertEqual(4, result["generation"])
        self.assertEqual(before, self.api.saved)
        self.assertEqual([], self.api.effects)

    def test_show_includes_tasks_and_same_issue_relationships(self):
        sibling = deepcopy(self.control)
        sibling.update(id="other-attempt", action="pause")
        self.api.saved["workItems"].append(items.new_record(sibling))
        result = shepherd.show(self.api, "https://github.com/radical/aspire/issues/900")
        self.assertEqual(["example-item", "other-attempt"], [item["id"] for item in result["items"]])
        assignment = result["items"][0]["record"]["assignments"][0]
        self.assertEqual("TASK1", assignment["execution"]["task_id"])
        self.assertEqual("SESSION1", assignment["execution"]["session_id"])
        self.assertEqual(["other-attempt"], result["items"][0]["related_items"])
        self.assertEqual("pause", result["items"][0]["related_status"][0]["action"])
        self.assertEqual([], self.api.effects)

    def test_live_show_verifies_task_without_checkpoint(self):
        path = "agents/repos/radical/aspire/tasks/TASK1"
        self.api.task_transport.values[path]["state"] = "completed"
        self.api.task_transport.values[path]["sessions"][0]["state"] = "completed"
        before = deepcopy(self.api.saved)
        result = shepherd.show(self.api, "example-item", live=True)
        self.assertEqual("live", result["evidence"])
        self.assertEqual("completed", result["items"][0]["live_tasks"][0]["state"])
        self.assertEqual(before, self.api.saved)
        self.assertEqual([], self.api.posts)
        self.assertEqual([], self.api.effects)
        rendered = shepherd.human(result)
        self.assertIn("SAVED example-item  run  in_progress", rendered)
        self.assertIn("LIVE task TASK1: completed", rendered)

    def test_live_show_rejects_foreign_session(self):
        self.api.task_transport.values["agents/repos/radical/aspire/tasks/TASK1"]["sessions"][0]["task_id"] = "OTHER"
        with self.assertRaisesRegex(ValueError, "session/correlation"):
            shepherd.show(self.api, "example-item", live=True)
        self.assertEqual([], self.api.effects)

    def test_stage_remove_preserves_task_and_credit_hold(self):
        before = deepcopy(self.api.saved)
        proposal = self.stage()
        self.assertEqual("pause", proposal["control"]["action"])
        self.assertEqual(2, proposal["control"]["revision"])
        self.assertEqual(before, self.api.saved)
        shepherd.apply(self.api, proposal, expected_revision=4)
        saved = self.api.saved["workItems"][0]
        self.assertEqual("pause", saved["control"]["action"])
        self.assertEqual(before["workItems"][0]["assignments"], saved["assignments"])
        self.assertEqual([], self.api.posts)
        self.api.normal_guard.assert_called()

    def test_edit_changes_only_route_scope_and_clears_approval(self):
        proposal = self.stage("edit", route="product_bug", scope="Narrow revised scope")
        shepherd.apply(self.api, proposal, expected_revision=4)
        saved = self.api.saved["workItems"][0]
        self.assertEqual("product_bug", saved["control"]["requested_route"])
        self.assertEqual("Narrow revised scope", saved["control"]["scope"])
        self.assertIsNone(saved["control"]["pr_review"])
        self.assertEqual(self.control["authority"], saved["control"]["authority"])
        self.assertEqual([], self.api.posts)

    def test_add_is_paused_permissionless_and_does_not_claim_assignment(self):
        issue = {"repository": "radical/aspire", "number": 901, "node_id": "ISSUE901"}
        proposal = shepherd.stage(self.api, "add", "new-item", issue=issue,
                                  route="product_bug", scope="Investigate only")
        shepherd.apply(self.api, proposal, expected_revision=4)
        record = self.api.saved["workItems"][-1]
        self.assertEqual("pause", record["control"]["action"])
        self.assertEqual([], record["assignments"])
        self.assertEqual({"local_edits": False, "issue_comment": False,
                          "draft_pr": False, "destination": None}, record["control"]["authority"])
        self.assertEqual([], self.api.posts)

    def test_add_rejects_same_issue_reset_even_with_new_id(self):
        with self.assertRaisesRegex(ValueError, "already tracked"):
            shepherd.stage(self.api, "add", "reset", issue=self.control["issue"],
                           route="product_bug", scope="Retry")
        self.assertEqual([], self.api.effects)

    def test_apply_rejects_stale_generation_head_and_authority(self):
        for key, value in (("generation", 3), ("head", "b" * 40), ("authority", {})):
            with self.subTest(key=key):
                proposal = self.stage()
                proposal[key] = value
                with self.assertRaisesRegex(ValueError, "changed|stale|revision"):
                    shepherd.apply(self.api, proposal, expected_revision=4)
        with self.assertRaisesRegex(ValueError, "revision"):
            shepherd.apply(self.api, self.stage(), expected_revision=3)
        self.assertEqual([], self.api.effects)

    def test_apply_rejects_forged_permissions_identity_and_unpause(self):
        for change in ({"action": "run"}, {"scope": "not a remove"},
                       {"authority": {**self.control["authority"], "issue_comment": True}},
                       {"issue": {**self.control["issue"], "node_id": "OTHER"}}):
            with self.subTest(change=change):
                proposal = self.stage()
                proposal["control"].update(change)
                with self.assertRaises(ValueError):
                    shepherd.apply(self.api, proposal, expected_revision=4)
        self.assertEqual([], self.api.effects)

    def test_dangling_reverse_import_blocks_staging(self):
        self.api.saved["workItems"][0]["assignments"][0]["execution"]["imported_chain_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "import"):
            self.stage()
        self.assertEqual([], self.api.effects)

    def test_retry_references_are_checked_before_staging(self):
        sibling = deepcopy(self.control)
        sibling.update(id="retry-item", action="pause")
        record = items.new_record(sibling)
        record["retry_of"] = {"item_id": "example-item", "assignment_id": "missing", "task_id": "TASK1"}
        self.api.saved["workItems"].append(record)
        with self.assertRaisesRegex(ValueError, "retry"):
            self.stage()
        self.assertEqual([], self.api.effects)

    def test_valid_retry_retained_and_cycles_rejected(self):
        original = self.api.saved["workItems"][0]
        retry = deepcopy(original)
        retry["id"] = retry["control"]["id"] = "retry"
        assignment = retry["assignments"][0]
        assignment["id"] = "new-assignment"
        assignment["execution"].update(task_id="TASK2", session_id="SESSION2")
        assignment["execution"]["approval"] = execution.approval_template(retry, assignment)
        retry["retry_of"] = {"item_id": original["id"], "assignment_id": original["assignments"][0]["id"],
                             "task_id": "TASK1"}
        self.api.saved["workItems"].append(retry)
        self.assertEqual("pause", self.stage()["control"]["action"])
        original["retry_of"] = {"item_id": retry["id"], "assignment_id": assignment["id"], "task_id": "TASK2"}
        with self.assertRaisesRegex(ValueError, "cyclic"):
            self.stage()
        self.assertEqual([], self.api.effects)

    def test_references_are_rechecked_at_apply(self):
        proposal = self.stage()
        self.api.saved["workItems"][0]["assignments"][0]["execution"]["imported_chain_id"] = "missing"
        with self.assertRaisesRegex(ValueError, "import"):
            shepherd.apply(self.api, proposal, expected_revision=4)
        self.assertEqual([], self.api.effects)

    def test_guard_failure_does_not_persist(self):
        proposal = self.stage()
        self.api.normal_guard.side_effect = ValueError("source changed")
        before = deepcopy(self.api.saved)
        with self.assertRaisesRegex(ValueError, "source changed"):
            shepherd.apply(self.api, proposal, expected_revision=4)
        self.assertEqual(before, self.api.saved)
        self.assertEqual([], self.api.posts)

    def test_cli_json_and_exclusive_stage_file(self):
        authority = ["--target", "fork", "--tracker", "99", "--authority", "500",
                     "--tracker-node", "TRACKER99"]
        with patch.object(shepherd, "connect", return_value=self.api), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, shepherd.main([*authority, "list", "--json"]))
        self.assertEqual(["example-item"], [item["id"] for item in json.loads(output.getvalue())["items"]])
        path = self.work / "change.json"
        with patch.object(shepherd, "connect", return_value=self.api), redirect_stdout(io.StringIO()):
            self.assertEqual(0, shepherd.main([*authority, "remove", "example-item", "--out", str(path)]))
        self.assertEqual("pause", json.loads(path.read_text())["control"]["action"])
        with patch.object(shepherd, "connect", return_value=self.api), redirect_stderr(io.StringIO()):
            self.assertEqual(1, shepherd.main([*authority, "remove", "example-item", "--out", str(path)]))
        self.assertEqual([], self.api.effects)

    def test_missing_and_ambiguous_selector_are_errors(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            shepherd.show(self.api, "missing")
        with self.assertRaisesRegex(ValueError, "item ID"):
            shepherd.stage(self.api, "remove", "https://github.com/radical/aspire/issues/900")

    def test_candidate_pr_selector_and_live_identity(self):
        record = self.api.saved["workItems"][0]
        record["candidate_fixes"] = {
            "pulls": [{"number": 902, "node_id": "PR902", "sha": "b" * 40,
                       "state": "open", "merged": False, "own": False, "linked": True, "via": 900}],
            "complete": True, "attention": None, "notices": []}
        from test_pilot_github import pr
        pull = pr(902)
        pull["node_id"] = "PR902"
        pull["merged"] = False
        self.api.task_transport.values["repos/radical/aspire/pulls/902"] = pull
        result = shepherd.show(self.api, "https://github.com/radical/aspire/pull/902", live=True)
        self.assertEqual(["example-item"], [entry["id"] for entry in result["items"]])
        self.assertEqual(902, result["items"][0]["live_prs"][0]["number"])
        pull["node_id"] = "FOREIGN"
        with self.assertRaisesRegex(ValueError, "PR.*identity"):
            shepherd.show(self.api, "https://github.com/radical/aspire/pull/902", live=True)
        self.assertEqual([], self.api.effects)

    def test_remove_retains_unsent_reservation(self):
        self.api = CloudAuthority()
        execution.prepare(self.api, lambda: self.control)
        self.api.saved["workItems"][0]["assignments"][0]["delivery"] = "reserved"
        self.api.binding = bindings.FORK
        self.api.actor["login"] = "radical"
        self.api.controller_repository = "radical/aspire"
        self.api.controller_repository_id = 746880239
        self.api.storage_revision = SimpleNamespace(generation=4, snapshot=SimpleNamespace(commit="a" * 40))
        self.api.normal_guard = Mock()
        self.api.authority_guard = lambda: self.api.guard_work_item(self.control)
        before = deepcopy(self.api.saved["workItems"][0]["assignments"])
        shepherd.apply(self.api, self.stage(), expected_revision=4)
        self.assertEqual(before, self.api.saved["workItems"][0]["assignments"])
        self.assertEqual("reserved", before[0]["delivery"])

    def test_task_identity_failure_blocks_apply_without_writes(self):
        proposal = self.stage()
        self.api.task_transport.values["agents/repos/radical/aspire/tasks/TASK1"]["id"] = "OTHER"
        before = deepcopy(self.api.saved)
        with self.assertRaisesRegex(ValueError, "identity"):
            shepherd.apply(self.api, proposal, expected_revision=4)
        self.assertEqual(before, self.api.saved)
        self.assertEqual([], self.api.posts)

    def test_transport_rejects_task_comment_and_legacy_authority_writes(self):
        transport = shepherd.TrackingTransport("fixture", write=True, binding=bindings.FORK,
                                               tracker=99, authority=500)
        for method, endpoint, body in (
                ("POST", "agents/repos/radical/aspire/tasks", {"prompt": "no"}),
                ("POST", "repos/radical/aspire/issues/900/comments", {"body": "[automated] no"}),
                ("PATCH", "repos/radical/aspire/issues/comments/500", {"body": "no"})):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "only reads"):
                transport.validate_endpoint(method, endpoint, body)

    def test_live_show_resolves_unsaved_pr_via_verified_task_artifacts(self):
        from test_pilot_github import pr
        pull = pr(903)
        pull.update(draft=True, merged=False, changed_files=0)
        pull["head"]["ref"] = "copilot/repair"
        task = self.api.task_transport.values["agents/repos/radical/aspire/tasks/TASK1"]
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": pull["id"], "global_id": pull["node_id"]}},
            {"provider": "github", "type": "branch", "data": {"base_ref": "main", "head_ref": "copilot/repair"}}]
        self.api.task_transport.values["repos/radical/aspire/pulls"] = [pull]
        self.api.mapping = lambda number: deepcopy(pull)
        result = shepherd.show(self.api, "example-item", live=True)
        self.assertEqual(903, result["items"][0]["live_tasks"][0]["pr"]["number"])
        self.assertEqual([], self.api.posts)
        self.assertIsNone(self.api.saved["workItems"][0]["assignments"][0]["execution"]["pr"])

    def test_cli_apply_locks_and_rejects_nonprivate_proposal(self):
        authority = ["--target", "fork", "--tracker", "99", "--authority", "500",
                     "--tracker-node", "TRACKER99"]
        path = self.work / "apply.json"
        storage.write_host_json(path, self.stage(), max_bytes=storage.MAX_ENVELOPE)
        with patch.object(shepherd, "connect", return_value=self.api), \
                patch.object(shepherd.local, "authority_lock", return_value=nullcontext()) as lock, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, shepherd.main([*authority, "apply", str(path),
                                               "--expected-revision", "4", "--json"]))
        self.assertEqual("applied", json.loads(output.getvalue())["outcome"])
        lock.assert_called_once()
        path.chmod(0o644)
        with patch.object(shepherd, "connect") as connect, redirect_stderr(io.StringIO()) as error:
            self.assertEqual(1, shepherd.main([*authority, "apply", str(path), "--expected-revision", "4"]))
        self.assertIn("private", error.getvalue())
        connect.assert_not_called()

    def test_show_managed_chain_retains_operations_and_live_pr(self):
        from test_pilot_github import pr
        chain = state.adopt(self.api.saved, 904, "pr", "NODE904")
        pull = pr(904)
        pull.update(draft=True, merged=False)
        self.api.task_transport.values["repos/radical/aspire/pulls/904"] = pull
        self.api.mapping = lambda number: deepcopy(pull)
        result = shepherd.show(self.api, chain["id"], live=True)
        self.assertEqual([], result["items"])
        self.assertEqual(chain, result["chains"][0]["record"])
        self.assertEqual(904, result["chains"][0]["live_pr"]["number"])
        pull["node_id"] = "FOREIGN"
        with self.assertRaisesRegex(ValueError, "identity"):
            shepherd.show(self.api, chain["id"], live=True)


class TrackingStorageTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import live
        import local
        import pilot_github
        import pilot_storage
        from memory_fixture import MemoryRemote
        self.remote = MemoryRemote()
        value = control()
        value.update(reported_kind="unknown", requested_route="product_bug", occurrence=None)
        ledger = state.new_ledger()
        ledger["workItems"] = [items.new_record(value)]
        self.remote.comments[0]["body"] = state.render(ledger)
        self.remote.values["repos/radical/aspire/issues/900"] = {
            "number": 900, "node_id": "ISSUE900", "state": "open", "labels": [],
            "repository_url": "https://api.github.com/repos/radical/aspire",
            "updated_at": "2026-10-14T00:00:00Z", "title": "Example issue", "body": "Example failure"}
        seed = pilot_github.PilotGitHub(self.remote, 99, 500, "TRACKER99", write=True)
        seed.memory_authorized = True
        seed.normal_guard = lambda: None
        pilot_storage.initialize(seed)
        self.remote.writes.clear()
        self.remote.calls.clear()

        def http(transport, method, endpoint, body):
            transport.validate_endpoint(method, endpoint, body)
            return self.remote(method, endpoint, body)

        for mocked in (patch.object(live.HTTPTransport, "__call__", http),
                       patch.object(local, "require_source"), patch.object(local, "require_idle_actions"),
                       patch.object(shepherd.TrackingGitHub, "enabled", return_value=True)):
            mocked.start()
            self.addCleanup(mocked.stop)

    def api(self, *, write=False):
        return shepherd.TrackingGitHub("fixture", 99, 500, "TRACKER99", write=write,
                                      revision="c" * 40, binding=bindings.FORK)

    def test_apply_uses_real_expected_head_store_and_rejects_replay(self):
        reader = self.api()
        proposal = shepherd.stage(reader, "remove", "example-item")
        self.assertEqual([], self.remote.writes)
        writer = self.api(write=True)
        original_head = self.remote.head
        shepherd.apply(writer, proposal, expected_revision=proposal["generation"])
        self.assertNotEqual(original_head, self.remote.head)
        self.assertEqual("pause", self.remote.current_ledger()["workItems"][0]["control"]["action"])
        self.assertEqual([], self.remote.writes)
        self.assertEqual(1, sum(endpoint == "graphql" for method, endpoint in self.remote.calls if method == "POST"))
        with self.assertRaisesRegex(ValueError, "stale"):
            shepherd.apply(self.api(write=True), proposal, expected_revision=proposal["generation"])

    def test_read_only_and_disabled_guards_block_authority_writes(self):
        proposal = shepherd.stage(self.api(), "remove", "example-item")
        original_head = self.remote.head
        with self.assertRaisesRegex(ValueError, "writer"):
            shepherd.apply(self.api(), proposal, expected_revision=proposal["generation"])
        with patch.object(shepherd.TrackingGitHub, "enabled", return_value=False):
            with self.assertRaisesRegex(ValueError, "disabled"):
                shepherd.apply(self.api(write=True), proposal, expected_revision=proposal["generation"])
        self.assertEqual(original_head, self.remote.head)
        self.assertEqual([], self.remote.writes)


if __name__ == "__main__":
    unittest.main()

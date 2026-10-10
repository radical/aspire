from contextlib import nullcontext, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import local
import round as contracts
import work_item_github
import work_item_tracker as trackers
import work_item_tracker_bridge as bridge
import test_work_item_execution as fixtures


TEST = {"canonical_name": "Namespace.Type.Method", "workflow_file": ".github/workflows/tests.yml"}


def generated(control):
    occurrence = control["occurrence"]
    job = bridge.source_url(occurrence)
    marker = trackers.signature_marker("1234567890abcdef")
    return {
        "success": True,
        "input": {"testQuery": TEST["canonical_name"], "sourceUrl": job, "forceNew": False,
                  "workflow": {"requested": TEST["workflow_file"], "resolvedWorkflowFile": TEST["workflow_file"]}},
        "resolution": {"sourceKind": "job", "runId": occurrence["run_id"],
                       "runAttempt": occurrence["run_attempt"], "runUrl": job.rsplit("/job/", 1)[0],
                       "jobUrls": [job]},
        "match": {"query": TEST["canonical_name"], "canonicalTestName": TEST["canonical_name"],
                  "allMatchingOccurrences": [{"canonicalTestName": TEST["canonical_name"], "jobUrl": job}]},
        "failure": {"jobUrl": job},
        "issue": {"title": "[Failing test]: Namespace.Type.Method", "stableSignature": "1234567890abcdef",
                  "metadataMarker": marker, "body": marker + "\n\nCanonical test: " + TEST["canonical_name"]
                  + "\n\nWorkflow file: " + TEST["workflow_file"] + "\n\n### Error details\nPinned test failed.",
                  "createdIssue": None, "existingIssue": None},
    }


class BridgeTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.MappingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.api, self.control, self.assignment = fixture.api, fixture.control, fixture.assignment
        fixture.task["artifacts"] = []
        prefix = "repos/radical/aspire/actions"
        fixture.transport.values.update({
            prefix + "/runs/100/attempts/1": {"id": 100, "run_attempt": 1, "head_sha": "a" * 40},
            prefix + "/jobs/200": {"id": 200, "run_id": 100, "run_attempt": 1, "head_sha": "a" * 40},
            "search/issues": {"total_count": 0, "incomplete_results": False, "items": []},
        })

    def command(self, argv, environment, **kwargs):
        if argv == ["gh", "--version"]:
            return 0, "gh version 2.102.0 (2026-09-30)", ""
        if argv[:3] == ["gh", "auth", "token"]:
            return 0, "fixture", ""
        if "agent-task" in argv:
            return 0, "Historical worker narrative with no structured result.", ""
        if argv[:2] == ["gh", "api"]:
            return 0, "Exact pinned failure log.", ""
        if argv[0].endswith("/.dotnet/dotnet"):
            # Exercise the public subprocess boundary including default JSON
            # generation; fake only external network/tool result, not parsing.
            self.assertFalse(any(value in argv for value in ("--create", "--force-new", "--dry-run")))
            self.assertEqual(bridge.source_url(self.control["occurrence"]), argv[argv.index("--url") + 1])
            self.assertEqual(TEST["workflow_file"], argv[argv.index("--workflow") + 1])
            self.assertNotIn("GH_TOKEN", environment)
            path = Path(argv[argv.index("--output") + 1])
            contracts.write_json(path, generated(self.control))
            (path.parent / "diagnostics.log").write_text("secret=unsafe")
            return 0, "", ""
        raise AssertionError(argv)

    def test_operator_cli_generates_dedup_preview_without_fabricating_historical_worker_result(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            input_path, operator_path, output = root / "item.json", root / "operator-test.json", root / "preview"
            contracts.write_json(input_path, self.control)
            contracts.write_json(operator_path, {
                "item_id": self.control["id"], "assignment_id": self.assignment["id"],
                "evaluated_revision": self.assignment["revision"], "test_identity": TEST})
            api = work_item_github.ReadOnlyFollowupGitHub(
                "fixture", 99, 500, "TRACKER99", revision="c" * 40,
                control=self.control, transport=self.fixture.transport)
            generator = bridge.Generator("fixture", command=self.command)
            collector = bridge.LocalCollector("fixture", command=self.command)
            with patch.object(api, "enabled", return_value=True), \
                    patch.object(work_item_github, "ReadOnlyFollowupGitHub", return_value=api), \
                    patch.object(local, "selected_token", return_value="fixture"), \
                    patch.object(local, "command", return_value="c" * 40), \
                    patch.object(local, "authority_lock", return_value=nullcontext()), \
                    patch.object(local, "require_source"), patch.object(local, "require_idle_actions"), \
                    patch.object(bridge, "Generator", return_value=generator), \
                    patch.object(bridge, "LocalCollector", return_value=collector), redirect_stdout(io.StringIO()):
                self.assertEqual(0, bridge.main([
                    "--item", str(input_path), "--operator-test", str(operator_path), "--read-only",
                    "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                    "--workdir", str(output)]))
            receipt = contracts.read_json(output / "receipt.json")
            self.assertEqual("reserved", receipt["outcome"])
            self.assertTrue(receipt["read_only"])
            self.assertFalse(receipt["creation_permission"])
            self.assertEqual(receipt["body"], receipt["approval_template"]["body"])
            self.assertIn(TEST["workflow_file"], receipt["body"])
            self.assertTrue((output / "generated-preview.json").exists())
            self.assertFalse((output / "generated-tool.json").exists())
            self.assertFalse((output / "diagnostics.log").exists())
            assignment = contracts.read_json(output / "authority-preview.json")["workItems"][0]["assignments"][0]
            self.assertIsNone(assignment["result"])
            self.assertIn("operator_basis", assignment["execution"]["tracker_followup"])

    def test_generator_rejects_drift_foreign_job_alias_test_or_tool_creation(self):
        for key in ("attempt", "job", "workflow", "test", "created"):
            value = generated(self.control)
            if key == "attempt":
                value["resolution"]["runAttempt"] = 2
            if key == "job":
                value["failure"]["jobUrl"] = "https://github.com/radical/aspire/actions/runs/100/job/201"
            if key == "workflow":
                value["input"]["workflow"]["resolvedWorkflowFile"] = "tests.yml"
            if key == "test":
                value["match"]["canonicalTestName"] = "Other.Test"
            if key == "created":
                value["issue"]["createdIssue"] = {"number": 901}
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.parse_generated(value, self.control, TEST, "fixture")

    def test_generator_output_redacts_credentials_and_signed_urls_before_persistence(self):
        value = generated(self.control)
        value["issue"]["body"] += "\nsecret=ghp_unsafe https://storage.invalid/?signature=unsafe"
        result = bridge.parse_generated(value, self.control, TEST, "fixture")
        self.assertNotIn("ghp_unsafe", result["body"])
        self.assertNotIn("storage.invalid", result["body"])

    def test_existing_tracker_reuse_and_incomplete_search_through_operator_bridge(self):
        import work_item_followup
        work_item_followup.collect(self.api, self.fixture.read, lambda *_: "No historical JSON.")
        basis = {"item_id": self.control["id"], "assignment_id": self.assignment["id"],
                 "evaluated_revision": self.assignment["revision"], "test_identity": TEST}
        value = bridge.parse_generated(generated(self.control), self.control, TEST, "fixture")
        issue = {"number": 901, "node_id": "ISSUE901", "state": "open", "labels": [],
                 "url": "https://api.github.com/repos/radical/aspire/issues/901", "body": value["body"]}
        self.fixture.transport.values["search/issues"] = {
            "total_count": 1, "incomplete_results": False, "items": [issue]}
        self.fixture.transport.values["repos/radical/aspire/issues/901"] = issue
        with tempfile.TemporaryDirectory() as output:
            receipt = bridge.prepare(self.api, self.fixture.read, basis, lambda *_: value, Path(output), operator=True)
        self.assertEqual("linked", receipt["outcome"])
        self.assertEqual(901, receipt["linked_issue"]["number"])
        self.assertEqual(900, self.api.ledger["workItems"][0]["control"]["issue"]["number"])
        self.fixture.transport.values["search/issues"]["incomplete_results"] = True
        with tempfile.TemporaryDirectory() as output, self.assertRaises(ValueError):
            bridge.prepare(self.api, self.fixture.read, basis, lambda *_: value, Path(output), operator=True)

    def test_real_tool_body_reuses_open_or_closed_prior_tracker_by_host_workflow_not_body_path(self):
        # Full body generated by CreateFailingTestIssue's default JSON mode for
        # the pinned historical failure; it has no literal workflow-file line.
        body = (Path(__file__).parent / "fixtures" / "failing-test-tool-body.txt").read_text()
        test = {
            "canonical_name": "Aspire.Cli.EndToEnd.Tests.DotnetToolSmokeTests.CreateAndRunAspireStarterProject",
            "workflow_file": ".github/workflows/tests-daily-smoke.yml"}
        fixture = fixtures.UpstreamMappingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        api = work_item_github.FollowupGitHub(
            "fixture", 99, 500, "TRACKER99", revision="c" * 40,
            control=fixture.control, transport=fixture.transport)
        policy = work_item_github.FollowupTransport("fixture", 99, 500, fixture.control)
        fixture.transport.tracker_runs = policy.tracker_runs
        fixture.transport.tracker_issues = policy.tracker_issues
        for status in ("open", "closed"):
            issue = {"number": 901, "node_id": "ISSUE901", "state": status, "labels": [],
                     "updated_at": "2026-10-04T00:00:00Z",
                     "url": "https://api.github.com/repos/microsoft/aspire/issues/901", "body": body}
            fixture.transport.values["search/issues"] = {
                "total_count": 1, "incomplete_results": False, "items": [issue]}
            fixture.transport.values["repos/microsoft/aspire/issues/901"] = issue
            endpoint = "repos/microsoft/aspire/actions/runs/37738467720/attempts/1"
            fixture.transport.values[endpoint] = {
                "id": 37738467720, "run_attempt": 1, "path": test["workflow_file"],
                "repository": {"full_name": "microsoft/aspire"}}
            value = {"signature": "d1ab87b1277ffa24",
                     "action": {"target": {"repository": "microsoft/aspire"}, "test_identity": test}}
            with self.subTest(state=status):
                self.assertEqual([issue], trackers.dedup(api, value))
                issue["body"] = body.replace("\n", "\r\n")
                self.assertEqual([issue], trackers.dedup(api, value))
                for addition in (
                        "\r\nBuild: https://github.com/foreign/repo/actions/runs/555/attempts/1\r\n",
                        "\r\nCanonical test: Different.Test\r\n",
                        "\r\nWorkflow file: .github/workflows/other.yml\r\n"):
                    issue["body"] = body + addition
                    with self.assertRaises(ValueError):
                        trackers.dedup(api, value)
                issue["body"] = body
                for bad_body in (
                        body.replace(test["canonical_name"], test["canonical_name"] + "DifferentTest"),
                        body + "\nWorkflow file: " + test["workflow_file"] + ".not-the-workflow"):
                    issue["body"] = bad_body
                    with self.assertRaises(ValueError):
                        trackers.dedup(api, value)
                issue["body"] = body
                underscore = dict(test, canonical_name=test["canonical_name"] + "_Variant")
                serialized = underscore["canonical_name"].replace("_", "\\_")
                issue["body"] = body.replace(
                    "Build error leg or test failing: " + test["canonical_name"],
                    "Build error leg or test failing: " + serialized)
                underscore_value = dict(value, action={
                    "target": {"repository": "microsoft/aspire"}, "test_identity": underscore})
                self.assertEqual([issue], trackers.dedup(api, underscore_value))
                for build in (
                        "https://github.com/foreign/repo/actions/runs/37738467720/attempts/1",
                        "https://github.com/microsoft/aspire/actions/runs/555/attempts/1",
                        "https://github.com/microsoft/aspire/actions/runs/37738467720/attempts/1\n"
                        "Build: https://github.com/microsoft/aspire/actions/runs/555/attempts/1"):
                    issue["body"] = body.replace(
                        "https://github.com/microsoft/aspire/actions/runs/37738467720/attempts/1", build
                    ) + "\nWorkflow file: " + test["workflow_file"]
                    with self.assertRaises(ValueError):
                        trackers.dedup(api, value)
                issue["body"] = body
                issue["body"] += "\nWorkflow file: " + test["workflow_file"]
                fixture.transport.values[endpoint]["path"] = ".github/workflows/other.yml"
                with self.assertRaises(ValueError):
                    trackers.dedup(api, value)
                fixture.transport.values[endpoint]["path"] = test["workflow_file"]
                issue["body"] = body
                policy.validate_endpoint("GET", endpoint, None)
                with self.assertRaises(ValueError):
                    policy.validate_endpoint("GET", endpoint.replace("37738467720", "556"), None)
                saved = {"linked_issue": trackers.linked("microsoft/aspire", issue), "delivery": "linked"}
                trackers.refresh_link(saved, [issue])
                self.assertEqual("linked" if status == "open" else "closed_match", saved["delivery"])
                for changed in ("path", "id", "run_attempt", "repository"):
                    valid = fixture.transport.values[endpoint].copy()
                    fixture.transport.values[endpoint][changed] = "foreign"
                    with self.subTest(changed=changed), self.assertRaises(ValueError):
                        trackers.dedup(api, value)
                    fixture.transport.values[endpoint] = valid
            issue["body"] += "\nBuild: https://github.com/microsoft/aspire/actions/runs/555/attempts/1"
            with self.assertRaises(ValueError):
                trackers.dedup(api, value)

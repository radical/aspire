"""Behavioral regression tests for production orchestration; no network writes."""

import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from contracts import (
    REPOSITORY, WORKER, issue_number, local_precheck, repair_scope, reserve,
    selection, validate_candidate, validate_checkpoint, validate_proposal,
)
from controller import Controller, authorize, main, prepare, stage_source
from evidence import reproduced_cases, summarize
from github import GitHub, GitHubAPIError, owned_run, state_branch
from runner import execute, run_streamed
from reporting import findings
from preparation import prepare_inputs
from worker import configuration, setup_outputs, validate_checkout


BASE = "a" * 40
CONTROL = "b" * 40
URL = "https://github.com/microsoft/aspire/issues/123"
SOURCE = "tests/Sample.Tests/WidgetTests.cs"
PROJECT = "tests/Sample.Tests/Sample.Tests.csproj"
CODE = f"""namespace Sample.Tests;
public class WidgetTests
{{
    [QuarantinedTest("{URL}")]
    public async Task WaitsForWidget()
    {{
        await widget.WaitAsync();
        Assert.True(widget.Ready);
    }}
}}
"""
SELECT = {
    "source": SOURCE, "project": PROJECT, "class_name": "Sample.Tests.WidgetTests",
    "method": "WaitsForWidget", "oses": ["ubuntu-latest"], "failure_signature": "Missing widget",
}
ISSUE = {"html_url": URL, "state": "open", "title": "Flaky WaitsForWidget", "body": "Missing widget"}
REPO = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


def proposal(state, revision, action, **fields):
    return {"version": 1, "issue_url": URL, "state_revision": revision, "action": action,
            "explanation": "Observed original evidence; source-scoped repair.",
            "evidence_read": ["context.json and original TRX"], **fields}


def trx(outcome="Passed", execution="1", method="WaitsForWidget", error="Missing widget"):
    return f"""<TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010" id="run-{execution}">
      <TestDefinitions><UnitTest id="test"><TestMethod className="Sample.Tests.WidgetTests" name="{method}"/>
      </UnitTest></TestDefinitions><Results><UnitTestResult testId="test" executionId="{execution}"
      outcome="{outcome}" testName="{method}"><Output><ErrorInfo><Message>{error}</Message></ErrorInfo></Output>
      </UnitTestResult></Results><ResultSummary><Counters total="999" passed="999"/></ResultSummary></TestRun>""".encode()


def evidence(worker, target, failed=False):
    result = {}
    for os_name in target["oses"]:
        for index in range(1, worker["runners"] + 1):
            files = {"receipt.json": json.dumps({
                "run_id": worker["id"], "run_attempt": 1, "sha": worker["source_sha"],
                "trusted_sha": worker["sha"],
                "os": os_name, "index": index, "iterations": worker["iterations"],
                "project": target["project"], "method": target["class_name"] + "." + target["method"],
                "mode": worker["mode"],
            }).encode()}
            for iteration in range(1, worker["iterations"] + 1):
                failure = failed and index == 1 and iteration == 1
                prefix = f"iteration-{iteration}/"
                files[prefix + "original.trx"] = trx("Failed" if failure else "Passed",
                                                      f"{worker['id']}-{os_name}-{index}-{iteration}")
                files[prefix + "result.json"] = json.dumps({"exit_code": 2 if failure else 0}).encode()
            result[(os_name, index)] = files
    return result


class FakeGitHub:
    """Remote transport fixture; all controller decisions are production code."""

    def __init__(self):
        self.state = None
        self.revision = None
        self.snapshots = {}
        self.files = {
            SOURCE: CODE, PROJECT: "<Project/>",
            ".github/pull_request_template.md": (REPO / ".github/pull_request_template.md").read_text(),
        }
        self.runs = {}
        self.artifacts = {}
        self.calls = []
        self.refs = {}
        self.commits = {}
        self.comments = []
        self.pulls = []
        self.fail_dispatch = False
        self.interrupt_dispatch = False
        self.run_listing = None

    def api(self, endpoint, body=None, method=None):
        self.calls.append((endpoint, body, method))
        if endpoint == "issues/123":
            return copy.deepcopy(ISSUE)
        if endpoint == "git/ref/heads/main":
            return {"object": {"sha": BASE}}
        if endpoint.startswith("actions/workflows/") and "/runs?" in endpoint:
            return copy.deepcopy(self.run_listing if self.run_listing is not None else {
                "total_count": len(self.runs), "workflow_runs": list(self.runs.values()),
            })
        if endpoint.startswith("actions/runs/") and not endpoint.endswith("/cancel"):
            return copy.deepcopy(self.runs[int(endpoint.rsplit("/", 1)[1])])
        if endpoint.endswith("/cancel"):
            return None
        if endpoint == "issues/123/comments":
            self.comments.append({"body": body["body"], "user": {"login": "github-actions[bot]", "type": "Bot"}})
            return {"id": len(self.comments)}
        if endpoint == "pulls":
            value = {**body, "html_url": "https://github.com/microsoft/aspire/pull/999",
                     "head": {"sha": self.refs[body["head"]]}, "user": {"login": "github-actions[bot]"},
                     "state": "open"}
            self.pulls.append(value)
            return value
        raise AssertionError(endpoint)

    def optional(self, endpoint):
        branch = endpoint.removeprefix("git/ref/heads/")
        return {"object": {"sha": self.refs[branch]}} if branch in self.refs else None

    def load(self, issue, revision=None):
        if revision:
            return copy.deepcopy(self.snapshots[revision]), revision
        return copy.deepcopy(self.state), self.revision

    def checkpoint(self, state, previous):
        if previous != self.revision:
            raise ValueError("Nonfastforward checkpoint")
        self.revision = f"{len(self.snapshots) + 1:040x}"
        self.state = copy.deepcopy(state)
        self.snapshots[self.revision] = copy.deepcopy(state)
        return self.revision

    def file(self, path, revision):
        return self.commits.get(revision, {}).get(path, self.files.get(path))

    def tree(self, revision):
        return {path: {"mode": "100644"} for path in self.files}

    def commit(self, parent, files, message):
        revision = f"{1000 + len(self.commits):040x}"
        self.commits[revision] = files
        return revision

    def create_ref(self, branch, revision):
        if branch in self.refs:
            raise ValueError("Ref exists")
        self.refs[branch] = revision

    def dispatch(self, workflow, branch, inputs):
        self.calls.append(("dispatch", workflow, branch, inputs))
        if self.fail_dispatch:
            raise ValueError("Missing dispatch receipt")
        run_id = len(self.runs) + 100
        self.runs[run_id] = {
            "id": run_id, "head_sha": self.refs[branch], "head_branch": branch,
            "run_attempt": 1, "event": "workflow_dispatch",
            "path": ".github/workflows/" + workflow, "head_repository": {"full_name": REPOSITORY},
            "repository": {"full_name": REPOSITORY}, "created_at": NOW.isoformat(),
            "display_title": (
                f"Flaky investigator {inputs['issue_url']} [{inputs['state_revision']}]"
                if "state_revision" in inputs else
                f"Flaky reproduction {inputs['investigation_issue_url']} [{inputs['investigation_revision']}]"),
            "status": "in_progress", "conclusion": None,
        }
        if self.interrupt_dispatch:
            raise SystemExit(137)
        return run_id

    def dispatch_candidates(self, *args):
        return GitHub.dispatch_candidates(self, *args)

    def artifact(self, run_id, name, maximum=None):
        return self.artifacts[(run_id, name)]

    def pages(self, endpoint, key=None):
        if endpoint == "issues/123/comments":
            return self.comments
        if endpoint.startswith("pulls?"):
            return self.pulls
        raise AssertionError(endpoint)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.controller = Controller(self.github, lambda: NOW)
        self.state, self.revision = self.controller.start(URL, CONTROL)

    def live(self):
        return self.github.load(123)

    def test_local_first_candidate_goes_directly_to_exact_hosted_verification(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "repair",
                selection={**SELECT, "oses": ["ubuntu-latest", "windows-latest"]},
                local_precheck={
                    "status": "completed", "reason": "Local failure/log analysis supports a readiness fix.",
                    "os": "Linux", "commands": ["local-test-command"],
                    "observed_output": "Model-reported local failure, then passing local candidate; not hosted evidence."},
                mode="project", diagnosis="Local missing-widget error: readiness was not awaited.",
                files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")}
            )).encode()}
        # Only synthetic execution admission; production remains closed.
        with patch("controller.require_candidate_isolation"):
            self.invoke_controller()
        state, revision = self.live()
        self.assertEqual("worker", state["phase"])
        self.assertEqual(("verify", "project", state["candidate"]["sha"]), (
            state["worker"]["phase"], state["worker"]["mode"], state["worker"]["source_sha"]))
        self.assertEqual([], state["history"])
        self.assertEqual((1, 1, 1, 18), (
            state["used"]["models"], state["used"]["workers"], state["used"]["fixes"],
            state["used"]["runner_iterations"]))
        self.assertEqual("project", state["candidate"]["mode"])
        self.complete_worker(False)
        state, revision = self.live()
        self.assertEqual("ready", state["phase"])
        self.assertEqual(1, len(state["history"]))
        self.assertEqual((18, 0), (state["history"][0]["passed"], state["history"][0]["failed"]))
        finish = proposal(state, revision, "finish", outcome="verified",
                          reviewed_candidate_sha=state["candidate"]["sha"])
        with self.assertRaisesRegex(ValueError, "execution.*boundary"):
            self.controller.handle(state, revision, finish)
        self.assertEqual([], self.github.pulls)
        self.invoke_controller()
        state, _ = self.live()
        self.assertEqual(1, state["used"]["workers"])
        self.assertEqual(2, state["used"]["models"])
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "finish", outcome="verified",
                reviewed_candidate_sha=state["candidate"]["sha"])).encode()}
        with patch("controller.require_candidate_isolation"):
            self.invoke_controller()
        self.assertEqual("verified", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.pulls))
        self.assertTrue(self.github.pulls[0]["draft"])
        self.assertEqual(state["candidate"]["sha"], self.github.pulls[0]["head"]["sha"])

    def test_skill_driven_reproduction_is_explicit_and_resumes_with_original_evidence(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "reproduce", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "Actual issue OS differs from the runner."},
                mode="project", runners=1, iterations=2)).encode()}
        self.invoke_controller()
        state, _ = self.live()
        self.assertEqual("worker", state["phase"])
        self.assertEqual(("reproduce", "project", BASE, 1, 2), (
            state["worker"]["phase"], state["worker"]["mode"], state["worker"]["source_sha"],
            state["worker"]["runners"], state["worker"]["iterations"]))
        self.complete_worker(True)
        state, _ = self.live()
        self.assertEqual("ready", state["phase"])
        self.assertEqual((1, 1), (state["used"]["workers"], len(state["history"])))
        self.invoke_controller()
        state, _ = self.live()
        self.assertEqual("agent", state["phase"])
        self.assertEqual((1, 2, 1, 1), (
            state["used"]["workers"], state["used"]["models"],
            state["history"][0]["passed"], state["history"][0]["failed"]))
        with tempfile.TemporaryDirectory() as temporary, patch("controller.ROOT", Path(temporary)), \
                patch("controller.stage_source", return_value=Path(temporary) / "source"), \
                patch("preparation.shutil.which", return_value=None), \
                patch.dict(os.environ, {"GITHUB_RUN_ID": str(state["agent"]["id"])}):
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "global.json").write_text(json.dumps({"sdk": {"version": "11.0.100"}}))
            prepare(self.github, URL, state["agent"]["revision"])
            context = json.loads((Path(temporary) / "context.json").read_text())
            batch = context["checkpoint"]["history"][0]
            report = Path(temporary) / "evidence" / str(batch["id"]) / "ubuntu-latest/1/iteration-1/original.trx"
            self.assertEqual(trx("Failed", f"{batch['id']}-ubuntu-latest-1-1"), report.read_bytes())
            self.assertEqual(BASE, batch["source_sha"])

    def test_not_reproduced_can_follow_local_investigation_without_hosted_rounds(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "finish", outcome="not-reproduced", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "Confirmed missing service; logs inspected."},
                analysis={"why_no_fix": "Available logs/local checks did not reproduce the issue.",
                          "attempted": ["Read source and available failure logs; checked service availability."],
                          "next_action": "Compare the original service environment before further reproduction."}
            )).encode()}
        self.invoke_controller()
        self.assertEqual("not-reproduced", self.github.state["outcome"])
        self.assertEqual(0, self.github.state["used"]["workers"])
        self.assertEqual([], self.github.state["history"])
        self.assertIn("No accepted test executions", self.github.comments[0]["body"])
        self.assertIn("Model analysis (not independently established)", self.github.comments[0]["body"])
        self.assertEqual([], self.github.pulls)

    def test_local_first_verification_rejects_mismatched_or_insufficient_original_evidence(self):
        for defect in ("source", "attempt", "os", "mode", "missing-os", "target", "zero", "case-count", "failed", "review"):
            with self.subTest(defect=defect):
                self.setUp()
                self.selected()
                self.state["target"]["oses"] = ["ubuntu-latest", "windows-latest"]
                with patch("controller.require_candidate_isolation"):
                    self.controller.handle(self.state, self.revision, proposal(
                        self.state, self.revision, "repair", diagnosis="Local missing-widget readiness race.",
                        files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")}))
                state, _ = self.live()
                worker = state["worker"]
                self.github.runs[worker["id"]].update(
                    status="completed", conclusion="failure" if defect == "failed" else "success")
                for (os_name, index), files in evidence(worker, state["target"]).items():
                    if os_name == "ubuntu-latest" and index == 1:
                        if defect in {"source", "attempt", "os", "mode"}:
                            receipt = json.loads(files["receipt.json"])
                            key, value = {"source": ("sha", BASE), "attempt": ("run_attempt", 2),
                                          "os": ("os", "windows-latest"), "mode": ("mode", "project")}[defect]
                            receipt[key] = value
                            files["receipt.json"] = json.dumps(receipt).encode()
                        elif defect == "target":
                            files["iteration-1/original.trx"] = trx(method="OtherTest")
                        elif defect == "zero":
                            files["iteration-1/original.trx"] = b"<TestRun/>"
                        elif defect == "case-count":
                            files["iteration-1/original.trx"] = files["iteration-1/original.trx"].replace(
                                b'testName="WaitsForWidget"', b'testName="WaitsForWidget(other row)"')
                        elif defect == "failed":
                            files["iteration-1/original.trx"] = trx("Failed", execution="failed-verification")
                            files["iteration-1/result.json"] = b'{"exit_code":2}'
                    if defect != "missing-os" or os_name != "windows-latest":
                        self.github.artifacts[(worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
                self.invoke_controller()
                if defect in {"case-count", "failed", "review"}:
                    state, _ = self.live()
                    self.assertEqual("agent", state["phase"])
                    self.assertEqual(1, len(state["history"]))
                    run_id = state["agent"]["id"]
                    self.github.runs[run_id].update(status="completed", conclusion="success")
                    self.github.artifacts[(run_id, "repo-memory-default")] = {
                        "proposal.json": json.dumps(proposal(
                            state, state["agent"]["revision"], "finish", outcome="verified",
                            reviewed_candidate_sha=BASE if defect == "review" else state["candidate"]["sha"]
                        )).encode()}
                    with patch("controller.require_candidate_isolation") as isolation:
                        self.invoke_controller()
                    isolation.assert_not_called()
                else:
                    self.assertEqual([], self.github.state["history"])
                self.assertEqual("blocked", self.github.state["outcome"])
                self.assertEqual([], self.github.pulls)

    def test_requested_contention_is_retained_without_a_matching_hosted_failure(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "project")
        self.complete_worker(False)
        state, revision = self.live()
        request = proposal(state, revision, "repair", mode="single", diagnosis="Local readiness diagnosis.",
                           files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")})
        with self.assertRaisesRegex(ValueError, "retain required contention"):
            self.controller.handle(copy.deepcopy(state), revision, request)
        state["candidate"] = {
            "sha": "c" * 40, "paths": [SOURCE], "diagnosis": "Local readiness diagnosis.", "mode": "single"}
        state["phase"] = "candidate-ready"
        revision = self.controller.save(state, revision)
        with patch("controller.require_candidate_isolation"):
            self.controller.advance(state, revision)
        self.assertEqual("project", self.github.state["worker"]["mode"])
        self.complete_worker(False)
        state, revision = self.live()
        with self.assertRaisesRegex(ValueError, "execution.*boundary"):
            self.controller.handle(state, revision, proposal(
                state, revision, "finish", outcome="verified", reviewed_candidate_sha="c" * 40))
        self.assertEqual([], self.github.pulls)

    def test_selection_only_handoff_does_not_dispatch_or_claim_substantive_work(self):
        before = copy.deepcopy(self.github.calls)
        with self.assertRaisesRegex(ValueError, "Selection alone is not a handoff"):
            self.controller.handle(self.state, self.revision, proposal(
                self.state, self.revision, "select", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "No investigation performed."}))
        self.assertIsNone(self.github.state["target"])
        self.assertEqual(before, self.github.calls)
        self.assertEqual({}, self.github.commits)

    def test_local_contention_is_retained_across_candidate_revisions(self):
        self.selected()
        request = proposal(self.state, self.revision, "repair", mode="project", diagnosis="Local contention race.",
                           files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")})
        with patch("controller.require_candidate_isolation"):
            self.controller.handle(self.state, self.revision, request)
        self.complete_worker(True)
        state, revision = self.live()
        request = proposal(state, revision, "repair", mode="single", diagnosis="Refined local contention fix.",
                           files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReady2Async()")})
        with self.assertRaisesRegex(ValueError, "retain required contention"):
            self.controller.handle(copy.deepcopy(state), revision, request)
        del request["mode"]
        with patch("controller.require_candidate_isolation"):
            self.controller.handle(state, revision, request)
        self.assertEqual(("project", 2, 2), (
            self.github.state["worker"]["mode"], self.github.state["used"]["workers"],
            self.github.state["used"]["fixes"]))

    def test_explicit_reproduction_configuration_is_bounded_and_quarantine_scoped(self):
        self.selected()
        for fields in ({"mode": []}, {"mode": "shell"}, {"runners": 4}, {"iterations": 11},
                       {"runners": 0}, {"iterations": True}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.controller.handle(copy.deepcopy(self.state), self.revision, proposal(
                    self.state, self.revision, "reproduce", **fields))
        self.state["target"]["quarantined"] = False
        with self.assertRaisesRegex(ValueError, "quarantined"):
            self.controller.handle(self.state, self.revision, proposal(
                self.state, self.revision, "reproduce", mode="project"))
        self.assertEqual(0, self.github.state["used"]["workers"])
        self.assertEqual(0, self.github.state["used"]["runner_iterations"])
        self.assertEqual([], [call for call in self.github.calls if call[0] == "dispatch"])

    def invoke_controller(self):
        with patch.dict(os.environ, {
            "GITHUB_REPOSITORY": REPOSITORY, "ISSUE_URL": URL,
            "OPERATION": "resume", "GITHUB_EVENT_NAME": "schedule",
        }), patch("sys.argv", ["controller.py", "advance"]), \
                patch("controller.GitHub", return_value=self.github), \
                patch("controller.Controller", return_value=self.controller):
            main()

    def test_main_console_and_step_summary_distinguish_changes_waiting_and_terminal_noop(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"GITHUB_STEP_SUMMARY": str(Path(directory) / "summary.md")}):
            with patch("sys.stdout", io.StringIO()) as output:
                self.invoke_controller()
                self.invoke_controller()
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(status="completed", conclusion="failure")
                self.invoke_controller()
                revision = self.github.revision
                self.invoke_controller()
            events = [json.loads(line.removeprefix("flaky-investigation "))
                      for line in output.getvalue().splitlines()]
            self.assertTrue(events)
            self.assertTrue({"changed", "waiting", "terminal-noop"} <= {item["event"] for item in events})
            self.assertTrue(all(item["issue"] == URL and item["base_sha"] == BASE for item in events))
            self.assertTrue(all(item["checkpoint"] for item in events))
            self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", output.getvalue())
            self.assertEqual(revision, self.github.revision)
            self.assertEqual(1, len(self.github.comments))
            self.assertIn("accepted", events[-1])
            self.assertEqual(1, events[-1]["rejected_attempts"])
            self.assertIn("issuecomment-1", events[-1]["finding_url"])
            summary = (Path(directory) / "summary.md").read_text()
            self.assertIn("waiting", summary)
            self.assertIn("terminal-noop", summary)
            self.assertIn("Agent failed/cancelled", summary)

    def test_requested_worker_progress_is_observable_and_clean_completion_returns_to_agent(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "reproduce", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "Issue OS differs from agent runner."})).encode()}
        with patch("sys.stdout", io.StringIO()) as output:
            self.invoke_controller()
            self.invoke_controller()
        self.assertEqual("worker", self.github.state["phase"])
        events = [json.loads(frame.removeprefix("flaky-investigation "))
                  for frame in output.getvalue().splitlines()]
        self.assertEqual("waiting", events[-1]["event"])
        worker_events = [item for item in events if item["phase"] == "worker"]
        self.assertTrue(worker_events)
        for item in worker_events:
            self.assertEqual({
                "method": "Sample.Tests.WidgetTests.WaitsForWidget",
                "project": PROJECT, "oses": ["ubuntu-latest"],
            }, item["selection"])
            self.assertEqual([
                {"kind": "agent", "status": "checkpointed", "run_id": run_id,
                 "url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
                 "deadline_at": "2026-10-21T00:30:00+00:00"},
                {"kind": "worker", "status": "current", "run_id": self.github.state["worker"]["id"],
                 "url": f"https://github.com/{REPOSITORY}/actions/runs/{self.github.state['worker']['id']}",
                 "deadline_at": "2026-10-21T01:30:00+00:00",
                 "source_sha": BASE, "phase": "reproduce", "mode": "single", "runners": 3, "iterations": 3},
            ], item["children"])
        self.complete_worker(False)
        self.invoke_controller()
        state, _ = self.live()
        self.assertEqual("agent", state["phase"])
        self.assertEqual(2, state["used"]["models"])
        self.assertEqual(1, state["used"]["workers"])
        self.assertEqual(9, state["used"]["runner_iterations"])
        self.assertEqual(9, sum(item["passed"] for item in state["history"]))
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(state, state["agent"]["revision"], "finish",
                                                outcome="not-reproduced")).encode()}
        self.invoke_controller()
        self.assertEqual("not-reproduced", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))

    def test_main_console_keeps_model_and_remote_error_commands_as_decodable_data(self):
        payload = "##[error]legacy ##[add-mask]value\r\n::error::modern\r::add-mask::value"
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        self.github.runs[state["agent"]["id"]].update(status="completed", conclusion="success")
        self.github.state["notes"].append({
            "action": "reproduce", "explanation": payload, "evidence_read": ["Original TRX"]})
        with patch.object(self.github, "artifact", side_effect=ValueError(payload)), \
                patch("sys.stdout", io.StringIO()) as output:
            self.invoke_controller()
        frames = output.getvalue().splitlines()
        self.assertTrue(frames)
        for frame in frames:
            self.assertNotRegex(frame, r"##\[|^\s*::")
        events = [json.loads(frame.removeprefix("flaky-investigation ")) for frame in frames]
        self.assertEqual(payload, events[0]["model_reason_not_independently_established"])
        self.assertEqual("Validation/remote operation blocked: " + payload, events[-1]["reason"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual("terminal", self.github.state["phase"])

    def test_ready_checkpoint_recovery_returns_to_agent_without_another_worker(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        state, revision = self.live()
        worker = state["worker"]
        self.github.runs[worker["id"]].update(status="completed", conclusion="success")
        for (os_name, index), files in evidence(worker, state["target"]).items():
            self.github.artifacts[(worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
        self.controller.advance(state, revision)
        self.assertEqual("ready", self.github.state["phase"])
        self.invoke_controller()
        self.assertEqual("agent", self.github.state["phase"])
        self.assertEqual(1, self.github.state["used"]["models"])
        self.assertEqual(1, self.github.state["used"]["workers"])
        self.assertEqual(1, len(self.github.state["history"]))

    def test_not_reproduced_accepts_one_clean_requested_round(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(False)
        state, revision = self.live()
        self.controller.handle(state, revision, proposal(state, revision, "finish", outcome="not-reproduced"))
        self.assertEqual("not-reproduced", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.state["history"]))
        self.assertEqual(1, len(self.github.comments))

    def test_requested_extra_baseline_rounds_use_shared_budget_without_automatic_continuation(self):
        for last_round in (4, 5):
            with self.subTest(last_round=last_round):
                self.setUp()
                self.selected()
                for _ in range(3):
                    state, revision = self.live()
                    self.controller.dispatch_worker(state, revision, "reproduce", 3, 3, "single")
                    self.complete_worker(False)
                for round_number in range(4, last_round + 1):
                    state, revision = self.live()
                    self.assertEqual("ready", state["phase"])
                    self.assertEqual(round_number - 1, state["used"]["workers"])
                    self.controller.advance(state, revision)
                    state, _ = self.live()
                    self.assertEqual("agent", state["phase"])
                    self.assertEqual(round_number - 1, state["used"]["workers"])
                    run_id = state["agent"]["id"]
                    self.github.runs[run_id].update(status="completed", conclusion="success")
                    self.github.artifacts[(run_id, "repo-memory-default")] = {
                        "proposal.json": json.dumps(proposal(
                            state, state["agent"]["revision"], "reproduce",
                            explanation="Request another bounded sample after inspecting the original clean reports."
                        )).encode()}
                    self.invoke_controller()
                    state, _ = self.live()
                    self.assertEqual("worker", state["phase"])
                    self.assertEqual((round_number, 9 * round_number),
                                     (state["used"]["workers"], state["used"]["runner_iterations"]))
                    self.assertEqual(("single", BASE, CONTROL, 3, 3), (
                        state["worker"]["mode"], state["worker"]["source_sha"],
                        state["worker"]["sha"], state["worker"]["runners"], state["worker"]["iterations"]))
                    self.complete_worker(False)
                    state, _ = self.live()
                    self.assertEqual("ready", state["phase"])
                    self.assertEqual(round_number, len(state["history"]))
                    self.assertEqual(round_number, state["used"]["workers"])
                for field, value in (("failed", 1), ("source_sha", CONTROL)):
                    with self.subTest(extra_round_defect=field):
                        changed = copy.deepcopy(state)
                        changed["history"][-1][field] = value
                        with self.assertRaisesRegex(ValueError, "accepted target failure"):
                            self.controller.handle(changed, self.github.revision, proposal(
                                changed, self.github.revision, "finish", outcome="not-reproduced"))
                self.invoke_controller()
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(status="completed", conclusion="success")
                self.github.artifacts[(run_id, "repo-memory-default")] = {
                    "proposal.json": json.dumps(proposal(
                        state, state["agent"]["revision"], "finish", outcome="not-reproduced")).encode()}
                self.invoke_controller()
                self.assertEqual("not-reproduced", self.github.state["outcome"])
                self.assertEqual(last_round, self.github.state["used"]["workers"])
                self.assertEqual([], self.github.pulls)

    def test_extra_baseline_requests_cannot_exceed_existing_shared_budgets(self):
        for exhausted in ("workers", "runner_iterations", "models", "deadline"):
            with self.subTest(exhausted=exhausted):
                self.setUp()
                self.selected()
                for _ in range(3):
                    state, revision = self.live()
                    self.controller.dispatch_worker(state, revision, "reproduce", 3, 3, "single")
                    self.complete_worker(False)
                state, revision = self.live()
                if exhausted == "models":
                    state["used"]["models"] = 8
                    self.controller.save(state, revision)
                else:
                    self.controller.advance(state, revision)
                    state, revision = self.live()
                    run_id = state["agent"]["id"]
                    self.github.runs[run_id].update(status="completed", conclusion="success")
                    self.github.artifacts[(run_id, "repo-memory-default")] = {
                        "proposal.json": json.dumps(proposal(
                            state, state["agent"]["revision"], "reproduce")).encode()}
                    if exhausted == "deadline":
                        self.controller.now = lambda: NOW + timedelta(hours=13)
                    else:
                        # Reservations can outnumber accepted batches after rejected attempts.
                        state["used"][exhausted] = 5 if exhausted == "workers" else 180
                        self.controller.save(state, revision)
                self.invoke_controller()
                self.assertEqual("budget-exhausted", self.github.state["outcome"])
                self.assertIn({
                    "workers": "workers budget exhausted",
                    "runner_iterations": "runner_iterations budget exhausted",
                    "models": "model phase budget exhausted",
                    "deadline": "twelve-hour investigation budget expired",
                }[exhausted], self.github.state["explanation"])
                self.assertEqual(3, len(self.github.state["history"]))
                self.assertEqual(3, len([call for call in self.github.calls
                                         if call[0] == "dispatch" and call[1] == WORKER]))
                self.assertEqual([], self.github.pulls)

    def test_sixth_baseline_and_verification_after_five_workers_are_blocked_before_dispatch(self):
        for action in ("reproduce", "verify"):
            with self.subTest(action=action):
                self.setUp()
                self.selected()
                for _ in range(3):
                    state, revision = self.live()
                    self.controller.dispatch_worker(state, revision, "reproduce", 3, 3, "single")
                    self.complete_worker(False)
                for _ in range(2):
                    state, revision = self.live()
                    self.controller.dispatch_worker(state, revision, "reproduce", 3, 3, "single")
                    self.complete_worker(True)
                state, revision = self.live()
                if action == "verify":
                    state["candidate"] = {
                        "sha": "c" * 40, "paths": [SOURCE], "diagnosis": "Readiness defect."}
                    with self.assertRaisesRegex(ValueError, "nine target passes"):
                        self.controller.handle(copy.deepcopy(state), revision, proposal(
                            state, revision, "finish", outcome="verified", reviewed_candidate_sha="c" * 40))
                    state["phase"] = "candidate-ready"
                    self.controller.save(state, revision)
                else:
                    self.controller.advance(state, revision)
                    state, _ = self.live()
                    run_id = state["agent"]["id"]
                    self.github.runs[run_id].update(status="completed", conclusion="success")
                    self.github.artifacts[(run_id, "repo-memory-default")] = {
                        "proposal.json": json.dumps(proposal(
                            state, state["agent"]["revision"], "reproduce")).encode()}
                # Synthetic isolation admission reaches the existing shared worker
                # budget; it does not authorize production candidate execution.
                with patch("controller.require_candidate_isolation"):
                    self.invoke_controller()
                self.assertEqual("budget-exhausted", self.github.state["outcome"])
                self.assertEqual(5, self.github.state["used"]["workers"])
                self.assertEqual(45, self.github.state["used"]["runner_iterations"])
                self.assertEqual(5, len([call for call in self.github.calls
                                         if call[0] == "dispatch" and call[1] == WORKER]))
                self.assertEqual([], self.github.pulls)

    def test_main_unsupported_cli_selection_posts_one_blocked_finding_without_interrupted_resume(self):
        project = "tests/Aspire.Cli.EndToEnd.Tests/Aspire.Cli.EndToEnd.Tests.csproj"
        source = "tests/Aspire.Cli.EndToEnd.Tests/WidgetTests.cs"
        self.github.files.update({project: "<Project/>", source: CODE})
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "reproduce",
                selection={**SELECT, "source": source, "project": project},
                local_precheck={"status": "skipped", "reason": "Prebuilt CLI images unavailable."})).encode()}
        with patch("sys.stdout", io.StringIO()) as output, patch("sys.stderr", io.StringIO()) as errors:
            self.invoke_controller()
            self.assertEqual("terminal", self.github.state["phase"])
            self.assertEqual("blocked", self.github.state["outcome"])
            self.assertEqual(1, len(self.github.comments))
            self.assertIn("candidate-aware prebuilt images", self.github.comments[0]["body"])
            self.assertEqual("reproduce", self.github.state["rejected_attempts"][0]["action"])
            before = copy.deepcopy((self.github.state, self.github.revision, self.github.comments, self.github.refs))
            self.invoke_controller()
            self.assertEqual(before, (self.github.state, self.github.revision, self.github.comments, self.github.refs))
        self.assertEqual("", errors.getvalue())
        self.assertEqual(0, self.github.state["used"]["workers"])
        self.assertEqual([], [call for call in self.github.calls if call[0] == "dispatch" and call[1] == WORKER])
        events = [json.loads(frame.removeprefix("flaky-investigation "))
                  for frame in output.getvalue().splitlines()]
        self.assertEqual("terminal-noop", events[-1]["event"])

    def test_not_reproduced_cannot_contradict_accepted_failure_source_or_candidate(self):
        for os_count in (1, 2, 3):
            oses = ["ubuntu-latest", "windows-latest", "macos-latest"][:os_count]
            for quarantined in (True, False):
                cases = [
                    ("valid", None), ("failed", 1), ("source_sha", CONTROL),
                    ("candidate", {"sha": CONTROL}), ("target", None),
                ]
                for field, value in cases:
                    with self.subTest(oses=oses, quarantined=quarantined, field=field):
                        self.setUp()
                        self.selected()
                        self.state["target"].update(oses=oses, quarantined=quarantined)
                        self.state["history"] = [
                            {"id": 100 + index, "phase": "reproduce", "source_sha": BASE,
                             "failed": 0, "passed": 9 * os_count, "actual_tests": 9 * os_count,
                             "runners": 3, "iterations": 3,
                             "mode": "project" if index == 2 and quarantined else "single"}
                            for index in range(3)]
                        if field in {"target", "candidate"}:
                            self.state[field] = value
                        elif field != "valid":
                            self.state["history"][-1][field] = value
                        request = proposal(self.state, self.revision, "finish", outcome="not-reproduced")
                        if field == "valid":
                            self.controller.handle(self.state, self.revision, request)
                            self.assertEqual("not-reproduced", self.github.state["outcome"])
                            self.assertEqual(1, len(self.github.comments))
                        else:
                            with self.assertRaisesRegex(ValueError, "accepted target failure"):
                                self.controller.handle(self.state, self.revision, request)
                            self.assertEqual(self.revision, self.github.revision)
                            self.assertEqual([], self.github.comments)
                        self.assertEqual([], self.github.pulls)

    def test_preparation_retains_report_dates_without_claiming_historical_logs_collected(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        self.github.comments.append({
            "body": "0/215 failures in the last 30 days.",
            "created_at": "2026-05-13T00:00:00Z", "updated_at": "2026-05-13T00:00:00Z",
            "html_url": URL + "#issuecomment-1",
        })
        with tempfile.TemporaryDirectory() as temporary, patch("controller.ROOT", Path(temporary)), \
                patch("controller.stage_source", return_value=Path(temporary) / "source"), \
                patch.dict(os.environ, {"GITHUB_RUN_ID": str(state["agent"]["id"])}):
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "global.json").write_text(json.dumps({"sdk": {"version": "11.0.100"}}))
            prepare(self.github, URL, state["agent"]["revision"])
            context = json.loads((Path(temporary) / "context.json").read_text())
            self.assertEqual(BASE, context["source"]["sha"])
            self.assertEqual("2026-05-13T00:00:00Z", context["comments"][0]["updated_at"])
            self.assertFalse(context["historical_evidence"]["logs_collected"])
            self.assertIn("not current", context["historical_evidence"]["limitation"])

    @patch("controller.require_candidate_isolation")
    def test_substantive_local_work_survives_external_waits_into_fresh_agent_context(self, isolation):
        local = {
            "status": "completed", "reason": "Matching environment; local results are preliminary.",
            "os": "Linux", "commands": ["./dotnet.sh test --project " + PROJECT],
            "observed_output": "Total: 3, Passed: 3. No matching local failure.",
        }
        selection_reason = (
            "Observed: shared readiness fixture; historical report dated May 13. "
            "Hypothesis: startup exhausts the shared timeout. "
            "Outstanding: hosted matching failure and original logs before proposing a fix.")
        inspected = [f"{BASE}:{SOURCE}: await widget.WaitAsync();", "Historical logs unavailable; dated report only."]
        def complete_agent(action, explanation, **fields):
            state, _ = self.live()
            run_id = state["agent"]["id"]
            self.github.runs[run_id].update(status="completed", conclusion="success")
            self.github.artifacts[(run_id, "repo-memory-default")] = {
                "proposal.json": json.dumps(proposal(
                    state, state["agent"]["revision"], action, explanation=explanation,
                    evidence_read=inspected, **fields)).encode()}
            self.invoke_controller()
        def context():
            state, _ = self.live()
            with tempfile.TemporaryDirectory() as temporary, patch("controller.ROOT", Path(temporary)), \
                    patch("controller.stage_source", return_value=Path(temporary) / "source"), \
                    patch.dict(os.environ, {"GITHUB_RUN_ID": str(state["agent"]["id"])}):
                source = Path(temporary) / "source"
                source.mkdir()
                (source / "global.json").write_text(json.dumps({"sdk": {"version": "11.0.100"}}))
                prepare(self.github, URL, state["agent"]["revision"])
                return json.loads((Path(temporary) / "context.json").read_text())
        self.controller.advance(self.state, self.revision)
        complete_agent("reproduce", selection_reason, selection=SELECT, local_precheck=local)
        self.complete_worker(True)
        self.invoke_controller()
        first = context()
        self.assertEqual(BASE, first["source"]["sha"])
        self.assertEqual(selection_reason, first["checkpoint"]["notes"][-1]["explanation"])
        self.assertEqual(inspected, first["checkpoint"]["notes"][-1]["evidence_read"])
        self.assertEqual(local, first["checkpoint"]["local_precheck"])
        repaired = CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")
        verification = {**local, "observed_output": "Total: 3, Passed: 3 after the local candidate change."}
        repair_reason = (
            "Observed: hosted target passes and matching failure; readiness wait uses shared startup budget. "
            "Hypothesis: separate readiness wait removes that race. "
            "Outstanding: exact candidate hosted verification, then final full source review.")
        complete_agent("repair", repair_reason, diagnosis="Readiness race; wait for readiness separately.",
                       files={SOURCE: repaired}, local_precheck=verification)
        self.complete_worker(False)
        self.invoke_controller()
        final = context()
        self.assertEqual(BASE, final["source"]["sha"])
        self.assertEqual(self.github.state["candidate"]["sha"], final["checkpoint"]["candidate"]["sha"])
        self.assertEqual({SOURCE: repaired}, final["candidate_files"])
        self.assertEqual(repair_reason, final["checkpoint"]["notes"][-1]["explanation"])
        self.assertEqual(inspected, final["checkpoint"]["notes"][-1]["evidence_read"])
        self.assertEqual(verification, final["checkpoint"]["notes"][-1]["local_precheck"])
        self.assertEqual(local, final["checkpoint"]["notes"][0]["local_precheck"])
        self.assertEqual(local, final["checkpoint"]["local_precheck"])
        self.assertEqual(3, self.github.state["used"]["models"])
        self.assertEqual([], self.github.comments)
        self.assertEqual([], self.github.pulls)

    def test_invalid_repair_local_work_is_rejected_before_candidate_publication(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        request = proposal(
            state, revision, "repair", diagnosis="Readiness race.",
            files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
            local_precheck={"status": "completed", "reason": "Claimed local verification without commands."})
        before = copy.deepcopy((self.github.state, self.github.revision, self.github.refs, self.github.commits))
        with self.assertRaisesRegex(ValueError, "commands"):
            self.controller.handle(state, revision, request)
        self.assertEqual(before, (self.github.state, self.github.revision, self.github.refs, self.github.commits))
        self.assertEqual([], self.github.comments)
        self.assertEqual([], self.github.pulls)

    def test_rejected_worker_posts_short_analysis_with_exact_evidence_and_resumes_without_duplicates(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        self.complete_worker(False)
        state, revision = self.live()
        self.controller.dispatch_worker(state, revision, "reproduce", 3, 3, "single")
        state, _ = self.live()
        worker = state["worker"]
        self.github.runs[worker["id"]].update(status="completed", conclusion="failure")
        for (os_name, index), files in evidence(worker, state["target"]).items():
            if index == 1:
                files["iteration-1/unrelated.trx"] = trx("Failed", execution="unrelated", method="Other")
            self.github.artifacts[(worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
        self.invoke_controller()
        state, revision = self.live()
        self.assertEqual("terminal", state["phase"])
        self.assertEqual("blocked", state["outcome"])
        self.assertEqual(1, len(state["history"]))
        self.assertEqual(1, state["history"][0]["passed"])
        self.assertEqual(worker["id"], state["rejected_attempts"][0]["run_id"])
        self.assertEqual(1, len(self.github.comments))
        body = self.github.comments[0]["body"]
        summary, details = body.split("<details>", 1)
        self.assertIn("Why no verified fix", summary)
        self.assertIn("Unrelated project test failed", summary)
        self.assertIn("**Tried:**", summary)
        self.assertIn("**Next:**", summary)
        self.assertIn("1 target passes, 0 target failures", summary)
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{worker['id']}", details)
        self.assertIn("Rejected attempts (not accepted test counts)", details)
        self.assertLess(len(summary), 1600)
        golden = Path(__file__).with_name("fixtures") / "rejected-worker-comment.txt"
        self.assertEqual(golden.read_text(encoding="utf-8"), body + "\n")
        self.assertEqual([], self.github.pulls)
        before = copy.deepcopy((self.github.state, self.github.comments, self.github.refs))
        self.invoke_controller()
        self.assertEqual(revision, self.github.revision)
        self.assertEqual(before, (self.github.state, self.github.comments, self.github.refs))

    def test_failed_or_malformed_agent_uses_trusted_fallback_not_prior_model_analysis(self):
        for defect in ("failure", "missing", "malformed"):
            with self.subTest(defect=defect):
                self.setUp()
                stale_analysis = {
                    "why_no_fix": "A stale model diagnosis must not be reused.",
                    "attempted": ["A stale attempt."], "next_action": "A stale recommendation.",
                }
                self.state["analysis"] = stale_analysis
                self.controller.advance(self.state, self.revision)
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(
                    status="completed", conclusion="failure" if defect == "failure" else "success")
                if defect == "malformed":
                    self.github.artifacts[(run_id, "repo-memory-default")] = {"proposal.json": b"{"}
                self.invoke_controller()
                self.assertEqual("blocked", self.github.state["outcome"])
                body = self.github.comments[0]["body"]
                self.assertIn("Trusted controller fallback", body)
                self.assertIn("No valid terminal model analysis was received", body)
                self.assertIsNone(self.github.state["analysis"])
                self.assertNotIn(stale_analysis["why_no_fix"], body)
                self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", body)
                self.assertEqual([], self.github.pulls)

    def test_terminal_model_analysis_is_bounded_and_distinguished_from_observed_counts(self):
        analysis = {
            "why_no_fix": "The target passed; a readiness race remains only a hypothesis.",
            "attempted": ["Read the test and original reports.", "Repeated the exact target."],
            "next_action": "Reproduce on the issue's Windows environment.",
        }
        request = proposal(self.state, self.revision, "finish", outcome="blocked", analysis=analysis)
        validate_proposal(self.state, self.revision, request)
        self.controller.handle(self.state, self.revision, request)
        body = self.github.comments[0]["body"]
        self.assertIn("Model analysis (not independently established)", body)
        self.assertIn(analysis["why_no_fix"], body)
        self.assertIn(analysis["next_action"], body)
        self.assertEqual(analysis, self.github.state["analysis"])
        self.assertIn("**Observed:** No accepted test executions.", body)
        self.assertEqual([], self.github.pulls)

    def test_signed_artifact_error_posts_one_safe_terminal_finding_and_resume_is_noop(self):
        for parameter in ("sig", "token", "access_token"):
            with self.subTest(parameter=parameter):
                self.setUp()
                self.controller.advance(self.state, self.revision)
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(status="completed", conclusion="success")
                error = GitHubAPIError(1, ["gh", "api"], stderr=(
                    f'Get "https://artifacts.example/b?{parameter}=abc123&se=2026": '
                    "TLS handshake timeout").encode())
                with patch.object(self.github, "artifact", side_effect=error):
                    self.invoke_controller()
                self.assertEqual("terminal", self.github.state["phase"])
                self.assertEqual("blocked", self.github.state["outcome"])
                self.assertEqual(1, len(self.github.comments))
                body = self.github.comments[0]["body"]
                self.assertIn("TLS handshake timeout", body)
                self.assertNotIn("abc123", body)
                self.assertNotIn(f"?{parameter}=", body)
                self.assertEqual([], self.github.pulls)
                before = copy.deepcopy((self.github.state, self.github.comments, self.github.refs))
                revision = self.github.revision
                self.invoke_controller()
                self.assertEqual(revision, self.github.revision)
                self.assertEqual(before, (self.github.state, self.github.comments, self.github.refs))

    def test_terminal_analysis_rejects_wall_of_text_and_invalid_shapes(self):
        valid = {"why_no_fix": "Not reproduced.", "attempted": ["Read reports."], "next_action": "Try the issue OS."}
        for field, value in (
            ("why_no_fix", "x" * 601), ("why_no_fix", "first\nsecond"),
            ("attempted", []), ("attempted", ["step"] * 4),
            ("attempted", ["x" * 241]), ("next_action", "x" * 301),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_proposal(self.state, self.revision, proposal(
                    self.state, self.revision, "finish", outcome="blocked", analysis={**valid, field: value}))

    def test_legacy_terminal_proposal_and_checkpoint_render_without_inventing_analysis(self):
        for outcome in ("ambiguous", "blocked", "budget-exhausted"):
            with self.subTest(outcome=outcome):
                self.setUp()
                model_claim = "An unverified model hypothesis about the cause."
                self.controller.handle(self.state, self.revision, proposal(
                    self.state, self.revision, "finish", outcome=outcome, explanation=model_claim))
                body = self.github.comments[0]["body"]
                summary, details = body.split("<details>", 1)
                self.assertIn("Trusted controller fallback", summary)
                self.assertNotIn(model_claim, summary)
                self.assertIn("Legacy model explanation (not independently established)", details)
                self.assertIn(model_claim, details)
                self.assertIn("No accepted test executions", summary)
                self.assertNotIn("Model analysis (not independently established)", body)

    def test_budget_cancellation_and_uncertain_intent_have_actionable_fallbacks(self):
        for reason in ("time", "models", "cancel"):
            with self.subTest(reason=reason):
                self.setUp()
                if reason == "time":
                    self.state["created_at"] = (NOW - timedelta(hours=12)).isoformat()
                elif reason == "models":
                    self.state["used"]["models"] = 8
                self.controller.advance(self.state, self.revision, cancel=reason == "cancel")
                body = self.github.comments[0]["body"]
                self.assertIn("**Why no verified fix:**", body)
                self.assertIn("**Next:**", body)
                self.assertIn("Trusted controller fallback", body)
                self.assertIn("<summary>Evidence</summary>", body)
                self.assertEqual([], self.github.pulls)

    def test_invalid_candidate_and_exhausted_repairs_retain_rejection_without_publishing_fix(self):
        for defect in ("candidate", "budget"):
            with self.subTest(defect=defect):
                self.setUp()
                self.selected()
                self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
                self.complete_worker(True)
                state, revision = self.live()
                if defect == "budget":
                    state["used"]["fixes"] = 2
                self.controller.advance(state, revision)
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(status="completed", conclusion="success")
                path = SOURCE if defect == "budget" else "eng/unsafe.cs"
                self.github.artifacts[(run_id, "repo-memory-default")] = {
                    "proposal.json": json.dumps(proposal(
                        state, state["agent"]["revision"], "repair", files={
                            path: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                        diagnosis="Readiness race.")).encode()}
                self.invoke_controller()
                self.assertEqual([], self.github.pulls)
                self.assertEqual({}, self.github.commits)
                self.assertEqual("repair", self.github.state["rejected_attempts"][0]["action"])
                self.assertIn("Rejected attempts", self.github.comments[0]["body"])
                self.assertIn("**Next:**", self.github.comments[0]["body"])

    def test_missing_reports_and_worker_errors_keep_rejected_run_out_of_accepted_counts(self):
        for defect in ("missing-trx", "build", "cancelled"):
            with self.subTest(defect=defect):
                self.setUp()
                self.selected()
                self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
                state, _ = self.live()
                worker = state["worker"]
                self.github.runs[worker["id"]].update(
                    status="completed", conclusion="cancelled" if defect == "cancelled" else "failure")
                files = evidence(worker, state["target"])[("ubuntu-latest", 1)]
                if defect == "missing-trx":
                    del files["iteration-1/original.trx"]
                elif defect == "build":
                    files["iteration-1/result.json"] = b'{"exit_code":1}'
                self.github.artifacts[(worker["id"], "flaky-evidence-ubuntu-latest-1")] = files
                self.invoke_controller()
                self.assertEqual([], self.github.state["history"])
                self.assertEqual(worker["id"], self.github.state["rejected_attempts"][0]["run_id"])
                self.assertIn("**Observed:** No accepted test executions.", self.github.comments[0]["body"])
                self.assertEqual([], self.github.pulls)

    def test_intent_recovery_retains_exact_failed_phase_and_source_without_dispatch(self):
        self.selected()
        self.github.fail_dispatch = True
        with self.assertRaises(ValueError):
            self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        state, revision = self.live()
        Controller(self.github, lambda: NOW + timedelta(minutes=6)).advance(state, revision)
        rejected = self.github.state["rejected_attempts"][0]
        self.assertEqual("worker-intent", rejected["phase"])
        self.assertEqual(CONTROL, rejected["sha"])
        self.assertEqual(BASE, rejected["source_sha"])
        self.assertNotIn("run_id", rejected)
        self.assertIn("No run receipt was persisted", self.github.comments[0]["body"])
        summary = self.github.comments[0]["body"].split("<details>", 1)[0]
        self.assertIn("**Rejected evidence:** No run receipt was persisted.", summary)
        self.assertEqual(1, len([call for call in self.github.calls if call[0] == "dispatch"]))

    def test_checkpointed_run_identities_include_accepted_worker_and_separate_rejected_agent(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        accepted_id = state["history"][0]["id"]
        self.controller.advance(state, revision)
        state, _ = self.live()
        rejected_id = state["agent"]["id"]
        self.github.runs[rejected_id].update(status="completed", conclusion="failure")
        self.invoke_controller()
        self.assertEqual(1, len(self.github.comments))
        body = self.github.comments[0]["body"]
        summary, details = body.split("<details>", 1)
        self.assertIn("8 target passes, 1 target failures; 9 actual tests in accepted evidence", summary)
        checkpointed = details.split("### Latest checkpointed run identities (not test results)", 1)[1]
        checkpointed, rejected = checkpointed.split("### Rejected attempts (not accepted test counts)", 1)
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{accepted_id}", checkpointed)
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{rejected_id}", rejected)
        self.assertNotIn(f"https://github.com/{REPOSITORY}/actions/runs/{accepted_id}", rejected)
        self.assertEqual([rejected_id], [item["run_id"] for item in self.github.state["rejected_attempts"]])

    def test_all_reservation_budgets_produce_budget_findings_not_invented_model_analysis(self):
        for budget in ("workers", "runner_iterations", "fixes"):
            with self.subTest(budget=budget):
                self.setUp()
                self.selected()
                if budget == "fixes":
                    self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
                    self.complete_worker(True)
                state, revision = self.live()
                state["used"][budget] = {"workers": 5, "runner_iterations": 180, "fixes": 2}[budget]
                self.controller.dispatch_agent(state, revision)
                state, _ = self.live()
                run_id = state["agent"]["id"]
                self.github.runs[run_id].update(status="completed", conclusion="success")
                action = "repair" if budget == "fixes" else "reproduce"
                fields = {"files": {SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                          "diagnosis": "Readiness race."} if budget == "fixes" else {}
                self.github.artifacts[(run_id, "repo-memory-default")] = {
                    "proposal.json": json.dumps(proposal(state, state["agent"]["revision"], action, **fields)).encode()}
                self.invoke_controller()
                self.assertEqual("budget-exhausted", self.github.state["outcome"])
                self.assertIn("Trusted controller fallback", self.github.comments[0]["body"])
                self.assertEqual([], self.github.pulls)

    def test_timeout_budget_links_uncollected_checkpoint_run_without_claiming_results(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        run_id = state["agent"]["id"]
        state["created_at"] = (NOW - timedelta(hours=12)).isoformat()
        self.controller.advance(state, revision)
        self.assertEqual("cancelling", self.github.state["phase"])
        self.assertEqual([], self.github.comments)
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        self.controller.advance(state, revision)
        body = self.github.comments[0]["body"]
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", body)
        self.assertIn("Latest checkpointed run identities (not test results)", body)
        self.assertIn("**Observed:** No accepted test executions.", body)

    @patch("controller.require_candidate_isolation")
    def test_lost_pr_response_never_posts_definite_no_pr_or_retries_publication(self, isolation):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        self.controller.handle(state, revision, proposal(state, revision, "repair", files={
            SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")}, diagnosis="Readiness race."))
        self.complete_worker(False)
        state, revision = self.live()
        self.controller.advance(state, revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(state, state["agent"]["revision"], "finish",
                                                outcome="verified",
                                                reviewed_candidate_sha=state["candidate"]["sha"])).encode()}
        api = self.github.api
        def lose_response(endpoint, body=None, method=None):
            result = api(endpoint, body, method)
            if endpoint == "pulls":
                raise subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Connection lost")
            return result
        with patch.object(self.github, "api", side_effect=lose_response), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke_controller()
            self.assertEqual("publication-intent", self.github.state["phase"])
            with patch("sys.stdout", io.StringIO()) as output:
                self.invoke_controller()
                self.invoke_controller()
            events = [json.loads(frame.removeprefix("flaky-investigation "))
                      for frame in output.getvalue().splitlines()]
            self.assertEqual("terminal-noop", events[-1]["event"])
            final_events = [item for item in events if item["event"] in {"changed", "terminal-noop"}]
            self.assertEqual(2, len(final_events))
            for item in final_events:
                self.assertEqual("blocked", item["outcome"])
                self.assertTrue(item["publication_uncertain"])
                self.assertEqual(
                    "Publication unconfirmed; inspect remote objects and checkpoint before retrying.", item["reason"])
        self.assertTrue(self.github.state["publication_uncertain"])
        self.assertEqual(1, len(self.github.pulls))
        self.assertEqual([], self.github.comments)
        body = findings(self.github.state, "No publication receipt.")
        self.assertIn("publication unconfirmed", body)
        self.assertNotIn("no verified fix PR", body)
        self.assertIn("a remote PR or comment may already exist", body)

    def test_untrusted_detail_cannot_escape_evidence_and_report_stays_bounded(self):
        self.state.update(
            outcome="blocked", phase="publication-intent", explanation="Malformed model proposal",
            local_precheck={"observed_output": "</pre></details>" * 1000})
        self.state["notes"].append({
            "action": "finish", "run_id": 100, "explanation": "<" * 8000,
            "evidence_read": ["</pre></details>" * 50] * 30,
        })
        self.state["rejected_attempts"] = [
            {"phase": "worker", "reason": "<" * 4000, "run_id": index} for index in range(1, 6)]
        self.controller.finish(self.state, self.revision, "blocked", "No valid proposal.")
        body = self.github.comments[0]["body"]
        self.assertEqual(1, body.count("</details>"))
        self.assertLess(len(body), 60000)
        self.assertIn("Showing the latest 3 of 5 rejected attempts", body)
        self.assertIn("[truncated]", body)

    def test_failed_publication_reconciliation_does_not_retry_a_possible_write(self):
        self.state.update(phase="publication-intent", outcome="verified", explanation="Reviewed candidate.")
        revision = self.controller.save(self.state, self.revision)
        error = subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Read API unavailable")
        with patch.object(self.github, "pages", side_effect=error), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(subprocess.CalledProcessError):
                self.invoke_controller()
        self.assertEqual("publication-intent", self.github.state["phase"])
        self.assertEqual("verified", self.github.state["outcome"])
        self.assertEqual(revision, self.github.revision)
        self.assertEqual([], self.github.comments)
        self.assertEqual([], self.github.pulls)

    def test_cancel_during_uncertain_publication_reconciles_without_claiming_no_pr(self):
        self.state.update(phase="publication-intent", outcome="verified", explanation="Reviewed candidate.")
        self.revision = self.controller.save(self.state, self.revision)
        with patch("sys.stderr", io.StringIO()):
            self.controller.advance(self.state, self.revision, cancel=True)
        self.assertTrue(self.github.state.get("publication_uncertain"))
        self.assertEqual([], self.github.comments)
        self.assertEqual([], self.github.pulls)

    def selected(self):
        self.state["target"] = selection(SELECT, ISSUE, self.github.files)
        self.state["repair_scope"] = ["tests/Sample.Tests/"]
        self.revision = self.controller.save(self.state, self.revision)

    def complete_worker(self, failure):
        state, revision = self.live()
        worker = state["worker"]
        self.github.runs[worker["id"]].update(status="completed", conclusion="failure" if failure else "success")
        for (os_name, index), files in evidence(worker, state["target"], failure).items():
            self.github.artifacts[(worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
        self.controller.advance(state, revision)

    def test_issue_url_only_selection_has_no_target_catalog(self):
        self.controller.handle(self.state, self.revision, proposal(
            self.state, self.revision, "reproduce", selection=SELECT,
            local_precheck={"status": "skipped", "reason": "Windows issue; Linux runner lacks matching OS."}))
        state, _ = self.live()
        self.assertEqual("WaitsForWidget", state["target"]["method"])
        self.assertEqual(["tests/Sample.Tests/"], state["repair_scope"])
        self.assertEqual("worker", state["phase"])

    def test_duplicate_enrollment_preserves_existing_revision(self):
        self.assertEqual(self.revision, self.controller.start(URL, CONTROL)[1])
        self.assertEqual(1, len(self.github.snapshots))

    def test_bootstrap_identity_is_persisted_and_cannot_change(self):
        other = Controller(FakeGitHub(), lambda: NOW)
        state, _ = other.start(URL, CONTROL, WORKER, BASE, "c" * 40)
        self.assertEqual(WORKER, state["agent_workflow"])
        self.assertEqual(BASE, state["agent_sha"])
        self.assertEqual("c" * 40, state["worker_sha"])
        with self.assertRaisesRegex(ValueError, "Runtime identities"):
            self.controller.start(URL, BASE)

    def test_checkpoint_nonforced_update_rejects_concurrent_writer(self):
        state2 = copy.deepcopy(self.state)
        self.controller.save(self.state, self.revision)
        with self.assertRaisesRegex(ValueError, "Nonfastforward"):
            self.controller.save(state2, self.revision)

    def test_missing_dispatch_receipt_reports_blocker_but_keeps_cleanup_live_without_redispatch(self):
        self.github.fail_dispatch = True
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        self.assertEqual("agent-intent", state["phase"])
        Controller(self.github, lambda: NOW + timedelta(minutes=6)).advance(state, revision)
        self.assertEqual("blocked", self.github.state["outcome"])
        self.assertEqual("cleanup-pending", self.github.state["phase"])
        self.assertEqual(1, len([call for call in self.github.calls if call[0] == "dispatch"]))

    def interrupted_dispatch(self, kind):
        if kind == "worker":
            self.selected()
        self.github.interrupt_dispatch = True
        with self.assertRaises(SystemExit) as stopped:
            if kind == "worker":
                self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
            else:
                self.controller.dispatch_agent(self.state, self.revision)
        self.assertEqual(137, stopped.exception.code)
        self.github.interrupt_dispatch = False
        state, revision = self.live()
        self.assertEqual(kind + "-intent", state["phase"])
        self.assertNotIn("id", state[kind])
        return state, revision, next(iter(self.github.runs))

    def test_receipt_loss_recovers_exact_correlated_child_then_cancels_once_and_confirms_stop(self):
        for kind in ("agent", "worker"):
            with self.subTest(kind=kind):
                self.setUp()
                state, intent, run_id = self.interrupted_dispatch(kind)
                budgets = copy.deepcopy(state["used"])
                Controller(self.github, lambda: NOW + timedelta(seconds=20)).advance(state, intent)
                state, revision = self.live()
                self.assertEqual(kind, state["phase"])
                self.assertEqual(run_id, state[kind].get("id"))
                if kind == "agent":
                    self.assertEqual(intent, state[kind]["revision"])
                Controller(self.github, lambda: NOW + timedelta(minutes=1)).advance(state, revision, cancel=True)
                state, revision = self.live()
                self.assertEqual("cancelling", state["phase"])
                Controller(self.github, lambda: NOW + timedelta(minutes=2)).advance(state, revision)
                self.github.runs[run_id].update(status="completed", conclusion="cancelled")
                state, revision = self.live()
                Controller(self.github, lambda: NOW + timedelta(minutes=3)).advance(state, revision)
                self.assertEqual("terminal", self.github.state["phase"])
                self.assertTrue(self.github.state["cancellation"]["confirmed"])
                self.assertEqual("cancelled", self.github.state["outcome"])
                self.assertEqual(budgets, self.github.state["used"])
                self.assertEqual([], self.github.state["history"])
                self.assertEqual(1, len(self.github.comments))
                self.assertEqual(1, len([c for c in self.github.calls if c[0] == "dispatch"]))
                self.assertEqual([f"actions/runs/{run_id}/cancel"],
                                 [c[0] for c in self.github.calls if c[0].endswith("/cancel")])
                for snapshot in self.github.snapshots.values():
                    validate_checkpoint(snapshot, 123)

    def test_receipt_lookup_zero_then_eventual_visibility_keeps_original_intent_authority(self):
        self.controller = Controller(self.github, lambda: NOW.replace(microsecond=590960))
        state, intent, run_id = self.interrupted_dispatch("agent")
        self.github.run_listing = {"total_count": 0, "workflow_runs": []}
        Controller(self.github, lambda: NOW + timedelta(seconds=20)).advance(state, intent)
        state, revision = self.live()
        self.assertEqual("agent-intent", state["phase"])
        self.assertEqual(intent, state["dispatch_recovery"]["revision"])
        self.assertEqual([], self.github.comments)
        self.github.run_listing = None
        Controller(self.github, lambda: NOW + timedelta(seconds=40)).advance(state, revision)
        state, _ = self.live()
        self.assertEqual("agent", state["phase"])
        self.assertEqual(run_id, state["agent"]["id"])
        self.assertEqual(intent, state["agent"]["revision"])
        lookup = [call[0] for call in self.github.calls if "/runs?" in call[0]][-1]
        self.assertEqual({
            "branch": ["ankj/flaky-test-123-agent-1"], "event": ["workflow_dispatch"], "head_sha": [CONTROL],
            "created": ["2026-10-21T00:00:00+00:00..2026-10-21T00:00:40+00:00"], "per_page": ["100"],
        }, parse_qs(urlsplit(lookup).query))
        validate_checkpoint(state, 123)

    def test_receipt_lookup_missing_after_grace_keeps_cleanup_live_until_late_child_stops(self):
        state, intent, run_id = self.interrupted_dispatch("worker")
        budgets = copy.deepcopy(state["used"])
        self.github.run_listing = {"total_count": 0, "workflow_runs": []}
        later = Controller(self.github, lambda: NOW + timedelta(minutes=6))
        later.advance(state, intent)
        state, revision = self.live()
        self.assertEqual("cleanup-pending", state["phase"])
        self.assertFalse(state["dispatch_recovery"]["confirmed"])
        self.assertEqual(1, len(self.github.comments))
        later.advance(state, revision)
        self.github.run_listing = None
        state, revision = self.live()
        later.advance(state, revision)
        self.assertEqual("cancelling", self.github.state["phase"])
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        later.advance(state, revision)
        self.assertEqual("terminal", self.github.state["phase"])
        self.assertEqual(budgets, self.github.state["used"])
        self.assertEqual([], self.github.state["history"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual(1, len([c for c in self.github.calls if c[0] == "dispatch"]))

    def test_receipt_lookup_ambiguity_foreign_metadata_and_failure_never_end_cleanup(self):
        for defect in ("duplicate", "two-runs", "foreign-sha", "foreign-repo", "foreign-head",
                       "foreign-workflow", "foreign-ref", "foreign-event", "rerun", "missing-title",
                       "wrong-intent", "wrong-issue", "old-created", "future-created",
                       "truncated", "malformed", "lookup-error", "changed-direct-run"):
            with self.subTest(defect=defect):
                self.setUp()
                state, intent, run_id = self.interrupted_dispatch("worker")
                item = copy.deepcopy(self.github.runs[run_id])
                rows = [item]
                changes = {
                    "foreign-sha": {"head_sha": BASE},
                    "foreign-repo": {"repository": {"full_name": "foreign/aspire"}},
                    "foreign-head": {"head_repository": {"full_name": "foreign/aspire"}},
                    "foreign-workflow": {"path": ".github/workflows/other.yml"},
                    "foreign-ref": {"head_branch": "ankj/flaky-test-124-run-1"},
                    "foreign-event": {"event": "push"}, "rerun": {"run_attempt": 2},
                    "missing-title": {"display_title": "Reproduce Flaky Tests"},
                    "wrong-intent": {"display_title": f"Flaky reproduction {URL} [{BASE}]"},
                    "wrong-issue": {"display_title": f"Flaky reproduction {URL}4 [{intent}]"},
                    "old-created": {"created_at": (NOW - timedelta(seconds=1)).isoformat()},
                    "future-created": {"created_at": (NOW + timedelta(hours=1)).isoformat()},
                }
                item.update(changes.get(defect, {}))
                if defect in {"duplicate", "two-runs"}:
                    other = copy.deepcopy(item)
                    if defect == "two-runs":
                        other["id"] += 1
                        self.github.runs[other["id"]] = other
                    rows.append(other)
                self.github.run_listing = {"total_count": 101 if defect == "truncated" else len(rows),
                                           "workflow_runs": rows}
                if defect == "malformed":
                    self.github.run_listing["workflow_runs"] = [[]]
                api = self.github.api
                def transport(endpoint, body=None, method=None):
                    if defect == "lookup-error" and "/runs?" in endpoint:
                        raise subprocess.TimeoutExpired(["gh", "api"], 120)
                    result = api(endpoint, body, method)
                    if defect == "changed-direct-run" and endpoint.startswith("actions/runs/"):
                        result["id"] += 1
                    return result
                for minute in (1, 7):
                    state, revision = self.live()
                    self.controller = Controller(self.github, lambda: NOW + timedelta(minutes=minute))
                    with patch.object(self.github, "api", side_effect=transport):
                        self.invoke_controller()
                    self.assertEqual("cleanup-pending", self.github.state["phase"])
                    self.assertNotIn("id", self.github.state["worker"])
                    validate_checkpoint(self.github.state, 123)
                self.assertEqual(1, len(self.github.comments))
                self.assertFalse(any(c[0].endswith("/cancel") for c in self.github.calls))
                self.assertEqual([], self.github.state["history"])
                self.assertEqual(1, len([c for c in self.github.calls if c[0] == "dispatch"]))

    def test_receipt_recovery_cancel_and_expiry_do_not_accept_results_or_reset_deadlines(self):
        for kind in ("agent", "worker"):
            for operation in ("cancel", "child-expiry", "overall-expiry"):
                with self.subTest(kind=kind, operation=operation):
                    self.setUp()
                    state, intent, run_id = self.interrupted_dispatch(kind)
                    deadline = state[kind]["deadline_at"]
                    delay = 1 if operation == "cancel" else 91 if operation == "child-expiry" else 721
                    later = Controller(self.github, lambda: NOW + timedelta(minutes=delay))
                    later.advance(state, intent, cancel=operation == "cancel")
                    state, revision = self.live()
                    self.assertEqual("cancelling", state["phase"])
                    self.assertEqual(deadline, state[kind]["deadline_at"])
                    self.assertEqual(run_id, state["cancellation"]["run_id"])
                    self.github.runs[run_id].update(status="completed", conclusion="cancelled")
                    later.advance(state, revision)
                    self.assertEqual("terminal", self.github.state["phase"])
                    self.assertEqual("cancelled" if operation == "cancel" else "budget-exhausted",
                                     self.github.state["outcome"])
                    self.assertEqual([], self.github.state["history"])
                    self.assertEqual(1, len(self.github.comments))
                    self.assertEqual(1, len([c for c in self.github.calls if c[0] == "dispatch"]))

    def test_receipt_recovery_cannot_adopt_a_child_configuration_changed_after_the_original_intent(self):
        state, intent, _ = self.interrupted_dispatch("worker")
        self.github.run_listing = {"total_count": 0, "workflow_runs": []}
        self.controller.advance(state, intent)
        state, revision = self.live()
        state["worker"]["source_sha"] = "c" * 40
        revision = self.controller.save(state, revision)
        self.github.run_listing = None
        self.invoke_controller()
        self.assertEqual("cleanup-pending", self.github.state["phase"])
        self.assertNotIn("id", self.github.state["worker"])
        self.assertFalse(any(c[0].endswith("/cancel") for c in self.github.calls))

    def test_dispatch_recovery_present_shapes_are_validated_before_the_sweep(self):
        state, intent, _ = self.interrupted_dispatch("worker")
        self.github.run_listing = {"total_count": 0, "workflow_runs": []}
        self.controller.advance(state, intent)
        state, _ = self.live()
        validate_checkpoint(state, 123)
        for key, value in (("kind", []), ("outcome", []), ("confirmed", 1), ("confirmed", True),
                           ("revision", "unknown"),
                           ("requested_at", "today"), ("observed_ids", [True]), ("reason", [])):
            with self.subTest(key=key):
                malformed = copy.deepcopy(state)
                malformed["dispatch_recovery"][key] = value
                with self.assertRaises(ValueError):
                    validate_checkpoint(malformed, 123)
        for key in state["dispatch_recovery"]:
            with self.subTest(missing=key):
                malformed = copy.deepcopy(state)
                del malformed["dispatch_recovery"][key]
                with self.assertRaises(ValueError):
                    validate_checkpoint(malformed, 123)

    def test_ambiguous_dispatches_never_select_a_newest_run_and_cleanup_waits_for_every_observed_id(self):
        state, intent, run_id = self.interrupted_dispatch("worker")
        second = copy.deepcopy(self.github.runs[run_id])
        second["id"] += 1
        self.github.runs[second["id"]] = second
        self.controller.advance(state, intent)
        state, revision = self.live()
        self.assertEqual("cleanup-pending", state["phase"])
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        self.github.run_listing = {"total_count": 1, "workflow_runs": [self.github.runs[run_id]]}
        self.controller.advance(state, revision)
        self.assertEqual("cleanup-pending", self.github.state["phase"])
        self.github.runs[second["id"]].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        self.controller.advance(state, revision)
        self.assertEqual("terminal", self.github.state["phase"])
        self.assertTrue(self.github.state["dispatch_recovery"]["confirmed"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual([], self.github.state["history"])
        self.assertFalse(any(c[0].endswith("/cancel") for c in self.github.calls))

    def test_receipt_recovery_never_retries_uncertain_finding_after_eventual_owned_cleanup(self):
        for delivered in (False, True):
            with self.subTest(delivered=delivered):
                self.setUp()
                state, intent, run_id = self.interrupted_dispatch("worker")
                self.github.run_listing = {"total_count": 0, "workflow_runs": []}
                later = Controller(self.github, lambda: NOW + timedelta(minutes=6))
                api = self.github.api
                posts = []
                def transport(endpoint, body=None, method=None):
                    if endpoint == "issues/123/comments" and body is not None:
                        posts.append(body)
                        if len(posts) == 1:
                            if delivered:
                                api(endpoint, body, method)
                            raise SystemExit(137)
                    return api(endpoint, body, method)
                with patch.object(self.github, "api", side_effect=transport):
                    with self.assertRaises(SystemExit):
                        later.advance(state, intent)
                    state, revision = self.live()
                    self.assertEqual("publication-intent", state["phase"])
                    later.advance(state, revision)
                    state, revision = self.live()
                    self.assertEqual("cleanup-pending", state["phase"])
                    self.github.run_listing = None
                    later.advance(state, revision)
                    self.github.runs[run_id].update(status="completed", conclusion="cancelled")
                    state, revision = self.live()
                    later.advance(state, revision)
                self.assertEqual("terminal", self.github.state["phase"])
                self.assertEqual(1, len(posts))
                self.assertEqual(1 if delivered else 0, len(self.github.comments))
                self.assertEqual(not delivered, self.github.state.get("publication_uncertain", False))
                self.assertEqual([], self.github.state["history"])

    def test_recovered_id_and_desired_cancel_survive_interruption_before_cancel_write(self):
        state, intent, run_id = self.interrupted_dispatch("worker")
        save = self.controller.save
        def interrupt(state, revision, reason=None):
            result = save(state, revision, reason)
            if state["phase"] == "worker":
                raise SystemExit(137)
            return result
        with patch.object(self.controller, "save", side_effect=interrupt), self.assertRaises(SystemExit):
            self.controller.advance(state, intent, cancel=True)
        state, revision = self.live()
        self.assertEqual(run_id, state["worker"]["id"])
        self.assertEqual("cancelled", state["dispatch_recovery"]["outcome"])
        self.assertFalse(any(c[0].endswith("/cancel") for c in self.github.calls))
        Controller(self.github, lambda: NOW + timedelta(minutes=1)).advance(state, revision)
        self.assertEqual("cancelling", self.github.state["phase"])
        self.assertEqual([f"actions/runs/{run_id}/cancel"],
                         [c[0] for c in self.github.calls if c[0].endswith("/cancel")])

    def test_missing_receipt_cancel_remains_desired_outcome_even_after_overall_expiry(self):
        state, intent, run_id = self.interrupted_dispatch("agent")
        self.github.run_listing = {"total_count": 0, "workflow_runs": []}
        self.controller.advance(state, intent, cancel=True)
        state, revision = self.live()
        self.assertEqual("cleanup-pending", state["phase"])
        much_later = Controller(self.github, lambda: NOW + timedelta(hours=13))
        much_later.advance(state, revision)
        self.assertEqual("cancelled", self.github.state["dispatch_recovery"]["outcome"])
        self.github.run_listing = None
        state, revision = self.live()
        much_later.advance(state, revision)
        self.assertEqual("cancelling", self.github.state["phase"])
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        much_later.advance(state, revision)
        self.assertEqual("cancelled", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual([], self.github.state["history"])

    def test_recovered_completed_agent_observed_late_keeps_original_intent_and_job_deadline(self):
        state, intent, run_id = self.interrupted_dispatch("agent")
        self.github.runs[run_id].update(status="completed", conclusion="success")
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(state, intent, "finish", outcome="not-flaky")).encode()}
        jobs = [{
            "id": 777, "run_id": run_id, "run_attempt": 1, "head_sha": CONTROL, "status": "completed",
            "completed_at": (NOW + timedelta(minutes=10)).isoformat(),
        }]
        pages = self.github.pages
        with patch.object(self.github, "pages", side_effect=lambda endpoint, key=None:
                          jobs if endpoint.endswith("/attempts/1/jobs") else pages(endpoint, key)):
            Controller(self.github, lambda: NOW + timedelta(minutes=31)).advance(state, intent)
        self.assertEqual("not-flaky", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual(1, len([c for c in self.github.calls if c[0] == "dispatch"]))
        self.assertFalse(any(c[0].endswith("/cancel") for c in self.github.calls))

    def test_worker_ref_failure_resumes_to_blocked_without_worker_or_retry(self):
        self.selected()
        error = subprocess.CalledProcessError(
            1, ["gh", "api", "git/refs"], stderr=b"gh: Resource not accessible by integration (HTTP 403)")
        with patch.object(self.github, "create_ref", side_effect=error) as create_ref:
            with self.assertRaises(subprocess.CalledProcessError):
                self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
            state, revision = self.live()
            self.assertEqual("worker-intent", state["phase"])
            self.assertIsNone(state["worker"].get("id"))
            Controller(self.github, lambda: NOW + timedelta(minutes=6)).advance(state, revision)
            create_ref.assert_called_once_with(state["worker"]["branch"], CONTROL)
        self.assertEqual("cleanup-pending", self.github.state["phase"])
        self.assertEqual("blocked", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual([], self.github.pulls)
        self.assertFalse(any(call[0] == "dispatch" for call in self.github.calls))

    def test_lost_callback_resumes_exact_owned_worker_once(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        self.complete_worker(False)
        state, revision = self.live()
        self.assertEqual(1, len(state["history"]))
        self.assertEqual(1, state["history"][0]["passed"])
        self.controller.advance(state, revision)
        self.assertEqual(1, len(self.github.state["history"]))
        self.assertEqual("agent", self.github.state["phase"])
        self.assertEqual(2, len(self.github.runs))

    def test_fixed_worker_dispatch_never_writes_workflow_or_candidate_trees(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        state, _ = self.live()
        worker = state["worker"]
        self.assertEqual(CONTROL, self.github.refs[worker["branch"]])
        self.assertEqual(BASE, worker["source_sha"])
        self.assertEqual({}, self.github.commits)
        dispatch = self.github.calls[-1]
        self.assertEqual(WORKER, dispatch[1])
        self.assertEqual({"investigation_issue_url", "investigation_revision"}, set(dispatch[3]))
        config = configuration(self.github, URL, dispatch[3]["investigation_revision"], worker["id"], CONTROL)
        self.assertEqual(BASE, config["source_sha"])
        self.assertEqual(CONTROL, config["trusted_sha"])
        matrix = json.loads(setup_outputs(config)["matrix"])
        self.assertEqual([{"os": "ubuntu-latest", "index": index} for index in range(1, 4)], matrix["include"])

    def test_fixed_worker_rejects_foreign_source_run_and_stale_checkpoint(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        state, _ = self.live()
        intent = self.github.calls[-1][3]["investigation_revision"]
        for run_id, trusted_sha in ((999, CONTROL), (state["worker"]["id"], BASE)):
            with self.subTest(run_id=run_id, trusted_sha=trusted_sha), self.assertRaises(ValueError):
                configuration(self.github, URL, intent, run_id, trusted_sha)
        self.github.state["phase"] = "ready"
        with self.assertRaisesRegex(ValueError, "Stale"):
            configuration(self.github, URL, intent, state["worker"]["id"], CONTROL)

    def test_cli_e2e_does_not_verify_an_unchanged_prebuilt_runtime(self):
        self.selected()
        self.state["target"]["project"] = "tests/Aspire.Cli.EndToEnd.Tests/Aspire.Cli.EndToEnd.Tests.csproj"
        with self.assertRaisesRegex(ValueError, "candidate-aware"):
            self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        self.assertEqual(0, self.state["used"]["workers"])
        self.assertFalse(any(call[0] == "dispatch" for call in self.github.calls))

    def test_incomplete_agent_does_not_consume_early_proposal(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        self.github.artifacts[(state["agent"]["id"], "repo-memory-default")] = {
            "proposal.json": b'{"action":"repair"}'}
        self.assertEqual(revision, self.controller.advance(state, revision))
        self.assertEqual("agent", self.github.state["phase"])

    def test_completed_agent_consumes_immutable_intent_proposal_not_live_checkpoint_revision(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        self.assertNotEqual(revision, state["agent"]["revision"])
        self.github.runs[state["agent"]["id"]].update(status="completed", conclusion="success")
        self.github.artifacts[(state["agent"]["id"], "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "reproduce", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "Runner environment unavailable."})).encode()}
        self.controller.advance(state, revision)
        self.assertEqual("worker", self.github.state["phase"])
        self.assertEqual("reproduce", self.github.state["notes"][-1]["action"])
        self.assertEqual("WaitsForWidget", self.github.state["target"]["method"])

    def test_failed_model_requires_blocked_findings_not_candidate_publication(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        self.github.runs[state["agent"]["id"]].update(status="completed", conclusion="failure")
        with self.assertRaisesRegex(ValueError, "Agent failed"):
            self.controller.proposal(state)
        self.assertEqual([], self.github.pulls)

    def test_proposal_requires_exact_checkpoint_and_issue(self):
        for field, value in (("state_revision", BASE), ("issue_url", URL + "9"), ("action", "shell")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_proposal(self.state, self.revision, proposal(
                    self.state, self.revision, "reproduce", **{field: value}) if field != "action" else
                    proposal(self.state, self.revision, value))

    def test_cancel_only_exact_owned_run_and_never_foreign_sha(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id]["head_sha"] = BASE
        with self.assertRaisesRegex(ValueError, "identity"):
            self.controller.advance(state, revision, cancel=True)
        self.assertFalse(any(call[0].endswith("/cancel") for call in self.github.calls))
        self.github.runs[run_id]["head_sha"] = CONTROL
        self.controller.advance(state, revision, cancel=True)
        self.assertEqual([f"actions/runs/{run_id}/cancel"],
                         [call[0] for call in self.github.calls if call[0].endswith("/cancel")])
        self.assertEqual("cancelling", self.github.state["phase"])
        self.assertEqual([], self.github.comments)
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        self.controller.advance(state, revision)
        self.assertEqual("cancelled", self.github.state["outcome"])

    def test_child_deadline_survives_resumes_and_waits_for_confirmed_cancellation(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        run_id = state["agent"]["id"]
        deadline = state["agent"].get("deadline_at")
        self.assertIsNotNone(deadline, "child needs an absolute durable deadline")
        self.assertEqual(revision, self.controller.advance(state, revision))
        later = Controller(self.github, lambda: NOW + timedelta(minutes=31))
        later.advance(state, revision)
        state, revision = self.live()
        self.assertEqual("cancelling", state["phase"])
        self.assertEqual([], self.github.comments)
        later.advance(state, revision)
        self.assertEqual(1, len([call for call in self.github.calls if call[0].endswith("/cancel")]))
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        later.advance(state, revision)
        self.assertEqual("budget-exhausted", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertTrue(self.github.state["cancellation"]["confirmed"])
        state, revision = self.live()
        later.advance(state, revision)
        self.assertEqual(1, len(self.github.comments))

    def test_uncertain_cancel_response_reconciles_exact_run_without_retry_or_false_success(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        run_id = state["agent"]["id"]
        api = self.github.api
        def lose_response(endpoint, body=None, method=None):
            result = api(endpoint, body, method)
            if endpoint.endswith("/cancel"):
                raise subprocess.TimeoutExpired(["gh", "api"], 120)
            return result
        with patch.object(self.github, "api", side_effect=lose_response):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.controller.advance(state, revision, cancel=True)
            state, revision = self.live()
            self.controller.advance(state, revision)
        self.assertEqual("cancelling", self.github.state["phase"])
        self.assertEqual([], self.github.comments)
        self.assertEqual(1, len([call for call in self.github.calls if call[0].endswith("/cancel")]))
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        self.controller.advance(state, revision)
        self.assertEqual("cancelled", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))

    def test_worker_deadline_does_not_cancel_healthy_worker_and_unconfirmed_stop_is_honest(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        state, revision = self.live()
        run_id = state["worker"]["id"]
        earlier = Controller(self.github, lambda: NOW + timedelta(minutes=31))
        self.assertEqual(revision, earlier.advance(state, revision))
        self.assertFalse(any(call[0].endswith("/cancel") for call in self.github.calls))
        expired = Controller(self.github, lambda: NOW + timedelta(minutes=91))
        expired.advance(state, revision)
        state, revision = self.live()
        self.assertEqual("cancelling", state["phase"])
        self.assertEqual(run_id, state["cancellation"]["run_id"])
        late = Controller(self.github, lambda: NOW + timedelta(minutes=122))
        late.advance(state, revision)
        self.assertEqual("blocked", self.github.state["outcome"])
        self.assertFalse(self.github.state["cancellation"]["confirmed"])
        self.assertIn("may still be active", self.github.state["explanation"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual(1, len([call for call in self.github.calls if call[0].endswith("/cancel")]))
        state, revision = self.live()
        self.assertEqual("cleanup-pending", state["phase"])
        self.assertEqual(revision, late.advance(state, revision))
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        state, revision = self.live()
        late.advance(state, revision)
        self.assertEqual("terminal", self.github.state["phase"])
        self.assertTrue(self.github.state["cancellation"]["confirmed"])
        self.assertEqual("budget-exhausted", self.github.state["cancellation"]["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual(1, len(self.github.runs))

    def test_recovery_continues_other_issues_when_one_checkpoint_is_tainted(self):
        healthy = copy.deepcopy(self.state)
        healthy.update(issue=124, issue_url=URL.replace("/123", "/124"))
        load = self.github.load
        def load_issue(issue, revision=None):
            if issue == 123:
                raise ValueError("Malformed checkpoint budgets")
            return load(issue, revision)
        self.github.state = healthy
        with patch.dict(os.environ, {
            "GITHUB_REPOSITORY": REPOSITORY, "ISSUE_URL": "", "OPERATION": "resume",
            "GITHUB_EVENT_NAME": "workflow_run",
        }), patch("sys.argv", ["controller.py", "advance"]), \
                patch("controller.GitHub", return_value=self.github), \
                patch("controller.Controller", return_value=self.controller), \
                patch.object(self.github, "load", side_effect=load_issue), \
                patch.object(self.github, "optional", return_value=[
                    {"ref": "refs/heads/ankj/flaky-test-123-state"},
                    {"ref": "refs/heads/ankj/flaky-test-124-state"}]), \
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()) as errors:
            with self.assertRaisesRegex(ValueError, r"issues \[123\]"):
                main()
        self.assertIn("Malformed checkpoint budgets", errors.getvalue())
        self.assertEqual("agent", self.github.state["phase"])
        self.assertEqual(124, self.github.state["issue"])
        self.assertEqual([], self.github.comments)
        self.assertEqual(1, len([call for call in self.github.calls if call[0] == "dispatch"]))

    def test_malformed_present_objects_do_not_escape_or_starve_the_recovery_sweep(self):
        for field, value in (
            ("agent", []), ("worker", []), ("target", []), ("candidate", []),
            ("cancellation", []), ("history", [[]]), ("notes", [[]]), ("transitions", [[]]),
        ):
            with self.subTest(field=field):
                self.setUp()
                malformed = {**copy.deepcopy(self.state), field: value}
                healthy = copy.deepcopy(self.state)
                healthy.update(issue=124, issue_url=URL.replace("/123", "/124"))
                self.github.state = healthy
                load = self.github.load
                def load_issue(issue, revision=None):
                    return (malformed, self.revision) if issue == 123 else load(issue, revision)
                with patch.dict(os.environ, {
                    "GITHUB_REPOSITORY": REPOSITORY, "ISSUE_URL": "", "OPERATION": "resume",
                    "GITHUB_EVENT_NAME": "workflow_run",
                }), patch("sys.argv", ["controller.py", "advance"]), \
                        patch("controller.GitHub", return_value=self.github), \
                        patch("controller.Controller", return_value=self.controller), \
                        patch.object(self.github, "load", side_effect=load_issue), \
                        patch.object(self.github, "optional", return_value=[
                            {"ref": "refs/heads/ankj/flaky-test-123-state"},
                            {"ref": "refs/heads/ankj/flaky-test-124-state"}]), \
                        patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    with self.assertRaisesRegex(ValueError, r"issues \[123\]"):
                        main()
                self.assertEqual(124, self.github.state["issue"])
                self.assertEqual("agent", self.github.state["phase"])
                self.assertEqual(1, len(self.github.runs))

    def test_completed_child_observed_late_uses_exact_attempt_job_completion_not_updated_at(self):
        self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        run_id = state["agent"]["id"]
        self.github.runs[run_id].update(
            status="completed", conclusion="success", updated_at=(NOW + timedelta(minutes=31)).isoformat())
        self.github.artifacts[(run_id, "repo-memory-default")] = {
            "proposal.json": json.dumps(proposal(
                state, state["agent"]["revision"], "finish", outcome="not-flaky")).encode()}
        jobs = [{
            "id": 777, "run_id": run_id, "run_attempt": 1, "head_sha": CONTROL, "status": "completed",
            "started_at": NOW.isoformat(), "completed_at": (NOW + timedelta(minutes=10)).isoformat(),
        }]
        later = Controller(self.github, lambda: NOW + timedelta(minutes=31))
        original = self.github.pages
        with patch.object(self.github, "pages", wraps=self.github.pages) as pages:
            pages.side_effect = lambda endpoint, key=None: jobs if endpoint.endswith("/attempts/1/jobs") else original(
                endpoint, key)
            later.advance(state, revision)
        self.assertEqual("not-flaky", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertFalse(any(call[0].endswith("/cancel") for call in self.github.calls))

    def test_late_child_requires_all_exact_attempt_job_timestamps_and_cannot_extend_overall_budget(self):
        for defect, outcome in (
            ("late-job", "budget-exhausted"), ("at-deadline", "budget-exhausted"),
            ("unknown-time", "blocked"), ("foreign-attempt", "blocked"), ("foreign-source", "blocked"),
            ("unfinished-job", "blocked"), ("no-jobs", "blocked"), ("future-time", "blocked"),
            ("overall-expired", "budget-exhausted"),
        ):
            with self.subTest(defect=defect):
                self.setUp()
                self.controller.advance(self.state, self.revision)
                state, _ = self.live()
                owned = state["agent"]
                self.github.runs[owned["id"]].update(status="completed", conclusion="success")
                jobs = [{
                    "id": 777, "run_id": owned["id"], "run_attempt": 1, "head_sha": CONTROL,
                    "status": "completed", "completed_at": (NOW + timedelta(minutes=10)).isoformat(),
                }]
                if defect in {"late-job", "at-deadline", "future-time"}:
                    jobs.append({**jobs[0], "id": 778, "completed_at": (
                        NOW + timedelta(minutes={
                            "at-deadline": 30, "late-job": 32, "future-time": 34,
                        }[defect])).isoformat()})
                elif defect == "unknown-time":
                    jobs[0]["completed_at"] = None
                elif defect == "foreign-attempt":
                    jobs[0]["run_attempt"] = 2
                elif defect == "foreign-source":
                    jobs[0]["head_sha"] = BASE
                elif defect == "unfinished-job":
                    jobs[0]["status"] = "in_progress"
                elif defect == "no-jobs":
                    jobs = []
                original = self.github.pages
                self.controller = Controller(self.github, lambda: NOW + timedelta(
                    hours=13) if defect == "overall-expired" else NOW + timedelta(minutes=33))
                with patch.object(self.github, "pages", side_effect=lambda endpoint, key=None:
                                  jobs if endpoint.endswith("/attempts/1/jobs") else original(endpoint, key)), \
                        patch("sys.stdout", io.StringIO()):
                    self.invoke_controller()
                self.assertEqual(outcome, self.github.state["outcome"])
                self.assertEqual([], self.github.state["notes"])
                self.assertEqual([], self.github.state["history"])
                self.assertEqual(1, len(self.github.runs))

    def test_timely_completed_worker_observed_late_is_counted_without_an_extra_model(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        state, revision = self.live()
        owned = state["worker"]
        self.github.runs[owned["id"]].update(status="completed", conclusion="success")
        for (os_name, index), files in evidence(owned, state["target"]).items():
            self.github.artifacts[(owned["id"], f"flaky-evidence-{os_name}-{index}")] = files
        jobs = [{
            "id": 777, "run_id": owned["id"], "run_attempt": 1, "head_sha": CONTROL,
            "status": "completed", "completed_at": (NOW + timedelta(minutes=10)).isoformat(),
        }]
        later = Controller(self.github, lambda: NOW + timedelta(minutes=91))
        with patch.object(self.github, "pages", return_value=jobs):
            later.advance(state, revision)
        self.assertEqual(9, self.github.state["history"][0]["passed"])
        self.assertEqual(0, self.github.state["used"]["models"])
        self.assertEqual(1, self.github.state["used"]["workers"])
        self.assertEqual("ready", self.github.state["phase"])

    def test_complete_live_checkpoint_shapes_and_inactive_objects_are_validated(self):
        self.controller.handle(self.state, self.revision, proposal(
            self.state, self.revision, "reproduce", selection=SELECT,
            local_precheck={"status": "skipped", "reason": "Target OS unavailable."}))
        self.complete_worker(True)
        state, revision = self.live()
        self.controller.advance(state, revision)
        for snapshot in self.github.snapshots.values():
            validate_checkpoint(snapshot, 123)
        state, _ = self.live()
        for keys, value in (
            (("agent", "id"), []), (("worker", "mode"), []), (("target", "oses"), [[]]),
            (("target", "method"), []), (("target", "project"), "tests/X\nEVIL=value/Y.csproj"),
            (("history", 0, "cases"), [[]]), (("history", 0, "cases", 0, "test_id"), []),
            (("history", 0, "passed"), []), (("history", 0, "errors"), [[]]),
            (("notes", 0, "action"), []), (("notes", 0, "explanation"), []),
            (("notes", 0, "evidence_read"), [[]]), (("local_precheck",), []),
            (("transitions", 0, "at"), []), (("rejected_attempts",), [[]]), (("repair_scope",), [[]]),
        ):
            with self.subTest(keys=keys):
                malformed = copy.deepcopy(state)
                parent = malformed
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.assertRaises(ValueError):
                    validate_checkpoint(malformed, 123)

    def test_controller_read_failure_does_not_abandon_an_active_owned_child(self):
        self.controller.advance(self.state, self.revision)
        state, _ = self.live()
        run_id = state["agent"]["id"]
        api = self.github.api
        failures = [True]
        def fail_first_read(endpoint, body=None, method=None):
            if endpoint == f"actions/runs/{run_id}" and failures:
                failures.pop()
                raise GitHubAPIError(1, ["gh"], stderr=b"HTTP 503")
            return api(endpoint, body, method)
        with patch.object(self.github, "api", side_effect=fail_first_read):
            self.invoke_controller()
        self.assertEqual("cancelling", self.github.state["phase"])
        self.assertEqual([], self.github.comments)
        self.github.runs[run_id].update(status="completed", conclusion="cancelled")
        self.invoke_controller()
        self.assertEqual("blocked", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.comments))
        self.assertIn("HTTP 503", self.github.comments[0]["body"])
        self.assertTrue(self.github.state["cancellation"]["confirmed"])

    def test_expired_and_model_budget_have_finite_terminal_findings(self):
        for exhausted in ("time", "models"):
            with self.subTest(exhausted=exhausted):
                github = FakeGitHub()
                controller = Controller(github, lambda: NOW)
                state, revision = controller.start(URL, CONTROL)
                if exhausted == "time":
                    state["created_at"] = (NOW - timedelta(hours=12)).isoformat()
                else:
                    state["used"]["models"] = 8
                controller.advance(state, revision)
                self.assertEqual("budget-exhausted", github.state["outcome"])

    def test_budget_reservation_does_not_allow_overcommit_or_boolean(self):
        self.state["used"]["runner_iterations"] = 179
        for count in (2, True, -1):
            with self.subTest(count=count), self.assertRaises(ValueError):
                reserve(self.state, "runner_iterations", count)
        self.assertEqual(179, self.state["used"]["runner_iterations"])

    def test_all_failed_matching_baseline_reaches_repair_and_final_gates_without_a_prior_pass(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        state, revision = self.live()
        worker = state["worker"]
        self.github.runs[worker["id"]].update(status="completed", conclusion="failure")
        for (os_name, index), files in evidence(worker, state["target"]).items():
            for iteration in range(1, worker["iterations"] + 1):
                prefix = f"iteration-{iteration}/"
                files[prefix + "original.trx"] = trx("Failed", f"{index}-{iteration}")
                files[prefix + "result.json"] = b'{"exit_code":2}'
            self.github.artifacts[(worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
        self.controller.advance(state, revision)
        state, revision = self.live()
        self.assertEqual((0, 9), (state["history"][0]["passed"], state["history"][0]["failed"]))
        request = proposal(state, revision, "repair", files={
            SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
            diagnosis="Reproduced Missing widget on every observed attempt; readiness was not awaited.")
        with self.assertRaisesRegex(ValueError, "execution.*boundary"):
            self.controller.handle(state, revision, request)
        self.assertEqual({}, self.github.commits)
        state, revision = self.live()
        with patch("controller.require_candidate_isolation"):
            self.controller.handle(state, revision, request)
            self.complete_worker(False)
        state, revision = self.live()
        candidate_sha = state["candidate"]["sha"]
        self.assertEqual((9, 0), (state["history"][-1]["passed"], state["history"][-1]["failed"]))
        for defect in ("mode", "source", "review"):
            with self.subTest(defect=defect):
                changed = copy.deepcopy(state)
                reviewed = candidate_sha
                if defect == "mode":
                    changed["history"][-1]["mode"] = "project"
                elif defect == "source":
                    changed["history"][-1]["source_sha"] = BASE
                else:
                    reviewed = BASE
                with patch("controller.require_candidate_isolation") as isolation, \
                        self.assertRaisesRegex(ValueError, "reproduced mode|exact candidate"):
                    self.controller.handle(changed, revision, proposal(
                        changed, revision, "finish", outcome="verified", reviewed_candidate_sha=reviewed))
                isolation.assert_not_called()
        finish = proposal(state, revision, "finish", outcome="verified", reviewed_candidate_sha=candidate_sha)
        with self.assertRaisesRegex(ValueError, "execution.*boundary"):
            self.controller.handle(state, revision, finish)
        self.assertEqual([], self.github.pulls)
        state, revision = self.live()
        # Synthetic isolation admission tests orchestration only, not production
        # authorization, hosted review or execution.
        with patch("controller.require_candidate_isolation"):
            self.controller.handle(state, revision, finish)
        self.assertEqual("verified", self.github.state["outcome"])
        self.assertEqual(1, len(self.github.pulls))
        self.assertEqual(candidate_sha, self.github.pulls[0]["head"]["sha"])
        self.assertTrue(self.github.pulls[0]["draft"])

    def test_local_first_candidate_cannot_bypass_production_isolation(self):
        self.selected()
        request = proposal(self.state, self.revision, "repair",
                           files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                           diagnosis="Observed missing-widget error and confirmed readiness race.")
        with self.assertRaisesRegex(ValueError, "execution.*boundary"):
            self.controller.handle(self.state, self.revision, request)
        self.assertEqual({}, self.github.commits)

    def test_clean_or_legacy_evidence_does_not_bypass_production_isolation(self):
        for defect in ("clean", "legacy", "source"):
            with self.subTest(defect=defect):
                self.setUp()
                self.selected()
                batch = {
                    "phase": "reproduce", "source_sha": BASE, "passed": 1, "failed": 1,
                    "id": 88, "sha": CONTROL, "runners": 3, "iterations": 3, "mode": "single",
                    "actual_tests": 2, "runner_iterations": 2, "errors": ["Missing widget"],
                    "cases": [
                        {"os": "ubuntu-latest", "name": "row-a", "test_id": "test", "passed": 0, "failed": 1},
                        {"os": "ubuntu-latest", "name": "row-a", "test_id": "test", "passed": 1, "failed": 0},
                    ],
                }
                if defect == "clean":
                    batch.update(passed=2, failed=0)
                    batch["cases"][0].update(passed=1, failed=0)
                elif defect == "legacy":
                    del batch["cases"]
                else:
                    batch["source_sha"] = CONTROL
                self.state["history"] = [batch]
                request = proposal(self.state, self.revision, "repair", files={
                    SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                    diagnosis="Unproven readiness hypothesis.")
                with self.assertRaisesRegex(ValueError, "execution.*boundary"):
                    self.controller.handle(self.state, self.revision, request)
                self.assertEqual({}, self.github.commits)
                self.assertEqual([], self.github.pulls)

    def test_verification_requires_nine_passes_for_the_exact_reproduced_case_and_os(self):
        for defect in ("case", "os", "count", "definition", "legacy", "legacy-id"):
            with self.subTest(defect=defect), patch("controller.require_candidate_isolation") as isolation:
                self.setUp()
                self.selected()
                self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
                self.complete_worker(True)
                state, revision = self.live()
                state["history"][0].update(passed=0, failed=9)
                state["history"][0]["cases"][0].update(passed=0, failed=9)
                state["candidate"] = {"sha": "c" * 40, "paths": [SOURCE], "diagnosis": "Readiness race"}
                verified = copy.deepcopy(state["history"][0])
                verified.update(phase="verify", source_sha="c" * 40, passed=9, failed=0,
                                cases=[{"os": "ubuntu-latest", "name": "WaitsForWidget", "test_id": "test",
                                        "passed": 9, "failed": 0}])
                if defect == "case":
                    verified["cases"][0]["name"] = "Different theory row"
                elif defect == "os":
                    verified["cases"][0]["os"] = "windows-latest"
                elif defect == "count":
                    verified["cases"][0]["passed"] = 8
                elif defect == "definition":
                    verified["cases"][0]["test_id"] = "different-definition"
                elif defect == "legacy-id":
                    del verified["cases"][0]["test_id"]
                else:
                    del verified["cases"]
                state["history"].append(verified)
                with self.assertRaisesRegex(ValueError, "each reproduced case and OS"):
                    self.controller.handle(state, revision, proposal(
                        state, revision, "finish", outcome="verified", reviewed_candidate_sha="c" * 40))
                isolation.assert_not_called()
                self.assertEqual([], self.github.pulls)
                self.assertEqual([], self.github.comments)

    @patch("controller.require_candidate_isolation")
    def test_checkpointed_candidate_resumes_verification_without_republishing_source(self, isolation):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        request = proposal(state, revision, "repair", files={
            SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
            diagnosis="Confirmed readiness race; use the existing structured readiness helper.")
        with patch.object(self.controller, "dispatch_worker", side_effect=RuntimeError("interrupted controller")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.controller.handle(state, revision, request)
        state, revision = self.live()
        self.assertEqual("candidate-ready", state["phase"])
        candidate_sha = state["candidate"]["sha"]
        self.controller.advance(state, revision)
        self.assertEqual("worker", self.github.state["phase"])
        self.assertEqual(candidate_sha, self.github.state["worker"]["source_sha"])
        self.assertEqual(1, self.github.state["used"]["fixes"])

    @patch("controller.require_candidate_isolation")
    def test_real_failed_candidate_then_verified_revision_produces_one_clean_draft(self, isolation):
        # Exercise publication orchestration with a synthetic approved boundary.
        # This is not evidence that production candidate execution is enabled.
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        for attempt, failure in ((1, True), (2, False)):
            request = proposal(state, revision, "repair", files={
                SOURCE: CODE.replace("widget.WaitAsync()", f"widget.WaitReady{attempt}Async()")},
                diagnosis="Missing widget: readiness observation races with startup. Use structured readiness.")
            self.controller.handle(state, revision, request)
            self.complete_worker(failure)
            state, revision = self.live()
        candidate = state["candidate"]
        self.controller.handle(state, revision, proposal(
            state, revision, "finish", outcome="verified", reviewed_candidate_sha=candidate["sha"]))
        self.assertEqual("terminal", self.github.state["phase"])
        self.assertEqual(1, len(self.github.pulls))
        pr = self.github.pulls[0]
        self.assertTrue(pr["draft"])
        self.assertEqual(candidate["sha"], pr["head"]["sha"])
        self.assertEqual({SOURCE}, set(self.github.commits[candidate["sha"]]))
        self.assertIn("## Checklist", pr["body"])
        self.assertIn("[automated]", self.github.comments[0]["body"])
        self.assertIn("Model explanation and evidence references (not independently established)",
                      self.github.comments[0]["body"])
        self.assertNotIn("Legacy model explanation", self.github.comments[0]["body"])
        state, revision = self.live()
        self.controller.advance(state, revision)
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual(2, state["used"]["fixes"])

    def test_production_repair_and_verification_are_blocked_without_execution_isolation(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 3, 3, "single")
        self.complete_worker(True)
        state, revision = self.live()
        state["candidate"] = {"sha": "c" * 40, "paths": [SOURCE], "diagnosis": "Readiness hypothesis"}
        runs = len(self.github.runs)
        for action in ("repair", "verify"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "execution.*boundary"):
                if action == "repair":
                    self.controller.handle(state, revision, proposal(state, revision, "repair", files={
                        SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                        diagnosis="Readiness hypothesis."))
                else:
                    self.controller.dispatch_worker(state, revision, "verify", 3, 3, "single")
        self.assertEqual({}, self.github.commits)
        self.assertEqual(runs, len(self.github.runs))
        self.assertEqual([], self.github.pulls)

    def test_impersonated_comment_marker_does_not_suppress_bot_finding(self):
        self.github.comments.append({
            "body": f"<!-- aspire-flaky-investigation:123:{BASE} -->",
            "user": {"login": "someone", "type": "User"},
        })
        self.controller.finish(self.state, self.revision, "not-reproduced", "No target failure.")
        self.assertEqual(2, len(self.github.comments))

    def test_comment_written_before_controller_interrupt_is_reconciled_without_duplicate(self):
        self.state.update(phase="publication-intent", outcome="blocked", explanation="No evidence.")
        self.github.comments.append({
            "body": f"<!-- aspire-flaky-investigation:123:{BASE} -->",
            "user": {"login": "github-actions[bot]", "type": "Bot"},
        })
        self.controller.advance(self.state, self.revision)
        self.assertEqual(1, len(self.github.comments))
        self.assertEqual("terminal", self.github.state["phase"])

    def test_uncertain_publication_without_matching_comment_is_not_repeated(self):
        self.state.update(phase="publication-intent", outcome="blocked", explanation="No evidence.")
        with patch("sys.stderr", io.StringIO()) as stderr:
            self.controller.advance(self.state, self.revision)
        self.assertIn("Uncertain findings publication", stderr.getvalue())
        self.assertEqual([], self.github.comments)
        self.assertEqual("terminal", self.github.state["phase"])
        self.assertTrue(self.github.state["publication_uncertain"])


class BoundaryTests(unittest.TestCase):
    def test_prepared_evidence_records_sdk_mismatch_exact_sources_and_real_linked_log_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / Path(SOURCE).parent).mkdir(parents=True)
            (source / SOURCE).write_text(CODE)
            (source / "global.json").write_text(json.dumps({"sdk": {"version": "11.0.100-rc.2"}}))
            issue = {**ISSUE, "body": "Missing widget\nhttps://github.com/" + REPOSITORY + "/actions/runs/50/job/51"}
            github = GitHub()
            logs = b"original historical failure bytes: Missing widget\n"
            with patch("preparation.shutil.which", return_value="/installed/dotnet"), \
                    patch("preparation.subprocess.run", return_value=subprocess.CompletedProcess(
                        ["dotnet", "--list-sdks"], 0, stdout="10.0.401 [/sdk]\n", stderr="")), \
                    patch.object(github, "api", side_effect=[{"run_id": 50, "conclusion": "failure"}, logs]) as api:
                prepared = prepare_inputs(github, source, issue, [], root)
            self.assertFalse(prepared["environment"]["exact_sdk_available"])
            self.assertEqual("11.0.100-rc.2", prepared["environment"]["expected_sdk"])
            self.assertFalse(prepared["environment"]["restore_attempted"])
            self.assertIn("not the model sandbox", prepared["environment"]["scope"])
            self.assertEqual(SOURCE, prepared["candidate_test_sources"][0]["path"])
            self.assertEqual(CODE, prepared["candidate_test_sources"][0]["content"])
            self.assertEqual(logs, (root / prepared["historical_jobs"][0]["path"]).read_bytes())
            self.assertEqual(["actions/jobs/51", "actions/jobs/51/logs"],
                             [call.args[0] for call in api.call_args_list])
            self.assertEqual(prepared, json.loads((root / "preparation.json").read_text()))

    def test_prepared_history_records_denial_and_never_fetches_foreign_repository_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "global.json").write_text(json.dumps({"sdk": {"version": "11.0.100"}}))
            issue = {**ISSUE, "body": (
                "https://github.com/elsewhere/aspire/actions/runs/50/job/51\n"
                f"https://github.com/{REPOSITORY}/actions/runs/60/job/61")}
            github = GitHub()
            with patch("preparation.shutil.which", return_value=None), patch.object(
                    github, "api", side_effect=GitHubAPIError(1, ["gh"], stderr=b"HTTP 403")) as api:
                prepared = prepare_inputs(github, root, issue, [], root)
            self.assertEqual(1, api.call_count)
            self.assertEqual("actions/jobs/61", api.call_args.args[0])
            self.assertIn("HTTP 403", prepared["historical_jobs"][0]["unavailable"])
            self.assertEqual([], prepared["candidate_test_sources"])
            self.assertFalse(prepared["environment"]["exact_sdk_available"])

    def test_tainted_checkpoint_identity_budgets_and_clock_fail_before_remote_writes(self):
        state, revision = Controller(FakeGitHub(), lambda: NOW).start(URL, CONTROL)
        for key, value in (
            ("issue_url", URL.replace("microsoft", "elsewhere")),
            ("base_sha", "main"), ("phase", "shell"), ("created_at", "2026-10-21"),
            ("used", {**state["used"], "workers": -1}), ("history", None),
        ):
            tainted = {**state, key: value}
            github = GitHub()
            with self.subTest(key=key), patch.object(github, "file", return_value=json.dumps(tainted)), \
                    patch.object(github, "api") as writes:
                with self.assertRaises(ValueError):
                    github.load(123, revision)
                writes.assert_not_called()

    def test_trusted_fork_context_cannot_authorize_upstream_issue_run_or_api(self):
        code = """
import json
from unittest.mock import patch
from contracts import REPOSITORY, issue_number
from github import GitHub, owned_run
assert REPOSITORY == "radical/aspire"
assert issue_number("https://github.com/radical/aspire/issues/123") == 123
try:
    issue_number("https://github.com/microsoft/aspire/issues/123")
except ValueError:
    pass
else:
    raise AssertionError("upstream issue was authorized by a fork context")
with patch("github.subprocess.run") as run:
    run.return_value.stdout = b'{}'
    GitHub().api("issues/123")
    assert run.call_args.args[0][:3] == ["gh", "api", "repos/radical/aspire/issues/123"]
expected = {"id": 1, "sha": "a" * 40, "workflow": "reproduce-flaky-tests.yml", "branch": "branch"}
run = {"id": 1, "run_attempt": 1, "head_sha": "a" * 40, "head_branch": "branch",
       "event": "workflow_dispatch", "path": ".github/workflows/reproduce-flaky-tests.yml",
       "head_repository": {"full_name": "microsoft/aspire"}}
try:
    owned_run(run, expected)
except ValueError:
    pass
else:
    raise AssertionError("upstream run was authorized by a fork context")
print("fork identity and exact authority verified")
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                env={**os.environ, "GITHUB_REPOSITORY": "radical/aspire"},
                                capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual("fork identity and exact authority verified\n", result.stdout)

    def test_first_phase_source_and_fixture_are_materialized_from_base_not_wrapper_head(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "--no-pager", *args], cwd=root, text=True).strip()
            git("init", "--quiet")
            guidance = root / ".agents/skills/fix-flaky-test/SKILL.md"
            guidance.parent.mkdir(parents=True)
            guidance.write_text("Read original failure data before reproducing or repairing.\n")
            fixture = root / "tests/Fixture.cs"
            fixture.parent.mkdir()
            fixture.write_text("frozen fixture\n")
            git("add", "--", ".agents/skills/fix-flaky-test/SKILL.md", "tests/Fixture.cs")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Fixture")
            base = git("rev-parse", "HEAD")
            fixture.write_text("different wrapper fixture\n")
            git("add", "--", "tests/Fixture.cs")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "Wrapper")
            wrapper = git("rev-parse", "HEAD")
            with patch("controller.ROOT", Path(temporary) / "inputs"):
                source = stage_source({"base_sha": base}, root)
            self.assertEqual("frozen fixture\n", (source / "tests/Fixture.cs").read_text())
            self.assertEqual(guidance.read_text(), (source / ".agents/skills/fix-flaky-test/SKILL.md").read_text())
            self.assertEqual(wrapper, git("rev-parse", "HEAD"))
            self.assertEqual("", git("status", "--porcelain"))

    def test_api_error_retains_http_diagnostic_and_never_retries(self):
        command = ["gh", "api", f"repos/{REPOSITORY}/git/refs", "--input", "-"]
        diagnostic = b"gh: Resource not accessible by integration (HTTP 403)\n"
        error = subprocess.CalledProcessError(1, command, output=b'{"status":"403"}', stderr=diagnostic)
        with patch("github.subprocess.run", side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                GitHub().create_ref("ankj/flaky-test-123-run-1", CONTROL)
        self.assertEqual(
            f"{error}\nGitHub API diagnostic: \"gh: Resource not accessible by integration (HTTP 403)\"",
            str(caught.exception))
        self.assertEqual(diagnostic, caught.exception.stderr)
        run.assert_called_once()

    def test_optional_lookup_still_suppresses_only_not_found(self):
        for status in (404, 403):
            error = subprocess.CalledProcessError(
                1, ["gh", "api"], stderr=f"gh: lookup failed (HTTP {status})".encode())
            with self.subTest(status=status), patch("github.subprocess.run", side_effect=error) as run, \
                    patch("sys.stderr", io.StringIO()) as stderr:
                if status == 404:
                    self.assertIsNone(GitHub().optional("git/ref/heads/missing"))
                else:
                    with self.assertRaisesRegex(subprocess.CalledProcessError, "HTTP 403"):
                        GitHub().optional("git/ref/heads/forbidden")
                self.assertEqual("", stderr.getvalue())
                run.assert_called_once()

    def test_api_diagnostics_are_bounded_escaped_and_redacted(self):
        diagnostic = "HTTP 403\n::error::untrusted\nghp_example_test_only https://example.invalid/?token=example&sig=example\n"
        error = subprocess.CalledProcessError(
            1, ["gh", "api"], stderr=(diagnostic + "x" * 5000).encode())
        with patch("github.subprocess.run", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                GitHub().api("git/refs", {"ref": "refs/heads/test", "sha": CONTROL})
        detail = str(caught.exception).partition("\nGitHub API diagnostic: ")[2]
        expected = "HTTP 403\n::error::untrusted\n[REDACTED] https://example.invalid/?token=[REDACTED]&sig=[REDACTED]\n"
        self.assertEqual(json.dumps((expected + "x" * 5000)[:4000]), detail)

    def test_agent_preparation_matches_proposal_branch_to_issue(self):
        for number in ("123", "124"):
            with self.subTest(number=number), patch.dict(os.environ, {
                "GITHUB_REPOSITORY": REPOSITORY, "ISSUE_URL": URL,
                "ISSUE_NUMBER": number, "STATE_REVISION": CONTROL,
            }), patch("sys.argv", ["controller.py", "prepare"]), \
                    patch("controller.GitHub") as transport, patch("controller.prepare") as prepare_context:
                if number == "123":
                    main()
                    prepare_context.assert_called_once_with(transport.return_value, URL, CONTROL)
                else:
                    with self.assertRaisesRegex(ValueError, "branch issue number"):
                        main()
                    prepare_context.assert_not_called()

    def test_local_precheck_requires_actual_commands_and_discards_untrusted_extra_fields(self):
        with self.assertRaisesRegex(ValueError, "commands"):
            local_precheck({"status": "completed", "reason": "Claimed it ran"})
        self.assertEqual({"status": "skipped", "reason": "Wrong OS"},
                         local_precheck({"status": "skipped", "reason": "Wrong OS", "shell_request": "never run"}))
        value = {"status": "completed", "reason": "Ran on matching OS", "os": "Darwin",
                 "commands": ["./dotnet.sh test ..."], "observed_output": "Total: 1, Passed: 1"}
        self.assertEqual(value, local_precheck(value))

    def test_enrollment_and_cancellation_require_maintainer_not_issue_authorship(self):
        github = GitHub()
        for operation in ("start", "cancel"):
            for permission in ("read", "triage", "none"):
                with self.subTest(operation=operation, permission=permission), patch.object(
                        github, "api", return_value={"permission": permission}):
                    with self.assertRaisesRegex(ValueError, "Maintainer"):
                        authorize(github, "issue-author", operation)
            with patch.object(github, "api", return_value={"permission": "write"}):
                authorize(github, "maintainer", operation)
        with patch.object(github, "api") as api:
            authorize(github, "github-actions[bot]", "resume")
            api.assert_not_called()

    def test_reject_foreign_urls_pull_requests_query_strings_and_shell(self):
        for url in (URL + "?x=1", URL + "#comment", URL.replace("/issues/", "/pull/"),
                    URL.replace("microsoft", "elsewhere"), URL + ";echo hacked", "123"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                issue_number(url)

    def test_discovery_rejects_ambiguous_methods_unrelated_issue_and_path_traversal(self):
        for changes, code in (
            ({"source": "tests/../../etc/file.cs"}, CODE),
            ({"method": "OtherMethod"}, CODE),
            ({"class_name": "Elsewhere.WidgetTests"}, CODE),
            ({"oses": ["self-hosted"]}, CODE),
            ({"failure_signature": "invented error"}, CODE),
            ({}, CODE + CODE),
            ({"project": "tests/Other.Tests/Other.Tests.csproj"}, CODE),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                selection({**SELECT, **changes}, ISSUE, {SOURCE: code, PROJECT: "<Project/>"})

    def test_source_scope_follows_explicit_references_not_dynamic_globs(self):
        scopes = repair_scope(PROJECT, {
            PROJECT: '<Project><ItemGroup><ProjectReference Include="../../src/Widget/Widget.csproj"/>'
                     '<ProjectReference Include="$(Untrusted)"/><ProjectReference Include="../../src/*/*.csproj"/>'
                     '</ItemGroup></Project>',
            "src/Widget/Widget.csproj": "<Project/>",
        })
        self.assertEqual(["src/Widget/", "tests/Sample.Tests/"], scopes)

    def test_candidate_checkout_is_validated_before_any_build(self):
        with patch("worker.subprocess.check_output", return_value=CONTROL):
            with self.assertRaisesRegex(ValueError, "checkout"):
                validate_checkout(Path("."), {"source_sha": BASE})
        with patch("worker.subprocess.check_output", return_value=BASE):
            validate_checkout(Path("."), {"source_sha": BASE})

    def test_malformed_model_values_fail_validation_not_the_controller_process(self):
        for field, value in (("action", {}), ("version", True), ("evidence_read", [None])):
            state = {"issue_url": URL}
            request = proposal(state, BASE, "select")
            request[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_proposal(state, BASE, request)
        with self.assertRaises(ValueError):
            selection({**SELECT, "oses": [{}]}, ISSUE, {SOURCE: CODE, PROJECT: "<Project/>"})
        with self.assertRaises(ValueError):
            local_precheck({"status": {}, "reason": "Not a status"})

    def test_candidate_rejects_workflows_generated_new_files_quarantine_and_skips(self):
        for files in (
            {".github/workflows/ci.yml": "bad"},
            {"src/Widget/api/Widget.cs": "bad"},
            {"tests/Sample.Tests/New.cs": CODE},
            {SOURCE: CODE.replace("QuarantinedTest", "Fact")},
            {SOURCE: CODE.replace("[QuarantinedTest", "[assembly: QuarantinedTest")},
            {SOURCE: CODE.replace("[QuarantinedTest", "[Fact(Skip = \"disabled\")]\n    [QuarantinedTest")},
            {SOURCE: CODE},
            {SOURCE: "\n".join(f"new line {i}" for i in range(801))},
        ):
            with self.subTest(files=list(files)), self.assertRaises(ValueError):
                validate_candidate(files, {SOURCE: CODE})

    def test_candidate_preserves_active_complete_attributes_and_their_owners(self):
        originals = [
            CODE,
            CODE.replace(f'[QuarantinedTest("{URL}")]', '[Theory]\n    [InlineData("[]", false)]'),
            CODE.replace("public class", '[CollectionDefinition("serial", DisableParallelization = true)]\npublic class'),
            CODE.replace("public async Task", '[Fact]\n    public async Task'),
        ]
        changes = [
            lambda value: value.replace(f'[QuarantinedTest("{URL}")]', f'/*[QuarantinedTest("{URL}")]*/'),
            lambda value: value.replace('InlineData("[]", false)', 'InlineData("[]", true)'),
            lambda value: value.replace("DisableParallelization = true", "DisableParallelization = false"),
            lambda value: value.replace("[Fact]\n    public async Task WaitsForWidget()",
                                        "[Fact]\n    public async Task DifferentTest()"),
        ]
        for original, change in zip(originals, changes):
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, "attributes"):
                validate_candidate({SOURCE: change(original)}, {SOURCE: original})
        for changed in (
            CODE.replace("class WidgetTests", "class ElsewhereTests"),
            CODE.replace("namespace Sample.Tests", "namespace Other.Tests"),
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "declarations"):
                validate_candidate({SOURCE: changed}, {SOURCE: CODE})

    def test_candidate_attribute_guard_accepts_body_fix_and_fails_closed_on_unsupported_syntax(self):
        original = CODE.replace("await widget.WaitAsync();", 'var label = @"[not an attribute]";\n        await widget.WaitAsync();')
        changed = original.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")
        self.assertEqual({SOURCE: changed}, validate_candidate({SOURCE: changed}, {SOURCE: original}))
        interpolated = CODE.replace("await widget.WaitAsync();", 'var label = $"value {widget.Ready}";\n        await widget.WaitAsync();')
        with self.assertRaisesRegex(ValueError, "human candidate review"):
            validate_candidate({SOURCE: interpolated.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                               {SOURCE: interpolated})

    def test_candidate_cannot_move_an_attributed_test_out_of_its_actual_nested_type(self):
        for original, moved in (
            ("class Outer { class Inner { [Fact] public void T() { Work(); } } }",
             "class Outer { class Inner { } [Fact] public void T() { Work(); } }"),
            ("namespace A { namespace B { class Test { [Fact] public void T() { Work(); } } } }",
             "namespace A { namespace B { } class Test { [Fact] public void T() { Work(); } } }"),
        ):
            with self.subTest(original=original):
                with self.assertRaisesRegex(ValueError, "attributes"):
                    validate_candidate({SOURCE: moved}, {SOURCE: original})
                fixed = original.replace("Work()", "ReadyWork()")
                self.assertEqual({SOURCE: fixed}, validate_candidate({SOURCE: fixed}, {SOURCE: original}))

    def test_owned_run_rejects_rerun_foreign_repository_workflow_and_sha(self):
        expected = {"id": 1, "sha": BASE, "workflow": WORKER, "branch": "ankj/flaky-test-123-run-1"}
        run = {"id": 1, "head_sha": BASE, "run_attempt": 1, "event": "workflow_dispatch",
               "path": ".github/workflows/" + WORKER, "head_repository": {"full_name": REPOSITORY},
               "head_branch": expected["branch"]}
        self.assertEqual(run, owned_run(run, expected))
        for field, value in (("id", 2), ("head_sha", CONTROL), ("run_attempt", 2),
                             ("path", ".github/workflows/ci.yml"), ("event", "push"), ("head_branch", "main"),
                             ("head_repository", {"full_name": "elsewhere/repo"})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                owned_run({**run, field: value}, expected)

    def test_dispatch_captures_response_id_and_rejects_204_without_latest_lookup(self):
        github = GitHub()
        branch = "ankj/flaky-test-123-run-1"
        with patch.object(github, "api", return_value={"workflow_run_id": 456}) as api:
            self.assertEqual(456, github.dispatch(WORKER, branch, {}))
            self.assertEqual(branch, api.call_args.args[1]["ref"])
            self.assertTrue(api.call_args.args[1]["return_run_details"])
        with patch.object(github, "api", return_value=None) as api:
            with self.assertRaisesRegex(ValueError, "run-ID"):
                github.dispatch(WORKER, branch, {})
            self.assertEqual(1, api.call_count)

    def test_git_checkpoint_adapter_uses_nonforced_update_and_parent(self):
        github = GitHub()
        state = {"issue": 123, "phase": "ready"}
        with patch.object(github, "commit", return_value=CONTROL) as commit, patch.object(github, "api") as api:
            self.assertEqual(CONTROL, github.checkpoint(state, BASE))
            self.assertEqual(BASE, commit.call_args.args[0])
            self.assertEqual(("git/refs/heads/" + state_branch(123), {"sha": CONTROL, "force": False}, "PATCH"),
                             api.call_args.args)

    def test_artifact_rejects_duplicate_identity_and_zip_traversal(self):
        github = GitHub()
        item = {"id": 7, "name": "repo-memory-default", "size_in_bytes": 1, "expired": False}
        with patch.object(github, "pages", return_value=[item, item]):
            with self.assertRaisesRegex(ValueError, "one unexpired"):
                github.artifact(1, "repo-memory-default")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../proposal.json", "{}")
        with patch.object(github, "pages", return_value=[item]), patch.object(github, "api", return_value=data.getvalue()):
            with self.assertRaisesRegex(ValueError, "artifact path"):
                github.artifact(1, "repo-memory-default")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.target = {**SELECT, "quarantined": True}
        self.worker = {"id": 88, "sha": CONTROL, "source_sha": BASE, "runners": 1, "iterations": 2,
                       "mode": "single", "phase": "reproduce"}
        self.files = evidence(self.worker, self.target, True)

    def test_original_results_not_summary_counters_count_passes_and_failures(self):
        result = summarize(self.files, self.worker, self.target)
        self.assertEqual((1, 1, 2, 2),
                         (result["passed"], result["failed"], result["actual_tests"], result["runner_iterations"]))

    def test_all_failed_issue_matching_case_is_reproduced_without_observed_intermittency(self):
        for iteration in (1, 2):
            files = self.files[("ubuntu-latest", 1)]
            prefix = f"iteration-{iteration}/"
            files[prefix + "original.trx"] = trx("Failed", f"all-failed-{iteration}")
            files[prefix + "result.json"] = b'{"exit_code":2}'
        result = summarize(self.files, self.worker, self.target)
        self.assertEqual((0, 2, 2, 2),
                         (result["passed"], result["failed"], result["actual_tests"], result["runner_iterations"]))
        self.assertEqual({("ubuntu-latest", "WaitsForWidget", "test")},
                         reproduced_cases([{**self.worker, **result}], BASE))

    def test_fully_qualified_mtp_method_names_count_the_exact_target(self):
        for files in self.files.values():
            for path in list(files):
                if path.endswith(".trx"):
                    files[path] = files[path].replace(
                        b'name="WaitsForWidget"', b'name="Sample.Tests.WidgetTests.WaitsForWidget"')
        result = summarize(self.files, self.worker, self.target)
        self.assertEqual((1, 1), (result["passed"], result["failed"]))

    def test_stdout_cannot_supply_the_failure_signature(self):
        files = self.files[("ubuntu-latest", 1)]
        files["iteration-1/original.trx"] = trx("Failed", error="Unrelated assertion").replace(
            b"</Output>", b"<StdOut>Missing widget</StdOut></Output>")
        with self.assertRaisesRegex(ValueError, "signature"):
            summarize(self.files, self.worker, self.target)

    def test_case_and_os_counts_do_not_pool_different_theory_rows(self):
        files = self.files[("ubuntu-latest", 1)]
        for iteration, name in ((1, "WaitsForWidget(value: 1)"), (2, "WaitsForWidget(value: 2)")):
            path = f"iteration-{iteration}/original.trx"
            files[path] = files[path].replace(
                b'testName="WaitsForWidget"', f'testName="{name}"'.encode())
        result = summarize(self.files, self.worker, self.target)
        self.assertEqual([
            {"os": "ubuntu-latest", "name": "WaitsForWidget(value: 1)", "test_id": "test", "passed": 0, "failed": 1},
            {"os": "ubuntu-latest", "name": "WaitsForWidget(value: 2)", "test_id": "test", "passed": 1, "failed": 0},
        ], result["cases"])
        self.assertEqual({("ubuntu-latest", "WaitsForWidget(value: 1)", "test")},
                         reproduced_cases([{**self.worker, **result}], BASE))

    def test_distinct_test_ids_with_the_same_display_name_cannot_pool_deterministic_cases(self):
        for iteration in (1, 2):
            files = self.files[("ubuntu-latest", 1)]
            prefix = f"iteration-{iteration}/"
            files[prefix + "original.trx"] = trx("Failed", execution=f"fail-{iteration}").replace(
                b'"test"', b'"always-fails"')
            files[prefix + "passing.trx"] = trx("Passed", execution=f"pass-{iteration}").replace(
                b'"test"', b'"always-passes"')
            files[prefix + "result.json"] = b'{"exit_code":2}'
        with self.assertRaisesRegex(ValueError, "case.*identit"):
            summarize(self.files, self.worker, self.target)

    def test_distinct_definition_ids_across_rounds_are_rejected_before_accepting_a_second_batch(self):
        first = summarize(self.files, self.worker, self.target)
        changed = copy.deepcopy(self.files)
        for files in changed.values():
            for path in files:
                if path.endswith(".trx"):
                    files[path] = files[path].replace(b'"test"', b'"different-definition"')
        second = summarize(changed, self.worker, self.target)
        with self.assertRaisesRegex(ValueError, "identit.*across rounds"):
            reproduced_cases([
                {**self.worker, **first}, {**self.worker, **second},
            ], BASE)
        github = FakeGitHub()
        github.runs[self.worker["id"]] = {
            "id": self.worker["id"], "head_sha": CONTROL, "head_branch": "owned", "run_attempt": 1,
            "event": "workflow_dispatch", "path": ".github/workflows/" + WORKER,
            "head_repository": {"full_name": REPOSITORY}, "status": "completed", "conclusion": "failure",
        }
        for (os_name, index), files in changed.items():
            github.artifacts[(self.worker["id"], f"flaky-evidence-{os_name}-{index}")] = files
        state = {"worker": {**self.worker, "workflow": WORKER, "branch": "owned"},
                 "target": self.target, "base_sha": BASE, "history": [{**self.worker, **first}]}
        with self.assertRaisesRegex(ValueError, "identit.*across rounds"):
            Controller(github).collect_worker(state)
        self.assertEqual([{**self.worker, **first}], state["history"])

    def test_case_identity_is_os_scoped_and_legacy_name_only_counts_cannot_authorize_repair(self):
        batch = {**self.worker, **summarize(self.files, self.worker, self.target)}
        case = batch["cases"][0]
        batch["cases"].append({**case, "os": "windows-latest", "test_id": "different-os-identity"})
        self.assertEqual({
            ("ubuntu-latest", "WaitsForWidget", "test"),
            ("windows-latest", "WaitsForWidget", "different-os-identity"),
        }, reproduced_cases([batch], BASE))
        for item in batch["cases"]:
            del item["test_id"]
        self.assertEqual(set(), reproduced_cases([batch], BASE))

    def test_project_mode_actual_tests_are_not_runner_iterations(self):
        self.worker["mode"] = "project"
        files = evidence(self.worker, self.target)
        files[("ubuntu-latest", 1)]["iteration-1/another.trx"] = trx(method="DifferentTest", execution="other")
        result = summarize(files, self.worker, self.target)
        self.assertEqual(2, result["runner_iterations"])
        self.assertEqual(3, result["actual_tests"])

    def test_missing_reports_zero_tests_build_exit_skip_signature_and_duplicate_fail_closed(self):
        for defect in ("missing", "empty", "build", "skip", "signature", "duplicate",
                       "identity", "os", "target", "unrelated", "malformed-xml", "malformed-exit"):
            files = copy.deepcopy(self.files)
            runner = files[("ubuntu-latest", 1)]
            with self.subTest(defect=defect), self.assertRaises((ValueError, KeyError)):
                if defect == "missing":
                    del runner["iteration-1/original.trx"]
                elif defect == "empty":
                    runner["iteration-1/original.trx"] = b"<TestRun/>"
                elif defect == "build":
                    runner["iteration-1/result.json"] = b'{"exit_code":1}'
                elif defect == "skip":
                    runner["iteration-1/original.trx"] = trx("NotExecuted")
                elif defect == "signature":
                    runner["iteration-1/original.trx"] = trx("Failed", error="network restore error")
                elif defect == "duplicate":
                    runner["iteration-1/merged.trx"] = runner["iteration-1/original.trx"]
                elif defect in {"identity", "os"}:
                    receipt = json.loads(runner["receipt.json"])
                    receipt["sha" if defect == "identity" else "os"] = (
                        CONTROL if defect == "identity" else "windows-latest")
                    runner["receipt.json"] = json.dumps(receipt).encode()
                elif defect == "target":
                    runner["iteration-1/original.trx"] = trx("Failed", method="DifferentTest")
                elif defect == "malformed-xml":
                    runner["iteration-1/original.trx"] = b"<not XML"
                elif defect == "malformed-exit":
                    runner["iteration-1/result.json"] = b'[]'
                else:
                    runner["iteration-1/unrelated.trx"] = trx("Failed", execution="other", method="Other")
                summarize(files, self.worker, self.target)

    def test_adapter_executes_and_retains_every_original_report_including_passes(self):
        config = {"target": self.target, "runners": 1, "iterations": 2, "mode": "single",
                  "source_sha": BASE, "trusted_sha": CONTROL}
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            directory = Path(command[command.index("--results-directory") + 1])
            (directory / "original.trx").write_bytes(trx(execution=str(len(calls))))
            return subprocess.CompletedProcess(command, 0)
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1",
            "FLAKY_RUNNER_OS": "ubuntu-latest", "FLAKY_RUNNER_INDEX": "1",
        }), patch("runner.subprocess.check_output", return_value=BASE):
            root = Path(temporary)
            self.assertEqual(0, execute(root, config, run=run))
            files = {str(path.relative_to(root / "flaky-evidence")).replace("\\", "/"): path.read_bytes()
                     for path in (root / "flaky-evidence").rglob("*") if path.is_file()}
            result = summarize({("ubuntu-latest", 1): files}, self.worker, self.target)
            self.assertEqual(2, result["passed"])
            self.assertEqual(2, len(calls))

    def test_real_process_console_streams_before_exit_without_commands_or_credentials_and_keeps_raw_bytes(self):
        raw = (b"phase-one: target assertion failed \xff\n"
               b"::error::untrusted-command\n"
               b"https://example.invalid/?sig=secret-value&token=other-secret\n"
               b"ghp_fake_credential_for_test_only\n")
        noise = (b"ordinary test progress " * 5 + b"\n") * 1000
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acknowledgement = root / "seen"
            class Console(io.StringIO):
                def write(self, value):
                    if "phase-one" in value:
                        acknowledgement.write_text("observed before child exits")
                    return super().write(value)
            code = ("import os,time,pathlib; "
                    f"os.write(1,{raw!r}); "
                    f"ack=pathlib.Path({str(acknowledgement)!r}); "
                    "deadline=time.monotonic()+5\n"
                    "while not ack.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    "assert ack.exists(), 'output was not streamed while running'\n"
                    "os.write(2,b'final stderr failure\\n'); os.write(1,b'x'*100000+b'\\n'); "
                    "os.write(1,(b'ordinary test progress '*5+b'\\n')*1000); "
                    "raise SystemExit(2)")
            with (root / "output.log").open("wb") as log, patch("sys.stdout", Console()) as console:
                result = run_streamed([sys.executable, "-c", code], cwd=root, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=10, check=False)
            self.assertEqual(2, result.returncode)
            self.assertEqual(raw + b"final stderr failure\n" + b"x" * 100000 + b"\n" + noise,
                             (root / "output.log").read_bytes())
            self.assertIn("target assertion failed", console.getvalue())
            self.assertIn("final stderr failure", console.getvalue())
            self.assertNotIn("secret-value", console.getvalue())
            self.assertNotIn("other-secret", console.getvalue())
            self.assertNotIn("ghp_fake", console.getvalue())
            self.assertLess(len(console.getvalue()), 66000)
            self.assertIn("Console limit reached", console.getvalue())
            self.assertTrue(all(line.startswith("flaky-test-output ") for line in console.getvalue().splitlines()))

    def test_real_process_console_escapes_legacy_commands_across_read_boundaries_losslessly(self):
        command = b"##[error]legacy ##[add-mask]mask\r::error::modern\r\n"
        raw = b"x" * 8189 + b"\n" + command + b"::add-mask::modern\nsuffix##[warning]last"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acknowledgement = root / "seen"
            class Console(io.StringIO):
                def write(self, value):
                    if value.startswith("flaky-test-output "):
                        acknowledgement.write_text("first chunk consumed")
                    return super().write(value)
            # The first 8192-byte read ends in "##"; wait for it to be consumed
            # before writing "[error]..." so command detection spans two reads.
            code = ("import os,time,pathlib; "
                    f"os.write(1,{raw[:8192]!r}); "
                    f"ack=pathlib.Path({str(acknowledgement)!r}); "
                    "deadline=time.monotonic()+5\n"
                    "while not ack.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                    "assert ack.exists(), 'first chunk was not streamed'\n"
                    f"os.write(1,{raw[8192:]!r})")
            with (root / "output.log").open("wb") as log, patch("sys.stdout", Console()) as console:
                result = run_streamed([sys.executable, "-c", code], cwd=root, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=10, check=False)
            self.assertEqual(0, result.returncode)
            self.assertEqual(raw, (root / "output.log").read_bytes())
            frames = console.getvalue().splitlines()
            self.assertEqual(4, len(frames))
            for frame in frames:
                self.assertNotRegex(frame, r"##\[|^\s*::")
            self.assertEqual(
                ["x" * 4000, command[:-1].decode(), "::add-mask::modern", "suffix##[warning]last"],
                [json.loads(frame.removeprefix("flaky-test-output "))["text"] for frame in frames])

    def test_real_streamed_process_timeout_returns_without_reader_hang(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (root / "output.log").open("wb") as log, patch("sys.stdout", io.StringIO()):
                with self.assertRaises(subprocess.TimeoutExpired):
                    run_streamed([sys.executable, "-c", "import os,time; os.write(1,b'started\\n'); time.sleep(30)"],
                                 cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=2, check=False)
            self.assertEqual(b"started\n", (root / "output.log").read_bytes())

    def test_timeout_stops_exact_owned_descendants_not_an_unrelated_process(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            heartbeat = root / "heartbeat"
            pid_path = root / "child.pid"
            child_code = (
                "import time,pathlib; "
                f"p=pathlib.Path({str(heartbeat)!r})\n"
                "while True:\n"
                " with p.open('ab') as stream: stream.write(b'alive\\n')\n"
                " time.sleep(.02)\n")
            parent_code = (
                "import subprocess,sys,time,pathlib; "
                f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid)); time.sleep(30)")
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            try:
                with (root / "output.log").open("wb") as log, patch("sys.stdout", io.StringIO()):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        run_streamed([sys.executable, "-c", parent_code], cwd=root, stdout=log,
                                     stderr=subprocess.STDOUT, timeout=1, check=False)
                self.assertTrue(heartbeat.is_file(), "descendant did not start")
                size = heartbeat.stat().st_size
                time.sleep(.2)
                self.assertEqual(size, heartbeat.stat().st_size, "timed-out descendant is still running")
                self.assertIsNone(unrelated.poll(), "unrelated process was terminated")
            finally:
                unrelated.kill()
                unrelated.wait(timeout=5)
                if pid_path.exists():
                    try:
                        os.kill(int(pid_path.read_text()), 9)
                    except ProcessLookupError:
                        pass

    def test_runner_surfaces_signals_and_timeouts(self):
        for code, expected in ((-9, 137), (124, 124)):
            def run(command, **kwargs):
                if code == 124:
                    raise subprocess.TimeoutExpired(command, 720)
                return subprocess.CompletedProcess(command, code)

            config = {"target": self.target, "iterations": 1, "mode": "single",
                      "source_sha": BASE, "trusted_sha": CONTROL}
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary, \
                    patch.dict(os.environ, {
                        "GITHUB_RUN_ID": "88", "GITHUB_RUN_ATTEMPT": "1",
                        "FLAKY_RUNNER_OS": "ubuntu-latest", "FLAKY_RUNNER_INDEX": "1",
                    }), patch("runner.subprocess.check_output", return_value=BASE):
                root = Path(temporary)
                self.assertEqual(expected, execute(root, config, run=run))
                result = json.loads((root / "flaky-evidence/iteration-1/result.json").read_text())
                self.assertEqual({"exit_code": code}, result)


if __name__ == "__main__":
    unittest.main()

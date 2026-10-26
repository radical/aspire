"""Behavioral regression tests for production orchestration; no network writes."""

import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from contracts import (
    REPOSITORY, WORKER, issue_number, local_precheck, repair_scope, reserve,
    selection, validate_candidate, validate_proposal,
)
from controller import Controller, authorize, main
from evidence import summarize
from github import GitHub, GitHubAPIError, owned_run, state_branch
from runner import execute
from reporting import findings
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

    def api(self, endpoint, body=None, method=None):
        self.calls.append((endpoint, body, method))
        if endpoint == "issues/123":
            return copy.deepcopy(ISSUE)
        if endpoint == "git/ref/heads/main":
            return {"object": {"sha": BASE}}
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
            "status": "in_progress", "conclusion": None,
        }
        return run_id

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

    def invoke_controller(self):
        with patch.dict(os.environ, {
            "GITHUB_REPOSITORY": REPOSITORY, "ISSUE_URL": URL,
            "OPERATION": "resume", "GITHUB_EVENT_NAME": "schedule",
        }), patch("sys.argv", ["controller.py", "advance"]), \
                patch("controller.GitHub", return_value=self.github), \
                patch("controller.Controller", return_value=self.controller):
            main()

    def test_rejected_worker_posts_short_analysis_with_exact_evidence_and_resumes_without_duplicates(self):
        self.selected()
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        self.complete_worker(False)
        state, revision = self.live()
        self.controller.dispatch_worker(state, revision, "reproduce", 1, 1, "single")
        state, _ = self.live()
        worker = state["worker"]
        self.github.runs[worker["id"]].update(status="completed", conclusion="failure")
        files = evidence(worker, state["target"])[("ubuntu-latest", 1)]
        files["iteration-1/unrelated.trx"] = trx("Failed", execution="unrelated", method="Other")
        self.github.artifacts[(worker["id"], "flaky-evidence-ubuntu-latest-1")] = files
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
        request = proposal(self.state, self.revision, "finish", outcome="not-reproduced", analysis=analysis)
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
        for reason in ("time", "models", "cancel", "intent"):
            with self.subTest(reason=reason):
                self.setUp()
                if reason == "time":
                    self.state["created_at"] = (NOW - timedelta(hours=12)).isoformat()
                elif reason == "models":
                    self.state["used"]["models"] = 8
                elif reason == "intent":
                    self.state["phase"] = "worker-intent"
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
        self.controller.advance(state, revision)
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
        self.controller.dispatch_worker(self.state, self.revision, "reproduce", 1, 1, "single")
        self.complete_worker(False)
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
        self.assertIn("1 target passes, 0 target failures; 1 actual tests in accepted evidence", summary)
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
                self.controller.advance(state, revision)
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
        body = self.github.comments[0]["body"]
        self.assertIn(f"https://github.com/{REPOSITORY}/actions/runs/{run_id}", body)
        self.assertIn("Latest checkpointed run identities (not test results)", body)
        self.assertIn("**Observed:** No accepted test executions.", body)

    def test_lost_pr_response_never_posts_definite_no_pr_or_retries_publication(self):
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
            self.invoke_controller()
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
            self.state, self.revision, "select", selection=SELECT,
            local_precheck={"status": "skipped", "reason": "Windows issue; Linux runner lacks matching OS."}))
        state, _ = self.live()
        self.assertEqual("WaitsForWidget", state["target"]["method"])
        self.assertEqual(["tests/Sample.Tests/"], state["repair_scope"])
        self.assertEqual("ready", state["phase"])

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

    def test_lost_dispatch_receipt_recovers_to_finite_blocker_without_redispatch(self):
        self.github.fail_dispatch = True
        with self.assertRaisesRegex(ValueError, "receipt"):
            self.controller.advance(self.state, self.revision)
        state, revision = self.live()
        self.assertEqual("agent-intent", state["phase"])
        self.controller.advance(state, revision)
        self.assertEqual("blocked", self.github.state["outcome"])
        self.assertEqual(1, len([call for call in self.github.calls if call[0] == "dispatch"]))

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
            self.controller.advance(state, revision)
            create_ref.assert_called_once_with(state["worker"]["branch"], CONTROL)
        self.assertEqual("terminal", self.github.state["phase"])
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
                state, state["agent"]["revision"], "select", selection=SELECT,
                local_precheck={"status": "skipped", "reason": "Runner environment unavailable."})).encode()}
        self.controller.advance(state, revision)
        self.assertEqual("ready", self.github.state["phase"])
        self.assertEqual("select", self.github.state["notes"][-1]["action"])
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
        self.assertEqual("cancelled", self.github.state["outcome"])

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

    def test_candidate_cannot_be_published_without_matching_pass_and_failure(self):
        self.selected()
        request = proposal(self.state, self.revision, "repair",
                           files={SOURCE: CODE.replace("widget.WaitAsync()", "widget.WaitReadyAsync()")},
                           diagnosis="Observed missing-widget error and confirmed readiness race.")
        with self.assertRaisesRegex(ValueError, "unreproduced"):
            self.controller.handle(self.state, self.revision, request)
        self.assertEqual({}, self.github.commits)

    def test_checkpointed_candidate_resumes_verification_without_republishing_source(self):
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

    def test_real_failed_candidate_then_verified_revision_produces_one_clean_draft(self):
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

    def test_project_mode_actual_tests_are_not_runner_iterations(self):
        self.worker["mode"] = "project"
        files = evidence(self.worker, self.target)
        files[("ubuntu-latest", 1)]["iteration-1/another.trx"] = trx(method="DifferentTest", execution="other")
        result = summarize(files, self.worker, self.target)
        self.assertEqual(2, result["runner_iterations"])
        self.assertEqual(3, result["actual_tests"])

    def test_missing_reports_zero_tests_build_exit_skip_signature_and_duplicate_fail_closed(self):
        for defect in ("missing", "empty", "build", "skip", "signature", "duplicate",
                       "identity", "unrelated", "malformed-xml", "malformed-exit"):
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
                elif defect == "identity":
                    receipt = json.loads(runner["receipt.json"])
                    receipt["sha"] = CONTROL
                    runner["receipt.json"] = json.dumps(receipt).encode()
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

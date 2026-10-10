"""Operational read-only generator/dedup bridge; never passes --create to .NET."""

from copy import deepcopy
import argparse
import json
from pathlib import Path
import re
import sys

import local
import round as contracts
from result_collector import LocalCollector, bounded_command
import work_item_followup as followup
import work_item_tracker as trackers
import work_items as items


TOOL_PROJECT = local.ROOT / "tools" / "CreateFailingTestIssue" / "CreateFailingTestIssue.csproj"


def source_url(occurrence):
    return (f"https://github.com/{occurrence['repository']}/actions/runs/{occurrence['run_id']}"
            f"/attempts/{occurrence['run_attempt']}/job/{occurrence['job_id']}")


def generated_text(text, token):
    # Keep canonical public GitHub evidence links/template Markdown. Never
    # retain signed storage URLs or credentials embedded in stack/log output.
    text = text.replace(token, "[credential omitted]") if token else text
    text = re.sub(r"(?i)\b(?:password|secret|token|authorization|api[_-]?key)"
                  r"\s*[:=]\s*(?:bearer\s+)?\S+", "[credential omitted]", text)
    text = re.sub(r"(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)",
                  "[credential omitted]", text)
    return re.sub(r"https?://[^\s)<>]+", lambda match: (
        match[0] if match[0].startswith("https://github.com/") and "?" not in match[0]
        else "[URL omitted]"), text, flags=re.I)


def parse_generated(value, control, test, token):
    """Validate primary tool JSON, including exact attempt/job/test provenance."""
    occurrence = control["occurrence"]
    if occurrence is None:
        raise ValueError("test tracker generation requires pinned occurrence")
    if not isinstance(value, dict) or value.get("success") is not True:
        raise ValueError("failing-test tool did not return successful generation")
    input_, resolution, match = value.get("input"), value.get("resolution"), value.get("match")
    failure, issue = value.get("failure"), value.get("issue")
    if not all(isinstance(entry, dict) for entry in (input_, resolution, match, failure, issue)):
        raise ValueError("generated tracker evidence unavailable")
    workflow = input_.get("workflow")
    if (not isinstance(workflow, dict) or input_.get("testQuery") != test["canonical_name"]
            or input_.get("sourceUrl") != source_url(occurrence) or input_.get("forceNew") is not False
            or workflow.get("requested") != test["workflow_file"]
            or workflow.get("resolvedWorkflowFile") != test["workflow_file"]
            or match.get("canonicalTestName") != test["canonical_name"]
            or match.get("query") != test["canonical_name"]
            or resolution.get("sourceKind") != "job"
            or type(resolution.get("runId")) is not int or resolution["runId"] != occurrence["run_id"]
            or type(resolution.get("runAttempt")) is not int
            or resolution["runAttempt"] != occurrence["run_attempt"]
            or resolution.get("runUrl") != source_url(occurrence).rsplit("/job/", 1)[0]):
        raise ValueError("generated tracker canonical workflow/test/run identity mismatch")
    job_url = f"https://github.com/{occurrence['repository']}/actions/runs/{occurrence['run_id']}/job/{occurrence['job_id']}"
    accepted_jobs = {job_url, source_url(occurrence)}
    occurrences = match.get("allMatchingOccurrences")
    if (failure.get("jobUrl") not in accepted_jobs or not isinstance(occurrences, list) or not occurrences
            or not all(isinstance(entry, dict) and entry.get("canonicalTestName") == test["canonical_name"]
                       for entry in occurrences)
            or not any(entry.get("jobUrl") in accepted_jobs for entry in occurrences)
            or not isinstance(resolution.get("jobUrls"), list)
            or not any(url in accepted_jobs for url in resolution["jobUrls"])
            or issue.get("createdIssue") is not None or issue.get("existingIssue") is not None):
        raise ValueError("generated tracker job/effect identity mismatch")
    signature = issue.get("stableSignature")
    if (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{16}", signature)
            or issue.get("metadataMarker") != trackers.signature_marker(signature)):
        raise ValueError("canonical tool signature unavailable")
    body, title = issue.get("body"), issue.get("title")
    items.proposal({"title": title, "body": body})
    if body.count(trackers.signature_marker(signature)) != 1:
        raise ValueError("generated tracker body marker mismatch")
    return {"title": generated_text(title, token), "body": generated_text(body, token),
            "signature": signature, "occurrence": deepcopy(occurrence), "test_identity": deepcopy(test)}


class Generator:
    def __init__(self, token, *, command=bounded_command):
        self.token, self.command = token, command

    def __call__(self, control, test, output):
        occurrence = control["occurrence"]
        if occurrence is None or occurrence["repository"] != control["issue"]["repository"]:
            raise ValueError("same-repository pinned occurrence required")
        result_path = output / "generated-tool.json"
        if result_path.exists():
            raise ValueError("exclusive tracker output required")
        environment = LocalCollector(self.token, command=self.command).verified_environment()
        # The child gh process must use the already verified selected identity.
        # Running outside the checkout contains diagnostics.log and tool scratch.
        try:
            code, _, _ = self.command([
                str(local.ROOT / ".dotnet" / "dotnet"), "run", "--project", str(TOOL_PROJECT),
                "--no-launch-profile", "--",
                "--test", test["canonical_name"], "--url", source_url(occurrence),
                "--workflow", test["workflow_file"], "--repo", occurrence["repository"],
                "--output", str(result_path)],
                environment, limit=100_000, timeout=300, cwd=output)
            if code:
                raise ValueError("failing-test tool generation failed; no publication authorized")
            result = parse_generated(contracts.read_json(result_path), control, test, self.token)
            contracts.write_json(output / "generated-preview.json", result)
            return result
        finally:
            # Contain raw logs/stack traces, including error paths, within the
            # exclusive operator output directory; retain redacted receipt only.
            for path in (result_path, output / "diagnostics.log"):
                path.unlink(missing_ok=True)


def prepare(api, read_control, test_basis, generator, output, *, operator=False):
    control, record, assignment = followup.owned(api, read_control)
    if operator:
        trackers.validate_operator(test_basis, record, assignment)
        test = test_basis["test_identity"]
        action = {"kind": "create_issue", "target": {
            "repository": control["issue"]["repository"], "number": None, "node_id": None},
            "title": "[Failing test]: " + test["canonical_name"], "body": "Generate pinned failure tracker.",
            "evidence": ["Exact pinned occurrence supplied by the operator."],
            "justification": "Prepare or reuse a dedicated tracker without creating an issue.",
            "test_identity": test}
        items.validate_actions([action])
    else:
        action = test_basis
        items.validate_actions([action])
        if action not in (assignment["result"] or {}).get("proposed_actions", []):
            raise ValueError("checkpointed exact worker proposal required")
        test = action["test_identity"]
    collection = assignment["execution"].get("followup")
    if collection is None or collection["status"] == "collecting":
        raise ValueError("completed owned-task collection required before tracker preparation")
    snapshot = collection["task_version"]
    followup.stable(api, read_control, control, record, assignment, snapshot)
    followup.pinned(api, control["occurrence"], None)
    generated = generator(control, test, output)
    # Tool provenance does not permit replacing worker JSON. Store generated
    # canonical content separately and retain exact original worker action.
    if operator:
        action.update(title=generated["title"], body="Pinned artifact-generated issue preview.",
                      evidence=["Verified exact test/workflow/run/attempt/job generator output."])
    followup.stable(api, read_control, control, record, assignment, snapshot)
    receipt = trackers.prepare(api, read_control, action, generated=generated,
                               operator_basis=test_basis if operator else None)
    receipt["approval_template"] = {
        "item_id": record["id"], "assignment_id": assignment["id"], "revision": control["revision"],
        "repository": control["issue"]["repository"], "title": receipt["title"], "body": receipt["body"]}
    receipt["creation_permission"] = control["authority"].get("issue_create", False)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item", type=Path, required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--proposal", type=Path, help="Exact checkpointed worker tracker proposal")
    inputs.add_argument("--operator-test", type=Path, help="Separate trusted operator assignment/test basis")
    parser.add_argument("--tracker", type=int, required=True)
    parser.add_argument("--authority", type=int, required=True)
    parser.add_argument("--tracker-node", required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--read-only", action="store_true", help="Local preview only; no authority writes")
    args = parser.parse_args(argv)
    try:
        from work_item_github import FollowupGitHub, ReadOnlyFollowupGitHub
        read_control = lambda: contracts.read_json(args.item)
        control = items.validate_control(read_control())
        token = local.selected_token()
        revision = local.command(["git", "--no-pager", "-C", str(local.ROOT), "rev-parse", "HEAD"])
        local.require_source(revision)
        local.require_idle_actions(token)
        with local.authority_lock(Path.home() / ".copilot" / "ci-shepherd" / "locks", args.authority):
            args.workdir.mkdir(mode=0o700, parents=True, exist_ok=False)
            cls = ReadOnlyFollowupGitHub if args.read_only else FollowupGitHub
            api = cls(token, args.tracker, args.authority, args.tracker_node, revision=revision, control=control)
            # Historical tasks have no worker proposal; the explicit operator
            # basis is separate, and collection never fabricates worker JSON.
            followup.collect(api, read_control, LocalCollector(token))
            receipt = prepare(api, read_control, contracts.read_json(args.operator_test or args.proposal),
                              Generator(token), args.workdir, operator=args.operator_test is not None)
            receipt["read_only"] = args.read_only
            contracts.write_json(args.workdir / "receipt.json", receipt)
            if args.read_only:
                contracts.write_json(args.workdir / "authority-preview.json", api.ledger)
            print(json.dumps(receipt, allow_nan=False))
            return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"CI Shepherd test tracker preparation failed closed: {followup.safe(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

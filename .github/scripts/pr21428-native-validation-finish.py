"""Verify the actual native handoff, source/count fields and request diagnostics."""

import json
from pathlib import Path
import re


ROOT = Path("/tmp/gh-aw/pr21428-validation")
proposal = json.loads((ROOT / "proposal.json").read_text())
context = json.loads(Path(".github/scripts/pr21428-native-validation-context.json").read_text())
expected = {
    "version": 1, "pr": 21428, "state_revision": context["state_revision"],
    "action": "finish", "outcome": context["expected_outcome"],
    "sources": context["sources"],
    "baseline_reproduced": context["expected_counts"]["before"]["original_timeouts"] > 0,
    "observed_suite_iterations": {
        case: context["expected_counts"][case]["executions"] for case in ("before", "after")},
    "target_results": context["expected_counts"], "image_results": context["expected_images"],
    "agent_directed_test_dispatches": 0,
}
if set(proposal) != set(expected) | {"evidence_read", "explanation", "short_result"}:
    raise ValueError("Handoff does not match the closed validation-only envelope")
if any(proposal[key] != value for key, value in expected.items()):
    raise ValueError("Handoff source identities, action, counts or outcomes disagree with original evidence")
if not isinstance(proposal["evidence_read"], list) or len(proposal["evidence_read"]) < 5:
    raise ValueError("Handoff must name inspected source and evidence")
for entry in proposal["evidence_read"]:
    if not isinstance(entry, str):
        raise ValueError("Invalid evidence-read entry")
    path = Path(entry)
    if not path.is_file() or not path.resolve().is_relative_to(ROOT):
        raise ValueError("Claimed evidence is absent or outside admitted staging")
if not all(isinstance(proposal[key], str) and proposal[key].strip()
           for key in ("explanation", "short_result")):
    raise ValueError("Missing substantive result or explanation")
logs = list(Path("/tmp/gh-aw/sandbox/agent/logs").glob("process-*.log"))
if len(logs) != 1:
    raise ValueError("Expected one native CLI process with no harness retries")
process = logs[0].read_text()
if "Starting Copilot CLI: 1.0.85" not in process:
    raise ValueError("Unexpected actual CLI version")
fingerprints = [json.loads(match) for match in re.findall(
    r'"copilotToolsFingerprint":\s*(\{.*?\n  \})', process, re.S)]
allowed = {"apply_patch", "view", "rg", "glob"}
if not fingerprints or any(
        entry["count"] != 4 or {tool.split(":", 1)[0] for tool in entry["tools"]} != allowed
        for entry in fingerprints):
    raise ValueError("Unexpected actual model-visible tool catalog")
spans = []
for line in Path("/tmp/gh-aw/sandbox/firewall/logs/api-proxy-logs/otel.jsonl").read_text().splitlines():
    span = json.loads(line)
    if span.get("name") == "api_proxy.copilot.request":
        spans.append(span["attributes"])
if not spans or any(span.get("url.path") != "/responses"
                    or span.get("http.response.status_code") != 200 for span in spans):
    raise ValueError("Model did not use only successful native Responses requests")
verification = {
    "handoff_verified": True, "actual_cli": "1.0.85", "actual_catalogs": fingerprints,
    "actual_requests": [{"method": span.get("http.request.method"),
                         "path": span["url.path"], "status": span["http.response.status_code"]}
                        for span in spans],
    "sources": expected["sources"], "counts": expected["target_results"],
    "test_dispatches_agent_directed": False,
    "scope": "Hosted source/evidence reading and finish handoff, not agent-directed test initiation",
}
(ROOT / "handoff-verification.json").write_text(json.dumps(verification, indent=2) + "\n")
print(json.dumps(verification))

"""Offline process fixture: replace external services, not normal host machinery."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from test_normal_loop import NormalLoopTests, FEED, SESSION
import pilot_github
import pilot_state as state
import work_item_execution as execution
from test_work_items import result
import base64


def main():
    path, command = Path(sys.argv[1]), sys.argv[2]
    fixture = NormalLoopTests()
    fixture.setUp()
    try:
        saved = json.loads(path.read_text()) if path.exists() else {}
        if saved:
            fixture.remote.comments[0]["body"] = saved["authority"]
            for key, value in saved["values"].items():
                if not key.endswith("/issues/99/comments"):
                    fixture.remote.values[key] = value
            fixture.issue = fixture.remote.values[fixture.prefix + "/issues/20872"]
            fixture.posts, fixture.logs = saved["posts"], saved["logs"]
            fixture.issue_posts = saved.get("issue_posts", [])
            if saved.get("approval"):
                os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = saved["approval"]
            if saved.get("policy"):
                os.environ["CI_SHEPHERD_NORMAL_POLICY"] = saved["policy"]
        packet = saved.get("packet")
        with redirect_stdout(sys.stderr):
            if command in {"prepare", "prepare-feed"}:
                packet = fixture.prepare(FEED if command == "prepare-feed" else None)
                value = packet
            elif command == "settle-intake":
                value = fixture.settle(packet, fixture.intake(packet))
                saved["approval"] = json.dumps(value["approval_template"])
            elif command == "approve":
                os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = saved["approval"]
                value = {"outcome": "approved-offline"}
            elif command == "terminal":
                fixture.task("completed")
                record = fixture.saved()["workItems"][0]
                assignment = record["assignments"][0]
                claimed = result(assignment) | {"item_id": record["id"], "worker_id": SESSION,
                    "outcome": "no_fix", "changed_files": [], "pr_proposal": None,
                    "actual_classification": "inconclusive", "summary": "No justified fix."}
                fixture.logs = "CSWORKITEMBEGIN" + base64.b64encode(json.dumps(claimed).encode()).decode() + "CSWORKITEMEND"
                value = {"outcome": "terminal"}
            elif command == "settle-assessment":
                value = fixture.settle(packet, fixture.assessment(packet))
            elif command == "closed-empty-edit":
                fixture.add_pr(0)["state"] = "closed"
                fixture.issue["body"] += "\nAnother reported occurrence."
                fixture.issue["updated_at"] = "2026-10-05T00:00:00Z"
                value = {"outcome": "edited"}
            elif command == "grant-report":
                fixture.policy(allow_cloud_task=False, allow_branch_writes=False,
                               allow_draft_pr=False, track_linked_pr=False)
                value = {"outcome": "report-authorized"}
            elif command == "source-edit":
                fixture.issue["body"] += "\nA later occurrence."
                fixture.issue["updated_at"] = "2026-10-06T00:00:00Z"
                value = {"outcome": "source-edited"}
            elif command == "settle-crash-after-post":
                original = pilot_github.PilotGitHub.post_comment
                def crash(api, *args):
                    original(api, *args)
                    raise KeyboardInterrupt("after external POST before receipt")
                try:
                    with patch.object(pilot_github.PilotGitHub, "post_comment", crash):
                        fixture.settle(packet, fixture.assessment(packet))
                except KeyboardInterrupt:
                    value = {"outcome": "interrupted-after-post"}
            else:
                raise ValueError("unsupported fixture command")
        saved.update(authority=fixture.remote.comments[0]["body"], values=fixture.remote.values,
                     posts=fixture.posts, logs=fixture.logs, packet=packet,
                     issue_posts=fixture.issue_posts,
                     policy=os.environ.get("CI_SHEPHERD_NORMAL_POLICY"),
                     scratch={"tmpdir": os.environ["TMPDIR"], "root": str(fixture.root.resolve())},
                     approval=os.environ.get("CI_SHEPHERD_WORK_ITEM_APPROVAL") or saved.get("approval"))
        path.write_text(json.dumps(saved))
        print(json.dumps(value))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()

"""Offline process fixture: replace external services, not normal host machinery."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys

from test_normal_loop import NormalLoopTests, FEED, SESSION
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
            if saved.get("approval"):
                os.environ["CI_SHEPHERD_WORK_ITEM_APPROVAL"] = saved["approval"]
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
            else:
                raise ValueError("unsupported fixture command")
        saved.update(authority=fixture.remote.comments[0]["body"], values=fixture.remote.values,
                     posts=fixture.posts, logs=fixture.logs, packet=packet,
                     approval=os.environ.get("CI_SHEPHERD_WORK_ITEM_APPROVAL") or saved.get("approval"))
        path.write_text(json.dumps(saved))
        print(json.dumps(value))
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()

import argparse
import json
import os
from pathlib import Path
import sys

from helpers import decision_for, host_events, jsonl, wire_report

parser = argparse.ArgumentParser()
parser.add_argument("--session-id")
parser.add_argument("-C", type=Path)
args, _ = parser.parse_known_args()
if os.environ.get("COPILOT_MODEL") == "fixture-failure":
    sys.exit(7)

packet = json.loads((args.C / "packet.json").read_text())
events = host_events(args.session_id, decision_for(packet))
session = Path(os.environ["COPILOT_HOME"]) / "session-state" / args.session_id
session.mkdir(parents=True)
(session / "events.jsonl").write_text(jsonl([events[0], events[-2]]))
logs = args.C / "logs"
logs.mkdir()
(logs / "process-fixture.log").write_text(wire_report())
print(jsonl(events[1:]))

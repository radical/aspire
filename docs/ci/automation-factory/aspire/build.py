#!/usr/bin/env python3
"""Build or check standalone CIF prompts; never deploy or contact a service."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LANES = ("scout", "worker", "supervisor")
MAX_CHARACTERS = 12_000
MAX_INLINE_BYTES = 32_768


def build(check: bool) -> None:
    config = json.loads((ROOT / "configuration.json").read_text(encoding="utf-8"))
    if set(config["automations"]) != set(LANES):
        raise ValueError("Configuration must describe exactly the three CIF lanes")

    common = (ROOT / "common.md").read_text(encoding="utf-8").rstrip("\n")
    audit = (ROOT / "self-audit.md").read_text(encoding="utf-8")
    outputs = {}
    for lane in LANES:
        settings = config["automations"][lane]
        if settings["baseline"]["enabled"] is not False:
            raise ValueError(f"{lane}: baseline must explicitly remain disabled")
        text = common + "\n\n" + (ROOT / f"{lane}.md").read_text(encoding="utf-8").rstrip("\n") + "\n\n" + audit
        size = len(text.encode("utf-8"))
        if len(text) > MAX_CHARACTERS or size > MAX_INLINE_BYTES:
            raise ValueError(f"{lane}: ceiling exceeded: {len(text)} characters, {size} bytes")
        destination = ROOT / settings["prompt"]
        if destination != ROOT / "prompts" / f"{lane}.md":
            raise ValueError(f"{lane}: unexpected generated destination")
        if check and destination.read_bytes() != text.encode("utf-8"):
            raise ValueError(f"{lane}: stale generated prompt; run python3 build.py")
        outputs[destination] = text

    for destination, text in outputs.items():
        if not check:
            destination.parent.mkdir(exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        print(f"{destination.stem}: {len(text)} characters, "
              f"{len(text.encode('utf-8'))} UTF-8 bytes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Reject stale generated files")
    build(parser.parse_args().check)

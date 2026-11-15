#!/usr/bin/env python3
"""Authoring checker for the CI-health automation prompts.

What it does (deterministic, local, no network):
  * extracts the canonical recovery core from
    .agents/skills/automation-recovery/SKILL.md;
  * renders prompts/source/<role>.md by replacing the single
    {{RECOVERY_CORE}} placeholder, and compares the result with the rendered
    files in the output directory (or writes them with --write);
  * checks that every rendered prompt contains the identical core exactly once;
  * measures Unicode code points, UTF-16 code units and UTF-8 bytes against
    the authoring target and hard ceiling;
  * checks that every anchor phrase listed in the obligation preservation map
    (prompt-obligations.md) is literally present in the matching prompt.

What it does NOT do: it does not run, simulate or evaluate an automation.
Passing means the text is consistently assembled and within size limits and
that mapped anchor phrases were not accidentally deleted. It is not evidence
that any automation behaves correctly; that needs observed runs.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROLES = ("monitor", "worker", "reviewer", "supervisor")
MAP_COLUMNS = {"monitor": 1, "worker": 2, "reviewer": 3, "supervisor": 4}
PLACEHOLDER = "{{RECOVERY_CORE}}"
TARGET = 11400
CEILING = 12000

PROMPTS_DIR = Path(__file__).resolve().parent
GROUP_DIR = PROMPTS_DIR.parent
REPO_ROOT = GROUP_DIR.parents[2]
SKILL = REPO_ROOT / ".agents" / "skills" / "automation-recovery" / "SKILL.md"
OBLIGATIONS = GROUP_DIR / "prompt-obligations.md"


def read_text(path: Path) -> str:
    # newline="" keeps the exact bytes; prompts are compared byte-for-byte.
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def extract_core(skill_text: str) -> str:
    # The skill holds the core as:
    #   <!-- recovery-core:begin -->
    #   ```text
    #   RECOVERY CORE v1 ...
    #   ```
    #   <!-- recovery-core:end -->
    match = re.search(
        r"<!-- recovery-core:begin -->\n```text\n(.*?)\n```\n<!-- recovery-core:end -->",
        skill_text,
        re.DOTALL,
    )
    if match is None:
        raise SystemExit(f"error: recovery core markers not found in {SKILL}")
    return match.group(1)


def parse_obligations(map_text: str) -> list[tuple[str, dict[str, list[str]]]]:
    # Rows between the markers look like:
    #   | Obligation | `anchor a`; `anchor b` | `anchor` | n/a: reason | ... |
    # Each backticked phrase must occur verbatim in that role's prompt.
    # Cells without backticks (for example "n/a: ...") assert nothing.
    match = re.search(
        r"<!-- obligations:begin -->\n(.*?)<!-- obligations:end -->", map_text, re.DOTALL
    )
    if match is None:
        raise SystemExit(f"error: obligation markers not found in {OBLIGATIONS}")
    rows = []
    for line in match.group(1).splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Obligation" or len(cells) != 5:
            continue
        anchors = {role: re.findall(r"`([^`]+)`", cells[col]) for role, col in MAP_COLUMNS.items()}
        rows.append((cells[0], anchors))
    return rows


def sizes(text: str) -> tuple[int, int, int]:
    return len(text), len(text.encode("utf-16-le")) // 2, len(text.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="candidate", help="rendered prompt directory under prompts/ (default: candidate)")
    parser.add_argument("--write", action="store_true", help="write rendered prompts instead of only comparing")
    args = parser.parse_args()

    out_dir = PROMPTS_DIR / args.out
    core = extract_core(read_text(SKILL))
    obligations = parse_obligations(read_text(OBLIGATIONS))
    failures: list[str] = []
    warnings: list[str] = []

    print(f"core: {sizes(core)[0]} chars, {sizes(core)[2]} bytes")
    print(f"{'role':<11}{'codepoints':>11}{'utf16':>8}{'bytes':>8}  status")
    for role in ROLES:
        source = read_text(PROMPTS_DIR / "source" / f"{role}.md")
        if source.count(PLACEHOLDER) != 1:
            failures.append(f"{role}: source must contain {PLACEHOLDER} exactly once")
            continue
        rendered = source.replace(PLACEHOLDER, core)
        target_path = out_dir / f"{role}.md"
        if args.write:
            out_dir.mkdir(parents=True, exist_ok=True)
            with target_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(rendered)
        elif not target_path.exists() or read_text(target_path) != rendered:
            failures.append(f"{role}: {target_path.relative_to(REPO_ROOT)} differs from rendered source")

        if rendered.count(core) != 1:
            failures.append(f"{role}: rendered prompt must contain the recovery core exactly once")

        measured = sizes(rendered)
        largest = max(measured)
        status = "ok"
        if largest > CEILING:
            status = "OVER CEILING"
            failures.append(f"{role}: {largest} exceeds hard ceiling {CEILING}")
        elif largest > TARGET:
            status = "over target"
            warnings.append(f"{role}: {largest} exceeds authoring target {TARGET}")
        non_ascii = sorted({ch for ch in rendered if ord(ch) > 127})
        if non_ascii:
            status += f" (non-ASCII: {' '.join(non_ascii)})"
        print(f"{role:<11}{measured[0]:>11}{measured[1]:>8}{measured[2]:>8}  {status}")

        for obligation, anchors in obligations:
            for anchor in anchors[role]:
                if anchor not in rendered:
                    failures.append(f"{role}: obligation '{obligation}' anchor not found: {anchor!r}")

    checked = sum(len(a[r]) for _, a in obligations for r in ROLES)
    print(f"obligation rows: {len(obligations)}, anchors checked: {checked}")
    for message in warnings:
        print(f"warning: {message}")
    for message in failures:
        print(f"FAIL: {message}")
    print(
        "limits: text assembly, size and anchor presence only; "
        "this does not test or prove automation behavior."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

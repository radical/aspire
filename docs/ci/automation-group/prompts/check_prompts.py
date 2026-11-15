#!/usr/bin/env python3
"""Authoring checker for the CI-health automation prompts.

What it does (deterministic, local, no network):
  * extracts the canonical recovery core from
    .agents/skills/automation-recovery/SKILL.md and the canonical public
    report contract from .agents/skills/ci-health-report/SKILL.md;
  * renders prompts/source/<role>.md by replacing the single
    {{RECOVERY_CORE}} placeholder (every role) and the single
    {{REPORT_CONTRACT}} placeholder (monitor only; other roles must not
    carry it), and compares the result with the rendered files in the output
    directory (or writes them with --write);
  * checks that every rendered prompt contains each of its blocks exactly once;
  * measures Unicode code points, UTF-16 code units and UTF-8 bytes against
    the authoring target and hard ceiling;
  * checks that every anchor phrase listed in the obligation preservation map
    (prompt-obligations.md) is literally present in the matching prompt, and
    that every retired-reviewer obligation in the transfers table names a new
    owner whose prompt holds its anchors (or an explicit retired:/n/a: reason);
  * with --historical, re-renders the archived 4-role set (never rolled out)
    with its pinned RECOVERY CORE v2 copy and checks it is unchanged; the
    archived map is in git history only, so anchors are not checked in that
    mode.

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

ROLES = ("monitor", "worker", "supervisor")
MAP_COLUMNS = {"monitor": 1, "worker": 2, "supervisor": 3}
HISTORICAL_DIR = "archive-4role-9095dd85c"
HISTORICAL_ROLES = ("monitor", "worker", "reviewer", "supervisor")
PLACEHOLDER = "{{RECOVERY_CORE}}"
REPORT_PLACEHOLDER = "{{REPORT_CONTRACT}}"
REPORT_ROLES = ("monitor",)
# Advisory authoring target and hard ceiling, applied to the largest of code
# points, UTF-16 code units and UTF-8 bytes.
TARGET = 10500
CEILING = 12000

PROMPTS_DIR = Path(__file__).resolve().parent
GROUP_DIR = PROMPTS_DIR.parent
REPO_ROOT = GROUP_DIR.parents[2]
SKILL = REPO_ROOT / ".agents" / "skills" / "automation-recovery" / "SKILL.md"
REPORT_SKILL = REPO_ROOT / ".agents" / "skills" / "ci-health-report" / "SKILL.md"
OBLIGATIONS = GROUP_DIR / "prompt-obligations.md"
# The archived set was rendered with core v2; the skill now holds a newer core,
# so the archive keeps its own exact copy and stays byte-for-byte reproducible.
HISTORICAL_CORE = PROMPTS_DIR / HISTORICAL_DIR / "recovery-core-v2.txt"


def read_text(path: Path) -> str:
    # newline="" keeps the exact bytes; prompts are compared byte-for-byte.
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def extract_block(skill_text: str, marker: str, source: Path) -> str:
    # Each skill holds its canonical block as:
    #   <!-- <marker>:begin -->
    #   ```text
    #   RECOVERY CORE v3 ...      (or REPORT CONTRACT v1 ...)
    #   ```
    #   <!-- <marker>:end -->
    # Exactly one block is allowed so the prompts cannot silently pick one of
    # two diverging copies.
    pattern = rf"<!-- {re.escape(marker)}:begin -->\n```text\n(.*?)\n```\n<!-- {re.escape(marker)}:end -->"
    matches = re.findall(pattern, skill_text, re.DOTALL)
    if len(matches) != 1:
        raise SystemExit(f"error: expected exactly one {marker} block in {source}, found {len(matches)}")
    return matches[0]


def extract_core(skill_text: str) -> str:
    return extract_block(skill_text, "recovery-core", SKILL)


def render_role(role: str, source: str, blocks: dict[str, str], block_roles: dict[str, tuple[str, ...]]) -> tuple[str, list[str]]:
    # Replace each placeholder the role must carry; a role outside a block's
    # roles must not carry that placeholder (for example the report contract
    # belongs only to the monitor, which alone edits #22241).
    failures: list[str] = []
    rendered = source
    for placeholder, text in blocks.items():
        count = source.count(placeholder)
        if role in block_roles[placeholder]:
            if count != 1:
                failures.append(f"{role}: source must contain {placeholder} exactly once")
                continue
            rendered = rendered.replace(placeholder, text)
        elif count:
            failures.append(f"{role}: source must not contain {placeholder}")
    for placeholder, text in blocks.items():
        if role in block_roles[placeholder] and not failures and rendered.count(text) != 1:
            failures.append(f"{role}: rendered prompt must contain the {placeholder} block exactly once")
    return rendered, failures


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
    # Malformed rows are errors, not skips: silently dropping a row (for
    # example one with a missing role cell) would let a lost safeguard pass.
    rows = []
    errors = []
    for number, line in enumerate(match.group(1).splitlines(), start=1):
        if not line.strip():
            continue
        if not line.startswith("|"):
            errors.append(f"line {number}: not a table row: {line[:60]!r}")
            continue
        if set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Obligation":
            continue
        if len(cells) != len(MAP_COLUMNS) + 1 or not all(cells):
            errors.append(
                f"line {number}: expected {len(MAP_COLUMNS) + 1} non-empty cells, got {len(cells)}: {cells[0][:60]!r}"
            )
            continue
        anchors = {role: re.findall(r"`([^`]+)`", cells[col]) for role, col in MAP_COLUMNS.items()}
        for role, col in MAP_COLUMNS.items():
            # Each role cell must name an anchor or give an explicit "n/a: reason".
            if not anchors[role] and not cells[col].startswith("n/a:"):
                errors.append(f"line {number}: {role} cell has no anchor or 'n/a:' reason: {cells[0][:60]!r}")
        rows.append((cells[0], anchors))
    if not rows:
        errors.append("obligation map has no rows")
    elif not any(anchor for _, anchors in rows for values in anchors.values() for anchor in values):
        errors.append("obligation map has no anchors")
    if errors:
        raise SystemExit("error: malformed obligation map:\n  " + "\n  ".join(errors))
    return rows


def parse_transfers(map_text: str) -> list[tuple[str, str, list[str]]]:
    # Rows between the transfer markers look like:
    #   | Reviewer obligation | worker | `anchor a`; `anchor b` |
    #   | Reviewer obligation | retired: reason | n/a |
    # A role owner must list anchors present in that role's prompt; a
    # "retired:" or "n/a:" owner records a deliberate drop and lists none.
    match = re.search(
        r"<!-- transfers:begin -->\n(.*?)<!-- transfers:end -->", map_text, re.DOTALL
    )
    if match is None:
        raise SystemExit(f"error: transfer markers not found in {OBLIGATIONS}")
    rows = []
    errors = []
    for number, line in enumerate(match.group(1).splitlines(), start=1):
        if not line.strip() or set(line) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] == "Reviewer obligation":
            continue
        if len(cells) != 3 or not all(cells):
            errors.append(f"line {number}: expected 3 non-empty cells: {cells[0][:60]!r}")
            continue
        anchors = re.findall(r"`([^`]+)`", cells[2])
        if cells[1] in ROLES:
            if not anchors:
                errors.append(f"line {number}: owner {cells[1]} needs anchors: {cells[0][:60]!r}")
        elif cells[1].startswith(("retired:", "n/a:")):
            if anchors:
                errors.append(f"line {number}: retired row must not list anchors: {cells[0][:60]!r}")
        else:
            errors.append(f"line {number}: owner must be a role or 'retired:'/'n/a:' reason: {cells[0][:60]!r}")
        rows.append((cells[0], cells[1], anchors))
    if not rows:
        errors.append("transfers table has no rows")
    if errors:
        raise SystemExit("error: malformed transfers table:\n  " + "\n  ".join(errors))
    return rows


def sizes(text: str) -> tuple[int, int, int]:
    return len(text), len(text.encode("utf-16-le")) // 2, len(text.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="candidate", help="rendered prompt directory under prompts/ (default: candidate)")
    parser.add_argument("--write", action="store_true", help="write rendered prompts instead of only comparing")
    parser.add_argument(
        "--historical",
        action="store_true",
        help=f"check the archived 4-role set under prompts/{HISTORICAL_DIR}/ (size/core/render only)",
    )
    args = parser.parse_args()

    if args.historical:
        core = read_text(HISTORICAL_CORE).removesuffix("\n")
    else:
        core = extract_core(read_text(SKILL))
    blocks = {PLACEHOLDER: core}
    block_roles: dict[str, tuple[str, ...]] = {PLACEHOLDER: HISTORICAL_ROLES}
    if args.historical:
        roles = HISTORICAL_ROLES
        source_dir = PROMPTS_DIR / HISTORICAL_DIR / "source"
        out_dir = PROMPTS_DIR / HISTORICAL_DIR / "candidate"
        obligations: list[tuple[str, dict[str, list[str]]]] = []
        transfers: list[tuple[str, str, list[str]]] = []
    else:
        roles = ROLES
        source_dir = PROMPTS_DIR / "source"
        out_dir = PROMPTS_DIR / args.out
        map_text = read_text(OBLIGATIONS)
        obligations = parse_obligations(map_text)
        transfers = parse_transfers(map_text)
        blocks[REPORT_PLACEHOLDER] = extract_block(read_text(REPORT_SKILL), "report-contract", REPORT_SKILL)
        block_roles[REPORT_PLACEHOLDER] = REPORT_ROLES
    rendered_by_role: dict[str, str] = {}
    failures: list[str] = []
    warnings: list[str] = []

    for placeholder, text in blocks.items():
        print(f"{placeholder}: {sizes(text)[0]} chars, {sizes(text)[2]} bytes")
    print(f"{'role':<11}{'codepoints':>11}{'utf16':>8}{'bytes':>8}  status")
    for role in roles:
        source = read_text(source_dir / f"{role}.md")
        rendered, role_failures = render_role(role, source, blocks, block_roles)
        if role_failures:
            failures.extend(role_failures)
            continue
        rendered_by_role[role] = rendered
        target_path = out_dir / f"{role}.md"
        if args.write:
            out_dir.mkdir(parents=True, exist_ok=True)
            with target_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write(rendered)
        elif not target_path.exists() or read_text(target_path) != rendered:
            failures.append(f"{role}: {target_path.relative_to(REPO_ROOT)} differs from rendered source")

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

    for obligation, owner, anchors in transfers:
        for anchor in anchors:
            if owner in rendered_by_role and anchor not in rendered_by_role[owner]:
                failures.append(f"{owner}: transfer '{obligation}' anchor not found: {anchor!r}")

    if args.historical:
        print("historical: archived 4-role set; anchors not checked (map in git history)")
    else:
        checked = sum(len(a[r]) for _, a in obligations for r in ROLES)
        transferred = sum(len(a) for _, _, a in transfers)
        retired = sum(1 for _, owner, _ in transfers if owner not in ROLES)
        print(f"obligation rows: {len(obligations)}, anchors checked: {checked}")
        print(f"reviewer transfers: {len(transfers)} rows, {transferred} anchors, {retired} retired")
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

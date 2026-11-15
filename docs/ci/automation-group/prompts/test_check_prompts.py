"""Behavioral checks for the prompt renderer in check_prompts.py.

Run from the repository root:
    python3 -m unittest docs/ci/automation-group/prompts/test_check_prompts.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_prompts as cp  # noqa: E402

CORE = "{{RECOVERY_CORE}}"
REPORT = "{{REPORT_CONTRACT}}"
BLOCK_ROLES = {CORE: cp.ROLES, REPORT: cp.REPORT_ROLES}
BLOCKS = {CORE: "CORE TEXT", REPORT: "REPORT TEXT"}


def skill(*bodies: str) -> str:
    return "".join(f"<!-- report-contract:begin -->\n```text\n{body}\n```\n<!-- report-contract:end -->\n" for body in bodies)


class RenderRoleTests(unittest.TestCase):
    def test_monitor_embeds_both_blocks_once(self):
        rendered, failures = cp.render_role("monitor", f"a\n{CORE}\nb\n{REPORT}\n", BLOCKS, BLOCK_ROLES)
        self.assertEqual(failures, [])
        self.assertEqual(rendered, "a\nCORE TEXT\nb\nREPORT TEXT\n")

    def test_report_contract_rejected_outside_monitor(self):
        for role in ("worker", "supervisor"):
            _, failures = cp.render_role(role, f"{CORE}\n{REPORT}\n", BLOCKS, BLOCK_ROLES)
            self.assertEqual(failures, [f"{role}: source must not contain {REPORT}"])

    def test_monitor_without_report_contract_fails(self):
        _, failures = cp.render_role("monitor", f"{CORE}\n", BLOCKS, BLOCK_ROLES)
        self.assertEqual(failures, [f"monitor: source must contain {REPORT} exactly once"])

    def test_duplicate_placeholder_fails(self):
        _, failures = cp.render_role("worker", f"{CORE}\n{CORE}\n", BLOCKS, BLOCK_ROLES)
        self.assertEqual(failures, [f"worker: source must contain {CORE} exactly once"])

    def test_block_text_already_in_source_fails(self):
        # A hand-pasted stale copy next to the placeholder would make two diverging copies.
        _, failures = cp.render_role("worker", f"CORE TEXT\n{CORE}\n", BLOCKS, BLOCK_ROLES)
        self.assertEqual(failures, [f"worker: rendered prompt must contain the {CORE} block exactly once"])


class ExtractBlockTests(unittest.TestCase):
    def test_single_block_extracted(self):
        self.assertEqual(cp.extract_block(skill("line1\nline2"), "report-contract", Path("s")), "line1\nline2")

    def test_missing_or_duplicate_block_rejected(self):
        for text in ("", skill("a", "b")):
            with self.assertRaises(SystemExit):
                cp.extract_block(text, "report-contract", Path("s"))


if __name__ == "__main__":
    unittest.main()

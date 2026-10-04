"""Exercise the emitted budget config without launching AWF or inference."""

import json
import os
import re
import subprocess
import unittest

from helpers import WorkspaceTest, compiled_step


def expression(value, context):
    """Evaluate the emitted Actions string/boolean subset, not Python eval."""
    tokens = re.findall(r"'[^']*'|[A-Za-z_][A-Za-z0-9_.]*|==|&&|\|\||[()]", value[3:-2].strip())
    index = 0

    def primary():
        nonlocal index
        token = tokens[index]
        index += 1
        if token == "(":
            result = either()
            if tokens[index] != ")":
                raise ValueError("invalid expression")
            index += 1
            return result
        return token[1:-1] if token.startswith("'") else context.get(token, "")

    def equal():
        nonlocal index
        result = primary()
        while index < len(tokens) and tokens[index] == "==":
            index += 1
            result = result == primary()
        return result

    def both():
        nonlocal index
        result = equal()
        while index < len(tokens) and tokens[index] == "&&":
            index += 1
            right = equal()
            result = right if result else result
        return result

    def either():
        nonlocal index
        result = both()
        while index < len(tokens) and tokens[index] == "||":
            index += 1
            right = both()
            result = result if result else right
        return result

    result = either()
    if index != len(tokens):
        raise ValueError("unsupported expression")
    return result


class PilotHostedBudgetTests(WorkspaceTest, unittest.TestCase):
    def test_emitted_prepare_and_settle_route_manual_only_target_without_schedule_drift(self):
        for step_name in ("Prepare host-owned envelope", "Settle native billing before authorizing an action"):
            step = compiled_step(step_name)
            for event, requested, expected in (
                    ("workflow_dispatch", "upstream-20722", "upstream-20722"),
                    ("workflow_dispatch", "", "fork"), ("schedule", "upstream-20722", "fork")):
                with self.subTest(step=step_name, event=event, requested=requested):
                    self.assertEqual(expected, expression(step["env"]["SHEPHERD_TARGET"], {
                        "github.event_name": event, "inputs.target": requested}))

    def test_emitted_config_admits_thirty_pilot_five_legacy_ignoring_repo_default(self):
        step = compiled_step("Execute GitHub Copilot CLI")
        lines = step["run"].splitlines()
        start = next(index for index, line in enumerate(lines) if line.strip().startswith('GH_AW_MAX_AI_CREDITS='))
        end = next(index for index in range(start, len(lines)) if lines[index].strip().startswith("printf ")
                   and "awf-config.json" in lines[index])
        script = "\n".join(lines[start:end + 1])
        root = self.work.resolve()
        (root / "gh-aw").mkdir()
        for mode, event, expected in (("pilot", "workflow_dispatch", 30), ("", "schedule", 30),
                                      ("live", "workflow_dispatch", 5), ("observe", "workflow_dispatch", 5),
                                      ("transport-proof", "workflow_dispatch", 5)):
            with self.subTest(mode=mode, event=event):
                limit = expression(step["env"]["GH_AW_MAX_AI_CREDITS"], {
                    "inputs.mode": mode, "github.event_name": event, "vars.GH_AW_DEFAULT_MAX_AI_CREDITS": "9000"})
                completed = subprocess.run(["bash", "-c", script], cwd=root,
                    env={"PATH": os.defpath, "RUNNER_TEMP": str(root), "GH_AW_MAX_AI_CREDITS": str(limit)},
                    capture_output=True, text=True, check=False)
                self.assertEqual(0, completed.returncode, completed.stderr)
                config = json.loads((root / "gh-aw" / "awf-config.json").read_text())
                self.assertEqual(expected, config["apiProxy"]["maxAiCredits"])

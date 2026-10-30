"""Strict exact-target accounting shared by trusted preparation and local checks."""

import xml.etree.ElementTree as ET

CLASS = "Aspire.Hosting.RemoteHost.Tests.IntegrationHostLauncherTests"
METHOD = "InitializeHostsAsync_WaitsForEveryRegistrationBeforeDiscovery"


def read_target(path, case):
    tree = ET.parse(path)
    definitions = tree.findall(".//{*}TestDefinitions/{*}UnitTest")
    ids = []
    for definition in definitions:
        method = definition.find("{*}TestMethod")
        if method is not None and method.get("className") == CLASS and method.get("name") == METHOD:
            ids.append(definition.get("id"))
    results = tree.findall(".//{*}Results/{*}UnitTestResult")
    targets = [row for row in results if row.get("testId") in ids]
    if len(ids) != 1 or len(targets) != 1 or targets[0].get("outcome") not in {"Passed", "Failed"}:
        raise ValueError("Target must be defined and executed exactly once, not skipped")
    target = targets[0]
    passed = sum(row.get("outcome") == "Passed" for row in results)
    failed = sum(row.get("outcome") == "Failed" for row in results)
    counters = tree.find(".//{*}ResultSummary/{*}Counters")
    expected = {"total": len(results), "passed": passed, "failed": failed,
                "executed": passed + failed}
    if counters is None or any(int(counters.get(k, "-1")) != v for k, v in expected.items()):
        raise ValueError("TRX counters disagree with actual test records")
    if len(results) != 675:
        raise ValueError(f"Unexpected suite size {len(results)}; expected exact-source675")
    error = target.find(".//{*}ErrorInfo/{*}Message")
    stack = target.find(".//{*}ErrorInfo/{*}StackTrace")
    message = error.text if error is not None else None
    frames = [line.strip() for line in (stack.text or "").splitlines()
              if line.strip()] if stack is not None else []
    frame = frames[0] if frames else None
    line = 124 if case == "before" else 129
    original = (target.get("outcome") == "Failed" and
                message == "System.TimeoutException : The operation has timed out." and
                frame is not None and METHOD in frame and
                f"IntegrationHostLauncherTests.cs:line {line}" in frame)
    return {"target_outcome": target.get("outcome"),
            "target_duration": target.get("duration"),
            "original_timeout": original, "target_error": message,
            "target_first_frame": frame,
            "other_failures": failed - int(target.get("outcome") == "Failed"),
            "suite_total": len(results), "suite_passed": passed,
            "suite_failed": failed, "suite_skipped": len(results) - passed - failed}

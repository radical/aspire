// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace KnownTestFailures;

public static class Evaluator
{
    public const int BlockedExitCode = 3;

    // Loaders deliberately execute inside the fail-closed boundary, and never execute in off mode.
    public static Decision Evaluate(
        string mode, int originalExitCode, DateTimeOffset evaluatedAt, bool experimentalOptIn,
        Func<RegistryInput> loadRegistry, Func<Evidence> loadEvidence, Func<string, string> readArtifact)
    {
        Format.Require(mode is "off" or "shadow" or "experimental", "Unknown evaluation mode.");
        if (mode == "off")
        {
            return new(mode, "off", originalExitCode, originalExitCode, evaluatedAt, [], [], []);
        }

        RegistryInput? input = null;
        Evidence? evidence = null;
        var artifacts = new List<Artifact>();
        var matches = new List<Match>();
        try
        {
            Format.Utc(evaluatedAt, "evaluatedAt");
            Format.Require(mode != "experimental" || experimentalOptIn, "Experimental replay requires explicit opt-in.");
            input = loadRegistry();
            var entries = RegistryHistory.Replay(input.Registry);
            Format.Require(input.Registry.History.All(e => e.At <= evaluatedAt), "Registry contains future history/approval.");
            evidence = loadEvidence();
            ValidateEvidence(evidence);
            var failures = 0;
            foreach (var invocation in evidence.Invocations)
            {
                var xml = readArtifact(invocation.TrxPath);
                artifacts.Add(new(invocation.Id, invocation.TrxPath, xml));
                var results = StrictTrx.Read(xml);
                Format.Require(invocation.DirectMtpProcess, "Exit codes must come from the direct MTP test process, not a wrapper.");
                Format.Require(invocation.SetupSucceeded && invocation.CompletedNormally && invocation.Diagnostics.Length == 0,
                    "Setup, crash, or completion diagnostics block evaluation.");
                var byTestId = invocation.ExpectedCases.ToDictionary(c => c.TestId, StringComparer.Ordinal);
                Format.Require(results.Length == byTestId.Count && results.All(r => byTestId.ContainsKey(r.TestId)),
                    "Discovery inventory and TRX results differ (missing/extra cases).");
                var failed = results.Count(r => r.Outcome == "Failed");
                // MTP: 0 = success, 2 = test failures; every other code is nonwaivable.
                // https://learn.microsoft.com/dotnet/core/testing/microsoft-testing-platform-troubleshooting#exit-codes
                Format.Require(invocation.ProcessExitCode == (failed == 0 ? 0 : 2),
                    "Direct MTP process exit is nonwaivable or contradicts TRX.");
                foreach (var result in results)
                {
                    var expected = byTestId[result.TestId];
                    Format.Require(expected.Method == result.Method && expected.DisplayName == result.DisplayName,
                        "Discovered case identity does not match TRX.");
                    if (result.Outcome == "Passed")
                    {
                        continue;
                    }
                    failures++;
                    var eligible = entries.Values.Where(entry =>
                        entry.State == "active" &&
                        entry.Approval is { } approval && approval.ContentRevision == entry.ContentRevision &&
                        entry.Content.CreatedAt <= approval.ApprovedAt && approval.ApprovedAt <= evaluatedAt &&
                        evaluatedAt < entry.Content.ExpiresAt &&
                        entry.Content.Project == invocation.Project &&
                        entry.Content.Method == result.Method && entry.Content.DisplayName == result.DisplayName &&
                        entry.Content.CaseId == expected.CaseId &&
                        entry.Content.Lane == evidence.Lane && entry.Content.Platform == evidence.Platform &&
                        entry.Content.Ref == evidence.Ref && entry.Content.Message == result.Message).ToArray();
                    Format.Require(eligible.Length == 1, $"Failure '{invocation.Id}/{expected.CaseId}' has no unique eligible allowance.");
                    matches.Add(new(invocation.Id, expected.CaseId, eligible[0].Id, eligible[0].ContentRevision));
                }
            }
            Format.Require(originalExitCode == (failures == 0 ? 0 : 2),
                "Original aggregate exit contradicts the supported 0=success, 2=test-failure contract.");
            return new(mode, failures == 0 ? "pass" : "allowed", originalExitCode,
                mode == "shadow" ? originalExitCode : 0, evaluatedAt, [.. artifacts], [.. matches], [],
                input.Source, input.RawJson, evidence);
        }
        catch (Exception ex) when (ex is InvalidDataException or System.Text.Json.JsonException or
            IOException or UnauthorizedAccessException or System.Xml.XmlException or ArgumentException or
            InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            return new(mode, "blocked", originalExitCode,
                mode == "shadow" || originalExitCode != 0 ? originalExitCode : BlockedExitCode,
                evaluatedAt, [.. artifacts], [.. matches], [ex.Message], input?.Source, input?.RawJson, evidence);
        }
    }

    private static void ValidateEvidence(Evidence evidence)
    {
        Format.Require(evidence.SchemaVersion == 1, "Unsupported evidence schemaVersion.");
        Format.Text(evidence.Lane, "lane");
        Format.Text(evidence.Platform, "platform");
        Format.FullRef(evidence.Ref);
        Format.Require(evidence.SelectedInvocations.Length > 0 &&
            evidence.SelectedInvocations.Distinct(StringComparer.Ordinal).Count() == evidence.SelectedInvocations.Length &&
            evidence.Invocations.Select(i => i.Id).Distinct(StringComparer.Ordinal).Count() == evidence.Invocations.Length &&
            evidence.SelectedInvocations.ToHashSet(StringComparer.Ordinal).SetEquals(evidence.Invocations.Select(i => i.Id)),
            "Selected invocation inventory must be nonempty and match invocations one-to-one.");
        var paths = new HashSet<string>(StringComparer.Ordinal);
        var identities = new HashSet<(string, string)>();
        foreach (var invocation in evidence.Invocations)
        {
            Format.Text(invocation.Id, "invocation id");
            Format.Path(invocation.Project, "project");
            Format.Require(invocation.Project.EndsWith(".csproj", StringComparison.Ordinal), "Only managed csproj invocations are supported.");
            Format.Path(invocation.TrxPath, "trxPath");
            Format.Require(paths.Add(invocation.TrxPath), "Duplicate TRX artifact.");
            Format.Require(invocation.ExpectedCases.Length > 0 &&
                invocation.ExpectedCases.Select(c => c.TestId).Distinct(StringComparer.Ordinal).Count() == invocation.ExpectedCases.Length,
                "Discovery inventory must be nonempty with unique testId values.");
            foreach (var item in invocation.ExpectedCases)
            {
                Format.Text(item.CaseId, "caseId");
                Format.Text(item.TestId, "testId");
                Format.Text(item.Method, "method");
                Format.Text(item.DisplayName, "displayName");
                Format.Require(identities.Add((invocation.Project, item.CaseId)), "Duplicate project/caseId inventory identity.");
            }
        }
    }
}

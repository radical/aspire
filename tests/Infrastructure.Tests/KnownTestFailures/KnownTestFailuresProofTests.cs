// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using System.Text.Json;
using System.Xml.Linq;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public sealed class KnownTestFailuresProofTests(ITestOutputHelper output)
{
    [Fact]
    public async Task RealFixtureEntryPointsPreserveRawFailuresAndRequireFreshApprovedRegistry()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var build = await ProcessRunner.RunAsync(output, "dotnet",
            ["build", Path.Combine(RepoRoot.Path, ProofRunner.Project), "--nologo", "-v:q"],
            RepoRoot.Path, new Dictionary<string, string> { ["MSBUILDTERMINALLOGGER"] = "false" });
        Assert.Equal(0, build.ExitCode);
        var executable = Path.Combine(RepoRoot.Path, "artifacts", "bin", "KnownTestFailuresFixture", "Debug", "net11.0",
            OperatingSystem.IsWindows() ? "KnownTestFailuresFixture.exe" : "KnownTestFailuresFixture");
        var safe = await ProcessRunner.RunAsync(output, executable,
            ["--results-directory", Path.Combine(fixture.Path, "safe"), "--no-ansi", "--progress", "off"],
            RepoRoot.Path, new Dictionary<string, string>
            {
                ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
                ["KNOWN_FAILURE_PROOF_OPT_IN"] = "false"
            });
        Assert.Equal(0, safe.ExitCode);

        var proof = Path.Combine(fixture.Path, "proof");
        var proven = await fixture.Cli("prove", "--repo", RepoRoot.Path, "--fixture", executable, "--output", proof);
        Assert.Equal(0, proven.Exit);
        using var summary = JsonDocument.Parse(File.ReadAllText(Path.Combine(proof, "scenario-summary.json")));
        Assert.Equal(16, summary.RootElement.GetArrayLength());
        var approved = Format.Read<Decision>(File.ReadAllText(Path.Combine(proof, "approved", "decision.json")));
        Assert.Equal(2, approved.OriginalExitCode);
        Assert.Equal(0, approved.EffectiveExitCode);
        Assert.Single(approved.Matches);
        var mixed = Format.Read<Decision>(File.ReadAllText(Path.Combine(proof, "mixed", "decision.json")));
        Assert.Equal(2, mixed.OriginalExitCode);
        Assert.Equal(2, mixed.EffectiveExitCode);
        Assert.Equal("blocked", mixed.Status);

        var lifecycle = XDocument.Load(Path.Combine(proof, "approved", "lifecycle.xml"));
        var results = StrictTrx.Read(File.ReadAllText(Path.Combine(proof, "approved", "results.trx")));
        lifecycle.Root!.Element("assembly")!.Attribute("finish-rtf")!.Remove();
        Assert.Throws<InvalidDataException>(() => ProofRunner.ValidateLifecycle(lifecycle.ToString(), results));
        lifecycle = XDocument.Load(Path.Combine(proof, "approved", "lifecycle.xml"));
        lifecycle.Root!.Element("assembly")!.SetAttributeValue("errors", "1");
        Assert.Throws<InvalidDataException>(() => ProofRunner.ValidateLifecycle(lifecycle.ToString(), results));
        var trx = XDocument.Load(Path.Combine(proof, "approved", "results.trx"));
        XNamespace ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010";
        trx.Root!.Element(ns + "TestEntries")!.Elements().First().SetAttributeValue("executionId", "incorrect");
        Assert.Throws<InvalidDataException>(() => StrictTrx.Read(trx.ToString()));

        // Seed and gate use their actual CLI entry points, including two discovery
        // processes, direct MTP execution and a new fetch on EVERY invocation.
        var seedPath = Path.Combine(fixture.Path, "seed");
        var seeded = await fixture.Cli("seed", "--repo", RepoRoot.Path, "--fixture", executable, "--output", seedPath,
            "--expires", DateTimeOffset.UtcNow.AddHours(2).ToString("O"), "--issue", "https://example.com/experimental-proof",
            "--owner", "test-owner", "--platform", RuntimeInformation.RuntimeIdentifier);
        Assert.Equal(0, seeded.Exit);
        var add = Format.Read<RegistryEvent>(File.ReadAllText(Path.Combine(seedPath, "add.json")));
        Assert.Equal(approved.Evidence!.Invocations[0].ExpectedCases.Single(c => c.Method.EndsWith(".Candidate", StringComparison.Ordinal)).CaseId,
            add.Content!.CaseId);
        fixture.InitializeGit();
        var remote = Path.Combine(fixture.Path, "source");
        GitCli.Run(fixture.Path, "init", "--initial-branch=main", remote);
        var revision = await GitRegistry.MutateAsync(remote, ProofRunner.RegistryRef, new string('0', 40), true, add);
        async Task<(int Exit, string Stdout, string Stderr)> Gate(string name) =>
            await fixture.Cli("gate", "--repo", fixture.Path, "--fixture", executable, "--output", Path.Combine(fixture.Path, name), "--remote", remote);
        Assert.Equal(2, (await Gate("unapproved-gate")).Exit);
        var approval = new RegistryEvent(2, add.Id, "approve", "test-reviewer", "Reviewed exact fixed fixture failure.",
            DateTimeOffset.UtcNow, 1);
        revision = await GitRegistry.MutateAsync(remote, ProofRunner.RegistryRef, revision, false, approval);
        Assert.Equal(0, (await Gate("approved-gate")).Exit);
        var gateDecision = Format.Read<Decision>(File.ReadAllText(Path.Combine(fixture.Path, "approved-gate", "decision.json")));
        Assert.Equal(revision, gateDecision.RegistrySource!.Commit);
        Assert.Equal(2, gateDecision.OriginalExitCode);
        // The cached tracking ref still points at an approved registry. A failed
        // fresh fetch must nevertheless block rather than read that stale commit.
        GitCli.Run(remote, "update-ref", "-d", ProofRunner.RegistryRef, revision);
        Assert.Equal(3, (await Gate("failed-fetch-gate")).Exit);
        Assert.True(File.Exists(Path.Combine(fixture.Path, "failed-fetch-gate", "blocked.json")));
    }
}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public class KnownTestFailuresCliTests(ITestOutputHelper output)
{
    [Fact]
    public async Task DefaultOffIgnoresInvalidArtifactPathsAndTimestamp()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var result = await fixture.Cli("evaluate", "--original-exit", "7", "--registry", "does-not-exist",
            "--evidence", "does-not-exist", "--at", "invalid");
        Assert.Equal(7, result.Exit);
        var report = Format.Read<Decision>(result.Stdout);
        Assert.Equal("off", report.Status);
        Assert.Equal(7, report.OriginalExitCode);
        Assert.Equal(7, report.EffectiveExitCode);
        Assert.Equal("", result.Stderr);
    }

    [Theory]
    [InlineData("shadow", 0, 0)]
    [InlineData("shadow", 7, 7)]
    [InlineData("experimental", 0, 3)]
    [InlineData("experimental", 7, 7)]
    public async Task ActiveReaderErrorsHaveObservableSafeExits(string mode, int original, int expected)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var result = await fixture.Cli("evaluate", "--mode", mode, "--opt-in",
            "--original-exit", original.ToString(System.Globalization.CultureInfo.InvariantCulture),
            "--at", "2026-10-20T12:00:00Z", "--registry", Path.Combine(fixture.Path, "missing"));
        Assert.Equal(expected, result.Exit);
        var report = Format.Read<Decision>(result.Stdout);
        Assert.Equal("blocked", report.Status);
        Assert.Equal(original, report.OriginalExitCode);
        Assert.Equal(expected, report.EffectiveExitCode);
        Assert.False(string.IsNullOrWhiteSpace(result.Stderr));
    }

    [Theory]
    [InlineData("shadow", 0)]
    [InlineData("experimental", 3)]
    public async Task GitProcessStartErrorsAreSafeDecisions(string mode, int expected)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var result = await fixture.Cli("evaluate", "--mode", mode, "--opt-in", "--original-exit", "0",
            "--at", "2026-10-20T12:00:00Z", "--repo", Path.Combine(fixture.Path, "missing-repository"),
            "--ref", "refs/heads/allowances", "--registry-path", "registry.json");
        Assert.Equal(expected, result.Exit);
        Assert.Equal("blocked", Format.Read<Decision>(result.Stdout).Status);
        Assert.False(string.IsNullOrWhiteSpace(result.Stderr));
    }

    [Fact]
    public async Task ReplayWritesDecisionAndNeverRewritesRawInputs()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var trx = fixture.Trx();
        var registryPath = Path.Combine(fixture.Path, "registry.json");
        var evidencePath = Path.Combine(fixture.Path, "evidence.json");
        var reportPath = Path.Combine(fixture.Path, "report.json");
        var registry = Format.Write(KnownTestFailuresFixture.Registry());
        var evidence = Format.Write(KnownTestFailuresFixture.Evidence());
        File.WriteAllText(registryPath, registry);
        File.WriteAllText(evidencePath, evidence);
        var result = await fixture.Cli("evaluate", "--mode", "experimental", "--opt-in", "--original-exit", "2",
            "--at", "2026-10-20T12:00:00Z", "--registry", registryPath, "--evidence", evidencePath,
            "--artifact-root", fixture.Path, "--report", reportPath);
        Assert.Equal((0, "", ""), result);
        var report = Format.Read<Decision>(File.ReadAllText(reportPath));
        Assert.Equal("allowed", report.Status);
        Assert.Equal("local-untrusted", report.RegistrySource!.Kind);
        Assert.Equal(new Match("invocation-1", "stable-case-1", "allow-1", 1), Assert.Single(report.Matches));
        Assert.Equal(registry, File.ReadAllText(registryPath));
        Assert.Equal(evidence, File.ReadAllText(evidencePath));
        Assert.Equal(trx, File.ReadAllText(Path.Combine(fixture.Path, "results.trx")));
    }

    [Theory]
    [InlineData("replay")]
    [InlineData("shadow")]
    public async Task ReportWriteErrorsDoNotBecomeReplaySuccess(string mode)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var registry = Path.Combine(fixture.Path, "registry.json");
        var evidence = Path.Combine(fixture.Path, "evidence.json");
        File.WriteAllText(registry, Format.Write(KnownTestFailuresFixture.Registry()));
        File.WriteAllText(evidence, Format.Write(KnownTestFailuresFixture.Evidence()));
        fixture.Trx();
        var result = await fixture.Cli("evaluate", "--mode", mode == "replay" ? "experimental" : "shadow",
            "--opt-in", "--original-exit", "0", "--at", "2026-10-20T12:00:00Z", "--registry", registry,
            "--evidence", evidence, "--artifact-root", fixture.Path, "--report", fixture.Path);
        Assert.Equal(mode == "shadow" ? 0 : 3, result.Exit);
        Assert.False(string.IsNullOrWhiteSpace(result.Stderr));
    }

    [Theory]
    [InlineData("registry")]
    [InlineData("evidence")]
    [InlineData("trx")]
    public async Task ReportsCannotOverwriteRawEvidence(string destination)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var registryPath = Path.Combine(fixture.Path, "registry.json");
        var evidencePath = Path.Combine(fixture.Path, "evidence.json");
        var trxPath = Path.Combine(fixture.Path, "results.trx");
        var registry = Format.Write(KnownTestFailuresFixture.Registry());
        var evidence = Format.Write(KnownTestFailuresFixture.Evidence());
        File.WriteAllText(registryPath, registry);
        File.WriteAllText(evidencePath, evidence);
        var trx = fixture.Trx();
        var reportPath = destination switch
        {
            "registry" => registryPath,
            "evidence" => evidencePath,
            _ => trxPath
        };
        var result = await fixture.Cli("evaluate", "--mode", "experimental", "--opt-in", "--original-exit", "2",
            "--at", "2026-10-20T12:00:00Z", "--registry", registryPath, "--evidence", evidencePath,
            "--artifact-root", fixture.Path, "--report", reportPath);
        Assert.Equal(2, result.Exit);
        Assert.False(string.IsNullOrWhiteSpace(result.Stderr));
        Assert.Equal(registry, File.ReadAllText(registryPath));
        Assert.Equal(evidence, File.ReadAllText(evidencePath));
        Assert.Equal(trx, File.ReadAllText(trxPath));
    }

    [Fact]
    public async Task ManagementCliInitializesAndAppendsUsingExpectedRevision()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        var eventPath = Path.Combine(fixture.Path, "event.json");
        File.WriteAllText(eventPath, Format.Write(KnownTestFailuresFixture.Event("add")));
        var result = await fixture.Cli("mutate", "--repo", fixture.Path, "--ref", "refs/heads/allowances",
            "--expected", new string('0', 40), "--initialize", "--event", eventPath);
        Assert.Equal(0, result.Exit);
        Assert.Equal("", result.Stderr);
        var commit = result.Stdout.Trim();
        Assert.Equal(commit, GitCli.Run(fixture.Path, "rev-parse", "refs/heads/allowances"));
        var input = await GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/allowances", "registry.json");
        Assert.Equal(KnownTestFailuresFixture.Content, RegistryHistory.Replay(input.Registry)["allow-1"].Content);
        File.WriteAllText(eventPath, Format.Write(KnownTestFailuresFixture.Event("approve", 2)));
        result = await fixture.Cli("mutate", "--repo", fixture.Path, "--ref", "refs/heads/allowances",
            "--expected", commit, "--event", eventPath);
        Assert.Equal(0, result.Exit);
        input = await GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/allowances", "registry.json");
        Assert.NotNull(RegistryHistory.Replay(input.Registry)["allow-1"].Approval);
    }

    [Fact]
    public async Task UnknownAndDuplicateOptionsFailWithHelpAvailable()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var result = await fixture.Cli("evaluate", "--original-exit", "0", "--unknown", "value");
        Assert.Equal(3, result.Exit);
        Assert.False(string.IsNullOrWhiteSpace(result.Stderr));
        result = await fixture.Cli("evaluate", "--original-exit", "0", "--original-exit", "2");
        Assert.Equal(3, result.Exit);
        result = await fixture.Cli("--help");
        Assert.Equal((0, KnownTestFailures.Program.Help + Environment.NewLine, ""), result);
    }

    [Theory]
    [InlineData("off", 7, 7, "off")]
    [InlineData("shadow", 0, 0, "blocked")]
    [InlineData("experimental", 0, 3, "blocked")]
    public async Task ExecutableMainPreservesObservableModeExits(string mode, int original, int expected, string status)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var result = await ProcessRunner.RunAsync(output, "dotnet",
            [typeof(KnownTestFailures.Program).Assembly.Location, "evaluate", "--mode", mode, "--opt-in",
                "--original-exit", original.ToString(System.Globalization.CultureInfo.InvariantCulture),
                "--at", "2026-10-20T12:00:00Z", "--registry", Path.Combine(fixture.Path, "missing.json")],
            fixture.Path);
        Assert.Equal(expected, result.ExitCode);
        var decision = Format.Read<Decision>(result.StandardOutput);
        Assert.Equal(status, decision.Status);
        Assert.Equal(original, decision.OriginalExitCode);
        Assert.Equal(expected, decision.EffectiveExitCode);
        Assert.Equal(mode != "off", !string.IsNullOrWhiteSpace(result.StandardError));
    }
}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public class KnownTestFailuresRegistryTests(ITestOutputHelper output)
{
    [Theory]
    [InlineData("version")]
    [InlineData("production")]
    [InlineData("unknown-property")]
    [InlineData("duplicate-property")]
    [InlineData("null")]
    [InlineData("missing-required")]
    [InlineData("null-item")]
    [InlineData("bad-history")]
    [InlineData("bad-action")]
    [InlineData("bad-revision")]
    [InlineData("bad-issue")]
    [InlineData("bad-project")]
    [InlineData("bad-expiry")]
    [InlineData("non-utc")]
    [InlineData("missing-offset")]
    [InlineData("empty-signature")]
    [InlineData("empty-case")]
    [InlineData("ambiguous")]
    public void InvalidRegistryFailsClosed(string variant)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var registry = KnownTestFailuresFixture.Registry();
        var first = registry.History[0];
        var content = first.Content!;
        var changed = variant switch
        {
            "bad-issue" => content with { Issue = "http://example.com/issue" },
            "bad-project" => content with { Project = "../tests/Example.csproj" },
            "bad-expiry" => content with { ExpiresAt = content.CreatedAt },
            "non-utc" => content with { CreatedAt = content.CreatedAt.ToOffset(TimeSpan.FromHours(1)) },
            "empty-signature" => content with { Message = " " },
            "empty-case" => content with { CaseId = "" },
            _ => content
        };
        registry = registry with { History = [first with { Content = changed }, registry.History[1]] };
        registry = variant switch
        {
            "version" => registry with { SchemaVersion = 2 },
            "production" => registry with { Environment = "production" },
            "bad-history" => registry with { History = [first with { Sequence = 2 }] },
            "bad-action" => registry with { History = [first with { Action = "renew" }] },
            "bad-revision" => registry with { History = [first with { ContentRevision = 0 }] },
            "ambiguous" => registry with { History = [.. registry.History, first with { Sequence = 3, Id = "ambiguous" }] },
            _ => registry
        };
        var json = Format.Write(registry);
        json = variant switch
        {
            "unknown-property" => json.Replace("\"schemaVersion\": 1", "\"schemaVersion\": 1, \"surprise\": true", StringComparison.Ordinal),
            "duplicate-property" => json.Replace("\"owner\": \"team\"", "\"owner\": \"team\", \"owner\": \"team\"", StringComparison.Ordinal),
            "null" => json.Replace("\"owner\": \"team\"", "\"owner\": null", StringComparison.Ordinal),
            "missing-required" => json.Replace("\"caseId\": \"stable-case-1\",", "", StringComparison.Ordinal),
            "null-item" => """{"schemaVersion":1,"environment":"experimental","history":[null]}""",
            "missing-offset" => json.Replace("2026-10-19T12:00:00+00:00", "2026-10-19T12:00:00", StringComparison.Ordinal),
            _ => json
        };
        var decision = Evaluator.Evaluate("experimental", 0, KnownTestFailuresFixture.Now, true,
            () => new(Format.Read<Registry>(json), KnownTestFailuresFixture.Source, json),
            KnownTestFailuresFixture.Evidence, _ => fixture.Trx());
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(3, decision.EffectiveExitCode);
        Assert.Single(decision.Diagnostics);
    }

    [Fact]
    public void ContentUpdateInvalidatesApprovalAndReapprovalKeepsSubjectRevision()
    {
        var registry = KnownTestFailuresFixture.Registry();
        var update = KnownTestFailuresFixture.Event("update", 3, 2,
            content: KnownTestFailuresFixture.Content with { Owner = "new owner" });
        registry = RegistryHistory.Append(registry, update);
        var entry = RegistryHistory.Replay(registry)["allow-1"];
        Assert.Equal(2, entry.ContentRevision);
        Assert.Null(entry.Approval);
        registry = RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("approve", 4, 2));
        entry = RegistryHistory.Replay(registry)["allow-1"];
        Assert.Equal(2, entry.ContentRevision);
        Assert.Equal(new Approval("reviewer", KnownTestFailuresFixture.Now.AddHours(-1), 2), entry.Approval);
        Assert.Equal(4, registry.History.Length);
    }

    [Theory]
    [InlineData("revoke")]
    [InlineData("remove")]
    [InlineData("expire")]
    public void TerminalEntriesCannotBeUpdatedApprovedOrReused(string action)
    {
        var registry = RegistryHistory.Append(KnownTestFailuresFixture.Registry(), KnownTestFailuresFixture.Event(action, 3));
        var entry = RegistryHistory.Replay(registry)["allow-1"];
        Assert.Equal(action, entry.State);
        Assert.Null(entry.Approval);
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("add", 4)));
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("approve", 4)));
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("update", 4, 2,
            content: KnownTestFailuresFixture.Content with { ExpiresAt = KnownTestFailuresFixture.Now.AddDays(2) })));
    }

    [Fact]
    public void NaturalExpiryCannotBeRenewedAndMutationMetadataIsRequired()
    {
        var registry = KnownTestFailuresFixture.Registry();
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry,
            KnownTestFailuresFixture.Event("update", 3, 2, at: KnownTestFailuresFixture.Content.ExpiresAt,
                content: KnownTestFailuresFixture.Content with { ExpiresAt = KnownTestFailuresFixture.Now.AddDays(2) })));
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry,
            KnownTestFailuresFixture.Event("revoke", 3) with { Reason = " " }));
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry,
            KnownTestFailuresFixture.Event("revoke", 3) with { Actor = "" }));
        Assert.Throws<InvalidDataException>(() => RegistryHistory.Append(registry,
            KnownTestFailuresFixture.Event("revoke", 3, at: KnownTestFailuresFixture.Now.AddHours(-2))));
    }

    [Fact]
    public async Task BranchLifecyclePinsProvenanceAndLeavesCheckoutAndIndexUntouched()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        var status = GitCli.Run(fixture.Path, "status", "--porcelain=v1");
        var index = GitCli.Run(fixture.Path, "ls-files", "--stage");
        var head = GitCli.Run(fixture.Path, "rev-parse", "HEAD");
        const string branch = "refs/heads/experimental-allowances";
        var first = await GitRegistry.MutateAsync(fixture.Path, branch, new string('0', 40), true, KnownTestFailuresFixture.Event("add"));
        var added = await GitRegistry.ReadBranchAsync(fixture.Path, branch, "registry.json");
        Assert.Equal(new RegistrySource("git-local", "registry.json", first,
            GitCli.Run(fixture.Path, "rev-parse", first + ":registry.json")), added.Source);
        Assert.Null(RegistryHistory.Replay(added.Registry)["allow-1"].Approval);
        var second = await GitRegistry.MutateAsync(fixture.Path, branch, first, false, KnownTestFailuresFixture.Event("approve", 2));
        await Assert.ThrowsAsync<InvalidDataException>(() =>
            GitRegistry.MutateAsync(fixture.Path, branch, first, false, KnownTestFailuresFixture.Event("remove", 3)));
        Assert.Equal(second, GitCli.Run(fixture.Path, "rev-parse", branch));
        Assert.Equal(first, GitCli.Run(fixture.Path, "rev-parse", second + "^"));
        var third = await GitRegistry.MutateAsync(fixture.Path, branch, second, false,
            KnownTestFailuresFixture.Event("update", 3, 2, content: KnownTestFailuresFixture.Content with { Issue = "https://example.com/issues/2" }));
        var updated = await GitRegistry.ReadBranchAsync(fixture.Path, branch, "registry.json");
        Assert.Equal(2, RegistryHistory.Replay(updated.Registry)["allow-1"].ContentRevision);
        Assert.Null(RegistryHistory.Replay(updated.Registry)["allow-1"].Approval);
        var fourth = await GitRegistry.MutateAsync(fixture.Path, branch, third, false, KnownTestFailuresFixture.Event("remove", 4, 2));
        var removed = await GitRegistry.ReadBranchAsync(fixture.Path, branch, "registry.json");
        Assert.Equal(fourth, removed.Source.Commit);
        Assert.Equal("remove", RegistryHistory.Replay(removed.Registry)["allow-1"].State);
        await Assert.ThrowsAsync<InvalidDataException>(() =>
            GitRegistry.MutateAsync(fixture.Path, branch, fourth, false, KnownTestFailuresFixture.Event("add", 5)));
        Assert.Equal(index, GitCli.Run(fixture.Path, "ls-files", "--stage"));
        Assert.Equal(status, GitCli.Run(fixture.Path, "status", "--porcelain=v1"));
        Assert.Equal(head, GitCli.Run(fixture.Path, "rev-parse", "HEAD"));
        Assert.Equal("unstaged user changes\n", File.ReadAllText(Path.Combine(fixture.Path, "sentinel.txt")));
    }

    [Fact]
    public async Task CompetingWritersUseCompareAndSwap()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        const string branch = "refs/heads/allowances";
        var first = await GitRegistry.MutateAsync(fixture.Path, branch, new string('0', 40), true, KnownTestFailuresFixture.Event("add"));
        async Task<string?> Attempt(string actor)
        {
            try
            {
                return await GitRegistry.MutateAsync(fixture.Path, branch, first, false,
                    KnownTestFailuresFixture.Event("approve", 2) with { Actor = actor });
            }
            catch (InvalidDataException)
            {
                return null;
            }
        }
        var results = await Task.WhenAll(Attempt("reviewer-a"), Attempt("reviewer-b"));
        var winner = Assert.Single(results, result => result is not null);
        Assert.Equal(winner, GitCli.Run(fixture.Path, "rev-parse", branch));
        Assert.Equal(2, (await GitRegistry.ReadBranchAsync(fixture.Path, branch, "registry.json")).Registry.History.Length);
    }

    [Theory]
    [InlineData("main")]
    [InlineData("--all")]
    [InlineData("refs/heads/../main")]
    [InlineData("refs/heads/main@{1}")]
    [InlineData("refs/tags/main")]
    public async Task AmbiguousOrUnsafeRefsAreRejected(string fullRef)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.ReadBranchAsync(fixture.Path, fullRef, "registry.json"));
    }

    [Fact]
    public async Task MissingMalformedAndSymbolicBranchesAreRejected()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/missing", "registry.json"));
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/main", "registry.json"));
        GitCli.Run(fixture.Path, "symbolic-ref", "refs/heads/alias", "refs/heads/main");
        var head = GitCli.Run(fixture.Path, "rev-parse", "HEAD");
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.MutateAsync(fixture.Path, "refs/heads/main",
            head, false, KnownTestFailuresFixture.Event("add")));
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.MutateAsync(fixture.Path, "refs/heads/alias",
            head, false, KnownTestFailuresFixture.Event("add")));
        Assert.Equal(head, GitCli.Run(fixture.Path, "rev-parse", "HEAD"));
        await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/main", "../registry.json"));

        File.WriteAllText(Path.Combine(fixture.Path, "registry.json"), "{not json");
        GitCli.Run(fixture.Path, "add", "--", "registry.json");
        GitCli.Run(fixture.Path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "-c", "commit.gpgsign=false", "commit", "-m", "Malformed registry fixture");
        await Assert.ThrowsAnyAsync<JsonException>(() => GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/main", "registry.json"));
    }

    [Theory]
    [InlineData("refs/heads/alias")]
    [InlineData("refs/remotes/origin/HEAD")]
    public async Task SymbolicReadersCannotConsumeAnOtherwiseValidRegistry(string alias)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        const string branch = "refs/heads/allowances";
        var commit = await GitRegistry.MutateAsync(fixture.Path, branch, new string('0', 40), true, KnownTestFailuresFixture.Event("add"));
        GitCli.Run(fixture.Path, "symbolic-ref", alias, branch);
        var direct = await GitRegistry.ReadBranchAsync(fixture.Path, branch, "registry.json");
        Assert.Equal(commit, direct.Source.Commit);
        Assert.Equal(KnownTestFailuresFixture.Content, RegistryHistory.Replay(direct.Registry)["allow-1"].Content);
        var error = await Assert.ThrowsAsync<InvalidDataException>(() => GitRegistry.ReadBranchAsync(fixture.Path, alias, "registry.json"));
        Assert.Equal("Registry branch must not be a symbolic ref.", error.Message);
    }

    [Theory]
    [InlineData("default")]
    [InlineData("local")]
    [InlineData("global")]
    public async Task RegistryPlumbingNeverInvokesConfiguredHooks(string configuration)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        var gitDirectory = Path.Combine(fixture.Path, ".git");
        var hookDirectory = Path.Combine(gitDirectory, configuration == "default" ? "hooks" : "configured hooks");
        Directory.CreateDirectory(hookDirectory);
        var hookPath = Path.Combine(hookDirectory, "reference-transaction");
        File.WriteAllText(hookPath, "#!/bin/sh\nprintf '%s\\n' \"$1\" >> hook-invocations.log\n");
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(hookPath, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        // Isolate the child processes' global config without changing the user's global config.
        var globalConfig = Path.Combine(gitDirectory, "isolated-global-config");
        File.WriteAllText(globalConfig, "");
        var environment = new Dictionary<string, string>
        {
            ["GIT_CONFIG_GLOBAL"] = globalConfig,
            ["GIT_CONFIG_NOSYSTEM"] = "1"
        };
        if (configuration == "local")
        {
            GitCli.Run(fixture.Path, "config", "--local", "core.hooksPath", hookDirectory);
        }
        else if (configuration == "global")
        {
            GitCli.Run(fixture.Path, "config", "--file", globalConfig, "core.hooksPath", hookDirectory);
        }
        var head = GitCli.Run(fixture.Path, "rev-parse", "HEAD");
        var control = await ProcessRunner.RunAsync(output, "git",
            ["--no-pager", "-c", $"core.hooksPath={hookDirectory}", "update-ref", "refs/heads/hook-control", head],
            fixture.Path, environment);
        Assert.Equal(0, control.ExitCode);
        var marker = Path.Combine(fixture.Path, "hook-invocations.log");
        Assert.Equal(["prepared", "committed"], File.ReadAllLines(marker));
        File.Delete(marker);
        var status = GitCli.Run(fixture.Path, "status", "--porcelain=v1");
        var index = GitCli.Run(fixture.Path, "ls-files", "--stage");
        var config = GitCli.Run(fixture.Path, "config", "--local", "--list");
        var global = File.ReadAllText(globalConfig);
        var eventPath = Path.Combine(gitDirectory, "event.json");
        File.WriteAllText(eventPath, Format.Write(KnownTestFailuresFixture.Event("add")));
        var initialize = await ProcessRunner.RunAsync(output, "dotnet",
            [typeof(KnownTestFailures.Program).Assembly.Location, "mutate", "--repo", fixture.Path,
                "--ref", "refs/heads/allowances", "--expected", new string('0', 40), "--initialize", "--event", eventPath],
            fixture.Path, environment);
        Assert.Equal(0, initialize.ExitCode);
        File.WriteAllText(eventPath, Format.Write(KnownTestFailuresFixture.Event("update", 2, 2,
            content: KnownTestFailuresFixture.Content with { Owner = "new owner" })));
        var update = await ProcessRunner.RunAsync(output, "dotnet",
            [typeof(KnownTestFailures.Program).Assembly.Location, "mutate", "--repo", fixture.Path,
                "--ref", "refs/heads/allowances", "--expected", initialize.StandardOutput.Trim(), "--event", eventPath],
            fixture.Path, environment);
        Assert.Equal(0, update.ExitCode);
        Assert.False(File.Exists(marker), "Registry plumbing executed a reference-transaction hook.");
        var registry = await GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/allowances", "registry.json");
        Assert.Equal(2, RegistryHistory.Replay(registry.Registry)["allow-1"].ContentRevision);
        Assert.Equal("new owner", RegistryHistory.Replay(registry.Registry)["allow-1"].Content.Owner);
        Assert.Equal(index, GitCli.Run(fixture.Path, "ls-files", "--stage"));
        Assert.Equal(status, GitCli.Run(fixture.Path, "status", "--porcelain=v1"));
        Assert.Equal(head, GitCli.Run(fixture.Path, "rev-parse", "HEAD"));
        Assert.Equal(config, GitCli.Run(fixture.Path, "config", "--local", "--list"));
        Assert.Equal(global, File.ReadAllText(globalConfig));
        Assert.Equal("unstaged user changes\n", File.ReadAllText(Path.Combine(fixture.Path, "sentinel.txt")));
    }

    [Fact]
    public async Task ManagementPreservesOtherBranchTreeFiles()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        File.WriteAllText(Path.Combine(fixture.Path, "registry.json"), Format.Write(KnownTestFailuresFixture.Registry()));
        GitCli.Run(fixture.Path, "add", "--", "registry.json");
        GitCli.Run(fixture.Path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "-c", "commit.gpgsign=false", "commit", "-m", "Registry fixture");
        GitCli.Run(fixture.Path, "branch", "registry");
        var previous = GitCli.Run(fixture.Path, "rev-parse", "refs/heads/registry");
        var sentinel = GitCli.Run(fixture.Path, "rev-parse", previous + ":sentinel.txt");
        var updated = await GitRegistry.MutateAsync(fixture.Path, "refs/heads/registry", previous, false,
            KnownTestFailuresFixture.Event("expire", 3));
        Assert.Equal(sentinel, GitCli.Run(fixture.Path, "rev-parse", updated + ":sentinel.txt"));
        Assert.Equal(previous, GitCli.Run(fixture.Path, "rev-parse", "HEAD"));
    }
}

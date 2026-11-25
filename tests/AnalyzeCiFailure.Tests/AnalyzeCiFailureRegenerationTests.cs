// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Collections.Immutable;
using System.Text.Json.Nodes;
using Aspire.TestUtilities;
using VerifyXunit;
using Xunit;

namespace Infrastructure.Tests;

public sealed partial class AnalyzeCiFailureWorkflowTests
{
    [Fact]
    [RequiresTools(["bash", "jq", "git"])]
    public async Task RegenerationReplacesManualBodyAndRegistersOnlyTheSelectedCause()
    {
        await WriteGroupedFixtureAsync();
        var snapshot = Path.Combine(_workspace.Path, "issue-snapshot.json");
        await File.WriteAllTextAsync(snapshot, new JsonObject
        {
            ["number"] = "77",
            ["updated_at"] = "2026-10-21T00:00:00Z",
            ["body"] = ExistingGroupedIssue,
        }.ToJsonString());
        var result = await RunSuccessfulGroupedPublicationAsync(
            body: ExistingGroupedIssue,
            extraArguments: ["--regenerate-issue", snapshot, "--issue-footer", "Publisher attribution"]);

        Assert.True(result.ExitCode == 0, result.Output);
        var body = await File.ReadAllTextAsync(Path.Combine(_workspace.Path, "issue-body.md"));
        await Verifier.Verify(body).UseDirectory("Snapshots");
        var cause = JsonNode.Parse(await File.ReadAllTextAsync(
            Path.Combine(_workspace.Path, "memory-repo", "causes", "debugger-timeout.json")))!;
        Assert.Equal("https://github.com/microsoft/aspire/issues/77", cause["issue_url"]!.GetValue<string>());
        Assert.Single(cause["occurrences"]!.AsArray());
        Assert.Equal("pull-request", cause["occurrences"]![0]!["run_scope"]!.GetValue<string>());
        Assert.False(File.Exists(Path.Combine(_workspace.Path, "memory-repo", "runs", "123.json")));
        Assert.All((await ReadHttpRequestsAsync()).Where(request => request!["method"]!.GetValue<string>() != "GET"),
            request =>
            {
                Assert.Equal("PATCH", request!["method"]!.GetValue<string>());
                Assert.Equal("/repos/microsoft/aspire/issues/77", request["path"]!.GetValue<string>());
                Assert.Equal(["body"], request["body"]!.AsObject().Select(field => field.Key));
            });
    }

    [Theory]
    [InlineData(null, "unavailable")]
    [InlineData("pull-request", "unavailable")]
    [InlineData("main", "main")]
    public void RegeneratedOccurrenceDoesNotInferMainFromAnUnresolvedPr(string? scope, string expectedContext)
    {
        const string canonical = """
            <!-- ci-failure-cause:flaky -->
            <!-- ci-failure-cause-type:flaky-test -->

            ## Build Information

            Build: https://github.com/microsoft/aspire/actions/runs/123

            <!-- ci-failure-occurrences:start -->
            ## Occurrences

            | Date | Build | Job | Context |
            |------|-------|-----|----|
            <!-- ci-failure-occurrences:end -->
            """;
        var history = new JsonObject
        {
            ["occurrences"] = new JsonArray(new JsonObject
            {
                ["run_id"] = 123,
                ["run_url"] = "https://github.com/microsoft/aspire/actions/runs/123",
                ["observed_at"] = "2026-10-21T00:00:00Z",
                ["pr_number"] = 0,
                ["job"] = "Tests",
                ["run_scope"] = scope,
            }),
        };

        var body = AnalyzeCiFailure.CauseIssueRenderer.RegenerateIssue(canonical, history, footer: null)!;

        Assert.Equal($"| 2026-10-21 | [123](https://github.com/microsoft/aspire/actions/runs/123) | ` Tests ` | {expectedContext} |",
            Assert.Single(body.Split('\n'), line => line.StartsWith("| 2026-", StringComparison.Ordinal)));
    }

    [Theory]
    [InlineData("")]
    [InlineData("999")]
    [InlineData("123,123")]
    public void SelectedJobScopeMustMatchTheCompleteCapturedJobArray(string jobIds)
    {
        var evidence = AnalyzeCiFailureFixtures.CreateEvidence(AnalyzeCiFailure.RunScope.Main) with
        {
            SelectedJobIds = jobIds.Length == 0 ? [] : jobIds.Split(',').Select(long.Parse).ToImmutableArray(),
        };

        var error = Assert.Throws<AnalyzeCiFailure.ValidationException>(() =>
            AnalyzeCiFailure.AnalysisValidator.ValidateBundle(evidence, "microsoft/aspire"));

        Assert.Equal("Selected job scope must exactly match the captured jobs.", error.Message);
    }

    [Fact]
    [RequiresTools(["bash", "jq", "git"])]
    public async Task JobScopedEvidenceCannotPublishOrdinaryRunHistory()
    {
        await WriteGroupedFixtureAsync();
        AnalyzeCiFailureFixtures.Write(_workspace.Path);
        var path = Path.Combine(EvidenceDirectory, "evidence.json");
        var evidence = JsonNode.Parse(await File.ReadAllTextAsync(path))!;
        evidence["selected_job_ids"] = new JsonArray("123");
        await File.WriteAllTextAsync(path, evidence.ToJsonString());

        var result = await RunSuccessfulGroupedPublicationAsync(useTypedProposal: true);

        Assert.Equal(1, result.ExitCode);
        Assert.Contains("Job-scoped evidence requires --regenerate-issue", result.Output, StringComparison.Ordinal);
        Assert.False(File.Exists(Path.Combine(_workspace.Path, "memory-repo", "runs", "123.json")));
        Assert.False(File.Exists(Path.Combine(_workspace.Path, "http-requests.jsonl")));
    }

    [Theory]
    [InlineData("body")]
    [InlineData("updated_at")]
    [RequiresTools(["bash", "jq", "git"])]
    public async Task RegenerationRejectsStaleSnapshotBeforeHistoryWrites(string staleField)
    {
        await WriteGroupedFixtureAsync();
        var snapshot = Path.Combine(_workspace.Path, "issue-snapshot.json");
        var expected = new JsonObject
        {
            ["number"] = "77",
            ["updated_at"] = "2026-10-21T00:00:00Z",
            ["body"] = ExistingGroupedIssue,
        };
        expected[staleField] = "stale";
        await File.WriteAllTextAsync(snapshot, expected.ToJsonString());

        var result = await RunSuccessfulGroupedPublicationAsync(
            body: ExistingGroupedIssue, extraArguments: ["--regenerate-issue", snapshot]);

        Assert.Equal(1, result.ExitCode);
        Assert.Contains("changed since the regeneration snapshot", result.Output, StringComparison.Ordinal);
        Assert.False(File.Exists(Path.Combine(_workspace.Path, "memory-repo", "causes", "debugger-timeout.json")));
        Assert.All(await ReadHttpRequestsAsync(), request => Assert.Equal("GET", request!["method"]!.GetValue<string>()));
    }

    [Theory]
    [InlineData("77", 0)]
    [InlineData("999", 1)]
    [RequiresTools(["bash", "git"])]
    public async Task JobScopedCollectionRequiresAnExactActionableJob(string jobId, int exitCode)
    {
        var result = await RunCollectionAsync(extraArguments: ["--job-ids", jobId]);

        Assert.True(result.ExitCode == exitCode, result.Output);
        if (exitCode == 0)
        {
            var evidence = JsonNode.Parse(await File.ReadAllTextAsync(Path.Combine(EvidenceDirectory, "evidence.json")))!;
            Assert.Equal(["77"], evidence["selected_job_ids"]!.AsArray().Select(id => id!.GetValue<string>()));
            Assert.Equal(["77"], evidence["jobs"]!.AsArray().Select(job => job!["id"]!.GetValue<string>()));
        }
        else
        {
            Assert.Contains("Selected job IDs must identify actionable failures", result.Output, StringComparison.Ordinal);
        }
    }
}

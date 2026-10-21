// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task TrustedReplayCanEnrichLegacyAttemptOneWithoutRewritingHistoricalDateOrJob()
    {
        var input = Fixture("main");
        var stored = StoredCause(123);
        var historical = stored["occurrences"]![0]!.AsObject();
        historical.Remove("run_attempt");
        historical.Remove("job_ids");
        historical.Remove("job_names");
        historical["run_url"] = RunUrl;
        historical["observed_at"] = "2026-10-01T12:00:00Z";
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var occurrence = Assert.Single(PersistedCause(result)["occurrences"]!.AsArray())!;
        Assert.Equal(1, occurrence["run_attempt"]!.GetValue<int>());
        Assert.Equal(RunUrl + "/attempts/1", occurrence["run_url"]!.GetValue<string>());
        Assert.Equal("Historical job", occurrence["job"]!.GetValue<string>());
        Assert.Equal("2026-10-01T12:00:00Z", occurrence["observed_at"]!.GetValue<string>());
        Assert.Null(occurrence["job_ids"]);
    }

    [Theory]
    [InlineData("0")]
    [InlineData("-1")]
    [InlineData("1.5")]
    [InlineData("\"1\"")]
    [InlineData("null")]
    public async Task ExplicitMalformedLegacyAttemptIsNotTreatedAsAbsent(string attempt)
    {
        var input = Fixture("main");
        var stored = StoredCause(100);
        stored["occurrences"]![0]!["run_attempt"] = JsonNode.Parse(attempt);
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        AssertNoCalls(result, call => call.Kind is "create-issue" or "update-issue" or "reopen-issue");
    }

    [Fact]
    public async Task LegacyOccurredAtParticipatesInChronologicalHistory()
    {
        var input = Fixture("main");
        var stored = StoredCause(101, 100);
        stored["occurrences"]![0]!["observed_at"] = "2026-10-20T12:00:00Z";
        var older = stored["occurrences"]![1]!.AsObject();
        older.Remove("observed_at");
        older["occurred_at"] = "2026-10-19T12:00:00Z";
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal([100L, 101L, 123L], PersistedCause(result)["occurrences"]!.AsArray()
            .Select(row => row!["run_id"]!.GetValue<long>()).ToArray());
    }

    [Theory]
    [InlineData("main", "#42", 42, "main")]
    [InlineData("pull-request", "main", 42, "#42")]
    [InlineData("pull-request", "#0", 0, "unavailable")]
    [InlineData("pull-request", "#0", 42, "#42")]
    public async Task AuthenticatedHistoricalScopeOverridesStaleCachedContext(
        string scope, string context, int pr, string expected)
    {
        var input = Fixture("main");
        var stored = StoredCause(100);
        stored["occurrences"]![0]!["run_scope"] = scope;
        stored["occurrences"]![0]!["issue_context"] = context;
        stored["occurrences"]![0]!["pr_number"] = pr;
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Contains($"| {expected} |", ManagedOccurrences(LatestIssueBody(result)).Split('\n')
            .Single(line => line.Contains("[100](", StringComparison.Ordinal)), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("""{"run_id":100,"run_scope":"main"}""", "main")]
    [InlineData("""{"run_id":999,"run_scope":"main"}""", "unavailable")]
    [InlineData("""{"run_id":100,"run_scope":"pull-request"}""", "unavailable")]
    [InlineData("""{"run_id":100,"run_scope":"unknown"}""", "unavailable")]
    public async Task MissingHistoricalScopeCanOnlyRecoverMainFromMatchingTrustedSummary(string summary, string expected)
    {
        var input = Fixture("main");
        var stored = StoredCause(100);
        stored["occurrences"]![0]!.AsObject().Remove("run_scope");
        input["memory"]!["runs/100.json"] = Object(summary);
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Contains($"| {expected} |", ManagedOccurrences(LatestIssueBody(result)).Split('\n')
            .Single(line => line.Contains("[100](", StringComparison.Ordinal)), StringComparison.Ordinal);
    }

    [Fact]
    public async Task LegacyCauseCategoryIsNotEvidenceOfHistoricalRunScope()
    {
        var input = Fixture("main", "main-repository-breakage");
        var stored = StoredCause(100);
        stored["type"] = "main-repository-breakage";
        stored["occurrences"]![0]!.AsObject().Remove("run_scope");
        AttachIssue(input, stored, Issue(type: "main-repository-breakage"));

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Contains("| unavailable |", ManagedOccurrences(LatestIssueBody(result)).Split('\n')
            .Single(line => line.Contains("[100](", StringComparison.Ordinal)), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("https://github.com/other/repo/actions/runs/100/attempts/1")]
    [InlineData("https://github.com/microsoft/aspire/actions/runs/100/attempts/2")]
    [InlineData("https://evil.example/actions/runs/100")]
    public async Task StoredHistoryCannotPublishReboundRepositoryOrAttemptLinks(string url)
    {
        var input = Fixture("main");
        var stored = StoredCause(100);
        stored["occurrences"]![0]!["run_url"] = url;
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        AssertNoCalls(result, call => call.Kind is "create-issue" or "update-issue" or "reopen-issue");
    }

    [Fact]
    public async Task LegacyPrHeaderMigratesWithoutDeletingOperatorNotes()
    {
        var input = Fixture("main");
        var issue = Issue();
        issue["body"] = """
            <!-- ci-failure-cause:feed-timeout -->
            <!-- ci-failure-cause-type:infra-failure -->

            **Type**: infra-failure

            Operator note before.

            ## Occurrences

            | Date | Build | Job | PR |
            |------|-------|-----|----|
            | 2026-10-20 | [100](https://github.com/microsoft/aspire/actions/runs/100) | Historical job | #42 |

            Operator note after.
            """;
        AttachIssue(input, issue: issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var body = LatestIssueBody(result);
        Assert.Contains("| Date | Build | Job | Context |", ManagedOccurrences(body), StringComparison.Ordinal);
        Assert.Contains("Operator note before.", body, StringComparison.Ordinal);
        Assert.Contains("Operator note after.", body, StringComparison.Ordinal);
        Assert.Equal(2, OccurrenceLinks(body).Length);
    }

    [Fact]
    public async Task ReplayingOlderRunDoesNotPushItAfterNewerOccurrences()
    {
        var input = Fixture("main");
        var stored = StoredCause(123, 200);
        stored["occurrences"]![1]!["observed_at"] = "2026-10-22T12:00:00Z";
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(
            [RunUrl + "/attempts/1", "https://github.com/microsoft/aspire/actions/runs/200/attempts/1"],
            OccurrenceLinks(LatestIssueBody(result)));
        Assert.Equal(2, PersistedCause(result)["occurrences"]!.AsArray().Count);
    }

    [Fact]
    public async Task MainIssueMigrationRefreshesOnlyPublisherOwnedDetails()
    {
        var input = Fixture("main", "main-repository-breakage");
        var stored = StoredCause(100);
        stored["type"] = "main-repository-breakage";
        var issue = Issue(type: "main-repository-breakage");
        issue["title"] = "Old agent-authored blame";
        issue["body"] = issue["body"]!.GetValue<string>().Replace(
            "**Type**: main-repository-breakage", "Old generated blame\n\n**Type**: main-repository-breakage", StringComparison.Ordinal);
        AttachIssue(input, stored, issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var edit = OnlyCall(result, "update-issue").Data;
        Assert.Equal("[Main CI Failure] Main branch CI failure at trusted-sha", edit["title"]!.GetValue<string>());
        var body = edit["body"]!.GetValue<string>();
        Assert.False(body.Contains("Old generated blame", StringComparison.Ordinal));
        Assert.Contains("Operator note before.", body, StringComparison.Ordinal);
        Assert.Contains("Operator note after.", body, StringComparison.Ordinal);
        Assert.Equal(2, OccurrenceLinks(body).Length);
    }
}

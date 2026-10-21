// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task MainPublicationDoesNotRequireSafeOutputPrNumbers()
    {
        var result = await ExecuteAsync("publish", Fixture("main"));

        AssertSuccess(result);
        OnlyCall(result, "create-issue");
        AssertNoCalls(result, call => call.Kind is "create-comment" or "update-comment");
    }

    [Theory]
    [InlineData("infra-failure", "ci-failure-cause")]
    [InlineData("flaky-test", "test-failure")]
    [InlineData("main-repository-breakage", "main-ci-break")]
    public async Task NewCauseIssuesCarryExactIdentityAndCategoryLabel(string type, string label)
    {
        var input = type == "flaky-test" ? FlakyFixture("main") :
            Fixture("main", type == "main-repository-breakage" ? type : "transient-infra");

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var issue = OnlyCall(result, "create-issue").Data;
        var lines = issue["body"]!.GetValue<string>().Split('\n');
        Assert.Equal("<!-- ci-failure-cause:feed-timeout -->", lines[0]);
        Assert.Equal($"<!-- ci-failure-cause-type:{type} -->", lines[1]);
        Assert.Contains(label, issue["labels"]!.AsArray().Select(node => node!.GetValue<string>()));
    }

    [Fact]
    public async Task ClosedExactCauseIssueIsReopenedRatherThanDuplicated()
    {
        var input = Fixture("main");
        AttachIssue(input, issue: Issue(state: "closed"));

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(90, OnlyCall(result, "reopen-issue").Data["number"]!.GetValue<int>());
        AssertNoCalls(result, call => call.Kind == "create-issue");
        Assert.Equal(2, PersistedCause(result)["occurrences"]!.AsArray().Count);
    }

    [Fact]
    public async Task MissingStoredLinkCanRecoverExactIssueOnLaterPage()
    {
        var input = Fixture("main");
        var stored = StoredCause(100);
        stored.AsObject().Remove("issue_url");
        input["memory"]!["causes/feed-timeout.json"] = stored;
        input["issue_pages"] = new JsonArray(new JsonArray(), new JsonArray(Issue()));

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        AssertNoCalls(result, call => call.Kind == "create-issue");
        Assert.Equal("https://github.com/microsoft/aspire/issues/90", PersistedCause(result)["issue_url"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("foreign-url")]
    [InlineData("prefix-marker")]
    [InlineData("marker-in-prose")]
    [InlineData("wrong-type")]
    [InlineData("missing-label")]
    [InlineData("pull-request")]
    public async Task LinkedIssueMustAuthenticateRepositoryMarkerTypeAndLabel(string mutation)
    {
        var input = Fixture("main");
        var issue = Issue();
        var stored = StoredCause(100);
        switch (mutation)
        {
            case "foreign-url":
                stored["issue_url"] = "https://github.com/other/repo/issues/90";
                input["issues"]!.AsArray().Add(issue);
                issue["body"] = "Unrelated";
                break;
            case "prefix-marker":
                issue["body"] = issue["body"]!.GetValue<string>().Replace(
                    "ci-failure-cause:feed-timeout -->", "ci-failure-cause:feed-timeout-extra -->", StringComparison.Ordinal);
                break;
            case "marker-in-prose":
                issue["body"] = "Operator introduction\n" + issue["body"]!.GetValue<string>();
                break;
            case "wrong-type":
                issue["body"] = issue["body"]!.GetValue<string>().Replace("infra-failure", "flaky-test", StringComparison.Ordinal);
                break;
            case "missing-label":
                issue["labels"] = new JsonArray();
                break;
            case "pull-request":
                issue["pull_request"] = Object("""{"url":"https://api.github.com/repos/microsoft/aspire/pulls/90"}""");
                break;
        }
        input["memory"]!["causes/feed-timeout.json"] = stored;
        if (mutation != "foreign-url")
        {
            input["issues"]!.AsArray().Add(issue);
        }

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(200, OnlyCall(result, "create-issue").Data["number"]!.GetValue<int>());
        AssertNoCalls(result, call => call.Kind == "update-issue" && call.Data["number"]!.GetValue<int>() == 90);
    }

    [Fact]
    public async Task LegacyTypeLineCanAuthenticateIssueWithoutTypeMarker()
    {
        var input = Fixture("main");
        var issue = Issue();
        issue["body"] = issue["body"]!.GetValue<string>().Replace(
            "<!-- ci-failure-cause-type:infra-failure -->\n", "", StringComparison.Ordinal);
        AttachIssue(input, issue: issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        AssertNoCalls(result, call => call.Kind == "create-issue");
        Assert.Equal(90, OnlyCall(result, "update-issue").Data["number"]!.GetValue<int>());
    }

    [Theory]
    [InlineData("list-open-issues")]
    [InlineData("list-closed-issues")]
    public async Task IncompleteIssueLookupCannotCreateDuplicate(string fault)
    {
        var input = Fixture("main");
        input["faults"]![fault] = "HTTP 503";

        var result = await ExecuteAsync("publish", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Single(PersistedCause(result)["occurrences"]!.AsArray());
        AssertNoCalls(result, call => call.Kind is "create-issue" or "update-issue" or "reopen-issue");
    }

    [Fact]
    public async Task DistinctMechanismsRemainDistinctEvenForSameJob()
    {
        var input = Fixture("main");
        input["analysis"]!["causes"]!.AsArray().Add("network-reset");
        input["causes"]!["network-reset.json"] = Object("""
            {"id":"network-reset","type":"infra-failure","title":"Network reset",
             "error_pattern":"ECONNRESET","job_ids":[1]}
            """);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(2, result.Calls.Count(call => call.Kind == "create-issue"));
        Assert.NotNull(result.Memory["causes/feed-timeout.json"]);
        Assert.NotNull(result.Memory["causes/network-reset.json"]);
    }

    [Theory]
    [InlineData("deleted")]
    [InlineData("duplicated")]
    [InlineData("edited")]
    [InlineData("stale")]
    public async Task ManagedIssueRowsAreProjectionOfStoredHistoryNotIssueText(string mutation)
    {
        var input = Fixture("main");
        var issue = Issue();
        var body = issue["body"]!.GetValue<string>();
        var row = body.Split('\n').Single(line => line.Contains("[100](", StringComparison.Ordinal));
        issue["body"] = mutation switch
        {
            "deleted" => body.Replace(row + "\n", "", StringComparison.Ordinal),
            "duplicated" => body.Replace(row, row + "\n" + row, StringComparison.Ordinal),
            "edited" => body.Replace("Historical job", "Operator-edited fake job", StringComparison.Ordinal),
            _ => body.Replace(row, "| 1900-01-01 | [999](https://github.com/microsoft/aspire/actions/runs/999) | fake | #1 |",
                StringComparison.Ordinal),
        };
        AttachIssue(input, issue: issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var published = LatestIssueBody(result);
        Assert.Equal(
            ["https://github.com/microsoft/aspire/actions/runs/100/attempts/1", RunUrl + "/attempts/1"],
            OccurrenceLinks(published));
        Assert.Contains("Operator note before.", published, StringComparison.Ordinal);
        Assert.Contains("Operator note after.", published, StringComparison.Ordinal);
        Assert.Contains("Historical job", ManagedOccurrences(published), StringComparison.Ordinal);
    }

    [Fact]
    public async Task IssueRecreationRestoresStoredHistoryNotOnlyLatestOccurrence()
    {
        var input = Fixture("main");
        input["memory"]!["causes/feed-timeout.json"] = StoredCause(99, 100);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var issue = OnlyCall(result, "create-issue").Data;
        Assert.Equal(
            [
                "https://github.com/microsoft/aspire/actions/runs/99/attempts/1",
                "https://github.com/microsoft/aspire/actions/runs/100/attempts/1",
                RunUrl + "/attempts/1",
            ],
            OccurrenceLinks(issue["body"]!.GetValue<string>()));
        Assert.Equal(2, Regex.Matches(ManagedOccurrences(issue["body"]!.GetValue<string>()), "Historical job").Count);
    }

    [Fact]
    public async Task HumanRunLinksOutsideManagedSectionDoNotSuppressOccurrencePublication()
    {
        var input = Fixture("main");
        var issue = Issue();
        issue["body"] = issue["body"]!.GetValue<string>() + $"\nHuman cross reference: [123]({RunUrl})";
        AttachIssue(input, issue: issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Contains(RunUrl + "/attempts/1", OccurrenceLinks(LatestIssueBody(result)));
        Assert.Contains($"Human cross reference: [123]({RunUrl})", LatestIssueBody(result), StringComparison.Ordinal);
    }

    [Fact]
    public async Task IssueBodyKeepsNewestRowsWithinUtf8BudgetWithoutTrimmingMemory()
    {
        var input = Fixture("main");
        var stored = StoredCause(Enumerable.Range(1000, 200).Select(i => (long)i).ToArray());
        foreach (var row in stored["occurrences"]!.AsArray())
        {
            row!["job"] = new string('\u754c', 500);
            row["job_names"] = new JsonArray(new string('\u754c', 500));
        }
        AttachIssue(input, stored);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var body = LatestIssueBody(result);
        Assert.InRange(Encoding.UTF8.GetByteCount(body), 1, 65000);
        var links = OccurrenceLinks(body);
        Assert.InRange(links.Length, 1, 200);
        var fullHistory = Enumerable.Range(1000, 200)
            .Select(id => $"https://github.com/microsoft/aspire/actions/runs/{id}/attempts/1")
            .Append(RunUrl + "/attempts/1").ToArray();
        Assert.Equal(fullHistory.TakeLast(links.Length), links);
        Assert.Equal(201, PersistedCause(result)["occurrences"]!.AsArray().Count);
        Assert.Contains("of 201 occurrences", body, StringComparison.Ordinal);
    }

    [Fact]
    public async Task AmbiguousManagedSectionPreservesHumanBodyAndReportsWarning()
    {
        var input = Fixture("main");
        var issue = Issue();
        issue["body"] = issue["body"]!.GetValue<string>() + "\n<!-- ci-failure-occurrences:start -->\nHuman text";
        AttachIssue(input, issue: issue);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.NotEmpty(result.Diagnostics);
        AssertNoCalls(result, call => call.Kind == "update-issue" && call.Data["body"] is not null);
        Assert.Equal(2, PersistedCause(result)["occurrences"]!.AsArray().Count);
    }

    [Theory]
    [InlineData("available", true)]
    [InlineData("incomplete", false)]
    [InlineData("unavailable", false)]
    public async Task MainIssueUsesTrustedShaAndDoesNotPublishAgentBlame(string historyState, bool hasMergeContext)
    {
        var input = Fixture("main", "main-repository-breakage");
        input["causes"]!["feed-timeout.json"]!["title"] = "PR 999 broke everything";
        input["causes"]!["feed-timeout.json"]!["error_pattern"] = "Invented blame";
        input["last_successful_run"] = Object("""{"head_sha":"previous-good"}""");
        input["candidate_history"] = new JsonObject { ["state"] = historyState };
        input["triggering_merge"] = Object("""{"number":42,"title":"Merge context only"}""");

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var issue = OnlyCall(result, "create-issue").Data;
        Assert.Equal("[Main CI Failure] Main branch CI failure at trusted-sha", issue["title"]!.GetValue<string>());
        var body = issue["body"]!.GetValue<string>();
        Assert.Contains("previous-good", body, StringComparison.Ordinal);
        Assert.Contains("trusted-sha", body, StringComparison.Ordinal);
        Assert.Equal(hasMergeContext, body.Contains("context only, not necessarily causal", StringComparison.Ordinal));
        Assert.False(body.Contains("PR 999 broke everything", StringComparison.Ordinal));
        Assert.False(body.Contains("Invented blame", StringComparison.Ordinal));
    }

    [Fact]
    public async Task PublicationFailureAfterCauseCommitIsReplayableWithoutDuplicateOccurrence()
    {
        var input = Fixture("main");
        input["faults"]!["create-issue"] = "HTTP 503";
        var failed = await ExecuteAsync("publish", input);
        Assert.Equal(CiFailureStatus.Rejected, failed.Status);
        Assert.Single(PersistedCause(failed)["occurrences"]!.AsArray());
        Assert.NotEmpty(failed.Diagnostics);
        input["memory"] = failed.Memory.DeepClone();
        input["faults"] = new JsonObject();

        var recovered = await ExecuteAsync("publish", input);

        AssertSuccess(recovered);
        OnlyCall(recovered, "create-issue");
        Assert.Single(PersistedCause(recovered)["occurrences"]!.AsArray());
    }

    [Fact]
    public async Task LostIssueLinkCommitRecoversCreatedIssueByExactMarker()
    {
        var input = Fixture("main");
        input["faults"]!["memory-commit:2"] = "push rejected";
        var failed = await ExecuteAsync("publish", input);
        Assert.Equal(CiFailureStatus.Rejected, failed.Status);
        var created = OnlyCall(failed, "create-issue").Data;
        Assert.Null(PersistedCause(failed)["issue_url"]);
        input["memory"] = failed.Memory.DeepClone();
        input["issues"] = new JsonArray(created.DeepClone());
        input["faults"] = new JsonObject();

        var recovered = await ExecuteAsync("publish", input);

        AssertSuccess(recovered);
        AssertNoCalls(recovered, call => call.Kind == "create-issue");
        Assert.Single(PersistedCause(recovered)["occurrences"]!.AsArray());
        Assert.Equal(created["url"]!.GetValue<string>(), PersistedCause(recovered)["issue_url"]!.GetValue<string>());
    }

    [Fact]
    public async Task PrCommentUsesTrustedJobNameAndDoesNotRequireRerunEnabled()
    {
        var input = Fixture();
        input["analysis"]!["failed_jobs"]![0]!["name"] = "Agent-invented job";
        input["enable_rerun"] = false;

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var comment = OnlyCall(result, "create-comment").Data;
        Assert.Equal(42, comment["pr_number"]!.GetValue<int>());
        Assert.StartsWith("<!-- analyze-ci-failure -->\n", comment["body"]!.GetValue<string>(), StringComparison.Ordinal);
        Assert.Contains("Build", comment["body"]!.GetValue<string>(), StringComparison.Ordinal);
        Assert.False(comment["body"]!.GetValue<string>().Contains("Agent-invented job", StringComparison.Ordinal));
        AssertNoCalls(result, call => call.Kind == "rerun");
    }

    [Theory]
    [InlineData("github-actions[bot]", "<!-- analyze-ci-failure -->\nold", true)]
    [InlineData("human", "<!-- analyze-ci-failure -->\nold", false)]
    [InlineData("github-actions[bot]", "preface\n<!-- analyze-ci-failure -->\nold", false)]
    [InlineData("github-actions[bot]", "<!-- analyze-ci-failure-other -->\nold", false)]
    public async Task CommentUpdateRequiresBotAndExactLeadingMarker(string author, string body, bool update)
    {
        var input = Fixture(verdict: "code-issue");
        input["comments"]!.AsArray().Add(new JsonObject
        {
            ["id"] = 80, ["user"] = new JsonObject { ["login"] = author }, ["body"] = body,
        });

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var call = Assert.Single(result.Calls, call => call.Kind is "create-comment" or "update-comment");
        Assert.Equal(update ? "update-comment" : "create-comment", call.Kind);
        if (update)
        {
            Assert.Equal(80, call.Data["id"]!.GetValue<int>());
        }
    }

    [Theory]
    [InlineData("closed", false)]
    [InlineData("open", true)]
    public async Task PrBecomingUnavailableBeforeMutationPreventsComment(string state, bool locked)
    {
        var input = Fixture();
        input["pull_request_sequence"] = new JsonArray(
            input["pull_request"]!.DeepClone(),
            new JsonObject { ["number"] = 42, ["state"] = state, ["locked"] = locked });

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        AssertNoCalls(result, call => call.Kind is "create-comment" or "update-comment");
        Assert.NotNull(result.Memory["runs/123.json"]);
    }

    [Theory]
    [InlineData("get-pr")]
    [InlineData("list-comments")]
    public async Task UnknownPrOrCommentStateDoesNotCreateFallbackComment(string fault)
    {
        var input = Fixture(verdict: "code-issue");
        input["faults"]![fault] = "HTTP 503";

        var result = await ExecuteAsync("publish", input);

        Assert.NotEmpty(result.Diagnostics);
        AssertNoCalls(result, call => call.Kind is "create-comment" or "update-comment");
    }

    [Fact]
    public async Task NoSubjectPrStillAllowsRunScopedMemoryAndCauseIssue()
    {
        var input = Fixture();
        input["context"]!["pr_numbers"] = "";
        input["analysis"]!["pr"] = null;

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        OnlyCall(result, "create-issue");
        AssertNoCalls(result, call => call.Kind is "create-comment" or "update-comment");
    }

    private static string LatestIssueBody(CiFailureObservation result)
        => result.Calls.Last(call => call.Kind is "create-issue" or "update-issue" && call.Data["body"] is not null)
            .Data["body"]!.GetValue<string>();

    private static string ManagedOccurrences(string body)
    {
        var pieces = body.Split("<!-- ci-failure-occurrences:start -->", StringSplitOptions.None);
        Assert.Equal(2, pieces.Length);
        var end = pieces[1].Split("<!-- ci-failure-occurrences:end -->", StringSplitOptions.None);
        Assert.Equal(2, end.Length);

        return end[0];
    }

    private static string[] OccurrenceLinks(string body)
        => Regex.Matches(ManagedOccurrences(body), @"\[\d+\]\((https://github\.com/microsoft/aspire/actions/runs/\d+(?:/attempts/\d+)?)\)")
            .Select(match => match.Groups[1].Value).ToArray();
}

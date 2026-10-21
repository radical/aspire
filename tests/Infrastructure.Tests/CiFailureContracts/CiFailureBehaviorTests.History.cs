// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task LastSuccessOrdersByTimestampThenRunIdRatherThanApiOrder()
    {
        var input = Fixture("main");
        input["history_pages"] = Array("""
            [{"total_count":5,"workflow_runs":[
              {"id":122,"created_at":"2026-10-21T10:00:00Z","head_sha":"nearest"},
              {"id":124,"created_at":"2026-10-21T10:00:00Z","head_sha":"future"},
              {"id":121,"created_at":"2026-10-21T10:00:00Z","head_sha":"same-second-earlier"},
              {"id":119,"created_at":"2026-10-21T09:00:00Z","head_sha":"older"},
              {"id":125,"created_at":"2026-10-21T10:01:00Z","head_sha":"too-late"}]}]
            """);

        var result = await ExecuteAsync("last-success", input);

        AssertSuccess(result);
        Assert.Equal(122, result.Output["run"]!["id"]!.GetValue<int>());
        Assert.Equal("nearest", result.Output["run"]!["head_sha"]!.GetValue<string>());
        Assert.All(result.Calls.Where(call => call.Kind == "list-successful-runs"), call =>
        {
            Assert.Equal(99, call.Data["workflow_id"]!.GetValue<int>());
            Assert.Equal("main", call.Data["branch"]!.GetValue<string>());
            Assert.Equal("push", call.Data["event"]!.GetValue<string>());
            Assert.Equal("success", call.Data["status"]!.GetValue<string>());
        });
    }

    [Fact]
    public async Task LastSuccessContinuesAfterFullPageDespiteStaleTotalCount()
    {
        var input = Fixture("main");
        var page = new JsonArray();
        for (var i = 1; i <= 100; i++)
        {
            page.Add(new JsonObject
            {
                ["id"] = i, ["created_at"] = "2026-10-21T08:00:00Z", ["head_sha"] = $"sha-{i}",
            });
        }

        input["history_pages"] = new JsonArray(
            new JsonObject { ["total_count"] = 100, ["workflow_runs"] = page },
            Object("""{"workflow_runs":[{"id":122,"created_at":"2026-10-21T09:00:00Z","head_sha":"later-page"}]}"""));

        var result = await ExecuteAsync("last-success", input);

        AssertSuccess(result);
        Assert.Equal(122, result.Output["run"]!["id"]!.GetValue<int>());
        Assert.Equal(2, result.Calls.Count(call => call.Kind == "list-successful-runs"));
    }

    [Theory]
    [InlineData("""{"total_count":2,"workflow_runs":[{"id":1,"created_at":"2026-10-21T09:00:00Z","head_sha":"sha"}]}""")]
    [InlineData("""{"total_count":-1,"workflow_runs":[]}""")]
    [InlineData("""{"total_count":1.5,"workflow_runs":[]}""")]
    [InlineData("""{"total_count":1,"workflow_runs":[{"id":1,"head_sha":"sha"}]}""")]
    [InlineData("""{"total_count":1,"workflow_runs":[{"id":1,"created_at":"2026-10-21T09:00:00Z","head_sha":""}]}""")]
    public async Task LastSuccessRejectsMalformedOrPartialHistory(string page)
    {
        var input = Fixture("main");
        input["history_pages"] = new JsonArray(Object(page));

        AssertRejected(await ExecuteAsync("last-success", input));
    }

    [Fact]
    public async Task LastSuccessSubdividesCappedWindowNewerHalfFirst()
    {
        var input = Fixture("main");
        input["history_pages"] = Array("""
            [{"total_count":1000,"workflow_runs":[]},
             {"total_count":1,"workflow_runs":[{"id":122,"created_at":"2026-10-21T09:00:00Z","head_sha":"nearest"}]}]
            """);

        var result = await ExecuteAsync("last-success", input);

        AssertSuccess(result);
        Assert.Equal(122, result.Output["run"]!["id"]!.GetValue<int>());
        var calls = result.Calls.Where(call => call.Kind == "list-successful-runs").ToArray();
        Assert.Equal(2, calls.Length);
        Assert.Equal(calls[0].Data["end"]!.GetValue<string>(), calls[1].Data["end"]!.GetValue<string>());
        Assert.True(string.CompareOrdinal(calls[0].Data["start"]!.GetValue<string>(),
            calls[1].Data["start"]!.GetValue<string>()) < 0);
    }

    [Fact]
    public async Task LastSuccessExpandsToOlderWindowWhenRecentWindowIsEmpty()
    {
        var input = Fixture("main");
        input["history_pages"] = Array("""
            [{"total_count":0,"workflow_runs":[]},
             {"total_count":1,"workflow_runs":[{"id":100,"created_at":"2026-10-19T09:00:00Z","head_sha":"older"}]}]
            """);

        var result = await ExecuteAsync("last-success", input);

        AssertSuccess(result);
        Assert.Equal("older", result.Output["run"]!["head_sha"]!.GetValue<string>());
        var calls = result.Calls.Where(call => call.Kind == "list-successful-runs").ToArray();
        Assert.Equal(calls[0].Data["start"]!.GetValue<string>(), calls[1].Data["end"]!.GetValue<string>());
    }

    [Fact]
    public async Task LastSuccessSurfacesApiFailure()
    {
        var input = Fixture("main");
        input["faults"]!["list-successful-runs"] = "HTTP 403";

        AssertRejected(await ExecuteAsync("last-success", input));
    }

    [Theory]
    [InlineData("identical")]
    [InlineData("behind")]
    [InlineData("diverged")]
    [InlineData("unknown")]
    public async Task CandidateAttributionRequiresAheadComparison(string relation)
    {
        var input = CandidateFixture();
        input["comparison_pages"]![0]!["status"] = relation;

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["state"]!.GetValue<string>());
        Assert.Empty(result.Output["candidates"]!.AsArray());
    }

    [Theory]
    [InlineData("total_commits", "0")]
    [InlineData("total_commits", "1.5")]
    [InlineData("total_commits", "\"1\"")]
    [InlineData("commits", "null")]
    public async Task CandidateAttributionRejectsMalformedComparison(string field, string value)
    {
        var input = CandidateFixture();
        input["comparison_pages"]![0]![field] = JsonNode.Parse(value);

        var result = await ExecuteAsync("candidate-merges", input);

        Assert.Equal("unavailable", result.Output["state"]!.GetValue<string>());
        Assert.Empty(result.Output["candidates"]!.AsArray());
        Assert.NotEmpty(result.Diagnostics);
    }

    [Fact]
    public async Task CandidateAttributionDeduplicatesCommitsAndPrAssociationsAcrossPages()
    {
        var input = CandidateFixture();
        input["comparison_pages"]!.AsArray().Add(input["comparison_pages"]![0]!.DeepClone());
        input["candidate_pr_pages"]!["merge-sha"]!.AsArray().Add(input["candidate_pr_pages"]!["merge-sha"]![0]!.DeepClone());

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("available", result.Output["state"]!.GetValue<string>());
        Assert.Equal(42, Assert.Single(result.Output["candidates"]!.AsArray())!["pull_request"]!["number"]!.GetValue<int>());
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("ambiguous")]
    [InlineData("wrong-base")]
    [InlineData("not-merged")]
    [InlineData("api-failure")]
    [InlineData("truncated-comparison")]
    public async Task IncompleteCandidateHistoryNeverBecomesConfidentAttribution(string reason)
    {
        var input = CandidateFixture();
        var association = input["candidate_pr_pages"]!["merge-sha"]![0]![0]!;
        switch (reason)
        {
            case "missing":
                input["candidate_pr_pages"]!["merge-sha"] = Array("[[]]");
                break;
            case "ambiguous":
                var other = association.DeepClone();
                other["number"] = 43;
                input["candidate_pr_pages"]!["merge-sha"]![0]!.AsArray().Add(other);
                break;
            case "wrong-base":
                association["base"]!["ref"] = "release/13.6";
                break;
            case "not-merged":
                association["merged_at"] = null;
                break;
            case "api-failure":
                input["faults"]!["list-commit-prs:merge-sha"] = "HTTP 503";
                break;
            case "truncated-comparison":
                input["comparison_pages"]![0]!["total_commits"] = 2;
                break;
        }

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("incomplete", result.Output["state"]!.GetValue<string>());
        Assert.NotEmpty(result.Diagnostics);
    }

    [Theory]
    [InlineData("")]
    [InlineData("known-success")]
    public async Task MissingCandidateRangeDoesNotGuessFromTriggeringPr(string successfulSha)
    {
        var input = Fixture("main");
        input["last_successful_sha"] = successfulSha;
        input["failed_sha"] = "";

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["state"]!.GetValue<string>());
        Assert.Empty(result.Output["candidates"]!.AsArray());
    }

    private static JsonObject CandidateFixture()
    {
        var input = Fixture("main");
        input["last_successful_sha"] = "successful-sha";
        input["failed_sha"] = "trusted-sha";
        input["comparison_pages"] = Array("""
            [{"status":"ahead","total_commits":1,"commits":[{
              "sha":"merge-sha","commit":{"message":"Merge change"},
              "html_url":"https://github.com/microsoft/aspire/commit/merge-sha"}]}]
            """);
        input["candidate_pr_pages"] = Object("""
            {"merge-sha":[[{"number":42,"title":"Merged change","merged_at":"2026-10-21T09:00:00Z",
              "html_url":"https://github.com/microsoft/aspire/pull/42",
              "base":{"ref":"main","repo":{"full_name":"microsoft/aspire"}}}]]}
            """);

        return input;
    }
}

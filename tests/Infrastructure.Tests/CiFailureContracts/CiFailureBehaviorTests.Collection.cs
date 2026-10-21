// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("push", "main", "main")]
    [InlineData("pull_request", "feature", "pull-request")]
    [InlineData("pull_request_target", "main", "pull-request")]
    public async Task CollectionDerivesScopeFromRunEventAndBranch(string eventName, string branch, string expected)
    {
        var input = Fixture();
        input["run"]!["event"] = eventName;
        input["run"]!["head_branch"] = branch;

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(expected, result.Output["context"]!["run_scope"]!.GetValue<string>());
        Assert.Empty(result.AttemptedMutations);
    }

    [Theory]
    [InlineData("push", "release/13.6")]
    [InlineData("workflow_dispatch", "main")]
    [InlineData("schedule", "main")]
    public async Task CollectionSkipsUnsupportedSourceRuns(string eventName, string branch)
    {
        var input = Fixture();
        input["run"]!["event"] = eventName;
        input["run"]!["head_branch"] = branch;

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.False(result.Output["has_work"]!.GetValue<bool>());
        Assert.Empty(result.AttemptedMutations);
    }

    [Fact]
    public async Task CollectionRejectsAnotherWorkflowEvenIfItIsCalledCi()
    {
        var input = Fixture();
        input["run"]!["name"] = "CI";
        input["run"]!["path"] = ".github/workflows/not-ci.yml";

        AssertRejected(await ExecuteAsync("collect", input));
    }

    [Fact]
    public async Task ManualCollectionSkipsSuccessfulRun()
    {
        var input = Fixture();
        input["run"]!["conclusion"] = "success";

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.False(result.Output["has_work"]!.GetValue<bool>());
        Assert.Empty(result.AttemptedMutations);
    }

    [Fact]
    public async Task AutomaticCollectionReadsEventAttemptNotLatestRerun()
    {
        var input = Fixture("main");
        input["trigger"] = Object("""{"event":"workflow_run","run_id":123,"run_attempt":1,"conclusion":"failure"}""");
        input["current_run"]!["run_attempt"] = 2;

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(1, result.Output["context"]!["run_attempt"]!.GetValue<int>());
        Assert.Equal(1, OnlyCall(result, "get-run").Data["attempt"]!.GetValue<int>());
    }

    [Fact]
    public async Task ManualCollectionUsesLatestAttempt()
    {
        var input = Fixture();
        input["run"]!["run_attempt"] = 2;
        input["current_run"] = input["run"]!.DeepClone();

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(2, result.Output["context"]!["run_attempt"]!.GetValue<int>());
        Assert.Null(OnlyCall(result, "get-run").Data["attempt"]);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-1)]
    public async Task AutomaticCollectionRequiresPositiveAttempt(int attempt)
    {
        var input = Fixture("main");
        input["trigger"] = new JsonObject
        {
            ["event"] = "workflow_run", ["run_id"] = 123, ["run_attempt"] = attempt, ["conclusion"] = "failure",
        };

        AssertRejected(await ExecuteAsync("collect", input));
    }

    [Theory]
    [InlineData(3, "failure", true)]
    [InlineData(4, "failure", false)]
    [InlineData(1, "success", false)]
    [InlineData(1, "cancelled", false)]
    public async Task AutomaticTriggerHonorsConclusionAndAttemptLimit(int attempt, string conclusion, bool hasWork)
    {
        var input = Fixture("main");
        input["trigger"] = new JsonObject
        {
            ["event"] = "workflow_run", ["run_id"] = 123, ["run_attempt"] = attempt, ["conclusion"] = conclusion,
        };
        input["run"]!["run_attempt"] = attempt;

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(hasWork, result.Output["has_work"]!.GetValue<bool>());
        Assert.Equal(hasWork ? CiFailureStatus.Success : CiFailureStatus.Skipped, result.Status);
    }

    [Theory]
    [InlineData("failure")]
    [InlineData("cancelled")]
    [InlineData("timed_out")]
    public async Task CollectionIncludesDiagnosticFailedJobConclusions(string conclusion)
    {
        var input = Fixture();
        input["jobs"]![0]!["conclusion"] = conclusion;
        input["jobs"]![0]!["steps"]![0]!["conclusion"] = conclusion;

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(1, Assert.Single(result.Output["jobs"]!.AsArray())!["id"]!.GetValue<int>());
        Assert.Equal("skipped", result.Output["jobs"]![0]!["steps"]![1]!["conclusion"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("Fail if dependent jobs failed")]
    [InlineData("Check dependent jobs failed")]
    public async Task CollectionExcludesAggregateGateJobs(string step)
    {
        var input = Fixture();
        input["jobs"]!.AsArray().Add(new JsonObject
        {
            ["id"] = 2, ["name"] = "Final Results", ["conclusion"] = "failure",
            ["steps"] = new JsonArray(new JsonObject { ["name"] = step, ["conclusion"] = "failure" }),
        });

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(1, Assert.Single(result.Output["jobs"]!.AsArray())!["id"]!.GetValue<int>());
    }

    [Fact]
    public async Task CollectionWithNoDiagnosticJobsDoesNotAnalyzeEmptyEvidence()
    {
        var input = Fixture();
        input["jobs"] = new JsonArray();

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.False(result.Output["has_work"]!.GetValue<bool>());
    }

    [Fact]
    public async Task SubjectAssociationDeduplicatesSamePrAndIgnoresForeignBaseRepository()
    {
        var input = Fixture();
        input["run"]!["pull_requests"]!.AsArray().Add(input["run"]!["pull_requests"]![0]!.DeepClone());
        input["run"]!["pull_requests"]!.AsArray().Add(Object(
            """{"number":999,"base":{"repo":{"url":"https://api.github.com/repos/other/repo"}}}"""));

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("42", result.Output["context"]!["pr_numbers"]!.GetValue<string>());
        AssertNoCalls(result, call => call.Kind == "list-commit-prs");
    }

    [Fact]
    public async Task AmbiguousRunAssociationCannotBeResolvedByGuessingFromCommit()
    {
        var input = Fixture();
        var another = input["run"]!["pull_requests"]![0]!.DeepClone();
        another["number"] = 43;
        input["run"]!["pull_requests"]!.AsArray().Add(another);
        input["commit_pr_pages"] = Array("""[[{"number":42,"base":{"repo":{"full_name":"microsoft/aspire"}}}]]""");

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("", result.Output["context"]!["pr_numbers"]!.GetValue<string>());
        AssertNoCalls(result, call => call.Kind == "list-commit-prs");
    }

    [Fact]
    public async Task SubjectAssociationSearchesEveryCommitPage()
    {
        var input = Fixture();
        input["run"]!["pull_requests"] = new JsonArray();
        input["commit_pr_pages"] = Array("""
            [[],[{"number":42,"base":{"repo":{"full_name":"microsoft/aspire"}}}]]
            """);

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("42", result.Output["context"]!["pr_numbers"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("trusted-sha", "42")]
    [InlineData("newer-branch-sha", "")]
    public async Task ForkBranchFallbackRequiresExactFailedSha(string sha, string expected)
    {
        var input = Fixture();
        input["run"]!["pull_requests"] = new JsonArray();
        input["commit_pr_pages"] = Array("[[]]");
        input["fork_pr_pages"] = Array(
            $$$"""[[{"number":42,"head":{"sha":"{{{sha}}}"}}]]""");

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(expected, result.Output["context"]!["pr_numbers"]!.GetValue<string>());
        var lookup = OnlyCall(result, "list-fork-prs").Data;
        Assert.Equal("contributor:feature", lookup["head"]!.GetValue<string>());
        Assert.Equal("all", lookup["state"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("list-commit-prs")]
    [InlineData("list-fork-prs")]
    [InlineData("get-run")]
    [InlineData("list-jobs")]
    public async Task EssentialCollectionFailuresAreNotEmptySuccess(string fault)
    {
        var input = Fixture();
        input["run"]!["pull_requests"] = new JsonArray();
        input["faults"]![fault] = "API unavailable";

        AssertRejected(await ExecuteAsync("collect", input));
    }

    [Fact]
    public async Task FailedLogFetchRetainsBoundedSanitizedDiagnosticAndSkippedSteps()
    {
        var input = Fixture();
        input["faults"]!["get-job-log:1"] = "HTTP 504\nAuthorization: Bearer fake-secret";
        input["logs"] = Object("""{"1":"Partial log before download failed"}""");

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        var log = result.Output["logs"]!["1"]!.GetValue<string>();
        Assert.Contains("HTTP 504", log, StringComparison.Ordinal);
        Assert.Contains("Partial log", log, StringComparison.Ordinal);
        Assert.Contains("[REDACTED]", log, StringComparison.Ordinal);
        Assert.Equal("skipped", result.Output["jobs"]![0]!["steps"]![1]!["conclusion"]!.GetValue<string>());
    }

    [Fact]
    public async Task CollectionMakesEveryPriorCauseAvailableBeyondSummaryPreview()
    {
        var input = Fixture();
        for (var i = 0; i < 75; i++)
        {
            var id = $"mechanism-{i:D3}";
            input["memory"]![$"causes/{id}.json"] = new JsonObject
            {
                ["id"] = id, ["type"] = "infra-failure", ["title"] = new string('x', 238),
                ["error_pattern"] = new string('y', 500), ["occurrences"] = new JsonArray(),
            };
        }

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(75, result.Output["prior_causes"]!.AsObject().Count);
        Assert.Equal("mechanism-074", result.Output["prior_causes"]!["mechanism-074.json"]!["id"]!.GetValue<string>());
    }
}

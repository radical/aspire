// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("""[{"name":"Tests.First","job_id":0}]""")]
    [InlineData("""[{"name":"Tests.First","job_id":-1}]""")]
    [InlineData("""[{"name":"Tests.First","job_id":1.5}]""")]
    [InlineData("""[{"name":"Tests.First","job_id":1,"diagnostic":"agent-owned"}]""")]
    public async Task StructuredObservationsRejectInvalidIdsAndUnknownFields(string tests)
    {
        var input = FlakyFixture();
        input["causes"]!["feed-timeout.json"]!["tests"] = JsonNode.Parse(tests);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ObservationCannotClaimKnownJobOmittedFromItsCause()
    {
        var input = FlakyFixture();
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Windows","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        AddFlakyTest(input, "Tests.Second", 2, "Windows");

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task IndependentMechanismsCanOwnDisjointFlakyTestsWithinOneJob()
    {
        var input = FlakyFixture("main");
        AddFlakyTest(input, "Tests.Second");
        input["causes"]!["feed-timeout.json"]!["tests"]!.AsArray().RemoveAt(1);
        input["analysis"]!["causes"]!.AsArray().Add("other-mechanism");
        input["causes"]!["other-mechanism.json"] = Object("""
            {"id":"other-mechanism","type":"flaky-test","title":"Different failure",
             "error_pattern":"different mechanism","job_ids":[1],"tests":[{"name":"Tests.Second","job_id":1}]}
            """);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(2, result.Calls.Count(call => call.Kind == "create-issue"));
        AssertJson(Array("""[{"name":"Tests.First","job_id":1}]"""),
            PersistedCause(result)["occurrences"]![0]!["tests"]);
        AssertJson(Array("""[{"name":"Tests.Second","job_id":1}]"""),
            result.Memory["causes/other-mechanism.json"]!["occurrences"]![0]!["tests"]);
    }

    [Fact]
    public async Task LegacyScalarCauseCannotCoverOnlySomeOfReportedFlakyTests()
    {
        var input = FlakyFixture();
        AddFlakyTest(input, "Tests.Second");
        input["causes"]!["feed-timeout.json"]!.AsObject().Remove("tests");
        input["causes"]!["feed-timeout.json"]!["test_name"] = "Tests.First";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task CauseUnknownFieldIsRejectedEvenWhenItLooksBenign()
    {
        var input = Fixture();
        input["causes"]!["feed-timeout.json"]!["description"] = "Unrecognized field";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task FlakyVerdictCannotHideDeterministicTestInSameJob()
    {
        var input = FlakyFixture();
        input["test_failures"]!.AsArray().Add(Object("""{"test":"Tests.Deterministic","job":"Build","error":"broken code"}"""));
        input["analysis"]!["failed_tests"]!.AsArray().Add(Object("""
            {"name":"Tests.Deterministic","job":"Build","error":"agent copy","classification":"code-issue","reason":"Code regression"}
            """));

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("pull-request", "code-issue")]
    [InlineData("main", "main-repository-breakage")]
    [InlineData("pull-request", "transient-infra")]
    public async Task PureVerdictCannotHideIncompatibleFlakyEvidence(string scope, string verdict)
    {
        var input = FlakyFixture(scope);
        input["analysis"]!["verdict"] = verdict;
        input["analysis"]!["failed_jobs"]![0]!["classification"] = verdict;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task MainMixedVerdictCannotOmitTransientSide()
    {
        var input = Fixture("main", "main-repository-breakage");
        input["analysis"]!["verdict"] = "mixed";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task SecondAttemptRepairsIssueHistoryEvenWhenFirstAttemptRowAlreadyExists()
    {
        var input = Fixture("main");
        var issue = Issue();
        issue["body"] = issue["body"]!.GetValue<string>().Replace(
            "[100](https://github.com/microsoft/aspire/actions/runs/100/attempts/1)",
            $"[123]({RunUrl}/attempts/1)", StringComparison.Ordinal);
        AttachIssue(input, StoredCause(123), issue);
        input["context"]!["run_attempt"] = 2;
        input["run"]!["run_attempt"] = 2;

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal([RunUrl + "/attempts/1", RunUrl + "/attempts/2"], OccurrenceLinks(LatestIssueBody(result)));
        Assert.Equal(2, result.Memory["runs/123.json"]!["run_attempt"]!.GetValue<int>());
        Assert.Equal(2, PersistedCause(result)["occurrences"]!.AsArray().Count);
    }

    [Fact]
    public async Task PublicationCommitsEvidenceThenIssuesThenLinksBeforeComment()
    {
        var result = await ExecuteAsync("publish", Fixture());

        AssertSuccess(result);
        var calls = result.Calls.ToList();
        var evidence = calls.FindIndex(call => call.Kind == "memory-commit");
        var issue = calls.FindIndex(call => call.Kind == "create-issue");
        var links = calls.FindLastIndex(call => call.Kind == "memory-commit");
        var comment = calls.FindIndex(call => call.Kind == "create-comment");
        Assert.True(evidence >= 0 && evidence < issue && issue < links && links < comment);
        Assert.Equal(PersistedCause(result)["issue_url"]!.GetValue<string>(),
            calls[links].Data["files"]!["causes/feed-timeout.json"]!["issue_url"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("create-comment", false)]
    [InlineData("update-comment", true)]
    public async Task FailedCommentMutationDoesNotEraseAlreadyPublishedMemory(string fault, bool existingComment)
    {
        var input = Fixture();
        if (existingComment)
        {
            input["comments"] = Array("""
                [{"id":80,"user":{"login":"github-actions[bot]"},"body":"<!-- analyze-ci-failure -->\nold"}]
                """);
        }
        input["faults"]![fault] = "HTTP 503";

        var result = await ExecuteAsync("publish", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.NotNull(result.Memory["runs/123.json"]);
        Assert.NotNull(PersistedCause(result)["issue_url"]);
        AssertNoCalls(result, call => call.Succeeded && call.Kind is ("create-comment" or "update-comment"));
    }

    [Fact]
    public async Task CollectionRejectsForeignRepositoryBeforeAnyMutation()
    {
        var input = Fixture("main");
        input["repository"] = "other/repo";

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.Empty(result.AttemptedMutations);
    }

    [Fact]
    public async Task CollectionDoesNotPromoteStepLessFailuresIntoDiagnosticJobs()
    {
        var input = Fixture();
        input["jobs"]![0]!["steps"] = new JsonArray();

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.False(result.Output["has_work"]!.GetValue<bool>());
    }

    [Theory]
    [InlineData("check dependent jobs FAILED", false)]
    [InlineData("Compile", true)]
    public async Task AggregateFilterExaminesFirstFailedStepNotAnyLaterStep(string first, bool retained)
    {
        var input = Fixture();
        input["jobs"]![0]!["steps"] = new JsonArray(
            Object("""{"name":"Setup","conclusion":"success"}"""),
            new JsonObject { ["name"] = first, ["conclusion"] = "failure" },
            Object("""{"name":"Check dependent jobs failed","conclusion":"failure"}"""));

        var result = await ExecuteAsync("collect", input);

        Assert.Equal(retained ? CiFailureStatus.Success : CiFailureStatus.Skipped, result.Status);
        Assert.Equal(retained, result.Output["has_work"]!.GetValue<bool>());
    }

    [Fact]
    public async Task CollectionReadsAllJobPagesForPinnedAttempt()
    {
        var input = Fixture();
        input["job_pages"] = new JsonArray(
            input["jobs"]!.DeepClone(),
            Array("""[{"id":2,"name":"Second","conclusion":"failure","steps":[{"name":"Build","conclusion":"failure"}]}]"""));

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal([1, 2], result.Output["jobs"]!.AsArray().Select(job => job!["id"]!.GetValue<int>()).ToArray());
        Assert.All(result.Calls.Where(call => call.Kind == "list-jobs"),
            call => Assert.Equal(1, call.Data["attempt"]!.GetValue<int>()));
    }

    [Fact]
    public async Task CommitAssociationRejectsForeignBaseAndAmbiguousLocalCandidates()
    {
        var input = Fixture();
        input["run"]!["pull_requests"] = new JsonArray();
        input["commit_pr_pages"] = Array("""
            [[{"number":999,"base":{"repo":{"full_name":"other/repo"}}},
              {"number":42,"base":{"repo":{"full_name":"microsoft/aspire"}}},
              {"number":43,"base":{"repo":{"full_name":"microsoft/aspire"}}}]]
            """);

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("", result.Output["context"]!["pr_numbers"]!.GetValue<string>());
        AssertNoCalls(result, call => call.Kind == "list-fork-prs");
    }

    [Theory]
    [InlineData("owner")]
    [InlineData("branch")]
    public async Task ForkLookupDoesNotRunWithoutCompleteBranchIdentity(string missing)
    {
        var input = Fixture();
        input["run"]!["pull_requests"] = new JsonArray();
        input["commit_pr_pages"] = Array("[[]]");
        if (missing == "owner")
        {
            input["run"]!["head_repository"] = new JsonObject();
        }
        else
        {
            input["run"]!["head_branch"] = "";
        }

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("", result.Output["context"]!["pr_numbers"]!.GetValue<string>());
        AssertNoCalls(result, call => call.Kind == "list-fork-prs");
    }

    [Fact]
    public async Task CollectorMemoryReadFailureIsNotMisreportedAsFirstRun()
    {
        var input = Fixture();
        input["faults"]!["memory-read"] = "authentication failed";

        AssertRejected(await ExecuteAsync("collect", input));
    }

    [Fact]
    public async Task CompareTransportFailureIsUnavailableNotEmptyCompleteHistory()
    {
        var input = CandidateFixture();
        input["faults"]!["compare"] = "HTTP 503";

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["state"]!.GetValue<string>());
        Assert.Empty(result.Output["candidates"]!.AsArray());
        Assert.NotEmpty(result.Diagnostics);
    }

    [Fact]
    public async Task IncompleteComparisonRetainsOnlyAuthenticatedCandidateRecords()
    {
        var input = CandidateFixture();
        input["comparison_pages"]![0]!["total_commits"] = 2;
        input["comparison_pages"]![0]!["commits"]!.AsArray().Add(Object("""
            {"sha":"unassociated","commit":{"message":"Unknown association"},
             "html_url":"https://github.com/microsoft/aspire/commit/unassociated"}
            """));
        input["candidate_pr_pages"]!["unassociated"] = Array("[[]]");

        var result = await ExecuteAsync("candidate-merges", input);

        AssertSuccess(result);
        Assert.Equal("incomplete", result.Output["state"]!.GetValue<string>());
        var candidate = Assert.Single(result.Output["candidates"]!.AsArray())!;
        Assert.Equal("merge-sha", candidate["sha"]!.GetValue<string>());
        Assert.Equal("Merged change", candidate["pull_request"]!["title"]!.GetValue<string>());
        Assert.Equal("https://github.com/microsoft/aspire/pull/42", candidate["pull_request"]!["url"]!.GetValue<string>());
        Assert.Equal("2026-10-21T09:00:00Z", candidate["pull_request"]!["merged_at"]!.GetValue<string>());
    }

    [Fact]
    public async Task CappedHistoryCannotLoopForeverAtOneSecondGranularity()
    {
        var input = Fixture("main");
        var pages = new JsonArray();
        for (var i = 0; i < 20; i++)
        {
            pages.Add(Object("""{"total_count":1000,"workflow_runs":[]}"""));
        }
        input["history_pages"] = pages;

        var result = await ExecuteAsync("last-success", input);

        AssertRejected(result);
        Assert.InRange(result.Calls.Count(call => call.Kind == "list-successful-runs"), 1, 20);
    }

    [Fact]
    public async Task ExhaustedHistoryReturnsExplicitlyEmptyRun()
    {
        var input = Fixture("main");
        input["run"]!["created_at"] = "1970-01-01T00:00:01Z";
        input["history_pages"] = Array("""[{"total_count":0,"workflow_runs":[]}]""");

        var result = await ExecuteAsync("last-success", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["run"]!.AsObject());
    }

    [Theory]
    [InlineData(10, true)]
    [InlineData(11, false)]
    public async Task RerunRechecksCauseBudgetIndependentlyOfPublication(int count, bool accepted)
    {
        var input = Fixture("main");
        input["enable_rerun"] = true;
        input["analysis"]!["causes"] = new JsonArray();
        input["causes"] = new JsonObject();
        for (var i = 0; i < count; i++)
        {
            var id = $"cause-{i}";
            input["analysis"]!["causes"]!.AsArray().Add(id);
            input["causes"]![id + ".json"] = new JsonObject
            {
                ["id"] = id, ["type"] = "infra-failure", ["title"] = "Feed timeout",
                ["error_pattern"] = "HTTP 504", ["job_ids"] = new JsonArray(1),
            };
        }

        var result = await ExecuteAsync("rerun", input);
        if (accepted)
        {
            AssertSuccess(result);
            OnlyCall(result, "rerun");
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Theory]
    [InlineData("job_ids", "[1,1]")]
    [InlineData("type", "\"flaky-test\"")]
    public async Task RerunRejectsDuplicateCoverageAndNonInfrastructureCause(string field, string value)
    {
        var input = Fixture("main");
        input["enable_rerun"] = true;
        input["causes"]!["feed-timeout.json"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("complete")]
    [InlineData("not-applicable")]
    public async Task BothAvailableEvidenceStatesPermitInfrastructureRerun(string state)
    {
        var input = Fixture("main");
        input["test_evidence"]!["state"] = state;
        input["enable_rerun"] = true;

        var result = await ExecuteAsync("rerun", input);

        AssertSuccess(result);
        OnlyCall(result, "rerun");
    }
}

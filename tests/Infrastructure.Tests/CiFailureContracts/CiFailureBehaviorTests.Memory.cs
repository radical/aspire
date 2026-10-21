// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task OneMechanismCanCoverSeveralTestsInSeveralJobs()
    {
        var input = FlakyFixture();
        AddFlakyTest(input, "Tests.Second");
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Windows","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        input["causes"]!["feed-timeout.json"]!["job_ids"]!.AsArray().Add(2);
        AddFlakyTest(input, "Tests.Third", 2, "Windows");

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        OnlyCall(result, "create-issue");
        var occurrence = Assert.Single(PersistedCause(result)["occurrences"]!.AsArray())!;
        AssertJson(Array("""[{"name":"Tests.First","job_id":1},{"name":"Tests.Second","job_id":1},{"name":"Tests.Third","job_id":2}]"""),
            occurrence["tests"]);
        AssertJson(new JsonArray(1, 2), occurrence["job_ids"]);
    }

    [Theory]
    [InlineData("""[{"name":"Tests.First","job_id":2}]""")]
    [InlineData("""[{"name":"Tests.Invented","job_id":1}]""")]
    [InlineData("""[{"name":"Tests.First","job_id":1},{"name":"Tests.First","job_id":1}]""")]
    [InlineData("""[{"name":"Tests.First","job_id":"1"}]""")]
    [InlineData("""[{"name":"","job_id":1}]""")]
    [InlineData("[]")]
    public async Task GroupedCauseRequiresExactUniqueTestAndNumericJobEvidence(string tests)
    {
        var input = FlakyFixture();
        input["causes"]!["feed-timeout.json"]!["tests"] = JsonNode.Parse(tests);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task GroupedCauseCannotMixLegacyScalarAndExplicitTests()
    {
        var input = FlakyFixture();
        input["causes"]!["feed-timeout.json"]!["test_name"] = "Tests.First";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task EachFlakyPairHasExactlyOneOwningCause()
    {
        var input = FlakyFixture();
        input["analysis"]!["causes"]!.AsArray().Add("second-mechanism");
        var secondCause = input["causes"]!["feed-timeout.json"]!.DeepClone();
        secondCause["id"] = "second-mechanism";
        input["causes"]!["second-mechanism.json"] = secondCause;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task EveryFlakyPairNeedsAnOwnerEvenWhenItsJobIsAlreadyCovered()
    {
        var input = FlakyFixture();
        AddFlakyTest(input, "Tests.Second");
        input["causes"]!["feed-timeout.json"]!["tests"]!.AsArray().RemoveAt(1);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task GroupedCauseCannotListJobWithoutAnObservedTest()
    {
        var input = FlakyFixture();
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Windows","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        input["causes"]!["feed-timeout.json"]!["job_ids"]!.AsArray().Add(2);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task SameTestNameInDifferentJobsRetainsSeparateEvidence()
    {
        var input = FlakyFixture();
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Windows","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        input["causes"]!["feed-timeout.json"]!["job_ids"]!.AsArray().Add(2);
        AddFlakyTest(input, "Tests.First", 2, "Windows");
        input["test_failures"]![1]!["error"] = "Windows diagnostic";

        var result = await ExecuteAsync("validate", input);

        AssertSuccess(result);
        Assert.Collection(result.Output["analysis"]!["failed_tests"]!.AsArray(),
            test => Assert.Equal("trusted error", test!["error"]!.GetValue<string>()),
            test => Assert.Equal("Windows diagnostic", test!["error"]!.GetValue<string>()));
    }

    [Theory]
    [InlineData(200, true)]
    [InlineData(201, false)]
    public async Task GroupedCauseHasExplicitObservationBudget(int count, bool accepted)
    {
        var input = FlakyFixture();
        for (var i = 1; i < count; i++)
        {
            AddFlakyTest(input, $"Tests.Case{i}");
        }

        var result = await ExecuteAsync("validate", input);
        if (accepted)
        {
            AssertSuccess(result);
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Theory]
    [InlineData("Tests.Old")]
    [InlineData("Tests.First / Tests.Second")]
    [InlineData("Legacy shard label")]
    [InlineData("")]
    public async Task MechanismRecurrenceCanUseDifferentTestWithoutRewritingLegacyDescription(string legacyName)
    {
        var input = FlakyFixture();
        var stored = StoredCause(100);
        stored["type"] = "flaky-test";
        stored["test_name"] = legacyName;
        AttachIssue(input, stored, Issue(type: "flaky-test"));
        input["prior_causes"]!["feed-timeout.json"] = stored.DeepClone();

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Equal(legacyName, PersistedCause(result)["test_name"]!.GetValue<string>());
        Assert.Equal(2, PersistedCause(result)["occurrences"]!.AsArray().Count);
        AssertNoCalls(result, call => call.Kind == "create-issue");
        Assert.Equal(90, OnlyCall(result, "update-issue").Data["number"]!.GetValue<int>());
    }

    [Fact]
    public async Task LegacyScalarCurrentCauseIsAcceptedWhenItNamesExactTrustedTest()
    {
        var input = FlakyFixture();
        input["causes"]!["feed-timeout.json"]!.AsObject().Remove("tests");
        input["causes"]!["feed-timeout.json"]!["test_name"] = "Tests.First";

        AssertSuccess(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task JoinedLegacyDisplayLabelIsNotCurrentVerifiedTestEvidence()
    {
        var input = FlakyFixture();
        input["causes"]!["feed-timeout.json"]!.AsObject().Remove("tests");
        input["causes"]!["feed-timeout.json"]!["test_name"] = "Tests.First / Tests.Second";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task PublicationInitializesMemoryAndPersistsBeforeIssueMutation()
    {
        var result = await ExecuteAsync("publish", Fixture("main"));

        AssertSuccess(result);
        var firstCommit = result.Calls.ToList().FindIndex(call => call.Kind == "memory-commit");
        var issueCreation = result.Calls.ToList().FindIndex(call => call.Kind == "create-issue");
        Assert.True(firstCommit >= 0 && firstCommit < issueCreation);
        var beforeIssue = result.Calls[firstCommit].Data["files"]!;
        Assert.NotNull(beforeIssue["runs/123.json"]);
        Assert.Single(beforeIssue["causes/feed-timeout.json"]!["occurrences"]!.AsArray());
        Assert.Null(beforeIssue["causes/feed-timeout.json"]!["issue_url"]);
        Assert.Equal(OnlyCall(result, "create-issue").Data["url"]!.GetValue<string>(),
            PersistedCause(result)["issue_url"]!.GetValue<string>());
    }

    [Fact]
    public async Task ReusedCauseKeepsAuthoritativeDiagnosticsAndIssueLink()
    {
        var input = Fixture();
        AttachIssue(input);
        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var cause = PersistedCause(result);
        Assert.Equal("Original title", cause["title"]!.GetValue<string>());
        Assert.Equal("Original diagnostic", cause["error_pattern"]!.GetValue<string>());
        Assert.Equal("https://github.com/microsoft/aspire/issues/90", cause["issue_url"]!.GetValue<string>());
        Assert.Equal(2, cause["occurrences"]!.AsArray().Count);
        Assert.Null(cause["job_ids"]);
        Assert.Null(cause["tests"]);
    }

    [Theory]
    [InlineData("id", "\"different-cause\"")]
    [InlineData("type", "\"flaky-test\"")]
    public async Task FreshMemoryIdentityIsRevalidatedBeforeAnyMutation(string field, string value)
    {
        var input = Fixture();
        input["memory"]!["causes/feed-timeout.json"] = StoredCause(100);
        input["memory"]!["causes/feed-timeout.json"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("publish", input));
    }

    [Fact]
    public async Task PublicationUsesTrustedIdentityInsteadOfAgentRunMetadata()
    {
        var input = Fixture("main");
        input["analysis"]!["run_attempt"] = 999;
        input["analysis"]!["run_url"] = "https://evil.example";
        input["analysis"]!["analyzed_at"] = "1900-01-01";
        input["analysis"]!["failed_jobs"]![0]!["name"] = "Invented job";
        input["analysis"]!["main_context"] = Object("""{"failed_sha":"agent-sha"}""");

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var summary = result.Memory["runs/123.json"]!;
        Assert.Equal(123, summary["run_id"]!.GetValue<int>());
        Assert.Equal(1, summary["run_attempt"]!.GetValue<int>());
        Assert.Equal(RunUrl, summary["run_url"]!.GetValue<string>());
        Assert.Equal(Now, summary["analyzed_at"]!.GetValue<string>());
        Assert.Equal("trusted-sha", summary["main_context"]!["failed_sha"]!.GetValue<string>());
        Assert.Equal("Build", summary["failed_jobs"]![0]!["name"]!.GetValue<string>());
        Assert.Null(summary["pr"]);
    }

    [Theory]
    [InlineData("main", "", "main")]
    [InlineData("pull-request", "42", "#42")]
    [InlineData("pull-request", "", "unavailable")]
    public async Task OccurrenceContextComesOnlyFromTrustedScope(string scope, string pr, string expected)
    {
        var input = Fixture(scope);
        input["context"]!["pr_numbers"] = pr;
        if (pr.Length == 0)
        {
            input["analysis"]!["pr"] = null;
        }

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var occurrence = Assert.Single(PersistedCause(result)["occurrences"]!.AsArray())!;
        Assert.Equal(scope, occurrence["run_scope"]!.GetValue<string>());
        Assert.Equal(expected, occurrence["issue_context"]!.GetValue<string>());
        Assert.Equal(RunUrl + "/attempts/1", occurrence["run_url"]!.GetValue<string>());
    }

    [Fact]
    public async Task IdenticalReplayDoesNotDuplicateOrChangeStoredOccurrence()
    {
        var input = FlakyFixture("main");
        var first = await ExecuteAsync("publish", input);
        AssertSuccess(first);
        var replay = input.DeepClone().AsObject();
        replay["memory"] = first.Memory.DeepClone();
        replay["issues"] = new JsonArray(OnlyCall(first, "create-issue").Data.DeepClone());
        replay["now"] = "2026-10-22T12:00:00Z";

        var second = await ExecuteAsync("publish", replay);

        AssertSuccess(second);
        AssertJson(PersistedCause(first)["occurrences"], PersistedCause(second)["occurrences"]);
        AssertNoCalls(second, call => call.Kind == "create-issue");
    }

    [Fact]
    public async Task DifferentAttemptsAreDistinctOccurrencesWithImmutableLinks()
    {
        var input = Fixture("main");
        AttachIssue(input, StoredCause(123));
        input["context"]!["run_attempt"] = 2;
        input["run"]!["run_attempt"] = 2;

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Collection(PersistedCause(result)["occurrences"]!.AsArray(),
            row => Assert.Equal(RunUrl + "/attempts/1", row!["run_url"]!.GetValue<string>()),
            row => Assert.Equal(RunUrl + "/attempts/2", row!["run_url"]!.GetValue<string>()));
    }

    [Fact]
    public async Task ReplayCannotReplaceVerifiedTestsWithNewAnalyzerEvidence()
    {
        var input = FlakyFixture("main");
        var first = await ExecuteAsync("publish", input);
        AssertSuccess(first);
        input["memory"] = first.Memory.DeepClone();
        input["issues"] = new JsonArray(OnlyCall(first, "create-issue").Data.DeepClone());
        AddFlakyTest(input, "Tests.LaterClaim");

        var replay = await ExecuteAsync("publish", input);

        AssertSuccess(replay);
        AssertJson(PersistedCause(first)["occurrences"], PersistedCause(replay)["occurrences"]);
    }

    [Fact]
    public async Task LegacyRunOnlyOccurrenceKeepsUnknownAttemptUntilTrustedReplay()
    {
        var input = Fixture("main");
        var cause = StoredCause(100);
        cause["occurrences"]![0]!.AsObject().Remove("run_attempt");
        cause["occurrences"]![0]!["run_url"] = "https://github.com/microsoft/aspire/actions/runs/100";
        AttachIssue(input, cause);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var historical = PersistedCause(result)["occurrences"]![0]!;
        Assert.Null(historical["run_attempt"]);
        Assert.Equal("https://github.com/microsoft/aspire/actions/runs/100", historical["run_url"]!.GetValue<string>());
    }

    [Fact]
    public async Task CodeIssueIsCommentOnlyAndDoesNotReadOrWriteMemory()
    {
        var input = Fixture(verdict: "code-issue");
        input["faults"]!["memory-read"] = "forbidden";

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.Empty(result.Memory);
        Assert.Collection(result.AttemptedMutations, call => Assert.Equal("create-comment", call.Kind));
        AssertNoCalls(result, call => call.Kind is "memory-read" or "memory-commit");
    }

    [Theory]
    [InlineData("memory-read")]
    [InlineData("memory-commit:1")]
    public async Task MemoryFailureBeforeInitialCommitPreventsIssueAndCommentMutations(string fault)
    {
        var input = Fixture("main");
        input["faults"]![fault] = "permission denied";

        var result = await ExecuteAsync("publish", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Empty(result.Mutations);
        AssertNoCalls(result, call => call.Kind is "create-issue" or "update-issue" or "reopen-issue" or "create-comment" or "update-comment");
        Assert.Empty(result.Memory);
        if (fault == "memory-commit:1")
        {
            Assert.False(OnlyCall(result, "memory-commit").Succeeded);
        }
    }

    [Fact]
    public async Task MissingMemoryBranchIsDifferentFromAuthenticationFailure()
    {
        var input = Fixture("main");
        input["memory_branch_exists"] = false;

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.NotNull(result.Memory["runs/123.json"]);
        OnlyCall(result, "create-issue");
    }
}

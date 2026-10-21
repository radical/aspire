// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("context")]
    [InlineData("run")]
    [InlineData("jobs")]
    [InlineData("analysis")]
    [InlineData("test_evidence")]
    public async Task ValidationCannotProceedWithoutRequiredDocument(string document)
    {
        var input = Fixture();
        input.Remove(document);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("analysis", "not json")]
    [InlineData("analysis", "[]")]
    [InlineData("jobs", "{}")]
    [InlineData("test_evidence", "[]")]
    [InlineData("causes/feed-timeout.json", "{broken")]
    public async Task ValidationRejectsMalformedRawDocuments(string document, string raw)
    {
        var input = Fixture();
        input["raw_documents"] = new JsonObject { [document] = raw };

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("main", "transient-infra")]
    [InlineData("main", "main-repository-breakage")]
    [InlineData("pull-request", "transient-infra")]
    [InlineData("pull-request", "code-issue")]
    public async Task ValidationAcceptsConsistentScopeAndVerdict(string scope, string verdict)
        => AssertSuccess(await ExecuteAsync("validate", Fixture(scope, verdict)));

    [Theory]
    [InlineData("main", "code-issue")]
    [InlineData("pull-request", "main-repository-breakage")]
    [InlineData("main", "unknown")]
    [InlineData("pull-request", "flaky")]
    public async Task ValidationRejectsUnsupportedVerdicts(string scope, string verdict)
        => AssertRejected(await ExecuteAsync("validate", Fixture(scope, verdict)));

    [Theory]
    [InlineData("run_id", "999")]
    [InlineData("run_id", "\"123\"")]
    [InlineData("run_scope", "\"main\"")]
    [InlineData("pr", "{\"number\":43}")]
    [InlineData("pr", "42")]
    [InlineData("pr", "false")]
    public async Task ValidationRejectsReboundRunOrSubject(string field, string value)
    {
        var input = Fixture();
        input["analysis"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("")]
    [InlineData("42 43")]
    [InlineData("42,43")]
    public async Task ValidationCannotInventSubjectWhenTrustedAssociationIsUnavailable(string numbers)
    {
        var input = Fixture();
        input["context"]!["pr_numbers"] = numbers;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ValidationAcceptsExplicitlyUnavailableSubject()
    {
        var input = Fixture();
        input["context"]!["pr_numbers"] = "";
        input["analysis"]!["pr"] = null;

        AssertSuccess(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task MainValidationCannotBlameTriggeringMergeAsSubject()
    {
        var input = Fixture("main");
        input["analysis"]!["pr"] = Object("""{"number":42}""");

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("id", "999")]
    [InlineData("html_url", "\"https://evil.example/actions/runs/123\"")]
    [InlineData("html_url", "\"https://github.com/microsoft/aspire/actions/runs/1234\"")]
    [InlineData("html_url", "\"https://github.com/microsoft/aspire/actions/runs/123\\n::warning::injected\"")]
    public async Task ValidationRejectsInvalidTrustedRunMetadata(string field, string value)
    {
        var input = Fixture();
        input["run"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("""[{"id":2,"classification":"transient-infra"}]""")]
    [InlineData("""[{"id":1,"classification":"transient-infra"},{"id":1,"classification":"transient-infra"}]""")]
    [InlineData("""[{"id":"1","classification":"transient-infra"}]""")]
    [InlineData("""[{"id":1,"classification":"unknown"}]""")]
    [InlineData("""[{"id":1.5,"classification":"transient-infra"}]""")]
    public async Task ValidationRequiresExactUniqueNumericFailedJobCoverage(string jobs)
    {
        var input = Fixture();
        input["analysis"]!["failed_jobs"] = JsonNode.Parse(jobs);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("unavailable")]
    [InlineData("unknown")]
    [InlineData("")]
    public async Task ValidationRejectsUnusableTestEvidence(string state)
    {
        var input = Fixture();
        input["test_evidence"]!["state"] = state;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("name", "\"Tests.Invented\"")]
    [InlineData("job", "\"Different job\"")]
    [InlineData("name", "\"\"")]
    [InlineData("classification", "\"unknown\"")]
    [InlineData("error", "42")]
    public async Task ValidationAuthenticatesEachTestAndJobPair(string field, string value)
    {
        var input = FlakyFixture();
        input["analysis"]!["failed_tests"]![0]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("omit")]
    [InlineData("duplicate-reported")]
    [InlineData("conflicting-trusted")]
    public async Task ValidationRejectsIncompleteOrAmbiguousTestEvidence(string mutation)
    {
        var input = FlakyFixture();
        if (mutation == "omit")
        {
            input["analysis"]!["failed_tests"] = new JsonArray();
        }
        else if (mutation == "duplicate-reported")
        {
            input["analysis"]!["failed_tests"]!.AsArray().Add(input["analysis"]!["failed_tests"]![0]!.DeepClone());
        }
        else
        {
            var conflict = input["test_failures"]![0]!.DeepClone();
            conflict["error"] = "different diagnostic";
            input["test_failures"]!.AsArray().Add(conflict);
        }

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ValidationDeduplicatesIdenticalTrustedRecords()
    {
        var input = FlakyFixture();
        input["test_failures"]!.AsArray().Add(input["test_failures"]![0]!.DeepClone());

        AssertSuccess(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ValidationReconstructsAllDiagnosticFieldsFromTrustedTest()
    {
        var result = await ExecuteAsync("validate", FlakyFixture());

        AssertSuccess(result);
        var test = result.Output["analysis"]!["failed_tests"]![0]!;
        Assert.Equal("trusted error", test["error"]!.GetValue<string>());
        Assert.Equal("trusted frame", test["stack_trace"]!.GetValue<string>());
        Assert.Equal("trusted stdout", test["standard_output"]!.GetValue<string>());
        Assert.Equal("trusted stderr", test["standard_error"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("id", "\"Wrong-ID\"")]
    [InlineData("id", "\"../escape\"")]
    [InlineData("id", "\"different-file\"")]
    [InlineData("type", "\"code-issue\"")]
    [InlineData("title", "\" \"")]
    [InlineData("error_pattern", "\"\\t\\n\"")]
    [InlineData("job_ids", "[]")]
    [InlineData("job_ids", "[0]")]
    [InlineData("job_ids", "[-1]")]
    [InlineData("job_ids", "[1.5]")]
    [InlineData("job_ids", "[\"1\"]")]
    [InlineData("job_ids", "[1,1]")]
    [InlineData("job_ids", "[999]")]
    [InlineData("issue_url", "\"https://github.com/microsoft/aspire/issues/9\"")]
    [InlineData("occurrences", "[]")]
    public async Task ValidationRejectsInvalidOrPublisherOwnedCauseFields(string field, string value)
    {
        var input = Fixture();
        input["causes"]!["feed-timeout.json"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("title", 238, true)]
    [InlineData("title", 239, false)]
    [InlineData("error_pattern", 500, true)]
    [InlineData("error_pattern", 501, false)]
    public async Task ValidationEnforcesCauseFieldBoundaries(string field, int length, bool accepted)
    {
        var input = Fixture();
        input["causes"]!["feed-timeout.json"]![field] = new string('x', length);
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
    [InlineData("[]")]
    [InlineData("""["feed-timeout","feed-timeout"]""")]
    [InlineData("""["missing-cause"]""")]
    public async Task ValidationRequiresSummaryAndFilesToAgree(string causes)
    {
        var input = Fixture();
        input["analysis"]!["causes"] = JsonNode.Parse(causes);

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData(10, true)]
    [InlineData(11, false)]
    public async Task ValidationFailsClosedAtDistinctCauseBudget(int count, bool accepted)
    {
        var input = Fixture();
        input["analysis"]!["causes"] = new JsonArray();
        input["causes"] = new JsonObject();
        for (var i = 0; i < count; i++)
        {
            var id = $"cause-{i}";
            input["analysis"]!["causes"]!.AsArray().Add(id);
            input["causes"]![id + ".json"] = Object(
                $$"""{"id":"{{id}}","type":"infra-failure","title":"Failure","error_pattern":"504","job_ids":[1]}""");
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

    [Fact]
    public async Task ValidationPreservesStoredCauseType()
    {
        var input = Fixture();
        input["prior_causes"]!["feed-timeout.json"] = Object("""{"id":"feed-timeout","type":"flaky-test"}""");

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ValidationRequiresEveryTransientJobToHaveCompatibleCause()
    {
        var input = Fixture();
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Second","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"transient-infra"}"""));

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("main")]
    [InlineData("pull-request")]
    public async Task ValidationAcceptsMixedFailureWithinOneJob(string scope)
    {
        var input = FlakyFixture(scope);
        var deterministic = scope == "main" ? "main-repository-breakage" : "code-issue";
        input["analysis"]!["verdict"] = "mixed";
        input["analysis"]!["failed_jobs"]![0]!["classification"] = deterministic;
        if (scope == "main")
        {
            input["analysis"]!["causes"]!.AsArray().Add("main-break");
            input["causes"]!["main-break.json"] = Object("""
                {"id":"main-break","type":"main-repository-breakage","title":"Broken build",
                 "error_pattern":"CS1234","job_ids":[1]}
                """);
        }

        AssertSuccess(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("main")]
    [InlineData("pull-request")]
    public async Task ValidationRejectsMixedWithoutDeterministicFailure(string scope)
    {
        var input = Fixture(scope);
        input["analysis"]!["verdict"] = "mixed";

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task ValidationDoesNotTreatSkippedTestAsFlakyEvidence()
    {
        var input = Fixture();
        input["causes"]!["feed-timeout.json"]!["type"] = "flaky-test";
        input["causes"]!["feed-timeout.json"]!["tests"] = Array("""[{"name":"Tests.NeverRan","job_id":1}]""");

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("transient-infra", "code-issue")]
    [InlineData("code-issue", "transient-infra")]
    [InlineData("flaky-test", "code-issue")]
    [InlineData("main-repository-breakage", "transient-infra")]
    public async Task VerdictMustAgreeWithEveryJobNotOnlyCausePresence(string verdict, string classification)
    {
        var input = Fixture(verdict == "main-repository-breakage" ? "main" : "pull-request", verdict);
        input["analysis"]!["failed_jobs"]![0]!["classification"] = classification;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData("not-applicable")]
    [InlineData("unavailable")]
    public async Task ReportedTestsCannotBeAcceptedWithoutCompleteEvidence(string state)
    {
        var input = FlakyFixture();
        input["test_evidence"]!["state"] = state;

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task InfrastructureCauseCannotClaimStructuredTests()
    {
        var input = Fixture();
        input["causes"]!["feed-timeout.json"]!["tests"] = Array("""[{"name":"Tests.First","job_id":1}]""");

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Fact]
    public async Task FlakyObservationCannotDisambiguateDuplicateTrustedJobNamesByGuessing()
    {
        var input = FlakyFixture();
        var job = input["jobs"]![0]!.DeepClone();
        job["id"] = 2;
        input["jobs"]!.AsArray().Add(job);
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        input["causes"]!["feed-timeout.json"]!["job_ids"]!.AsArray().Add(2);
        input["causes"]!["feed-timeout.json"]!["tests"]!.AsArray().Add(Object("""{"name":"Tests.First","job_id":2}"""));

        AssertRejected(await ExecuteAsync("validate", input));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task CommentByteBudgetOnlyAppliesWhenThereIsAnActionableSubject(bool hasSubject)
    {
        var input = Fixture(verdict: "code-issue");
        input["jobs"] = new JsonArray();
        input["analysis"]!["failed_jobs"] = new JsonArray();
        if (!hasSubject)
        {
            input["context"]!["pr_numbers"] = "";
            input["analysis"]!["pr"] = null;
        }
        for (var i = 1; i <= 150; i++)
        {
            input["jobs"]!.AsArray().Add(new JsonObject { ["id"] = i, ["name"] = $"Job {i}" });
            input["analysis"]!["failed_jobs"]!.AsArray().Add(new JsonObject
            {
                ["id"] = i, ["classification"] = "code-issue", ["reason"] = new string('x', 500),
            });
        }

        var result = await ExecuteAsync("validate", input);

        if (hasSubject)
        {
            AssertRejected(result);
        }
        else
        {
            AssertSuccess(result);
        }
    }
}

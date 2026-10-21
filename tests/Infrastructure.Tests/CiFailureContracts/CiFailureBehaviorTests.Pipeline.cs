// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text;
using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("rebound-run")]
    [InlineData("unavailable-evidence")]
    [InlineData("omitted-test")]
    [InlineData("duplicate-job")]
    [InlineData("unowned-pair")]
    [InlineData("publisher-owned-field")]
    [InlineData("malformed-analysis")]
    [InlineData("malformed-cause")]
    public async Task PublicationCannotBypassItsValidationBoundary(string mutation)
    {
        var input = FlakyFixture("main");
        input["memory"]!["operator-owned.json"] = Object("""{"preserve":"unchanged"}""");
        var originalMemory = input["memory"]!.DeepClone();
        switch (mutation)
        {
            case "rebound-run":
                input["analysis"]!["run_id"] = 999;
                break;
            case "unavailable-evidence":
                input["test_evidence"]!["state"] = "unavailable";
                break;
            case "omitted-test":
                input["analysis"]!["failed_tests"] = new JsonArray();
                break;
            case "duplicate-job":
                input["analysis"]!["failed_jobs"]!.AsArray().Add(input["analysis"]!["failed_jobs"]![0]!.DeepClone());
                break;
            case "unowned-pair":
                AddFlakyTest(input, "Tests.Unowned");
                input["causes"]!["feed-timeout.json"]!["tests"]!.AsArray().RemoveAt(1);
                break;
            case "publisher-owned-field":
                input["causes"]!["feed-timeout.json"]!["issue_url"] = "https://github.com/microsoft/aspire/issues/90";
                break;
            case "malformed-analysis":
                input["raw_documents"] = Object("""{"analysis":"not json"}""");
                break;
            case "malformed-cause":
                input["raw_documents"] = Object("""{"causes/feed-timeout.json":"{broken"}""");
                break;
        }

        var result = await ExecuteAsync("publish", input);

        AssertRejected(result);
        AssertJson(originalMemory, result.Memory);
    }

    [Theory]
    [InlineData("flaky")]
    [InlineData("code")]
    [InlineData("main-break")]
    [InlineData("mixed-pr")]
    [InlineData("mixed-main")]
    public async Task ValidNonInfrastructureAnalysisStillCannotRequestRerun(string kind)
    {
        var input = kind switch
        {
            "code" => Fixture(verdict: "code-issue"),
            "main-break" => Fixture("main", "main-repository-breakage"),
            "mixed-pr" => MixedFixture("pull-request"),
            "mixed-main" => MixedFixture("main"),
            _ => FlakyFixture(),
        };
        input["enable_rerun"] = true;
        AssertSuccess(await ExecuteAsync("validate", input.DeepClone().AsObject()));

        var result = await ExecuteAsync("rerun", input);

        AssertRejected(result);
    }

    [Fact]
    public async Task FreshMemoryWinsOverNonemptyStaleCollectionSnapshot()
    {
        var input = Fixture("main");
        var prior = StoredCause(100);
        input["prior_causes"]!["feed-timeout.json"] = prior;
        var fresh = StoredCause(100, 101);
        fresh["title"] = "Fresh authoritative title";
        fresh["error_pattern"] = "Fresh authoritative pattern";
        fresh["issue_url"] = "https://github.com/microsoft/aspire/issues/91";
        input["memory"]!["causes/feed-timeout.json"] = fresh;
        input["memory"]!["operator-owned.json"] = Object("""{"preserve":"unchanged"}""");
        input["issues"] = new JsonArray(Issue(90), Issue(91));

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var cause = PersistedCause(result);
        Assert.Equal([100L, 101L, 123L], cause["occurrences"]!.AsArray()
            .Select(row => row!["run_id"]!.GetValue<long>()).ToArray());
        Assert.Equal("Fresh authoritative title", cause["title"]!.GetValue<string>());
        Assert.Equal("Fresh authoritative pattern", cause["error_pattern"]!.GetValue<string>());
        Assert.Equal("https://github.com/microsoft/aspire/issues/91", cause["issue_url"]!.GetValue<string>());
        AssertJson(Object("""{"preserve":"unchanged"}"""), result.Memory["operator-owned.json"]);
        Assert.Equal(91, OnlyCall(result, "update-issue").Data["number"]!.GetValue<int>());
        AssertNoCalls(result, call => call.Kind == "create-issue");
    }

    [Theory]
    [InlineData("available", true)]
    [InlineData("incomplete", false)]
    [InlineData("unavailable", false)]
    public async Task PersistedMainAttributionWithholdsPartialHistory(string state, bool exposed)
    {
        var input = Fixture("main");
        input["candidate_history"] = new JsonObject { ["state"] = state };
        input["triggering_merge"] = Object("""
            {"number":42,"title":"Merge context","user":{"login":"author"},"state":"closed",
             "head":{"ref":"feature"},"base":{"ref":"main"},"html_url":"https://github.com/microsoft/aspire/pull/42",
             "merged_at":"2026-10-21T09:00:00Z"}
            """);
        input["candidate_merges"] = Array("""
            [{"sha":"merge-sha","message":"Merge context","html_url":"https://github.com/microsoft/aspire/commit/merge-sha",
              "pull_request":{"number":42,"title":"Merge context","url":"https://github.com/microsoft/aspire/pull/42",
              "merged_at":"2026-10-21T09:00:00Z"}}]
            """);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var summary = result.Memory["runs/123.json"]!;
        Assert.Equal(state, summary["main_context"]!["candidate_merge_history_state"]!.GetValue<string>());
        if (exposed)
        {
            Assert.Equal(42, summary["triggering_merge_pr"]!["number"]!.GetValue<int>());
            AssertJson(input["candidate_merges"], summary["main_context"]!["candidate_merges"]);
        }
        else
        {
            Assert.Null(summary["triggering_merge_pr"]);
            Assert.Null(summary["main_context"]!["candidate_merges"]);
        }
    }

    [Theory]
    [InlineData("""{"state":"open"}""")]
    [InlineData("""{"state":"open","locked":"false"}""")]
    [InlineData("""{"state":42,"locked":false}""")]
    [InlineData("""{"state":"unknown","locked":false}""")]
    public async Task MalformedLivePrStateCannotAuthorizeComment(string pr)
    {
        var input = Fixture();
        input["pull_request"] = Object(pr);

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        Assert.NotEmpty(result.Diagnostics);
        Assert.NotNull(result.Memory["runs/123.json"]);
        AssertNoCalls(result, call => call.Kind is "create-comment" or "update-comment");
    }

    [Fact]
    public async Task CollectionComposesDownloadsExtractionAndParsingWithExactJobProvenance()
    {
        var input = CollectedArtifactsFixture();

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("complete", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Collection(result.Output["test_failures"]!.AsArray(),
            first =>
            {
                Assert.Equal("Tests.Same", first!["test"]!.GetValue<string>());
                Assert.Equal(TestJob, first["job"]!.GetValue<string>());
                Assert.Equal("first diagnostic", first["error"]!.GetValue<string>());
            },
            second =>
            {
                Assert.Equal("Tests.Same", second!["test"]!.GetValue<string>());
                Assert.Equal(SecondTestJob, second["job"]!.GetValue<string>());
                Assert.Equal("second diagnostic", second["error"]!.GetValue<string>());
            });
        Assert.Equal([50, 51], result.Calls.Where(call => call.Kind == "download-artifact")
            .Select(call => call.Data["id"]!.GetValue<int>()).ToArray());
    }

    [Theory]
    [InlineData("download")]
    [InlineData("zip")]
    [InlineData("trx")]
    public async Task LaterRequiredArtifactFailureDiscardsEarlierStructuredEvidence(string stage)
    {
        var input = CollectedArtifactsFixture();
        if (stage == "download")
        {
            input["faults"]!["download-artifact:51"] = "HTTP 404";
        }
        else if (stage == "zip")
        {
            input["artifact_archives"]!["51"] = Convert.ToBase64String("not a zip"u8);
            input["artifacts"]![1]!["size_in_bytes"] = 9;
        }
        else
        {
            SetArtifactArchive(input, 1, 51, ExtractionFixture(("results.trx", "<NotTrx/>", 0)));
        }

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Empty(result.Output["test_failures"]!.AsArray());
        Assert.NotEmpty(result.Diagnostics);
    }

    [Fact]
    public async Task FirstRequiredArtifactFailureDoesNotDownloadLaterArtifacts()
    {
        var input = CollectedArtifactsFixture();
        input["faults"]!["download-artifact:50"] = "HTTP 404";

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Empty(result.Output["test_failures"]!.AsArray());
        AssertNoCalls(result, call => call.Kind == "download-artifact" && call.Data["id"]!.GetValue<int>() == 51);
    }

    [Theory]
    [InlineData("download")]
    [InlineData("zip")]
    public async Task OptionalArtifactFailurePreservesRequiredTrustedResults(string stage)
    {
        var input = CollectedArtifactsFixture();
        AddOptionalArtifact(input);
        if (stage == "download")
        {
            input["faults"]!["download-artifact:52"] = "HTTP 404";
        }
        else
        {
            input["artifact_archives"]!["52"] = Convert.ToBase64String("not a zip"u8);
            input["artifacts"]![2]!["size_in_bytes"] = 9;
        }

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("complete", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Equal(["first diagnostic", "second diagnostic"],
            result.Output["test_failures"]!.AsArray().Select(test => test!["error"]!.GetValue<string>()).ToArray());
    }

    [Fact]
    public async Task SuccessfulExtensionArtifactProducesTrustedMochaEvidenceThroughCollector()
    {
        var input = CollectedArtifactsFixture();
        AddOptionalArtifact(input);
        input["jobs"]!.AsArray().RemoveAt(1);
        input["jobs"]!.AsArray().RemoveAt(0);
        input["artifacts"]!.AsArray().RemoveAt(1);
        input["artifacts"]!.AsArray().RemoveAt(0);

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("complete", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        var test = Assert.Single(result.Output["test_failures"]!.AsArray())!;
        Assert.Equal("suite test", test["test"]!.GetValue<string>());
        Assert.Equal("Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, debug)",
            test["job"]!.GetValue<string>());
        Assert.Equal("mocha diagnostic", test["error"]!.GetValue<string>());
        Assert.Equal(52, OnlyCall(result, "download-artifact").Data["id"]!.GetValue<int>());
    }

    [Fact]
    public async Task CollectorAppliesOneSharedUncompressedBudgetAcrossArtifacts()
    {
        var input = CollectedArtifactsFixture();
        input["budgets"] = new JsonObject
        {
            ["uncompressed_bytes"] = Encoding.UTF8.GetByteCount(CollectTrx("first diagnostic")) +
                Encoding.UTF8.GetByteCount(CollectTrx("second diagnostic")) - 1,
        };

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal("unavailable", result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Theory]
    [InlineData("no-test-jobs", "not-applicable")]
    [InlineData("missing-optional", "not-applicable")]
    [InlineData("missing-required", "unavailable")]
    [InlineData("listing-failed", "unavailable")]
    public async Task CollectorEvidenceStateDistinguishesAbsentOptionalAndFailedRequiredData(string scenario, string state)
    {
        var input = scenario == "no-test-jobs" ? Fixture() : CollectedArtifactsFixture();
        if (scenario == "missing-optional")
        {
            AddOptionalArtifact(input);
            input["jobs"]!.AsArray().RemoveAt(1);
            input["jobs"]!.AsArray().RemoveAt(0);
        }
        input["artifacts"] = new JsonArray();
        if (scenario == "listing-failed")
        {
            input["faults"]!["list-artifacts"] = "HTTP 503";
        }

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(state, result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    private const string SecondTestJob = "Tests / No-package tests / Unit (8-core-ubuntu-latest)";

    private static JsonObject MixedFixture(string scope)
    {
        var input = FlakyFixture(scope);
        input["analysis"]!["verdict"] = "mixed";
        input["analysis"]!["failed_jobs"]![0]!["classification"] =
            scope == "main" ? "main-repository-breakage" : "code-issue";
        if (scope == "main")
        {
            input["analysis"]!["causes"]!.AsArray().Add("main-break");
            input["causes"]!["main-break.json"] = Object("""
                {"id":"main-break","type":"main-repository-breakage","title":"Build regression",
                 "error_pattern":"CS1234","job_ids":[1]}
                """);
        }

        return input;
    }

    private static string CollectTrx(string error)
        => $"""<TestRun><Results><UnitTestResult testName="Tests.Same" outcome="Failed"><Output><ErrorInfo><Message>{error}</Message></ErrorInfo></Output></UnitTestResult></Results></TestRun>""";

    private static JsonObject CollectedArtifactsFixture()
    {
        var input = ArtifactFixture();
        var secondJob = input["jobs"]![0]!.DeepClone();
        secondJob["id"] = 2;
        secondJob["name"] = SecondTestJob;
        input["jobs"]!.AsArray().Add(secondJob);
        var secondArtifact = input["artifacts"]![0]!.DeepClone();
        secondArtifact["id"] = 51;
        secondArtifact["name"] = "logs-Unit-8-core-ubuntu-latest";
        input["artifacts"]!.AsArray().Add(secondArtifact);
        input["artifact_archives"] = new JsonObject();
        SetArtifactArchive(input, 0, 50, ExtractionFixture(("results.trx", CollectTrx("first diagnostic"), 0)));
        SetArtifactArchive(input, 1, 51, ExtractionFixture(("results.trx", CollectTrx("second diagnostic"), 0)));

        return input;
    }

    private static void SetArtifactArchive(JsonObject input, int index, int id, JsonObject archive)
    {
        input["artifact_archives"]![id.ToString(System.Globalization.CultureInfo.InvariantCulture)] = archive["archive_base64"]!.DeepClone();
        input["artifacts"]![index]!["size_in_bytes"] = archive["expected_bytes"]!.DeepClone();
    }

    private static void AddOptionalArtifact(JsonObject input)
    {
        input["jobs"]!.AsArray().Add(Object("""
            {"id":3,"name":"Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, debug)",
             "conclusion":"failure","steps":[{"name":"Run E2E tests","conclusion":"failure"}]}
            """));
        input["artifacts"]!.AsArray().Add(Object("""
            {"id":52,"name":"extension-e2e-diagnostics-linux-x64-debug-attempt1","expired":false,
             "created_at":"2026-10-21T10:30:00Z","size_in_bytes":0}
            """));
        SetArtifactArchive(input, 2, 52, ExtractionFixture(("nested/mocha.json", """
            {"tests":[{"fullTitle":"suite test"}],
             "failures":[{"fullTitle":"suite test","err":{"name":"AssertionError","message":"mocha diagnostic","stack":"frame"}}]}
            """, 0)));
    }
}

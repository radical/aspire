// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("main")]
    [InlineData("pull-request")]
    public async Task RerunOnlyRequestsFailedJobsForTrustedRun(string scope)
    {
        var input = Fixture(scope);
        input["enable_rerun"] = true;

        var result = await ExecuteAsync("rerun", input);

        AssertSuccess(result);
        Assert.Equal(123, OnlyCall(result, "rerun").Data["run_id"]!.GetValue<int>());
        Assert.Collection(result.AttemptedMutations, call => Assert.Equal("rerun", call.Kind));
        Assert.Empty(result.Memory);
    }

    [Fact]
    public async Task MainRerunDoesNotNeedPrAssociation()
    {
        var input = Fixture("main");
        input["enable_rerun"] = true;
        input["faults"]!["get-pr"] = "No PR should be read";

        var result = await ExecuteAsync("rerun", input);

        AssertSuccess(result);
        OnlyCall(result, "rerun");
        AssertNoCalls(result, call => call.Kind == "get-pr");
    }

    [Fact]
    public async Task DryRunValidatesButDoesNotMutateOrReadPrState()
    {
        var input = Fixture();
        input["enable_rerun"] = false;

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.Empty(result.AttemptedMutations);
        AssertNoCalls(result, call => call.Kind == "get-pr");
        Assert.NotEmpty(result.Diagnostics);
    }

    [Fact]
    public async Task DryRunCannotHideInvalidAnalysis()
    {
        var input = Fixture();
        input["analysis"]!["run_id"] = 999;

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("flaky-test")]
    [InlineData("code-issue")]
    [InlineData("mixed")]
    [InlineData("main-repository-breakage")]
    public async Task RerunDoesNotRetryNonInfrastructureVerdicts(string verdict)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["analysis"]!["verdict"] = verdict;

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("run_id", "0")]
    [InlineData("run_id", "-1")]
    [InlineData("run_id", "123.5")]
    [InlineData("run_id", "999")]
    public async Task RerunRequestMustIdentifySamePositiveIntegralRun(string field, string value)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["rerun_request"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("run_attempt", "0")]
    [InlineData("run_attempt", "1.5")]
    [InlineData("run_scope", "\"release\"")]
    public async Task RerunRequiresValidTrustedContext(string field, string value)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["context"]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("analysis-tests")]
    [InlineData("trusted-tests")]
    [InlineData("unavailable-evidence")]
    [InlineData("unknown-evidence")]
    [InlineData("missing-job")]
    [InlineData("duplicate-job")]
    [InlineData("missing-cause")]
    [InlineData("unknown-cause-job")]
    [InlineData("wrong-prior-type")]
    public async Task RerunIndependentlyRevalidatesEvidenceAndCauseCoverage(string mutation)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        switch (mutation)
        {
            case "analysis-tests":
                input["analysis"]!["failed_tests"]!.AsArray().Add(Object("""{"name":"Tests.Failed"}"""));
                break;
            case "trusted-tests":
                input["test_evidence"]!["state"] = "complete";
                input["test_failures"]!.AsArray().Add(Object("""{"test":"Tests.Failed","job":"Build","error":"failed"}"""));
                break;
            case "unavailable-evidence":
                input["test_evidence"]!["state"] = "unavailable";
                break;
            case "unknown-evidence":
                input["test_evidence"]!["state"] = "unknown";
                break;
            case "missing-job":
                input["analysis"]!["failed_jobs"] = new JsonArray();
                break;
            case "duplicate-job":
                input["analysis"]!["failed_jobs"]!.AsArray().Add(input["analysis"]!["failed_jobs"]![0]!.DeepClone());
                break;
            case "missing-cause":
                input["causes"] = new JsonObject();
                break;
            case "unknown-cause-job":
                input["causes"]!["feed-timeout.json"]!["job_ids"] = new JsonArray(2);
                break;
            case "wrong-prior-type":
                input["prior_causes"]!["feed-timeout.json"] = Object("""{"type":"flaky-test"}""");
                break;
        }

        AssertRejected(await ExecuteAsync("rerun", input));
    }

    [Theory]
    [InlineData("closed", false)]
    [InlineData("open", true)]
    public async Task RerunSkipsClosedOrLockedPr(string state, bool locked)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["pull_request"]!["state"] = state;
        input["pull_request"]!["locked"] = locked;

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.Empty(result.AttemptedMutations);
    }

    [Theory]
    [InlineData("")]
    [InlineData("0")]
    [InlineData("42 43")]
    public async Task RerunCannotGuessSubjectPr(string numbers)
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["context"]!["pr_numbers"] = numbers;

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.Empty(result.AttemptedMutations);
    }

    [Fact]
    public async Task RerunPrLookupFailureProducesWarningWithoutMutation()
    {
        var input = Fixture();
        input["enable_rerun"] = true;
        input["faults"]!["get-pr"] = "HTTP 403";

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Empty(result.AttemptedMutations);
    }

    [Fact]
    public async Task RerunRechecksCurrentAttemptAndSkipsStaleRequest()
    {
        var input = Fixture("main");
        input["enable_rerun"] = true;
        input["current_run"]!["run_attempt"] = 2;

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Empty(result.AttemptedMutations);
        OnlyCall(result, "get-current-run");
    }

    [Theory]
    [InlineData("get-current-run")]
    [InlineData("rerun")]
    public async Task RerunApiFailureIsObservableFailureNotSuccess(string fault)
    {
        var input = Fixture("main");
        input["enable_rerun"] = true;
        input["faults"]![fault] = "HTTP 503";

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Empty(result.Mutations);
        Assert.Single(result.Calls, call => call.Kind == fault && !call.Succeeded);
    }
}

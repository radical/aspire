// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("Tests / Package tests - Linux / Infrastructure (8-core-ubuntu-latest)")]
    [InlineData("Tests / Package tests - Windows / Infrastructure (windows-latest)")]
    [InlineData("Tests / Package tests - macOS / Infrastructure (macos-latest)")]
    [InlineData("Tests / CLI archive tests / Infrastructure (8-core-ubuntu-latest)")]
    [InlineData("Tests / No-package tests / nested / Infrastructure (8-core-ubuntu-latest)")]
    public async Task RequiredArtifactContractRecognizesEveryTestCallerFamily(string job)
    {
        var input = ArtifactFixture();
        input["jobs"]![0]!["name"] = job;
        input["jobs"]![0]!["steps"] = new JsonArray();
        input["artifacts"]![0]!["name"] = job.Contains("windows-latest", StringComparison.Ordinal)
            ? "logs-Infrastructure-windows-latest"
            : job.Contains("macos-latest", StringComparison.Ordinal)
                ? "logs-Infrastructure-macos-latest"
                : ArtifactName;

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        Assert.Equal(job, Assert.Single(result.Output["artifacts"]!.AsArray())!["job"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("id", "0")]
    [InlineData("id", "-1")]
    [InlineData("id", "1.5")]
    [InlineData("id", "\"50\"")]
    [InlineData("size_in_bytes", "-1")]
    [InlineData("size_in_bytes", "1.5")]
    [InlineData("size_in_bytes", "\"10\"")]
    public async Task InvalidOptionalArtifactMetadataIsWithheldWithoutRejectingSelection(string field, string value)
    {
        var input = OptionalArtifactFixture();
        input["artifacts"]![0]![field] = JsonNode.Parse(value);

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["artifacts"]!.AsArray());
    }

    [Fact]
    public async Task AmbiguousOptionalArtifactsAreNotSilentlySelected()
    {
        var input = OptionalArtifactFixture();
        var duplicate = input["artifacts"]![0]!.DeepClone();
        duplicate["id"] = 51;
        input["artifacts"]!.AsArray().Add(duplicate);

        AssertRejected(await ExecuteAsync("select-artifacts", input));
    }

    [Theory]
    [InlineData(20, true)]
    [InlineData(21, false)]
    public async Task DefaultRequiredArtifactCountLimitIsTwenty(int count, bool accepted)
    {
        var input = ArtifactFixture();
        input["jobs"] = new JsonArray();
        input["artifacts"] = new JsonArray();
        for (var i = 0; i < count; i++)
        {
            input["jobs"]!.AsArray().Add(new JsonObject
            {
                ["id"] = i + 1,
                ["name"] = $"Tests / No-package tests / Project{i} (ubuntu-latest)",
            });
            input["artifacts"]!.AsArray().Add(new JsonObject
            {
                ["id"] = 50 + i, ["name"] = $"logs-Project{i}-ubuntu-latest",
                ["expired"] = false, ["created_at"] = "2026-10-21T10:30:00Z", ["size_in_bytes"] = 10,
            });
        }

        var result = await ExecuteAsync("select-artifacts", input);

        if (accepted)
        {
            AssertSuccess(result);
            Assert.Equal(20, result.Output["artifacts"]!.AsArray().Count);
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Fact]
    public async Task OptionalBudgetClippingUsesDeterministicJobOrderRatherThanApiOrder()
    {
        var input = OptionalArtifactFixture("zeta");
        var second = OptionalArtifactFixture("alpha");
        second["jobs"]![0]!["id"] = 2;
        second["artifacts"]![0]!["id"] = 51;
        input["jobs"]!.AsArray().Add(second["jobs"]![0]!.DeepClone());
        input["artifacts"]!.AsArray().Add(second["artifacts"]![0]!.DeepClone());
        input["budgets"] = Object("""{"artifact_count":1,"total_bytes":100,"artifact_bytes":100}""");

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        Assert.Equal(51, Assert.Single(result.Output["artifacts"]!.AsArray())!["id"]!.GetValue<int>());
    }

    [Theory]
    [InlineData("Error")]
    [InlineData("Timeout")]
    [InlineData("Aborted")]
    [InlineData("Inconclusive")]
    [InlineData("Disconnected")]
    public async Task TrxCompatibilityCountsOnlyExplicitFailedOutcome(string outcome)
    {
        var input = ResultsFixture("trx",
            $"""<TestRun><Results><UnitTestResult testName="Tests.OtherOutcome" outcome="{outcome}"/></Results></TestRun>""");

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Theory]
    [InlineData("error", "Message", 1000)]
    [InlineData("stack_trace", "StackTrace", 2000)]
    [InlineData("standard_output", "StdOut", 4000)]
    [InlineData("standard_error", "StdErr", 4000)]
    public async Task TrxCollectorBoundsDiagnosticsBeforeValidation(string field, string element, int limit)
    {
        var payload = $"<{element}>{new string('x', limit + 1)}</{element}>";
        if (field is "error" or "stack_trace")
        {
            payload = $"<ErrorInfo>{payload}</ErrorInfo>";
        }
        var input = ResultsFixture("trx",
            $"""<TestRun><Results><UnitTestResult testName="Tests.First" outcome="Failed"><Output>{payload}</Output></UnitTestResult></Results></TestRun>""");

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Equal(new string('x', limit),
            Assert.Single(result.Output["test_failures"]!.AsArray())![field]!.GetValue<string>());
    }

    [Fact]
    public async Task MultipleTrxFilesRetainDistinctDiagnosticsForSameTest()
    {
        var input = ResultsFixture("trx", CollectTrx("first diagnostic"));
        input["files"]!["second.trx"] = CollectTrx("second diagnostic");

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Equal(["first diagnostic", "second diagnostic"], result.Output["test_failures"]!.AsArray()
            .Select(test => test!["error"]!.GetValue<string>()).Order(StringComparer.Ordinal).ToArray());
        Assert.All(result.Output["test_failures"]!.AsArray(), test =>
        {
            Assert.Equal("Tests.Same", test!["test"]!.GetValue<string>());
            Assert.Equal("Build", test["job"]!.GetValue<string>());
        });
    }

    [Theory]
    [InlineData("", 100, "")]
    [InlineData("nonempty", 0, "")]
    [InlineData("a\tb", 100, "a\tb")]
    [InlineData("a\u2066b\u2067c\u2068d\u2069e\ufeff", 100, "abcde")]
    [InlineData("\u001b[?25lvisible\u001b[?25h", 100, "visible")]
    [InlineData("a\u001bb", 100, "ab")]
    [InlineData("password=first;token=second", 100, "password=[REDACTED];token=[REDACTED]")]
    [InlineData("https://example.test?password=first&token=second", 100,
        "https://example.test?password=[REDACTED]&token=[REDACTED]")]
    [InlineData("eyJabcdefghijk.abcdefghijk.abcdefghijk", 100, "[REDACTED]")]
    public async Task SanitizerHandlesEmptyBoundsControlsAndMultipleCredentials(string text, int limit, string expected)
    {
        var input = TextFixture(text);
        input["max_length"] = limit;

        var result = await ExecuteAsync("sanitize-text", input);

        AssertSuccess(result);
        Assert.Equal(expected, result.Output["text"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("main")]
    [InlineData("pull-request")]
    public async Task DryRunDoesNotReadClosedOrLockedPrEvenWhenLookupWouldFail(string scope)
    {
        var input = Fixture(scope);
        input["pull_request"]!["state"] = "closed";
        input["pull_request"]!["locked"] = true;
        input["faults"]!["get-pr"] = "Unexpected lookup";

        var result = await ExecuteAsync("rerun", input);

        Assert.Equal(CiFailureStatus.Skipped, result.Status);
        Assert.Empty(result.AttemptedMutations);
        AssertNoCalls(result, call => call.Kind == "get-pr");
    }

    private static JsonObject OptionalArtifactFixture(string shard = "debug")
    {
        var input = ArtifactFixture();
        input["jobs"]![0]!["name"] = $"Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, {shard})";
        input["jobs"]![0]!["steps"] = new JsonArray();
        input["artifacts"]![0]!["name"] = $"extension-e2e-diagnostics-linux-x64-{shard}-attempt1";

        return input;
    }
}

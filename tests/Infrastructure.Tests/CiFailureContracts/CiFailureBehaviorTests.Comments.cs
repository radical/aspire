// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using VerifyXunit;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("infra")]
    [InlineData("flaky")]
    [InlineData("code")]
    [InlineData("mixed")]
    public async Task PublishedCommentPreservesVerdictRemediationAndTrustedDiagnostics(string kind)
    {
        var result = await ExecuteAsync("publish", CommentFixture(kind));

        AssertSuccess(result);
        var body = OnlyCall(result, "create-comment").Data["body"]!.GetValue<string>();
        await Verifier.Verify(body.TrimEnd('\r', '\n'), "md")
            .UseDirectory("Snapshots")
            .UseFileName($"CiFailureComment.{kind}");
    }

    [Fact]
    public async Task GroupedIssueShowsEachVerifiedTestUnderItsTrustedJob()
    {
        var input = FlakyFixture("main");
        AddFlakyTest(input, "Tests.Second");
        input["jobs"]!.AsArray().Add(Object("""{"id":2,"name":"Windows","conclusion":"failure"}"""));
        input["analysis"]!["failed_jobs"]!.AsArray().Add(Object("""{"id":2,"classification":"flaky-test"}"""));
        input["causes"]!["feed-timeout.json"]!["job_ids"]!.AsArray().Add(2);
        AddFlakyTest(input, "Tests.Third", 2, "Windows");

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var body = OnlyCall(result, "create-issue").Data["body"]!.GetValue<string>();
        var section = ManagedOccurrences(body);
        foreach (var name in new[] { "Tests.First", "Tests.Second", "Tests.Third", "Build", "Windows" })
        {
            Assert.Contains(name, section, StringComparison.Ordinal);
        }
        Assert.Equal(3, PersistedCause(result)["occurrences"]![0]!["tests"]!.AsArray().Count);
    }

    [Fact]
    public async Task LargeGroupedDisplayBoundsVisibleTestsWithoutDroppingStoredEvidence()
    {
        var input = FlakyFixture("main");
        for (var i = 1; i <= 20; i++)
        {
            AddFlakyTest(input, $"Tests.Case{i:D2}");
        }

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var section = ManagedOccurrences(OnlyCall(result, "create-issue").Data["body"]!.GetValue<string>());
        Assert.Contains("1 more tests in the linked run", section, StringComparison.Ordinal);
        Assert.Equal(21, PersistedCause(result)["occurrences"]![0]!["tests"]!.AsArray().Count);
        Assert.InRange(System.Text.Encoding.UTF8.GetByteCount(section), 1, 65000);
    }

    [Fact]
    public async Task ReplayPublicationUsesStoredDateJobAndTestsRatherThanNewAgentClaims()
    {
        var input = FlakyFixture("main");
        var first = await ExecuteAsync("publish", input);
        AssertSuccess(first);
        var firstIssue = OnlyCall(first, "create-issue").Data;
        input["memory"] = first.Memory.DeepClone();
        input["issues"] = new JsonArray(firstIssue.DeepClone());
        input["now"] = "2026-10-22T12:00:00Z";
        input["jobs"]![0]!["name"] = "Renamed job";
        input["test_failures"]![0]!["job"] = "Renamed job";
        input["analysis"]!["failed_tests"]![0]!["job"] = "Renamed job";
        AddFlakyTest(input, "Tests.NewAnalyzerClaim", 1, "Renamed job");

        var replay = await ExecuteAsync("publish", input);

        AssertSuccess(replay);
        AssertJson(PersistedCause(first)["occurrences"], PersistedCause(replay)["occurrences"]);
        var updatedBody = replay.Calls.LastOrDefault(call =>
            call.Kind == "update-issue" && call.Data["body"] is not null)?.Data["body"]?.GetValue<string>()
            ?? firstIssue["body"]!.GetValue<string>();
        Assert.Equal(ManagedOccurrences(firstIssue["body"]!.GetValue<string>()), ManagedOccurrences(updatedBody));
    }

    private static JsonObject CommentFixture(string kind)
    {
        var input = kind switch
        {
            "flaky" => FlakyFixture(),
            "code" => Fixture(verdict: "code-issue"),
            "mixed" => MixedFixture("pull-request"),
            _ => Fixture(),
        };
        const string job = "Build | [link](https://example.test) @reviewers `quoted`";
        input["jobs"]![0]!["name"] = job;
        input["analysis"]!["failed_jobs"]![0]!["reason"] =
            "phase | @reviewers ```\r\nAuthorization: Bearer fake-credential";
        if (kind is "flaky" or "mixed")
        {
            const string test = "Tests.First [`case`](https://example.test) @reviewers";
            input["test_failures"]![0]!["test"] = test;
            input["test_failures"]![0]!["job"] = job;
            input["test_failures"]![0]!["error"] =
                "expected < actual\r\n```\r\n@reviewers\r\nAuthorization: Bearer fake-credential";
            input["test_failures"]![0]!["stack_trace"] =
                "frame1\nframe2\nframe3\nframe4\nframe5\nframe6\nframe7";
            input["test_failures"]![0]!["standard_output"] = "safe stdout\npassword=fake-output";
            input["test_failures"]![0]!["standard_error"] = "safe stderr\nx-api-key: fake-error";
            input["analysis"]!["failed_tests"]![0]!["name"] = test;
            input["analysis"]!["failed_tests"]![0]!["job"] = job;
            input["analysis"]!["failed_tests"]![0]!["reason"] = "Intermittent | @reviewers `quoted`";
            input["causes"]!["feed-timeout.json"]!["tests"]![0]!["name"] = test;
        }

        return input;
    }
}

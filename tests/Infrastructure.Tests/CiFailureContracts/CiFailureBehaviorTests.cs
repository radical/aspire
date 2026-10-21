// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

/// <summary>
/// Implementation-independent acceptance tests. Derive a concrete class and connect
/// <see cref="ExecuteAsync"/> to the real tool and recording external dependencies.
/// </summary>
public abstract partial class CiFailureBehaviorTests
{
    protected const string RunUrl = "https://github.com/microsoft/aspire/actions/runs/123";
    protected const string TestJob = "Tests / No-package tests / Infrastructure (8-core-ubuntu-latest)";
    protected const string ArtifactName = "logs-Infrastructure-8-core-ubuntu-latest";
    protected const string Now = "2026-10-21T12:00:00Z";

    protected abstract Task<CiFailureObservation> ExecuteAsync(string operation, JsonObject input);

    protected static JsonObject Object(string json) => JsonNode.Parse(json)!.AsObject();

    protected static JsonArray Array(string json) => JsonNode.Parse(json)!.AsArray();

    protected static JsonObject Fixture(string scope = "pull-request", string verdict = "transient-infra")
    {
        var input = Object("""
            {
              "repository":"microsoft/aspire",
              "now":"2026-10-21T12:00:00Z",
              "trigger":{"event":"workflow_dispatch","run_id":123},
              "context":{
                "run_id":123,"run_attempt":1,"run_scope":"pull-request",
                "event":"pull_request","head_branch":"feature","head_sha":"trusted-sha","pr_numbers":"42"
              },
              "run":{
                "id":123,"run_attempt":1,"event":"pull_request","path":".github/workflows/ci.yml",
                "workflow_id":99,"head_branch":"feature","head_sha":"trusted-sha",
                "html_url":"https://github.com/microsoft/aspire/actions/runs/123",
                "conclusion":"failure","created_at":"2026-10-21T10:00:00Z",
                "run_started_at":"2026-10-21T10:00:00Z","updated_at":"2026-10-21T11:00:00Z",
                "head_repository":{"owner":{"login":"contributor"}},
                "pull_requests":[{"number":42,"base":{"repo":{"url":"https://api.github.com/repos/microsoft/aspire"}}}]
              },
              "jobs":[{
                "id":1,"name":"Build","conclusion":"failure",
                "html_url":"https://github.com/microsoft/aspire/actions/runs/123/job/1",
                "steps":[{"name":"Restore","conclusion":"failure"},{"name":"Test","conclusion":"skipped"}]
              }],
              "test_evidence":{"state":"not-applicable"},
              "test_failures":[],
              "analysis":{
                "run_id":123,"run_scope":"pull-request","verdict":"transient-infra","pr":{"number":42},
                "failed_jobs":[{"id":1,"classification":"transient-infra","reason":"HTTP 504 during restore"}],
                "failed_tests":[],"causes":["feed-timeout"]
              },
              "causes":{
                "feed-timeout.json":{
                  "id":"feed-timeout","type":"infra-failure","title":"Feed timeout",
                  "error_pattern":"HTTP 504","job_ids":[1]
                }
              },
              "prior_causes":{},
              "memory":{},
              "issues":[],
              "comments":[],
              "next_issue_number":200,
              "next_comment_id":300,
              "pull_request":{"number":42,"state":"open","locked":false,"head":{"sha":"trusted-sha"}},
              "current_run":{"id":123,"run_attempt":1},
              "publish_request":{"run_id":123,"pr_numbers":"42"},
              "rerun_request":{"run_id":123,"pr_numbers":"42","reason":"Feed timeout"},
              "enable_rerun":false,
              "faults":{}
            }
            """);
        input["context"]!["run_scope"] = scope;
        input["analysis"]!["run_scope"] = scope;
        input["analysis"]!["verdict"] = verdict;
        if (scope == "main")
        {
            input["context"]!["event"] = "push";
            input["context"]!["head_branch"] = "main";
            input["context"]!["pr_numbers"] = "";
            input["run"]!["event"] = "push";
            input["run"]!["head_branch"] = "main";
            input["analysis"]!["pr"] = null;
            input["publish_request"]!.AsObject().Remove("pr_numbers");
            input["rerun_request"]!["pr_numbers"] = "";
        }

        if (verdict is "code-issue" or "main-repository-breakage")
        {
            input["analysis"]!["failed_jobs"]![0]!["classification"] = verdict;
            if (verdict == "code-issue")
            {
                input["analysis"]!["causes"] = new JsonArray();
                input["causes"] = new JsonObject();
            }
            else
            {
                input["causes"]!["feed-timeout.json"]!["type"] = verdict;
            }
        }

        return input;
    }

    protected static JsonObject FlakyFixture(string scope = "pull-request")
    {
        var input = Fixture(scope);
        input["analysis"]!["verdict"] = "flaky-test";
        input["analysis"]!["failed_jobs"]![0]!["classification"] = "flaky-test";
        input["test_evidence"]!["state"] = "complete";
        input["test_failures"] = Array("""
            [{"test":"Tests.First","job":"Build","error":"trusted error","stack_trace":"trusted frame",
              "standard_output":"trusted stdout","standard_error":"trusted stderr"}]
            """);
        input["analysis"]!["failed_tests"] = Array("""
            [{"name":"Tests.First","job":"Build","error":"agent error","stack_trace":"agent frame",
              "standard_output":"agent stdout","standard_error":"agent stderr","classification":"flaky","reason":"Intermittent"}]
            """);
        input["causes"]!["feed-timeout.json"] = Object("""
            {"id":"feed-timeout","type":"flaky-test","title":"Shared failure mechanism",
             "error_pattern":"timeout","job_ids":[1],"tests":[{"name":"Tests.First","job_id":1}]}
            """);

        return input;
    }

    protected static void AddFlakyTest(JsonObject input, string name, long jobId = 1, string job = "Build")
    {
        input["test_failures"]!.AsArray().Add(new JsonObject
        {
            ["test"] = name, ["job"] = job, ["error"] = "trusted error", ["stack_trace"] = "",
        });
        input["analysis"]!["failed_tests"]!.AsArray().Add(new JsonObject
        {
            ["name"] = name, ["job"] = job, ["error"] = "agent error",
            ["classification"] = "flaky", ["reason"] = "Intermittent",
        });
        input["causes"]!["feed-timeout.json"]!["tests"]!.AsArray().Add(new JsonObject
        {
            ["name"] = name, ["job_id"] = jobId,
        });
    }

    protected static JsonObject StoredCause(params long[] runs)
    {
        var cause = Object("""
            {"id":"feed-timeout","type":"infra-failure","title":"Original title",
             "error_pattern":"Original diagnostic","issue_url":"https://github.com/microsoft/aspire/issues/90",
             "occurrences":[]}
            """);
        foreach (var run in runs)
        {
            cause["occurrences"]!.AsArray().Add(new JsonObject
            {
                ["run_id"] = run, ["run_attempt"] = 1, ["run_scope"] = "main",
                ["run_url"] = $"https://github.com/microsoft/aspire/actions/runs/{run}/attempts/1",
                ["observed_at"] = "2026-10-20T12:00:00Z", ["job"] = "Historical job",
                ["job_names"] = new JsonArray("Historical job"), ["job_ids"] = new JsonArray(9),
                ["pr_number"] = 0,
            });
        }

        return cause;
    }

    protected static JsonObject Issue(long number = 90, string state = "open", string type = "infra-failure")
        => new()
        {
            ["number"] = number, ["state"] = state,
            ["html_url"] = $"https://github.com/microsoft/aspire/issues/{number}",
            ["labels"] = Array("""[{"name":"ci-failure-cause"}]"""),
            ["body"] = $"<!-- ci-failure-cause:feed-timeout -->\n<!-- ci-failure-cause-type:{type} -->\n\n" +
                $"**Type**: {type}\n\nOperator note before.\n\n" +
                "<!-- ci-failure-occurrences:start -->\n## Occurrences\n\n" +
                "Showing 1 most recent of 1 occurrences.\n\n| Date | Build | Job | Context |\n|------|-------|-----|----|\n" +
                "| 2026-10-20 | [100](https://github.com/microsoft/aspire/actions/runs/100/attempts/1) | ` Historical job ` | main |\n" +
                "<!-- ci-failure-occurrences:end -->\n\nOperator note after.",
        };

    protected static void AttachIssue(JsonObject input, JsonObject? cause = null, JsonObject? issue = null)
    {
        input["memory"]!["causes/feed-timeout.json"] = cause ?? StoredCause(100);
        input["issues"]!.AsArray().Add(issue ?? Issue());
    }

    protected static void AssertSuccess(CiFailureObservation result)
        => Assert.Equal(CiFailureStatus.Success, result.Status);

    protected static void AssertRejected(CiFailureObservation result)
    {
        Assert.Equal(CiFailureStatus.Rejected, result.Status);
        Assert.NotEmpty(result.Diagnostics);
        Assert.Empty(result.AttemptedMutations);
    }

    protected static JsonObject PersistedCause(CiFailureObservation result)
        => result.Memory["causes/feed-timeout.json"]!.AsObject();

    protected static CiFailureCall OnlyCall(CiFailureObservation result, string kind)
        => Assert.Single(result.Calls, call => call.Kind == kind);

    protected static void AssertNoCalls(CiFailureObservation result, Func<CiFailureCall, bool> predicate)
        => Assert.All(result.Calls, call =>
            Assert.False(predicate(call), $"Unexpected call {call.Kind}: {call.Data.ToJsonString()}"));

    protected static void AssertJson(JsonNode? expected, JsonNode? actual)
        => Assert.True(JsonNode.DeepEquals(expected, actual),
            $"Expected {expected?.ToJsonString() ?? "null"}, got {actual?.ToJsonString() ?? "null"}.");
}

public enum CiFailureStatus
{
    Success,
    Skipped,
    Rejected,
}

public sealed record CiFailureCall(string Kind, JsonObject Data, bool Succeeded = true)
{
    public bool IsMutationAttempt => Kind is
        ("memory-commit" or "create-issue" or "update-issue" or "reopen-issue" or
         "create-comment" or "update-comment" or "rerun" or "ensure-label");

    public bool IsMutation => Succeeded && IsMutationAttempt;
}

public sealed record CiFailureObservation(
    CiFailureStatus Status,
    JsonObject Output,
    JsonObject Memory,
    IReadOnlyList<CiFailureCall> Calls,
    IReadOnlyList<string> Diagnostics)
{
    public IEnumerable<CiFailureCall> Mutations => Calls.Where(call => call.IsMutation);
    public IEnumerable<CiFailureCall> AttemptedMutations => Calls.Where(call => call.IsMutationAttempt);
}

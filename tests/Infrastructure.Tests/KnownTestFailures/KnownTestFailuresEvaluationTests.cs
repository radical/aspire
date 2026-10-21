// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Xml.Linq;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public class KnownTestFailuresEvaluationTests(ITestOutputHelper output)
{
    [Fact]
    public void OffDoesNotLoadAnything()
    {
        var decision = Evaluator.Evaluate("off", 17, default, false,
            () => throw new InvalidOperationException(), () => throw new InvalidOperationException(),
            _ => throw new InvalidOperationException());
        Assert.Equal("off", decision.Status);
        Assert.Equal(17, decision.OriginalExitCode);
        Assert.Equal(17, decision.EffectiveExitCode);
        Assert.Null(decision.RegistrySource);
        Assert.Null(decision.Evidence);
        Assert.Empty(decision.Artifacts);
        Assert.Empty(decision.Matches);
        Assert.Empty(decision.Diagnostics);
    }

    [Theory]
    [InlineData("experimental", 0)]
    [InlineData("shadow", 2)]
    public void ExactApprovedFailureIsAllowed(string mode, int expectedExit)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var trx = fixture.Trx();
        var evidence = KnownTestFailuresFixture.Evidence();
        var decision = KnownTestFailuresFixture.Evaluate(trx: trx, mode: mode);
        Assert.Equal("allowed", decision.Status);
        Assert.Equal(2, decision.OriginalExitCode);
        Assert.Equal(expectedExit, decision.EffectiveExitCode);
        Assert.Equal(KnownTestFailuresFixture.Now, decision.EvaluatedAt);
        Assert.Equal(KnownTestFailuresFixture.Source, decision.RegistrySource);
        Assert.Equal(Format.Write(KnownTestFailuresFixture.Registry()), decision.RegistryJson);
        Assert.Equal(Format.Write(evidence), Format.Write(decision.Evidence));
        Assert.Equal(new Match("invocation-1", "stable-case-1", "allow-1", 1), Assert.Single(decision.Matches));
        Assert.Equal(new Artifact("invocation-1", "results.trx", trx), Assert.Single(decision.Artifacts));
        Assert.Empty(decision.Diagnostics);
    }

    [Fact]
    public void PassingCompleteRunNeedsNoAllowance()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var trx = fixture.Trx(new TestTrxCase(content.Method, content.DisplayName, "Passed"));
        var evidence = KnownTestFailuresFixture.Evidence();
        evidence = evidence with { Invocations = [evidence.Invocations[0] with { ProcessExitCode = 0 }] };
        var decision = KnownTestFailuresFixture.Evaluate(new(1, "experimental", []), evidence, trx, originalExit: 0);
        Assert.Equal("pass", decision.Status);
        Assert.Equal(0, decision.EffectiveExitCode);
        Assert.Empty(decision.Matches);
        Assert.Empty(decision.Diagnostics);
    }

    [Theory]
    [InlineData("case")]
    [InlineData("method")]
    [InlineData("display")]
    [InlineData("project")]
    [InlineData("lane")]
    [InlineData("platform")]
    [InlineData("ref")]
    [InlineData("message")]
    [InlineData("unapproved")]
    [InlineData("updated")]
    [InlineData("revoked")]
    [InlineData("removed")]
    [InlineData("manual-expire")]
    [InlineData("expiry-boundary")]
    [InlineData("future-approval")]
    [InlineData("no-opt-in")]
    public void NeighborOrIneligibleAllowanceCannotSuppressFailure(string variant)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var changed = variant switch
        {
            "case" => content with { CaseId = "stable-case-2" },
            "method" => content with { Method = "Example.Tests.Neighbor" },
            "display" => content with { DisplayName = "Flaky(value: 2)" },
            "project" => content with { Project = "tests/Other.Tests/Other.Tests.csproj" },
            "lane" => content with { Lane = "quarantine" },
            "platform" => content with { Platform = "windows-x64" },
            "ref" => content with { Ref = "refs/heads/release" },
            "message" => content with { Message = "new error" },
            _ => content
        };
        var registry = new Registry(1, "experimental",
            [KnownTestFailuresFixture.Event("add", content: changed), KnownTestFailuresFixture.Event("approve", 2)]);
        registry = variant switch
        {
            "unapproved" => registry with { History = [registry.History[0]] },
            "updated" => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("update", 3, 2,
                content: changed with { Owner = "another-team" })),
            "revoked" => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("revoke", 3)),
            "removed" => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("remove", 3)),
            "manual-expire" => RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("expire", 3)),
            "future-approval" => registry with
            {
                History = [registry.History[0], registry.History[1] with { At = KnownTestFailuresFixture.Now.AddMinutes(1) }]
            },
            _ => registry
        };
        var decision = KnownTestFailuresFixture.Evaluate(registry, trx: fixture.Trx(),
            at: variant == "expiry-boundary" ? content.ExpiresAt : null, optIn: variant != "no-opt-in");
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(2, decision.EffectiveExitCode);
        Assert.Single(decision.Diagnostics);
    }

    [Theory]
    [InlineData("missing-artifact")]
    [InlineData("empty-inventory")]
    [InlineData("missing-case")]
    [InlineData("duplicate-case")]
    [InlineData("duplicate-test-id")]
    [InlineData("duplicate-artifact")]
    [InlineData("extra-invocation")]
    [InlineData("missing-invocation")]
    [InlineData("setup")]
    [InlineData("crash")]
    [InlineData("diagnostic")]
    [InlineData("wrapper-exit")]
    [InlineData("non-test-exit")]
    [InlineData("false-success")]
    public void UnprovenCompletenessAndInfrastructureFailuresBlock(string variant)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var evidence = KnownTestFailuresFixture.Evidence();
        var invocation = evidence.Invocations[0];
        invocation = variant switch
        {
            "empty-inventory" => invocation with { ExpectedCases = [] },
            "missing-case" => invocation with { ExpectedCases = [.. invocation.ExpectedCases, new("other", "test-1", "Example.Tests.Other", "other")] },
            "duplicate-case" => invocation with { ExpectedCases = [.. invocation.ExpectedCases, invocation.ExpectedCases[0] with { TestId = "test-1" }] },
            "duplicate-test-id" => invocation with { ExpectedCases = [.. invocation.ExpectedCases, invocation.ExpectedCases[0] with { CaseId = "other" }] },
            "setup" => invocation with { SetupSucceeded = false },
            "crash" => invocation with { CompletedNormally = false },
            "diagnostic" => invocation with { Diagnostics = ["host crashed after writing partial TRX"] },
            "wrapper-exit" => invocation with { DirectMtpProcess = false },
            "non-test-exit" => invocation with { ProcessExitCode = 8 },
            "false-success" => invocation with { ProcessExitCode = 0 },
            _ => invocation
        };
        evidence = evidence with { Invocations = [invocation] };
        evidence = variant switch
        {
            "duplicate-artifact" => evidence with
            {
                SelectedInvocations = ["invocation-1", "invocation-2"],
                Invocations = [invocation, invocation with { Id = "invocation-2", Project = "tests/Other.csproj" }]
            },
            "extra-invocation" => evidence with { SelectedInvocations = [] },
            "missing-invocation" => evidence with { SelectedInvocations = ["invocation-1", "invocation-2"] },
            _ => evidence
        };
        var decision = KnownTestFailuresFixture.Evaluate(evidence: evidence, trx: variant == "missing-artifact" ? null : fixture.Trx());
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(2, decision.EffectiveExitCode);
        Assert.Single(decision.Diagnostics);
    }

    [Theory]
    [InlineData("malformed")]
    [InlineData("dtd")]
    [InlineData("namespace")]
    [InlineData("run-error")]
    [InlineData("run-info")]
    [InlineData("aborted")]
    [InlineData("nested")]
    [InlineData("duplicate-result")]
    [InlineData("duplicate-definition")]
    [InlineData("missing-definition")]
    [InlineData("missing-message")]
    [InlineData("nested-message")]
    [InlineData("result-type")]
    [InlineData("unsupported-result-child")]
    [InlineData("skipped")]
    [InlineData("counter")]
    [InlineData("zero-results")]
    [InlineData("definition-display")]
    public void UnsafeTrxIsNeverTreatedAsSuccess(string variant)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var document = XDocument.Parse(fixture.Trx());
        XNamespace ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010";
        var root = document.Root!;
        var results = root.Element(ns + "Results")!;
        var definitions = root.Element(ns + "TestDefinitions")!;
        var summary = root.Element(ns + "ResultSummary")!;
        switch (variant)
        {
            case "namespace": root.Name = "TestRun"; root.Attribute("xmlns")?.Remove(); break;
            case "run-error": summary.Add(new XElement(ns + "Output", new XElement(ns + "ErrorInfo", new XElement(ns + "Message", "run error")))); break;
            case "run-info": summary.Add(new XElement(ns + "RunInfos", new XElement(ns + "RunInfo", new XAttribute("outcome", "Error")))); break;
            case "aborted": summary.SetAttributeValue("outcome", "Aborted"); break;
            case "nested": results.Elements().Single().Add(new XElement(ns + "InnerResults")); break;
            case "duplicate-result": results.Add(new XElement(results.Elements().Single())); break;
            case "duplicate-definition": definitions.Add(new XElement(definitions.Elements().Single())); break;
            case "missing-definition": definitions.RemoveNodes(); break;
            case "missing-message": root.Descendants(ns + "Message").Single().Remove(); break;
            case "nested-message": root.Descendants(ns + "Message").Single().Add(new XElement(ns + "Message", "nested")); break;
            case "result-type": results.Elements().Single().SetAttributeValue("resultType", "DataDrivenTest"); break;
            case "unsupported-result-child": results.Elements().Single().Add(new XElement(ns + "Unknown")); break;
            case "skipped": results.Elements().Single().SetAttributeValue("outcome", "NotExecuted"); break;
            case "counter": summary.Element(ns + "Counters")!.SetAttributeValue("total", 2); break;
            case "zero-results": results.RemoveNodes(); definitions.RemoveNodes(); break;
            case "definition-display": definitions.Elements().Single().SetAttributeValue("name", "another"); break;
        }
        var xml = variant switch
        {
            "malformed" => "<TestRun",
            "dtd" => "<!DOCTYPE TestRun [<!ENTITY x 'injected'>]>" + document,
            _ => document.ToString()
        };
        var decision = KnownTestFailuresFixture.Evaluate(trx: xml, originalExit: 0);
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(Evaluator.BlockedExitCode, decision.EffectiveExitCode);
        Assert.Single(decision.Diagnostics);
    }

    [Fact]
    public void MixedKnownAndNewFailureRemainsRed()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var trx = fixture.Trx(
            new(content.Method, content.DisplayName, "Failed", content.Message),
            new("Example.Tests.NewFailure", "new failure", "Failed", "new error"));
        var evidence = KnownTestFailuresFixture.Evidence();
        evidence = evidence with
        {
            Invocations = [evidence.Invocations[0] with
            {
                ExpectedCases = [KnownTestFailuresFixture.Case, new("new-case", "test-1", "Example.Tests.NewFailure", "new failure")]
            }]
        };
        var decision = KnownTestFailuresFixture.Evaluate(evidence: evidence, trx: trx);
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(2, decision.EffectiveExitCode);
        Assert.Equal("allow-1", Assert.Single(decision.Matches).AllowanceId);
    }

    [Fact]
    public void ClassPrefixedMethodNameLinksExactly()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var trx = fixture.Trx(new TestTrxCase(content.Method, content.DisplayName, "Failed", content.Message, TestMethodName: content.Method));
        Assert.Equal("allowed", KnownTestFailuresFixture.Evaluate(trx: trx).Status);
    }

    [Theory]
    [InlineData("shadow", 0, 0)]
    [InlineData("shadow", 9, 9)]
    [InlineData("experimental", 0, 3)]
    [InlineData("experimental", 9, 9)]
    public void ReaderErrorsAreAdvisoryOnlyInShadow(string mode, int original, int expected)
    {
        var decision = Evaluator.Evaluate(mode, original, KnownTestFailuresFixture.Now, true,
            () => throw new InvalidDataException("Bad registry"), KnownTestFailuresFixture.Evidence, _ => "");
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(expected, decision.EffectiveExitCode);
        Assert.Equal(["Bad registry"], decision.Diagnostics);
    }

    [Fact]
    public void AggregateSuccessCannotExcuseProjectFailure()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var decision = KnownTestFailuresFixture.Evaluate(trx: fixture.Trx(), originalExit: 0);
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(3, decision.EffectiveExitCode);
    }

    [Theory]
    [InlineData("experimental", 2, "allowed", 0)]
    [InlineData("shadow", 2, "allowed", 2)]
    [InlineData("experimental", 9, "blocked", 9)]
    [InlineData("shadow", 9, "blocked", 9)]
    [InlineData("experimental", 137, "blocked", 137)]
    [InlineData("shadow", 137, "blocked", 137)]
    public void MatchedFailuresCannotExcuseUnsupportedAggregateExits(string mode, int original, string status, int expected)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var decision = KnownTestFailuresFixture.Evaluate(trx: fixture.Trx(), mode: mode, originalExit: original);
        Assert.Equal(status, decision.Status);
        Assert.Equal(original, decision.OriginalExitCode);
        Assert.Equal(expected, decision.EffectiveExitCode);
        Assert.Equal(new Match("invocation-1", "stable-case-1", "allow-1", 1), Assert.Single(decision.Matches));
        Assert.Equal(original == 2 ? [] : new[] { "Original aggregate exit contradicts the supported 0=success, 2=test-failure contract." },
            decision.Diagnostics);
    }

    [Theory]
    [InlineData(-1, "allowed", 0)]
    [InlineData(0, "blocked", 2)]
    [InlineData(1, "blocked", 2)]
    public void ExactUtcExpiryIsExclusive(long ticksFromExpiry, string status, int exit)
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var decision = KnownTestFailuresFixture.Evaluate(trx: fixture.Trx(),
            at: KnownTestFailuresFixture.Content.ExpiresAt.AddTicks(ticksFromExpiry));
        Assert.Equal(status, decision.Status);
        Assert.Equal(exit, decision.EffectiveExitCode);
    }

    [Fact]
    public void ParameterCasesWithIdenticalDisplaysRemainDistinct()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var trx = fixture.Trx(
            new(content.Method, content.DisplayName, "Failed", content.Message),
            new(content.Method, content.DisplayName, "Failed", content.Message));
        var evidence = KnownTestFailuresFixture.Evidence();
        evidence = evidence with
        {
            Invocations = [evidence.Invocations[0] with
            {
                ExpectedCases = [KnownTestFailuresFixture.Case, KnownTestFailuresFixture.Case with { CaseId = "stable-case-2", TestId = "test-1" }]
            }]
        };
        var decision = KnownTestFailuresFixture.Evaluate(evidence: evidence, trx: trx);
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(2, decision.EffectiveExitCode);
        Assert.Equal("stable-case-1", Assert.Single(decision.Matches).CaseId);
        var registry = RegistryHistory.Append(KnownTestFailuresFixture.Registry(),
            KnownTestFailuresFixture.Event("add", 3, id: "allow-2", content: content with { CaseId = "stable-case-2" }));
        registry = RegistryHistory.Append(registry, KnownTestFailuresFixture.Event("approve", 4, id: "allow-2"));
        decision = KnownTestFailuresFixture.Evaluate(registry, evidence, trx);
        Assert.Equal("allowed", decision.Status);
        Assert.Equal(0, decision.EffectiveExitCode);
        Assert.Equal(
            [new Match("invocation-1", "stable-case-1", "allow-1", 1), new Match("invocation-1", "stable-case-2", "allow-2", 1)],
            decision.Matches);
    }

    [Fact]
    public void CompleteMultiProjectRunRequiresEveryInvocation()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        var content = KnownTestFailuresFixture.Content;
        var failed = fixture.Trx();
        var passed = fixture.Trx(new TestTrxCase(content.Method, content.DisplayName, "Passed"));
        var evidence = KnownTestFailuresFixture.Evidence();
        var invocation = evidence.Invocations[0];
        evidence = evidence with
        {
            SelectedInvocations = ["invocation-1", "invocation-2"],
            Invocations = [invocation, invocation with
            {
                Id = "invocation-2", Project = "tests/Second.Tests/Second.Tests.csproj",
                TrxPath = "second.trx", ProcessExitCode = 0
            }]
        };
        var decision = Evaluator.Evaluate("experimental", 2, KnownTestFailuresFixture.Now, true,
            () => new(KnownTestFailuresFixture.Registry(), KnownTestFailuresFixture.Source, Format.Write(KnownTestFailuresFixture.Registry())),
            () => evidence, path => path == "second.trx" ? passed : failed);
        Assert.Equal("allowed", decision.Status);
        Assert.Equal(0, decision.EffectiveExitCode);
        Assert.Equal(["invocation-1", "invocation-2"], decision.Artifacts.Select(a => a.InvocationId));
        Assert.Equal(new Match("invocation-1", "stable-case-1", "allow-1", 1), Assert.Single(decision.Matches));
    }
}

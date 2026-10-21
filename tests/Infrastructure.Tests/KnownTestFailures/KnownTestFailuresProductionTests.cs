// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using System.Xml.Linq;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public sealed class KnownTestFailuresProductionTests(ITestOutputHelper output)
{
    [Fact]
    public async Task ManagementEntryPointExplicitlyInitializesProductionEnvironment()
    {
        using var fixture = new KnownTestFailuresFixture(output);
        fixture.InitializeGit();
        var path = Path.Combine(fixture.Path, "add.json");
        File.WriteAllText(path, Format.Write(KnownTestFailuresFixture.Event("add")));
        var result = await fixture.Cli("mutate", "--repo", fixture.Path,
            "--ref", "refs/heads/ci-registry", "--expected", new string('0', 40),
            "--event", path, "--initialize", "--environment", "github-ci");
        Assert.Equal(0, result.Exit);
        var input = await GitRegistry.ReadBranchAsync(fixture.Path, "refs/heads/ci-registry", "registry.json");
        Assert.Equal("github-ci", input.Registry.Environment);
        Assert.Equal(result.Stdout.Trim(), input.Source.Commit);
        Assert.Null(Assert.Single(RegistryHistory.Replay(input.Registry).Values).Approval);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task CiEntryPointResolvesTheEvaluatedModuleAndCannotEnableForkFailureInjectionUpstream(bool absoluteProject)
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        using var assets = JsonDocument.Parse(File.ReadAllText(
            Path.Combine(RepoRoot.Path, "artifacts", "obj", "Infrastructure.Tests", "project.assets.json")));
        var packages = assets.RootElement.GetProperty("packageFolders").EnumerateObject().First().Name;
        var directory = Path.Combine(fixture.Files.Path, "cli");
        var result = await ProcessRunner.RunAsync(output, fixture.Module.Command,
            [Path.Combine(RepoRoot.Path, "artifacts", "bin", "KnownTestFailures", "Debug", "net10.0", "KnownTestFailures.dll"),
             "ci-run", "--repo", RepoRoot.Path, "--project",
             absoluteProject ? Path.Combine(RepoRoot.Path, KnownTestFailuresProductionFixture.Project) : KnownTestFailuresProductionFixture.Project,
             "--output", directory],
            RepoRoot.Path, new Dictionary<string, string>
            {
                ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
                ["NUGET_PACKAGES"] = packages,
                ["MSBUILDTERMINALLOGGER"] = "false",
                ["GITHUB_REPOSITORY"] = "microsoft/aspire",
                ["KTF_LANE"] = "regular",
                ["KTF_EXECUTION_KIND"] = "normal",
                ["KTF_SCOPE_REF"] = "refs/heads/main",
                ["KTF_EXECUTED_SHA"] = "",
                ["KTF_BASE_ARGS"] = "--ignore-exit-code 8 --timeout 2m",
                ["KTF_EXTRA_ARGS"] = "--filter-class '*.KnownFailurePipelineCases' --filter-not-trait 'quarantined=true' --filter-not-trait 'outerloop=true'",
                ["KTF_VERSION_ARGS"] = "/p:OutDir=\"" + Path.GetDirectoryName(fixture.Module.Assembly) + "/\"",
                ["KTF_REGISTRY_REF"] = "invalid-registry-not-needed-for-passing-tests",
                ["KTF_PROOF"] = "true",
                ["ASPIRE_KNOWN_FAILURE_PIPELINE_PROOF"] = "true",
                ["ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO"] = "candidate"
            });
        Assert.Equal(0, result.ExitCode);
        var request = Format.Read<ProductionRequest>(File.ReadAllText(Path.Combine(directory, "request.json")));
        var module = Assert.Single(request.Modules);
        Assert.Equal(KnownTestFailuresProductionFixture.Project, module.Project);
        Assert.Equal("regular", request.Lane);
        Assert.Equal(fixture.Module.Assembly, module.Assembly);
        Assert.Equal(Path.GetDirectoryName(fixture.Module.Assembly), module.WorkingDirectory);
        Assert.Equal(1, module.Arguments.Count(a => a == "category=failing"));
        var decision = Format.Read<Decision>(File.ReadAllText(Path.Combine(directory, "decision.json")));
        Assert.Equal("pass", decision.Status);
        Assert.Null(decision.RegistrySource);
        Assert.Equal(6, Assert.Single(decision.Evidence!.Invocations).ExpectedCases.Length);
        Assert.True(File.Exists(Path.Combine(directory, "module-0-evaluation.binlog")));

        var baselineDirectory = Path.Combine(fixture.Files.Path, "baseline");
        var baseline = await ProcessRunner.RunAsync(output, fixture.Module.Command,
            ["test", "--project", KnownTestFailuresProductionFixture.Project, "-p:ContinuousIntegrationBuild=true",
             "-p:OutDir=" + Path.GetDirectoryName(fixture.Module.Assembly) + "/", "-p:RunWorkingDirectory=" + RepoRoot.Path,
             "--no-build", "--no-restore", "--no-launch-profile", "--",
             "--filter-method", "*.KnownFailurePipelineCases.Pass",
             "--filter-not-trait", "quarantined=true", "--filter-not-trait", "outerloop=true",
             "--report-xunit-xml", "--report-xunit-xml-filename", "baseline.xml", "--results-directory", baselineDirectory],
            RepoRoot.Path, new Dictionary<string, string>
            {
                ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
                ["NUGET_PACKAGES"] = packages,
                ["MSBUILDTERMINALLOGGER"] = "false"
            });
        Assert.Equal(0, baseline.ExitCode);
        var baselineXml = XDocument.Load(Path.Combine(baselineDirectory, "baseline.xml"));
        Assert.Contains("Working directory: " + module.WorkingDirectory,
            baselineXml.Descendants("output").Single().Value, StringComparison.Ordinal);
        var directXml = XDocument.Load(Path.Combine(directory, "module-0", "lifecycle.xml"));
        Assert.Contains("Working directory: " + module.WorkingDirectory,
            directXml.Descendants("test").Single(t => ((string?)t.Attribute("name"))?.EndsWith(".Pass", StringComparison.Ordinal) == true)
                .Element("output")!.Value, StringComparison.Ordinal);
    }

    [Fact]
    public void CiProjectAdmissionNormalizesOnlyPathsInsideTheCheckoutWithoutTraversal()
    {
        var project = KnownTestFailuresProductionFixture.Project;
        Assert.Equal(project, ProductionRunner.NormalizeProjectPath(RepoRoot.Path, project));
        Assert.Equal(project, ProductionRunner.NormalizeProjectPath(RepoRoot.Path, Path.Combine(RepoRoot.Path, project)));
        Assert.Throws<InvalidDataException>(() => ProductionRunner.NormalizeProjectPath(RepoRoot.Path,
            Path.Combine(RepoRoot.Path + "-sibling", project)));
        Assert.Throws<InvalidDataException>(() => ProductionRunner.NormalizeProjectPath(RepoRoot.Path, "../" + project));
        Assert.Throws<InvalidDataException>(() => ProductionRunner.NormalizeProjectPath(RepoRoot.Path, "tests/../" + project));
        Assert.Throws<InvalidDataException>(() => ProductionRunner.NormalizeProjectPath(RepoRoot.Path,
            Path.Combine(RepoRoot.Path, "tests", "..", project)));
    }

    [Theory]
    [InlineData("")]
    [InlineData("outerloop")]
    [InlineData("specialized")]
    public async Task CiEntryPointRejectsMissingOrUnsupportedLaneBeforeExecution(string lane)
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        using var assets = JsonDocument.Parse(File.ReadAllText(
            Path.Combine(RepoRoot.Path, "artifacts", "obj", "Infrastructure.Tests", "project.assets.json")));
        var directory = Path.Combine(fixture.Files.Path, "unsupported-lane");
        var result = await ProcessRunner.RunAsync(output, fixture.Module.Command,
            [Path.Combine(RepoRoot.Path, "artifacts", "bin", "KnownTestFailures", "Debug", "net10.0", "KnownTestFailures.dll"),
             "ci-run", "--repo", RepoRoot.Path, "--project",
             Path.Combine(RepoRoot.Path, KnownTestFailuresProductionFixture.Project), "--output", directory],
            RepoRoot.Path, new Dictionary<string, string>
            {
                ["GITHUB_REPOSITORY"] = "microsoft/aspire",
                ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
                ["NUGET_PACKAGES"] = assets.RootElement.GetProperty("packageFolders").EnumerateObject().First().Name,
                ["KTF_EXECUTED_SHA"] = "",
                ["KTF_LANE"] = lane,
                ["KTF_EXECUTION_KIND"] = "normal",
                ["KTF_SCOPE_REF"] = "refs/heads/main",
                ["KTF_BASE_ARGS"] = "--ignore-exit-code 8 --timeout 2m",
                ["KTF_EXTRA_ARGS"] = "--filter-method '*.KnownFailurePipelineCases.Pass' --filter-not-trait 'quarantined=true' --filter-not-trait 'outerloop=true'",
                ["KTF_VERSION_ARGS"] = "/p:OutDir=\"" + Path.GetDirectoryName(fixture.Module.Assembly) + "/\""
            });
        Assert.Equal(Evaluator.BlockedExitCode, result.ExitCode);
        Assert.Contains("explicit supported KTF_LANE=regular", result.StandardError, StringComparison.Ordinal);
        Assert.False(File.Exists(Path.Combine(directory, "request.json")));
    }

    [Fact]
    public async Task RealTheoryOutputPassesWithoutFetchingUnavailableRegistry()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("ordinary-theories");
        request = request with
        {
            Modules = [fixture.Module with
            {
                Arguments = ["--filter-class", "*.MtpExitCodeScriptTests", "--filter-not-trait", "category=failing",
                    "--filter-not-trait", "quarantined=true", "--filter-not-trait", "outerloop=true", "--ignore-exit-code", "8",
                    "--timeout", "2m"]
            }]
        };
        using var log = new StringWriter();
        var fetches = 0;
        var decision = await ProductionRunner.RunAsync(request, () =>
        {
            fetches++;
            throw new IOException("Registry unavailable.");
        }, log);
        Assert.True(decision.EffectiveExitCode == 0, log.ToString());
        Assert.Equal(0, fetches);
        Assert.Equal("pass", decision.Status);
        Assert.Null(decision.RegistrySource);
        Assert.Equal(8, Assert.Single(decision.Evidence!.Invocations).ExpectedCases.Length);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task RealMixedOutputRequiresFreshProductionApprovalAndRejectsNeighborParameterSignatureAndUpdates(bool archived)
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        if (archived)
        {
            await fixture.UseArchiveAsync();
        }
        fixture.Files.InitializeGit();
        var remote = Path.Combine(fixture.Files.Path, "registry-source");
        GitCli.Run(fixture.Files.Path, "init", "--initial-branch=main", remote);
        const string branch = "refs/heads/known-test-failures-ci-registry";
        string? revision = null;
        RegistryEvent? add = null;
        var fetches = 0;
        async Task<Decision> Run(string name, string scenario = "candidate", string scope = "refs/heads/main", string lane = "regular")
        {
            var request = fixture.Request(name, scenario) with { Ref = scope, Lane = lane };
            using var log = new StringWriter();
            var decision = await ProductionRunner.RunAsync(request, async () =>
            {
                // Capture has already independently discovered and executed every case.
                // Only then initialize/fetch data through the real branch-management path.
                fetches++;
                if (revision is null)
                {
                    var evidence = Format.Read<Evidence>(File.ReadAllText(Path.Combine(request.OutputDirectory, "evidence.json")));
                    var invocation = Assert.Single(evidence.Invocations);
                    var candidate = invocation.ExpectedCases.Single(c => c.Method.EndsWith(".Candidate", StringComparison.Ordinal));
                    var result = StrictTrx.Read(File.ReadAllText(Path.Combine(request.OutputDirectory, invocation.TrxPath)), true)
                        .Single(r => r.TestId == candidate.TestId);
                    var now = DateTimeOffset.UtcNow;
                    var content = new Allowance(invocation.Project, candidate.Method, candidate.DisplayName, candidate.CaseId,
                        evidence.Lane, evidence.Platform, evidence.Ref, result.Message!, "https://example.com/test-only",
                        "test-owner", now.AddMinutes(-1), now.AddHours(1));
                    add = new(1, "candidate", "add", "test-author", "Test-only admission assertion.", now, 1, content);
                    revision = await GitRegistry.MutateAsync(remote, branch, new string('0', 40), true, add, "github-ci");
                }
                return await GitRegistry.FetchBranchAsync(fixture.Files.Path, remote, branch,
                    ProductionRunner.TrackingRef, (_, _) => { });
            }, log);
            output.WriteLine(log.ToString());
            return decision;
        }
        var unapproved = await Run("unapproved");
        Assert.Equal(2, unapproved.OriginalExitCode);
        Assert.Equal(2, unapproved.EffectiveExitCode);
        Assert.Equal(1, fetches);
        Assert.Equal(6, Assert.Single(unapproved.Evidence!.Invocations).ExpectedCases.Length);
        var initialRegistry = Format.Read<Registry>(unapproved.RegistryJson!);
        Assert.Equal("github-ci", initialRegistry.Environment);
        Assert.Equal("pipeline candidate fixed failure", Assert.Single(initialRegistry.History).Content!.Message);

        var approve = new RegistryEvent(2, "candidate", "approve", "test-reviewer", "Reviewed exact failure.",
            DateTimeOffset.UtcNow, 1);
        revision = await GitRegistry.MutateAsync(remote, branch, revision!, false, approve);
        var allowed = await Run("approved");
        Assert.Equal("allowed", allowed.Status);
        Assert.Equal(2, allowed.OriginalExitCode);
        Assert.Equal(0, allowed.EffectiveExitCode);
        Assert.Single(allowed.Matches);
        Assert.Equal(revision, allowed.RegistrySource!.Commit);
        var results = StrictTrx.Read(Assert.Single(allowed.Artifacts).RawTrx, true);
        Assert.Equal(4, results.Count(r => r.Outcome == "Passed"));
        Assert.Single(results, r => r.Outcome == "Failed");
        Assert.Single(results, r => r.Outcome == "NotExecuted");
        var wrongLane = await Run("wrong-lane", lane: "outerloop");
        Assert.Equal(2, wrongLane.OriginalExitCode);
        Assert.Equal(2, wrongLane.EffectiveExitCode);
        Assert.Equal("blocked", wrongLane.Status);
        Assert.Empty(wrongLane.Matches);
        Assert.Equal("outerloop", wrongLane.Evidence!.Lane);
        Assert.Equal(allowed.RegistrySource!.Commit, wrongLane.RegistrySource!.Commit);
        var fetchesBeforeCrash = fetches;
        var crash = await Run("abnormal-exit", "crash");
        // The native MTP controller reports its actual incomplete-session exit 7,
        // not the child test's requested termination status.
        Assert.Equal(7, crash.OriginalExitCode);
        Assert.Equal(7, crash.EffectiveExitCode);
        Assert.Equal("blocked", crash.Status);
        Assert.Equal(fetchesBeforeCrash, fetches);

        foreach (var scenario in new[] { "mixed", "parameter", "changed" })
        {
            var blocked = await Run(scenario, scenario);
            Assert.Equal(2, blocked.OriginalExitCode);
            Assert.Equal(2, blocked.EffectiveExitCode);
            Assert.Equal("blocked", blocked.Status);
        }
        Assert.Equal("blocked", (await Run("wrong-scope", scope: "refs/heads/release")).Status);
        var update = new RegistryEvent(3, "candidate", "update", "test-author", "Changed owner requires reapproval.",
            DateTimeOffset.UtcNow, 2, add!.Content! with { Owner = "another-owner" });
        revision = await GitRegistry.MutateAsync(remote, branch, revision!, false, update);
        Assert.Equal(2, (await Run("updated")).EffectiveExitCode);
        revision = await GitRegistry.MutateAsync(remote, branch, revision, false,
            approve with { Sequence = 4, ContentRevision = 2, At = DateTimeOffset.UtcNow });
        var reapproved = await Run("reapproved");
        Assert.Equal(0, reapproved.EffectiveExitCode);
        Assert.Equal(2, Assert.Single(reapproved.Matches).ContentRevision);
        var boundary = Evaluator.Evaluate("enforce", 2, update.Content!.ExpiresAt, false,
            () => new(Format.Read<Registry>(reapproved.RegistryJson!), reapproved.RegistrySource!, reapproved.RegistryJson!),
            () => reapproved.Evidence!, path => File.ReadAllText(Path.Combine(fixture.Files.Path, "reapproved", path)));
        Assert.Equal(2, boundary.EffectiveExitCode);
        Assert.Equal("blocked", boundary.Status);

        // A cached approved tracking ref cannot substitute for a failed new fetch.
        GitCli.Run(remote, "update-ref", "-d", branch, revision);
        var failedFetch = await Run("failed-fetch");
        Assert.Equal(2, failedFetch.EffectiveExitCode);
        Assert.Equal("blocked", failedFetch.Status);
        Assert.Contains("git fetch failed", Assert.Single(failedFetch.Diagnostics), StringComparison.Ordinal);

        GitCli.Run(remote, "update-ref", branch, revision);
        revision = await GitRegistry.MutateAsync(remote, branch, revision, false,
            new(5, "candidate", "revoke", "test-reviewer", "Withdraw approval.", DateTimeOffset.UtcNow, 2));
        Assert.Equal(2, (await Run("revoked")).EffectiveExitCode);

        // Removal is a separate terminal alternative, starting from the approved history.
        GitCli.Run(remote, "update-ref", branch, reapproved.RegistrySource!.Commit!, revision);
        revision = await GitRegistry.MutateAsync(remote, branch, reapproved.RegistrySource.Commit!, false,
            new(5, "candidate", "remove", "test-reviewer", "Remove obsolete allowance.", DateTimeOffset.UtcNow, 2));
        Assert.Equal(2, (await Run("removed")).EffectiveExitCode);

        var experimental = Evaluator.Evaluate("enforce", 2, DateTimeOffset.UtcNow, false,
            () => new(Format.Read<Registry>(reapproved.RegistryJson!) with { Environment = "experimental" },
                reapproved.RegistrySource!, reapproved.RegistryJson!),
            () => reapproved.Evidence!, path => File.ReadAllText(Path.Combine(fixture.Files.Path, "reapproved", path)));
        Assert.Equal("blocked", experimental.Status);
        Assert.Equal(2, experimental.EffectiveExitCode);
    }

    [Fact]
    public async Task EarlyInventoryProblemDoesNotStopAnySelectedModuleAndCannotTurnRawSuccessGreen()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("all-modules");
        request = request with
        {
            Modules =
            [
                fixture.Module with { SupportedAdapter = false },
                fixture.Module with { Id = "module-1" }
            ]
        };
        using var log = new StringWriter();
        var decision = await ProductionRunner.RunAsync(request,
            () => throw new InvalidOperationException("Must not fetch for invalid evidence."), log);
        Assert.Equal(0, decision.OriginalExitCode);
        Assert.Equal(3, decision.EffectiveExitCode);
        Assert.Equal("blocked", decision.Status);
        Assert.Equal(2, decision.Evidence!.Invocations.Length);
        Assert.All(decision.Evidence.Invocations, invocation =>
        {
            Assert.True(invocation.CompletedNormally);
            Assert.Equal(6, invocation.ExpectedCases.Length);
            Assert.True(File.Exists(Path.Combine(request.OutputDirectory, invocation.Id, "execution.process.json")));
        });
    }

    [Fact]
    public async Task EmptySelectionRequiresExplicitIgnoreEightAndCompletedEmptyEvidence()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("empty");
        request = request with
        {
            Modules = [fixture.Module with
            {
                Arguments = ["--filter-method", "*.NoSuchPipelineCase", "--ignore-exit-code", "8", "--timeout", "2m"]
            }]
        };
        using var log = new StringWriter();
        var decision = await ProductionRunner.RunAsync(request,
            () => throw new IOException("Passing empty selection must not fetch."), log);
        Assert.True(decision.EffectiveExitCode == 0, log.ToString());
        var invocation = Assert.Single(decision.Evidence!.Invocations);
        Assert.Empty(invocation.ExpectedCases);
        Assert.True(invocation.AllowEmpty);
        var trx = Assert.Single(decision.Artifacts).RawTrx;
        Assert.Throws<InvalidDataException>(() => StrictTrx.Read(trx, true));
        var unauthorized = Evaluator.Evaluate("enforce", 0, DateTimeOffset.UtcNow, false,
            () => throw new IOException("Must not fetch."), () => decision.Evidence with
            {
                Invocations = [invocation with { AllowEmpty = false }]
            }, _ => trx);
        Assert.Equal(3, unauthorized.EffectiveExitCode);

        var lifecyclePath = Path.Combine(request.OutputDirectory, "module-0", "lifecycle.xml");
        var lifecycle = XDocument.Load(lifecyclePath);
        lifecycle.Root!.Element("assembly")!.Attribute("finish-rtf")!.Remove();
        Assert.Throws<InvalidDataException>(() => NativeEvidence.ValidateLifecycle(lifecycle.ToString(), []));
        var nonempty = NativeEvidence.Discover(File.ReadAllText(Path.Combine(request.OutputDirectory, "module-0", "discovery-1.stdout.txt")));
        Assert.Empty(nonempty);
        Assert.Throws<InvalidDataException>(() => NativeEvidence.Bind([new("case", "N.C.M", "N.C.M")], []));
    }

    [Fact]
    public async Task AllSkippedIsACompletedNonemptyRunAndMissingReportsCannotPass()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("all-skipped");
        request = request with
        {
            Modules = [fixture.Module with
            {
                Arguments = ["--filter-method", "*.KnownFailurePipelineCases.Skip", "--ignore-exit-code", "8", "--timeout", "2m"]
            }]
        };
        using var log = new StringWriter();
        var decision = await ProductionRunner.RunAsync(request,
            () => throw new IOException("Must not fetch for a skipped run."), log);
        Assert.True(decision.EffectiveExitCode == 0, log.ToString());
        Assert.Single(Assert.Single(decision.Evidence!.Invocations).ExpectedCases);
        Assert.Equal("NotExecuted", Assert.Single(StrictTrx.Read(Assert.Single(decision.Artifacts).RawTrx, true)).Outcome);

        request = fixture.Request("missing-reports");
        request = request with { Modules = [fixture.Module with { PrefixArguments = [.. fixture.Module.PrefixArguments, "--help"] }] };
        var blocked = await ProductionRunner.RunAsync(request,
            () => throw new IOException("Must not fetch for malformed/incomplete evidence."), log);
        Assert.Equal(0, blocked.OriginalExitCode);
        Assert.Equal(3, blocked.EffectiveExitCode);
        Assert.Equal("blocked", blocked.Status);
        Assert.False(Assert.Single(blocked.Evidence!.Invocations).CompletedNormally);
    }

    [Fact]
    public async Task NativeOutputDoesNotPermitIncompleteContradictoryOrExtendedLifecycle()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("native-shapes");
        using var log = new StringWriter();
        var capture = await ProductionRunner.CaptureAsync(request, log);
        Assert.Empty(capture.Diagnostics);
        var invocation = Assert.Single(capture.Evidence.Invocations);
        var results = StrictTrx.Read(File.ReadAllText(Path.Combine(request.OutputDirectory, invocation.TrxPath)), true);
        foreach (var mutation in new[] { "finish", "errors", "not-run", "skip-reason", "nested-output", "result", "reason", "time", "namespace" })
        {
            var lifecycle = XDocument.Load(Path.Combine(request.OutputDirectory, "module-0", "lifecycle.xml"));
            var assembly = lifecycle.Root!.Element("assembly")!;
            var skip = assembly.Descendants("test").Single(t => (string?)t.Attribute("result") == "Skip");
            switch (mutation)
            {
                case "finish": assembly.Attribute("finish-rtf")!.Remove(); break;
                case "errors": assembly.SetAttributeValue("errors", 1); break;
                case "not-run": assembly.SetAttributeValue("not-run", 1); break;
                case "skip-reason": skip.Element("reason")!.Remove(); break;
                case "nested-output": skip.Element("output")!.Add(new XElement("diagnostic", "not text")); break;
                case "result": skip.SetAttributeValue("result", "NotRun"); break;
                case "reason": skip.Element("reason")!.Value = "contradictory reason"; break;
                case "time": skip.SetAttributeValue("finish-rtf", "not a timestamp"); break;
                case "namespace": skip.Add(new XElement(XName.Get("output", "urn:unexpected"), "foreign output")); break;
            }
            Assert.Throws<InvalidDataException>(() => NativeEvidence.ValidateLifecycle(lifecycle.ToString(), results));
        }
        var trx = XDocument.Parse(File.ReadAllText(Path.Combine(request.OutputDirectory, invocation.TrxPath)));
        XNamespace ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010";
        trx.Descendants(ns + "TextMessages").First().Add(new XElement(ns + "ErrorInfo", "not ordinary output"));
        Assert.Throws<InvalidDataException>(() => StrictTrx.Read(trx.ToString(), true));
    }

    [Fact]
    public async Task RealExecutionUsesThirtyMinuteSessionPlusShutdownGrace()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("long-session-budget");
        request = request with
        {
            Modules = [fixture.Module with
            {
                Arguments = ["--filter-method", "*.KnownFailurePipelineCases.Pass", "--timeout", "30m"]
            }]
        };
        using var log = new StringWriter();
        var decision = await ProductionRunner.RunAsync(request,
            () => throw new IOException("Passing execution must not fetch."), log);
        Assert.True(decision.EffectiveExitCode == 0, log.ToString());
        using var receipt = JsonDocument.Parse(File.ReadAllText(
            Path.Combine(request.OutputDirectory, "module-0", "execution.process.json")));
        Assert.Equal(TimeSpan.FromMinutes(35).TotalMilliseconds,
            receipt.RootElement.GetProperty("deadlineMilliseconds").GetDouble());
        var arguments = receipt.RootElement.GetProperty("arguments").EnumerateArray().Select(a => a.GetString()).ToArray();
        Assert.Equal("30m", arguments[Array.IndexOf(arguments, "--timeout") + 1]);
        Assert.False(receipt.RootElement.GetProperty("timedOut").GetBoolean());
    }

    [Theory]
    [InlineData("--timeout 0s")]
    [InlineData("--timeout 30m --timeout 1m")]
    [InlineData("--timeout invalid")]
    public async Task InvalidTimeoutConfigurationCannotLaunchAnUnboundedProcess(string text)
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var request = fixture.Request("invalid-session-budget");
        request = request with { Modules = [fixture.Module with { Arguments = NativeEvidence.Arguments(text) }] };
        using var log = new StringWriter();
        await Assert.ThrowsAsync<InvalidDataException>(() => ProductionRunner.CaptureAsync(request, log));
        var recorded = Format.Read<ProductionRequest>(File.ReadAllText(Path.Combine(request.OutputDirectory, "request.json")));
        Assert.Equal(request.Modules[0].Arguments, recorded.Modules[0].Arguments);
        Assert.False(File.Exists(Path.Combine(request.OutputDirectory, "module-0", "execution.process.json")));
    }

    [Theory]
    [InlineData("30m", 2_100_000)]
    [InlineData("20m", 1_500_000)]
    [InlineData("90s", 390_000)]
    [InlineData("1.5h", 5_700_000)]
    [InlineData("500ms", 300_500)]
    [InlineData("2 minutes", 420_000)]
    [InlineData("1H", 3_900_000)]
    [InlineData("1d", 86_700_000)]
    public void ExecutionDeadlinePreservesTheEffectiveMtpTimeoutPlusShutdownGrace(string duration, double expectedMilliseconds)
    {
        string[] arguments = ["--filter-method", "*.Cases", "--timeout", duration, "--ignore-exit-code", "8"];
        var original = arguments.ToArray();
        Assert.Equal(TimeSpan.FromMilliseconds(expectedMilliseconds), ProductionRunner.ExecutionDeadline(arguments));
        Assert.Equal(original, arguments);
    }

    [Theory]
    [InlineData("")]
    [InlineData("--timeout")]
    [InlineData("--timeout 0s")]
    [InlineData("--timeout -1m")]
    [InlineData("--timeout infinite")]
    [InlineData("--timeout 30")]
    [InlineData("--timeout 30x")]
    [InlineData("--timeout 30m --timeout 1m")]
    [InlineData("--timeout 1m --timeout 30m")]
    [InlineData("--timeout=30m")]
    [InlineData("--timeout 999999999999999999999999999999999h")]
    public void InvalidOrAmbiguousTimeoutsAreRejectedWithoutRewritingArguments(string text)
    {
        var arguments = NativeEvidence.Arguments(text);
        var original = arguments.ToArray();
        Assert.Throws<InvalidDataException>(() => ProductionRunner.ExecutionDeadline(arguments));
        Assert.Equal(original, arguments);
    }

    [Theory]
    [InlineData("--filter-class \"*.Cases\" --filter-not-trait 'category=failing'", new[] { "--filter-class", "*.Cases", "--filter-not-trait", "category=failing" })]
    [InlineData("a\\ b \"quoted\\\"value\" ''", new[] { "a b", "quoted\"value", "" })]
    [InlineData("'$(touch nope)' '*.Tests' \"$HOME\"", new[] { "$(touch nope)", "*.Tests", "$HOME" })]
    public void ArgumentsAreTokenizedWithoutShellExpansion(string text, string[] expected) =>
        Assert.Equal(expected, NativeEvidence.Arguments(text));

    [Theory]
    [InlineData("'unterminated")]
    [InlineData("trailing\\")]
    public void MalformedArgumentStringsAreRejected(string text) =>
        Assert.Throws<InvalidDataException>(() => NativeEvidence.Arguments(text));

    [Fact]
    public void AmbiguousOrMalformedDiscoveryCannotBecomeExpectedInventory()
    {
        Assert.ThrowsAny<JsonException>(() => NativeEvidence.Discover("not JSON"));
        Assert.Throws<InvalidDataException>(() => NativeEvidence.Discover("""{"schemaVersion":2,"schemaVersion":1,"tests":[]}"""));
        const string json = """
            {"schemaVersion":1,"tests":[
              {"uid":"same","displayName":"N.C.M","type":{"namespace":"N","typeName":"C","methodName":"M"}},
              {"uid":"same","displayName":"N.C.Other","type":{"namespace":"N","typeName":"C","methodName":"Other"}}]}
            """;
        Assert.Throws<InvalidDataException>(() => NativeEvidence.Discover(json));
        Assert.Throws<InvalidDataException>(() => NativeEvidence.Discover(
            json.Replace("\"uid\":\"same\",\"displayName\":\"N.C.Other\"", "\"uid\":\"other\",\"displayName\":\"N.C.M\"")
                .Replace("\"methodName\":\"Other\"", "\"methodName\":\"M\"")));
    }
}

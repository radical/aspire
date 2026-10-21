// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.IO.Compression;
using System.Text.Json;
using System.Xml.Linq;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

public sealed class KnownTestFailuresArchiveTests(ITestOutputHelper output)
{
    [Fact]
    public async Task BuildIdentityPreservesAuthoredConflictsAndCleansOnlyGeneratedSidecars()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        var host = Path.GetDirectoryName(fixture.Module.Assembly)!;
        var sidecar = Path.ChangeExtension(fixture.Module.Assembly, ".uniqueid");
        var owner = Path.Combine(host, "Infrastructure.Tests.aspire-known-failure-identity");
        Task<ProcessResult> Generate(bool active) => ProcessRunner.RunAsync(output, fixture.Module.Command,
            ["msbuild", KnownTestFailuresProductionFixture.Project, "-nologo", "-t:KnownTestFailuresIdentity",
             "-p:OutDir=" + host + "/", "-p:KnownTestFailures=" + (active ? "true" : "false")],
            RepoRoot.Path, EnvironmentFor());

        var disabled = await Generate(false);
        Assert.True(disabled.ExitCode == 0, disabled.Output);
        Assert.False(File.Exists(sidecar));
        Assert.False(File.Exists(owner));
        File.WriteAllText(sidecar, "authored-identity");
        var conflict = await Generate(true);
        Assert.NotEqual(0, conflict.ExitCode);
        Assert.Contains("conflicts with an authored sidecar", conflict.Output, StringComparison.Ordinal);
        Assert.Equal("authored-identity", File.ReadAllText(sidecar));
        disabled = await Generate(false);
        Assert.True(disabled.ExitCode == 0, disabled.Output);
        Assert.Equal("authored-identity", File.ReadAllText(sidecar));

        File.Delete(sidecar);
        var generated = await Generate(true);
        Assert.True(generated.ExitCode == 0, generated.Output);
        Assert.Equal([NativeEvidence.Identity(KnownTestFailuresProductionFixture.Project, "net11.0")],
            File.ReadAllLines(sidecar));
        Assert.Equal(File.ReadAllText(sidecar), File.ReadAllText(owner));
        File.WriteAllText(sidecar, "modified-generated-identity");
        foreach (var active in new[] { true, false })
        {
            var modified = await Generate(active);
            Assert.NotEqual(0, modified.ExitCode);
            Assert.Contains("identity was modified", modified.Output, StringComparison.Ordinal);
            Assert.Equal("modified-generated-identity", File.ReadAllText(sidecar));
            Assert.True(File.Exists(owner));
        }
    }

    [Theory]
    [InlineData("/p:OutDir=\"C:\\work tree\\\" --filter-class '*.Cases' '$HOME' ''")]
    [InlineData("--name 'it''s quoted' --value \"say `\"hello`\"\" C:\\unquoted\\path")]
    public async Task WindowsLiteralArgumentsMatchPowerShellWithoutLosingBackslashes(string text)
    {
        var actual = await ProcessRunner.RunAsync(output, "pwsh",
            ["-NoProfile", "-Command", "& { ConvertTo-Json -InputObject @($args) -Compress } " + text], RepoRoot.Path);
        Assert.Equal(0, actual.ExitCode);
        Assert.Equal(JsonSerializer.Deserialize<string[]>(actual.StandardOutput), NativeEvidence.Arguments(text, true));
    }

    [Fact]
    public async Task ActualArchivePreparationAndCiEntryPointsExecuteTheSelectedBuiltModule()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        await fixture.UseArchiveAsync();
        var root = Path.Combine(fixture.Files.Path, "cli-archive");
        var manifest = Path.Combine(fixture.Files.Path, "prepared.json");
        // The archive receipt retains the exact evaluated ZIP and assembly. Resolve
        // against the actual build output, not the already relocated runtime module.
        var original = Path.Combine(fixture.Files.Path, "host");
        var properties = $"/p:OutDir=\"{original}/\" /p:TestArchiveTestsDir=\"{fixture.Files.Path}/zips/\" /p:DeployOutsideOfRepoSupportFiles=false";
        var environment = EnvironmentFor();
        environment["KTF_VERSION_ARGS"] = properties;
        var prepare = await Cli(fixture, environment, "prepare-archives", "--repo", RepoRoot.Path,
            "--project", Path.Combine(RepoRoot.Path, KnownTestFailuresProductionFixture.Project),
            "--output", root, "--manifest", manifest);
        Assert.True(prepare.ExitCode == 0, prepare.Output);
        var prepared = Format.Read<PreparedArchives>(File.ReadAllText(manifest));
        var module = Assert.Single(prepared.Modules);
        Assert.Equal(root, module.WorkingDirectory);
        Assert.Equal([module.Assembly], module.PrefixArguments);
        Assert.Empty(module.Environment);
        Assert.Equal(1, module.Arguments.Count(a => a == "category=failing"));
        environment["KTF_EXECUTION_KIND"] = "archive";
        environment["KTF_ARCHIVE_MANIFEST"] = manifest;
        var directory = Path.Combine(fixture.Files.Path, "gate");
        var run = await Cli(fixture, environment, "ci-run", "--repo", RepoRoot.Path,
            "--project", KnownTestFailuresProductionFixture.Project, "--output", directory);
        Assert.True(run.ExitCode == 0, run.Output);
        var decision = Format.Read<Decision>(File.ReadAllText(Path.Combine(directory, "decision.json")));
        Assert.Equal(0, decision.OriginalExitCode);
        Assert.Equal("pass", decision.Status);
        Assert.Null(decision.RegistrySource);
        Assert.Equal(6, Assert.Single(decision.Evidence!.Invocations).ExpectedCases.Length);
        var wrongHead = prepared with { ExecutedSha = new string('0', 40) };
        File.WriteAllText(manifest, Format.Write(wrongHead));
        var blocked = await Cli(fixture, environment, "ci-run", "--repo", RepoRoot.Path,
            "--project", KnownTestFailuresProductionFixture.Project, "--output", Path.Combine(fixture.Files.Path, "wrong-head"));
        Assert.Equal(Evaluator.BlockedExitCode, blocked.ExitCode);
        Assert.Contains("Prepared archives differ", blocked.StandardError, StringComparison.Ordinal);
    }

    [Fact]
    public async Task MalformedUnselectedAndTraversingArchiveInputsFailClosed()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        await fixture.UseArchiveAsync();
        foreach (var mutation in new[] { "missing", "framework", "assembly", "traversal" })
        {
            var path = Path.Combine(fixture.Files.Path, mutation + ".zip");
            File.Copy(fixture.Module.ArchivePath!, path);
            using (var zip = ZipFile.Open(path, ZipArchiveMode.Update))
            {
                var entry = zip.GetEntry("aspire-known-test-failures.xml")!;
                if (mutation == "missing")
                {
                    entry.Delete();
                }
                else if (mutation == "traversal")
                {
                    using var writer = new StreamWriter(zip.CreateEntry("../unexpected").Open());
                    writer.Write("must not extract");
                }
                else
                {
                    XDocument document;
                    using (var stream = entry.Open())
                    {
                        document = XDocument.Load(stream);
                    }
                    document.Root!.SetAttributeValue(mutation, mutation == "framework" ? "net999.0" : "unselected.dll");
                    entry.Delete();
                    using var streamOut = zip.CreateEntry("aspire-known-test-failures.xml").Open();
                    document.Save(streamOut);
                }
            }
            Assert.Throws<InvalidDataException>(() => ArchivedExecution.Extract(
                [fixture.Module with { ArchivePath = path }], Path.Combine(fixture.Files.Path, mutation)));
        }
    }

    [Fact]
    public async Task ActualMultiFrameworkArchivesKeepTheoryInventoryAndSelectionIndependent()
    {
        using var fixture = new KnownTestFailuresProductionFixture(output);
        const string project = "tests/ConfigurationSchemaGenerator.Tests/ConfigurationSchemaGenerator.Tests.csproj";
        var environment = EnvironmentFor();
        var zips = Path.Combine(fixture.Files.Path, "multi-zips");
        // Building the real multi-target project is necessary: copying one DLL and
        // relabeling it cannot prove inner-TFM identity or archive output selection.
        var build = await ProcessRunner.RunAsync(output, fixture.Module.Command,
            ["build", project, "--nologo", "-v:q", "-p:KnownTestFailures=true",
             "-p:PrepareForHelix=true", "-p:ArchiveTests=true", "-p:RequiresNugets=true",
             "-p:IsGitHubActionsRunner=true", "-p:DeployOutsideOfRepoSupportFiles=false",
             "-p:InstallBrowsersForPlaywright=false", "-p:TestArchiveTestsDir=" + zips,
             "-p:ArtifactsBinDir=" + Path.Combine(fixture.Files.Path, "build", "bin") + "/",
             "-p:ArtifactsObjDir=" + Path.Combine(fixture.Files.Path, "build", "obj") + "/"],
            RepoRoot.Path, environment);
        Assert.True(build.ExitCode == 0, build.Output);
        environment["KTF_VERSION_ARGS"] = $"/p:TestArchiveTestsDir=\"{zips}\" /p:ArtifactsBinDir=\"{fixture.Files.Path}/build/bin/\" /p:ArtifactsObjDir=\"{fixture.Files.Path}/build/obj/\" /p:DeployOutsideOfRepoSupportFiles=false /p:TestClass=*.MustNotAffectArchive";
        environment["KTF_EXTRA_ARGS"] = "--filter-method '*.ShouldRemoveInsignificantWhitespace' --filter-not-trait 'quarantined=true' --filter-not-trait 'outerloop=true'";
        var manifest = Path.Combine(fixture.Files.Path, "multi.json");
        var prepare = await Cli(fixture, environment, "prepare-archives", "--repo", RepoRoot.Path,
            "--project", project, "--output", Path.Combine(fixture.Files.Path, "multi"), "--manifest", manifest);
        Assert.True(prepare.ExitCode == 0, prepare.Output);
        var modules = Format.Read<PreparedArchives>(File.ReadAllText(manifest)).Modules;
        Assert.Equal(["net10.0", "net11.0"], modules.Select(m => m.Framework));
        Assert.Equal(2, modules.Select(m => m.ArchivePath).Distinct().Count());
        environment["KTF_EXECUTION_KIND"] = "archive";
        environment["KTF_ARCHIVE_MANIFEST"] = manifest;
        var directory = Path.Combine(fixture.Files.Path, "multi-gate");
        var run = await Cli(fixture, environment, "ci-run", "--repo", RepoRoot.Path,
            "--project", project, "--output", directory);
        Assert.True(run.ExitCode == 0, run.Output);
        var decision = Format.Read<Decision>(File.ReadAllText(Path.Combine(directory, "decision.json")));
        var invocations = decision.Evidence!.Invocations;
        Assert.Equal(2, invocations.Length);
        Assert.All(invocations, invocation =>
        {
            Assert.True(invocation.CompletedNormally);
            Assert.Equal(23, invocation.ExpectedCases.Length);
            Assert.All(invocation.ExpectedCases, c => Assert.EndsWith(".ShouldRemoveInsignificantWhitespace", c.Method, StringComparison.Ordinal));
        });
        Assert.Empty(invocations[0].ExpectedCases.Select(c => c.CaseId).Intersect(invocations[1].ExpectedCases.Select(c => c.CaseId)));
        Assert.Null(decision.RegistrySource);
    }

    private static Dictionary<string, string> EnvironmentFor()
    {
        using var assets = JsonDocument.Parse(File.ReadAllText(
            Path.Combine(RepoRoot.Path, "artifacts", "obj", "Infrastructure.Tests", "project.assets.json")));
        return new()
        {
            ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
            ["NUGET_PACKAGES"] = assets.RootElement.GetProperty("packageFolders").EnumerateObject().First().Name,
            ["GITHUB_REPOSITORY"] = "microsoft/aspire",
            ["KTF_LANE"] = "regular", ["KTF_EXECUTION_KIND"] = "normal",
            ["KTF_EXECUTED_SHA"] = "", ["KTF_SCOPE_REF"] = "refs/heads/main",
            ["KTF_BASE_ARGS"] = "--ignore-exit-code 8 --timeout 2m",
            ["KTF_EXTRA_ARGS"] = "--filter-class '*.KnownFailurePipelineCases' --filter-not-trait 'quarantined=true' --filter-not-trait 'outerloop=true'",
            ["KTF_REGISTRY_REF"] = "unavailable-registry-unused-for-passing-tests",
            ["MSBUILDTERMINALLOGGER"] = "false"
        };
    }

    private Task<ProcessResult> Cli(KnownTestFailuresProductionFixture fixture, Dictionary<string, string> environment, params string[] args) =>
        ProcessRunner.RunAsync(output, fixture.Module.Command,
            [Path.Combine(RepoRoot.Path, "artifacts", "bin", "KnownTestFailures", "Debug", "net10.0", "KnownTestFailures.dll"), .. args],
            RepoRoot.Path, environment);
}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using System.Text.Json;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

internal sealed class KnownTestFailuresProductionFixture : IDisposable
{
    public const string Project = "tests/Infrastructure.Tests/Infrastructure.Tests.csproj";
    public KnownTestFailuresFixture Files { get; }
    public ProductionModule Module { get; private set; }
    private readonly ITestOutputHelper _output;

    public KnownTestFailuresProductionFixture(ITestOutputHelper output)
    {
        _output = output;
        Files = new(output);
        var host = Directory.CreateDirectory(Path.Combine(Files.Path, "host")).FullName;
        var original = typeof(KnownFailurePipelineCases).Assembly.Location;
        foreach (var file in Directory.GetFiles(Path.GetDirectoryName(original)!))
        {
            File.Copy(file, Path.Combine(host, Path.GetFileName(file)));
        }
        var assembly = Path.Combine(host, Path.GetFileName(original));
        File.WriteAllText(Path.ChangeExtension(assembly, ".uniqueid"), NativeEvidence.Identity(Project, "net11.0"));
        File.WriteAllText(Path.Combine(host, "Infrastructure.Tests.aspire-known-failure-identity"), NativeEvidence.Identity(Project, "net11.0"));
        Module = new("module-0", Project, "net11.0", assembly,
            Path.Combine(RepoRoot.Path, ".dotnet", OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet"),
            [assembly], host,
            ["--filter-class", "*.KnownFailurePipelineCases", "--filter-not-trait", "category=failing",
             "--filter-not-trait", "quarantined=true", "--filter-not-trait", "outerloop=true", "--ignore-exit-code", "8", "--timeout", "2m"],
            new()
            {
                ["DOTNET_ROOT"] = Path.Combine(RepoRoot.Path, ".dotnet"),
                ["GITHUB_REPOSITORY"] = "radical/aspire",
                ["ASPIRE_KNOWN_FAILURE_PIPELINE_PROOF"] = "true",
                ["ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO"] = "pass"
            }, true);
    }

    public async Task UseArchiveAsync()
    {
        using var assets = JsonDocument.Parse(File.ReadAllText(
            Path.Combine(RepoRoot.Path, "artifacts", "obj", "Infrastructure.Tests", "project.assets.json")));
        var packages = assets.RootElement.GetProperty("packageFolders").EnumerateObject().First().Name;
        var properties = $"/p:OutDir=\"{Path.GetDirectoryName(Module.Assembly)}/\" " +
            $"/p:TestArchiveTestsDir=\"{Files.Path}/zips/\" /p:NuGetPackageRoot=\"{packages}\" /p:DeployOutsideOfRepoSupportFiles=false";
        var result = await ProcessRunner.RunAsync(_output, Module.Command,
            ["msbuild", Project, "-nologo", "-t:KnownTestFailuresIdentity;ZipTestArchive",
             "-p:KnownTestFailures=true", "-p:PrepareForHelix=true", "-p:ArchiveTests=true",
             "-p:RequiresNugets=true", "-p:IsGitHubActionsRunner=true",
             .. NativeEvidence.Arguments(properties, OperatingSystem.IsWindows())],
            RepoRoot.Path, new Dictionary<string, string>
            {
                ["NUGET_PACKAGES"] = packages
            });
        Assert.True(result.ExitCode == 0, result.Output);
        var receipts = Directory.CreateDirectory(Path.Combine(Files.Path, "archive-receipts")).FullName;
        // Resolve through the real inner-build target rather than constructing a
        // test-owned archive/module manifest from whichever DLL happens to exist.
        // Keep metadata imports bound to the actual restored package cache.
        var modules = await ProductionRunner.ResolveAsync(RepoRoot.Path, Project, receipts,
            "--ignore-exit-code 8 --timeout 2m",
            "--filter-class '*.KnownFailurePipelineCases' --filter-not-trait 'quarantined=true' --filter-not-trait 'outerloop=true'",
            properties, true);
        Module = Assert.Single(ArchivedExecution.Extract(modules, Path.Combine(Files.Path, "archived-host"))) with
        {
            Environment = Module.Environment
        };
    }

    public ProductionRequest Request(string name, string scenario = "pass") =>
        new([Module with
        {
            Environment = new(Module.Environment) { ["ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO"] = scenario }
        }], Path.Combine(Files.Path, name), "regular", RuntimeInformation.RuntimeIdentifier,
            "refs/heads/main", "test-executed-sha", "test-head-sha", "radical/aspire");

    public void Dispose() => Files.Dispose();
}

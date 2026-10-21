// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

internal sealed class KnownTestFailuresProductionFixture : IDisposable
{
    public const string Project = "tests/Infrastructure.Tests/Infrastructure.Tests.csproj";
    public KnownTestFailuresFixture Files { get; }
    public ProductionModule Module { get; }

    public KnownTestFailuresProductionFixture(ITestOutputHelper output)
    {
        Files = new(output);
        var host = Directory.CreateDirectory(Path.Combine(Files.Path, "host")).FullName;
        var original = typeof(KnownFailurePipelineCases).Assembly.Location;
        foreach (var file in Directory.GetFiles(Path.GetDirectoryName(original)!))
        {
            File.Copy(file, Path.Combine(host, Path.GetFileName(file)));
        }
        var assembly = Path.Combine(host, Path.GetFileName(original));
        File.WriteAllText(Path.ChangeExtension(assembly, ".uniqueid"), NativeEvidence.Identity(Project, "net11.0"));
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

    public ProductionRequest Request(string name, string scenario = "pass") =>
        new([Module with
        {
            Environment = new(Module.Environment) { ["ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO"] = scenario }
        }], Path.Combine(Files.Path, name), "regular", RuntimeInformation.RuntimeIdentifier,
            "refs/heads/main", "test-executed-sha", "test-head-sha", "radical/aspire");

    public void Dispose() => Files.Dispose();
}

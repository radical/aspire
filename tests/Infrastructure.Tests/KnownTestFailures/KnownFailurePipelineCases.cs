// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.IO.Compression;
using System.Runtime.InteropServices;
using Xunit;

namespace Infrastructure.Tests;

public sealed class KnownFailurePipelineCases(ITestOutputHelper output)
{
    [Fact]
    public void Pass()
    {
        output.WriteLine("Ordinary passing test output is valid lifecycle evidence.");
        output.WriteLine($"Working directory: {Environment.CurrentDirectory}");
        if (Environment.GetEnvironmentVariable("ASPIRE_KNOWN_FAILURE_PIPELINE_ARCHIVE") == "true" &&
            Environment.GetEnvironmentVariable("GITHUB_REPOSITORY") == "radical/aspire")
        {
            Assert.Equal("true", Environment.GetEnvironmentVariable("TestsRunningOutsideOfRepo")?.ToLowerInvariant());
            var feed = Environment.GetEnvironmentVariable("BUILT_NUGETS_PATH")!;
            foreach (var package in new[] { "Aspire.StackExchange.Redis", $"Aspire.Hosting.Orchestration.{RuntimeInformation.RuntimeIdentifier}" })
            {
                var path = Assert.Single(Directory.GetFiles(feed, package + ".*.nupkg"),
                    p => !p.EndsWith(".symbols.nupkg", StringComparison.Ordinal));
                using var zip = ZipFile.OpenRead(path);
                Assert.Contains(zip.Entries, e => e.FullName == package + ".nuspec");
                Assert.Contains(zip.Entries, e => e.FullName.EndsWith(".dll", StringComparison.Ordinal) ||
                    e.FullName.EndsWith("/dcp", StringComparison.Ordinal) || e.FullName.EndsWith("/dcp.exe", StringComparison.Ordinal));
                output.WriteLine("Consumed built package: " + Path.GetFileName(path));
            }
        }
    }

    [Fact]
    public void Candidate()
    {
        output.WriteLine("The candidate runs even when its gate is waived.");
        if (Scenario == "crash")
        {
            // Terminate without framework completion, but without a large native
            // core dump. Only the explicit same-fork proof can select this path.
            Environment.Exit(42);
        }
        if (Scenario is "candidate" or "mixed" or "parameter")
        {
            Assert.Fail("pipeline candidate fixed failure");
        }
        if (Scenario == "changed")
        {
            Assert.Fail("pipeline candidate changed failure");
        }
    }

    [Fact]
    public void Neighbor()
    {
        if (Scenario == "mixed")
        {
            Assert.Fail("pipeline unknown neighbor");
        }
    }

    [Theory]
    [InlineData(1)]
    [InlineData(2)]
    public void Parameter(int value)
    {
        output.WriteLine($"Individually discovered parameter={value}.");
        if (Scenario == "parameter" && value == 2)
        {
            Assert.Fail("pipeline unknown parameter");
        }
    }

    [Fact]
    public void Skip()
    {
        output.WriteLine("Output before a legitimate runtime skip.");
        Assert.Skip("Pipeline evidence includes a legitimate skipped test.");
    }

    // Failure injection has no effect in upstream CI or ordinary local runs.
    // The real reusable workflow sets these only for its narrowly validated fork caller.
    private static string Scenario =>
        Environment.GetEnvironmentVariable("GITHUB_REPOSITORY") == "radical/aspire" &&
        Environment.GetEnvironmentVariable("ASPIRE_KNOWN_FAILURE_PIPELINE_PROOF") == "true"
            ? Environment.GetEnvironmentVariable("ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO") ?? "pass"
            : "pass";
}

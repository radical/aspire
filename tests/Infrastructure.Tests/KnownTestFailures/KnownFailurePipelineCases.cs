// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;

namespace Infrastructure.Tests;

public sealed class KnownFailurePipelineCases(ITestOutputHelper output)
{
    [Fact]
    public void Pass()
    {
        output.WriteLine("Ordinary passing test output is valid lifecycle evidence.");
        output.WriteLine($"Working directory: {Environment.CurrentDirectory}");
    }

    [Fact]
    public void Candidate()
    {
        output.WriteLine("The candidate runs even when its gate is waived.");
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

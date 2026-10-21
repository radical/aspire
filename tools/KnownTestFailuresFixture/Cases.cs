// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;

namespace KnownTestFailuresFixture;

// This executable is outside the solution and normal CI matrices. Failure injection
// requires both an explicit opt-in and one of the runner's fixed scenarios.
public sealed class Cases
{
    [Fact]
    public void Pass() => Assert.True(true);

    [Fact]
    public void Candidate()
    {
        if (Environment.GetEnvironmentVariable("KNOWN_FAILURE_PROOF_OPT_IN") == "true")
        {
            var scenario = Environment.GetEnvironmentVariable("KNOWN_FAILURE_PROOF_SCENARIO");
            if (scenario is "candidate" or "mixed" or "changed")
            {
                Assert.Fail(scenario == "changed" ? "proof candidate changed failure" : "proof candidate fixed failure");
            }
        }
    }

    [Fact]
    public void Neighbor()
    {
        if (Environment.GetEnvironmentVariable("KNOWN_FAILURE_PROOF_OPT_IN") == "true" &&
            Environment.GetEnvironmentVariable("KNOWN_FAILURE_PROOF_SCENARIO") == "mixed")
        {
            Assert.Fail("proof neighbor unexpected failure");
        }
    }
}

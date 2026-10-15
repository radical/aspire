// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Aspire.TestUtilities;
using Xunit;

namespace Infrastructure.Tests;

public sealed class MtpExitCodeScriptTests(ITestOutputHelper output)
{
    private static readonly string s_unixNormalizerPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "normalize-mtp-exit-code.sh");
    private static readonly string s_windowsNormalizerPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "normalize-mtp-exit-code.ps1");

    [Theory]
    [InlineData(0, 0)]
    [InlineData(8, 0)]
    [InlineData(2, 2)]
    [InlineData(7, 7)]
    [RequiresTools(["bash"])]
    public async Task UnixDotnetTestExitCodeClassificationPreservesFailures(int testExitCode, int expectedExitCode)
    {
        var result = await RunProcessAsync("bash", [s_unixNormalizerPath, testExitCode.ToString()]);

        Assert.Equal(expectedExitCode, result.ExitCode);
    }

    [Theory]
    [InlineData(0, 0)]
    [InlineData(8, 0)]
    [InlineData(2, 2)]
    [InlineData(7, 7)]
    [RequiresTools(["pwsh"])]
    public async Task WindowsDotnetTestExitCodeClassificationPreservesFailures(int testExitCode, int expectedExitCode)
    {
        var result = await RunProcessAsync(
            "pwsh",
            ["-NoProfile", "-File", s_windowsNormalizerPath, "-ExitCode", testExitCode.ToString()]);

        Assert.Equal(expectedExitCode, result.ExitCode);
    }

    private async Task<CommandResult> RunProcessAsync(string fileName, IReadOnlyList<string> arguments)
    {
        var startInfo = new ProcessStartInfo(fileName)
        {
            WorkingDirectory = RepoRoot.Path
        };
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        var result = await ProcessRunner.RunAsync(output, startInfo, TimeSpan.FromSeconds(30)).ConfigureAwait(false);
        return new CommandResult(result.ExitCode, result.Output);
    }

    private sealed record CommandResult(int ExitCode, string Output);
}

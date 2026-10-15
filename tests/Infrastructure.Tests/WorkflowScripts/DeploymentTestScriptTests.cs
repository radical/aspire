// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Aspire.TestUtilities;
using Xunit;

namespace Infrastructure.Tests;

/// <summary>
/// Guards the command and exit-code behavior of the nightly Deployment E2E test step.
/// </summary>
public sealed class DeploymentTestScriptTests(ITestOutputHelper output) : IDisposable
{
    private static readonly string s_runnerPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "run-deployment-test.sh");

    private readonly TemporaryWorkspace _workspace = TemporaryWorkspace.Create(output);

    [Theory]
    [InlineData(0, false)]   // all tests passed
    [InlineData(8, false)]   // MTP "zero tests ran": the only test was dynamically skipped
    [InlineData(2, true)]    // a test failed
    [InlineData(7, true)]    // the test host crashed
    [RequiresTools(["bash"])]
    public async Task RunsDeploymentTestAndMarksOnlyGenuineFailures(int testExitCode, bool expectFailure)
    {
        var outputFile = Path.Combine(_workspace.Path, "github-output.txt");
        var argumentsFile = Path.Combine(_workspace.Path, "dotnet-arguments.txt");
        var fakeDotnetScript = Path.Combine(_workspace.Path, "dotnet.sh");
        await File.WriteAllTextAsync(
            fakeDotnetScript,
            """
            #!/usr/bin/env bash
            printf '%s\n' "$@" > "$FAKE_DOTNET_ARGUMENTS"
            exit "$FAKE_DOTNET_EXIT_CODE"
            """);

        var resultsDirectory = Path.Combine(_workspace.Path, "test results");
        var result = await RunTestStepAsync(
            testExitCode,
            outputFile,
            argumentsFile,
            fakeDotnetScript,
            resultsDirectory);

        Assert.Equal(0, result.ExitCode);
        Assert.Equal(
            [
                "test",
                "--project",
                "tests/Aspire.Deployment.EndToEnd.Tests/Aspire.Deployment.EndToEnd.Tests.csproj",
                "-c",
                "Release",
                "--results-directory",
                resultsDirectory,
                "--",
                "--report-trx",
                "--report-trx-filename",
                "Deployment.EndToEnd-ExampleTests.trx",
                "--filter-not-trait",
                "quarantined=true",
                "--filter-class",
                "Aspire.ExampleTests",
            ],
            await File.ReadAllLinesAsync(argumentsFile));

        var stepOutputs = File.Exists(outputFile) ? await File.ReadAllTextAsync(outputFile) : string.Empty;
        if (expectFailure)
        {
            Assert.Equal($"test_failed=true{Environment.NewLine}", stepOutputs);
        }
        else
        {
            Assert.Equal(string.Empty, stepOutputs);
        }
    }

    private async Task<CommandResult> RunTestStepAsync(
        int testExitCode,
        string outputFile,
        string argumentsFile,
        string fakeDotnetScript,
        string resultsDirectory)
    {
        var startInfo = new ProcessStartInfo("bash")
        {
            WorkingDirectory = _workspace.Path
        };
        startInfo.ArgumentList.Add(s_runnerPath);
        startInfo.ArgumentList.Add(resultsDirectory);
        startInfo.ArgumentList.Add("Deployment.EndToEnd-ExampleTests");
        startInfo.ArgumentList.Add("--filter-class");
        startInfo.ArgumentList.Add("Aspire.ExampleTests");
        startInfo.Environment["GITHUB_OUTPUT"] = outputFile;
        startInfo.Environment["DOTNET_SCRIPT"] = fakeDotnetScript;
        startInfo.Environment["FAKE_DOTNET_ARGUMENTS"] = argumentsFile;
        startInfo.Environment["FAKE_DOTNET_EXIT_CODE"] = testExitCode.ToString();

        var processResult = await ProcessRunner.RunAsync(output, startInfo, TimeSpan.FromSeconds(30)).ConfigureAwait(false);
        return new CommandResult(processResult.ExitCode, processResult.Output);
    }

    public void Dispose() => _workspace.Dispose();

    private sealed record CommandResult(int ExitCode, string Output);
}

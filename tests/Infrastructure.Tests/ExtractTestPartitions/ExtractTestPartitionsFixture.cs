// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Xunit;

namespace Infrastructure.Tests;

/// <summary>
/// Class fixture that builds the ExtractTestPartitions tool once before all tests run.
/// Individual tests can then use <c>dotnet run --no-build</c> for faster execution.
/// </summary>
public sealed class ExtractTestPartitionsFixture : IAsyncLifetime
{
    public string ToolProjectPath { get; private set; } = string.Empty;

    public async ValueTask InitializeAsync()
    {
        ToolProjectPath = GetToolProjectPath();

        var psi = new ProcessStartInfo
        {
            FileName = "dotnet",
            WorkingDirectory = RepoRoot.Path,
            CreateNoWindow = true
        };
        psi.ArgumentList.Add("build");
        psi.ArgumentList.Add(ToolProjectPath);
        psi.ArgumentList.Add("--restore");

        var result = await ProcessRunner.RunAsync(psi, TimeSpan.FromMinutes(3)).ConfigureAwait(false);
        if (result.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Failed to build ExtractTestPartitions tool. Exit code: {result.ExitCode}\n" +
                $"stdout:\n{result.StandardOutput}\n" +
                $"stderr:\n{result.StandardError}");
        }
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    private static string GetToolProjectPath()
    {
        var projectPath = Path.Combine(RepoRoot.Path, "tools", "ExtractTestPartitions", "ExtractTestPartitions.csproj");

        if (!File.Exists(projectPath))
        {
            throw new InvalidOperationException(
                $"ExtractTestPartitions project not found at {projectPath}.");
        }

        return projectPath;
    }
}

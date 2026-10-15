// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Xunit;

namespace Infrastructure.Tests;

/// <summary>
/// Builds the CreateFailingTestIssue tool once before tests run.
/// </summary>
public sealed class CreateFailingTestIssueFixture : IAsyncLifetime
{
    public string ToolProjectPath { get; private set; } = string.Empty;

    public async ValueTask InitializeAsync()
    {
        ToolProjectPath = Path.Combine(RepoRoot.Path, "tools", "CreateFailingTestIssue", "CreateFailingTestIssue.csproj");

        if (!File.Exists(ToolProjectPath))
        {
            throw new InvalidOperationException($"CreateFailingTestIssue project not found at {ToolProjectPath}.");
        }

        await BuildToolAsync(ToolProjectPath).ConfigureAwait(false);
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;

    private static async Task BuildToolAsync(string toolProjectPath)
    {
        ProcessStartInfo processStartInfo = new()
        {
            FileName = "dotnet",
            CreateNoWindow = true,
            WorkingDirectory = RepoRoot.Path
        };
        processStartInfo.ArgumentList.Add("build");
        processStartInfo.ArgumentList.Add(toolProjectPath);
        processStartInfo.ArgumentList.Add("--restore");

        var result = await ProcessRunner.RunAsync(processStartInfo, TimeSpan.FromMinutes(3)).ConfigureAwait(false);
        if (result.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Failed to build CreateFailingTestIssue tool. Exit code: {result.ExitCode}{Environment.NewLine}" +
                $"stdout:{Environment.NewLine}{result.StandardOutput}{Environment.NewLine}" +
                $"stderr:{Environment.NewLine}{result.StandardError}");
        }
    }
}

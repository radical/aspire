// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Xunit;

namespace Infrastructure.Tests;

/// <summary>
/// Builds the GenerateTestSummary tool once before tests run.
/// </summary>
public sealed class GenerateTestSummaryFixture : IAsyncLifetime
{
    public string ToolProjectPath { get; private set; } = string.Empty;

    public async ValueTask InitializeAsync()
    {
        ToolProjectPath = Path.Combine(RepoRoot.Path, "tools", "GenerateTestSummary", "GenerateTestSummary.csproj");

        if (!File.Exists(ToolProjectPath))
        {
            throw new InvalidOperationException($"GenerateTestSummary project not found at {ToolProjectPath}.");
        }

        ProcessStartInfo processStartInfo = new()
        {
            FileName = "dotnet",
            CreateNoWindow = true,
            WorkingDirectory = RepoRoot.Path
        };
        processStartInfo.ArgumentList.Add("build");
        processStartInfo.ArgumentList.Add(ToolProjectPath);
        processStartInfo.ArgumentList.Add("--restore");

        var result = await ProcessRunner.RunAsync(processStartInfo, TimeSpan.FromMinutes(3)).ConfigureAwait(false);
        if (result.ExitCode != 0)
        {
            throw new InvalidOperationException(
                $"Failed to build GenerateTestSummary tool. Exit code: {result.ExitCode}{Environment.NewLine}" +
                $"stdout:{Environment.NewLine}{result.StandardOutput}{Environment.NewLine}" +
                $"stderr:{Environment.NewLine}{result.StandardError}");
        }
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

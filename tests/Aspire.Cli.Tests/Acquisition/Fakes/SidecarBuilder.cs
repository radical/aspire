// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Aspire.Cli.Tests.Acquisition.Fakes;

/// <summary>
/// Test helper for writing <c>.aspire-install.json</c> sidecar files into a prefix directory.
/// Mirrors the v3 sidecar JSON shape: <c>{ "route": "&lt;string&gt;" [, "updateCommand": "&lt;string&gt;"] }</c>.
/// </summary>
internal sealed class SidecarBuilder
{
    private static readonly JsonSerializerOptions s_writeOptions = new()
    {
        WriteIndented = false,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>The install route string (e.g. "script", "pr", "winget", "brew", "dotnet-tool").</summary>
    public string Route { get; init; } = "script";

    /// <summary>The update command string, or <see langword="null"/> for the script route.</summary>
    public string? UpdateCommand { get; init; }

    /// <summary>
    /// Writes <c>.aspire-install.json</c> into <paramref name="directory"/> and returns the path to the written file.
    /// </summary>
    public string WriteTo(string directory)
    {
        var path = Path.Combine(directory, ".aspire-install.json");
        var record = new SidecarRecord(Route, UpdateCommand);
        File.WriteAllText(path, JsonSerializer.Serialize(record, s_writeOptions));
        return path;
    }

    // ── Factory methods ──────────────────────────────────────────────────────

    /// <summary>Sidecar for a script-route install (no <c>updateCommand</c>).</summary>
    public static SidecarBuilder ForScript() =>
        new() { Route = "script" };

    /// <summary>Sidecar for a PR dogfood install. Update command invokes the PR script.</summary>
    public static SidecarBuilder ForPr(int prNumber) =>
        new() { Route = "pr", UpdateCommand = $"get-aspire-cli-pr.sh -r {prNumber}" };

    /// <summary>Sidecar for a winget-managed install.</summary>
    public static SidecarBuilder ForWinget() =>
        new() { Route = "winget", UpdateCommand = "winget upgrade Microsoft.Aspire" };

    /// <summary>Sidecar for a Homebrew-managed install.</summary>
    public static SidecarBuilder ForBrew() =>
        new() { Route = "brew", UpdateCommand = "brew upgrade aspire" };

    /// <summary>Sidecar for a global <c>dotnet tool install -g</c> install.</summary>
    public static SidecarBuilder ForDotnetTool() =>
        new() { Route = "dotnet-tool", UpdateCommand = "dotnet tool update -g Aspire.Cli" };

    /// <summary>Sidecar for a <c>dotnet tool install --tool-path</c> install at the given directory.</summary>
    public static SidecarBuilder ForDotnetToolPath(string toolPath) =>
        new() { Route = "dotnet-tool", UpdateCommand = $"dotnet tool update --tool-path \"{toolPath}\" Aspire.Cli" };

    // ── Prefix layout helpers ────────────────────────────────────────────────

    /// <summary>
    /// Builds a Mode-A prefix layout under <paramref name="root"/>:
    /// <c>root/.aspire-install.json</c> + <c>root/bin/aspire[.exe]</c>.
    /// </summary>
    /// <returns>The prefix directory path and the full binary path.</returns>
    public static (string prefix, string binaryPath) BuildModeA(string root, SidecarBuilder sidecar)
    {
        Directory.CreateDirectory(root);
        sidecar.WriteTo(root);
        var binDir = Directory.CreateDirectory(Path.Combine(root, "bin")).FullName;
        var binaryPath = Path.Combine(binDir, GetBinaryName());
        File.WriteAllText(binaryPath, string.Empty);
        return (root, binaryPath);
    }

    /// <summary>
    /// Builds a Mode-B prefix layout under <paramref name="root"/>:
    /// <c>root/.aspire-install.json</c> + <c>root/aspire[.exe]</c>.
    /// </summary>
    /// <returns>The prefix directory path and the full binary path.</returns>
    public static (string prefix, string binaryPath) BuildModeB(string root, SidecarBuilder sidecar)
    {
        Directory.CreateDirectory(root);
        sidecar.WriteTo(root);
        var binaryPath = Path.Combine(root, GetBinaryName());
        File.WriteAllText(binaryPath, string.Empty);
        return (root, binaryPath);
    }

    private static string GetBinaryName() =>
        OperatingSystem.IsWindows() ? "aspire.exe" : "aspire";

    // ── Private JSON shape ───────────────────────────────────────────────────

    private sealed record SidecarRecord(
        [property: JsonPropertyName("route")] string Route,
        [property: JsonPropertyName("updateCommand")] string? UpdateCommand);
}

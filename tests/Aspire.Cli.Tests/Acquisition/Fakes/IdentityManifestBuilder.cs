// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using System.Text.Json.Serialization;

namespace Aspire.Cli.Tests.Acquisition.Fakes;

/// <summary>
/// Test helper for writing v2 <c>identity.json</c> manifest files into an install tree.
/// Used to exercise the back-compat path in <c>InstallRoute</c> resolution.
/// Layout: <c>root/installs/&lt;installId&gt;/identity.json</c> + <c>root/installs/&lt;installId&gt;/bin/aspire[.exe]</c>.
/// </summary>
/// <remarks>
/// This builder targets the legacy v2 IdentityManifest format and will be deleted in Phase C
/// once the v2 code-path is removed from production.
/// </remarks>
internal sealed class IdentityManifestBuilder
{
    private static readonly JsonSerializerOptions s_writeOptions = new()
    {
        WriteIndented = false,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    /// <summary>The install identifier used as the subdirectory name under <c>installs/</c>.</summary>
    public string InstallId { get; init; } = "default";

    /// <summary>The install route string stored in the manifest.</summary>
    public string Route { get; init; } = "script";

    /// <summary>The update command, or <see langword="null"/> when not applicable.</summary>
    public string? UpdateCommand { get; init; }

    /// <summary>
    /// Writes the v2 identity manifest tree under <paramref name="root"/> and returns the install directory path.
    /// Creates: <c>root/installs/&lt;InstallId&gt;/identity.json</c> and a placeholder binary under
    /// <c>root/installs/&lt;InstallId&gt;/bin/aspire[.exe]</c>.
    /// </summary>
    /// <returns>The per-install directory (<c>root/installs/&lt;InstallId&gt;</c>).</returns>
    public string WriteTo(string root)
    {
        var installDir = Directory.CreateDirectory(Path.Combine(root, "installs", InstallId)).FullName;
        var manifest = new IdentityRecord(Route, UpdateCommand);
        File.WriteAllText(Path.Combine(installDir, "identity.json"), JsonSerializer.Serialize(manifest, s_writeOptions));

        var binDir = Directory.CreateDirectory(Path.Combine(installDir, "bin")).FullName;
        File.WriteAllText(Path.Combine(binDir, GetBinaryName()), string.Empty);

        return installDir;
    }

    // ── Factory methods ──────────────────────────────────────────────────────

    /// <summary>Identity manifest for a script-route install.</summary>
    public static IdentityManifestBuilder ForScript(string installId = "script-install") =>
        new() { InstallId = installId, Route = "script" };

    /// <summary>Identity manifest for a PR dogfood install.</summary>
    public static IdentityManifestBuilder ForPr(int prNumber, string? installId = null) =>
        new()
        {
            InstallId = installId ?? $"pr-{prNumber}",
            Route = "pr",
            UpdateCommand = $"get-aspire-cli-pr.sh -r {prNumber}",
        };

    private static string GetBinaryName() =>
        OperatingSystem.IsWindows() ? "aspire.exe" : "aspire";

    // ── Private JSON shape ───────────────────────────────────────────────────

    private sealed record IdentityRecord(
        [property: JsonPropertyName("route")] string Route,
        [property: JsonPropertyName("updateCommand")] string? UpdateCommand);
}

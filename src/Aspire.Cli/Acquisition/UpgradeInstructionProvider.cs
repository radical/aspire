// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Reflection;
using Aspire.Cli.Utils;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Provides the user-facing update command hint for a given install route.
/// </summary>
internal interface IUpgradeInstructionProvider
{
    /// <summary>
    /// Returns the update command string to print when <c>aspire update --self</c> refuses to
    /// update in-process for the given route, or <c>null</c> when the route handles its own update.
    /// </summary>
    /// <param name="route">The detected install route.</param>
    /// <param name="sidecarUpdateCommand">
    /// The <c>updateCommand</c> field from the sidecar (may be <c>null</c> if the sidecar was
    /// absent or the field was missing).
    /// </param>
    /// <param name="binaryPath">The fully-resolved binary path (used for dotnet-tool --tool-path detection).</param>
    string? Get(InstallRoute route, string? sidecarUpdateCommand, string binaryPath);
}

/// <summary>
/// Default implementation of <see cref="IUpgradeInstructionProvider"/>.
/// Source order: sidecar field → hardcoded per-route fallback → generic fallback.
/// </summary>
internal sealed class UpgradeInstructionProvider(IIdentityChannelReader channelReader) : IUpgradeInstructionProvider
{
    /// <inheritdoc/>
    public string? Get(InstallRoute route, string? sidecarUpdateCommand, string binaryPath)
    {
        // Script route: in-process. Caller should not call this method for script installs.
        if (route == InstallRoute.Script)
        {
            return null;
        }

        // 1. Sidecar updateCommand takes precedence when present — except for DotnetTool where we
        //    runtime-detect to handle --tool-path installs correctly (resolved decision B).
        if (!string.IsNullOrWhiteSpace(sidecarUpdateCommand) && route != InstallRoute.DotnetTool)
        {
            return sidecarUpdateCommand;
        }

        // 2. Per-route hardcoded fallback hints.
        return route switch
        {
            InstallRoute.Winget => "winget upgrade Microsoft.Aspire",
            InstallRoute.Brew => "brew upgrade aspire",
            InstallRoute.DotnetTool => GetDotnetToolUpdateCommand(binaryPath),
            InstallRoute.Pr => GetPrUpdateCommand(),
            InstallRoute.Unknown => null,
            _ => null,
        };
    }

    private static string GetDotnetToolUpdateCommand(string binaryPath)
    {
        // Delegate to DotNetToolDetection which already contains the path-shape logic for
        // distinguishing global (-g) vs --tool-path installs. Return the canonical global
        // command as fallback when path-shape detection fails.
        return DotNetToolDetection.GetDotNetToolUpdateCommand(binaryPath)
            ?? "dotnet tool update -g Aspire.Cli";
    }

    private string GetPrUpdateCommand()
    {
        // Attempt to parse the PR number from the running binary's InformationalVersion.
        var informationalVersion = Assembly.GetEntryAssembly()
            ?.GetCustomAttribute<AssemblyInformationalVersionAttribute>()
            ?.InformationalVersion;

        if (!string.IsNullOrEmpty(informationalVersion))
        {
            var prNumber = channelReader.GetPrNumber(informationalVersion);
            if (prNumber is not null)
            {
                return $"get-aspire-cli-pr.sh -r {prNumber}";
            }
        }

        return "get-aspire-cli-pr.sh";
    }
}

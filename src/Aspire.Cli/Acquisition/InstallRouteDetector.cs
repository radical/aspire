// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using Aspire.Cli.Utils;
using Microsoft.Extensions.Logging;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Detects the install route and optional update command for the running Aspire CLI binary.
/// </summary>
internal interface IInstallRouteDetector
{
    /// <summary>
    /// Detects the <see cref="InstallRoute"/> and the optional update-command hint from the
    /// <c>.aspire-install.json</c> sidecar (or from path-shape fallback when the sidecar is absent).
    /// </summary>
    /// <param name="binaryPath">
    /// The fully-resolved path to the running <c>aspire</c> binary (symlinks/shims already collapsed).
    /// </param>
    /// <returns>
    /// A tuple of (<see cref="InstallRoute"/>, <c>updateCommand</c>) where <c>updateCommand</c> is the
    /// user-facing hint string from the sidecar, or <c>null</c> if unavailable.
    /// </returns>
    (InstallRoute Route, string? UpdateCommand) Detect(string binaryPath);
}

/// <summary>
/// Default implementation of <see cref="IInstallRouteDetector"/>.
/// Reads the sidecar JSON via <see cref="AcquisitionJsonSerializerContext"/> (AOT-safe).
/// Falls back to <see cref="DotNetToolDetection"/> path-shape detection when the sidecar is absent.
/// </summary>
internal sealed class InstallRouteDetector(
    IInstallPathResolver pathResolver,
    ILogger<InstallRouteDetector> logger) : IInstallRouteDetector
{
    private const string SidecarFileName = ".aspire-install.json";

    /// <inheritdoc/>
    public (InstallRoute Route, string? UpdateCommand) Detect(string binaryPath)
    {
        var (mode, prefix) = pathResolver.Resolve(binaryPath);

        if (mode != InstallMode.Unknown)
        {
            var sidecarPath = Path.Combine(prefix, SidecarFileName);
            try
            {
                var json = File.ReadAllText(sidecarPath);
                var record = JsonSerializer.Deserialize(json, AcquisitionJsonSerializerContext.Default.SidecarRecord);
                if (record is not null && record.Route is not null)
                {
                    var route = ParseRoute(record.Route);
                    if (route != InstallRoute.Unknown)
                    {
                        return (route, record.UpdateCommand);
                    }

                    logger.LogWarning("Sidecar at '{SidecarPath}' contains unrecognized route '{Route}'. Falling back to path-shape detection.", sidecarPath, record.Route);
                }
                else
                {
                    logger.LogWarning("Sidecar at '{SidecarPath}' is missing or has a null route field. Falling back to path-shape detection.", sidecarPath);
                }
            }
            catch (Exception ex) when (ex is IOException or JsonException or UnauthorizedAccessException)
            {
                logger.LogWarning(ex, "Failed to read or parse sidecar at '{SidecarPath}'. Falling back to path-shape detection.", sidecarPath);
            }
        }

        // Last-resort: path-shape detection for dotnet-tool installs without a sidecar.
        if (DotNetToolDetection.IsRunningAsDotNetTool(binaryPath))
        {
            return (InstallRoute.DotnetTool, null);
        }

        return (InstallRoute.Unknown, null);
    }

    private static InstallRoute ParseRoute(string routeString) =>
        routeString.ToLowerInvariant() switch
        {
            "script" => InstallRoute.Script,
            "pr" => InstallRoute.Pr,
            "winget" => InstallRoute.Winget,
            "brew" => InstallRoute.Brew,
            "dotnet-tool" => InstallRoute.DotnetTool,
            _ => InstallRoute.Unknown,
        };
}

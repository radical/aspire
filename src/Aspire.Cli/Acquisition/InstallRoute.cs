// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Identifies the acquisition route used to install this Aspire CLI binary.
/// </summary>
internal enum InstallRoute
{
    /// <summary>
    /// The install route could not be determined.
    /// </summary>
    Unknown,

    /// <summary>
    /// Installed via the <c>get-aspire-cli.{sh,ps1}</c> script.
    /// </summary>
    Script,

    /// <summary>
    /// Installed via the <c>get-aspire-cli-pr.{sh,ps1}</c> PR script.
    /// </summary>
    Pr,

    /// <summary>
    /// Installed via Windows Package Manager (winget).
    /// </summary>
    Winget,

    /// <summary>
    /// Installed via Homebrew.
    /// </summary>
    Brew,

    /// <summary>
    /// Installed via <c>dotnet tool install</c>.
    /// </summary>
    DotnetTool,
}

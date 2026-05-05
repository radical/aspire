// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Describes the on-disk layout mode of an Aspire CLI install prefix.
/// </summary>
internal enum InstallMode
{
    /// <summary>
    /// The install mode could not be determined (sidecar not found at either expected location).
    /// </summary>
    Unknown,

    /// <summary>
    /// Mode A: the binary lives under a <c>bin/</c> subdirectory of the prefix.
    /// Used by the <c>script</c> and <c>pr</c> routes.
    /// </summary>
    A,

    /// <summary>
    /// Mode B: the binary lives at the prefix root alongside the sidecar.
    /// Used by the <c>winget</c>, <c>brew</c>, and <c>dotnet-tool</c> routes.
    /// </summary>
    B,
}

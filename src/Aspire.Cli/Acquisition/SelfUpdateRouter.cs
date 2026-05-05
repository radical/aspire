// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Determines whether a given install route performs self-update in-process or delegates.
/// </summary>
internal static class SelfUpdateRouter
{
    /// <summary>
    /// Returns <see langword="true"/> if the given <paramref name="route"/> performs self-update
    /// in-process (only <see cref="InstallRoute.Script"/>); returns <see langword="false"/> for all
    /// other routes including <see cref="InstallRoute.Unknown"/>, which must delegate or refuse.
    /// </summary>
    /// <param name="route">The install route of the running binary.</param>
    internal static bool RunsInProcess(InstallRoute route) => route == InstallRoute.Script;
}

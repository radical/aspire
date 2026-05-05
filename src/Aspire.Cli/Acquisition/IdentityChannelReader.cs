// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Reflection;
using System.Text.RegularExpressions;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Reads the immutable channel and PR number identity baked into the running binary at build time.
/// </summary>
internal interface IIdentityChannelReader
{
    /// <summary>
    /// Returns the channel name baked into the running binary via <c>[AssemblyMetadata("AspireCliChannel", ...)]</c>.
    /// Valid values are <c>stable</c>, <c>staging</c>, <c>daily</c>, or <c>pr</c>.
    /// </summary>
    /// <exception cref="InvalidOperationException">
    /// Thrown when the <c>AspireCliChannel</c> assembly metadata attribute is missing.
    /// This is a build-time bug that the CI smoke test in <c>AssemblyMetadataChannelTests.cs</c>
    /// should have caught before the binary reached production.
    /// </exception>
    string ReadChannel();

    /// <summary>
    /// Parses the PR number from an <c>InformationalVersion</c> string of the shape
    /// <c>0.0.0-pr&lt;N&gt;.&lt;sha&gt;</c> (for example <c>0.0.0-pr12345.abcdef1</c>).
    /// </summary>
    /// <param name="informationalVersion">The <see cref="AssemblyInformationalVersionAttribute.InformationalVersion"/> string.</param>
    /// <returns>The PR number if parseable; otherwise <c>null</c>.</returns>
    int? GetPrNumber(string informationalVersion);
}

/// <summary>
/// Default implementation of <see cref="IIdentityChannelReader"/> that reads assembly metadata
/// baked into the binary at build time. AOT-safe — only attribute reflection, no JSON.
/// </summary>
internal sealed class IdentityChannelReader : IIdentityChannelReader
{
    private const string ChannelMetadataKey = "AspireCliChannel";

    // Matches "0.0.0-pr12345.abcdef1" — captures just the decimal digits after "-pr".
    private static readonly Regex s_prVersionPattern = new(
        @"-pr(\d+)\.",
        RegexOptions.Compiled | RegexOptions.CultureInvariant,
        matchTimeout: TimeSpan.FromSeconds(1));

    /// <inheritdoc/>
    public string ReadChannel()
    {
        var attrs = Assembly.GetEntryAssembly()
            ?.GetCustomAttributes<AssemblyMetadataAttribute>();

        if (attrs is not null)
        {
            foreach (var attr in attrs)
            {
                if (string.Equals(attr.Key, ChannelMetadataKey, StringComparison.Ordinal))
                {
                    return attr.Value
                        ?? throw new InvalidOperationException(
                            $"Assembly metadata '{ChannelMetadataKey}' is present but has a null value. This is a build-time bug.");
                }
            }
        }

        throw new InvalidOperationException(
            $"Assembly metadata '{ChannelMetadataKey}' is missing from the running binary. " +
            "This is a build-time bug; the CI smoke test (AssemblyMetadataChannelTests) should have caught it.");
    }

    /// <inheritdoc/>
    public int? GetPrNumber(string informationalVersion)
    {
        if (string.IsNullOrEmpty(informationalVersion))
        {
            return null;
        }

        var match = s_prVersionPattern.Match(informationalVersion);
        if (!match.Success)
        {
            return null;
        }

        return int.TryParse(match.Groups[1].Value, out var prNumber) ? prNumber : null;
    }
}

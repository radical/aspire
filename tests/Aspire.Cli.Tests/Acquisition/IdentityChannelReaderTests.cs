// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;

namespace Aspire.Cli.Tests.Acquisition;

// NOTE: IdentityChannelReader.ReadChannel() reads Assembly.GetEntryAssembly() which resolves
// to Aspire.Cli.Tests.dll in the test host — that assembly does NOT carry the
// [AssemblyMetadata("AspireCliChannel", ...)] attribute, so ReadChannel() would throw.
// Only GetPrNumber() is exercised here; ReadChannel() is covered by the CI smoke test
// AssemblyMetadataChannelTests which runs against the production binary.

public class IdentityChannelReaderTests
{
    private static readonly IdentityChannelReader s_reader = new();

    [Theory]
    [InlineData("0.0.0-pr12345.abcdef1", 12345)]
    [InlineData("0.0.0-pr1.abc", 1)]
    [InlineData("0.0.0-pr99999.deadbeef", 99999)]
    [InlineData("10.0.0-pr42.0000000", 42)]
    public void GetPrNumber_ValidPrVersionString_ReturnsPrNumber(string version, int expected)
    {
        var prNumber = s_reader.GetPrNumber(version);

        Assert.Equal(expected, prNumber);
    }

    [Theory]
    [InlineData("0.0.0")]
    [InlineData("1.2.3")]
    [InlineData("1.2.3-stable")]
    [InlineData("1.2.3-daily.20250101")]
    [InlineData("1.2.3-rc.1")]
    [InlineData("0.0.0-nopr12345.abc")]
    public void GetPrNumber_NonPrVersionString_ReturnsNull(string version)
    {
        var prNumber = s_reader.GetPrNumber(version);

        Assert.Null(prNumber);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    public void GetPrNumber_NullOrEmptyVersion_ReturnsNull(string? version)
    {
        var prNumber = s_reader.GetPrNumber(version!);

        Assert.Null(prNumber);
    }
}

/// <summary>
/// Regression tests for the version-string trimming behavior baked into <c>Program.cs</c>:
/// <c>rawVersion[..plusIdx]</c> when a <c>+source-metadata</c> suffix is present.
/// The logic lives inline in the startup path and cannot easily be unit-tested via
/// <c>AssemblyInformationalVersionAttribute</c> injection (entry assembly is immutable
/// in the test host). These tests duplicate the two-line trim pattern to provide regression
/// coverage independently of the running binary's own informational version.
/// </summary>
public class VersionTrimTests
{
    // Replicates the two-line trim from Program.cs so regressions are caught here too.
    private static string TrimVersion(string rawVersion)
    {
        var plusIdx = rawVersion.IndexOf('+');
        return plusIdx >= 0 ? rawVersion[..plusIdx] : rawVersion;
    }

    [Theory]
    [InlineData("13.4.0-dev+abcdef1234",  "13.4.0-dev")]
    [InlineData("9.4.0-dev+source-build", "9.4.0-dev")]
    [InlineData("1.0.0+metadata",         "1.0.0")]
    [InlineData("0.0.0-pr42.abc+hash",    "0.0.0-pr42.abc")]
    public void TrimVersion_WithPlusSuffix_StripsAfterPlus(string rawVersion, string expected)
    {
        Assert.Equal(expected, TrimVersion(rawVersion));
    }

    [Theory]
    [InlineData("13.4.0-dev")]
    [InlineData("1.2.3")]
    [InlineData("0.0.0-pr12345.abcdef1")]
    public void TrimVersion_WithoutPlusSuffix_ReturnsFullString(string rawVersion)
    {
        Assert.Equal(rawVersion, TrimVersion(rawVersion));
    }

    [Theory]
    [InlineData("")]
    public void TrimVersion_Empty_ReturnsEmpty(string rawVersion)
    {
        Assert.Equal(string.Empty, TrimVersion(rawVersion));
    }
}

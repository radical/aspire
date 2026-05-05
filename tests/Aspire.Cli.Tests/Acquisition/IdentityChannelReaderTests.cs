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

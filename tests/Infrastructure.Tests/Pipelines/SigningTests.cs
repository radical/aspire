// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Xml.Linq;
using Xunit;

namespace Infrastructure.Tests;

public sealed class SigningTests
{
    [Fact]
    public async Task DashboardThirdPartyDependenciesUseThirdPartyCertificate()
    {
        var signingPropsPath = Path.Combine(RepoRoot.Path, "eng", "Signing.props");
        var signingProps = XDocument.Parse(await File.ReadAllTextAsync(signingPropsPath));
        var thirdPartyFiles = signingProps
            .Descendants("FileSignInfo")
            .Where(element => (string?)element.Attribute("CertificateName") == "3PartySHA2")
            .Select(element => (string?)element.Attribute("Include"))
            .ToHashSet(StringComparer.Ordinal);

        Assert.Contains("Dapper.dll", thirdPartyFiles);
        Assert.Contains("OpenTelemetry.Instrumentation.AspNetCore.dll", thirdPartyFiles);
        Assert.Contains("SQLitePCLRaw.batteries_v2.dll", thirdPartyFiles);
        Assert.Contains("SQLitePCLRaw.core.dll", thirdPartyFiles);
        Assert.Contains("SQLitePCLRaw.provider.e_sqlite3.dll", thirdPartyFiles);
        Assert.Contains("e_sqlite3.dll", thirdPartyFiles);
    }
}

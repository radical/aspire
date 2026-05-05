// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Tests.Acquisition.Fakes;

namespace Aspire.Cli.Tests.Acquisition;

public class PathResolverTests
{
    private static readonly InstallPathResolver s_resolver = new();

    [Fact]
    public void Resolve_ModeB_SidecarNextToBinary_ReturnsModeB()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForScript(), InstallMode.B);

        var (mode, prefix) = s_resolver.Resolve(fixture.BinaryPath);

        Assert.Equal(InstallMode.B, mode);
        Assert.Equal(fixture.Prefix, prefix);
    }

    [Fact]
    public void Resolve_ModeA_SidecarInParentDir_ReturnsModeA()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForScript(), InstallMode.A);

        var (mode, prefix) = s_resolver.Resolve(fixture.BinaryPath);

        Assert.Equal(InstallMode.A, mode);
        Assert.Equal(fixture.Prefix, prefix);
    }

    [Fact]
    public void Resolve_NoSidecar_ReturnsUnknown()
    {
        using var tempDir = new TestTempDirectory();
        var binaryPath = Path.Combine(tempDir.Path, "aspire");
        File.WriteAllText(binaryPath, string.Empty);

        var (mode, prefix) = s_resolver.Resolve(binaryPath);

        Assert.Equal(InstallMode.Unknown, mode);
        // Prefix is the binary directory when unknown.
        Assert.Equal(tempDir.Path, prefix);
    }

    [Fact]
    public void Resolve_ModeB_ReturnsPrefixEqualToBinaryDir()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForWinget(), InstallMode.B);

        var (mode, prefix) = s_resolver.Resolve(fixture.BinaryPath);

        Assert.Equal(InstallMode.B, mode);
        // In Mode B the binary is in the prefix root, so the prefix == binary directory.
        Assert.Equal(Path.GetDirectoryName(fixture.BinaryPath), prefix);
    }

    [Fact]
    public void Resolve_ModeA_ReturnsPrefixOneAboveBinaryDir()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForBrew(), InstallMode.A);

        var (mode, prefix) = s_resolver.Resolve(fixture.BinaryPath);

        Assert.Equal(InstallMode.A, mode);
        // In Mode A the binary is under prefix/bin, so prefix == parent of binary's directory.
        var expectedPrefix = Path.GetDirectoryName(Path.GetDirectoryName(fixture.BinaryPath));
        Assert.Equal(expectedPrefix, prefix);
    }
}

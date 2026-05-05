// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Tests.Acquisition.Fakes;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests.Acquisition;

public class InstallRouteDetectorTests
{
    private static InstallRouteDetector MakeDetector() =>
        new(new InstallPathResolver(), NullLogger<InstallRouteDetector>.Instance);

    [Theory]
    [InlineData("script", InstallRoute.Script, null)]
    [InlineData("pr", InstallRoute.Pr, "get-aspire-cli-pr.sh -r 42")]
    [InlineData("winget", InstallRoute.Winget, "winget upgrade Microsoft.Aspire")]
    [InlineData("brew", InstallRoute.Brew, "brew upgrade aspire")]
    [InlineData("dotnet-tool", InstallRoute.DotnetTool, "dotnet tool update -g Aspire.Cli")]
    internal void Detect_SidecarWithKnownRoute_ReturnsSidecarRouteAndUpdateCommand(
        string routeString, InstallRoute expectedRoute, string? expectedUpdateCommand)
    {
        var sidecar = new SidecarBuilder { Route = routeString, UpdateCommand = expectedUpdateCommand };
        using var fixture = new FakeRouteFixture().WithSidecar(sidecar, InstallMode.A);
        var detector = MakeDetector();

        var (route, updateCommand) = detector.Detect(fixture.BinaryPath);

        Assert.Equal(expectedRoute, route);
        Assert.Equal(expectedUpdateCommand, updateCommand);
    }

    [Fact]
    public void Detect_SidecarWithUnrecognizedRoute_FallsBackToPathShapeUnknown()
    {
        var sidecar = new SidecarBuilder { Route = "not-a-real-route" };
        using var fixture = new FakeRouteFixture().WithSidecar(sidecar, InstallMode.A);
        var detector = MakeDetector();

        var (route, updateCommand) = detector.Detect(fixture.BinaryPath);

        Assert.Equal(InstallRoute.Unknown, route);
        Assert.Null(updateCommand);
    }

    [Fact]
    public void Detect_NoSidecar_BinaryNotOnDotNetToolPath_ReturnsUnknown()
    {
        using var tempDir = new TestTempDirectory();
        var binaryPath = Path.Combine(tempDir.Path, "aspire");
        File.WriteAllText(binaryPath, string.Empty);
        var detector = MakeDetector();

        var (route, updateCommand) = detector.Detect(binaryPath);

        Assert.Equal(InstallRoute.Unknown, route);
        Assert.Null(updateCommand);
    }

    [Fact]
    public void Detect_ModeB_SidecarRouteIsReturned()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForScript(), InstallMode.B);
        var detector = MakeDetector();

        var (route, _) = detector.Detect(fixture.BinaryPath);

        Assert.Equal(InstallRoute.Script, route);
    }

    [Fact]
    public void Detect_SidecarPresentRouteIsScript_UpdateCommandIsNull()
    {
        using var fixture = new FakeRouteFixture().WithSidecar(SidecarBuilder.ForScript(), InstallMode.A);
        var detector = MakeDetector();

        var (route, updateCommand) = detector.Detect(fixture.BinaryPath);

        Assert.Equal(InstallRoute.Script, route);
        Assert.Null(updateCommand);
    }

    [Fact]
    public void Detect_CorruptSidecarJson_FallsBackToUnknown()
    {
        using var tempDir = new TestTempDirectory();
        var prefix = Directory.CreateDirectory(Path.Combine(tempDir.Path, "prefix")).FullName;
        // Write invalid JSON to the sidecar file.
        File.WriteAllText(Path.Combine(prefix, ".aspire-install.json"), "{ not valid json }");
        var binDir = Directory.CreateDirectory(Path.Combine(prefix, "bin")).FullName;
        var binaryPath = Path.Combine(binDir, OperatingSystem.IsWindows() ? "aspire.exe" : "aspire");
        File.WriteAllText(binaryPath, string.Empty);
        var detector = MakeDetector();

        var (route, updateCommand) = detector.Detect(binaryPath);

        Assert.Equal(InstallRoute.Unknown, route);
        Assert.Null(updateCommand);
    }
}

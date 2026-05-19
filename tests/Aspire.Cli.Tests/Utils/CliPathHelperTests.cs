// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Utils;

namespace Aspire.Cli.Tests.Utils;

public class CliPathHelperTests(ITestOutputHelper outputHelper)
{
    [Fact]
    public void CreateGuestAppHostSocketPath_UsesRandomizedIdentifier()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);

        var socketPath1 = CliPathHelper.CreateGuestAppHostSocketPath("apphost.sock");
        var socketPath2 = CliPathHelper.CreateGuestAppHostSocketPath("apphost.sock");

        Assert.NotEqual(socketPath1, socketPath2);

        if (OperatingSystem.IsWindows())
        {
            Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", socketPath1);
            Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", socketPath2);
        }
        else
        {
            var expectedDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".aspire", "cli", "runtime", "sockets");
            Assert.Equal(expectedDirectory, Path.GetDirectoryName(socketPath1));
            Assert.Equal(expectedDirectory, Path.GetDirectoryName(socketPath2));
            Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", Path.GetFileName(socketPath1));
            Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", Path.GetFileName(socketPath2));
        }
    }

    [Fact]
    public void CreateUnixDomainSocketPath_UsesRandomizedIdentifier()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);

        var socketPath1 = CliPathHelper.CreateUnixDomainSocketPath("apphost.sock");
        var socketPath2 = CliPathHelper.CreateUnixDomainSocketPath("apphost.sock");

        Assert.NotEqual(socketPath1, socketPath2);

        var expectedDirectory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".aspire", "cli", "runtime", "sockets");
        Assert.Equal(expectedDirectory, Path.GetDirectoryName(socketPath1));
        Assert.Equal(expectedDirectory, Path.GetDirectoryName(socketPath2));
        Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", Path.GetFileName(socketPath1));
        Assert.Matches("^apphost\\.sock\\.[a-f0-9]{12}$", Path.GetFileName(socketPath2));
    }

    [Theory]
    [InlineData("script")]
    [InlineData("localhive")]
    public void TryGetAspireHomeDirectoryFromInstallRoute_SharedPrefixRoute_ReturnsInstallPrefix(string source)
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var installPrefix = Path.Combine(workspace.WorkspaceRoot.FullName, "aspire");
        var binDir = Path.Combine(installPrefix, "bin");
        var binaryPath = WriteBinaryWithSidecar(binDir, source);

        var result = CliPathHelper.TryGetAspireHomeDirectoryFromInstallRoute(binaryPath);

        Assert.Equal(installPrefix, result);
    }

    [Fact]
    public void TryGetAspireHomeDirectoryFromInstallRoute_PrRoute_ReturnsOuterInstallPrefix()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var installPrefix = Path.Combine(workspace.WorkspaceRoot.FullName, "aspire-pr-test");
        var binDir = Path.Combine(installPrefix, "dogfood", "pr-17159", "bin");
        var binaryPath = WriteBinaryWithSidecar(binDir, "pr");

        var result = CliPathHelper.TryGetAspireHomeDirectoryFromInstallRoute(binaryPath);

        Assert.Equal(installPrefix, result);
    }

    [Theory]
    [InlineData("brew")]
    [InlineData("winget")]
    [InlineData("dotnet-tool")]
    [InlineData("unknown")]
    public void TryGetAspireHomeDirectoryFromInstallRoute_PackageManagerOrUnknownRoute_ReturnsNull(string source)
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var binaryPath = WriteBinaryWithSidecar(workspace.WorkspaceRoot.FullName, source);

        var result = CliPathHelper.TryGetAspireHomeDirectoryFromInstallRoute(binaryPath);

        Assert.Null(result);
    }

    [Fact]
    public void GetAspireHomeDirectory_PrRoute_UsesOuterInstallPrefix()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var installPrefix = Path.Combine(workspace.WorkspaceRoot.FullName, "portable");
        var binDir = Path.Combine(installPrefix, "dogfood", "pr-17159", "bin");
        var binaryPath = WriteBinaryWithSidecar(binDir, "pr");

        var result = CliPathHelper.GetAspireHomeDirectory(binaryPath);

        Assert.Equal(installPrefix, result);
    }

    [Fact]
    public void ResolveSymlinkOrOriginalPath_NonLink_ReturnsOriginalPath()
    {
        const string path = "relative/path/aspire";

        var result = CliPathHelper.ResolveSymlinkOrOriginalPath(path);

        Assert.Equal(path, result);
    }

    [Fact]
    public void ResolveSymlinkToFullPath_NonLink_ReturnsNormalizedFullPath()
    {
        var path = Path.Combine("relative", "path", "aspire");

        var result = CliPathHelper.ResolveSymlinkToFullPath(path);

        Assert.Equal(Path.GetFullPath(path), result);
    }

    [Fact]
    public void ResolveSymlinkToFullPath_InvalidPath_ReturnsNull()
    {
        var result = CliPathHelper.ResolveSymlinkToFullPath("invalid\0path");

        Assert.Null(result);
    }

    [Fact]
    public void ResolveSymlinkHelpers_Link_ReturnsTarget()
    {
        Assert.SkipUnless(OperatingSystem.IsLinux() || OperatingSystem.IsMacOS(),
            "Symlink resolution test only runs on Linux/macOS where unprivileged symlink creation is reliable.");

        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var target = Path.Combine(workspace.WorkspaceRoot.FullName, "target-aspire");
        File.WriteAllText(target, string.Empty);

        var link = Path.Combine(workspace.WorkspaceRoot.FullName, "aspire");
        File.CreateSymbolicLink(link, target);

        Assert.Equal(target, CliPathHelper.ResolveSymlinkOrOriginalPath(link));
        Assert.Equal(target, CliPathHelper.ResolveSymlinkToFullPath(link));
    }

    private static string WriteBinaryWithSidecar(string binaryDir, string source)
    {
        Directory.CreateDirectory(binaryDir);
        var binaryPath = Path.Combine(binaryDir, OperatingSystem.IsWindows() ? "aspire.exe" : "aspire");
        File.WriteAllText(binaryPath, string.Empty);
        File.WriteAllText(Path.Combine(binaryDir, InstallSidecarReader.SidecarFileName), $$"""{"source":"{{source}}"}""");

        return binaryPath;
    }
}

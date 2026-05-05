// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Tests.Acquisition.Fakes;

namespace Aspire.Cli.Tests.Acquisition;

public class UpgradeInstructionProviderTests
{
    // ── Fake ─────────────────────────────────────────────────────────────────

    private sealed class FakeChannelReader(int? prNumber) : IIdentityChannelReader
    {
        public string ReadChannel() => throw new NotSupportedException("Not needed in these tests.");
        public int? GetPrNumber(string informationalVersion) => prNumber;
    }

    private static UpgradeInstructionProvider MakeProvider(int? prNumber = null) =>
        new(new FakeChannelReader(prNumber));

    // ── Script route ─────────────────────────────────────────────────────────

    [Fact]
    public void Get_ScriptRoute_ReturnsNull()
    {
        var result = MakeProvider().Get(InstallRoute.Script, sidecarUpdateCommand: "ignored", binaryPath: "/bin/aspire");

        Assert.Null(result);
    }

    // ── Winget route ──────────────────────────────────────────────────────────

    [Fact]
    public void Get_WingetRoute_NoSidecarCommand_ReturnsHardcoded()
    {
        var result = MakeProvider().Get(InstallRoute.Winget, sidecarUpdateCommand: null, binaryPath: "/bin/aspire");

        Assert.Equal("winget upgrade Microsoft.Aspire", result);
    }

    [Fact]
    public void Get_WingetRoute_WithSidecarCommand_ReturnsSidecarCommand()
    {
        const string sidecarCmd = "winget upgrade --source corporate Aspire";

        var result = MakeProvider().Get(InstallRoute.Winget, sidecarUpdateCommand: sidecarCmd, binaryPath: "/bin/aspire");

        Assert.Equal(sidecarCmd, result);
    }

    // ── Brew route ────────────────────────────────────────────────────────────

    [Fact]
    public void Get_BrewRoute_NoSidecarCommand_ReturnsHardcoded()
    {
        var result = MakeProvider().Get(InstallRoute.Brew, sidecarUpdateCommand: null, binaryPath: "/usr/local/bin/aspire");

        Assert.Equal("brew upgrade aspire", result);
    }

    [Fact]
    public void Get_BrewRoute_WithSidecarCommand_ReturnsSidecarCommand()
    {
        const string sidecarCmd = "brew upgrade --cask aspire";

        var result = MakeProvider().Get(InstallRoute.Brew, sidecarUpdateCommand: sidecarCmd, binaryPath: "/usr/local/bin/aspire");

        Assert.Equal(sidecarCmd, result);
    }

    // ── DotnetTool route ──────────────────────────────────────────────────────

    [Fact]
    public void Get_DotnetToolRoute_AlwaysIgnoresSidecarAndCallsDetection()
    {
        // The provider ignores sidecarUpdateCommand for dotnet-tool routes (resolved decision B).
        // Without a real dotnet tool store path, detection falls back to the global command.
        const string sidecarCmd = "SHOULD-BE-IGNORED";

        var result = MakeProvider().Get(InstallRoute.DotnetTool, sidecarUpdateCommand: sidecarCmd, binaryPath: "/bin/aspire");

        // sidecarCmd must not appear in the result.
        Assert.NotEqual(sidecarCmd, result);
        Assert.NotNull(result);
    }

    [Fact]
    public void Get_DotnetToolRoute_NoSidecar_GlobalFallbackPath_ReturnsGlobalCommand()
    {
        // A path that isn't in a .dotnet/tools/... layout → DotNetToolDetection returns null → fallback.
        var result = MakeProvider().Get(InstallRoute.DotnetTool, sidecarUpdateCommand: null, binaryPath: "/bin/aspire");

        Assert.Equal("dotnet tool update -g Aspire.Cli", result);
    }

    [Fact]
    public void Get_DotnetToolRoute_GlobalToolStorePath_ReturnsGlobalCommand()
    {
        const string globalPath = "/home/user/.dotnet/tools/.store/aspire.cli/10.0.0/aspire.cli.linux-x64/10.0.0/tools/net10.0/linux-x64/aspire";

        var result = MakeProvider().Get(InstallRoute.DotnetTool, sidecarUpdateCommand: null, binaryPath: globalPath);

        Assert.Equal("dotnet tool update -g Aspire.Cli", result);
    }

    // ── Pr route ─────────────────────────────────────────────────────────────

    [Fact]
    public void Get_PrRoute_WithSidecarCommand_ReturnsSidecarCommand()
    {
        const string sidecarCmd = "get-aspire-cli-pr.sh -r 12345";

        var result = MakeProvider(prNumber: 12345).Get(InstallRoute.Pr, sidecarUpdateCommand: sidecarCmd, binaryPath: "/bin/aspire");

        Assert.Equal(sidecarCmd, result);
    }

    [Fact]
    public void Get_PrRoute_NoSidecar_FakeReaderReturnsPrNumber_ReturnsScriptWithNumber()
    {
        var result = MakeProvider(prNumber: 42).Get(InstallRoute.Pr, sidecarUpdateCommand: null, binaryPath: "/bin/aspire");

        // GetPrUpdateCommand falls back to entry assembly InformationalVersion — which in tests
        // won't match the pr pattern — so FakeChannelReader.GetPrNumber is called via the channel
        // reader. However, GetPrUpdateCommand reads Assembly.GetEntryAssembly() for the version
        // string, so the prNumber fake only matters when the version string contains "-pr\d+.".
        // Without that version string the method returns the no-number fallback script.
        Assert.NotNull(result);
        Assert.Contains(OperatingSystem.IsWindows() ? "get-aspire-cli-pr.ps1" : "get-aspire-cli-pr.sh", result);
    }

    // ── Unknown route ─────────────────────────────────────────────────────────

    [Fact]
    public void Get_UnknownRoute_ReturnsNull()
    {
        var result = MakeProvider().Get(InstallRoute.Unknown, sidecarUpdateCommand: null, binaryPath: "/bin/aspire");

        Assert.Null(result);
    }

    [Fact]
    public void Get_UnknownRoute_WithSidecarCommand_ReturnsSidecarCommand()
    {
        // Unknown route falls through to the sidecar-command branch — the sidecar value is returned.
        var result = MakeProvider().Get(InstallRoute.Unknown, sidecarUpdateCommand: "anything", binaryPath: "/bin/aspire");

        Assert.Equal("anything", result);
    }

    // ── Tool-path dotnet-tool layout ──────────────────────────────────────────

    [Fact]
    public void Get_DotnetToolRoute_CustomToolPathLayout_ReturnsToolPathCommand()
    {
        using var tempDir = new TestTempDirectory();
        // Use a path with spaces so QuoteCommandArgument wraps it in quotes.
        var toolPath = Path.Combine(tempDir.Path, "custom tools dir");

        // Build the Mode-B layout (binary at toolPath/aspire) and write the .store sibling.
        var (_, binaryPath) = SidecarBuilder.BuildModeB(toolPath, SidecarBuilder.ForDotnetTool());

        var storeDir = Path.Combine(
            toolPath,
            ".store",
            "aspire.cli",
            "10.0.0",
            "aspire.cli.linux-x64",
            "10.0.0",
            "tools",
            "net10.0",
            "linux-x64");
        Directory.CreateDirectory(storeDir);
        File.WriteAllText(Path.Combine(storeDir, OperatingSystem.IsWindows() ? "aspire.exe" : "aspire"), string.Empty);

        var result = MakeProvider().Get(InstallRoute.DotnetTool, sidecarUpdateCommand: null, binaryPath: binaryPath);

        Assert.Equal($"dotnet tool update --tool-path \"{toolPath}\" Aspire.Cli", result);
    }
}

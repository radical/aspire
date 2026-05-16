// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.TestUtilities;
using Microsoft.DotNet.XUnitExtensions;
using Xunit;

namespace Aspire.Acquisition.Tests.Scripts;

/// <summary>
/// Tests for package-manager installer modes on get-aspire-cli-pr.{sh,ps1}.
/// </summary>
public class PRScriptInstallerModeTests(ITestOutputHelper testOutput)
{
    private readonly ITestOutputHelper _testOutput = testOutput;

    private async Task<ScriptToolCommand> CreateBashCommandWithMockGhAsync(TestEnvironment env)
    {
        var mockGhPath = await env.CreateMockGhScriptAsync(_testOutput);
        var cmd = new ScriptToolCommand(ScriptPaths.PRShell, env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockGhPath}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");
        return cmd;
    }

    private async Task<ScriptToolCommand> CreatePsCommandWithMockGhAsync(TestEnvironment env)
    {
        var mockGhPath = await env.CreateMockGhScriptAsync(_testOutput);
        var cmd = new ScriptToolCommand(ScriptPaths.PRPowerShell, env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockGhPath}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");
        return cmd;
    }

    private static async Task<string> CreateHomebrewInstallerArtifactAsync(string root)
    {
        Directory.CreateDirectory(root);
        await File.WriteAllTextAsync(Path.Combine(root, "aspire.rb"), "cask \"aspire\" do\n  version \"13.3.0\"\nend\n");
        await File.WriteAllTextAsync(Path.Combine(root, "dogfood.sh"), "#!/usr/bin/env bash\nexit 0\n");
        await FakeArchiveHelper.CreateFakeNupkgAsync(root, "Aspire.Cli", "13.3.0-pr.1234.abc");
        await FakeArchiveHelper.CreateFakeNupkgAsync(root, "Aspire.Hosting", "13.3.0-pr.1234.abc");
        return root;
    }

    private static async Task<string> CreateWinGetInstallerArtifactAsync(string root)
    {
        Directory.CreateDirectory(root);
        await File.WriteAllTextAsync(Path.Combine(root, "Microsoft.Aspire.installer.yaml"), "PackageIdentifier: Microsoft.Aspire\nPackageVersion: 13.3.0\nInstallers: []\n");
        await File.WriteAllTextAsync(Path.Combine(root, "dogfood.ps1"), "exit 0\n");
        await FakeArchiveHelper.CreateFakeNupkgAsync(root, "Aspire.Cli", "13.3.0-pr.1234.abc");
        await FakeArchiveHelper.CreateFakeNupkgAsync(root, "Aspire.Hosting", "13.3.0-pr.1234.abc");
        return root;
    }

    [Fact]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_Help_DescribesInstallerModes()
    {
        using var env = new TestEnvironment();
        using var cmd = new ScriptToolCommand(ScriptPaths.PRShell, env, _testOutput);

        var result = await cmd.ExecuteAsync("--help");

        result.EnsureSuccessful();
        Assert.Contains("winget", result.Output);
        Assert.Contains("homebrew", result.Output);
    }

    [Fact]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_WinGetMode_PrDryRun_DownloadsManifestAndNativeArchives()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreateBashCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "12345",
            "--install-mode", "winget",
            "--force",
            "--dry-run",
            "--skip-extension",
            "--verbose");

        result.EnsureSuccessful();
        Assert.Contains("winget-manifests-prerelease", result.Output);
        Assert.Contains("cli-native-archives-win-x64", result.Output);
        Assert.Contains("cli-native-archives-win-arm64", result.Output);
        Assert.Contains("-ArchiveRoot", result.Output);
        Assert.Contains("-Force", result.Output);
        Assert.Contains("built-nugets", result.Output);
        Assert.DoesNotContain("Add to your shell profile", result.Output);
        Assert.DoesNotContain("route sidecar", result.Output, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("dogfood/pr-12345/bin", result.Output);
    }

    [Fact]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_HomebrewMode_PrDryRun_DownloadsCaskAndNativeArchives()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreateBashCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "12345",
            "--install-mode", "homebrew",
            "--dry-run",
            "--skip-extension",
            "--verbose");

        result.EnsureSuccessful();
        Assert.Contains("homebrew-cask-prerelease", result.Output);
        Assert.Contains("cli-native-archives-osx-arm64", result.Output);
        Assert.Contains("cli-native-archives-osx-x64", result.Output);
        Assert.Contains("--archive-root", result.Output);
        Assert.Contains("built-nugets", result.Output);
        Assert.DoesNotContain("Add to your shell profile", result.Output);
        Assert.DoesNotContain("route sidecar", result.Output, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("dogfood/pr-12345/bin", result.Output);
    }

    [Fact]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_HomebrewMode_LocalDir_DryRun_UsesDogfoodArtifact()
    {
        using var env = new TestEnvironment();
        var localDir = await CreateHomebrewInstallerArtifactAsync(Path.Combine(env.TempDirectory, "homebrew-artifact"));
        using var cmd = new ScriptToolCommand(ScriptPaths.PRShell, env, _testOutput);

        var result = await cmd.ExecuteAsync(
            "--local-dir", localDir,
            "--install-mode", "homebrew",
            "--dry-run",
            "--skip-path");

        result.EnsureSuccessful();
        Assert.Contains("dogfood.sh", result.Output);
        Assert.Contains("--archive-root", result.Output);
        Assert.Contains("Would copy nugets", result.Output);
        Assert.DoesNotContain("Would install CLI archive", result.Output);
    }

    [Fact]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_InstallerMode_RejectsHiveOnly()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreateBashCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "12345",
            "--install-mode", "homebrew",
            "--hive-only",
            "--dry-run",
            "--skip-extension");

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("--hive-only cannot be combined with --install-mode homebrew", result.Output);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task PowerShell_WinGetMode_WhatIf_DownloadsManifestAndNativeArchives()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreatePsCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "-PRNumber", "12345",
            "-InstallMode", "WinGet",
            "-Force",
            "-WhatIf",
            "-SkipExtension",
            "-Verbose");

        result.EnsureSuccessful();
        Assert.Contains("winget-manifests-prerelease", result.Output);
        Assert.Contains("cli-native-archives-win-x64", result.Output);
        Assert.Contains("cli-native-archives-win-arm64", result.Output);
        Assert.Contains("-ArchiveRoot", result.Output);
        Assert.Contains("-Force", result.Output);
        Assert.Contains("built-nugets", result.Output);
        Assert.DoesNotContain("Add to your shell profile", result.Output);
        Assert.DoesNotContain("Route sidecar", result.Output);
        Assert.DoesNotContain($"dogfood{Path.DirectorySeparatorChar}pr-12345{Path.DirectorySeparatorChar}bin", result.Output);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task PowerShell_HomebrewMode_WhatIf_DownloadsCaskAndNativeArchives()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreatePsCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "-PRNumber", "12345",
            "-InstallMode", "Homebrew",
            "-WhatIf",
            "-SkipExtension",
            "-Verbose");

        result.EnsureSuccessful();
        Assert.Contains("homebrew-cask-prerelease", result.Output);
        Assert.Contains("cli-native-archives-osx-arm64", result.Output);
        Assert.Contains("cli-native-archives-osx-x64", result.Output);
        Assert.Contains("--archive-root", result.Output);
        Assert.Contains("built-nugets", result.Output);
        Assert.DoesNotContain("Add to your shell profile", result.Output);
        Assert.DoesNotContain("Route sidecar", result.Output);
        Assert.DoesNotContain($"dogfood{Path.DirectorySeparatorChar}pr-12345{Path.DirectorySeparatorChar}bin", result.Output);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task PowerShell_WinGetMode_LocalDir_WhatIf_UsesDogfoodArtifact()
    {
        using var env = new TestEnvironment();
        var localDir = await CreateWinGetInstallerArtifactAsync(Path.Combine(env.TempDirectory, "winget-artifact"));
        using var cmd = new ScriptToolCommand(ScriptPaths.PRPowerShell, env, _testOutput);

        var result = await cmd.ExecuteAsync(
            "-LocalDir", localDir,
            "-InstallMode", "WinGet",
            "-WhatIf",
            "-SkipPath");

        result.EnsureSuccessful();
        Assert.Contains("dogfood.ps1", result.Output);
        Assert.Contains("-ArchiveRoot", result.Output);
        Assert.Contains("Copying built nugets", result.Output);
        Assert.DoesNotContain("Installing Aspire CLI to", result.Output);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task PowerShell_InstallerMode_RejectsHiveOnly()
    {
        using var env = new TestEnvironment();
        using var cmd = await CreatePsCommandWithMockGhAsync(env);

        var result = await cmd.ExecuteAsync(
            "-PRNumber", "12345",
            "-InstallMode", "Homebrew",
            "-HiveOnly",
            "-WhatIf",
            "-SkipExtension");

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("-HiveOnly cannot be combined with -InstallMode Homebrew", result.Output);
    }
}

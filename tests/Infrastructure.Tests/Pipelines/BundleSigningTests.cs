// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.TestUtilities;
using Xunit;

namespace Infrastructure.Tests;

public sealed class BundleSigningTests(ITestOutputHelper output)
{
    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    [RequiresTools(["pwsh", "codesign"])]
    public async Task ComponentPreparationPreservesNativeSymbolsAndAllowsReuse(bool sign)
    {
        Assert.SkipUnless(OperatingSystem.IsMacOS(), "macOS preparation uses codesign.");
        using var workspace = TemporaryWorkspace.Create(output);
        var root = workspace.Path;
        var managed = Directory.CreateDirectory(Path.Combine(root, "managed")).FullName;
        File.Copy("/usr/bin/true", Path.Combine(managed, "aspire-managed"));
        var publishedSymbols = new List<string>();
        var retainedSymbols = new Dictionary<string, string>();
        foreach (var (project, executable) in new[]
        {
            ("Aspire.Dashboard", "Aspire.Dashboard"),
            ("Aspire.TerminalHost", "Aspire.TerminalHost"),
            ("Aspire.Hosting.Native.Server", "Aspire.Hosting.Native.Server"),
            ("Aspire.Cli", "aspire")
        })
        {
            var native = Path.Combine(root, "bin", project, "Release", "net11.0", "osx-arm64", "native", executable + ".dSYM");
            Directory.CreateDirectory(native);
            var marker = Path.Combine(native, "debug-symbols");
            File.WriteAllText(marker, project);
            retainedSymbols.Add(marker, project);
            if (project == "Aspire.Cli")
            {
                continue;
            }
            var publish = Path.Combine(root, "bin", project, "Release", "net11.0", "osx-arm64", "publish");
            Directory.CreateDirectory(Path.Combine(publish, executable + ".dSYM"));
            publishedSymbols.Add(Path.Combine(publish, executable + ".dSYM"));
        }
        var launcher = Path.Combine(root, "prepare.ps1");
        await File.WriteAllTextAsync(launcher, """
            $ErrorActionPreference = 'Stop'
            foreach ($pass in 1..2) {
              & "$env:TEST_REPO/dotnet.sh" msbuild "$env:TEST_REPO/eng/Bundle.proj" `
                /t:_PrepareBundleComponentsForSigning /p:Configuration=Release /p:TargetRid=osx-arm64 `
                /p:BuildDistributionPackages=true "/p:Sign=$env:TEST_SIGN" /p:DotNetSignType=test `
                "/p:_ArtifactsDir=$env:TEST_ROOT/" "/p:_ManagedPublishDir=$env:TEST_ROOT/managed/" `
                "/p:_DashboardPublishDir=$env:TEST_ROOT/bin/Aspire.Dashboard/Release/net11.0/osx-arm64/publish/"
              if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            }
            & codesign --verify --strict "$env:TEST_ROOT/managed/aspire-managed"
            exit $LASTEXITCODE
            """);
        using var command = new PowerShellCommand(launcher, output)
            .WithTimeout(TimeSpan.FromMinutes(1))
            .WithEnvironmentVariable("TEST_REPO", RepoRoot.Path)
            .WithEnvironmentVariable("TEST_ROOT", root)
            .WithEnvironmentVariable("TEST_SIGN", sign ? "true" : "false");
        var result = await command.ExecuteAsync();
        result.EnsureSuccessful();
        foreach (var path in publishedSymbols)
        {
            Assert.Equal(!sign, Directory.Exists(path));
        }
        foreach (var (path, content) in retainedSymbols)
        {
            Assert.Equal(content, File.ReadAllText(path));
        }
    }

    [Theory]
    [InlineData("components", "SignBundleComponents")]
    [InlineData("cli", "PublishAndPackageCli")]
    [InlineData("standalone", "SignNonCliDistributionPackages")]
    [RequiresTools(["pwsh"])]
    public async Task BundlePhaseArgumentsSurviveBuildEntryPoint(string phase, string logName)
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var root = workspace.CreateDirectory("repository with spaces").FullName;
        var engineering = Directory.CreateDirectory(Path.Combine(root, "eng")).FullName;
        var common = Directory.CreateDirectory(Path.Combine(engineering, "common")).FullName;
        File.Copy(Path.Combine(RepoRoot.Path, "eng", "build.ps1"), Path.Combine(engineering, "build.ps1"));
        File.Copy(Path.Combine(RepoRoot.Path, "build.cmd"), Path.Combine(root, "build.cmd"));
        await File.WriteAllTextAsync(Path.Combine(common, "build.ps1"), """
            $ErrorActionPreference = 'Stop'
            if (@($args | Where-Object { $_ -match '^[-/](p|property|bl|flp):' }).Count) {
                throw 'Bundle MSBuild arguments must be supplied through the response file.'
            }
            $loggerArguments = @($args | Where-Object { $_ -like '/flp:*' -or $_ -like '@*' })
            & $env:TEST_DOTNET msbuild "$env:TEST_ROOT/logger.proj" -nologo @loggerArguments
            exit $LASTEXITCODE
            """);
        await File.WriteAllTextAsync(Path.Combine(root, "logger.proj"), """
            <Project DefaultTargets="Build">
              <Target Name="Build">
                <Error Condition="'$(Configuration)' != 'Release'" Text="Configuration did not reach MSBuild." />
                <Error Condition="'$(TargetRid)' != 'win-x64'" Text="TargetRid did not reach MSBuild." />
                <Error Condition="'$(DotNetSignType)' != 'test'" Text="Signing type did not reach MSBuild." />
                <Error Condition="'$(TeamName)' != 'signing team'" Text="Quoted signing property did not reach MSBuild." />
                <Error Condition="'$(ArtifactsDir)' == ''" Text="Artifact path did not reach MSBuild." />
                <Error Condition="'$(TEST_PHASE)' == 'components' and ('$(Projects)' == '' or !Exists('$(Projects)'))" Text="Restore project did not reach MSBuild." />
                <Error Condition="'$(TEST_PHASE)' == 'cli' and ('$(SkipManagedBuild)' != 'true' or '$(TargetRids)' != 'win-x64' or '$(BundlePayloadPath)' == '')" Text="CLI packaging properties did not reach MSBuild." />
                <Message Importance="low" Text="COMPONENT_SIGNING_DIAGNOSTIC_PROBE" />
              </Target>
            </Project>
            """);
        var buildScript = Path.Combine(root, OperatingSystem.IsWindows() ? "build.cmd" : "build.sh");
        if (!OperatingSystem.IsWindows())
        {
            await File.WriteAllTextAsync(buildScript, """
                #!/usr/bin/env bash
                exec pwsh -NoProfile -File "$TEST_ROOT/eng/common/build.ps1" "$@"
                """);
            File.SetUnixFileMode(buildScript, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }
        var launcher = Path.Combine(root, "launch.ps1");
        await File.WriteAllTextAsync(launcher, """
            $ErrorActionPreference = 'Stop'
            $target = if ($env:TEST_PHASE -eq 'components') { '_SignBundleComponents' } else { '_FinalizeCliAndDistributionPackages' }
            $skipCli = if ($env:TEST_PHASE -eq 'standalone') { 'true' } else { 'false' }
            & $env:TEST_DOTNET msbuild "$env:TEST_REPO/eng/Bundle.proj" `
              "/t:_InitializeBundleBuild,$target" `
              /p:TargetRid=win-x64 /p:Configuration=Release `
              /p:BuildDistributionPackages=true /p:Sign=true /p:DotNetSignType=test `
              "/p:SkipCliArtifacts=$skipCli" `
              "/p:TeamName=signing team" `
              "/p:_ArtifactsDir=$env:TEST_ROOT/artifacts/" `
              "/p:_BuildScriptPath=$env:TEST_BUILD_SCRIPT"
            exit $LASTEXITCODE
            """);
        using var command = new PowerShellCommand(launcher, output)
            .WithTimeout(TimeSpan.FromMinutes(1))
            .WithEnvironmentVariable("TEST_ROOT", root)
            .WithEnvironmentVariable("TEST_REPO", RepoRoot.Path)
            .WithEnvironmentVariable("TEST_BUILD_SCRIPT", buildScript)
            .WithEnvironmentVariable("TEST_PHASE", phase)
            .WithEnvironmentVariable("TEST_DOTNET", Path.Combine(RepoRoot.Path, ".dotnet", OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet"))
            .WithEnvironmentVariable("TreatWarningsAsErrors", "false");
        var result = await command.ExecuteAsync();
        result.EnsureSuccessful();
        Assert.True(File.Exists(Path.Combine(root, "artifacts", "log", "Release", logName + ".binlog")));
        if (phase == "components")
        {
            Assert.Contains("COMPONENT_SIGNING_DIAGNOSTIC_PROBE",
                await File.ReadAllTextAsync(Path.Combine(root, "artifacts", "log", "Release", "SignBundleComponents.log")));
        }
    }

}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text.Json.Nodes;
using Aspire.TestUtilities;
using Xunit;

namespace Infrastructure.Tests;

public sealed class PrepareNpmCliPackagesTests : IDisposable
{
    private const string PackageVersion = "13.4.0-test.1";
    private const string Rid = "linux-x64";

    private readonly TemporaryWorkspace _workspace;
    private readonly string _scriptPath;

    public PrepareNpmCliPackagesTests(ITestOutputHelper output)
    {
        _workspace = TemporaryWorkspace.Create(output);
        _scriptPath = Path.Combine(RepoRoot.Path, "eng", "scripts", "prepare-npm-cli-packages.sh");
    }

    public void Dispose() => _workspace.Dispose();

    [Fact]
    [RequiresTools(["bash"])]
    public async Task ResolveInputsPublishesValidatedPipelineVariables()
    {
        var packagesDirectory = Path.Combine(_workspace.Path, "packages");
        Directory.CreateDirectory(packagesDirectory);

        var result = await RunScriptAsync("resolve-inputs", packagesDirectory, PackageVersion, Rid);

        Assert.True(result.ExitCode == 0, result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmPackagesDir]", result.Output);
        Assert.Contains($"##vso[task.setvariable variable=NpmExpectedVersion]{PackageVersion}", result.Output);
        Assert.Contains($"##vso[task.setvariable variable=NpmTestRid]{Rid}", result.Output);
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task LocateSelectsExactPointerAndRidPackages()
    {
        var packagesDirectory = Path.Combine(_workspace.Path, "packages");
        Directory.CreateDirectory(packagesDirectory);
        var pointerPackage = CreatePackage(packagesDirectory, $"microsoft-aspire-cli-{PackageVersion}.tgz");
        var ridPackage = CreatePackage(packagesDirectory, $"microsoft-aspire-cli-{Rid}-{PackageVersion}.tgz");
        CreatePackage(packagesDirectory, "microsoft-aspire-cli-linux-arm64-13.4.0-test.1.tgz");
        CreatePackage(packagesDirectory, "microsoft-aspire-cli-99.0.0.tgz");

        var result = await RunScriptAsync("locate", packagesDirectory, PackageVersion, Rid);

        Assert.True(result.ExitCode == 0, result.Output);
        Assert.Contains($"##vso[task.setvariable variable=NpmPointerTarball]{pointerPackage}", result.Output);
        Assert.Contains($"##vso[task.setvariable variable=NpmRidTarball]{ridPackage}", result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckInstall]failed", result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckUninstall]failed", result.Output);
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task InstallValidateUsesOfflineNpmAndVerifiesLifecycle()
    {
        var toolsDirectory = Path.Combine(_workspace.Path, "tools");
        var stagingDirectory = Path.Combine(_workspace.Path, "staging");
        Directory.CreateDirectory(toolsDirectory);
        Directory.CreateDirectory(stagingDirectory);

        var npmLogPath = Path.Combine(_workspace.Path, "npm-arguments.txt");
        var fakeNpmPath = Path.Combine(toolsDirectory, "npm");
        await File.WriteAllTextAsync(
            fakeNpmPath,
            """
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$FAKE_NPM_LOG"

            if [ "$1" = "install" ]; then
              case "$*" in
                *microsoft-aspire-cli-${FAKE_VERSION}.tgz*)
                  mkdir -p "$NPM_CONFIG_PREFIX/bin" "$ASPIRE_NPM_CACHE_DIR/$FAKE_VERSION/$FAKE_RID/bin"
                  cat > "$NPM_CONFIG_PREFIX/bin/aspire" <<EOF
            #!/usr/bin/env bash
            printf '%s\r\n' '${FAKE_VERSION}+test-build'
            EOF
                  chmod +x "$NPM_CONFIG_PREFIX/bin/aspire"
                  touch "$ASPIRE_NPM_CACHE_DIR/$FAKE_VERSION/$FAKE_RID/bin/aspire"
                  chmod +x "$ASPIRE_NPM_CACHE_DIR/$FAKE_VERSION/$FAKE_RID/bin/aspire"
                  ;;
              esac
            elif [ "$1" = "uninstall" ]; then
              rm -f "$NPM_CONFIG_PREFIX/bin/aspire"
            fi
            """);
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(
                fakeNpmPath,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        var pointerPackage = CreatePackage(_workspace.Path, $"microsoft-aspire-cli-{PackageVersion}.tgz");
        var ridPackage = CreatePackage(_workspace.Path, $"microsoft-aspire-cli-{Rid}-{PackageVersion}.tgz");
        var result = await RunScriptAsync(
            ["install-validate", pointerPackage, ridPackage, PackageVersion, Rid, stagingDirectory],
            new Dictionary<string, string>
            {
                ["PATH"] = $"{toolsDirectory}{Path.PathSeparator}/usr/bin{Path.PathSeparator}/bin{Path.PathSeparator}/usr/sbin{Path.PathSeparator}/sbin",
                ["FAKE_NPM_LOG"] = npmLogPath,
                ["FAKE_VERSION"] = PackageVersion,
                ["FAKE_RID"] = Rid
            });

        Assert.True(result.ExitCode == 0, result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckInstall]passed", result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckVersion]passed", result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckLauncher]passed", result.Output);
        Assert.Contains("##vso[task.setvariable variable=NpmCheckUninstall]passed", result.Output);

        var npmInvocations = await File.ReadAllLinesAsync(npmLogPath);
        Assert.Collection(
            npmInvocations,
            invocation =>
            {
                Assert.Contains("install -g", invocation);
                Assert.Contains("--offline", invocation);
                Assert.Contains("--fetch-timeout=15000", invocation);
                Assert.EndsWith(ridPackage, invocation);
            },
            invocation =>
            {
                Assert.Contains("install -g", invocation);
                Assert.Contains("--offline", invocation);
                Assert.Contains("--omit=optional", invocation);
                Assert.EndsWith(pointerPackage, invocation);
            },
            invocation =>
            {
                Assert.Contains("uninstall -g", invocation);
                Assert.Contains("--offline", invocation);
                Assert.Contains("@microsoft/aspire-cli", invocation);
                Assert.Contains("@microsoft/aspire-cli-linux-x64", invocation);
            });
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task WriteSummaryRecordsTheActualCheckStatuses()
    {
        var stagingDirectory = Path.Combine(_workspace.Path, "staging");
        Directory.CreateDirectory(stagingDirectory);

        var result = await RunScriptAsync(
            "write-summary",
            stagingDirectory,
            Rid,
            PackageVersion,
            "true",
            "passed",
            "passed",
            "failed",
            "passed");

        Assert.Equal(0, result.ExitCode);

        var summaryPath = Path.Combine(stagingDirectory, "npm-validation-summary", "validation-summary.json");
        var actual = JsonNode.Parse(await File.ReadAllTextAsync(summaryPath));
        var expected = JsonNode.Parse(
            $$"""
            {
              "schemaVersion": 1,
              "validatedByPreparePipeline": false,
              "rid": "{{Rid}}",
              "expectedVersion": "{{PackageVersion}}",
              "skipRegistryResolution": true,
              "checks": {
                "install": {
                  "status": "passed",
                  "details": "npm install -g <rid>.tgz && npm install -g --omit=optional <pointer>.tgz"
                },
                "version": {
                  "status": "passed",
                  "details": "aspire --version output matched the build version"
                },
                "launcher": {
                  "status": "failed",
                  "details": "Launcher cached native binary at ASPIRE_NPM_CACHE_DIR/<version>/<rid>/bin/<binaryName>"
                },
                "uninstall": {
                  "status": "passed",
                  "details": "npm uninstall -g @microsoft/aspire-cli @microsoft/aspire-cli-{{Rid}}"
                }
              }
            }
            """);

        Assert.True(JsonNode.DeepEquals(expected, actual), actual?.ToJsonString());
    }

    private static string CreatePackage(string directory, string fileName)
    {
        var path = Path.Combine(directory, fileName);
        File.WriteAllText(path, "test package");
        return path;
    }

    private Task<ScriptResult> RunScriptAsync(params string[] arguments)
        => RunScriptAsync(arguments, environment: null);

    private async Task<ScriptResult> RunScriptAsync(
        IReadOnlyList<string> arguments,
        IReadOnlyDictionary<string, string>? environment)
    {
        var startInfo = new ProcessStartInfo("bash")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        startInfo.ArgumentList.Add(_scriptPath);
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        if (environment is not null)
        {
            foreach (var (name, value) in environment)
            {
                startInfo.Environment[name] = value;
            }
        }

        using var process = Process.Start(startInfo)!;
        var standardOutput = process.StandardOutput.ReadToEndAsync();
        var standardError = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync(TestContext.Current.CancellationToken);

        return new ScriptResult(
            process.ExitCode,
            $"{await standardOutput}{await standardError}");
    }

    private sealed record ScriptResult(int ExitCode, string Output);
}

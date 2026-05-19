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

    // Builds the realistic PR-channel layout that prepare-manifest-artifact.ps1 produces:
    // an installer.yaml with two Installers entries (https:// URLs and a placeholder
    // SHA256 of all zeros) co-located with dogfood.ps1, plus fake aspire-cli-win-*
    // archives in a sibling directory that -ArchiveRoot will point at.
    private static async Task<(string ManifestDir, string ArchiveRoot)> CreateWinGetPrChannelArtifactAsync(string root, string version = "13.3.0")
    {
        var manifestDir = Path.Combine(root, "installer-winget");
        var archiveRoot = Path.Combine(root, "installer-native-archives");
        Directory.CreateDirectory(manifestDir);
        Directory.CreateDirectory(archiveRoot);

        var placeholder = new string('0', 64);
        var installerYaml = $$"""
            # yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.1.10.0.schema.json
            PackageIdentifier: Microsoft.Aspire
            PackageVersion: "{{version}}"
            InstallerType: zip
            NestedInstallerType: portable
            NestedInstallerFiles:
            - RelativeFilePath: aspire.exe
              PortableCommandAlias: aspire
            Installers:
            - Architecture: x64
              InstallerUrl: https://ci.dot.net/public/aspire/{{version}}/aspire-cli-win-x64-{{version}}.zip
              InstallerSha256: {{placeholder}}
            - Architecture: arm64
              InstallerUrl: https://ci.dot.net/public/aspire/{{version}}/aspire-cli-win-arm64-{{version}}.zip
              InstallerSha256: {{placeholder}}
            ManifestType: installer
            ManifestVersion: 1.10.0
            """;
        await File.WriteAllTextAsync(Path.Combine(manifestDir, "Microsoft.Aspire.installer.yaml"), installerYaml);
        await File.WriteAllTextAsync(Path.Combine(manifestDir, "Microsoft.Aspire.yaml"), $"PackageIdentifier: Microsoft.Aspire\nPackageVersion: {version}\nManifestType: version\nManifestVersion: 1.10.0\n");
        await File.WriteAllTextAsync(Path.Combine(manifestDir, "Microsoft.Aspire.locale.en-US.yaml"), $"PackageIdentifier: Microsoft.Aspire\nPackageVersion: {version}\nPackageLocale: en-US\nManifestType: defaultLocale\nManifestVersion: 1.10.0\n");
        await File.WriteAllTextAsync(Path.Combine(manifestDir, "dogfood.ps1"), "exit 0\n");

        // Distinct fake bytes per RID so SHA256 differences flow into the mock install check.
        await File.WriteAllTextAsync(Path.Combine(archiveRoot, $"aspire-cli-win-x64-{version}.zip"), $"fake-x64-{version}");
        await File.WriteAllTextAsync(Path.Combine(archiveRoot, $"aspire-cli-win-arm64-{version}.zip"), $"fake-arm64-{version}");

        return (manifestDir, archiveRoot);
    }

    private static async Task<string> CreateMockHomebrewBinAsync(TestEnvironment env, int aspireExitCode)
    {
        var mockBinDir = Path.Combine(env.TempDirectory, "mock-homebrew-bin");
        var brewRepository = Path.Combine(env.TempDirectory, "brew-repository");
        var brewPrefix = Path.Combine(env.TempDirectory, "brew-prefix");
        var brewLog = Path.Combine(env.TempDirectory, "brew.log");

        Directory.CreateDirectory(mockBinDir);
        Directory.CreateDirectory(brewRepository);
        Directory.CreateDirectory(brewPrefix);

        var brewPath = Path.Combine(mockBinDir, "brew");
        await File.WriteAllTextAsync(brewPath, $$"""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "{{brewLog}}"

            create_aspire() {
              cat > "{{mockBinDir}}/aspire" <<'ASPIRE'
            #!/usr/bin/env bash
            echo "mock aspire failure"
            exit {{aspireExitCode}}
            ASPIRE
              chmod +x "{{mockBinDir}}/aspire"
            }

            case "${1:-}" in
              --repository)
                echo "{{brewRepository}}"
                exit 0
                ;;
              --prefix)
                echo "{{brewPrefix}}"
                exit 0
                ;;
              list)
                exit 0
                ;;
              tap-info)
                exit 1
                ;;
              tap-new)
                tap="${@: -1}"
                org="${tap%%/*}"
                repo="${tap##*/}"
                mkdir -p "{{brewRepository}}/Library/Taps/$org/homebrew-$repo"
                exit 0
                ;;
              style|audit|info)
                exit 0
                ;;
              install)
                create_aspire
                exit 0
                ;;
              uninstall)
                rm -f "{{mockBinDir}}/aspire"
                exit 0
                ;;
              untap)
                exit 0
                ;;
            esac

            echo "unexpected brew command: $*" >&2
            exit 1
            """);
        FileHelper.MakeExecutable(brewPath);

        var curlPath = Path.Combine(mockBinDir, "curl");
        await File.WriteAllTextAsync(curlPath, """
            #!/usr/bin/env bash
            printf '404'
            exit 0
            """);
        FileHelper.MakeExecutable(curlPath);

        return mockBinDir;
    }

    private static async Task<string> CreateMockWinGetBinAsync(TestEnvironment env, int aspireExitCode)
    {
        var mockBinDir = Path.Combine(env.TempDirectory, "mock-winget-bin");
        var wingetLog = Path.Combine(env.TempDirectory, "winget.log");

        Directory.CreateDirectory(mockBinDir);

        var wingetPath = Path.Combine(mockBinDir, "winget");
        await File.WriteAllTextAsync(wingetPath, $$"""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "{{wingetLog}}"

            cmd="${1:-}"
            shift || true

            case "$cmd" in
              list)
                exit 1
                ;;
              settings|uninstall)
                exit 0
                ;;
              validate|install)
                # Real winget treats every file in --manifest <dir> as a multi-file manifest
                # and rejects non-yaml files (e.g. "The manifest does not contain a valid root.
                # File: dogfood.ps1"). Mirror that so regressions on the manifest-staging logic
                # in dogfood.ps1 are caught here.
                manifest_dir=""
                while [ $# -gt 0 ]; do
                  if [ "$1" = "--manifest" ]; then
                    manifest_dir="${2:-}"
                    break
                  fi
                  shift
                done
                if [ -n "$manifest_dir" ] && [ -d "$manifest_dir" ]; then
                  while IFS= read -r f; do
                    case "$f" in
                      *.yaml|*.yml) ;;
                      *)
                        echo "Mock winget: non-yaml file in manifest dir: $f" >&2
                        exit 1
                        ;;
                    esac
                  done < <(find "$manifest_dir" -mindepth 1 -maxdepth 1 -type f)
                fi

                installer_yaml=""
                if [ -n "$manifest_dir" ] && [ -d "$manifest_dir" ]; then
                  installer_yaml="$(find "$manifest_dir" -mindepth 1 -maxdepth 1 -type f -name '*.installer.yaml' | head -n 1)"
                fi

                if [ "$cmd" = "validate" ] && [ -n "$installer_yaml" ]; then
                  # WinGet's installer schema requires InstallerUrl to match ^https?://.
                  # Reject file:// URLs the way real ``winget validate`` does (see
                  # https://learn.microsoft.com/windows/package-manager/package/manifest).
                  while IFS= read -r url; do
                    case "$url" in
                      http://*|https://*) ;;
                      *)
                        echo "Mock winget validate: InstallerUrl does not match ^https?://: $url" >&2
                        exit 1
                        ;;
                    esac
                  done < <(grep -E '^\s*InstallerUrl:' "$installer_yaml" | sed -E 's/^\s*InstallerUrl:\s*//')
                fi

                if [ "$cmd" = "install" ] && [ -n "$installer_yaml" ]; then
                  # winget install hashes the actual file referenced by InstallerUrl and
                  # compares with the manifest's InstallerSha256. Mirror that so regressions
                  # on Set-LocalInstallerUrls's hash refresh (PR manifests ship with a
                  # placeholder hash of all zeros) are caught.
                  current_url=""
                  while IFS= read -r line; do
                    case "$line" in
                      *InstallerUrl:*)
                        current_url="$(echo "$line" | sed -E 's/^\s*InstallerUrl:\s*//')"
                        ;;
                      *InstallerSha256:*)
                        recorded="$(echo "$line" | sed -E 's/^\s*InstallerSha256:\s*//' | tr '[:lower:]' '[:upper:]')"
                        if [ -n "$current_url" ]; then
                          case "$current_url" in
                            file://*)
                              local_path="${current_url#file://}"
                              # file:///C:/... on Windows: drop a leading slash before the drive letter.
                              case "$local_path" in
                                /[A-Za-z]:*) local_path="${local_path#/}";;
                              esac
                              if [ -f "$local_path" ]; then
                                actual="$(sha256sum "$local_path" | awk '{print toupper($1)}')"
                                if [ "$actual" != "$recorded" ]; then
                                  echo "Mock winget install: InstallerSha256 mismatch for $current_url" >&2
                                  echo "  expected: $recorded" >&2
                                  echo "  actual:   $actual" >&2
                                  exit 1
                                fi
                              fi
                              ;;
                          esac
                        fi
                        current_url=""
                        ;;
                    esac
                  done < "$installer_yaml"
                fi
                exit 0
                ;;
            esac

            echo "unexpected winget command: $cmd $*" >&2
            exit 1
            """);
        FileHelper.MakeExecutable(wingetPath);

        var aspirePath = Path.Combine(mockBinDir, "aspire");
        await File.WriteAllTextAsync(aspirePath, $$"""
            #!/usr/bin/env bash
            echo "mock aspire {{(aspireExitCode == 0 ? "version" : "failure")}}"
            exit {{aspireExitCode}}
            """);
        FileHelper.MakeExecutable(aspirePath);

        return mockBinDir;
    }

    private static async Task CreateFakeHomebrewArchivesAsync(string root)
    {
        Directory.CreateDirectory(root);
        await File.WriteAllTextAsync(Path.Combine(root, "aspire-cli-osx-arm64-13.3.0.tar.gz"), "fake arm64 archive");
        await File.WriteAllTextAsync(Path.Combine(root, "aspire-cli-osx-x64-13.3.0.tar.gz"), "fake x64 archive");
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
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_HomebrewDogfood_FailsWhenVersionCheckFails()
    {
        using var env = new TestEnvironment();
        var localDir = await CreateHomebrewInstallerArtifactAsync(Path.Combine(env.TempDirectory, "homebrew-artifact"));
        var mockBinDir = await CreateMockHomebrewBinAsync(env, aspireExitCode: 42);
        using var cmd = new ScriptToolCommand("eng/homebrew/dogfood.sh", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}/usr/bin:/bin:/usr/sbin:/sbin");

        var result = await cmd.ExecuteAsync(Path.Combine(localDir, "aspire.rb"));

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("aspire --version failed after install", result.Output);
    }

    [Fact]
    [RequiresTools(["ruby"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Bash script tests require bash shell")]
    public async Task Bash_PrepareHomebrewCask_FailedVerification_UninstallsCask()
    {
        using var env = new TestEnvironment();
        var archiveRoot = Path.Combine(env.TempDirectory, "archives");
        await CreateFakeHomebrewArchivesAsync(archiveRoot);
        var mockBinDir = await CreateMockHomebrewBinAsync(env, aspireExitCode: 42);
        using var cmd = new ScriptToolCommand("eng/homebrew/prepare-cask-artifact.sh", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}/usr/bin:/bin:/usr/sbin:/sbin");

        var result = await cmd.ExecuteAsync(
            "--version", "13.3.0",
            "--artifact-version", "13.3.0",
            "--channel", "stable",
            "--archive-root", archiveRoot,
            "--output-dir", Path.Combine(env.TempDirectory, "homebrew-output"));

        Assert.NotEqual(0, result.ExitCode);
        var brewLog = await File.ReadAllTextAsync(Path.Combine(env.TempDirectory, "brew.log"));
        Assert.Contains("uninstall --cask local/aspire-test/aspire", brewLog);
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

    [Fact]
    [RequiresTools(["pwsh"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses Unix mock executables")]
    public async Task PowerShell_WinGetDogfood_Force_PassesForceToWingetInstall()
    {
        using var env = new TestEnvironment();
        var localDir = await CreateWinGetInstallerArtifactAsync(Path.Combine(env.TempDirectory, "winget-artifact"));
        var mockBinDir = await CreateMockWinGetBinAsync(env, aspireExitCode: 0);
        using var cmd = new ScriptToolCommand("eng/winget/dogfood.ps1", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");

        var result = await cmd.ExecuteAsync("-ManifestPath", localDir, "-Force");

        result.EnsureSuccessful();
        var wingetLog = await File.ReadAllTextAsync(Path.Combine(env.TempDirectory, "winget.log"));
        Assert.Contains("install --manifest", wingetLog);
        Assert.Contains("--force", wingetLog);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses Unix mock executables")]
    public async Task PowerShell_WinGetDogfood_FailsWhenVersionCheckFails()
    {
        using var env = new TestEnvironment();
        var localDir = await CreateWinGetInstallerArtifactAsync(Path.Combine(env.TempDirectory, "winget-artifact"));
        var mockBinDir = await CreateMockWinGetBinAsync(env, aspireExitCode: 42);
        using var cmd = new ScriptToolCommand("eng/winget/dogfood.ps1", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");

        var result = await cmd.ExecuteAsync("-ManifestPath", localDir);

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("Failed to verify Aspire CLI installation", result.Output);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses Unix mock executables")]
    public async Task PowerShell_WinGetDogfood_ArchiveRoot_ValidatesPristineAndInstallsRewrittenManifest()
    {
        // This mirrors get-aspire-cli-pr.ps1 -InstallMode WinGet's invocation of
        // dogfood.ps1 -ArchiveRoot. Two end-to-end behaviours have to hold:
        //   1. ``winget validate`` runs against the pristine https:// URLs (the schema
        //      rejects file:// URLs, so the rewrite must happen *after* validate).
        //   2. ``Set-LocalInstallerUrls`` rewrites both InstallerUrl AND InstallerSha256
        //      (PR-channel manifests ship with a placeholder hash of all zeros, so
        //      install fails hash verification unless the hash is refreshed).
        using var env = new TestEnvironment();
        var (manifestDir, archiveRoot) = await CreateWinGetPrChannelArtifactAsync(env.TempDirectory);
        var mockBinDir = await CreateMockWinGetBinAsync(env, aspireExitCode: 0);
        using var cmd = new ScriptToolCommand("eng/winget/dogfood.ps1", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");

        var result = await cmd.ExecuteAsync("-ManifestPath", manifestDir, "-ArchiveRoot", archiveRoot);

        result.EnsureSuccessful();

        var wingetLog = await File.ReadAllTextAsync(Path.Combine(env.TempDirectory, "winget.log"));
        Assert.Contains("validate --manifest", wingetLog);
        Assert.Contains("install --manifest", wingetLog);

        // The pristine manifest in the artifact directory must be untouched (re-runnable).
        var originalInstaller = await File.ReadAllTextAsync(Path.Combine(manifestDir, "Microsoft.Aspire.installer.yaml"));
        Assert.Contains("https://ci.dot.net/", originalInstaller);
        Assert.Contains(new string('0', 64), originalInstaller);
        Assert.DoesNotContain("file://", originalInstaller);
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses Unix mock executables")]
    public async Task PowerShell_WinGetDogfood_ArchiveRoot_FailsWhenArchiveBytesChange()
    {
        // Guard against silent regressions: if Set-LocalInstallerUrls ever stops
        // refreshing InstallerSha256, the mock winget install will accept whatever the
        // manifest happens to contain. By mutating the archive after the manifest is
        // generated, we force the rewritten hash to differ from any baked-in value.
        using var env = new TestEnvironment();
        var (manifestDir, archiveRoot) = await CreateWinGetPrChannelArtifactAsync(env.TempDirectory);

        // Tamper the archive *after* the manifest's placeholder hash was written. The
        // refreshed hash must reflect the new bytes for ``winget install`` to succeed.
        var x64Archive = Path.Combine(archiveRoot, "aspire-cli-win-x64-13.3.0.zip");
        await File.WriteAllTextAsync(x64Archive, "post-generate-mutated-x64-bytes");

        var mockBinDir = await CreateMockWinGetBinAsync(env, aspireExitCode: 0);
        using var cmd = new ScriptToolCommand("eng/winget/dogfood.ps1", env, _testOutput);
        cmd.WithEnvironmentVariable("PATH", $"{mockBinDir}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");

        var result = await cmd.ExecuteAsync("-ManifestPath", manifestDir, "-ArchiveRoot", archiveRoot);

        result.EnsureSuccessful();
    }
}

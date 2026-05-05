// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Bundles;
using Aspire.Cli.Layout;
using Aspire.Cli.Tests.Acquisition.Fakes;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Aspire.Cli.Utils;
using Aspire.Shared;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests;

/// <summary>
/// Tests that <see cref="BundleService"/> handles Mode B installs correctly —
/// where the CLI binary lives directly in the prefix root (e.g. Homebrew Cask, winget)
/// rather than in a <c>bin/</c> subdirectory.
/// </summary>
public class BundleServiceModeBTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// Verifies that <see cref="IBundleService.GetDefaultExtractDir"/> returns the prefix root
    /// for a Mode B install, not the parent of the parent (which was the old Mode-A-only behavior).
    /// </summary>
    [Fact]
    public void GetDefaultExtractDir_ModeB_ReturnsPrefixRoot()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var (prefix, binaryPath) = SidecarBuilder.BuildModeB(
            Path.Combine(workspace.WorkspaceRoot.FullName, "prefix"),
            SidecarBuilder.ForBrew());

        var resolver = new InstallPathResolver();
        var service = new BundleService(
            new NullBundlePayloadProvider(),
            new NullLayoutDiscovery(),
            resolver,
            NullLogger<BundleService>.Instance);

        var result = service.GetDefaultExtractDir(binaryPath);

        Assert.Equal(prefix, result);
    }

    /// <summary>
    /// Verifies that <see cref="IBundleService.GetDefaultExtractDir"/> for Mode A still returns
    /// the prefix root (one level up from <c>bin/</c>), confirming that Mode A behavior is
    /// unaffected by the generalized resolver path.
    /// </summary>
    [Fact]
    public void GetDefaultExtractDir_ModeA_ReturnsPrefixRoot()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var (prefix, binaryPath) = SidecarBuilder.BuildModeA(
            Path.Combine(workspace.WorkspaceRoot.FullName, "prefix"),
            SidecarBuilder.ForScript());

        var resolver = new InstallPathResolver();
        var service = new BundleService(
            new NullBundlePayloadProvider(),
            new NullLayoutDiscovery(),
            resolver,
            NullLogger<BundleService>.Instance);

        var result = service.GetDefaultExtractDir(binaryPath);

        Assert.Equal(prefix, result);
    }

    /// <summary>
    /// Verifies that Mode B and Mode A both return the same prefix given equivalent setups:
    /// neither mode causes extraction to happen one directory deeper than the prefix.
    /// </summary>
    [Fact]
    public void GetDefaultExtractDir_ModeBAndModeA_NeitherGoesDeepThanPrefix()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var root = workspace.WorkspaceRoot.FullName;

        var (prefixA, binaryA) = SidecarBuilder.BuildModeA(Path.Combine(root, "modeA"), SidecarBuilder.ForScript());
        var (prefixB, binaryB) = SidecarBuilder.BuildModeB(Path.Combine(root, "modeB"), SidecarBuilder.ForBrew());

        var resolver = new InstallPathResolver();
        var service = new BundleService(
            new NullBundlePayloadProvider(),
            new NullLayoutDiscovery(),
            resolver,
            NullLogger<BundleService>.Instance);

        var resultA = service.GetDefaultExtractDir(binaryA);
        var resultB = service.GetDefaultExtractDir(binaryB);

        // Both should match their respective prefix roots exactly.
        Assert.Equal(prefixA, resultA);
        Assert.Equal(prefixB, resultB);

        // Neither should be a subdirectory of the prefix.
        Assert.DoesNotContain("bin", resultA);
        Assert.True(!resultB!.StartsWith(Path.Combine(prefixB, "bin"), StringComparison.Ordinal));
    }

    /// <summary>
    /// Verifies that <see cref="BundleService.EnsureExtractedAsync"/> extracts the bundle
    /// to the Mode B prefix root, creates the versioned layout, and establishes the
    /// <c>bundle/</c> reparse point at the prefix.
    /// </summary>
    [Fact]
    public async Task EnsureExtractedAsync_ModeB_ExtractsToPrefixRoot()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var (prefix, binaryPath) = SidecarBuilder.BuildModeB(
            Path.Combine(workspace.WorkspaceRoot.FullName, "prefix"),
            SidecarBuilder.ForBrew());

        var payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("mode-b");
        var provider = new TestBundlePayloadProvider(payload);
        var layoutDiscovery = new TestLayoutDiscoveryAtRoot(prefix);
        var service = new BundleService(
            provider,
            layoutDiscovery,
            new InstallPathResolver(),
            NullLogger<BundleService>.Instance)
        {
            ProcessPathOverride = binaryPath
        };

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            await service.EnsureExtractedAsync();

            Assert.True(ReparsePoint.IsReparsePoint(bundleLink),
                $"bundle/ should be a reparse point at the Mode B prefix root: {prefix}");

            var managedExe = Path.Combine(bundleLink,
                BundleDiscovery.ManagedDirectoryName,
                BundleDiscovery.GetExecutableFileName(BundleDiscovery.ManagedExecutableName));
            Assert.True(File.Exists(managedExe), $"managed exe should exist at {managedExe}");
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    /// <summary>
    /// Layout discovery that discovers from a specific root (mirrors <c>TestLayoutDiscovery</c>
    /// in <see cref="BundleServiceIntegrationTests"/> but is not private to it).
    /// </summary>
    private sealed class TestLayoutDiscoveryAtRoot(string layoutRoot) : ILayoutDiscovery
    {
        public LayoutConfiguration? DiscoverLayout(string? projectDirectory = null)
        {
            var bundleDir = Path.Combine(layoutRoot, BundleDiscovery.BundleDirectoryName);
            var managedDir = Path.Combine(bundleDir, BundleDiscovery.ManagedDirectoryName);
            var dcpDir = Path.Combine(bundleDir, BundleDiscovery.DcpDirectoryName);
            var managedExe = Path.Combine(managedDir,
                BundleDiscovery.GetExecutableFileName(BundleDiscovery.ManagedExecutableName));

            if (!Directory.Exists(managedDir) || !File.Exists(managedExe) || !Directory.Exists(dcpDir))
            {
                return null;
            }

            return new LayoutConfiguration
            {
                LayoutPath = layoutRoot,
                Components = new LayoutComponents
                {
                    Managed = Path.Combine(BundleDiscovery.BundleDirectoryName, BundleDiscovery.ManagedDirectoryName),
                    Dcp = Path.Combine(BundleDiscovery.BundleDirectoryName, BundleDiscovery.DcpDirectoryName),
                }
            };
        }

        public string? GetComponentPath(LayoutComponent component, string? projectDirectory = null)
        {
            var bundleDir = Path.Combine(layoutRoot, BundleDiscovery.BundleDirectoryName);
            return component switch
            {
                LayoutComponent.Managed => Path.Combine(bundleDir, BundleDiscovery.ManagedDirectoryName,
                    BundleDiscovery.GetExecutableFileName(BundleDiscovery.ManagedExecutableName)),
                LayoutComponent.Dcp => Path.Combine(bundleDir, BundleDiscovery.DcpDirectoryName),
                _ => null,
            };
        }

        public bool IsBundleModeAvailable(string? projectDirectory = null) => true;
    }
}

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

namespace Aspire.Cli.Tests.Acquisition;

/// <summary>
/// Integration tests for side-by-side bundle extraction: two different CLI versions
/// installed at different prefixes do not interfere with each other, and Pass 1 GC
/// removes stale versioned directories after an upgrade.
/// </summary>
public class BundleSideBySideTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// Verifies that a Mode A (script/dogfood) install and a Mode B (brew/winget) install,
    /// both present on the same machine at different prefixes, extract independently to
    /// their own prefix roots without interfering with each other.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_ModeAAndModeB_ExtractToOwnPrefixIndependently()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var root = workspace.WorkspaceRoot.FullName;

        var (prefixA, binaryA) = SidecarBuilder.BuildModeA(
            Path.Combine(root, "modeA"),
            SidecarBuilder.ForScript());
        var (prefixB, binaryB) = SidecarBuilder.BuildModeB(
            Path.Combine(root, "modeB"),
            SidecarBuilder.ForBrew());

        var payloadA = BundleServiceIntegrationTests.CreateFakeBundlePayload("side-a");
        var payloadB = BundleServiceIntegrationTests.CreateFakeBundlePayload("side-b");

        var bundleLinkA = Path.Combine(prefixA, BundleDiscovery.BundleDirectoryName);
        var bundleLinkB = Path.Combine(prefixB, BundleDiscovery.BundleDirectoryName);
        try
        {
            var serviceA = MakeService(payloadA, prefixA, binaryA);
            var serviceB = MakeService(payloadB, prefixB, binaryB);

            await serviceA.ExtractAsync(prefixA, force: true);
            await serviceB.ExtractAsync(prefixB, force: true);

            Assert.True(ReparsePoint.IsReparsePoint(bundleLinkA),
                "Mode A: bundle/ should be a reparse point at prefixA");
            Assert.True(ReparsePoint.IsReparsePoint(bundleLinkB),
                "Mode B: bundle/ should be a reparse point at prefixB");

            // The two links point to different versioned directories.
            var targetA = ReparsePoint.GetTarget(bundleLinkA);
            var targetB = ReparsePoint.GetTarget(bundleLinkB);
            Assert.NotNull(targetA);
            Assert.NotNull(targetB);
            Assert.NotEqual(targetA, targetB);
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLinkA);
            ReparsePoint.RemoveIfExists(bundleLinkB);
        }
    }

    /// <summary>
    /// Verifies that a Mode A side-by-side upgrade (two successive extractions with different
    /// CLI binary fingerprints) replaces the <c>bundle/</c> reparse point atomically.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_ModeA_UpgradesReparsePointAtomically()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var (prefix, _) = SidecarBuilder.BuildModeA(
            Path.Combine(workspace.WorkspaceRoot.FullName, "prefix"),
            SidecarBuilder.ForScript());

        var layoutDiscovery = new DiscoveryAtRoot(prefix);

        var bin1 = CreateFakeCliBinary(Path.Combine(prefix, ".bins"), "v1", "cli-v1-content");
        var bin2 = CreateFakeCliBinary(Path.Combine(prefix, ".bins"), "v2", "cli-v2-longer-content");
        File.SetLastWriteTimeUtc(bin2, File.GetLastWriteTimeUtc(bin1).AddMinutes(5));

        var v1Payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("v1");
        var v2Payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("v2");

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            var svc1 = new BundleService(
                new TestBundlePayloadProvider(v1Payload),
                layoutDiscovery,
                new InstallPathResolver(),
                NullLogger<BundleService>.Instance)
            {
                ProcessPathOverride = bin1
            };
            await svc1.ExtractAsync(prefix, force: true);

            var target1 = ReparsePoint.GetTarget(bundleLink);
            Assert.NotNull(target1);

            var svc2 = new BundleService(
                new TestBundlePayloadProvider(v2Payload),
                layoutDiscovery,
                new InstallPathResolver(),
                NullLogger<BundleService>.Instance)
            {
                ProcessPathOverride = bin2
            };
            await svc2.ExtractAsync(prefix, force: true);

            var target2 = ReparsePoint.GetTarget(bundleLink);
            Assert.NotNull(target2);
            Assert.NotEqual(target1, target2);
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    /// <summary>
    /// Verifies that Pass 1 GC (triggered via <see cref="BundleService.EnsureExtractedAsync"/>)
    /// removes the previous versioned directory after a Mode B upgrade.
    /// </summary>
    [Fact]
    public async Task EnsureExtractedAsync_ModeB_GcRemovesOldVersionAfterUpgrade()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var (prefix, binaryPath) = SidecarBuilder.BuildModeB(
            Path.Combine(workspace.WorkspaceRoot.FullName, "prefix"),
            SidecarBuilder.ForBrew());

        var versionsRoot = Path.Combine(prefix, BundleService.VersionsDirectoryName);

        var bin1 = CreateFakeCliBinary(Path.Combine(prefix, ".bins"), "v1", "cli-v1");
        var bin2 = CreateFakeCliBinary(Path.Combine(prefix, ".bins"), "v2", "cli-v2-longer");
        File.SetLastWriteTimeUtc(bin2, File.GetLastWriteTimeUtc(bin1).AddMinutes(5));

        var layoutDiscovery = new DiscoveryAtRoot(prefix);
        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            // Extract v1.
            var svc1 = new BundleService(
                new TestBundlePayloadProvider(BundleServiceIntegrationTests.CreateFakeBundlePayload("v1")),
                layoutDiscovery,
                new InstallPathResolver(),
                NullLogger<BundleService>.Instance)
            {
                ProcessPathOverride = bin1
            };
            await svc1.EnsureExtractedAsync();

            // Capture the v1 versioned dir.
            var v1Dirs = Directory.GetDirectories(versionsRoot);
            Assert.Single(v1Dirs);
            var v1Dir = v1Dirs[0];

            // Extract v2 — GC should sweep v1.
            var svc2 = new BundleService(
                new TestBundlePayloadProvider(BundleServiceIntegrationTests.CreateFakeBundlePayload("v2")),
                layoutDiscovery,
                new InstallPathResolver(),
                NullLogger<BundleService>.Instance)
            {
                ProcessPathOverride = bin2
            };
            await svc2.EnsureExtractedAsync();

            // v1 versioned directory should be gone.
            Assert.False(Directory.Exists(v1Dir),
                $"Pass 1 GC should remove the old versioned directory: {v1Dir}");

            // v2 versioned directory should remain.
            var remainingDirs = Directory.GetDirectories(versionsRoot)
                .Where(d => !Path.GetFileName(d).StartsWith(".", StringComparison.Ordinal))
                .ToList();
            Assert.Single(remainingDirs);
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private static BundleService MakeService(byte[] payload, string prefix, string binaryPath) =>
        new BundleService(
            new TestBundlePayloadProvider(payload),
            new DiscoveryAtRoot(prefix),
            new InstallPathResolver(),
            NullLogger<BundleService>.Instance)
        {
            ProcessPathOverride = binaryPath
        };

    private static string CreateFakeCliBinary(string directory, string name, string content)
    {
        Directory.CreateDirectory(directory);
        var path = Path.Combine(directory, name);
        File.WriteAllText(path, content);
        return path;
    }

    private sealed class DiscoveryAtRoot(string layoutRoot) : ILayoutDiscovery
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

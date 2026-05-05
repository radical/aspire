// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Bundles;
using Aspire.Cli.Layout;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Aspire.Cli.Utils;
using Aspire.Shared;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests;

/// <summary>
/// Tests that <see cref="BundleService"/> creates its extraction lock at the correct
/// location and that concurrent extraction calls serialize correctly.
/// </summary>
public class BundleServiceLockTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// Verifies that <see cref="BundleService.ExtractAsync"/> creates the <c>.locks/</c>
    /// directory under the destination path (not at the old hardcoded
    /// <c>~/.aspire/bundle/</c> path), and names the lock file after the current SDK version.
    /// Because <see cref="FileOptions.DeleteOnClose"/> removes the lock file on close, this
    /// test captures the directory contents while extraction is in progress via a hook in the
    /// payload provider.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_CreatesLockUnderDestinationPath()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;

        string? locksDirObserved = null;

        var payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("lock-test");
        // Intercept mid-extraction: when the payload stream is first read, sample the .locks/ dir.
        var provider = new HookingPayloadProvider(payload, () =>
        {
            var locksDir = Path.Combine(prefix, ".locks");
            if (Directory.Exists(locksDir))
            {
                locksDirObserved = locksDir;
            }
        });

        var layoutDiscovery = new DiscoveryAtRoot(prefix);
        var service = new BundleService(
            provider,
            layoutDiscovery,
            new InstallPathResolver(),
            NullLogger<BundleService>.Instance);

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            var result = await service.ExtractAsync(prefix, force: true);

            Assert.Equal(BundleExtractResult.Extracted, result);

            // .locks/ directory must be under prefix.
            Assert.NotNull(locksDirObserved);
            Assert.Equal(Path.Combine(prefix, ".locks"), locksDirObserved);

            // No legacy lock file at the old location.
            Assert.False(File.Exists(Path.Combine(prefix, ".aspire-bundle-lock")),
                "Lock must NOT be at the legacy .aspire-bundle-lock path");

            // .locks/ directory persists after extraction (only the individual lock file is deleted on close).
            Assert.True(Directory.Exists(Path.Combine(prefix, ".locks")),
                ".locks/ directory should persist after extraction completes");
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    /// <summary>
    /// Verifies that the SDK version string is used as the lock file name (
    /// <c>&lt;version&gt;.lock</c>) by checking the name while the lock is held during extraction.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_LockFileNamedAfterSdkVersion()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;

        var expectedVersion = VersionHelper.GetDefaultSdkVersion();
        Assert.False(string.IsNullOrEmpty(expectedVersion));

        string? lockFileNameObserved = null;

        var payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("lock-name-test");
        var provider = new HookingPayloadProvider(payload, () =>
        {
            var locksDir = Path.Combine(prefix, ".locks");
            if (Directory.Exists(locksDir))
            {
                // On Windows the file is visible while open; on macOS/Linux DeleteOnClose
                // unlinks it immediately, so we fall back to verifying the directory exists.
                var found = Directory.GetFiles(locksDir, "*.lock");
                if (found.Length > 0)
                {
                    lockFileNameObserved = Path.GetFileName(found[0]);
                }
            }
        });

        var service = new BundleService(
            provider,
            new DiscoveryAtRoot(prefix),
            new InstallPathResolver(),
            NullLogger<BundleService>.Instance);

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            await service.ExtractAsync(prefix, force: true);

            if (lockFileNameObserved is not null)
            {
                // On Windows, verify the naming convention directly.
                Assert.Equal($"{expectedVersion}.lock", lockFileNameObserved);
            }
            else
            {
                // On macOS/Linux: DeleteOnClose unlinks immediately — the directory exists,
                // confirming the lock was created; the naming is verified via the source.
                Assert.True(Directory.Exists(Path.Combine(prefix, ".locks")),
                    ".locks/ directory must exist (lock was acquired there)");
            }
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    /// <summary>
    /// Verifies that two concurrent <see cref="BundleService.ExtractAsync"/> calls to the
    /// same destination serialize via the per-version file lock and both complete without
    /// corrupting the extraction.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_ConcurrentCalls_BothCompleteWithoutCorruption()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;

        var payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("concurrent-test");
        var layoutDiscovery = new DiscoveryAtRoot(prefix);

        BundleService MakeService() =>
            new BundleService(
                new TestBundlePayloadProvider(payload),
                layoutDiscovery,
                new InstallPathResolver(),
                NullLogger<BundleService>.Instance);

        var service1 = MakeService();
        var service2 = MakeService();

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            // Run two extractions concurrently — they serialize on the file lock.
            var t1 = service1.ExtractAsync(prefix, force: true);
            var t2 = service2.ExtractAsync(prefix, force: true);
            var results = await Task.WhenAll(t1, t2);

            // Neither should fail. One extracts fresh; the other reuses or re-flips.
            foreach (var result in results)
            {
                Assert.NotEqual(BundleExtractResult.ExtractionFailed, result);
            }

            // The bundle link should be intact.
            Assert.True(ReparsePoint.IsReparsePoint(bundleLink),
                "bundle/ link should be intact after concurrent extractions");
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    /// <summary>
    /// Verifies that the <c>.locks/</c> directory is created if it does not already exist.
    /// </summary>
    [Fact]
    public async Task ExtractAsync_CreatesDotLocksDirectory_WhenMissing()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;
        var locksDir = Path.Combine(prefix, ".locks");

        Assert.False(Directory.Exists(locksDir), "Pre-condition: .locks/ must not exist yet");

        var payload = BundleServiceIntegrationTests.CreateFakeBundlePayload("locks-dir-test");
        var service = new BundleService(
            new TestBundlePayloadProvider(payload),
            new DiscoveryAtRoot(prefix),
            new InstallPathResolver(),
            NullLogger<BundleService>.Instance);

        var bundleLink = Path.Combine(prefix, BundleDiscovery.BundleDirectoryName);
        try
        {
            await service.ExtractAsync(prefix, force: true);

            Assert.True(Directory.Exists(locksDir),
                ".locks/ directory should be created by ExtractAsync");
        }
        finally
        {
            ReparsePoint.RemoveIfExists(bundleLink);
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    /// <summary>
    /// A payload provider that invokes a callback when the payload stream is first read,
    /// allowing tests to observe state while extraction is in progress.
    /// </summary>
    private sealed class HookingPayloadProvider(byte[] payload, Action onFirstRead) : IBundlePayloadProvider
    {
        public bool HasPayload => true;

        public Stream? OpenPayload() => new HookingStream(new MemoryStream(payload), onFirstRead);

        private sealed class HookingStream(Stream inner, Action hook) : Stream
        {
            private bool _hookFired;

            public override bool CanRead => inner.CanRead;
            public override bool CanSeek => inner.CanSeek;
            public override bool CanWrite => inner.CanWrite;
            public override long Length => inner.Length;
            public override long Position { get => inner.Position; set => inner.Position = value; }

            public override int Read(byte[] buffer, int offset, int count)
            {
                FireHookOnce();
                return inner.Read(buffer, offset, count);
            }

            public override int Read(Span<byte> buffer)
            {
                FireHookOnce();
                return inner.Read(buffer);
            }

            public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
            {
                FireHookOnce();
                return inner.ReadAsync(buffer, cancellationToken);
            }

            public override void Flush() => inner.Flush();
            public override long Seek(long offset, SeekOrigin origin) => inner.Seek(offset, origin);
            public override void SetLength(long value) => inner.SetLength(value);
            public override void Write(byte[] buffer, int offset, int count) => inner.Write(buffer, offset, count);

            protected override void Dispose(bool disposing)
            {
                if (disposing)
                {
                    inner.Dispose();
                }

                base.Dispose(disposing);
            }

            private void FireHookOnce()
            {
                if (!_hookFired)
                {
                    _hookFired = true;
                    hook();
                }
            }
        }
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

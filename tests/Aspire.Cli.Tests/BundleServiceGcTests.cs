// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Bundles;
using Aspire.Cli.Tests.Utils;

namespace Aspire.Cli.Tests;

/// <summary>
/// Tests for the Pass 1 GC logic in <see cref="BundleService.TryCleanupStaleVersions"/>.
/// </summary>
public class BundleServiceGcTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// Verifies that a stale version directory is deleted when a different version is active.
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_DeletesStaleVersion()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");
        Directory.CreateDirectory(versionsRoot);

        // Create a stale version directory with a marker file.
        var staleDir = Path.Combine(versionsRoot, "13.4.0-abc123");
        Directory.CreateDirectory(staleDir);
        File.WriteAllText(Path.Combine(staleDir, "marker"), "stale");

        BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: "13.5.0-def456");

        Assert.False(Directory.Exists(staleDir),
            "Stale version directory should be deleted by Pass 1 GC");
    }

    /// <summary>
    /// Verifies that the active version directory is NOT deleted.
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_RetainsActiveVersion()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");
        Directory.CreateDirectory(versionsRoot);

        var activeId = "13.5.0-def456";
        var activeDir = Path.Combine(versionsRoot, activeId);
        Directory.CreateDirectory(activeDir);
        File.WriteAllText(Path.Combine(activeDir, "marker"), "active");

        BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: activeId);

        Assert.True(Directory.Exists(activeDir),
            "Active version directory must be retained by Pass 1 GC");
    }

    /// <summary>
    /// Verifies that multiple stale versions are all removed while the active version is kept.
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_MultipleStale_DeletesAllStale_KeepsActive()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");
        Directory.CreateDirectory(versionsRoot);

        string[] staleIds = ["13.3.0-aaa", "13.4.0-bbb", "13.4.1-ccc"];
        var activeId = "13.5.0-ddd";

        foreach (var id in staleIds)
        {
            var d = Path.Combine(versionsRoot, id);
            Directory.CreateDirectory(d);
            File.WriteAllText(Path.Combine(d, "file"), id);
        }

        var activeDir = Path.Combine(versionsRoot, activeId);
        Directory.CreateDirectory(activeDir);
        File.WriteAllText(Path.Combine(activeDir, "file"), activeId);

        BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: activeId);

        foreach (var id in staleIds)
        {
            Assert.False(Directory.Exists(Path.Combine(versionsRoot, id)),
                $"Stale version '{id}' should have been deleted");
        }

        Assert.True(Directory.Exists(activeDir), "Active version must be retained");
    }

    /// <summary>
    /// Verifies that <see cref="BundleService.TryCleanupStaleVersions"/> is a no-op when the
    /// <c>versions/</c> directory does not exist (e.g. first-time extraction).
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_MissingVersionsRoot_DoesNotThrow()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");

        // versions/ does not exist — must not throw.
        var exception = Record.Exception(() =>
            BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: "13.5.0-abc"));

        Assert.Null(exception);
    }

    /// <summary>
    /// Verifies that when a stale directory is locked (can't be deleted), GC renames it
    /// with an <c>.old.&lt;tick&gt;</c> suffix instead of throwing.
    /// On non-Windows platforms deleting open directories is permitted by the OS, so this
    /// scenario only applies to Windows.
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_LockedDirectory_RenamesInstead()
    {
        if (!OperatingSystem.IsWindows())
        {
            // On macOS/Linux, the OS permits deletion of directories that have open file
            // handles inside them, so the rename fallback is never triggered.
            return;
        }

        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");
        Directory.CreateDirectory(versionsRoot);

        var staleId = "13.4.0-locked";
        var staleDir = Path.Combine(versionsRoot, staleId);
        Directory.CreateDirectory(staleDir);
        var lockedFile = Path.Combine(staleDir, "locked.dll");

        // Hold an exclusive file handle to prevent deletion.
        using var handle = new FileStream(lockedFile, FileMode.Create, FileAccess.ReadWrite, FileShare.None);

        // Must not throw.
        var exception = Record.Exception(() =>
            BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: "13.5.0-ok"));

        Assert.Null(exception);

        // The directory should have been renamed to <staleDir>.old.<tick>.
        var renamedEntries = Directory.GetDirectories(versionsRoot)
            .Select(Path.GetFileName)
            .Where(name => name is not null && name.StartsWith(staleId + ".old.", StringComparison.Ordinal))
            .ToList();
        Assert.NotEmpty(renamedEntries);
    }

    /// <summary>
    /// Verifies that <see cref="BundleService.TryCleanupStaleVersions"/> is a no-op when
    /// the <c>versions/</c> directory is empty.
    /// </summary>
    [Fact]
    public void TryCleanupStaleVersions_EmptyVersionsRoot_DoesNotThrow()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var versionsRoot = Path.Combine(workspace.WorkspaceRoot.FullName, "versions");
        Directory.CreateDirectory(versionsRoot);

        var exception = Record.Exception(() =>
            BundleService.TryCleanupStaleVersions(versionsRoot, activeVersionId: "13.5.0-abc"));

        Assert.Null(exception);
        Assert.Empty(Directory.GetDirectories(versionsRoot));
    }
}

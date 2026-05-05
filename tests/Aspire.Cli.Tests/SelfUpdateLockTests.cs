// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Tests.Utils;
using Aspire.Cli.Utils;

namespace Aspire.Cli.Tests;

/// <summary>
/// Tests the self-update lock behavior exercised by
/// <c>UpdateCommand.ExtractAndUpdateAsync</c>: a <see cref="FileLock"/> at
/// <c>&lt;prefix&gt;/.locks/self-update.lock</c> prevents two concurrent
/// <c>aspire update --self</c> invocations from racing over the binary rename.
/// </summary>
/// <remarks>
/// Because <c>ExtractAndUpdateAsync</c> is private and requires a real CLI
/// archive, we test the underlying <see cref="FileLock"/> primitive directly.
/// The path convention is verified by checking the lock file is created in the
/// expected directory; the mutual-exclusion contract is verified by attempting a
/// second acquisition while the first is held.
/// </remarks>
public class SelfUpdateLockTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// Two concurrent <see cref="FileLock.AcquireAsync"/> calls on the same
    /// <c>self-update.lock</c> path serialize: the second call times out while
    /// the first lock is held, proving mutual exclusion.
    /// </summary>
    [Fact]
    public async Task SelfUpdateLock_ConcurrentAcquisitions_SerializeCorrectly()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;
        var locksDir = Path.Combine(prefix, ".locks");
        Directory.CreateDirectory(locksDir);
        var lockPath = Path.Combine(locksDir, "self-update.lock");

        // Acquire the first lock — simulates the first 'aspire update --self'.
        using var lock1 = await FileLock.AcquireAsync(lockPath);

        // While lock1 is held, a second concurrent acquisition must time out.
        var shortTimeout = TimeSpan.FromMilliseconds(200);
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(5));

        await Assert.ThrowsAsync<TimeoutException>(
            () => FileLock.AcquireAsync(lockPath, cts.Token, timeout: shortTimeout));
    }

    /// <summary>
    /// After the first lock is disposed, a subsequent <see cref="FileLock.AcquireAsync"/>
    /// call succeeds, confirming the lock is properly released.
    /// </summary>
    [Fact]
    public async Task SelfUpdateLock_SecondAcquisitionSucceeds_AfterFirstReleased()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;
        var locksDir = Path.Combine(prefix, ".locks");
        Directory.CreateDirectory(locksDir);
        var lockPath = Path.Combine(locksDir, "self-update.lock");

        FileLock lock1 = await FileLock.AcquireAsync(lockPath);
        lock1.Dispose(); // release

        // Should succeed immediately after the first lock is released.
        using var lock2 = await FileLock.AcquireAsync(lockPath);
        Assert.NotNull(lock2);
    }

    /// <summary>
    /// <see cref="FileLock.AcquireAsync"/> creates the <c>.locks/</c> directory
    /// when it does not yet exist — matching the <c>Directory.CreateDirectory</c>
    /// call inside <c>ExtractAndUpdateAsync</c>.
    /// </summary>
    [Fact]
    public async Task SelfUpdateLock_CreatesDotLocksDirectory_WhenMissing()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;
        // Do NOT pre-create .locks/ — verify FileLock creates it.
        var locksDir = Path.Combine(prefix, ".locks");
        var lockPath = Path.Combine(locksDir, "self-update.lock");

        Assert.False(Directory.Exists(locksDir), "Pre-condition: .locks/ must not exist");

        using var fileLock = await FileLock.AcquireAsync(lockPath);

        Assert.True(Directory.Exists(locksDir),
            ".locks/ directory should be created by FileLock.AcquireAsync");
    }

    /// <summary>
    /// Acquiring the lock is cancellable: if the <see cref="CancellationToken"/> is
    /// cancelled while waiting for a contended lock, <see cref="OperationCanceledException"/>
    /// is thrown rather than blocking indefinitely.
    /// </summary>
    [Fact]
    public async Task SelfUpdateLock_Cancellation_ThrowsOperationCanceledException()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prefix = workspace.WorkspaceRoot.FullName;
        var locksDir = Path.Combine(prefix, ".locks");
        Directory.CreateDirectory(locksDir);
        var lockPath = Path.Combine(locksDir, "self-update.lock");

        // Hold the lock to create contention.
        using var lock1 = await FileLock.AcquireAsync(lockPath);

        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => FileLock.AcquireAsync(lockPath, cts.Token, timeout: TimeSpan.FromMinutes(5)));
    }
}

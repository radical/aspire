// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Commands;
using Aspire.Cli.Tests.Utils;
using Aspire.Cli.Uninstall;
using Microsoft.AspNetCore.InternalTesting;
using Microsoft.Extensions.DependencyInjection;

namespace Aspire.Cli.Tests.Uninstall;

public class CliCleanupServiceTests(ITestOutputHelper outputHelper)
{
    [Theory]
    [InlineData("stable")]
    [InlineData("staging")]
    [InlineData("daily")]
    [InlineData("pr-123")]
    [InlineData("local")]
    [InlineData("custom")]
    [InlineData("A0_b-c.1")]
    public void IsValidHiveName_AcceptsSafeNames(string name)
    {
        Assert.True(CliCleanupService.IsValidHiveName(name));
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("..")]
    [InlineData("../escape")]
    [InlineData("foo/bar")]      // forward slash separator
    [InlineData("foo\\bar")]     // backslash separator
    [InlineData("/abs")]         // absolute path (Unix)
    [InlineData(".hidden")]      // leading dot
    [InlineData("name with space")]
    [InlineData("name\twith\ttabs")]
    [InlineData("foo/../escape")]
    [InlineData("foo..bar")]     // contains ".." anywhere
    public void IsValidHiveName_RejectsUnsafeNames(string? name)
    {
        Assert.False(CliCleanupService.IsValidHiveName(name));
    }

    [Fact]
    public void Uninstall_PathTraversalChannel_IsRejectedBeforeAnyDelete()
    {
        // Verify the validator stops the destructive path; if it didn't,
        // ~/.aspire/hives/../../escape would normalize outside HivesDirectory
        // and the recursive Directory.Delete would target an arbitrary
        // parent path.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var aspireHome = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire");
        var escape = Path.Combine(workspace.WorkspaceRoot.FullName, "escape");
        Directory.CreateDirectory(Path.Combine(aspireHome, "hives"));
        Directory.CreateDirectory(escape);

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper);
        using var provider = services.BuildServiceProvider();

        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("uninstall --channel ../../escape --yes");

        // Validator failure produces a parse error and a non-success exit
        // code without ever invoking the destructive code path.
        Assert.NotEmpty(result.Errors);
        Assert.Contains(result.Errors, e => e.Message.Contains("path separators", StringComparison.Ordinal));
        Assert.True(Directory.Exists(escape), "Validator must reject the path-traversal channel before any deletion runs.");
    }

    [Fact]
    public async Task HivesDelete_PathTraversalName_IsRejectedBeforeAnyDelete()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var aspireHome = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire");
        var escape = Path.Combine(workspace.WorkspaceRoot.FullName, "escape");
        Directory.CreateDirectory(Path.Combine(aspireHome, "hives"));
        Directory.CreateDirectory(escape);

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper);
        using var provider = services.BuildServiceProvider();

        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("hives delete ../../escape --yes");
        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(CliExitCodes.InvalidCommand, exitCode);
        Assert.True(Directory.Exists(escape), "Hive delete must reject the path-traversal name before any deletion runs.");
    }

    [Fact]
    public void DeleteFileSystemInfoUnlessRunningFromTarget_RunningInsideTarget_ReturnsFailedWithSafetyMessage()
    {
        // Regression net for the "running CLI inside the deletion target"
        // safety. If currentProcessPath resolves to a path under target,
        // the helper must refuse to delete and surface a Failed operation —
        // otherwise running `aspire uninstall` from inside the dogfood
        // install would attempt to recursively delete its own directory tree.
        using var temp = new TestTempDirectory();
        var target = new DirectoryInfo(Path.Combine(temp.Path, "dogfood", "pr-999"));
        target.Create();
        Directory.CreateDirectory(Path.Combine(target.FullName, "bin"));
        var fakeProcessInsideTarget = Path.Combine(target.FullName, "bin", "aspire");
        File.WriteAllText(fakeProcessInsideTarget, string.Empty);

        var op = CliCleanupService.DeleteFileSystemInfoUnlessRunningFromTarget(
            target,
            currentProcessPath: fakeProcessInsideTarget,
            dryRun: false);

        Assert.Equal(CleanupOperationStatus.Failed, op.Status);
        Assert.Contains("running CLI is inside this target", op.Reason, StringComparison.Ordinal);
        Assert.True(target.Exists, "Helper must NOT delete the target when the running CLI is inside it.");
    }

    [Fact]
    public void DeleteFileSystemInfoUnlessRunningFromTarget_RunningOutsideTarget_DeletesDirectory()
    {
        using var temp = new TestTempDirectory();
        var target = new DirectoryInfo(Path.Combine(temp.Path, "hives", "pr-999"));
        target.Create();
        File.WriteAllText(Path.Combine(target.FullName, "marker"), string.Empty);
        var outsidePath = Path.Combine(temp.Path, "elsewhere", "aspire");
        Directory.CreateDirectory(Path.GetDirectoryName(outsidePath)!);
        File.WriteAllText(outsidePath, string.Empty);

        var op = CliCleanupService.DeleteFileSystemInfoUnlessRunningFromTarget(
            target,
            currentProcessPath: outsidePath,
            dryRun: false);

        Assert.Equal(CleanupOperationStatus.Removed, op.Status);
        Assert.False(Directory.Exists(target.FullName));
    }

    [Fact]
    public void DeleteFileSystemInfoUnlessRunningFromTarget_DryRun_DoesNotDelete()
    {
        using var temp = new TestTempDirectory();
        var target = new DirectoryInfo(Path.Combine(temp.Path, "hives", "pr-999"));
        target.Create();

        var op = CliCleanupService.DeleteFileSystemInfoUnlessRunningFromTarget(
            target,
            currentProcessPath: null,
            dryRun: true);

        Assert.Equal(CleanupOperationStatus.WouldRemove, op.Status);
        Assert.True(target.Exists);
    }
}

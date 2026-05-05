// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Commands;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Microsoft.AspNetCore.InternalTesting;
using Microsoft.Extensions.DependencyInjection;

namespace Aspire.Cli.Tests.Acquisition;

/// <summary>
/// Regression tests verifying that <c>update --self</c> respects the acquisition route
/// stored in <see cref="CliExecutionContext"/> at startup.  These tests exercise every
/// non-Script route to confirm they all refuse in-process self-update and either
/// print the stored update-command hint or the generic "unknown route" error.
/// </summary>
public class UpdateCommandSelfUpdateRouteTests(ITestOutputHelper outputHelper)
{
    // ──────────────────────────────────────────────────────────────
    // Routes that have a known update command (refuse + print hint)
    // ──────────────────────────────────────────────────────────────

    [Theory]
    [InlineData(InstallRoute.Brew, "brew upgrade aspire")]
    [InlineData(InstallRoute.Winget, "winget upgrade Aspire.CLI")]
    [InlineData(InstallRoute.Pr, "sidecar --update")]
    internal async Task SelfUpdate_WhenRouteHasSidecarCommand_RefusesAndPrintsCommand(
        InstallRoute route, string updateCommand)
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interactionService = new TestInteractionService();

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.InteractionServiceFactory = _ => interactionService;
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = route;
                ctx.UpdateCommand = updateCommand;
                return ctx;
            };
        });

        using var provider = services.BuildServiceProvider();
        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("update --self");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(ExitCodeConstants.InvalidCommand, exitCode);
        Assert.Contains(interactionService.DisplayedPlainText,
            text => text.Contains(updateCommand, StringComparison.Ordinal));
    }

    [Theory]
    [InlineData(InstallRoute.Brew, "brew upgrade aspire")]
    [InlineData(InstallRoute.Winget, "winget upgrade Aspire.CLI")]
    [InlineData(InstallRoute.Pr, "sidecar --update")]
    internal async Task SelfUpdate_WhenRouteHasSidecarCommand_DisplaysRouteLabel(
        InstallRoute route, string updateCommand)
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interactionService = new TestInteractionService();

        var expectedLabel = route switch
        {
            InstallRoute.Brew => "brew",
            InstallRoute.Winget => "winget",
            InstallRoute.Pr => "pr",
            _ => route.ToString().ToLowerInvariant(),
        };

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.InteractionServiceFactory = _ => interactionService;
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = route;
                ctx.UpdateCommand = updateCommand;
                return ctx;
            };
        });

        using var provider = services.BuildServiceProvider();
        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("update --self");

        await result.InvokeAsync().DefaultTimeout();

        Assert.Contains(interactionService.DisplayedMessages,
            m => m.Message.Contains(expectedLabel, StringComparison.OrdinalIgnoreCase));
    }

    // ──────────────────────────────────────────────────────────────
    // DotnetTool route uses "dotnet-tool" label (not lowercase)
    // ──────────────────────────────────────────────────────────────

    [Fact]
    internal async Task SelfUpdate_WhenDotnetToolRoute_DisplaysDotnetToolLabel()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interactionService = new TestInteractionService();

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.InteractionServiceFactory = _ => interactionService;
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = InstallRoute.DotnetTool;
                ctx.UpdateCommand = "dotnet tool update -g Aspire.Cli";
                return ctx;
            };
        });

        using var provider = services.BuildServiceProvider();
        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("update --self");

        await result.InvokeAsync().DefaultTimeout();

        Assert.Contains(interactionService.DisplayedMessages,
            m => m.Message.Contains("dotnet-tool", StringComparison.Ordinal));
    }

    // ──────────────────────────────────────────────────────────────
    // Unknown route with no stored update command → generic error
    // ──────────────────────────────────────────────────────────────

    [Fact]
    internal async Task SelfUpdate_WhenUnknownRouteAndNoUpdateCommand_DisplaysGenericError()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interactionService = new TestInteractionService();

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.InteractionServiceFactory = _ => interactionService;
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = InstallRoute.Unknown;
                ctx.UpdateCommand = null;
                return ctx;
            };
        });

        using var provider = services.BuildServiceProvider();
        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("update --self");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(ExitCodeConstants.InvalidCommand, exitCode);
        Assert.Contains(interactionService.DisplayedErrors,
            err => err.Contains("Unable to determine the install route", StringComparison.Ordinal));
    }

    // ──────────────────────────────────────────────────────────────
    // Script route proceeds to in-process self-update (reaches downloader)
    // ──────────────────────────────────────────────────────────────

    [Fact]
    internal async Task SelfUpdate_WhenScriptRoute_ReachesDownloader()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var downloaderInvoked = false;

        var services = CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.CliDownloaderFactory = _ => new TestCliDownloader(workspace.WorkspaceRoot)
            {
                DownloadLatestCliAsyncCallback = (channel, ct) =>
                {
                    downloaderInvoked = true;
                    // Return a fake archive; extraction will fail, but the downloader was reached.
                    var archivePath = Path.Combine(workspace.WorkspaceRoot.FullName, "fake.tar.gz");
                    File.WriteAllText(archivePath, "fake");
                    return Task.FromResult(archivePath);
                }
            };
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = InstallRoute.Script;
                return ctx;
            };
        });

        using var provider = services.BuildServiceProvider();
        var command = provider.GetRequiredService<RootCommand>();
        var result = command.Parse("update --self --channel stable");

        // Exit code will be non-zero because extraction fails; we only care that the downloader
        // was invoked (i.e., the Script route allowed in-process update to proceed).
        await result.InvokeAsync().DefaultTimeout();

        Assert.True(downloaderInvoked, "Script route should invoke the downloader for in-process self-update.");
    }
}

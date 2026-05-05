// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Commands;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Microsoft.AspNetCore.InternalTesting;
using Microsoft.Extensions.DependencyInjection;

namespace Aspire.Cli.Tests.Commands;

/// <summary>
/// Snapshot tests for <c>aspire which</c>. Covers all six routes in both human-readable
/// and JSON output modes. Snapshots live in the <c>Snapshots/</c> subdirectory.
/// </summary>
public class WhichCommandTests(ITestOutputHelper outputHelper)
{
    // ── Shared test data ──────────────────────────────────────────────────────

    // (routeLabel, route, channel, mode, prefix, updateCommand, version, prNumber)
    private static readonly (string Label, InstallRoute Route, string Channel, InstallMode Mode, string Prefix, string? UpdateCommand, string Version, int? PrNumber)[] s_routeCases =
    [
        ("unknown",     InstallRoute.Unknown,    "",          InstallMode.Unknown, "",                              null,                               "",           null),
        ("script",      InstallRoute.Script,     "stable",    InstallMode.A,       "/home/user/.aspire",            null,                               "9.4.0-dev",  null),
        ("pr",          InstallRoute.Pr,         "pr42",      InstallMode.A,       "/home/user/.aspire",            "get-aspire-cli-pr.sh -r 42",       "9.4.0-dev",  42),
        ("winget",      InstallRoute.Winget,     "stable",    InstallMode.B,       @"C:\Program Files\aspire",      "winget upgrade Microsoft.Aspire",   "9.4.0-dev",  null),
        ("brew",        InstallRoute.Brew,       "stable",    InstallMode.B,       "/opt/homebrew/aspire",          "brew upgrade aspire",              "9.4.0-dev",  null),
        ("dotnet-tool", InstallRoute.DotnetTool, "stable",    InstallMode.Unknown, "",                              "dotnet tool update -g Aspire.Cli", "9.4.0-dev",  null),
    ];

    // ── Helpers ───────────────────────────────────────────────────────────────

    private IServiceCollection CreateServices(
        TemporaryWorkspace workspace,
        TestInteractionService interaction,
        InstallRoute route,
        string channel,
        InstallMode mode,
        string prefix,
        string? updateCommand,
        string version = "",
        int? prNumber = null)
    {
        return CliTestHelper.CreateServiceCollection(workspace, outputHelper, options =>
        {
            options.InteractionServiceFactory = _ => interaction;
            options.CliExecutionContextFactory = provider =>
            {
                var ctx = CliTestHelper.CreateDefaultCliExecutionContext(provider, workspace);
                ctx.Route = route;
                ctx.Channel = channel;
                ctx.Mode = mode;
                ctx.Prefix = prefix;
                ctx.UpdateCommand = updateCommand;
                ctx.Version = version;
                ctx.PrNumber = prNumber;
                return ctx;
            };
        });
    }

    // ── Human-readable tests ──────────────────────────────────────────────────

    [Theory]
    [InlineData("unknown")]
    [InlineData("script")]
    [InlineData("pr")]
    [InlineData("winget")]
    [InlineData("brew")]
    [InlineData("dotnet-tool")]
    public async Task Which_HumanReadable_MatchesSnapshot(string routeLabel)
    {
        var (_, route, channel, mode, prefix, updateCommand, version, prNumber) =
            s_routeCases.First(r => r.Label == routeLabel);

        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interaction = new TestInteractionService();
        var services = CreateServices(workspace, interaction, route, channel, mode, prefix, updateCommand, version, prNumber);

        using var provider = services.BuildServiceProvider();
        var rootCommand = provider.GetRequiredService<RootCommand>();
        var result = rootCommand.Parse("which");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(0, exitCode);

        // Snapshot: list of "emoji: message" strings for readable comparison.
        var lines = interaction.DisplayedMessages
            .Select(m => $"{m.Emoji.Name}: {m.Message}")
            .ToArray();

        await Verify(lines)
            .UseFileName($"Which_HumanReadable_{routeLabel}");
    }

    // ── JSON output tests ─────────────────────────────────────────────────────

    [Theory]
    [InlineData("unknown")]
    [InlineData("script")]
    [InlineData("pr")]
    [InlineData("winget")]
    [InlineData("brew")]
    [InlineData("dotnet-tool")]
    public async Task Which_JsonOutput_MatchesSnapshot(string routeLabel)
    {
        var (_, route, channel, mode, prefix, updateCommand, version, prNumber) =
            s_routeCases.First(r => r.Label == routeLabel);

        using var workspace = TemporaryWorkspace.Create(outputHelper);
        string? capturedJson = null;
        var interaction = new TestInteractionService
        {
            DisplayRawTextCallback = text => capturedJson = text,
        };
        var services = CreateServices(workspace, interaction, route, channel, mode, prefix, updateCommand, version, prNumber);

        using var provider = services.BuildServiceProvider();
        var rootCommand = provider.GetRequiredService<RootCommand>();
        var result = rootCommand.Parse("which --format json");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(0, exitCode);
        Assert.NotNull(capturedJson);

        await Verify(capturedJson, extension: "json")
            .UseFileName($"Which_JsonOutput_{routeLabel}");
    }

    // ── PR-route prNumber regression tests ───────────────────────────────────

    /// <summary>
    /// Regression: prNumber is rendered in human-readable and JSON outputs for PR-route builds.
    /// Uses PrNumber=99999 (distinct from the s_routeCases PR fixture) to lock in the wiring.
    /// </summary>
    [Fact]
    public async Task Which_PrRoute_RendersPrNumber_Human()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var interaction = new TestInteractionService();
        var services = CreateServices(workspace, interaction,
            route: InstallRoute.Pr,
            channel: "pr",
            mode: InstallMode.A,
            prefix: "/home/user/.aspire/dogfood/pr-99999",
            updateCommand: "get-aspire-cli-pr.sh -r 99999",
            version: "9.4.0-dev",
            prNumber: 99999);

        using var provider = services.BuildServiceProvider();
        var rootCommand = provider.GetRequiredService<RootCommand>();
        var result = rootCommand.Parse("which");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(0, exitCode);

        var lines = interaction.DisplayedMessages
            .Select(m => $"{m.Emoji.Name}: {m.Message}")
            .ToArray();

        await Verify(lines)
            .UseFileName("Which_PrRoute_RendersPrNumber_Human");
    }

    [Fact]
    public async Task Which_PrRoute_RendersPrNumber_Json()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        string? capturedJson = null;
        var interaction = new TestInteractionService
        {
            DisplayRawTextCallback = text => capturedJson = text,
        };
        var services = CreateServices(workspace, interaction,
            route: InstallRoute.Pr,
            channel: "pr",
            mode: InstallMode.A,
            prefix: "/home/user/.aspire/dogfood/pr-99999",
            updateCommand: "get-aspire-cli-pr.sh -r 99999",
            version: "9.4.0-dev",
            prNumber: 99999);

        using var provider = services.BuildServiceProvider();
        var rootCommand = provider.GetRequiredService<RootCommand>();
        var result = rootCommand.Parse("which --format json");

        var exitCode = await result.InvokeAsync().DefaultTimeout();

        Assert.Equal(0, exitCode);
        Assert.NotNull(capturedJson);

        await Verify(capturedJson, extension: "json")
            .UseFileName("Which_PrRoute_RendersPrNumber_Json");
    }
}

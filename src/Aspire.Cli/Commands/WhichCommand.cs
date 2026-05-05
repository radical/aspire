// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.CommandLine;
using System.Text.Json;
using System.Text.Json.Serialization;
using Aspire.Cli.Configuration;
using Aspire.Cli.Interaction;
using Aspire.Cli.Resources;
using Aspire.Cli.Telemetry;
using Aspire.Cli.Utils;

namespace Aspire.Cli.Commands;

/// <summary>
/// The JSON output record for <c>aspire which --format json</c>.
/// </summary>
internal sealed record WhichOutput
{
    /// <summary>The detected install route (e.g., "script", "winget", "dotnet-tool", "unknown").</summary>
    public string Route { get; init; } = string.Empty;

    /// <summary>The channel embedded in the binary (e.g., "latest", "rc1", "pr12345").</summary>
    public string Channel { get; init; } = string.Empty;

    /// <summary>The binary's informational version, with any <c>+source-metadata</c> suffix stripped.</summary>
    public string Version { get; init; } = string.Empty;

    /// <summary>The PR number for PR-channel builds. Omitted from JSON when <c>null</c>.</summary>
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public int? PrNumber { get; init; }

    /// <summary>The install mode: "a", "b", or "unknown".</summary>
    public string Mode { get; init; } = string.Empty;

    /// <summary>The installation prefix directory. Empty when mode is unknown.</summary>
    public string Prefix { get; init; } = string.Empty;

    /// <summary>The user-facing update command hint, or <c>null</c> when not applicable.</summary>
    public string? UpdateCommand { get; init; }
}

/// <summary>
/// The <c>aspire which</c> command — prints identity information about the running Aspire CLI binary.
/// </summary>
internal sealed class WhichCommand : BaseCommand
{
    internal override HelpGroup HelpGroup => HelpGroup.ToolsAndConfiguration;

    private static readonly Option<OutputFormat> s_formatOption = new("--format")
    {
        Description = WhichCommandStrings.FormatOptionDescription,
    };

    public WhichCommand(
        IFeatures features,
        ICliUpdateNotifier updateNotifier,
        CliExecutionContext executionContext,
        IInteractionService interactionService,
        AspireCliTelemetry telemetry)
        : base("which", WhichCommandStrings.Description, features, updateNotifier, executionContext, interactionService, telemetry)
    {
        Options.Add(s_formatOption);
    }

    protected override bool UpdateNotificationsEnabled => false;

    protected override Task<int> ExecuteAsync(ParseResult parseResult, CancellationToken cancellationToken)
    {
        var format = parseResult.GetValue(s_formatOption);

        if (format == OutputFormat.Json)
        {
            OutputJson();
        }
        else
        {
            OutputHumanReadable();
        }

        return Task.FromResult(ExitCodeConstants.Success);
    }

    private void OutputJson()
    {
        var output = BuildOutput();
        var json = JsonSerializer.Serialize(output, JsonSourceGenerationContext.RelaxedEscaping.WhichOutput);
        InteractionService.DisplayRawText(json, ConsoleOutput.Standard);
    }

    private void OutputHumanReadable()
    {
        var unknown = WhichCommandStrings.UnknownValue;

        InteractionService.DisplayMessage(KnownEmojis.MagnifyingGlassTiltedLeft, $"{WhichCommandStrings.RouteLabel}: {RouteToString(ExecutionContext.Route)}");
        InteractionService.DisplayMessage(KnownEmojis.Information, $"{WhichCommandStrings.ChannelLabel}: {(string.IsNullOrEmpty(ExecutionContext.Channel) ? unknown : ExecutionContext.Channel)}");
        InteractionService.DisplayMessage(KnownEmojis.Information, $"{WhichCommandStrings.VersionLabel}: {(string.IsNullOrEmpty(ExecutionContext.Version) ? unknown : ExecutionContext.Version)}");

        if (ExecutionContext.PrNumber is not null)
        {
            InteractionService.DisplayMessage(KnownEmojis.Information, $"{WhichCommandStrings.PrNumberLabel}: {ExecutionContext.PrNumber}");
        }

        InteractionService.DisplayMessage(KnownEmojis.Package, $"{WhichCommandStrings.ModeLabel}: {ExecutionContext.Mode.ToString().ToLowerInvariant()}");

        if (!string.IsNullOrEmpty(ExecutionContext.Prefix))
        {
            InteractionService.DisplayMessage(KnownEmojis.FileFolder, $"{WhichCommandStrings.PrefixLabel}: {ExecutionContext.Prefix}");
        }

        if (!string.IsNullOrEmpty(ExecutionContext.UpdateCommand))
        {
            InteractionService.DisplayMessage(KnownEmojis.Gear, $"{WhichCommandStrings.UpdateCommandLabel}: {ExecutionContext.UpdateCommand}");
        }
    }

    private WhichOutput BuildOutput() =>
        new()
        {
            Route = RouteToString(ExecutionContext.Route),
            Channel = ExecutionContext.Channel ?? string.Empty,
            Version = ExecutionContext.Version,
            PrNumber = ExecutionContext.PrNumber,
            Mode = ExecutionContext.Mode.ToString().ToLowerInvariant(),
            Prefix = ExecutionContext.Prefix ?? string.Empty,
            UpdateCommand = ExecutionContext.UpdateCommand,
        };

    private static string RouteToString(Aspire.Cli.Acquisition.InstallRoute route) =>
        route switch
        {
            Aspire.Cli.Acquisition.InstallRoute.Script => "script",
            Aspire.Cli.Acquisition.InstallRoute.Pr => "pr",
            Aspire.Cli.Acquisition.InstallRoute.Winget => "winget",
            Aspire.Cli.Acquisition.InstallRoute.Brew => "brew",
            Aspire.Cli.Acquisition.InstallRoute.DotnetTool => "dotnet-tool",
            _ => "unknown",
        };
}

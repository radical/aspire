// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Reflection;
using Aspire.Cli.Configuration;
using Aspire.Cli.Tests.Utils;

namespace Aspire.Cli.Tests.Configuration;

/// <summary>
/// Tests for PR4 schema-cleanup: AspireConfigFile read-tolerance and migration correctness.
/// </summary>
public class AspireConfigFileMigrationTests(ITestOutputHelper outputHelper)
{
    /// <summary>
    /// aspire.config.json files written by pre-PR4 CLIs may still contain a "channel" field.
    /// Load must not throw and must surface the value so callers can read it.
    /// </summary>
    [Fact]
    public void Load_DoesNotThrow_WhenAspireConfigJsonContainsLegacyChannelField()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var configPath = Path.Combine(workspace.WorkspaceRoot.FullName, AspireConfigFile.FileName);
        File.WriteAllText(configPath, """{"channel":"daily","telemetry":false}""");

        var config = AspireConfigFile.Load(workspace.WorkspaceRoot.FullName);

        Assert.NotNull(config);
        Assert.Equal("daily", config.Channel);
    }

    /// <summary>
    /// Older tooling wrote a "preview" channel string; the model must tolerate arbitrary values.
    /// </summary>
    [Fact]
    public void Load_DoesNotThrow_WhenAspireConfigJsonContainsStalePreviewChannel()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var configPath = Path.Combine(workspace.WorkspaceRoot.FullName, AspireConfigFile.FileName);
        File.WriteAllText(configPath, """{"channel":"preview","sdk":{"version":"9.3.0"}}""");

        var config = AspireConfigFile.Load(workspace.WorkspaceRoot.FullName);

        Assert.NotNull(config);
        Assert.Equal("preview", config.Channel);
        Assert.Equal("9.3.0", config.SdkVersion);
    }

    /// <summary>
    /// FromLegacy (migration from .aspire/settings.json) must NOT copy the Channel field.
    /// Other fields (SdkVersion, AppHostPath, Language) must still be migrated.
    /// </summary>
    [Fact]
    public void FromLegacy_DropsChannel_WhenMigratingFromLegacySettings()
    {
        var settings = new AspireJsonConfiguration
        {
            Channel = "daily",
            SdkVersion = "9.3.0",
            AppHostPath = "app.ts",
            Language = "typescript"
        };

        var config = AspireConfigFile.FromLegacy(settings, profiles: null);

        Assert.Null(config.Channel);
        Assert.Equal("9.3.0", config.SdkVersion);
        Assert.Equal("app.ts", config.AppHost?.Path);
        Assert.Equal("typescript", config.AppHost?.Language);
    }

    /// <summary>
    /// AspireJsonConfiguration.Channel must carry [LocalAspireJsonConfigurationProperty] so it is
    /// excluded from the global JSON Schema generated for ~/.aspire/aspire.config.json.
    /// </summary>
    [Fact]
    public void AspireJsonConfiguration_Channel_HasLocalAspireJsonConfigurationPropertyAttribute()
    {
        var property = typeof(AspireJsonConfiguration).GetProperty(
            nameof(AspireJsonConfiguration.Channel),
            BindingFlags.Public | BindingFlags.Instance);

        Assert.NotNull(property);
        var attribute = property.GetCustomAttribute<LocalAspireJsonConfigurationPropertyAttribute>();
        Assert.NotNull(attribute);
    }
}

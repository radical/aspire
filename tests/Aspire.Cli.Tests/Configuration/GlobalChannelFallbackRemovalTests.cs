// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Reflection;
using Aspire.Cli.Commands;
using Aspire.Cli.Configuration;
using Aspire.Cli.Layout;
using Aspire.Cli.NuGet;
using Aspire.Cli.Projects;
using Aspire.Cli.Tests.Mcp;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests.Configuration;

/// <summary>
/// Regression guard for PR4 global-channel fallback removal.
/// Before PR4, <see cref="DotNetBasedAppHostServerProject"/>, <see cref="PrebuiltAppHostServer"/>,
/// and <see cref="NewCommand"/> all accepted an <c>IConfigurationService</c> and would fall back to
/// the global channel stored there when no project-local channel was set. PR4 removes that fallback
/// entirely; these tests assert that the structural and behavioral changes hold.
/// </summary>
public class GlobalChannelFallbackRemovalTests(ITestOutputHelper outputHelper)
{
    // ---------------------------------------------------------------------------
    // Structural: IConfigurationService must not appear in any constructor
    // ---------------------------------------------------------------------------

    [Fact]
    public void DotNetBasedAppHostServerProject_HasNoIConfigurationServiceConstructorParameter()
    {
        var ctors = typeof(DotNetBasedAppHostServerProject)
            .GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);

        foreach (var ctor in ctors)
        {
            var hasConfigService = ctor.GetParameters()
                .Any(p => p.ParameterType == typeof(IConfigurationService));

            Assert.False(hasConfigService,
                $"{nameof(DotNetBasedAppHostServerProject)} constructor must not accept IConfigurationService after PR4 global-channel removal.");
        }
    }

    [Fact]
    public void PrebuiltAppHostServer_HasNoIConfigurationServiceConstructorParameter()
    {
        var ctors = typeof(PrebuiltAppHostServer)
            .GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);

        foreach (var ctor in ctors)
        {
            var hasConfigService = ctor.GetParameters()
                .Any(p => p.ParameterType == typeof(IConfigurationService));

            Assert.False(hasConfigService,
                $"{nameof(PrebuiltAppHostServer)} constructor must not accept IConfigurationService after PR4 global-channel removal.");
        }
    }

    [Fact]
    public void NewCommand_HasNoIConfigurationServiceConstructorParameter()
    {
        var ctors = typeof(NewCommand)
            .GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);

        foreach (var ctor in ctors)
        {
            var hasConfigService = ctor.GetParameters()
                .Any(p => p.ParameterType == typeof(IConfigurationService));

            Assert.False(hasConfigService,
                $"{nameof(NewCommand)} constructor must not accept IConfigurationService after PR4 global-channel removal.");
        }
    }

    // ---------------------------------------------------------------------------
    // Behavioral: project-local channel resolution returns null with empty config
    // ---------------------------------------------------------------------------

    /// <summary>
    /// With no aspire.config.json or .aspire/settings.json channel field in the project directory,
    /// ResolveChannelName must return null — it must not pick up any global value.
    /// </summary>
    [Fact]
    public void PrebuiltAppHostServer_ResolveChannelName_ReturnsNull_WhenProjectConfigHasNoChannel()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var appHostDirectory = workspace.CreateDirectory("apphost");

        // Write a minimal aspire.config.json with no channel field.
        var configPath = Path.Combine(appHostDirectory.FullName, AspireConfigFile.FileName);
        File.WriteAllText(configPath, """{"sdk":{"version":"9.3.0"}}""");

        var nugetService = new BundleNuGetService(
            new NullLayoutDiscovery(),
            new LayoutProcessRunner(new TestProcessExecutionFactory()),
            new TestFeatures(),
            TestExecutionContextFactory.CreateTestContext(),
            NullLogger<BundleNuGetService>.Instance);

        var server = new PrebuiltAppHostServer(
            appHostDirectory.FullName,
            "test.sock",
            new LayoutConfiguration(),
            nugetService,
            new TestDotNetCliRunner(),
            new TestDotNetSdkInstaller(),
            MockPackagingServiceFactory.Create(),
            NullLogger.Instance);

        var method = typeof(PrebuiltAppHostServer)
            .GetMethod("ResolveChannelName", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(method);

        var channel = (string?)method.Invoke(server, []);

        Assert.Null(channel);
    }

    /// <summary>
    /// With a project-local channel set in aspire.config.json, ResolveChannelName must return that value.
    /// This confirms the method still reads from the project-local file (not from a now-absent global path).
    /// </summary>
    [Fact]
    public void PrebuiltAppHostServer_ResolveChannelName_ReturnsProjectLocalChannel()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var appHostDirectory = workspace.CreateDirectory("apphost");

        var configPath = Path.Combine(appHostDirectory.FullName, AspireConfigFile.FileName);
        File.WriteAllText(configPath, """{"channel":"stable"}""");

        var nugetService = new BundleNuGetService(
            new NullLayoutDiscovery(),
            new LayoutProcessRunner(new TestProcessExecutionFactory()),
            new TestFeatures(),
            TestExecutionContextFactory.CreateTestContext(),
            NullLogger<BundleNuGetService>.Instance);

        var server = new PrebuiltAppHostServer(
            appHostDirectory.FullName,
            "test.sock",
            new LayoutConfiguration(),
            nugetService,
            new TestDotNetCliRunner(),
            new TestDotNetSdkInstaller(),
            MockPackagingServiceFactory.Create(),
            NullLogger.Instance);

        var method = typeof(PrebuiltAppHostServer)
            .GetMethod("ResolveChannelName", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(method);

        var channel = (string?)method.Invoke(server, []);

        Assert.Equal("stable", channel);
    }
}

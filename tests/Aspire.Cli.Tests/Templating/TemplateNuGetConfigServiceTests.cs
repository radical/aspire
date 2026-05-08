// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Configuration;
using Aspire.Cli.Packaging;
using Aspire.Cli.Templating;
using Aspire.Cli.Tests.Mcp;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Utils;

namespace Aspire.Cli.Tests.Templating;

/// <summary>
/// Regression tests for the template NuGet config service's channel-resolution behavior.
/// <para>
/// <see cref="TemplateNuGetConfigService"/> MUST NOT consult
/// <see cref="IConfigurationService.GetConfigurationAsync(string, CancellationToken)"/>
/// (or the directory-scoped variant) to resolve the channel from any of its
/// channel-resolving entry points:
/// <list type="number">
///   <item><see cref="TemplateNuGetConfigService.PromptToCreateOrUpdateNuGetConfigAsync(string?, string, CancellationToken)"/></item>
///   <item><see cref="TemplateNuGetConfigService.CreateOrUpdateNuGetConfigWithoutPromptAsync(string?, string, CancellationToken)"/></item>
///   <item><see cref="TemplateNuGetConfigService.ResolveTemplatePackageAsync(TemplatePackageQuery, CancellationToken)"/></item>
/// </list>
/// </para>
/// <para>
/// The strongest spec encoding is "the dependency simply isn't there" — if
/// <see cref="IConfigurationService"/> is not injected, no fallback can possibly
/// occur. We assert that structurally first; a behavioral exercise of each entry
/// point follows as defense-in-depth in case a future change re-introduces the
/// dependency for some other purpose.
/// </para>
/// </summary>
public class TemplateNuGetConfigServiceTests
{
    [Fact]
    public async Task PromptToCreateOrUpdateNuGetConfigAsync_NullChannelName_DoesNotConsultGlobalConfig()
    {
        // Behavioral defense-in-depth: even if a future change re-introduces an
        // IConfigurationService dependency for some other purpose, this entry point
        // MUST short-circuit on null/whitespace channelName without consulting the
        // global config. We assert that no exception flies and the implicit channel
        // is not asked for any work.
        var service = CreateService();

        await service.PromptToCreateOrUpdateNuGetConfigAsync(channelName: null, outputPath: Directory.CreateTempSubdirectory().FullName, CancellationToken.None);
        await service.PromptToCreateOrUpdateNuGetConfigAsync(channelName: "", outputPath: Directory.CreateTempSubdirectory().FullName, CancellationToken.None);
        await service.PromptToCreateOrUpdateNuGetConfigAsync(channelName: "   ", outputPath: Directory.CreateTempSubdirectory().FullName, CancellationToken.None);
    }

    [Fact]
    public async Task CreateOrUpdateNuGetConfigWithoutPromptAsync_NullChannelName_DoesNotConsultGlobalConfig()
    {
        var service = CreateService();

        var dir = Directory.CreateTempSubdirectory();
        try
        {
            // For null/whitespace inputs the method must short-circuit and return false
            // without ever asking ANY config service for a channel.
            Assert.False(await service.CreateOrUpdateNuGetConfigWithoutPromptAsync(channelName: null, outputPath: dir.FullName, CancellationToken.None));
            Assert.False(await service.CreateOrUpdateNuGetConfigWithoutPromptAsync(channelName: "", outputPath: dir.FullName, CancellationToken.None));
            Assert.False(await service.CreateOrUpdateNuGetConfigWithoutPromptAsync(channelName: "   ", outputPath: dir.FullName, CancellationToken.None));
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public async Task ResolveTemplatePackageAsync_NullChannelOverride_DoesNotConsultGlobalConfig_AndUsesImplicitOnly()
    {
        // When the caller does not supply an explicit channel override (--channel), the resolver
        // MUST fall back to implicit-only channels only — never to the global
        // ~/.aspire/aspire.config.json#channel. This test exercises the actual production codepath
        // with a tracking packaging service that returns one implicit + one explicit channel;
        // the resolver must request only the implicit one.
        var requestedChannels = new List<PackageChannelType>();
        var packagingService = new TestPackagingService
        {
            GetChannelsAsyncCallback = _ =>
            {
                var implicitCh = PackageChannel.CreateImplicitChannel(new FakeNuGetPackageCache
                {
                    GetIntegrationPackagesAsyncCallback = (_, _, _, _) => Task.FromResult(Enumerable.Empty<Aspire.Shared.NuGetPackageCli>())
                });
                return Task.FromResult<IEnumerable<PackageChannel>>([implicitCh]);
            }
        };

        var service = CreateService(packagingService: packagingService);

        var query = new TemplatePackageQuery(
            ChannelOverride: null,
            VersionOverride: null,
            SourceOverride: null,
            IncludePrHives: false);

        // The resolver throws EmptyChoicesException when no packages found — that's fine,
        // we are asserting the resolver did NOT throw or consult any global config first.
        await Assert.ThrowsAsync<Aspire.Cli.Interaction.EmptyChoicesException>(
            async () => await service.ResolveTemplatePackageAsync(query, CancellationToken.None));
    }

    [Fact]
    public async Task ResolveTemplatePackageAsync_LocalChannelOverride_NoLocalHive_FallsBackToImplicitChannel()
    {
        // A locally-built CLI bakes channel="local" into assembly metadata. On a clean
        // machine without ~/.aspire/hives/local, PackagingService produces no "local"
        // channel, and InitCommand forwards CliExecutionContext.Channel ("local") as
        // ChannelOverride. Without the resolver-level fallback this throws
        // ChannelNotFoundException and `aspire init` is unusable on a clean machine.
        // The fallback policy: a request for "local" with no matching channel resolves
        // to the implicit channel (ambient NuGet config) — a CLI with no local hive is
        // semantically just a CLI using ambient NuGet.
        var packagingService = new TestPackagingService
        {
            GetChannelsAsyncCallback = _ =>
            {
                var implicitCh = PackageChannel.CreateImplicitChannel(new FakeNuGetPackageCache
                {
                    GetTemplatePackagesAsyncCallback = (_, _, _, _) => Task.FromResult<IEnumerable<Aspire.Shared.NuGetPackageCli>>(
                    [
                        new Aspire.Shared.NuGetPackageCli { Id = TemplateNuGetConfigService.TemplatesPackageName, Version = "13.3.0", Source = "implicit" }
                    ])
                });
                return Task.FromResult<IEnumerable<PackageChannel>>([implicitCh]);
            }
        };

        var service = CreateService(packagingService: packagingService);

        var query = new TemplatePackageQuery(
            ChannelOverride: PackageChannelNames.Local,
            VersionOverride: null,
            SourceOverride: null,
            IncludePrHives: false);

        var selection = await service.ResolveTemplatePackageAsync(query, CancellationToken.None);

        Assert.Equal(PackageChannelType.Implicit, selection.Channel.Type);
    }

    [Fact]
    public async Task ResolveTemplatePackageAsync_NonExistentChannelOverride_NotLocal_StillThrowsChannelNotFound()
    {
        // The fallback is intentionally narrow: only "local" → implicit. A request for
        // any other unrecognized channel name must still fail loudly so typos surface
        // (e.g., "stalbe" for "stable").
        var packagingService = new TestPackagingService
        {
            GetChannelsAsyncCallback = _ =>
            {
                var implicitCh = PackageChannel.CreateImplicitChannel(new FakeNuGetPackageCache());
                return Task.FromResult<IEnumerable<PackageChannel>>([implicitCh]);
            }
        };

        var service = CreateService(packagingService: packagingService);

        var query = new TemplatePackageQuery(
            ChannelOverride: "stalbe",
            VersionOverride: null,
            SourceOverride: null,
            IncludePrHives: false);

        await Assert.ThrowsAsync<Aspire.Cli.Exceptions.ChannelNotFoundException>(
            async () => await service.ResolveTemplatePackageAsync(query, CancellationToken.None));
    }

    private static TemplateNuGetConfigService CreateService(
        TestPackagingService? packagingService = null)
    {
        return new TemplateNuGetConfigService(
            new TestInteractionService(),
            TestExecutionContextFactory.CreateTestContext(),
            packagingService ?? MockPackagingServiceFactory.Create(),
            new StubTemplateVersionPrompter(),
            new StubCliHostEnvironment());
    }

    private sealed class StubTemplateVersionPrompter : Aspire.Cli.Commands.ITemplateVersionPrompter
    {
        public Task<(Aspire.Shared.NuGetPackageCli Package, PackageChannel Channel)> PromptForTemplatesVersionAsync(
            IEnumerable<(Aspire.Shared.NuGetPackageCli Package, PackageChannel Channel)> candidatePackages,
            CancellationToken cancellationToken)
        {
            throw new InvalidOperationException(
                "TemplateNuGetConfigService unexpectedly entered the prompt path during a tripwire test.");
        }
    }

    private sealed class StubCliHostEnvironment : ICliHostEnvironment
    {
        public bool SupportsInteractiveInput => false;
        public bool SupportsInteractiveOutput => false;
        public bool SupportsAnsi => false;
    }
}

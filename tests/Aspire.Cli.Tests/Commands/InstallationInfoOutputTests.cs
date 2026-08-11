// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json;
using Aspire.Cli.Acquisition;
using Aspire.Cli.Commands;
using Aspire.Cli.Tests.TestServices;
using Microsoft.AspNetCore.InternalTesting;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests.Commands;

public class InstallationInfoOutputTests
{
    // ---------------------------------------------------------------------------
    // Aggregate-discovery timeout: updated for the new DiscoveryResult return type
    // while preserving the core falsifiability — that a discovery which ignores
    // cancellation is still bounded and the bound surfaces as AggregateFailureReason.
    // ---------------------------------------------------------------------------

    [Fact]
    public async Task DiscoverAllToResultSafelyAsync_TimesOutWhenDiscoveryDoesNotObserveCancellation()
    {
        using var releaseDiscovery = new ManualResetEventSlim();
        var discoveryStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var discoveryExited = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var self = new InstallationInfo
        {
            Path = "/test/aspire",
            Status = InstallationInfoStatus.Ok,
        };
        var discovery = new FakeInstallationDiscovery(self)
        {
            DiscoverAllAsyncCallback = _ =>
            {
                discoveryStarted.SetResult();
                releaseDiscovery.Wait(CancellationToken.None);
                discoveryExited.SetResult();
                return Task.FromResult<IReadOnlyList<InstallationInfo>>([self]);
            },
        };
        var wingetProbe = new WingetFirstRunProbe(
            new TestWindowsRegistryReader(),
            NullLogger<WingetFirstRunProbe>.Instance);

        var discoveryTask = InstallationInfoOutput.DiscoverAllToResultSafelyAsync(
            discovery,
            wingetProbe,
            NullLogger.Instance,
            TimeSpan.FromMilliseconds(100),
            TestContext.Current.CancellationToken);
        DiscoveryResult result;
        try
        {
            await discoveryStarted.Task.DefaultTimeout();
            result = await discoveryTask.DefaultTimeout();
        }
        finally
        {
            releaseDiscovery.Set();
        }

        await discoveryExited.Task.DefaultTimeout();

        // Aggregate timeout surfaces as AggregateFailureReason, not as a peer row.
        Assert.Empty(result.Installations);
        Assert.NotNull(result.AggregateFailureReason);
        Assert.Contains("timed out after", result.AggregateFailureReason, StringComparison.OrdinalIgnoreCase);
    }

    // ---------------------------------------------------------------------------
    // MapToInfoInstallation: Route → source, hive correlation, field preservation
    // ---------------------------------------------------------------------------

    [Fact]
    public void MapToInfoInstallation_MapsRouteToSource_AndPreservesAllFields()
    {
        var install = new InstallationInfo
        {
            Path = "/usr/local/bin/aspire",
            CanonicalPath = "/opt/aspire/bin/aspire",
            Version = "13.0.0",
            Channel = "stable",
            Route = "path",
            PathStatus = InstallationPathStatus.Active,
            Status = InstallationInfoStatus.Ok,
        };
        var hivesByChannel = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["stable"] = "/home/test/.aspire/hives/stable",
        };

        var row = InstallationInfoOutput.MapToInfoInstallation(install, hivesByChannel);

        Assert.Equal(InfoInstallationKind.Installation, row.Kind);
        Assert.Equal("/usr/local/bin/aspire", row.Path);
        Assert.Equal("/opt/aspire/bin/aspire", row.CanonicalPath);
        Assert.Equal("13.0.0", row.Version);
        Assert.Equal("stable", row.Channel);
        Assert.Equal("path", row.Source);   // Route → source
        Assert.Equal("/home/test/.aspire/hives/stable", row.Hive);  // hive correlated by channel
        Assert.Equal(InstallationPathStatus.Active, row.PathStatus);
        Assert.Equal(InstallationInfoStatus.Ok, row.Status);
        Assert.Null(row.StatusReason);
    }

    [Fact]
    public void MapToInfoInstallation_InvalidChannel_DoesNotCorrelateHive()
    {
        // A null or otherwise invalid channel must not trigger hive correlation
        // even if a hive directory matching the value exists in the dict.
        var install = new InstallationInfo
        {
            Path = "/some/bin/aspire",
            Channel = null,
            Route = "script",
            PathStatus = InstallationPathStatus.NotOnPath,
            Status = InstallationInfoStatus.Ok,
        };
        var hivesByChannel = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["stable"] = "/home/test/.aspire/hives/stable",
        };

        var row = InstallationInfoOutput.MapToInfoInstallation(install, hivesByChannel);

        Assert.Equal(InfoInstallationKind.Installation, row.Kind);
        Assert.Null(row.Hive);
    }

    // ---------------------------------------------------------------------------
    // BuildInfoRows: orphan hives and aggregate failures
    // ---------------------------------------------------------------------------

    [Fact]
    public void BuildInfoRows_UnmatchedHivesBecome_OrphanHiveRows()
    {
        var self = new InstallationInfo
        {
            Path = "/usr/local/bin/aspire",
            Channel = "stable",
            Route = "path",
            PathStatus = InstallationPathStatus.Active,
            Status = InstallationInfoStatus.Ok,
        };
        // Two hives: one matched by the installation's channel, one orphan.
        var hives = new[]
        {
            new HiveInfo("stable", "/home/test/.aspire/hives/stable"),
            new HiveInfo("pr-999", "/home/test/.aspire/hives/pr-999"),
        };
        var discoveryResult = new DiscoveryResult([self], AggregateFailureReason: null);

        var rows = InstallationInfoOutput.BuildInfoRows(discoveryResult, hives);

        Assert.Equal(2, rows.Length);

        var installRow = Assert.Single(rows, r => r.Kind == InfoInstallationKind.Installation);
        Assert.Equal("/usr/local/bin/aspire", installRow.Path);
        Assert.Equal("/home/test/.aspire/hives/stable", installRow.Hive);

        var orphanRow = Assert.Single(rows, r => r.Kind == InfoInstallationKind.OrphanHive);
        Assert.Equal("/home/test/.aspire/hives/pr-999", orphanRow.Hive);
        Assert.Equal(InstallationPathStatus.NotOnPath, orphanRow.PathStatus);
        Assert.Equal("noInstallFound", orphanRow.Status);
        Assert.Null(orphanRow.Channel);  // orphan rows carry no synthetic channel
    }

    [Fact]
    public void BuildInfoRows_AggregateFailure_BecomesDiscoveryFailedRow()
    {
        var discoveryResult = new DiscoveryResult(
            Installations: [],
            AggregateFailureReason: "discovery exploded");
        var hives = Array.Empty<HiveInfo>();

        var rows = InstallationInfoOutput.BuildInfoRows(discoveryResult, hives);

        var single = Assert.Single(rows);
        Assert.Equal(InfoInstallationKind.DiscoveryFailed, single.Kind);
        Assert.Equal(InstallationPathStatus.NotOnPath, single.PathStatus);
        Assert.Equal(InstallationInfoStatus.Failed, single.Status);
        Assert.Equal("discovery exploded", single.StatusReason);
    }

    [Fact]
    public void BuildInfoRows_PeerFailedRow_RemainsInstallationKind()
    {
        // A peer-level failure (status: failed) is distinct from aggregate failure.
        // It must remain kind: "installation", not kind: "discovery-failed".
        var failedPeer = new InstallationInfo
        {
            Path = "/other/bin/aspire",
            PathStatus = InstallationPathStatus.Shadowed,
            Status = InstallationInfoStatus.Failed,
            StatusReason = "probe timed out",
        };
        var discoveryResult = new DiscoveryResult([failedPeer], AggregateFailureReason: null);
        var hives = Array.Empty<HiveInfo>();

        var rows = InstallationInfoOutput.BuildInfoRows(discoveryResult, hives);

        var single = Assert.Single(rows);
        Assert.Equal(InfoInstallationKind.Installation, single.Kind);
        Assert.Equal(InstallationInfoStatus.Failed, single.Status);
    }

    // ---------------------------------------------------------------------------
    // DiscoverAllToResultSafelyAsync: caller cancellation propagates
    // ---------------------------------------------------------------------------

    [Fact]
    public async Task DiscoverAllToResultSafelyAsync_CallerCancellation_Propagates()
    {
        using var cts = new CancellationTokenSource();
        var self = new InstallationInfo
        {
            Path = "/test/aspire",
            Status = InstallationInfoStatus.Ok,
        };
        var discovery = new FakeInstallationDiscovery(self)
        {
            DiscoverAllAsyncCallback = async ct =>
            {
                await cts.CancelAsync();
                ct.ThrowIfCancellationRequested();
                return [self];
            },
        };
        var wingetProbe = new WingetFirstRunProbe(
            new TestWindowsRegistryReader(),
            NullLogger<WingetFirstRunProbe>.Instance);

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => InstallationInfoOutput.DiscoverAllToResultSafelyAsync(
                discovery, wingetProbe, NullLogger.Instance, cts.Token));
    }

    // ---------------------------------------------------------------------------
    // DescribeSelfAsInfoInstallation
    // ---------------------------------------------------------------------------

    [Fact]
    public void DescribeSelfAsInfoInstallation_Success_MapsToInstallationRow()
    {
        var self = new InstallationInfo
        {
            Path = "/self/aspire",
            CanonicalPath = "/self/aspire",
            Version = "13.0.0",
            Channel = "stable",
            Route = "script",
            PathStatus = InstallationPathStatus.Active,
            Status = InstallationInfoStatus.Ok,
        };
        var discovery = new FakeInstallationDiscovery(self);
        var hivesByChannel = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        var row = InstallationInfoOutput.DescribeSelfAsInfoInstallation(
            discovery, NullLogger.Instance, hivesByChannel);

        Assert.Equal(InfoInstallationKind.Installation, row.Kind);
        Assert.Equal("/self/aspire", row.Path);
        Assert.Equal("script", row.Source);
    }

    [Fact]
    public void DescribeSelfAsInfoInstallation_NonCancellationException_ReturnsDiscoveryFailedRow()
    {
        var self = new InstallationInfo { Path = "", Status = InstallationInfoStatus.Failed };
        var discovery = new FakeInstallationDiscovery(self)
        {
            DescribeSelfCallback = () => throw new InvalidOperationException("self probe exploded"),
        };
        var hivesByChannel = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        var row = InstallationInfoOutput.DescribeSelfAsInfoInstallation(
            discovery, NullLogger.Instance, hivesByChannel);

        Assert.Equal(InfoInstallationKind.DiscoveryFailed, row.Kind);
        Assert.Equal(InstallationInfoStatus.Failed, row.Status);
    }

    [Fact]
    public void DescribeSelfAsInfoInstallation_CancellationException_Propagates()
    {
        var self = new InstallationInfo { Path = "", Status = InstallationInfoStatus.Failed };
        var discovery = new FakeInstallationDiscovery(self)
        {
            DescribeSelfCallback = () => throw new OperationCanceledException("cancelled"),
        };
        var hivesByChannel = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        Assert.Throws<OperationCanceledException>(
            () => InstallationInfoOutput.DescribeSelfAsInfoInstallation(
                discovery, NullLogger.Instance, hivesByChannel));
    }

    // ---------------------------------------------------------------------------
    // JSON serialization: source field, not route; nulls omitted
    // ---------------------------------------------------------------------------

    [Fact]
    public async Task SerializeInfoOutput_UsesSourceField_AndOmitsNulls()
    {
        var output = new InfoOutput
        {
            Version = "13.0.0",
            Channel = "stable",
            Installs =
            [
                new InfoInstallation
                {
                    Kind = InfoInstallationKind.Installation,
                    Path = "/usr/local/bin/aspire",
                    CanonicalPath = "/opt/aspire/bin/aspire",
                    Version = "13.0.0",
                    Channel = "stable",
                    Source = "path",
                    Hive = "/home/test/.aspire/hives/stable",
                    PathStatus = InstallationPathStatus.Active,
                    Status = InstallationInfoStatus.Ok,
                },
            ],
        };

        var json = JsonSerializer.Serialize(output, JsonSourceGenerationContext.Default.InfoOutput);

        // Wire name must be "source", not "route".
        Assert.Contains("\"source\"", json, StringComparison.Ordinal);
        Assert.DoesNotContain("\"route\"", json, StringComparison.Ordinal);
        // Required fields must be present.
        Assert.Contains("\"pathStatus\"", json, StringComparison.Ordinal);
        Assert.Contains("\"status\"", json, StringComparison.Ordinal);
        Assert.Contains("\"canonicalPath\"", json, StringComparison.Ordinal);
        // Null statusReason must be omitted (WhenWritingNull).
        Assert.DoesNotContain("\"statusReason\"", json, StringComparison.Ordinal);

        await Verify(json, "json");
    }

    // ---------------------------------------------------------------------------
    // InstallationInfoParser: source preferred; legacy route accepted as fallback
    // ---------------------------------------------------------------------------

    [Fact]
    public void InstallationInfoParser_AcceptsSourceField()
    {
        var element = JsonDocument.Parse("""
            {
              "path": "/usr/local/bin/aspire",
              "source": "script",
              "pathStatus": "active",
              "status": "ok"
            }
            """).RootElement;

        var info = InstallationInfoParser.Parse(element);

        Assert.Equal("script", info.Route);
    }

    [Fact]
    public void InstallationInfoParser_FallsBackToRouteWhenSourceAbsent()
    {
        var element = JsonDocument.Parse("""
            {
              "path": "/usr/local/bin/aspire",
              "route": "winget",
              "pathStatus": "active",
              "status": "ok"
            }
            """).RootElement;

        var info = InstallationInfoParser.Parse(element);

        Assert.Equal("winget", info.Route);
    }

    [Fact]
    public void InstallationInfoParser_PrefersSourceOverRoute_WhenBothPresent()
    {
        // Forward/backward compatibility: a row may carry both fields; source wins.
        var element = JsonDocument.Parse("""
            {
              "path": "/usr/local/bin/aspire",
              "source": "brew",
              "route": "script",
              "pathStatus": "active",
              "status": "ok"
            }
            """).RootElement;

        var info = InstallationInfoParser.Parse(element);

        Assert.Equal("brew", info.Route);
    }
}
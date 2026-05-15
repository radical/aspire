// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Tests.TestServices;
using Aspire.Cli.Tests.Utils;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

namespace Aspire.Cli.Tests.Acquisition;

/// <summary>
/// Behavior tests for <see cref="InstallationDiscovery.DiscoverAllAsync"/>
/// focused on the trust gate (RD-2) and dedup-by-canonical-path semantics.
/// PATH and well-known-prefix walks are exercised via an isolated
/// HOME-equivalent so a developer's real home directory doesn't leak
/// into the test.
/// </summary>
public class InstallationDiscoveryDiscoverAllTests(ITestOutputHelper outputHelper)
{
    [Fact]
    public async Task DiscoverAllAsync_PathHit_WithoutSidecar_IsListedAsNotProbed_AndNeverSpawned()
    {
        // RD-2 trust gate: a binary on $PATH with no .aspire-install.json
        // next to it must NOT be spawned. The user-installed binary on
        // PATH is the most dangerous case: if a user runs `aspire info --all`
        // we cannot execute arbitrary same-named binaries we happened to
        // find.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var pathDir = Path.Combine(workspace.WorkspaceRoot.FullName, "untrusted-bin");
        Directory.CreateDirectory(pathDir);
        var untrustedBinary = WriteFakeBinary(pathDir);

        var probe = new FakePeerInstallProbe();
        using var _ = new EnvVarOverride("PATH", pathDir + Path.PathSeparator + (Environment.GetEnvironmentVariable("PATH") ?? string.Empty));
        using var __ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var ___ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(probe);
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        // Trust gate enforces: untrusted PATH hit must NOT be probed.
        Assert.DoesNotContain(probe.ProbedPaths, p => string.Equals(p, untrustedBinary, StringComparison.Ordinal));

        var untrustedRow = Assert.Single(results, r =>
            string.Equals(r.CanonicalPath, untrustedBinary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
        Assert.Equal(InstallationInfoStatus.NotProbed, untrustedRow.Status);
        Assert.Contains("trust gate", untrustedRow.StatusReason!, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task DiscoverAllAsync_TrustedSidecar_IsSpawnedAndDecoratedWithDiscoveredPath()
    {
        // A binary with a script-route sidecar in its directory passes the
        // trust gate. The peer probe is called, and its returned
        // InstallationInfo is merged with the discovered path so the row
        // displayed to the user matches what `which` would show.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var binDir = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire", "bin");
        Directory.CreateDirectory(binDir);
        var binary = WriteFakeBinary(binDir);
        File.WriteAllText(Path.Combine(binDir, InstallSidecarReader.SidecarFileName), "{\"source\":\"script\"}");

        var probe = new FakePeerInstallProbe(new Dictionary<string, PeerProbeResult>
        {
            [binary] = new PeerProbeResult.Ok(new InstallationInfo
            {
                Path = "/peer-says/aspire",
                Version = "12.5.0",
                Channel = "stable",
                Route = "script",
                Status = InstallationInfoStatus.Ok,
            }),
        });

        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(probe);
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        Assert.Contains(probe.ProbedPaths, p => string.Equals(p, binary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));

        var discoveredRow = results.Single(r =>
            string.Equals(r.CanonicalPath, binary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
        Assert.Equal("12.5.0", discoveredRow.Version);
        Assert.Equal("stable", discoveredRow.Channel);
        // Discovered path wins over what the peer reported, so the table
        // reflects where the binary lives on disk.
        Assert.Equal(binary, discoveredRow.Path);
    }

    [Fact]
    public async Task DiscoverAllAsync_PeerProbeFails_RowSurvivesAsNotProbed_WithRouteIntact()
    {
        // RD-10: a peer that fails (timeout / non-zero exit / invalid JSON)
        // is per-row, not whole-command. The route from the sidecar is
        // still surfaced so the user sees "this is a PR install but it
        // wouldn't talk to me", not nothing.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prDir = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire", "dogfood", "pr-9999", "bin");
        Directory.CreateDirectory(prDir);
        var binary = WriteFakeBinary(prDir);
        File.WriteAllText(Path.Combine(prDir, InstallSidecarReader.SidecarFileName), "{\"source\":\"pr\"}");

        var probe = new FakePeerInstallProbe(new Dictionary<string, PeerProbeResult>
        {
            [binary] = new PeerProbeResult.Failed("simulated peer hang"),
        });

        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(probe);
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        var row = results.Single(r =>
            string.Equals(r.CanonicalPath, binary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
        Assert.Equal(InstallationInfoStatus.NotProbed, row.Status);
        Assert.Equal("pr", row.Route);
        Assert.Contains("simulated peer hang", row.StatusReason!);
    }

    [Fact]
    public async Task DiscoverAllAsync_PrRoute_DerivesChannelFromDogfoodPathWhenPeerOmits()
    {
        // The structural channel for a PR install is `pr-<N>` regardless
        // of whether the older peer's --version output includes channel
        // info. Discovery should overlay it from the dogfood/pr-<N>/
        // path layout when probe.Channel comes back null.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prDir = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire", "dogfood", "pr-12345", "bin");
        Directory.CreateDirectory(prDir);
        var binary = WriteFakeBinary(prDir);
        File.WriteAllText(Path.Combine(prDir, InstallSidecarReader.SidecarFileName), "{\"source\":\"pr\"}");

        var probe = new FakePeerInstallProbe(new Dictionary<string, PeerProbeResult>
        {
            // Older peer using --version fallback: version only, no channel.
            [binary] = new PeerProbeResult.Ok(new InstallationInfo
            {
                Path = binary,
                Version = "13.4.0-pr.12345.gabcdef",
                Status = InstallationInfoStatus.Ok,
            }),
        });

        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(probe);
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        var prRow = results.Single(r =>
            string.Equals(r.CanonicalPath, binary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
        Assert.Equal("pr-12345", prRow.Channel);
        Assert.Equal("pr", prRow.Route);
        Assert.Equal("13.4.0-pr.12345.gabcdef", prRow.Version);
    }

    [Fact]
    public async Task DiscoverAllAsync_PrRoute_DoesNotOverwritePeerReportedChannel()
    {
        // When the peer DOES report a channel (i.e., it has the new
        // `info` command), the discovery layer must not overwrite it
        // with the path-derived value, even if they happen to match.
        // This guards against a bug where overlay logic assumes channel
        // is always missing on the fallback path.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var prDir = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire", "dogfood", "pr-12345", "bin");
        Directory.CreateDirectory(prDir);
        var binary = WriteFakeBinary(prDir);
        File.WriteAllText(Path.Combine(prDir, InstallSidecarReader.SidecarFileName), "{\"source\":\"pr\"}");

        var probe = new FakePeerInstallProbe(new Dictionary<string, PeerProbeResult>
        {
            [binary] = new PeerProbeResult.Ok(new InstallationInfo
            {
                Path = binary,
                Version = "13.4.0-pr.12345.gabcdef",
                Channel = "pr-12345-from-peer", // intentionally distinct
                Status = InstallationInfoStatus.Ok,
            }),
        });

        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(probe);
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        var prRow = results.Single(r =>
            string.Equals(r.CanonicalPath, binary, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal));
        Assert.Equal("pr-12345-from-peer", prRow.Channel);
    }

    [Theory]
    [InlineData("pr-")]              // empty PR number suffix
    [InlineData("pr-not-digits")]    // non-digit suffix
    [InlineData("pull-12345")]       // wrong prefix
    public void TryDerivePrChannel_RejectsMalformedPrLabels(string labelName)
    {
        // We only synthesize a channel when the directory name strictly
        // matches `pr-<digits>`; anything else (custom --install-path
        // installs, manual layouts, future label shapes) returns null so
        // we don't surface a misleading channel string.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var binary = Path.Combine(workspace.WorkspaceRoot.FullName, "dogfood", labelName, "bin", "aspire");
        Directory.CreateDirectory(Path.GetDirectoryName(binary)!);

        var derived = InstallationDiscovery.TryDerivePrChannel(binary);
        Assert.Null(derived);
    }

    [Fact]
    public void TryDerivePrChannel_RejectsNonDogfoodGrandparent()
    {
        // The grandparent dir must literally be `dogfood` — anything else
        // (e.g., `~/.aspire/staging/pr-1/bin`) is not the conventional
        // PR-script layout and we shouldn't synthesize a channel from it.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var binary = Path.Combine(workspace.WorkspaceRoot.FullName, "staging", "pr-1234", "bin", "aspire");
        Directory.CreateDirectory(Path.GetDirectoryName(binary)!);

        var derived = InstallationDiscovery.TryDerivePrChannel(binary);
        Assert.Null(derived);
    }

    [Fact]
    public void TryDerivePrChannel_AcceptsValidDogfoodLayout()
    {
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var binary = Path.Combine(workspace.WorkspaceRoot.FullName, "dogfood", "pr-9876", "bin", "aspire");

        var derived = InstallationDiscovery.TryDerivePrChannel(binary);
        Assert.Equal("pr-9876", derived);
    }

    [Fact]
    public async Task DiscoverAllAsync_LogsTrustGateRejection_AtDebugLevel()
    {
        // When the trust gate rejects a candidate (no sidecar), the user
        // should see WHY in --log-level debug output. Without this, an
        // install that "doesn't show up correctly" in `aspire info --all`
        // is hard to diagnose.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var pathDir = Path.Combine(workspace.WorkspaceRoot.FullName, "untrusted-bin");
        Directory.CreateDirectory(pathDir);
        var untrustedBinary = WriteFakeBinary(pathDir);

        using var _ = new EnvVarOverride("PATH", pathDir + Path.PathSeparator + (Environment.GetEnvironmentVariable("PATH") ?? string.Empty));
        using var __ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var ___ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var capturedLog = new CapturingLogger<InstallationDiscovery>();
        var discovery = new InstallationDiscovery(
            channelReader: new FakeIdentityChannelReader("local"),
            sidecarReader: new InstallSidecarReader(),
            peerProbe: new FakePeerInstallProbe(),
            logger: capturedLog);

        await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        Assert.Contains(capturedLog.Entries, e =>
            e.Level == LogLevel.Debug &&
            e.Message.Contains("no .aspire-install.json sidecar", StringComparison.Ordinal) &&
            e.Message.Contains("trust gate", StringComparison.Ordinal));
    }

    [Fact]
    public async Task DiscoverAllAsync_LogsDogfoodDirectoryWithoutBinary_AtDebugLevel()
    {
        // A stale ~/.aspire/dogfood/pr-N directory without a bin/aspire
        // inside (failed install, partial uninstall, manual mucking) is
        // worth flagging in debug output so the user can correlate.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        var staleDogfoodDir = Path.Combine(workspace.WorkspaceRoot.FullName, ".aspire", "dogfood", "pr-9999");
        Directory.CreateDirectory(staleDogfoodDir); // exists, but no bin/aspire inside

        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var capturedLog = new CapturingLogger<InstallationDiscovery>();
        var discovery = new InstallationDiscovery(
            channelReader: new FakeIdentityChannelReader("local"),
            sidecarReader: new InstallSidecarReader(),
            peerProbe: new FakePeerInstallProbe(),
            logger: capturedLog);

        await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        Assert.Contains(capturedLog.Entries, e =>
            e.Level == LogLevel.Debug &&
            e.Message.Contains(staleDogfoodDir, StringComparison.Ordinal) &&
            e.Message.Contains("not classifying as a real install", StringComparison.Ordinal));
    }

    [Fact]
    public async Task DiscoverAllAsync_RunningCliIsAlwaysFirst()
    {
        // Self must appear first regardless of what walks find — both for
        // the table display contract ("(current)" marker) and to keep peer
        // dedup deterministic.
        using var workspace = TemporaryWorkspace.Create(outputHelper);
        using var _ = new EnvVarOverride("HOME", workspace.WorkspaceRoot.FullName);
        using var __ = new EnvVarOverride("USERPROFILE", workspace.WorkspaceRoot.FullName);

        var discovery = NewDiscovery(new FakePeerInstallProbe());
        var results = await discovery.DiscoverAllAsync(TestContext.Current.CancellationToken);

        Assert.NotEmpty(results);
        Assert.Equal(InstallationInfoStatus.Ok, results[0].Status);
        var canonicalSelf = ResolveCanonicalProcessPath();
        Assert.Equal(canonicalSelf, results[0].CanonicalPath, StringComparer.OrdinalIgnoreCase);
    }

    private static string ResolveCanonicalProcessPath()
    {
        var path = Environment.ProcessPath!;
        try
        {
            var resolved = File.ResolveLinkTarget(path, returnFinalTarget: true);
            return resolved?.FullName ?? Path.GetFullPath(path);
        }
        catch (IOException)
        {
            return Path.GetFullPath(path);
        }
    }

    private static InstallationDiscovery NewDiscovery(FakePeerInstallProbe probe)
    {
        return new InstallationDiscovery(
            channelReader: new FakeIdentityChannelReader("local"),
            sidecarReader: new InstallSidecarReader(),
            peerProbe: probe,
            logger: NullLogger<InstallationDiscovery>.Instance);
    }

    /// <summary>
    /// Writes a stub "binary" file to disk. The discovery walk only checks
    /// existence; it never executes — the FakePeerInstallProbe handles
    /// what would have been the spawn.
    /// </summary>
    private static string WriteFakeBinary(string dir)
    {
        var name = OperatingSystem.IsWindows() ? "aspire.exe" : "aspire";
        var path = Path.Combine(dir, name);
        File.WriteAllBytes(path, [0x00]); // existence is what matters
        if (!OperatingSystem.IsWindows())
        {
            // Match shell semantics: a non-executable file is not a binary on PATH.
            // PathLookupHelper.FindFullPathFromPath honors the executable bit on Unix,
            // so without this chmod the PATH walk would skip the test fixture.
            File.SetUnixFileMode(path,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
                | UnixFileMode.GroupRead | UnixFileMode.GroupExecute
                | UnixFileMode.OtherRead | UnixFileMode.OtherExecute);
        }
        return path;
    }
}

/// <summary>
/// In-memory logger that records every log call so tests can assert
/// on the structured rejection messages emitted by InstallationDiscovery.
/// </summary>
internal sealed class CapturingLogger<T> : ILogger<T>
{
    public List<(LogLevel Level, string Message)> Entries { get; } = new();

    public IDisposable BeginScope<TState>(TState state) where TState : notnull => NullScope.Instance;
    public bool IsEnabled(LogLevel logLevel) => true;
    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
    {
        Entries.Add((logLevel, formatter(state, exception)));
    }

    private sealed class NullScope : IDisposable
    {
        public static readonly NullScope Instance = new();
        public void Dispose() { }
    }
}

/// <summary>
/// Restores an environment variable to its prior value on dispose. Used
/// in DiscoverAll tests to point the discovery walk at a controlled
/// <c>HOME</c> / <c>USERPROFILE</c> / <c>PATH</c> sandbox.
/// </summary>
internal sealed class EnvVarOverride : IDisposable
{
    private readonly string _name;
    private readonly string? _previous;

    public EnvVarOverride(string name, string? value)
    {
        _name = name;
        _previous = Environment.GetEnvironmentVariable(name);
        Environment.SetEnvironmentVariable(name, value);
    }

    public void Dispose()
    {
        Environment.SetEnvironmentVariable(_name, _previous);
    }
}

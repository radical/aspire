// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Buffers;
using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Aspire.Cli.Utils;
using Microsoft.Extensions.Logging;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Default <see cref="IPeerInstallProbe"/>. Spawns the peer, in order, with
/// <c>--info --self --format json</c> (current self-describe contract),
/// <c>doctor --self --format json</c> (legacy compatibility fallback for peers
/// that predate <c>--info --self</c>), and <c>--version</c> (compatibility
/// floor for peers that predate both self-describe contracts). All three
/// attempts share a single wall-clock timeout budget, capture stdout up to a
/// byte cap, and kill the entire process tree on timeout so a hung peer
/// cannot survive past the parent's lifetime.
/// </summary>
/// <remarks>
/// Uses <see cref="Process"/> directly rather than the project's
/// <c>IProcessExecutionFactory</c> because the latter's cancellation
/// semantics await <see cref="Process.WaitForExitAsync(CancellationToken)"/>
/// directly: on cancellation, the await throws before any kill branch can
/// run, leaving the peer alive. The peer-probe contract requires the kill
/// to actually fire.
/// </remarks>
internal sealed class PeerInstallProbe : IPeerInstallProbe
{
    /// <summary>
    /// Maximum wall-clock time we wait for a peer to respond, shared across
    /// all compatibility attempts (see <see cref="ProbeAsync"/>).
    /// </summary>
    /// <remarks>
    /// 5 seconds is a generous budget for a native-AOT CLI to start, read
    /// its assembly metadata, write 1 KB of JSON, and exit. A peer slower
    /// than that is almost certainly broken; faster than that is the norm.
    /// This is a single shared budget, not a per-attempt one: <see cref="ProbeAsync"/>
    /// tries up to three invocations of the peer, and a peer that stalls on
    /// the first one cannot buy itself extra wall-clock time by having later
    /// attempts "reset" the clock — see <see cref="SpawnWithBudgetAsync"/>.
    /// </remarks>
    internal static readonly TimeSpan s_defaultTimeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Maximum captured-output budget per stream. A misbehaving peer that spams
    /// its stdout or stderr cannot allocate unbounded memory in the parent.
    /// 1 MiB is far more than the well-behaved JSON shape (~200 bytes per
    /// install) needs.
    /// </summary>
    /// <remarks>
    /// The cap is applied to the raw byte stream from each pipe and the
    /// captured bytes are decoded as UTF-8 once at the end. Both stdout and
    /// stderr are forced to UTF-8 on the spawn (see <c>StandardOutputEncoding</c>
    /// / <c>StandardErrorEncoding</c>) so the decode matches the wire shape.
    /// </remarks>
    internal const int OutputCap = 1 * 1024 * 1024;

    private readonly TimeSpan _timeout;
    private readonly ILogger<PeerInstallProbe> _logger;

    public PeerInstallProbe(ILogger<PeerInstallProbe> logger)
        : this(s_defaultTimeout, logger)
    {
    }

    internal PeerInstallProbe(TimeSpan timeout, ILogger<PeerInstallProbe> logger)
    {
        ArgumentNullException.ThrowIfNull(logger);
        _timeout = timeout;
        _logger = logger;
    }

    /// <summary>
    /// `--info --self --format json`: the current self-describe contract.
    /// Emits a bare JSON array (see <c>InstallationInfoOutput</c>) with one
    /// row using <c>source</c> for the install route.
    /// </summary>
    private static readonly string[] s_infoArgs = ["--info", "--self", "--format", "json"];

    /// <summary>
    /// `doctor --self --format json`: the legacy self-describe contract,
    /// kept as a compatibility fallback for peers built before <c>--info --self</c>
    /// existed. Wraps the same row shape inside an <c>installations</c>
    /// envelope alongside health checks, and uses <c>route</c> for the
    /// install route.
    /// </summary>
    private static readonly string[] s_doctorArgs = ["doctor", "--self", "--format", "json"];

    /// <summary>
    /// `--version`: the compatibility floor. Supported by every Aspire CLI
    /// build ever shipped, but reports only a version string.
    /// </summary>
    private static readonly string[] s_versionArgs = ["--version"];

    /// <inheritdoc />
    public async Task<PeerProbeResult> ProbeAsync(string binaryPath, CancellationToken cancellationToken)
    {
        if (string.IsNullOrEmpty(binaryPath) || !File.Exists(binaryPath))
        {
            return new PeerProbeResult.Failed("Binary not found.");
        }

        // All three attempts below share this single stopwatch-based budget:
        // the total wall-clock time we wait for ANY usable answer from this
        // peer is `_timeout`, not `_timeout` per attempt. See
        // SpawnWithBudgetAsync.
        var budget = Stopwatch.StartNew();

        // Attempt 1: ask the peer to self-describe via `--info --self --format json`.
        // `--self` is required: without it the peer would run a full discovery
        // walk and probe back into us (and into every other peer it finds),
        // turning a single discovery invocation into a recursive fan-out
        // bounded only by the per-level timeout. `--format json` is
        // required so the peer emits a machine-readable row (the human
        // table layout is the default when `--format` is omitted).
        var info = await SpawnWithBudgetAsync(binaryPath, s_infoArgs, budget, cancellationToken).ConfigureAwait(false);
        if (info.Cancelled)
        {
            cancellationToken.ThrowIfCancellationRequested();
        }

        if (info.ExitCode == 0 && TryParseRichProbeResult(binaryPath, info.Stdout, out var infoResult))
        {
            return new PeerProbeResult.Ok(infoResult);
        }

        // Attempt 2: `doctor --self --format json`, the legacy self-describe
        // contract for peers built before `--info --self` existed. We reach
        // here for the same reasons attempt 1 can fail (non-zero exit, no
        // stdout, unparseable/wrong-shape JSON) as well as the peer simply
        // predating `--info --self` (System.CommandLine rejects the unknown
        // option and the peer exits non-zero).
        var doctor = await SpawnWithBudgetAsync(binaryPath, s_doctorArgs, budget, cancellationToken).ConfigureAwait(false);
        if (doctor.Cancelled)
        {
            cancellationToken.ThrowIfCancellationRequested();
        }

        if (doctor.ExitCode == 0 && TryParseRichProbeResult(binaryPath, doctor.Stdout, out var doctorResult))
        {
            return new PeerProbeResult.Ok(doctorResult);
        }

        // Attempt 3: `--version`, the compatibility floor. Peers this old
        // can't report their channel/route here, but `InstallationDiscovery`
        // recovers `pr-<N>` from the reported informational version string
        // so the user-facing table still shows the channel for PR builds.
        var version = await SpawnWithBudgetAsync(binaryPath, s_versionArgs, budget, cancellationToken).ConfigureAwait(false);
        if (version.Cancelled)
        {
            cancellationToken.ThrowIfCancellationRequested();
        }

        if (version.ExitCode == 0)
        {
            var versionLine = ExtractVersionLine(version.Stdout);
            if (!string.IsNullOrEmpty(versionLine))
            {
                // Partial install details: version only. Route is overlaid by
                // InstallationDiscovery from the locally-readable sidecar.
                // Channel intentionally null — we can't read assembly
                // metadata from outside an AOT binary, and the older peer
                // has no surface that exposes its channel.
                return new PeerProbeResult.Ok(new InstallationInfo
                {
                    Path = binaryPath,
                    Version = versionLine,
                    Status = InstallationInfoStatus.Ok,
                });
            }
        }

        // All three attempts failed to produce a usable result. Describe the
        // doctor-stage result: `doctor --self` is the most broadly-supported
        // rich self-describe contract we tried, so its failure reason
        // (non-zero exit, captured stderr, or a shared-budget timeout) is the
        // most useful single reason to surface. The `--info` attempt's own
        // failure is intentionally not surfaced separately: for peers old
        // enough to not support `--info --self`, it's just "unrecognized
        // option" noise, and for peers that do support it, a failure there
        // almost always means the doctor attempt right after it fails
        // identically.
        return new PeerProbeResult.Failed(DescribeFailure(doctor));
    }

    /// <summary>
    /// Runs <paramref name="arguments"/> against the peer, provided the
    /// shared per-peer <see cref="_timeout"/> budget has time left. Computes
    /// the remaining slice of the budget from <paramref name="budget"/> (a
    /// stopwatch started once at the top of <see cref="ProbeAsync"/>) so
    /// three sequential attempts cannot each claim a fresh <see cref="_timeout"/>
    /// worth of wall-clock time. If the budget is already exhausted, returns
    /// a synthetic timeout failure without starting another process — a
    /// peer that stalls the first attempt cannot buy a second and third full
    /// budget by having later attempts "reset" the clock.
    /// </summary>
    private async Task<SpawnResult> SpawnWithBudgetAsync(string binaryPath, string[] arguments, Stopwatch budget, CancellationToken cancellationToken)
    {
        var remaining = _timeout - budget.Elapsed;
        if (remaining <= TimeSpan.Zero)
        {
            return new SpawnResult(
                ExitCode: -1,
                Stdout: string.Empty,
                Stderr: string.Empty,
                StderrTruncated: false,
                Failure: $"Peer probe timed out after {_timeout.TotalSeconds:F1}s.",
                Cancelled: false);
        }

        return await SpawnAndCaptureAsync(binaryPath, arguments, remaining, cancellationToken).ConfigureAwait(false);
    }

    private bool TryParseRichProbeResult(string binaryPath, string stdout, out InstallationInfo info)
    {
        info = null!;
        if (string.IsNullOrWhiteSpace(stdout))
        {
            _logger.LogDebug("Peer probe at {BinaryPath} produced no rich JSON output.", binaryPath);
            return false;
        }

        try
        {
            using var doc = JsonDocument.Parse(stdout);

            JsonElement? row = null;
            if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                doc.RootElement.TryGetProperty("installations", out var installations) &&
                installations.ValueKind == JsonValueKind.Array &&
                installations.GetArrayLength() > 0)
            {
                row = installations[0];
            }
            else if (doc.RootElement.ValueKind == JsonValueKind.Array && doc.RootElement.GetArrayLength() > 0)
            {
                row = doc.RootElement[0];
            }

            // The first element MUST be a JSON object before we hand it to
            // InstallationInfoParser. TryGetProperty (which the parser calls)
            // throws InvalidOperationException for non-object kinds (e.g. [1],
            // [null], [[]]). Treat anything else as a wrong-shape response and
            // fall through to the --version fallback rather than aborting the
            // whole discovery walk for the caller.
            if (row is { ValueKind: JsonValueKind.Object } element)
            {
                info = InstallationInfoParser.Parse(element);
                return true;
            }

            _logger.LogDebug("Peer probe at {BinaryPath} returned JSON without an installation row; trying the --version fallback.", binaryPath);
            return false;
        }
        catch (JsonException ex)
        {
            _logger.LogDebug(ex, "Peer probe at {BinaryPath} returned invalid JSON; trying the --version fallback.", binaryPath);
            return false;
        }
    }

    /// <summary>
    /// Spawns the peer with the given arguments and captures stdout under
    /// the timeout / kill-on-timeout / stdout-cap contract. Returns a
    /// structured result describing exit code, captured output, and any
    /// transport-level failure (process couldn't start, etc.).
    /// </summary>
    /// <param name="binaryPath">Path to the peer executable to spawn.</param>
    /// <param name="arguments">Arguments to pass to the peer.</param>
    /// <param name="timeout">
    /// The wall-clock budget for THIS attempt, computed by
    /// <see cref="SpawnWithBudgetAsync"/> as the remaining slice of the
    /// shared per-peer <see cref="_timeout"/>. Deliberately distinct from
    /// <see cref="_timeout"/> itself, which is used only to compose the
    /// timeout failure message below: the message always describes the
    /// total per-peer budget the caller configured, not the (possibly much
    /// smaller) remaining slice this particular attempt got — a "timed out
    /// after 0.3s" message would be misleading if the peer actually got the
    /// full 5s budget spread across three attempts.
    /// </param>
    /// <param name="cancellationToken">Propagated to the process wait/kill and capture paths.</param>
    private async Task<SpawnResult> SpawnAndCaptureAsync(string binaryPath, string[] arguments, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = binaryPath,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            // Force UTF-8 decoding so a peer running under a non-UTF-8 console code page
            // (e.g. legacy Windows CP1252) doesn't produce replacement characters when
            // its stderr is folded into the failure reason. Aspire CLI peers in scope
            // emit UTF-8 by default, so this aligns the decoder with the actual byte
            // shape on the wire.
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var arg in arguments)
        {
            startInfo.ArgumentList.Add(arg);
        }

        // Strip ASPIRE_CLI_* identity overrides before launching the peer.
        // These env vars exist so a developer or test bench can coerce the
        // *current* CLI process into pretending it is a different channel /
        // version / commit, or to retarget its emitted nuget.config at a
        // local proxy. Inheriting them into the peer would invert the meaning
        // of `aspire doctor`: the doctor would observe its own override
        // applied to every peer it inspects and report a false uniformity
        // across installs. The peer should reflect what it *is on disk*, not
        // what the parent process was told to pretend to be. See
        // docs/specs/cli-identity-sidecar.md.
        foreach (var envVarName in IdentityResolver.IdentityEnvVarNames)
        {
            startInfo.Environment.Remove(envVarName);
        }

        var result = await ProcessCaptureRunner.RunAsync(
            startInfo,
            timeout,
            CapturePeerOutputAsync,
            static () => new PeerProcessOutput(string.Empty, string.Empty, StderrTruncated: false),
            _logger,
            cancellationToken).ConfigureAwait(false);

        var failure = result.FailureKind switch
        {
            ProcessCaptureFailureKind.StartFailed => result.FailureMessage is { Length: > 0 } message
                ? $"Could not start peer process: {message}"
                : "Could not start peer process.",
            ProcessCaptureFailureKind.CaptureFailed => result.FailureMessage is { Length: > 0 } message
                ? $"Could not capture peer process output: {message}"
                : "Could not capture peer process output.",
            // See the `timeout` parameter doc comment above: this always
            // reports the total shared per-peer budget, not the remaining
            // slice passed to ProcessCaptureRunner for this attempt.
            ProcessCaptureFailureKind.TimedOut => $"Peer probe timed out after {_timeout.TotalSeconds:F1}s.",
            _ => null,
        };

        return new SpawnResult(
            ExitCode: result.ExitCode,
            Stdout: result.Capture.Stdout,
            Stderr: result.Capture.Stderr,
            StderrTruncated: result.Capture.StderrTruncated,
            Failure: failure,
            Cancelled: result.Cancelled);
    }

    /// <summary>
    /// Composes a user-facing reason from the doctor-stage result when all
    /// three probe attempts fail. Always appends a "(and --version
    /// fallback)" suffix: by the time this is called, the info attempt has
    /// already failed too, but the version attempt is the only one that ran
    /// AFTER the result this message describes.
    /// </summary>
    private static string DescribeFailure(SpawnResult doctor)
    {
        const string suffix = " (and --version fallback)";
        if (doctor.Failure is { } reason)
        {
            return FoldStderrIntoReason(reason + suffix, doctor);
        }
        if (doctor.ExitCode != 0)
        {
            return FoldStderrIntoReason($"Peer exited with code {doctor.ExitCode}{suffix}.", doctor);
        }
        return FoldStderrIntoReason($"Peer produced no usable output{suffix}.", doctor);
    }

    /// <summary>
    /// Pulls the first non-blank line out of <c>aspire --version</c>
    /// output. Older Aspire CLI versions emit just the bare version
    /// string; newer versions may add a banner, in which case the first
    /// non-blank line still holds the version.
    /// </summary>
    private static string? ExtractVersionLine(string stdout)
    {
        foreach (var raw in stdout.Split('\n'))
        {
            var trimmed = raw.Trim();
            if (trimmed.Length == 0)
            {
                continue;
            }
            return trimmed;
        }
        return null;
    }

    private static string FoldStderrIntoReason(string reason, SpawnResult result)
    {
        var stderr = SanitizeStderr(result.Stderr);
        if (string.IsNullOrEmpty(stderr))
        {
            return reason;
        }

        if (result.StderrTruncated)
        {
            stderr += "... [truncated]";
        }

        return string.IsNullOrEmpty(reason)
            ? stderr
            : $"{reason}; stderr: {stderr}";
    }

    private readonly record struct SpawnResult(int ExitCode, string Stdout, string Stderr, bool StderrTruncated, string? Failure, bool Cancelled);

    private readonly record struct PeerProcessOutput(string Stdout, string Stderr, bool StderrTruncated);

    private readonly record struct CappedOutput(string Text, bool Truncated);

    private static async Task<PeerProcessOutput> CapturePeerOutputAsync(Process process, CancellationToken cancellationToken)
    {
        var readStdoutTask = ReadCappedAsync(process.StandardOutput.BaseStream, OutputCap, cancellationToken);
        var readStderrTask = ReadCappedAsync(process.StandardError.BaseStream, OutputCap, cancellationToken);

        var stdout = await SwallowAsync(readStdoutTask).ConfigureAwait(false);
        var stderr = await SwallowAsync(readStderrTask).ConfigureAwait(false);

        return new PeerProcessOutput(stdout.Text, stderr.Text, stderr.Truncated);
    }

    /// <summary>
    /// Reads <paramref name="stream"/> into a pooled buffer until EOF or
    /// <paramref name="cap"/> bytes have been captured, whichever comes
    /// first. Past the cap the loop keeps draining the pipe so the peer
    /// doesn't block on a full pipe; trailing bytes are discarded and the
    /// returned <see cref="CappedOutput.Truncated"/> flag is set. The cap
    /// exists so a peer spamming output cannot make the parent allocate
    /// unbounded memory.
    /// </summary>
    private static async Task<CappedOutput> ReadCappedAsync(Stream stream, int cap, CancellationToken cancellationToken)
    {
        using var output = new MemoryStream(capacity: Math.Min(cap, 4096));
        var buffer = ArrayPool<byte>.Shared.Rent(4096);
        var truncated = false;
        try
        {
            while (true)
            {
                int read;
                try
                {
                    read = await stream.ReadAsync(buffer.AsMemory(), cancellationToken).ConfigureAwait(false);
                }
                // OperationCanceledException is swallowed alongside the I/O exceptions
                // because cancellation is owned by the process-kill path in
                // ProcessCaptureRunner; the reader's job is just to stop pulling and
                // surface whatever was captured so far.
                catch (Exception ex) when (ex is IOException or OperationCanceledException or ObjectDisposedException)
                {
                    break;
                }

                if (read == 0)
                {
                    break;
                }

                var remaining = cap - (int)output.Length;
                if (remaining <= 0)
                {
                    truncated = true;
                    continue;
                }

                var toWrite = Math.Min(read, remaining);
                output.Write(buffer, 0, toWrite);
                if (toWrite < read)
                {
                    truncated = true;
                }
            }
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer);
        }

        return new CappedOutput(
            Encoding.UTF8.GetString(output.GetBuffer().AsSpan(0, (int)output.Length)),
            truncated);
    }

    private static string SanitizeStderr(string stderr)
    {
        // The byte cap is applied before sanitization so raw peer output is
        // always bounded; the truncation marker is appended after stripping.
        if (string.IsNullOrEmpty(stderr))
        {
            return string.Empty;
        }

        var builder = new StringBuilder(stderr.Length);
        for (var i = 0; i < stderr.Length; i++)
        {
            var ch = stderr[i];
            if (ch == '\u001b')
            {
                if (i + 1 < stderr.Length && stderr[i + 1] == '[')
                {
                    i += 2;
                    while (i < stderr.Length && (stderr[i] < '@' || stderr[i] > '~'))
                    {
                        i++;
                    }
                }
                continue;
            }

            if (char.IsControl(ch) && ch != '\n')
            {
                continue;
            }

            builder.Append(ch);
        }

        return builder.ToString().Trim();
    }

    private static async Task<CappedOutput> SwallowAsync(Task<CappedOutput> task)
    {
        try
        {
            return await task.ConfigureAwait(false);
        }
        catch
        {
            // Reader is being torn down alongside the killed process —
            // any exception here is uninteresting noise.
            return new CappedOutput(string.Empty, Truncated: false);
        }
    }

}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;

namespace KnownTestFailures;

public static class GitRegistry
{
    public const string ManagedPath = "registry.json";

    public static async Task<RegistryInput> FetchBranchAsync(
        string repository, string remote, string sourceRef, string trackingRef, Action<RegistrySource, string?> recordFetched)
    {
        Format.Text(remote, "remote");
        Format.FullRef(sourceRef);
        Format.FullRef(trackingRef);
        Format.Require(sourceRef.StartsWith("refs/heads/", StringComparison.Ordinal) &&
            trackingRef.StartsWith("refs/remotes/", StringComparison.Ordinal), "Fetch requires a branch and a remote-tracking ref.");
        await RequireDirectRefAsync(repository, trackingRef).ConfigureAwait(false);
        // Never read a previously fetched revision after fetch fails.
        await RunAsync(repository, ["fetch", "--no-tags", "--force", "--", remote, $"+{sourceRef}:{trackingRef}"]).ConfigureAwait(false);
        await RequireDirectRefAsync(repository, trackingRef).ConfigureAwait(false);
        var commit = (await RunAsync(repository, ["rev-parse", "--verify", trackingRef + "^{commit}"]).ConfigureAwait(false)).Trim();
        recordFetched(new("git-fetched", ManagedPath, commit), null);
        return await ReadCommitAsync(repository, commit, ManagedPath, recordFetched).ConfigureAwait(false);
    }

    public static RegistryInput ReadLocal(string path)
    {
        var json = File.ReadAllText(path);
        var registry = Format.Read<Registry>(json);
        RegistryHistory.Replay(registry);
        return new(registry, new("local-untrusted", System.IO.Path.GetFullPath(path)), json);
    }

    public static async Task<RegistryInput> ReadBranchAsync(string repository, string fullRef, string path)
    {
        await ValidateRefAsync(repository, fullRef).ConfigureAwait(false);
        await RequireDirectRefAsync(repository, fullRef).ConfigureAwait(false);
        Format.Path(path, "registry path");
        var commit = (await RunAsync(repository, ["rev-parse", "--verify", fullRef + "^{commit}"]).ConfigureAwait(false)).Trim();
        return await ReadCommitAsync(repository, commit, path, null).ConfigureAwait(false);
    }

    public static async Task<string> MutateAsync(
        string repository, string fullRef, string expectedCommit, bool initialize, RegistryEvent mutation)
    {
        await ValidateRefAsync(repository, fullRef).ConfigureAwait(false);
        Format.Require(fullRef.StartsWith("refs/heads/", StringComparison.Ordinal), "Management writes only local refs/heads/ branches.");
        Format.Require(expectedCommit.Length is 40 or 64 && expectedCommit.All(char.IsAsciiHexDigit), "Expected revision must be a full object ID.");
        var zero = expectedCommit.All(c => c == '0');
        Format.Require(initialize == zero, "Only explicit initialization accepts an all-zero expected revision.");
        var worktrees = await RunAsync(repository, ["worktree", "list", "--porcelain", "-z"]).ConfigureAwait(false);
        Format.Require(!worktrees.Split('\0').Contains("branch " + fullRef, StringComparer.Ordinal),
            "Registry management requires a branch that is not checked out in any worktree.");
        // Reject symbolic refs rather than updating another branch or replacing a symbolic ref.
        // update-ref --no-deref also prevents following a symref introduced after this check.
        await RequireDirectRefAsync(repository, fullRef).ConfigureAwait(false);
        var exists = await RunResultAsync(repository, ["show-ref", "--verify", "--quiet", fullRef]).ConfigureAwait(false);
        if (zero)
        {
            Format.Require(exists.ExitCode == 1, "Initialization requires an absent branch.");
        }
        else
        {
            Format.Require(exists.ExitCode == 0 &&
                (await RunAsync(repository, ["show-ref", "--verify", "--hash", fullRef]).ConfigureAwait(false)).Trim() == expectedCommit,
                "Registry changed: expected revision does not match.");
        }
        var registry = zero ? new Registry(1, "experimental", []) :
            (await ReadCommitAsync(repository, expectedCommit, ManagedPath, null).ConfigureAwait(false)).Registry;
        var updated = RegistryHistory.Append(registry, mutation);
        var blob = (await RunAsync(repository, ["hash-object", "-w", "--stdin"], Format.Write(updated) + "\n").ConfigureAwait(false)).Trim();
        var treeEntries = zero ? "" : await RunAsync(repository, ["ls-tree", "-z", expectedCommit]).ConfigureAwait(false);
        // ls-tree/mktree -z use "100644 blob <oid>\t<filename>\0", including literal
        // tabs/newlines in filenames. Split only on NUL and inspect the first tab.
        var preserved = treeEntries.Split('\0', StringSplitOptions.RemoveEmptyEntries)
            .Where(line => line[(line.IndexOf('\t') + 1)..] != ManagedPath);
        var treeInput = string.Concat(preserved.Select(line => line + "\0")) + $"100644 blob {blob}\t{ManagedPath}\0";
        var tree = (await RunAsync(repository, ["mktree", "-z"], treeInput).ConfigureAwait(false)).Trim();
        var args = new List<string> { "commit-tree", tree };
        if (!zero)
        {
            args.AddRange(["-p", expectedCommit]);
        }
        var timestamp = mutation.At.ToString("O", System.Globalization.CultureInfo.InvariantCulture);
        var commit = (await RunAsync(repository, args,
            $"{mutation.Action} allowance {mutation.Id}\n\n{mutation.Reason}\n",
            new Dictionary<string, string>
            {
                ["GIT_AUTHOR_NAME"] = "Known test failures",
                ["GIT_AUTHOR_EMAIL"] = "known-test-failures@localhost",
                ["GIT_COMMITTER_NAME"] = "Known test failures",
                ["GIT_COMMITTER_EMAIL"] = "known-test-failures@localhost",
                ["GIT_AUTHOR_DATE"] = timestamp,
                ["GIT_COMMITTER_DATE"] = timestamp
            }).ConfigureAwait(false)).Trim();
        await RunAsync(repository, ["update-ref", "--no-deref", fullRef, commit, expectedCommit]).ConfigureAwait(false);
        return commit;
    }

    private static async Task<RegistryInput> ReadCommitAsync(string repository, string commit, string path,
        Action<RegistrySource, string?>? recordFetched)
    {
        var blob = (await RunAsync(repository, ["rev-parse", "--verify", $"{commit}:{path}"]).ConfigureAwait(false)).Trim();
        Format.Require((await RunAsync(repository, ["cat-file", "-t", blob]).ConfigureAwait(false)).Trim() == "blob", "Registry must be a blob.");
        var source = new RegistrySource(recordFetched is null ? "git-local" : "git-fetched", path, commit, blob);
        var json = await RunAsync(repository, ["show", $"{commit}:{path}"]).ConfigureAwait(false);
        // Record the fetched commit/blob and raw data even when parsing rejects it.
        recordFetched?.Invoke(source, json);
        var registry = Format.Read<Registry>(json);
        RegistryHistory.Replay(registry);
        return new(registry, source, json);
    }

    private static async Task ValidateRefAsync(string repository, string fullRef)
    {
        Format.FullRef(fullRef);
        await RunAsync(repository, ["check-ref-format", fullRef]).ConfigureAwait(false);
    }

    private static async Task RequireDirectRefAsync(string repository, string fullRef)
    {
        var symbolic = await RunResultAsync(repository, ["symbolic-ref", "-q", "--no-recurse", fullRef]).ConfigureAwait(false);
        Format.Require(symbolic.ExitCode == 1, "Registry branch must not be a symbolic ref.");
    }

    private static async Task<string> RunAsync(
        string repository, IReadOnlyList<string> arguments, string? input = null,
        IReadOnlyDictionary<string, string>? environment = null)
    {
        var result = await RunResultAsync(repository, arguments, input, environment).ConfigureAwait(false);
        Format.Require(result.ExitCode == 0, $"git {arguments[0]} failed ({result.ExitCode}): {result.Error.Trim()}");
        return result.Output;
    }

    private static async Task<(int ExitCode, string Output, string Error)> RunResultAsync(
        string repository, IReadOnlyList<string> arguments, string? input = null,
        IReadOnlyDictionary<string, string>? environment = null)
    {
        using var process = new Process();
        process.StartInfo = new("git")
        {
            WorkingDirectory = repository,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        process.StartInfo.ArgumentList.Add("--no-pager");
        // Git discovers hooks as children of core.hooksPath. Point it at the running
        // executable, an existing non-directory file, so no hook can exist beneath it.
        // This is the cross-platform equivalent of Git's documented /dev/null override,
        // without temporary files or changes to repository/global hook configuration.
        // https://git-scm.com/docs/git-config#Documentation/git-config.txt-corehooksPath
        var hooksDisabledPath = Environment.ProcessPath;
        Format.Require(hooksDisabledPath is not null && File.Exists(hooksDisabledPath),
            "Cannot disable Git hooks: the current executable path is unavailable.");
        process.StartInfo.ArgumentList.Add("-c");
        process.StartInfo.ArgumentList.Add("core.hooksPath=" + hooksDisabledPath.Replace(Path.DirectorySeparatorChar, '/'));
        foreach (var argument in arguments)
        {
            process.StartInfo.ArgumentList.Add(argument);
        }
        process.StartInfo.Environment["GIT_TERMINAL_PROMPT"] = "0";
        foreach (var (key, value) in environment ?? new Dictionary<string, string>())
        {
            process.StartInfo.Environment[key] = value;
        }
        process.Start();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(30));
        // Drain both streams while writing stdin: JSON and tree input can exceed pipe buffers.
        var stdout = process.StandardOutput.ReadToEndAsync(timeout.Token);
        var stderr = process.StandardError.ReadToEndAsync(timeout.Token);
        try
        {
            if (input is not null)
            {
                await process.StandardInput.WriteAsync(input.AsMemory(), timeout.Token).ConfigureAwait(false);
            }
            process.StandardInput.Close();
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            return (process.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
            await process.WaitForExitAsync().ConfigureAwait(false);
            throw new IOException("Git registry operation timed out.");
        }
    }
}

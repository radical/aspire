// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;

namespace KnownTestFailures;

public sealed record DiscoveredCase(string CaseId, string Method, string DisplayName);
public sealed record FixtureRun(int ExitCode, Evidence Evidence, string Directory);

/// <summary>
/// Fixed-contract, opt-in fixture proof, not a general-purpose test evidence producer.
/// </summary>
public static class ProofRunner
{
    public const string Project = "tools/KnownTestFailuresFixture/KnownTestFailuresFixture.csproj";
    public const string TestRef = "refs/heads/known-test-failure-allowances";
    public const string RegistryRef = "refs/heads/known-test-failures-registry-proof";
    public const string TrackingRef = "refs/remotes/known-failure-proof/registry";
    public const string Lane = "experimental-proof";
    public const string Message = "proof candidate fixed failure";
    private const string Class = "KnownTestFailuresFixture.Cases";
    private static readonly string[] s_methods = ["Candidate", "Neighbor", "Pass"];

    public static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter error)
    {
        string? directory = null;
        try
        {
            var allowed = args[0] switch
            {
                "gate" => new[] { "repo", "fixture", "output", "remote" },
                "prove" => ["repo", "fixture", "output"],
                "seed" => ["repo", "fixture", "output", "expires", "issue", "owner", "platform"],
                _ => throw new InvalidDataException("Unknown proof command.")
            };
            var options = new Dictionary<string, string>(StringComparer.Ordinal);
            for (var index = 1; index < args.Length; index += 2)
            {
                Format.Require(args[index].StartsWith("--", StringComparison.Ordinal) && index + 1 < args.Length,
                    "Proof commands require named option/value pairs.");
                var key = args[index][2..];
                Format.Require(allowed.Contains(key, StringComparer.Ordinal) && options.TryAdd(key, args[index + 1]),
                    $"Unknown or duplicate proof option '{key}'.");
            }
            string Required(string name) => options.GetValueOrDefault(name) ?? throw new InvalidDataException($"Missing --{name}.");
            var repo = Path.GetFullPath(Required("repo"));
            var fixture = Path.GetFullPath(Required("fixture"));
            var requestedDirectory = Path.GetFullPath(Required("output"));
            Format.Require(!Directory.Exists(requestedDirectory) && !File.Exists(requestedDirectory),
                "Proof output must be a new directory; stale artifacts are forbidden.");
            Directory.CreateDirectory(requestedDirectory);
            directory = requestedDirectory;
            if (args[0] == "seed")
            {
                var inventory = await DiscoverAsync(repo, fixture, directory).ConfigureAwait(false);
                var at = DateTimeOffset.UtcNow;
                var expires = Format.ParseUtc(Required("expires"), "expires");
                var content = Content(inventory, at, expires, options.GetValueOrDefault("platform", "linux-x64"),
                    Required("issue"), Required("owner"));
                var add = new RegistryEvent(1, "proof-candidate", "add", "experiment-author",
                    "Fixed-fixture experiment; actor assertions are not production authorization.", at, 1, content);
                RegistryHistory.Replay(new(1, "experimental", [add]));
                Save(directory, "add.json", add);
                await output.WriteLineAsync("Wrote unapproved add.json using repeated pre-execution discovery; no branch was changed.").ConfigureAwait(false);
                return 0;
            }
            if (args[0] == "prove")
            {
                return await ProveAsync(repo, fixture, directory, output).ConfigureAwait(false);
            }
            var run = await CaptureAsync(repo, fixture, directory, "candidate").ConfigureAwait(false);
            var remote = Required("remote");
            await output.WriteLineAsync($"MTP exit={run.ExitCode}; fetching fresh registry (no cached fallback).").ConfigureAwait(false);
            Save(directory, "fetch-request.json", new { remote, sourceRef = RegistryRef, trackingRef = TrackingRef });
            var input = await GitRegistry.FetchBranchAsync(repo, remote, RegistryRef, TrackingRef,
                (source, json) => SaveFetch(directory, source, json)).ConfigureAwait(false);
            var decision = Evaluate(run, input, DateTimeOffset.UtcNow);
            Save(directory, "decision.json", decision);
            await output.WriteLineAsync($"MTP exit={run.ExitCode}; effective exit={decision.EffectiveExitCode}; registry={input.Source.Commit}; status={decision.Status}").ConfigureAwait(false);
            return decision.EffectiveExitCode;
        }
        catch (Exception ex) when (ex is InvalidDataException or JsonException or IOException or UnauthorizedAccessException or
            ArgumentException or InvalidOperationException or KeyNotFoundException or XmlException or System.ComponentModel.Win32Exception)
        {
            if (directory is not null && Directory.Exists(directory))
            {
                Save(directory, "blocked.json", new { error = ex.Message, effectiveExitCode = Evaluator.BlockedExitCode });
            }
            await error.WriteLineAsync(ex.Message).ConfigureAwait(false);
            return Evaluator.BlockedExitCode;
        }
    }

    public static async Task<FixtureRun> CaptureAsync(string repo, string fixture, string directory, string scenario)
    {
        Format.Require(scenario is "pass" or "candidate" or "mixed" or "changed", "Unknown fixed fixture scenario.");
        Directory.CreateDirectory(directory);
        var inventory = await DiscoverAsync(repo, fixture, directory).ConfigureAwait(false);
        var process = await ExecuteAsync(fixture,
            ["--report-xunit-trx", "--report-xunit-trx-filename", "results.trx",
             "--report-xunit-xml", "--report-xunit-xml-filename", "lifecycle.xml",
             "--results-directory", directory, "--no-ansi", "--progress", "off", "--timeout", "30s"],
            repo, new Dictionary<string, string>
            {
                ["KNOWN_FAILURE_PROOF_OPT_IN"] = "true",
                ["KNOWN_FAILURE_PROOF_SCENARIO"] = scenario,
                ["DOTNET_ROOT"] = DotnetRoot(repo)
            }, directory, "execution").ConfigureAwait(false);
        Format.Require(process.ExitCode is 0 or 2 && process.Error.Length == 0,
            "Fixture process failed setup, timed out, or reported stderr.");
        var results = StrictTrx.Read(await File.ReadAllTextAsync(Path.Combine(directory, "results.trx")).ConfigureAwait(false));
        var completed = ValidateLifecycle(await File.ReadAllTextAsync(Path.Combine(directory, "lifecycle.xml")).ConfigureAwait(false), results);
        // Expected identities were persisted BEFORE execution. Only the per-run TRX GUID
        // is bound afterward, and only through a unique exact method + display match.
        var expected = inventory.Select(item =>
        {
            var matches = results.Where(r => r.Method == item.Method && r.DisplayName == item.DisplayName).ToArray();
            Format.Require(matches.Length == 1, "Discovered identity has no unique TRX binding.");
            return new ExpectedCase(item.CaseId, matches[0].TestId, item.Method, item.DisplayName);
        }).ToArray();
        Format.Require(results.Length == inventory.Length, "TRX includes missing/extra fixture cases.");
        var evidence = new Evidence(1, Lane, RuntimeInformation.RuntimeIdentifier, TestRef, ["fixture"],
            [new("fixture", Project, "results.trx", true, process.ExitCode, true, completed, [], expected)]);
        Save(directory, "evidence.json", evidence);
        return new(process.ExitCode, evidence, directory);
    }

    private static async Task<DiscoveredCase[]> DiscoverAsync(string repo, string fixture, string directory)
    {
        DiscoveredCase[]? inventory = null;
        for (var attempt = 1; attempt <= 2; attempt++)
        {
            var result = await ExecuteAsync(fixture, ["--list-tests", "json", "--no-ansi"], repo,
                new Dictionary<string, string>
                {
                    ["DOTNET_ROOT"] = DotnetRoot(repo),
                    ["KNOWN_FAILURE_PROOF_OPT_IN"] = "false"
                }, directory, $"discovery-{attempt}").ConfigureAwait(false);
            Format.Require(result.ExitCode == 0 && result.Error.Length == 0, "Fixture discovery did not complete successfully.");
            // MTP 2.4 --list-tests json:
            // {"schemaVersion":1,"tests":[{"uid":"...","displayName":"Namespace.Type.Method",
            // "type":{"namespace":"Namespace","typeName":"Type","methodName":"Method",...},...}]}.
            // https://learn.microsoft.com/dotnet/core/testing/microsoft-testing-platform-options
            using var json = JsonDocument.Parse(result.Output);
            var root = json.RootElement;
            Format.Require(root.GetProperty("schemaVersion").GetInt32() == 1, "Unsupported discovery schema.");
            var discovered = root.GetProperty("tests").EnumerateArray().Select(test =>
            {
                var type = test.GetProperty("type");
                Format.Require(type.GetProperty("namespace").GetString() == "KnownTestFailuresFixture" &&
                    type.GetProperty("typeName").GetString() == "Cases" &&
                    type.GetProperty("methodArity").GetInt32() == 0 &&
                    type.GetProperty("parameterTypeFullNames").GetArrayLength() == 0,
                    "Only the authored nonparameterized fixture is supported.");
                var method = Class + "." + type.GetProperty("methodName").GetString();
                var display = test.GetProperty("displayName").GetString()!;
                var uid = test.GetProperty("uid").GetString()!;
                Format.Text(uid, "discovered UID");
                Format.Require(display == method, "Fixture discovery display differs from authored method.");
                return new DiscoveredCase(uid, method, display);
            }).OrderBy(c => c.Method, StringComparer.Ordinal).ToArray();
            Format.Require(discovered.Select(c => c.Method).SequenceEqual(s_methods.Select(m => Class + "." + m)) &&
                discovered.Select(c => c.CaseId).Distinct(StringComparer.Ordinal).Count() == s_methods.Length,
                "Discovered inventory differs from the authored fixture.");
            Format.Require(inventory is null || inventory.SequenceEqual(discovered),
                "Fixture UIDs changed across separate discovery processes.");
            inventory = discovered;
        }
        Save(directory, "inventory-before-execution.json", inventory!);
        return inventory!;
    }

    public static bool ValidateLifecycle(string xml, TestResult[] results)
    {
        using var text = new StringReader(xml);
        using var reader = XmlReader.Create(text, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });
        var root = XDocument.Load(reader).Root ?? throw new InvalidDataException("Missing lifecycle report.");
        Format.Require(root.Name == "assemblies" && (string?)root.Attribute("schema-version") == "3" &&
            root.Elements().Count() == 1, "Unsupported lifecycle report.");
        var assembly = root.Element("assembly") ?? throw new InvalidDataException("Missing assembly lifecycle.");
        // xUnit's XML writer adds totals and finish-rtf from ITestAssemblyFinished,
        // not from a console success string. Incomplete or cleanup-error runs fail closed.
        // https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.runner.common/Results/Builtin/XmlV2ResultWriterMessageHandler.cs
        Format.Require(root.Attribute("finish-rtf") is not null && assembly.Attribute("finish-rtf") is not null &&
            (string?)assembly.Attribute("errors") == "0" && (string?)assembly.Attribute("not-run") == "0" &&
            (string?)assembly.Attribute("skipped") == "0" && !root.Descendants("errors").Any(),
            "Fixture assembly did not finish normally or reported framework errors.");
        Format.Require(assembly.Elements().All(c => c.Name == "collection" && c.Elements().All(t => t.Name == "test")),
            "Unsupported fixture lifecycle sections.");
        var tests = assembly.Elements("collection").SelectMany(c => c.Elements("test")).ToArray();
        Format.Require(tests.Length == results.Length &&
            (string?)assembly.Attribute("total") == results.Length.ToString(CultureInfo.InvariantCulture) &&
            (string?)assembly.Attribute("failed") == results.Count(r => r.Outcome == "Failed").ToString(CultureInfo.InvariantCulture) &&
            (string?)assembly.Attribute("passed") == results.Count(r => r.Outcome == "Passed").ToString(CultureInfo.InvariantCulture),
            "Lifecycle totals contradict TRX.");
        foreach (var result in results)
        {
            var matching = tests.Where(t => (string?)t.Attribute("type") + "." + (string?)t.Attribute("method") == result.Method &&
                (string?)t.Attribute("name") == result.DisplayName).ToArray();
            Format.Require(matching.Length == 1, "Lifecycle test identity is not unique.");
            var test = matching[0];
            Format.Require(test.Attribute("finish-rtf") is not null &&
                (string?)test.Attribute("result") == (result.Outcome == "Failed" ? "Fail" : "Pass") &&
                test.Elements().All(e => e.Name == "failure") &&
                test.Elements("failure").Count() == (result.Outcome == "Failed" ? 1 : 0) &&
                (string?)test.Element("failure")?.Element("message") == result.Message,
                "Lifecycle outcome/message contradicts TRX.");
            if (test.Element("failure") is { } failure)
            {
                Format.Require(failure.Elements().All(e => e.Name == "message" || e.Name == "stack-trace") &&
                    failure.Elements().All(e => !e.HasElements) &&
                    failure.Elements("message").Count() == 1 && failure.Elements("stack-trace").Count() <= 1,
                    "Unsupported lifecycle failure shape.");
            }
        }
        return true;
    }

    private static Decision Evaluate(FixtureRun run, RegistryInput input, DateTimeOffset at, string mode = "experimental") =>
        Evaluator.Evaluate(mode, run.ExitCode, at, true, () => input, () => run.Evidence,
            path => File.ReadAllText(Path.Combine(run.Directory, path)));

    private static Allowance Content(DiscoveredCase[] inventory, DateTimeOffset created, DateTimeOffset expires,
        string platform, string issue, string owner)
    {
        var candidate = inventory.Single(c => c.Method == Class + ".Candidate");
        return new(Project, candidate.Method, candidate.DisplayName, candidate.CaseId,
            Lane, platform, TestRef, Message, issue, owner, created, expires);
    }

    private static async Task<int> ProveAsync(string repo, string fixture, string directory, TextWriter output)
    {
        var baseline = await CaptureAsync(repo, fixture, Path.Combine(directory, "baseline"), "candidate").ConfigureAwait(false);
        var inventory = Format.Read<DiscoveredCase[]>(File.ReadAllText(Path.Combine(baseline.Directory, "inventory-before-execution.json")));
        // Explicit synthetic clock only for boundary scenarios; live gate always uses UtcNow.
        var at = DateTimeOffset.Parse("2026-10-21T00:00:00Z", CultureInfo.InvariantCulture);
        var content = Content(inventory, at.AddHours(-1), at.AddHours(1), RuntimeInformation.RuntimeIdentifier,
            "https://example.com/experimental-proof", "experimental-proof");
        RegistryEvent Event(string action, int sequence, int revision = 1, Allowance? value = null) =>
            new(sequence, "proof-candidate", action, "local-test-actor", "Test-only actor assertion.", at.AddMinutes(-30), revision, value);
        var add = Event("add", 1, value: content);
        var approve = Event("approve", 2);
        Registry History(params RegistryEvent[] events) => new(1, "experimental", events);
        var approved = History(add, approve);
        var scenarios = new (string Name, string Scenario, Registry Registry, int Raw, int Effective, string Status, string Mode, DateTimeOffset At)[]
        {
            ("pass", "pass", approved, 0, 0, "pass", "experimental", at),
            ("unapproved", "candidate", History(add), 2, 2, "blocked", "experimental", at),
            ("approved", "candidate", approved, 2, 0, "allowed", "experimental", at),
            ("mixed", "mixed", approved, 2, 2, "blocked", "experimental", at),
            ("changed-signature", "changed", approved, 2, 2, "blocked", "experimental", at),
            ("wrong-scope", "candidate", History(add with { Content = content with { Lane = "other" } }, approve), 2, 2, "blocked", "experimental", at),
            ("updated-unapproved", "candidate", History(add, approve, Event("update", 3, 2, content with { Owner = "updated-owner" })), 2, 2, "blocked", "experimental", at),
            ("reapproved", "candidate", History(add, approve, Event("update", 3, 2, content with { Owner = "updated-owner" }), Event("approve", 4, 2)), 2, 0, "allowed", "experimental", at),
            ("expiry-boundary", "candidate", approved, 2, 2, "blocked", "experimental", content.ExpiresAt),
            ("revoked", "candidate", History(add, approve, Event("revoke", 3)), 2, 2, "blocked", "experimental", at),
            ("removed", "candidate", History(add, approve, Event("remove", 3)), 2, 2, "blocked", "experimental", at),
            ("off", "candidate", approved, 2, 2, "off", "off", at),
            ("shadow", "candidate", approved, 2, 2, "allowed", "shadow", at)
        };
        var summaries = new List<object>();
        var relocated = Path.Combine(directory, "relocated-fixture");
        Directory.CreateDirectory(relocated);
        foreach (var file in Directory.GetFiles(Path.GetDirectoryName(fixture)!))
        {
            File.Copy(file, Path.Combine(relocated, Path.GetFileName(file)));
        }
        var portable = await CaptureAsync(repo, Path.Combine(relocated, Path.GetFileName(fixture)),
            Path.Combine(directory, "portable-identity"), "pass").ConfigureAwait(false);
        var portableInventory = Format.Read<DiscoveredCase[]>(File.ReadAllText(Path.Combine(portable.Directory, "inventory-before-execution.json")));
        Format.Require(inventory.SequenceEqual(portableInventory) && portable.ExitCode == 0,
            "Fixed fixture UIDs changed when the built executable moved to another directory.");
        summaries.Add(new { Name = "portable-identity", rawExit = portable.ExitCode, effectiveExit = 0, Status = "pass" });
        foreach (var item in scenarios)
        {
            var path = Path.Combine(directory, item.Name);
            var run = await CaptureAsync(repo, fixture, path, item.Scenario).ConfigureAwait(false);
            // Exercise branch mutation + an explicit fresh fetch, not a file-only replay.
            var remote = Path.Combine(path, "registry.git");
            Directory.CreateDirectory(remote);
            var init = await ExecuteAsync("git", ["--no-pager", "init", "--initial-branch=main", remote], repo, null, path, "registry-init").ConfigureAwait(false);
            Format.Require(init.ExitCode == 0, "Cannot initialize proof registry repository.");
            var revision = new string('0', 40);
            foreach (var mutation in item.Registry.History)
            {
                revision = await GitRegistry.MutateAsync(remote, RegistryRef, revision, revision.All(c => c == '0'), mutation).ConfigureAwait(false);
            }
            Save(path, "fetch-request.json", new { remote, sourceRef = RegistryRef, trackingRef = TrackingRef });
            // Use a separate local consumer repository so fixture refs never touch the user's checkout.
            var consumer = Path.Combine(path, "consumer.git");
            Directory.CreateDirectory(consumer);
            var consumerInit = await ExecuteAsync("git", ["--no-pager", "init", "--initial-branch=main", consumer], repo, null, path, "consumer-init").ConfigureAwait(false);
            Format.Require(consumerInit.ExitCode == 0, "Cannot initialize proof consumer repository.");
            var input = await GitRegistry.FetchBranchAsync(consumer, remote, RegistryRef, TrackingRef,
                (source, json) => SaveFetch(path, source, json)).ConfigureAwait(false);
            var decision = Evaluate(run, input, item.At, item.Mode);
            Save(path, "decision.json", decision);
            Format.Require(run.ExitCode == item.Raw && decision.EffectiveExitCode == item.Effective && decision.Status == item.Status,
                $"Scenario '{item.Name}' failed: raw={run.ExitCode}, effective={decision.EffectiveExitCode}, status={decision.Status}.");
            summaries.Add(new { item.Name, rawExit = run.ExitCode, effectiveExit = decision.EffectiveExitCode, decision.Status });
        }
        foreach (var name in new[] { "missing-registry", "malformed-registry" })
        {
            var path = Path.Combine(directory, name);
            Directory.CreateDirectory(path);
            var remote = Path.Combine(path, "source");
            var consumer = Path.Combine(path, "consumer");
            var init = await ExecuteAsync("git",
                ["--no-pager", "init", "--initial-branch=known-test-failures-registry-proof", remote],
                repo, null, path, "source-init").ConfigureAwait(false);
            Format.Require(init.ExitCode == 0, "Cannot initialize invalid-registry fixture.");
            var file = name == "malformed-registry" ? "registry.json" : "unrelated.txt";
            File.WriteAllText(Path.Combine(remote, file), name == "malformed-registry" ? "{invalid" : "No registry exists here.");
            // Invalid input cannot be created through mutate, which validates history.
            // These deliberately invalid local repositories exercise the real gate's
            // fresh-fetch/parse failure path without weakening management validation.
            var gitOptions = new[]
            {
                "--no-pager", "-c", "core.hooksPath=" + Environment.ProcessPath,
                "-c", "user.name=Proof fixture", "-c", "user.email=proof-fixture@localhost",
                "-c", "commit.gpgsign=false", "-C", remote
            };
            var stage = await ExecuteAsync("git", [.. gitOptions, "add", "--", file], repo, null, path, "source-stage").ConfigureAwait(false);
            var commit = await ExecuteAsync("git", [.. gitOptions, "commit", "--quiet", "-m", "Create invalid proof fixture"],
                repo, null, path, "source-commit").ConfigureAwait(false);
            var initializeConsumer = await ExecuteAsync("git", ["--no-pager", "init", "--initial-branch=main", consumer],
                repo, null, path, "consumer-init").ConfigureAwait(false);
            Format.Require(stage.ExitCode == 0 && commit.ExitCode == 0 && initializeConsumer.ExitCode == 0,
                "Cannot prepare invalid-registry fixture.");
            using var stdout = new StringWriter();
            using var stderr = new StringWriter();
            var gatePath = Path.Combine(path, "gate");
            var exit = await RunAsync(["gate", "--repo", consumer, "--fixture", fixture,
                "--output", gatePath, "--remote", remote], stdout, stderr).ConfigureAwait(false);
            File.WriteAllText(Path.Combine(path, "gate.stdout.txt"), stdout.ToString());
            File.WriteAllText(Path.Combine(path, "gate.stderr.txt"), stderr.ToString());
            using var raw = JsonDocument.Parse(File.ReadAllText(Path.Combine(gatePath, "execution.process.json")));
            Format.Require(raw.RootElement.GetProperty("exitCode").GetInt32() == 2 && exit == Evaluator.BlockedExitCode &&
                File.Exists(Path.Combine(gatePath, "blocked.json")) && File.Exists(Path.Combine(gatePath, "fetch-provenance.json")),
                $"{name} did not block after a real fixture execution and fresh branch fetch.");
            summaries.Add(new { Name = name, rawExit = 2, effectiveExit = exit, Status = "blocked" });
        }
        Save(directory, "scenario-summary.json", summaries);
        await output.WriteLineAsync($"{summaries.Count} real-fixture scenarios passed; discovery UIDs stable across separate processes; raw evidence preserved.").ConfigureAwait(false);
        return 0;
    }

    private static void Save<T>(string directory, string name, T value) =>
        File.WriteAllText(Path.Combine(directory, name), Format.Write(value) + Environment.NewLine);

    private static void SaveFetch(string directory, RegistrySource source, string? json)
    {
        Save(directory, "fetch-provenance.json", source);
        if (json is not null)
        {
            File.WriteAllText(Path.Combine(directory, "fetched-registry.json"), json);
        }
    }

    private static string DotnetRoot(string repo) => Directory.Exists(Path.Combine(repo, ".dotnet"))
        ? Path.Combine(repo, ".dotnet")
        : Path.GetFullPath(Path.Combine(RuntimeEnvironment.GetRuntimeDirectory(), "../../.."));

    private static async Task<(int ExitCode, string Output, string Error)> ExecuteAsync(
        string executable, string[] arguments, string repo, IReadOnlyDictionary<string, string>? environment,
        string directory, string prefix)
    {
        using var process = new Process
        {
            StartInfo = new(executable)
            {
                WorkingDirectory = repo, RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false
            }
        };
        foreach (var argument in arguments)
        {
            process.StartInfo.ArgumentList.Add(argument);
        }
        foreach (var pair in environment ?? new Dictionary<string, string>())
        {
            process.StartInfo.Environment[pair.Key] = pair.Value;
        }
        process.Start();
        // Drain both pipes concurrently, including on timeout, so crash/setup evidence survives.
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(60));
        var timedOut = false;
        try
        {
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            timedOut = true;
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
            await process.WaitForExitAsync().ConfigureAwait(false);
        }
        var capturedOutput = await stdout.ConfigureAwait(false);
        var capturedError = await stderr.ConfigureAwait(false);
        File.WriteAllText(Path.Combine(directory, prefix + ".stdout.txt"), capturedOutput);
        File.WriteAllText(Path.Combine(directory, prefix + ".stderr.txt"), capturedError);
        Save(directory, prefix + ".process.json", new { process.ExitCode, timedOut, arguments });
        Format.Require(!timedOut, "Proof subprocess timed out.");
        return (process.ExitCode, capturedOutput, capturedError);
    }
}

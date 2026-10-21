// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Globalization;

namespace KnownTestFailures;

public static class Program
{
    public const string Help = """
        KnownTestFailures: standalone experimental replay and isolated fork proof.
        prove --repo DIR --fixture EXE --output DIR
        gate --repo DIR --fixture EXE --output DIR --remote URL
        seed --repo DIR --fixture EXE --output DIR --expires UTC --issue HTTPS --owner NAME
          [--platform linux-x64]
        Proof commands use only the fixed, nonparameterized fixture; see docs/ci/known-test-failures.md.
        evaluate --original-exit N [--mode off|shadow|experimental] [--opt-in]
          [--registry FILE | --repo DIR --ref FULL_REF --registry-path PATH]
          [--evidence FILE --artifact-root DIR --at UTC] [--report FILE]
        mutate --repo DIR --ref refs/heads/NAME --expected COMMIT
          --event FILE [--initialize]
        Management appends one add/update/approve/revoke/remove/expire event.
        Branch management uses registry.json; initialize requires an all-zero expected ID.
        Off is the default and does not read registry, evidence, or TRX files.
        Active evaluation requires explicit --at; experimental replay also requires --opt-in.
        JSON output goes to stdout unless --report specifies a new file. Errors go to stderr.
        """;

    public static Task<int> Main(string[] args) => RunAsync(args, Console.Out, Console.Error);

    public static async Task<int> RunAsync(string[] args, TextWriter output, TextWriter error)
    {
        var originalExit = Evaluator.BlockedExitCode;
        var mode = "off";
        var evaluating = args.FirstOrDefault() == "evaluate";
        try
        {
            if (args.Length == 0 || args is ["--help"])
            {
                await output.WriteLineAsync(Help).ConfigureAwait(false);
                return 0;
            }
            if (args[0] is "prove" or "gate" or "seed")
            {
                return await ProofRunner.RunAsync(args, output, error).ConfigureAwait(false);
            }
            var allowed = args[0] switch
            {
                "evaluate" => new[] { "original-exit", "mode", "opt-in", "registry", "repo", "ref", "registry-path", "evidence", "artifact-root", "at", "report" },
                "mutate" => ["repo", "ref", "expected", "event", "initialize"],
                _ => throw new InvalidDataException("Unknown command; use --help.")
            };
            var options = new Dictionary<string, string>(StringComparer.Ordinal);
            for (var i = 1; i < args.Length; i++)
            {
                Format.Require(args[i].StartsWith("--", StringComparison.Ordinal), "Expected a named option.");
                var name = args[i][2..];
                Format.Require(allowed.Contains(name, StringComparer.Ordinal), $"Unknown option '--{name}'.");
                var value = name is "opt-in" or "initialize" ? "true" :
                    ++i < args.Length ? args[i] : throw new InvalidDataException($"Missing value for --{name}.");
                Format.Require(options.TryAdd(name, value), $"Duplicate option '--{name}'.");
            }
            string Required(string name) => options.TryGetValue(name, out var value)
                ? value : throw new InvalidDataException($"Missing --{name}.");

            if (evaluating)
            {
                originalExit = int.Parse(Required("original-exit"), CultureInfo.InvariantCulture);
                Format.Require(originalExit >= 0, "original-exit must be nonnegative.");
                mode = options.GetValueOrDefault("mode", "off");
                // Off must not parse timestamps or resolve any input paths.
                var at = mode == "off" ? DateTimeOffset.UnixEpoch :
                    Format.ParseUtc(Required("at"), "at");
                RegistryInput LoadRegistry()
                {
                    if (options.ContainsKey("registry"))
                    {
                        Format.Require(!options.ContainsKey("repo") && !options.ContainsKey("ref") &&
                            !options.ContainsKey("registry-path"), "Choose a local file or a branch source, not both.");
                        return GitRegistry.ReadLocal(Required("registry"));
                    }
                    return GitRegistry.ReadBranchAsync(Required("repo"), Required("ref"), Required("registry-path")).GetAwaiter().GetResult();
                }
                var decision = Evaluator.Evaluate(mode, originalExit, at, options.ContainsKey("opt-in"),
                    LoadRegistry,
                    () => Format.Read<Evidence>(File.ReadAllText(Required("evidence"))),
                    path => File.ReadAllText(Path.Combine(Required("artifact-root"), path)));
                if (options.TryGetValue("report", out var report))
                {
                    // Never overwrite raw evidence or an earlier decision, including via symlinks.
                    using var stream = new FileStream(report, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                    using var writer = new StreamWriter(stream);
                    await writer.WriteLineAsync(Format.Write(decision)).ConfigureAwait(false);
                }
                else
                {
                    await output.WriteLineAsync(Format.Write(decision)).ConfigureAwait(false);
                }
                if (decision.Status == "blocked")
                {
                    await error.WriteLineAsync(string.Join(Environment.NewLine, decision.Diagnostics)).ConfigureAwait(false);
                }
                return decision.EffectiveExitCode;
            }
            var mutation = Format.Read<RegistryEvent>(await File.ReadAllTextAsync(Required("event")).ConfigureAwait(false));
            var commit = await GitRegistry.MutateAsync(Required("repo"), Required("ref"), Required("expected"),
                options.ContainsKey("initialize"), mutation).ConfigureAwait(false);
            await output.WriteLineAsync(commit).ConfigureAwait(false);
            return 0;
        }
        catch (Exception ex) when (ex is InvalidDataException or System.Text.Json.JsonException or IOException or
            UnauthorizedAccessException or ArgumentException or InvalidOperationException or FormatException or
            OverflowException or System.ComponentModel.Win32Exception)
        {
            await error.WriteLineAsync(ex.Message).ConfigureAwait(false);
            // A shadow diagnostic cannot change the caller's original result. In replay, even
            // report-write failures must fail closed when the original aggregate was successful.
            return evaluating && (mode is "off" or "shadow" || originalExit != 0) ? originalExit : Evaluator.BlockedExitCode;
        }
    }
}

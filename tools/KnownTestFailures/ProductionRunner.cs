// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Xml;

namespace KnownTestFailures;

public sealed record ProductionModule(
    string Id, string Project, string Framework, string Assembly, string Command, string[] PrefixArguments,
    string WorkingDirectory, string[] Arguments, Dictionary<string, string> Environment, bool SupportedAdapter);

public sealed record ProductionRequest(
    ProductionModule[] Modules, string OutputDirectory, string Lane, string Platform, string Ref,
    string ExecutedSha, string HeadSha, string SourceRepository);

public sealed record ProductionCapture(int OriginalExitCode, Evidence Evidence, string[] Diagnostics);

public static class ProductionRunner
{
    public const string ProofBranch = "known-test-failure-allowances";
    public const string ProofWorkflow = "radical/aspire/.github/workflows/known-test-failures-ci-proof.yml@";
    public const string TrackingRef = "refs/remotes/known-test-failures/registry";

    public static async Task<int> RunCommandAsync(string[] args, TextWriter output, TextWriter error)
    {
        string? directory = null;
        try
        {
            Format.Require(args.Length == 7 && args[1] == "--repo" && args[3] == "--project" && args[5] == "--output",
                "ci-run requires --repo DIR --project PATH --output DIR.");
            var repo = Path.GetFullPath(args[2]);
            directory = Path.GetFullPath(args[6]);
            Format.Require(!Directory.Exists(directory), "CI output directory must be new.");
            Directory.CreateDirectory(directory);
            var checkout = await ExecuteAsync("git", ["--no-pager", "rev-parse", "HEAD"], repo,
                new Dictionary<string, string>(), directory, "checkout", TimeSpan.FromSeconds(30)).ConfigureAwait(false);
            var executedSha = checkout.Output.Trim();
            Format.Require(checkout.ExitCode == 0 && !checkout.TimedOut && executedSha.Length == 40,
                "Cannot identify the executed checkout.");
            var expectedSha = Environment.GetEnvironmentVariable("KTF_EXECUTED_SHA");
            Format.Require(string.IsNullOrEmpty(expectedSha) || expectedSha == executedSha, "Checkout differs from the requested executed SHA.");
            var repository = Environment.GetEnvironmentVariable("GITHUB_REPOSITORY") ?? "";
            var proof = repository == "radical/aspire";
            if (proof)
            {
                Format.Require((Environment.GetEnvironmentVariable("GITHUB_WORKFLOW_REF") ?? "").StartsWith(ProofWorkflow, StringComparison.Ordinal) &&
                    Environment.GetEnvironmentVariable("KTF_PROOF") == "true" &&
                    Environment.GetEnvironmentVariable("KTF_HEAD_REF") == ProofBranch,
                    "Fork execution requires the dedicated proof caller and exact proof branch.");
            }
            else
            {
                Format.Require(repository == "microsoft/aspire", "CI registry source is restricted to microsoft/aspire.");
            }
            var lane = Environment.GetEnvironmentVariable("KTF_LANE");
            Format.Require(lane == "regular", "Selective CI execution requires an explicit supported KTF_LANE=regular.");
            var scenario = proof ? Environment.GetEnvironmentVariable("KTF_SCENARIO") ?? "candidate" : "pass";
            Format.Require(scenario is "pass" or "candidate" or "mixed" or "parameter" or "changed", "Unknown proof scenario.");
            var modules = await ResolveAsync(repo, NormalizeProjectPath(repo, args[4]), directory,
                Environment.GetEnvironmentVariable("KTF_BASE_ARGS") ?? "",
                Environment.GetEnvironmentVariable("KTF_EXTRA_ARGS") ?? "",
                Environment.GetEnvironmentVariable("KTF_VERSION_ARGS") ?? "").ConfigureAwait(false);
            if (proof)
            {
                modules = modules.Select(module => module with
                {
                    Environment = new(module.Environment)
                    {
                        ["ASPIRE_KNOWN_FAILURE_PIPELINE_PROOF"] = "true",
                        ["ASPIRE_KNOWN_FAILURE_PIPELINE_SCENARIO"] = scenario
                    }
                }).ToArray();
            }
            var request = new ProductionRequest(modules, directory, lane, RuntimeInformation.RuntimeIdentifier,
                Environment.GetEnvironmentVariable("KTF_SCOPE_REF") ?? "",
                executedSha,
                Environment.GetEnvironmentVariable("KTF_HEAD_SHA") ?? "",
                Environment.GetEnvironmentVariable("KTF_SOURCE_REPO") ?? "");
            var remote = proof ? "https://github.com/radical/aspire.git" : "https://github.com/microsoft/aspire.git";
            var sourceRef = proof ? "refs/heads/known-test-failures-ci-registry" :
                Environment.GetEnvironmentVariable("KTF_REGISTRY_REF") ?? "";
            async Task<RegistryInput> Fetch()
            {
                Save(directory, "fetch-request.json", new { remote, sourceRef, trackingRef = TrackingRef });
                return await GitRegistry.FetchBranchAsync(repo, remote, sourceRef, TrackingRef, (source, json) =>
                {
                    Save(directory, "fetch-provenance.json", source);
                    if (json is not null)
                    {
                        File.WriteAllText(Path.Combine(directory, "fetched-registry.json"), json);
                    }
                }).ConfigureAwait(false);
            }
            var decision = await RunAsync(request, Fetch, output).ConfigureAwait(false);
            return decision.EffectiveExitCode;
        }
        catch (Exception ex) when (SupportedException(ex))
        {
            if (directory is not null && Directory.Exists(directory))
            {
                Save(directory, "blocked.json", new { error = ex.Message, effectiveExitCode = Evaluator.BlockedExitCode });
            }
            await error.WriteLineAsync(ex.Message).ConfigureAwait(false);
            return Evaluator.BlockedExitCode;
        }
    }

    public static string NormalizeProjectPath(string repo, string project)
    {
        // The reusable workflow retains an absolute TEST_PROJECT_PATH for its
        // build steps. Convert it only at CI admission, before strict evidence
        // paths are authored; never silently resolve traversal or leave the repo.
        Format.Text(project, "project");
        Format.Require(!project.Split(['/', '\\']).Any(segment => segment is "." or ".."),
            "project must not contain traversal segments.");
        Format.Require(!Path.IsPathRooted(project) || Path.IsPathFullyQualified(project),
            "project must not be drive-relative.");
        var relative = Path.GetRelativePath(Path.GetFullPath(repo), Path.GetFullPath(project, Path.GetFullPath(repo)))
            .Replace(Path.DirectorySeparatorChar, '/');
        Format.Path(relative, "project");
        return relative;
    }

    public static async Task<ProductionModule[]> ResolveAsync(
        string repo, string project, string directory, string baseArguments, string extraArguments, string versionArguments)
    {
        Format.Path(project, "project");
        Format.Require(project.EndsWith(".csproj", StringComparison.Ordinal), "Expected a repository-relative csproj.");
        var dotnet = Path.Combine(repo, ".dotnet", OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet");
        var properties = NativeEvidence.Arguments(versionArguments);
        Format.Require(properties.All(p => p.StartsWith("/p:", StringComparison.Ordinal) || p.StartsWith("-p:", StringComparison.Ordinal)),
            "Version overrides must be MSBuild property arguments.");
        var common = new[] { "msbuild", project, "-nologo", "-p:ContinuousIntegrationBuild=true", "-p:KnownTestFailures=true" }
            .Concat(properties).ToArray();
        var frameworks = await ExecuteAsync(dotnet, [.. common, "-getProperty:TargetFramework,TargetFrameworks"],
            repo, new Dictionary<string, string>(), directory, "frameworks", TimeSpan.FromMinutes(5)).ConfigureAwait(false);
        Format.Require(frameworks.ExitCode == 0 && !frameworks.TimedOut, "Cannot evaluate selected frameworks.");
        using var outer = JsonDocument.Parse(frameworks.Output);
        var props = outer.RootElement.GetProperty("Properties");
        var framework = props.GetProperty("TargetFramework").GetString()!;
        var selected = framework.Length > 0 ? [framework] :
            props.GetProperty("TargetFrameworks").GetString()!.Split(';', StringSplitOptions.RemoveEmptyEntries);
        Format.Require(selected.Length > 0 && selected.Distinct(StringComparer.Ordinal).Count() == selected.Length,
            "Selected frameworks must be nonempty and unique.");
        var modules = new List<ProductionModule>();
        for (var index = 0; index < selected.Length; index++)
        {
            var resolved = await ExecuteAsync(dotnet,
                [.. common, "-p:TargetFramework=" + selected[index], "-target:KnownTestFailuresModule",
                 "-bl:" + Path.Combine(directory, $"module-{index}-evaluation.binlog"),
                 "-getProperty:TargetPath,RunCommand,RunArguments,RunWorkingDirectory,DefineConstants,PublishAot,IsTestProject",
                 "-getItem:TestingPlatformEnvironmentVariable,PackageReference"],
                repo, new Dictionary<string, string>(), directory, $"module-{index}-metadata", TimeSpan.FromMinutes(5)).ConfigureAwait(false);
            Format.Require(resolved.ExitCode == 0 && !resolved.TimedOut, "Cannot resolve selected test module.");
            using var json = JsonDocument.Parse(resolved.Output);
            var root = json.RootElement;
            var values = root.GetProperty("Properties");
            string Value(string key) => values.GetProperty(key).GetString()!;
            var assembly = Value("TargetPath");
            var command = Value("RunCommand");
            var arguments = NativeEvidence.Arguments(Value("RunArguments")).Concat(NativeEvidence.Arguments(baseArguments))
                .Concat(NativeEvidence.Arguments(extraArguments)).ToArray();
            Format.Require(arguments.All(a => !a.StartsWith("--report-", StringComparison.Ordinal) &&
                a is not "--results-directory" and not "--list-tests"), "Runner owns discovery and report options.");
            var environment = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var item in root.GetProperty("Items").GetProperty("TestingPlatformEnvironmentVariable").EnumerateArray())
            {
                environment.Add(item.GetProperty("Identity").GetString()!, item.GetProperty("Value").GetString()!);
            }
            var adapter = root.GetProperty("Items").GetProperty("PackageReference").EnumerateArray()
                .Any(p => p.GetProperty("Identity").GetString() == "xunit.v3.mtp-v2") &&
                !Value("DefineConstants").Split(';').Contains("XUNIT_GENERATOR", StringComparer.Ordinal) &&
                !Value("PublishAot").Equals("true", StringComparison.OrdinalIgnoreCase);
            Format.Require(File.Exists(assembly) && File.Exists(command), "Selected module must already be built with an apphost.");
            // Native SDK dotnet test uses the module directory, even when the
            // dotnet-run RunWorkingDirectory property is set. Launching from the
            // checkout instead changes relative fixture/resource lookup behavior.
            modules.Add(new($"module-{index}", project, selected[index], assembly, command, [],
                Path.GetDirectoryName(assembly)!, arguments, environment, adapter));
        }
        return [.. modules];
    }

    public static async Task<Decision> RunAsync(
        ProductionRequest request, Func<Task<RegistryInput>> fetchRegistry, TextWriter output)
    {
        var capture = await CaptureAsync(request, output).ConfigureAwait(false);
        Decision decision;
        if (capture.Diagnostics.Length > 0)
        {
            decision = new("enforce", "blocked", capture.OriginalExitCode,
                capture.OriginalExitCode == 0 ? Evaluator.BlockedExitCode : capture.OriginalExitCode,
                DateTimeOffset.UtcNow, [], [], capture.Diagnostics, Evidence: capture.Evidence);
        }
        else
        {
            RegistryInput? registry = null;
            string? fetchError = null;
            if (capture.OriginalExitCode == 2)
            {
                try
                {
                    registry = await fetchRegistry().ConfigureAwait(false);
                }
                catch (Exception ex) when (SupportedException(ex))
                {
                    fetchError = ex.Message;
                }
            }
            var at = DateTimeOffset.UtcNow;
            decision = Evaluator.Evaluate("enforce", capture.OriginalExitCode, at, false,
                () => registry ?? throw new InvalidDataException(fetchError ?? "Unexpected registry access on passing run."),
                () => capture.Evidence, path => File.ReadAllText(Path.Combine(request.OutputDirectory, path)));
        }
        Save(request.OutputDirectory, "decision.json", decision);
        File.WriteAllText(Path.Combine(request.OutputDirectory, "effective-exit-code.txt"), decision.EffectiveExitCode.ToString(System.Globalization.CultureInfo.InvariantCulture));
        await output.WriteLineAsync($"MTP raw exit={capture.OriginalExitCode}; effective exit={decision.EffectiveExitCode}; " +
            $"status={decision.Status}; registry={decision.RegistrySource?.Commit ?? "not consumed"}").ConfigureAwait(false);
        foreach (var diagnostic in decision.Diagnostics)
        {
            await output.WriteLineAsync(diagnostic).ConfigureAwait(false);
        }
        return decision;
    }

    public static async Task<ProductionCapture> CaptureAsync(ProductionRequest request, TextWriter output)
    {
        Format.Require(request.Modules.Length > 0 &&
            request.Modules.Select(m => m.Id).Distinct(StringComparer.Ordinal).Count() == request.Modules.Length,
            "Selected modules must be nonempty and unique.");
        Format.FullRef(request.Ref);
        foreach (var module in request.Modules)
        {
            Format.Path(module.Id, "module id");
            Format.Require(!module.Id.Contains('/'), "Module id must be a single directory component.");
            Format.Path(module.Project, "project");
        }
        Directory.CreateDirectory(request.OutputDirectory);
        Save(request.OutputDirectory, "request.json", request);
        var diagnostics = new List<string>();
        var inventories = new Dictionary<string, DiscoveredCase[]>(StringComparer.Ordinal);
        // Persist every selected invocation BEFORE discovery/execution. A bad discovery
        // blocks waivers, but never stops execution of this or subsequent selected modules.
        foreach (var module in request.Modules)
        {
            var directory = Path.Combine(request.OutputDirectory, module.Id);
            Format.Require(!Directory.Exists(directory), "Module evidence directory must be new.");
            Directory.CreateDirectory(directory);
            try
            {
                Format.Require(module.SupportedAdapter, "Only reflection xUnit v3 MTP adapters support portable allowances.");
                Format.Require(File.ReadAllLines(Path.ChangeExtension(module.Assembly, ".uniqueid")).FirstOrDefault() ==
                    NativeEvidence.Identity(module.Project, module.Framework), "Missing or conflicting portable assembly identity.");
            }
            catch (Exception ex) when (SupportedException(ex))
            {
                diagnostics.Add($"{module.Id}: {ex.Message}");
            }
            DiscoveredCase[]? inventory = null;
            for (var attempt = 1; attempt <= 2; attempt++)
            {
                try
                {
                    var discovery = await ExecuteAsync(module.Command,
                        [.. module.PrefixArguments, .. module.Arguments, "--list-tests", "json", "--no-ansi"],
                        module.WorkingDirectory, module.Environment, directory, $"discovery-{attempt}", TimeSpan.FromMinutes(5)).ConfigureAwait(false);
                    var found = NativeEvidence.Discover(discovery.Output);
                    Format.Require(!discovery.TimedOut && (discovery.ExitCode == 0 || (discovery.ExitCode == 8 && found.Length == 0)),
                        "Framework discovery did not complete normally.");
                    Format.Require(inventory is null || inventory.SequenceEqual(found), "Discovery UIDs changed across separate processes.");
                    inventory = found;
                }
                catch (Exception ex) when (SupportedException(ex))
                {
                    diagnostics.Add($"{module.Id}: {ex.Message}");
                }
            }
            inventories[module.Id] = inventory ?? [];
            Save(directory, "inventory-before-execution.json", inventories[module.Id]);
        }
        var invocations = new List<Invocation>();
        var rawExits = new List<int>();
        foreach (var module in request.Modules)
        {
            var directory = Path.Combine(request.OutputDirectory, module.Id);
            var process = await ExecuteAsync(module.Command,
                [.. module.PrefixArguments, .. module.Arguments,
                 "--report-trx", "--report-trx-filename", "results.trx",
                 "--report-xunit-trx", "--report-xunit-trx-filename", "identity.evidence",
                 "--report-xunit-xml", "--report-xunit-xml-filename", "lifecycle.xml",
                 "--results-directory", directory, "--no-ansi"],
                module.WorkingDirectory, module.Environment, directory, "execution", TimeSpan.FromMinutes(25)).ConfigureAwait(false);
            rawExits.Add(process.ExitCode);
            await output.WriteLineAsync($"{module.Id} ({module.Project}/{module.Framework}): raw MTP exit={process.ExitCode}").ConfigureAwait(false);
            ExpectedCase[] expected = [];
            var completed = false;
            var allowEmpty = module.Arguments.Zip(module.Arguments.Skip(1))
                .Any(pair => pair.First == "--ignore-exit-code" && pair.Second == "8");
            try
            {
                Format.Require(!process.TimedOut && process.ExitCode is 0 or 2, "Abnormal MTP process exit or timeout.");
                var results = StrictTrx.Read(File.ReadAllText(Path.Combine(directory, "identity.evidence")), true, allowEmpty);
                expected = NativeEvidence.Bind(inventories[module.Id], results);
                NativeEvidence.ValidateLifecycle(File.ReadAllText(Path.Combine(directory, "lifecycle.xml")), results);
                Format.Require(process.ExitCode == (results.Any(r => r.Outcome == "Failed") ? 2 : 0),
                    "Direct MTP exit contradicts results.");
                Format.Require(!Directory.EnumerateFiles(directory, "*hangdump*", SearchOption.AllDirectories).Any(),
                    "Hang dump blocks allowances.");
                completed = true;
            }
            catch (Exception ex) when (SupportedException(ex))
            {
                diagnostics.Add($"{module.Id}: {ex.Message}");
            }
            invocations.Add(new(module.Id, module.Project, module.Id + "/identity.evidence", true,
                process.ExitCode, !process.TimedOut && process.ExitCode is 0 or 2, completed, [], expected) { AllowEmpty = allowEmpty });
        }
        var evidence = new Evidence(1, request.Lane, request.Platform, request.Ref,
            request.Modules.Select(m => m.Id).ToArray(), [.. invocations]);
        try
        {
            Evaluator.ValidateProductionInventory(evidence);
        }
        catch (Exception ex) when (SupportedException(ex))
        {
            diagnostics.Add(ex.Message);
        }
        // This is an explicitly direct-module aggregate, not a claimed SDK-wrapper exit:
        // retain the first infrastructure exit, otherwise any test failure, otherwise success.
        var raw = rawExits.FirstOrDefault(code => code is not 0 and not 2);
        if (raw == 0 && rawExits.Contains(2))
        {
            raw = 2;
        }
        var capture = new ProductionCapture(raw, evidence, [.. diagnostics]);
        Save(request.OutputDirectory, "capture.json", capture);
        Save(request.OutputDirectory, "evidence.json", evidence);
        File.WriteAllText(Path.Combine(request.OutputDirectory, "raw-exit-code.txt"), raw.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return capture;
    }

    private static bool SupportedException(Exception ex) =>
        ex is InvalidDataException or JsonException or IOException or UnauthorizedAccessException or ArgumentException or
        InvalidOperationException or KeyNotFoundException or XmlException or System.ComponentModel.Win32Exception;

    private static void Save<T>(string directory, string name, T value) =>
        File.WriteAllText(Path.Combine(directory, name), Format.Write(value) + Environment.NewLine);

    private sealed record ProcessResult(int ExitCode, string Output, bool TimedOut);

    private static async Task<ProcessResult> ExecuteAsync(
        string command, string[] arguments, string workingDirectory, IReadOnlyDictionary<string, string> environment,
        string directory, string name, TimeSpan limit)
    {
        try
        {
            using var process = new Process
            {
                StartInfo = new(command)
                {
                    WorkingDirectory = workingDirectory, UseShellExecute = false,
                    RedirectStandardOutput = true, RedirectStandardError = true
                }
            };
            foreach (var arg in arguments)
            {
                process.StartInfo.ArgumentList.Add(arg);
            }
            foreach (var (key, value) in environment)
            {
                process.StartInfo.Environment[key] = value;
            }
            process.Start();
            using var timeout = new CancellationTokenSource(limit);
            // Stream both pipes to artifacts, including partial output on cancellation.
            // The deadline also covers pipe draining: a surviving descendant can retain a
            // pipe after the test process exits, which must not hang the gate indefinitely.
            var stdoutPath = Path.Combine(directory, name + ".stdout.txt");
            var stderrPath = Path.Combine(directory, name + ".stderr.txt");
            using var stdoutFile = new FileStream(stdoutPath, FileMode.Create, FileAccess.Write, FileShare.Read);
            using var stderrFile = new FileStream(stderrPath, FileMode.Create, FileAccess.Write, FileShare.Read);
            var stdout = process.StandardOutput.BaseStream.CopyToAsync(stdoutFile, timeout.Token);
            var stderr = process.StandardError.BaseStream.CopyToAsync(stderrFile, timeout.Token);
            var timedOut = false;
            try
            {
                await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
                await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                timedOut = true;
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
                await process.WaitForExitAsync().ConfigureAwait(false);
                try
                {
                    await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    // Partial bytes are already in the artifact streams.
                }
            }
            await stdoutFile.DisposeAsync().ConfigureAwait(false);
            await stderrFile.DisposeAsync().ConfigureAwait(false);
            var text = await File.ReadAllTextAsync(stdoutPath).ConfigureAwait(false);
            Save(directory, name + ".process.json", new { process.ExitCode, timedOut, command, arguments });
            return new(process.ExitCode, text, timedOut);
        }
        catch (Exception ex) when (SupportedException(ex))
        {
            Save(directory, name + ".process.json", new { exitCode = Evaluator.BlockedExitCode, startError = ex.Message, command, arguments });
            File.WriteAllText(Path.Combine(directory, name + ".stdout.txt"), "");
            File.WriteAllText(Path.Combine(directory, name + ".stderr.txt"), ex.Message);
            return new(Evaluator.BlockedExitCode, "", false);
        }
    }
}

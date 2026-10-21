// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Xml.Linq;
using KnownTestFailures;
using Xunit;

namespace Infrastructure.Tests;

internal sealed class KnownTestFailuresFixture(ITestOutputHelper output) : IDisposable
{
    public static readonly DateTimeOffset Now = DateTimeOffset.Parse("2026-10-20T12:00:00Z", System.Globalization.CultureInfo.InvariantCulture);
    public static readonly Allowance Content = new(
        "tests/Example.Tests/Example.Tests.csproj", "Example.Tests.Flaky", "Flaky(value: 1)", "stable-case-1",
        "regular", "linux-x64", "refs/heads/main", "known error",
        "https://example.com/issues/123", "team", Now.AddDays(-1), Now.AddDays(1));
    public static readonly ExpectedCase Case = new("stable-case-1", "test-0", Content.Method, Content.DisplayName);
    public static readonly RegistrySource Source = new("local-untrusted", "registry.json");

    // Keep fixture files in the checkout, not OS temp. Reuse the shared disposal/retry behavior.
    public TemporaryWorkspace Workspace { get; } = new(output,
        Directory.CreateDirectory(System.IO.Path.GetFullPath(System.IO.Path.Combine("artifacts", "known-test-failures-tests", Guid.NewGuid().ToString("N")))));

    public string Path => Workspace.Path;
    public void Dispose() => Workspace.Dispose();

    public static RegistryEvent Event(string action, int sequence = 1, int revision = 1, string id = "allow-1",
        Allowance? content = null, DateTimeOffset? at = null) =>
        new(sequence, id, action, "reviewer", "Reproduced and reviewed", at ?? Now.AddHours(-1),
            revision, action is "add" or "update" ? content ?? Content : null);

    public static Registry Registry() => new(1, "experimental", [Event("add"), Event("approve", 2)]);

    public static Evidence Evidence() => new(1, Content.Lane, Content.Platform, Content.Ref, ["invocation-1"],
        [new("invocation-1", Content.Project, "results.trx", true, 2, true, true, [], [Case])]);

    public string Trx(params TestTrxCase[] cases)
    {
        if (cases.Length == 0)
        {
            cases = [new(Content.Method, Content.DisplayName, "Failed", Content.Message)];
        }
        var path = TestTrxBuilder.CreateTrxFile(System.IO.Path.Combine(Path, "results.trx"), cases);
        var document = XDocument.Load(path);
        XNamespace ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010";
        document.Root!.Element(ns + "ResultSummary")!.SetAttributeValue("outcome", cases.Any(c => c.Outcome == "Failed") ? "Failed" : "Passed");
        document.Save(path);
        return File.ReadAllText(path);
    }

    public static Decision Evaluate(Registry? registry = null, Evidence? evidence = null, string? trx = null,
        string mode = "experimental", int originalExit = 2, DateTimeOffset? at = null, bool optIn = true) =>
        Evaluator.Evaluate(mode, originalExit, at ?? Now, optIn,
            () => new(registry ?? Registry(), Source, Format.Write(registry ?? Registry())),
            () => evidence ?? Evidence(),
            _ => trx ?? throw new FileNotFoundException("Missing TRX."));

    public async Task<(int Exit, string Stdout, string Stderr)> Cli(params string[] args)
    {
        Workspace.TestOutputHelper.WriteLine($"KnownTestFailures {string.Join(' ', args)}");
        using var stdout = new StringWriter();
        using var stderr = new StringWriter();
        var exit = await KnownTestFailures.Program.RunAsync(args, stdout, stderr);
        return (exit, stdout.ToString(), stderr.ToString());
    }

    public void InitializeGit()
    {
        GitCli.Run(Path, "init", "--initial-branch=main");
        File.WriteAllText(System.IO.Path.Combine(Path, "sentinel.txt"), "working tree remains untouched\n");
        GitCli.Run(Path, "add", "--", "sentinel.txt");
        GitCli.Run(Path, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost",
            "-c", "commit.gpgsign=false", "commit", "-m", "Initialize fixture");
        File.WriteAllText(System.IO.Path.Combine(Path, "sentinel.txt"), "unstaged user changes\n");
    }
}

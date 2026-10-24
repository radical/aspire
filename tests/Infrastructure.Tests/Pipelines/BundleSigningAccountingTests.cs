// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.TestUtilities;
using Xunit;

namespace Infrastructure.Tests;

public sealed class BundleSigningAccountingTests(ITestOutputHelper output)
{
    [Theory]
    [InlineData("processed", null)]
    [InlineData("missing", "dashboard/helper.exe")]
    [InlineData("different-copy", "dashboard/helper.exe")]
    [InlineData("failed-pass", "did not complete")]
    [InlineData("dry-run", "real signing")]
    [InlineData("asset", null)]
    [InlineData("upstream", null)]
    [InlineData("unknown-native", "dashboard/helper.exe")]
    [InlineData("empty-layout", "No native files")]
    [InlineData("updating", null)]
    [InlineData("already-signed", null)]
    [InlineData("mac-transport", null)]
    [InlineData("hidden", "dashboard/.helper.exe")]
    [RequiresTools(["pwsh"])]
    public async Task AccountsForActualPayloadBytes(string scenario, string? error)
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var layout = workspace.CreateDirectory("layout").FullName;
        var publish = workspace.CreateDirectory("publish").FullName;
        var directory = Directory.CreateDirectory(Path.Combine(layout, "dashboard")).FullName;
        var bytes = new byte[128];
        bytes[0] = (byte)'M';
        bytes[1] = (byte)'Z';
        bytes[0x3c] = 64;
        bytes[64] = (byte)'P';
        bytes[65] = (byte)'E';
        var source = Path.Combine(publish, "helper.exe");
        File.WriteAllBytes(source, bytes);
        if (scenario != "empty-layout")
        {
            File.WriteAllBytes(Path.Combine(directory, "helper.exe"), bytes);
        }
        if (scenario == "asset")
        {
            File.WriteAllText(Path.Combine(directory, "contract.json"), "{}");
        }
        if (scenario == "upstream")
        {
            File.Move(Path.Combine(directory, "helper.exe"), Path.Combine(directory, "conpty.dll"));
        }
        if (scenario == "hidden")
        {
            File.Move(Path.Combine(directory, "helper.exe"), Path.Combine(directory, ".helper.exe"));
        }
        if (scenario == "different-copy")
        {
            bytes[127] = 1;
            File.WriteAllBytes(source, bytes);
        }
        var log = Path.Combine(workspace.Path, "sign.log");
        var processed = scenario switch
        {
            "missing" or "upstream" or "unknown-native" or "hidden" => "",
            "updating" => $"Updating '{source}' with signed content (TaskId:3)",
            "already-signed" => $"File '{source}' is already signed. (TaskId:3)",
            _ => $"PE file {source} is signed properly"
        };
        await File.WriteAllTextAsync(log, $"""
            SignToolTask starting.
            DryRun: {(scenario == "dry-run" ? "True" : "False")}
            Signing mode: Real
            {processed}
            {(scenario == "failed-pass" ? "" : "Build artifacts signed and validated.")}
            SignToolTask execution finished.
            """);
        var receipt = Path.Combine(workspace.Path, "receipt.json");
        var requests = workspace.CreateDirectory("requests").FullName;
        if (scenario == "mac-transport")
        {
            // Arcade transports Mach-O files in a ZIP and can sign them in place,
            // without emitting an "Updating" or PE verification record.
            var signing = Directory.CreateDirectory(Path.Combine(requests, "Signing")).FullName;
            await File.WriteAllTextAsync(Path.Combine(signing, "Round0-Sign.proj"), $"""
                <Project><ItemGroup><FilesToSign Include="{Uri.EscapeDataString(source + ".zip")}"><Authenticode>MacDeveloperHarden</Authenticode></FilesToSign></ItemGroup></Project>
                """);
            await File.WriteAllTextAsync(log, """
                SignToolTask starting.
                DryRun: False
                Signing mode: Real
                Build artifacts signed and validated.
                SignToolTask execution finished.
                """);
        }
        using var exporter = new PowerShellCommand(Path.Combine(RepoRoot.Path, "eng/scripts/write-bundle-signing-receipt.ps1"), output)
            .WithTimeout(TimeSpan.FromMinutes(1));
        var exportResult = await exporter.ExecuteAsync("-SigningLog", $"\"{log}\"", "-OutputPath", $"\"{receipt}\"",
            "-SigningRequestsDirectory", $"\"{requests}\"");
        if (scenario is "failed-pass" or "dry-run")
        {
            exportResult.EnsureExitCode(1);
            Assert.Contains(error!, exportResult.Output);
            return;
        }
        exportResult.EnsureSuccessful();
        using var verifier = new PowerShellCommand(Path.Combine(RepoRoot.Path, "eng/scripts/verify-bundle-signing-accounting.ps1"), output)
            .WithTimeout(TimeSpan.FromMinutes(1));
        var result = await verifier.ExecuteAsync("-LayoutDirectory", $"\"{layout}\"", "-Receipt", $"\"{receipt}\"",
            "-Policy", $"\"{Path.Combine(RepoRoot.Path, "eng/bundle-signing-policy.json")}\"", "-Rid", "win-x64",
            "-ReportPath", $"\"{Path.Combine(workspace.Path, "report.json")}\"");
        result.EnsureExitCode(error is null ? 0 : 1);
        Assert.Contains(error ?? "Signing accounting passed", result.Output);
    }
}

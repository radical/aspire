// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using System.Text.Json;
using Aspire.TestUtilities;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class SourceIndexBuildTests(ITestOutputHelper output)
{
    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task IndexingBuildPreservesEntireProjectSelection()
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var project = Path.Combine(workspace.Path, "selection.proj");
        await File.WriteAllTextAsync(project, $"""
            <Project>
              <Import Project="{Path.Combine(RepoRoot.Path, "eng", "Build.props")}" />
            </Project>
            """);

        string[] arguments = ["-getItem:ProjectToBuild", $"/p:RepoRoot={RepoRoot.Path}{Path.DirectorySeparatorChar}"];
        using var baseline = await EvaluateAsync(workspace, project, arguments, indexing: false);
        using var indexing = await EvaluateAsync(workspace, project, arguments, indexing: true);
        var expected = ProjectPaths(baseline);
        Assert.Equal(expected, ProjectPaths(indexing));
        Assert.Contains(expected, path => path.Contains("/tests/", StringComparison.Ordinal));
        Assert.Contains(expected, path => path.Contains("/playground/", StringComparison.Ordinal));
        Assert.Contains(expected, path => path.EndsWith("/src/Aspire.Dashboard/Aspire.Dashboard.csproj", StringComparison.Ordinal));
        Assert.Contains(expected, path => path.EndsWith("/src/Aspire.Cli/Aspire.Cli.csproj", StringComparison.Ordinal));
        Assert.Contains(expected, path => path.Contains("/eng/clipack/", StringComparison.Ordinal));
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task IndexingBuildDisablesOnlyDashboardNativePackaging()
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var rid = RuntimeInformation.RuntimeIdentifier;
        var project = Path.Combine(RepoRoot.Path, "eng", "dashboardpack", $"Aspire.Dashboard.Sdk.{rid}.csproj");
        string[] arguments = ["-getProperty:CanPublishDashboardNativeAot,IsPackable,GeneratePackageOnBuild"];
        using var baseline = await EvaluateAsync(workspace, project, arguments, indexing: false);
        using var indexing = await EvaluateAsync(workspace, project, arguments, indexing: true);
        foreach (var property in new[] { "CanPublishDashboardNativeAot", "IsPackable", "GeneratePackageOnBuild" })
        {
            Assert.Equal("true", baseline.RootElement.GetProperty("Properties").GetProperty(property).GetString());
            Assert.Equal("false", indexing.RootElement.GetProperty("Properties").GetProperty(property).GetString());
        }

        // A clean indexing build must not need a previously published native executable.
        using var built = await EvaluateAsync(workspace, project, ["-target:Build", .. arguments], indexing: true);
        Assert.Equal("false", built.RootElement.GetProperty("Properties").GetProperty("IsPackable").GetString());

        var cliProject = Path.Combine(RepoRoot.Path, "eng", "clipack", $"Aspire.Cli.{rid}.csproj");
        using var cli = await EvaluateAsync(workspace, cliProject,
            ["-getProperty:PublishNativeAot,CliRuntime"], indexing: true);
        Assert.Equal("true", cli.RootElement.GetProperty("Properties").GetProperty("PublishNativeAot").GetString());
        Assert.Equal(rid, cli.RootElement.GetProperty("Properties").GetProperty("CliRuntime").GetString());
    }

    [Theory]
    [InlineData("src/Aspire.Dashboard/Aspire.Dashboard.csproj")]
    [InlineData("src/Aspire.Cli/Aspire.Cli.csproj")]
    [RequiresTools(["pwsh"])]
    public async Task IndexingBuildPreservesManagedCompileItems(string relativeProject)
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var project = Path.Combine(RepoRoot.Path, relativeProject);
        using var baseline = await EvaluateAsync(workspace, project, ["-getItem:Compile"], indexing: false);
        using var indexing = await EvaluateAsync(workspace, project, ["-getItem:Compile"], indexing: true);
        Assert.Equal(
            baseline.RootElement.GetProperty("Items").GetProperty("Compile").GetRawText(),
            indexing.RootElement.GetProperty("Items").GetProperty("Compile").GetRawText());
    }

    [Fact]
    [RequiresTools(["pwsh"])]
    public async Task IndexingBuildDoesNotProvisionBrowsers()
    {
        using var workspace = TemporaryWorkspace.Create(output);
        var project = Path.Combine(workspace.Path, "browsers.proj");
        await File.WriteAllTextAsync(project, $"""
            <Project>
              <PropertyGroup>
                <InstallBrowsersForPlaywright>true</InstallBrowsersForPlaywright>
              </PropertyGroup>
              <Import Project="{Path.Combine(RepoRoot.Path, "tests", "Shared", "Playwright", "Playwright.targets")}" />
            </Project>
            """);
        using var result = await EvaluateAsync(workspace, project,
            ["-target:ProvisionBrowsersForPlaywright", "-getProperty:InstallBrowsersForPlaywright,ContinuousIntegrationBuild"],
            indexing: true);
        Assert.Equal("false", result.RootElement.GetProperty("Properties").GetProperty("InstallBrowsersForPlaywright").GetString());
        Assert.Equal("true", result.RootElement.GetProperty("Properties").GetProperty("ContinuousIntegrationBuild").GetString());
    }

    private async Task<JsonDocument> EvaluateAsync(TemporaryWorkspace workspace, string project, string[] arguments, bool indexing)
    {
        var properties = indexing ? await IndexingPropertiesAsync() : [];
        var script = Path.Combine(workspace.Path, "evaluate.ps1");
        await File.WriteAllTextAsync(script, """
            $arguments = @($env:TEST_ARGUMENTS | ConvertFrom-Json)
            & $env:TEST_DOTNET msbuild $env:TEST_PROJECT -nologo @arguments /p:ContinuousIntegrationBuild=true
            exit $LASTEXITCODE
            """);
        using var command = new PowerShellCommand(script, output)
            .WithWorkingDirectory(RepoRoot.Path)
            .WithTimeout(TimeSpan.FromMinutes(2))
            .WithEnvironmentVariable("TEST_DOTNET", Path.Combine(RepoRoot.Path, ".dotnet", OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet"))
            .WithEnvironmentVariable("TEST_PROJECT", project)
            .WithEnvironmentVariable("TEST_ARGUMENTS", JsonSerializer.Serialize(arguments.Concat(properties).ToArray()))
            .WithEnvironmentVariable("MSBUILDTERMINALLOGGER", "false");
        var result = await command.ExecuteAsync();
        result.EnsureSuccessful();
        return JsonDocument.Parse(result.Output);
    }

    private static async Task<string[]> IndexingPropertiesAsync()
    {
        var yaml = new YamlStream();
        yaml.Load(new StringReader(await File.ReadAllTextAsync(Path.Combine(RepoRoot.Path, "eng", "pipelines", "azure-pipelines-source-index.yml"))));
        var root = (YamlMappingNode)yaml.Documents[0].RootNode;
        var extends = (YamlMappingNode)root.Children[new YamlScalarNode("extends")];
        var parameters = (YamlMappingNode)extends.Children[new YamlScalarNode("parameters")];
        var stages = (YamlSequenceNode)parameters.Children[new YamlScalarNode("stages")];
        var stage = (YamlMappingNode)Assert.Single(stages.Children);
        var jobs = (YamlSequenceNode)stage.Children[new YamlScalarNode("jobs")];
        var job = (YamlMappingNode)Assert.Single(jobs.Children);
        var jobParameters = (YamlMappingNode)job.Children[new YamlScalarNode("parameters")];
        var sourceIndex = (YamlMappingNode)jobParameters.Children[new YamlScalarNode("sourceIndexParams")];
        var command = ((YamlScalarNode)sourceIndex.Children[new YamlScalarNode("sourceIndexBuildCommand")]).Value!;
        return command.Split(' ', StringSplitOptions.RemoveEmptyEntries)
            .Where(argument => argument.StartsWith("/p:", StringComparison.OrdinalIgnoreCase))
            .ToArray();
    }

    private static string[] ProjectPaths(JsonDocument document) =>
        document.RootElement.GetProperty("Items").GetProperty("ProjectToBuild").EnumerateArray()
            .Select(item => item.GetProperty("Identity").GetString()!.Replace('\\', '/'))
            .Order(StringComparer.Ordinal).ToArray();
}

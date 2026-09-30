// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Xml.Linq;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class CiWorkflowTests
{
    private static readonly YamlMappingNode s_testsJobs = Jobs("tests.yml");
    private static readonly YamlMappingNode s_installerJobs = Jobs("prepare-installer-artifacts.yml");
    private static readonly YamlMappingNode s_runTestsJobs = Jobs("run-tests.yml");
    private static readonly YamlMappingNode s_ciJobs = Jobs("ci.yml");

    [Theory]
    [InlineData("prepare_winget_installer_artifacts")]
    [InlineData("prepare_homebrew_installer_artifacts")]
    public void InstallerJobsDependOnBuiltPackages(string jobName)
    {
        var job = Mapping(s_testsJobs, jobName);

        Assert.Contains("build_packages", SequenceScalars(job, "needs"));
    }

    [Fact]
    public void InstallerWorkflowStagesSameRunTemplatePackages()
    {
        var steps = Steps(Mapping(s_installerJobs, "prepare_installer_artifacts"));
        var download = Assert.Single(
            steps,
            step => Scalar(step, "uses")?.StartsWith("actions/download-artifact@", StringComparison.Ordinal) == true &&
                    Scalar(Mapping(step, "with"), "name") == "built-nugets");
        var downloadInputs = Mapping(download, "with");

        // The artifact name, package glob, and environment variable form the hand-off between
        // independently maintained build, installer, and CLI restore logic.
        Assert.Equal("${{ github.workspace }}/built-nugets", Scalar(downloadInputs, "path"));

        var configure = Assert.Single(steps, step => Scalar(step, "shell") == "pwsh" &&
            Scalar(step, "run")?.Contains("ASPIRE_CLI_PACKAGES=", StringComparison.Ordinal) == true);
        var script = Scalar(configure, "run");
        Assert.Contains("Aspire.ProjectTemplates.*.nupkg", script, StringComparison.Ordinal);
        Assert.Contains("Shipping", script, StringComparison.Ordinal);
        Assert.Contains("$env:GITHUB_ENV", script, StringComparison.Ordinal);
    }

    [Fact]
    public void RunTestsInstallsJavaForProjectsThatRequireIt()
    {
        var steps = Steps(Mapping(s_runTestsJobs, "test"));
        var javaSetup = Assert.Single(
            steps,
            step => Scalar(step, "uses")?.StartsWith("actions/setup-java@", StringComparison.Ordinal) == true);

        Assert.Equal("${{ fromJson(inputs.properties).requiresJava == true }}", Scalar(javaSetup, "if"));
        Assert.Equal("temurin", Scalar(Mapping(javaSetup, "with"), "distribution"));
        Assert.Equal("21", Scalar(Mapping(javaSetup, "with"), "java-version"));

        var properties = XDocument.Load(RepoPath("eng", "testing", "CITestsProperties.props"));
        var requiresJava = Assert.Single(
            properties.Descendants("CITestsProperty"),
            element => (string?)element.Attribute("Include") == "requiresJava");
        Assert.Equal("RequiresJava", (string?)requiresJava.Attribute("MSBuildProp"));

        var javaProject = XDocument.Load(RepoPath(
            "tests",
            "Aspire.Hosting.CodeGeneration.Java.Tests",
            "Aspire.Hosting.CodeGeneration.Java.Tests.csproj"));
        Assert.Equal("true", Assert.Single(javaProject.Descendants("RequiresJava")).Value);
    }

    [Fact]
    public void CiFailureTrackerChecksOutTheEvaluatedBranch()
    {
        var tracker = Mapping(s_ciJobs, "ci_failure_tracker");
        var checkout = Assert.Single(
            Steps(tracker),
            step => Scalar(step, "uses")?.StartsWith("actions/checkout@", StringComparison.Ordinal) == true);

        Assert.False(
            checkout.Children.TryGetValue(new YamlScalarNode("with"), out var withNode) &&
            Assert.IsType<YamlMappingNode>(withNode).Children.ContainsKey(new YamlScalarNode("ref")));
    }

    private static YamlMappingNode Jobs(string workflowName)
        => Mapping(LoadWorkflow(workflowName), "jobs");

    private static List<YamlMappingNode> Steps(YamlMappingNode job)
        => Sequence(job, "steps").Cast<YamlMappingNode>().ToList();

    private static List<string?> SequenceScalars(YamlMappingNode node, string key)
        => Sequence(node, key).Cast<YamlScalarNode>().Select(item => item.Value).ToList();

    private static YamlSequenceNode Sequence(YamlMappingNode node, string key)
        => Assert.IsType<YamlSequenceNode>(node.Children[new YamlScalarNode(key)]);

    private static YamlMappingNode Mapping(YamlMappingNode node, string key)
        => Assert.IsType<YamlMappingNode>(node.Children[new YamlScalarNode(key)]);

    private static string? Scalar(YamlMappingNode node, string key)
        => node.Children.TryGetValue(new YamlScalarNode(key), out var value) && value is YamlScalarNode scalar
            ? scalar.Value
            : null;

    private static string RepoPath(params string[] path)
        => Path.Combine([RepoRoot.Path, .. path]);

    private static YamlMappingNode LoadWorkflow(string workflowName)
    {
        var yaml = new YamlStream();
        using var reader = new StringReader(File.ReadAllText(RepoPath(".github", "workflows", workflowName)));
        yaml.Load(reader);

        return Assert.IsType<YamlMappingNode>(yaml.Documents[0].RootNode);
    }
}

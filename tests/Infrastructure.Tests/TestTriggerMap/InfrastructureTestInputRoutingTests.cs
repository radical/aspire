// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.SelectTests;
using Microsoft.Extensions.FileSystemGlobbing;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests.TestTriggerMap;

public sealed class InfrastructureTestInputRoutingTests
{
    private const string SemanticInputsWorkflow = ".github/workflows/validate-infrastructure-test-inputs.yml";
    private const string AgenticWorkflow = ".github/workflows/validate-agentic-workflows.yml";

    private static readonly string[] s_pipelineInputs =
    [
        "eng/pipelines/azure-pipelines.yml",
        "eng/pipelines/common-variables.yml",
        "eng/pipelines/release-publish-nuget.yml",
        "eng/pipelines/templates/build_sign_native.yml",
        "eng/pipelines/templates/prepare-npm-cli-packages.yml",
        "eng/pipelines/templates/prepare-winget-manifest.yml",
        "eng/pipelines/templates/publish-winget.yml",
    ];

    private static readonly string[] s_pipelineAlignmentDocuments =
    [
        "docs/release-process.md",
        "docs/specs/npm-cli-package.md",
    ];

    [Theory]
    [InlineData("eng/pipelines/azure-pipelines.yml")]
    [InlineData("eng/pipelines/templates/build_sign_native.yml")]
    [InlineData("eng/pipelines/templates/prepare-npm-cli-packages.yml")]
    [InlineData(".github/workflows/backport.yml")]
    [InlineData(".github/workflows/auto-rerun-transient-ci-failures.js")]
    [InlineData("docs/specs/npm-cli-package.md")]
    [InlineData("docs/release-process.md")]
    public void DedicatedLaneRunsForSkippedSemanticInputs(string path)
    {
        Assert.True(WorkflowRunsFor(SemanticInputsWorkflow, path));
    }

    [Theory]
    [InlineData("eng/pipelines/azure-pipelines-public.yml")]
    [InlineData("eng/pipelines/templates/BuildAndTest.yml")]
    [InlineData("eng/Publishing.props")]
    [InlineData("eng/test-configuration.json")]
    [InlineData(".github/workflows/analyze-ci-failure.lock.yml")]
    public void DedicatedLaneDoesNotClaimInputsWithoutItsSemanticConsumers(string path)
    {
        Assert.False(WorkflowRunsFor(SemanticInputsWorkflow, path));
    }

    [Fact]
    public void DedicatedLaneListsOnlyPipelineInputsReadByInfrastructureTests()
    {
        var actual = WorkflowPaths(SemanticInputsWorkflow)
            .Where(path => path.StartsWith("eng/pipelines/", StringComparison.Ordinal))
            .Order(StringComparer.Ordinal);

        Assert.Equal(s_pipelineInputs.Order(StringComparer.Ordinal), actual);
    }

    [Fact]
    public void SemanticPipelineInputsStayAlignedAcrossSelectorAndDedicatedLane()
    {
        var expected = s_pipelineInputs.Concat(s_pipelineAlignmentDocuments).Order(StringComparer.Ordinal);
        var map = TestTriggerMap.Load(RepoRoot.Path);
        var selectorRule = Assert.Single(
            map.PathRules,
            rule => rule.Targets.Contains("test:Infrastructure.Tests", StringComparer.Ordinal)
                && rule.Paths.Contains("eng/pipelines/release-publish-nuget.yml", StringComparer.Ordinal));
        var selectorInputs = selectorRule.Paths
            .Where(IsPipelineSemanticInput)
            .Order(StringComparer.Ordinal);
        var workflowInputs = WorkflowPaths(SemanticInputsWorkflow)
            .Where(IsPipelineSemanticInput)
            .Order(StringComparer.Ordinal);

        Assert.Equal(expected, selectorInputs);
        Assert.Equal(expected, workflowInputs);
    }

    [Fact]
    public void MainSelectorPrefilterKeepsOnlyConsumedPipelineInputs()
    {
        var map = TriggerMap.Load(Path.Combine(RepoRoot.Path, "eng", "github-ci", "test-trigger-map.yml"));
        var filter = ChangedFileFilter.Create(RepoRoot.Path, map.Prefilter);

        Assert.False(filter.IsExcluded("eng/pipelines/release-publish-nuget.yml"));
        Assert.False(filter.IsExcluded("docs/specs/npm-cli-package.md"));
        Assert.True(filter.IsExcluded("eng/pipelines/azure-pipelines-public.yml"));
        Assert.True(filter.IsExcluded("eng/pipelines/templates/BuildAndTest.yml"));
        Assert.True(filter.IsExcluded("eng/Publishing.props"));
    }

    [Fact]
    public void EverySkippedRootWorkflowIsOwnedByASemanticLane()
    {
        var skipPatterns = File.ReadAllLines(Path.Combine(RepoRoot.Path, "eng", "github-ci", "ci-skip-entirely-patterns.txt"))
            .Select(line => line.Trim())
            .Where(line => line.StartsWith(".github/workflows/", StringComparison.Ordinal))
            .ToArray();
        var workflowFiles = Directory.EnumerateFiles(Path.Combine(RepoRoot.Path, ".github", "workflows"), "*.yml")
            .Select(path => Path.GetRelativePath(RepoRoot.Path, path).Replace('\\', '/'))
            .Where(path => skipPatterns.Any(pattern => TestTriggerMap.GlobMatches(pattern, path)))
            .ToArray();

        Assert.NotEmpty(workflowFiles);
        Assert.All(
            workflowFiles,
            path => Assert.True(
                WorkflowRunsFor(SemanticInputsWorkflow, path) || WorkflowRunsFor(AgenticWorkflow, path),
                $"{path} is skipped by the main CI gate but has no semantic Infrastructure.Tests lane."));
    }

    [Fact]
    public void GeneratedWorkflowInputsRemainOwnedByAgenticLane()
    {
        const string generatedWorkflow = ".github/workflows/analyze-ci-failure.lock.yml";

        Assert.True(WorkflowRunsFor(AgenticWorkflow, generatedWorkflow));
        Assert.False(WorkflowRunsFor(SemanticInputsWorkflow, generatedWorkflow));
    }

    [Fact]
    public void GeneratedWorkflowConsumersRunInAgenticLane()
    {
        Type[] consumers =
        [
            typeof(AgenticWorkflowTests),
            typeof(AnalyzeCiFailureWorkflowTests),
            typeof(ExtensionReleaseWorkflowTests),
            typeof(PrDocsCheckWorkflowTests),
            typeof(ValidateAgenticWorkflowsTests),
        ];

        Assert.All(consumers, type =>
        {
            var traits = type.GetCustomAttributesData()
                .Where(attribute => attribute.AttributeType.FullName == "Xunit.TraitAttribute")
                .Select(attribute => attribute.ConstructorArguments.Select(argument => argument.Value as string).ToArray());

            Assert.Contains(traits, values => values is ["Category", "AgenticWorkflow"]);
        });
    }

    private static bool WorkflowRunsFor(string relativePath, params string[] changedPaths)
    {
        var matcher = new Matcher(StringComparison.Ordinal, preserveFilterOrder: true);
        foreach (var path in WorkflowPaths(relativePath))
        {
            if (path.StartsWith('!'))
            {
                matcher.AddExclude(path[1..]);
            }
            else
            {
                matcher.AddInclude(path);
            }
        }

        return matcher.Match(changedPaths).HasMatches;
    }

    private static IReadOnlyList<string> WorkflowPaths(string relativePath)
    {
        var workflowPath = Path.Combine(RepoRoot.Path, relativePath);
        Assert.True(File.Exists(workflowPath), $"Expected workflow file at '{relativePath}'.");

        using var reader = File.OpenText(workflowPath);
        var yaml = new YamlStream();
        yaml.Load(reader);

        var root = Assert.IsType<YamlMappingNode>(Assert.Single(yaml.Documents).RootNode);
        var on = Assert.IsType<YamlMappingNode>(root.Children[new YamlScalarNode("on")]);
        var pullRequest = Assert.IsType<YamlMappingNode>(on.Children[new YamlScalarNode("pull_request")]);
        var paths = Assert.IsType<YamlSequenceNode>(pullRequest.Children[new YamlScalarNode("paths")]);
        return paths.Children.Select(path => path.ToString()).ToArray();
    }

    private static bool IsPipelineSemanticInput(string path)
        => path.StartsWith("eng/pipelines/", StringComparison.Ordinal)
            || s_pipelineAlignmentDocuments.Contains(path, StringComparer.Ordinal);
}

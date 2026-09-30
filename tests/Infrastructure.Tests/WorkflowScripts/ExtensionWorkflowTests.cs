// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class ExtensionWorkflowTests
{
    private static readonly YamlMappingNode s_testsWorkflow = LoadWorkflow("tests.yml");
    private static readonly YamlMappingNode s_testJobs = Mapping(s_testsWorkflow, "jobs");
    private static readonly YamlMappingNode s_extensionUnitWorkflow = LoadWorkflow("extension-unit-tests.yml");
    private static readonly YamlMappingNode s_extensionUnitJobs = Mapping(s_extensionUnitWorkflow, "jobs");
    private static readonly YamlMappingNode s_ciJobs = Mapping(LoadWorkflow("ci.yml"), "jobs");

    [Fact]
    public void FocusedExtensionWorkflowSupportsOptionalPackaging()
    {
        var workflowCall = Mapping(Mapping(s_extensionUnitWorkflow, "on"), "workflow_call");
        var inputs = Mapping(workflowCall, "inputs");
        var packageVsix = Mapping(inputs, "packageVsix");

        Assert.Equal("boolean", Scalar(packageVsix, "type"));
        Assert.Equal("true", Scalar(packageVsix, "default"));

        var extensionVersionOverride = Mapping(inputs, "extensionVersionOverride");
        Assert.Equal("string", Scalar(extensionVersionOverride, "type"));
        Assert.Equal(string.Empty, Scalar(extensionVersionOverride, "default"));
    }

    [Fact]
    public void FocusedExtensionWorkflowTestsBeforePublishingVsix()
    {
        var job = Mapping(s_extensionUnitJobs, "extension_tests_win");
        Assert.False(job.Children.ContainsKey(new YamlScalarNode("uses")));
        Assert.Equal("windows-latest", Scalar(job, "runs-on"));

        var steps = Steps(job);
        var testIndex = steps.FindIndex(step => Scalar(step, "run") == "corepack yarn test");
        var uploadIndex = steps.FindIndex(
            step => Scalar(step, "uses")?.StartsWith("actions/upload-artifact@", StringComparison.Ordinal) == true);

        Assert.True(testIndex >= 0, "The focused workflow must run the extension unit tests.");
        Assert.True(uploadIndex > testIndex, "The VSIX artifact must be produced after the unit tests execute.");

        var uploadInputs = Mapping(steps[uploadIndex], "with");
        Assert.Equal("aspire-extension", Scalar(uploadInputs, "name"));
        Assert.Equal("extension/out/aspire-extension.vsix", Scalar(uploadInputs, "path"));
    }

    [Fact]
    public void NormalTestsUseFocusedExtensionWorkflowWithPackaging()
    {
        var normalExtensionTests = Mapping(s_testJobs, "extension_tests_win");
        Assert.Equal("./.github/workflows/extension-unit-tests.yml", Scalar(normalExtensionTests, "uses"));
        Assert.Equal(
            "${{ needs.setup_for_tests.outputs.run_extension_unit == 'true' || needs.setup_for_tests.outputs.run_extension_e2e == 'true' }}",
            Scalar(normalExtensionTests, "if"));
        Assert.Equal(["setup_for_tests"], SequenceScalars(normalExtensionTests, "needs"));

        var normalInputs = Mapping(normalExtensionTests, "with");
        Assert.Equal("true", Scalar(normalInputs, "packageVsix"));
        Assert.Equal("${{ inputs.extensionVersionOverride }}", Scalar(normalInputs, "extensionVersionOverride"));
    }

    [Fact]
    public void FocusedWorkflowPackagesAfterTestFailuresOnlyWhenRequested()
    {
        var steps = Steps(Mapping(s_extensionUnitJobs, "extension_tests_win"));
        var versionOverride = Assert.Single(
            steps,
            step => Scalar(step, "run")?.Contains("yarn version", StringComparison.Ordinal) == true);
        Assert.Equal(
            "${{ inputs.packageVsix && !cancelled() && inputs.extensionVersionOverride != '' }}",
            Scalar(versionOverride, "if"));

        var packagingSteps = steps.Where(step =>
            Scalar(step, "run")?.Contains("vsce package", StringComparison.Ordinal) == true ||
            Scalar(step, "run")?.Contains("assert-extension-e2e-bridge-vsix.ps1", StringComparison.Ordinal) == true ||
            Scalar(step, "uses")?.StartsWith("actions/upload-artifact@", StringComparison.Ordinal) == true).ToList();

        Assert.NotEmpty(packagingSteps);
        Assert.All(
            packagingSteps,
            step => Assert.Equal("${{ inputs.packageVsix && !cancelled() }}", Scalar(step, "if")));
    }

    [Fact]
    public void FullTestsFinalResultsRejectFailedCancelledOrSkippedExtensionJobs()
    {
        var results = Mapping(s_testJobs, "results");
        var failureStep = Assert.Single(
            Steps(results),
            step => Scalar(step, "run")?.Contains("exit 1", StringComparison.Ordinal) == true);
        var condition = CollapseWhitespace(Scalar(failureStep, "if"));

        Assert.Contains("contains(needs.*.result, 'failure')", condition, StringComparison.Ordinal);
        Assert.Contains("contains(needs.*.result, 'cancelled')", condition, StringComparison.Ordinal);
        Assert.Contains("needs.extension_tests_win.result == 'skipped'", condition, StringComparison.Ordinal);
        Assert.Contains("needs.extension_e2e_tests.result == 'skipped'", condition, StringComparison.Ordinal);
    }

    [Fact]
    public void FullTestsWorkflowAlwaysAggregatesTestResults()
    {
        var results = Mapping(s_testJobs, "results");
        var steps = Steps(results);
        var downloads = steps.Where(
            step => Scalar(step, "uses")?.StartsWith("actions/download-artifact@", StringComparison.Ordinal) == true).ToList();

        Assert.Equal(
            ["logs-*-ubuntu-latest", "logs-*-windows-latest", "logs-*-macos-latest"],
            downloads.Select(step => Scalar(Mapping(step, "with"), "pattern")));
        Assert.All(downloads, step => Assert.False(step.Children.ContainsKey(new YamlScalarNode("if"))));

        var upload = Assert.Single(
            steps,
            step => Scalar(step, "uses")?.StartsWith("actions/upload-artifact@", StringComparison.Ordinal) == true);
        Assert.Equal("All-TestResults", Scalar(Mapping(upload, "with"), "name"));
        Assert.Equal("${{ always() }}", Scalar(upload, "if"));

        string[] reportProjects =
        [
            "tools/GenerateTestSummary/GenerateTestSummary.csproj",
            "tools/GenerateCITimeline/GenerateCITimeline.csproj",
        ];
        Assert.All(reportProjects, project =>
        {
            var step = Assert.Single(
                steps,
                candidate => Scalar(candidate, "run")?.Contains(project, StringComparison.Ordinal) == true);
            Assert.Equal("${{ always() }}", Scalar(step, "if"));
        });
    }

    [Fact]
    public void FocusedWorkflowUsesReadOnlyContentsPermission()
    {
        var workflowPermissions = Mapping(s_extensionUnitWorkflow, "permissions");

        Assert.Equal(["contents"], workflowPermissions.Children.Keys.Cast<YamlScalarNode>().Select(key => key.Value));
        Assert.Equal("read", Scalar(workflowPermissions, "contents"));
    }

    [Fact]
    public void CiRunsSelectorDrivenTestsAndStabilizationForEveryRequiredPr()
    {
        var normalTests = Mapping(s_ciJobs, "tests");
        Assert.Equal("./.github/workflows/tests.yml", Scalar(normalTests, "uses"));
        Assert.Equal(
            "${{ github.repository_owner == 'microsoft' && needs.prepare_for_ci.outputs.skip_workflow != 'true' }}",
            Scalar(normalTests, "if"));

        Assert.Equal(
            "${{ github.repository_owner == 'microsoft' && needs.prepare_for_ci.outputs.skip_workflow != 'true' }}",
            Scalar(Mapping(s_ciJobs, "stabilization_check"), "if"));
    }

    [Fact]
    public void CiRunsActionlintRegardlessOfBuildSkipDecision()
    {
        var actionlint = Mapping(s_ciJobs, "actionlint");

        Assert.False(actionlint.Children.ContainsKey(new YamlScalarNode("needs")));
        Assert.Equal("${{ github.repository_owner == 'microsoft' }}", Scalar(actionlint, "if"));
    }

    [Fact]
    public void FinalResultsAlwaysRequireActionlintAndConditionallyRequireBuildJobs()
    {
        var results = Mapping(s_ciJobs, "results");
        Assert.Equal(
            ["actionlint", "prepare_for_ci", "tests", "stabilization_check"],
            SequenceScalars(results, "needs"));

        var failureStep = Assert.Single(
            Steps(results),
            step => Scalar(step, "run")?.Contains("exit 1", StringComparison.Ordinal) == true);
        Assert.Equal(
            "${{ always() && (needs.actionlint.result != 'success' || " +
            "(needs.prepare_for_ci.outputs.skip_workflow != 'true' && " +
            "(contains(needs.*.result, 'failure') || contains(needs.*.result, 'cancelled') || " +
            "needs.tests.result != 'success' || needs.stabilization_check.result != 'success'))) }}",
            CollapseWhitespace(Scalar(failureStep, "if")));
    }

    [Fact]
    public void CiFailureTrackerIncludesActionlintInRedMainContract()
    {
        var tracker = Mapping(s_ciJobs, "ci_failure_tracker");

        Assert.Equal(["actionlint", "prepare_for_ci", "tests", "stabilization_check"], SequenceScalars(tracker, "needs"));
        Assert.Equal(
            "${{ always() && github.event_name == 'push' && github.repository_owner == 'microsoft' }}",
            Scalar(tracker, "if"));

        var scriptStep = Assert.Single(
            Steps(tracker),
            step => Scalar(step, "uses")?.StartsWith("actions/github-script@", StringComparison.Ordinal) == true);
        var environment = Mapping(scriptStep, "env");
        Assert.Equal("${{ contains(needs.*.result, 'failure') }}", Scalar(environment, "CI_RED"));
        Assert.Equal(
            "${{ needs.actionlint.result == 'success' && needs.prepare_for_ci.result == 'success' && " +
            "needs.tests.result == 'success' && needs.stabilization_check.result == 'success' }}",
            CollapseWhitespace(Scalar(environment, "CI_GREEN")));
    }

    private static List<YamlMappingNode> Steps(YamlMappingNode job)
        => ((YamlSequenceNode)job.Children[new YamlScalarNode("steps")]).Cast<YamlMappingNode>().ToList();

    private static List<string?> SequenceScalars(YamlMappingNode node, string key)
        => ((YamlSequenceNode)node.Children[new YamlScalarNode(key)]).Cast<YamlScalarNode>().Select(item => item.Value).ToList();

    private static YamlMappingNode Mapping(YamlMappingNode node, string key)
        => Assert.IsType<YamlMappingNode>(node.Children[new YamlScalarNode(key)]);

    private static string? Scalar(YamlMappingNode node, string key)
        => node.Children.TryGetValue(new YamlScalarNode(key), out var value) && value is YamlScalarNode scalar
            ? scalar.Value
            : null;

    private static string CollapseWhitespace(string? value)
        => string.Join(' ', (value ?? string.Empty).Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));

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

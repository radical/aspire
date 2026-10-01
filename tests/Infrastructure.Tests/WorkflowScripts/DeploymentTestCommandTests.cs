// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.TestUtilities;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class DeploymentTestCommandTests(ITestOutputHelper output)
{
    private const string ScriptDirectory = ".github/workflows/deployment-test-command";
    private const string PermissionScript = "check-permission.js";
    private const string PullRequestScript = "get-pull-request.js";
    private const string DispatchScript = "dispatch-deployment-tests.js";

    [Fact]
    public void CommandPrefilterSelectsCandidatesOnMicrosoftPullRequests()
    {
        var condition = Scalar(LoadJob(), "if");
        Assert.Equal(
            "${{ startsWith(github.event.comment.body, '/deployment-test') && github.event.issue.pull_request && github.repository_owner == 'microsoft' }}",
            string.Join(" ", condition.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)));
    }

    [Fact]
    public void DeploymentStepsRequireRepositoryWriteAccess()
    {
        var steps = LoadSteps();
        var permissionStep = Assert.Single(steps, step => ScalarOrNull(step, "id") == "check_permission");
        foreach (var step in steps.Where(step => !ReferenceEquals(step, permissionStep)))
        {
            if (ScalarOrNull(step, "uses")?.StartsWith("actions/github-script@", StringComparison.Ordinal) == true)
            {
                Assert.Equal("steps.check_permission.outputs.has_write_access == 'true'", Scalar(step, "if"));
            }
        }
    }

    [Fact]
    public void WorkflowLoadsCommandScriptsFromWorkflowDefinitionCommit()
    {
        var steps = LoadSteps();
        var checkout = Assert.Single(
            steps,
            step => ScalarOrNull(step, "name") == "Check out deployment command scripts");
        var checkoutOptions = Mapping(checkout, "with");
        Assert.Equal("${{ github.workflow_sha }}", Scalar(checkoutOptions, "ref"));
        Assert.Equal(".deployment-test-command", Scalar(checkoutOptions, "path"));
        Assert.Equal("false", Scalar(checkoutOptions, "persist-credentials"));
        Assert.Equal($"{ScriptDirectory}\n", Scalar(checkoutOptions, "sparse-checkout")?.ReplaceLineEndings("\n"));

        AssertScriptStep(steps, "check_permission", PermissionScript);
        AssertScriptStep(steps, "pr", PullRequestScript);
        AssertScriptStep(steps, id: null, DispatchScript, name: "Trigger deployment tests");
    }

    [Theory]
    [InlineData("write")]
    [InlineData("admin")]
    [InlineData("maintain")]
    [InlineData("read")]
    [InlineData("triage")]
    [InlineData("none")]
    [InlineData("unknown")]
    [InlineData("error-403")]
    [InlineData("error-404")]
    [InlineData("error-500")]
    [RequiresTools(["node"])]
    public async Task PermissionCheckOnlyAllowsRepositoryWriters(string scenario)
        => await RunPermissionCheckAsync(scenario, "/deployment-test", validCommand: true);

    [Theory]
    [InlineData("/deployment-test", true)]
    [InlineData("/deployment-test ", true)]
    [InlineData("/deployment-test arguments", true)]
    [InlineData("/deployment-test\n", true)]
    [InlineData("/deployment-test\r", true)]
    [InlineData("/deployment-test\r\nMore text", true)]
    [InlineData("/deployment-test\targuments", true)]
    [InlineData("/DEPLOYMENT-TEST\nMore text", true)]
    [InlineData("/deployment-testing", false)]
    [InlineData("/deployment-test-disabled", false)]
    [InlineData("/deployment-test/extra", false)]
    [InlineData("/deployment-test.", false)]
    [InlineData(" /deployment-test", false)]
    [InlineData("Please run /deployment-test", false)]
    [InlineData("", false)]
    [RequiresTools(["node"])]
    public async Task CommandRequiresEndOfTextOrWhitespace(string body, bool validCommand)
        => await RunPermissionCheckAsync("write", body, validCommand);

    [Theory]
    [InlineData("same-repository")]
    [InlineData("fork")]
    [RequiresTools(["node"])]
    public async Task PullRequestLookupRejectsForkHeads(string scenario)
        => await RunScriptHarnessAsync(PullRequestScript, "pull-request", scenario);

    [Theory]
    [InlineData("valid")]
    [InlineData("missing-head")]
    [InlineData("invalid-number")]
    [InlineData("api-error")]
    [RequiresTools(["node"])]
    public async Task DispatchUsesValidatedPullRequestOutputs(string scenario)
        => await RunScriptHarnessAsync(DispatchScript, "dispatch", scenario);

    private async Task RunPermissionCheckAsync(string scenario, string body, bool validCommand)
    {
        using var node = new NodeCommand(output, nameof(DeploymentTestCommandTests))
            .WithTimeout(TimeSpan.FromMinutes(1));
        var result = await node.ExecuteScriptAsync(
            Path.Combine(RepoRoot.Path, "tests", "Infrastructure.Tests", "WorkflowScripts", "deployment-test-command.harness.mjs"),
            GetScriptPath(PermissionScript),
            "permission",
            scenario,
            body,
            validCommand ? "true" : "false");
        Assert.True(result.ExitCode == 0, result.Output);
    }

    private async Task RunScriptHarnessAsync(string scriptName, string command, string scenario)
    {
        using var node = new NodeCommand(output, nameof(DeploymentTestCommandTests))
            .WithTimeout(TimeSpan.FromMinutes(1));
        var result = await node.ExecuteScriptAsync(
            Path.Combine(RepoRoot.Path, "tests", "Infrastructure.Tests", "WorkflowScripts", "deployment-test-command.harness.mjs"),
            GetScriptPath(scriptName),
            command,
            scenario);
        Assert.True(result.ExitCode == 0, result.Output);
    }

    private static string GetScriptPath(string scriptName)
        => Path.Combine(RepoRoot.Path, ScriptDirectory, scriptName);

    private static void AssertScriptStep(
        IReadOnlyList<YamlMappingNode> steps,
        string? id,
        string scriptName,
        string? name = null)
    {
        var step = Assert.Single(
            steps,
            step => (id is null || ScalarOrNull(step, "id") == id)
                && (name is null || ScalarOrNull(step, "name") == name));
        var script = Scalar(Mapping(step, "with"), "script");
        Assert.Equal(
            $"const run = require('${{{{ github.workspace }}}}/.deployment-test-command/{ScriptDirectory}/{scriptName}');\nawait run({{ github, context, core }});",
            script.ReplaceLineEndings("\n").TrimEnd());
    }

    private static YamlMappingNode[] LoadSteps()
    {
        var steps = Assert.IsType<YamlSequenceNode>(LoadJob().Children[new YamlScalarNode("steps")]);
        return steps.Children.Select(Assert.IsType<YamlMappingNode>).ToArray();
    }

    private static YamlMappingNode LoadJob()
    {
        using var reader = File.OpenText(Path.Combine(RepoRoot.Path, ".github", "workflows", "deployment-test-command.yml"));
        var yaml = new YamlStream();
        yaml.Load(reader);
        var root = Assert.IsType<YamlMappingNode>(yaml.Documents[0].RootNode);
        var jobs = Assert.IsType<YamlMappingNode>(root.Children[new YamlScalarNode("jobs")]);
        return Assert.IsType<YamlMappingNode>(jobs.Children[new YamlScalarNode("deployment-test")]);
    }

    private static string Scalar(YamlMappingNode node, string key)
        => Assert.IsType<YamlScalarNode>(node.Children[new YamlScalarNode(key)]).Value!;

    private static string? ScalarOrNull(YamlMappingNode node, string key)
        => node.Children.TryGetValue(new YamlScalarNode(key), out var value)
            ? Assert.IsType<YamlScalarNode>(value).Value
            : null;

    private static YamlMappingNode Mapping(YamlMappingNode node, string key)
        => Assert.IsType<YamlMappingNode>(node.Children[new YamlScalarNode(key)]);
}

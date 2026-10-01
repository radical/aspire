// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text;
using Aspire.TestUtilities;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class ExtensionReleaseWorkflowTests(ITestOutputHelper testOutput)
{
    private static readonly string s_releaseWorkflowPath = Path.Combine(RepoRoot.Path, ".github", "workflows", "extension-release.yml");
    private static readonly string s_changelogWorkflowPath = Path.Combine(RepoRoot.Path, ".github", "workflows", "extension-changelog.md");
    private static readonly string s_changelogWorkflowLockPath = Path.Combine(RepoRoot.Path, ".github", "workflows", "extension-changelog.lock.yml");
    private static readonly string s_releaseNotesGeneratorPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "extension-release",
        "generate_deterministic_release_notes.py");
    private static readonly string s_applyTriggerLabelScriptPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "extension-release",
        "apply_extension_release_trigger_label.sh");
    private static readonly string s_prBodyValidatorPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "extension-release",
        "validate_github_pr_body.py");
    private static readonly string s_preloadChangelogRangeScriptPath = Path.Combine(
        RepoRoot.Path,
        ".github",
        "workflows",
        "extension-changelog",
        "preload-authoritative-range.sh");

    [Fact]
    public void ExtensionReleaseWorkflowLoadsHelpersFromWorkflowDefinitionCommit()
    {
        var yaml = new YamlStream();
        using var reader = new StringReader(File.ReadAllText(s_releaseWorkflowPath));
        yaml.Load(reader);

        var root = (YamlMappingNode)yaml.Documents[0].RootNode;
        var jobs = (YamlMappingNode)root.Children[new YamlScalarNode("jobs")];
        var prepareReleaseJob = (YamlMappingNode)jobs.Children[new YamlScalarNode("prepare-release")];
        var steps = ((YamlSequenceNode)prepareReleaseJob.Children[new YamlScalarNode("steps")]).Cast<YamlMappingNode>().ToList();

        var checkoutRepositoryStep = Assert.Single(steps, step => Scalar(step, "name") == "Checkout Repository");
        Assert.StartsWith("actions/checkout@", Scalar(checkoutRepositoryStep, "uses"));
        var checkoutRepositoryWith = Assert.IsType<YamlMappingNode>(checkoutRepositoryStep.Children[new YamlScalarNode("with")]);
        Assert.Equal("main", Scalar(checkoutRepositoryWith, "ref"));
        Assert.Equal("0", Scalar(checkoutRepositoryWith, "fetch-depth"));
        Assert.Equal("false", Scalar(checkoutRepositoryWith, "persist-credentials"));

        var helperCheckoutStep = Assert.Single(steps, step => Scalar(step, "name") == "Checkout workflow helper scripts");
        Assert.StartsWith("actions/checkout@", Scalar(helperCheckoutStep, "uses"));
        var helperCheckoutWith = Assert.IsType<YamlMappingNode>(helperCheckoutStep.Children[new YamlScalarNode("with")]);
        Assert.Equal("${{ github.workflow_sha }}", Scalar(helperCheckoutWith, "ref"));
        Assert.Equal(".extension-release-workflow-source", Scalar(helperCheckoutWith, "path"));
        Assert.Equal("1", Scalar(helperCheckoutWith, "fetch-depth"));
        Assert.Equal("false", Scalar(helperCheckoutWith, "persist-credentials"));
        Assert.Equal(".github/workflows/extension-release\n", Scalar(helperCheckoutWith, "sparse-checkout")?.ReplaceLineEndings("\n"));

        Assert.Contains(
            steps,
            step => Scalar(step, "run")?.Contains(
                "python3 .extension-release-workflow-source/.github/workflows/extension-release/generate_deterministic_release_notes.py",
                StringComparison.Ordinal) == true);
        Assert.Contains(
            steps,
            step => Scalar(step, "run")?.Contains(
                "python3 .extension-release-workflow-source/.github/workflows/extension-release/validate_github_pr_body.py",
                StringComparison.Ordinal) == true);
        Assert.Contains(
            steps,
            step => Scalar(step, "run")?.Contains(
                "bash .extension-release-workflow-source/.github/workflows/extension-release/apply_extension_release_trigger_label.sh",
                StringComparison.Ordinal) == true);
    }

    [Fact]
    public void ChangelogWorkflowExecutesTrustedPreloadBeforeCredentialCleanup()
    {
        var sourceRoot = LoadAgenticWorkflowSource(s_changelogWorkflowPath);
        var sourceSteps = Sequence(sourceRoot, "pre-agent-steps").Children.Cast<YamlMappingNode>().ToList();
        var helperCheckout = Assert.Single(
            sourceSteps,
            step => Scalar(step, "name") == "Check out changelog workflow helper");
        var helperCheckoutWith = Assert.IsType<YamlMappingNode>(helperCheckout.Children[new YamlScalarNode("with")]);
        Assert.Equal("${{ github.workflow_sha }}", Scalar(helperCheckoutWith, "ref"));
        Assert.Equal(".extension-changelog-workflow-source", Scalar(helperCheckoutWith, "path"));
        Assert.Equal("false", Scalar(helperCheckoutWith, "persist-credentials"));
        Assert.Equal(
            ".github/workflows/extension-changelog\n",
            Scalar(helperCheckoutWith, "sparse-checkout")?.ReplaceLineEndings("\n"));
        var sourcePreload = Assert.Single(
            sourceSteps,
            step => Scalar(step, "name") == "Preload authoritative marker range for local changelog enumeration");
        Assert.True(sourceSteps.IndexOf(sourcePreload) > sourceSteps.IndexOf(helperCheckout));
        Assert.Equal(
            "bash .extension-changelog-workflow-source/.github/workflows/extension-changelog/preload-authoritative-range.sh",
            Scalar(sourcePreload, "run"));

        var compiledRoot = LoadYamlWorkflow(s_changelogWorkflowLockPath);
        var compiledSteps = GetJobSteps(compiledRoot, "agent");
        var compiledHelperCheckout = Step(compiledSteps, "Check out changelog workflow helper");
        var preload = Step(compiledSteps, "Preload authoritative marker range for local changelog enumeration");
        var preloadIndex = compiledSteps.IndexOf(preload);
        var configureIndex = compiledSteps.FindLastIndex(
            preloadIndex,
            step => Scalar(step, "name") == "Configure Git credentials");
        var checkoutPrIndex = compiledSteps.FindLastIndex(
            preloadIndex,
            step => Scalar(step, "name") == "Checkout PR branch");
        var cleanIndex = compiledSteps.FindIndex(
            preloadIndex + 1,
            step => Scalar(step, "name") == "Clean credentials");
        Assert.True(configureIndex >= 0);
        Assert.True(checkoutPrIndex >= 0);
        Assert.True(cleanIndex > preloadIndex);
        Assert.True(compiledSteps.IndexOf(compiledHelperCheckout) > checkoutPrIndex);
        Assert.True(preloadIndex > configureIndex);
        Assert.True(preloadIndex > checkoutPrIndex);
        Assert.True(compiledSteps.IndexOf(preload) > compiledSteps.IndexOf(compiledHelperCheckout));
        Assert.True(preloadIndex < cleanIndex);
        Assert.Equal(
            "bash .extension-changelog-workflow-source/.github/workflows/extension-changelog/preload-authoritative-range.sh",
            Scalar(preload, "run"));
    }

    [Theory]
    [InlineData("materializes-range")]
    [InlineData("no-marker")]
    [InlineData("invalid-marker")]
    [RequiresTools(["bash", "git"])]
    public async Task ChangelogRangePreloadUsesRepositoryHistory(string scenario)
    {
        using var workspace = TemporaryWorkspace.Create(testOutput);
        GitCli.Run(workspace.Path, "init", "-q", "-b", "main");
        GitCli.Run(workspace.Path, "config", "user.email", "test@example.com");
        GitCli.Run(workspace.Path, "config", "user.name", "Test");
        GitCli.Run(workspace.Path, "config", "commit.gpgsign", "false");
        Directory.CreateDirectory(Path.Combine(workspace.Path, "extension"));
        await File.WriteAllTextAsync(Path.Combine(workspace.Path, "extension", "feature.txt"), "baseline\n");
        GitCli.Run(workspace.Path, "add", "extension/feature.txt");
        GitCli.Run(workspace.Path, "commit", "-q", "-m", "Baseline");
        var fromSha = GitCli.Run(workspace.Path, "rev-parse", "HEAD").Trim();
        await File.AppendAllTextAsync(Path.Combine(workspace.Path, "extension", "feature.txt"), "changed\n");
        GitCli.Run(workspace.Path, "add", "extension/feature.txt");
        GitCli.Run(workspace.Path, "commit", "-q", "-m", "feat: Add extension behavior");
        var toSha = GitCli.Run(workspace.Path, "rev-parse", "HEAD").Trim();

        var marker = scenario switch
        {
            "materializes-range" => $"<!-- aspire-ext-changelog from={fromSha} to={toSha} base=1.0.0 -->",
            "invalid-marker" => "<!-- aspire-ext-changelog from=invalid to=invalid base=1.0.0 -->",
            _ => "## v1.0.0",
        };
        await File.WriteAllTextAsync(Path.Combine(workspace.Path, "extension", "CHANGELOG.md"), marker + "\n");

        var runnerTemp = workspace.CreateDirectory("runner").FullName;
        var result = await RunBashScriptAsync(
            s_preloadChangelogRangeScriptPath,
            [],
            new Dictionary<string, string?> { ["RUNNER_TEMP"] = runnerTemp },
            workspace.Path);

        if (scenario == "invalid-marker")
        {
            Assert.NotEqual(0, result.ExitCode);
            Assert.Contains("Could not parse authoritative marker", result.Output, StringComparison.Ordinal);
            return;
        }

        Assert.Equal(0, result.ExitCode);
        var candidatesPath = Path.Combine(runnerTemp, "gh-aw", "extension-changelog-candidates.tsv");
        if (scenario == "no-marker")
        {
            Assert.False(File.Exists(candidatesPath));
            Assert.Contains("No pending aspire-ext-changelog marker", result.Output, StringComparison.Ordinal);
            return;
        }

        Assert.Equal(
            $"{toSha}\tfeat: Add extension behavior\n",
            (await File.ReadAllTextAsync(candidatesPath)).ReplaceLineEndings("\n"));
    }

    [Fact]
    [RequiresTools(["bash", "git"])]
    public async Task ChangelogRangePreloadDeepensShallowCheckout()
    {
        using var workspace = TemporaryWorkspace.Create(testOutput);
        var sourcePath = workspace.CreateDirectory("source").FullName;
        GitCli.Run(sourcePath, "init", "-q", "-b", "main");
        GitCli.Run(sourcePath, "config", "user.email", "test@example.com");
        GitCli.Run(sourcePath, "config", "user.name", "Test");
        GitCli.Run(sourcePath, "config", "commit.gpgsign", "false");
        Directory.CreateDirectory(Path.Combine(sourcePath, "extension"));
        await File.WriteAllTextAsync(Path.Combine(sourcePath, "extension", "feature.txt"), "baseline\n");
        GitCli.Run(sourcePath, "add", "extension/feature.txt");
        GitCli.Run(sourcePath, "commit", "-q", "-m", "Baseline");
        var fromSha = GitCli.Run(sourcePath, "rev-parse", "HEAD").Trim();
        await File.AppendAllTextAsync(Path.Combine(sourcePath, "extension", "feature.txt"), "changed\n");
        GitCli.Run(sourcePath, "add", "extension/feature.txt");
        GitCli.Run(sourcePath, "commit", "-q", "-m", "feat: Add extension behavior");
        var toSha = GitCli.Run(sourcePath, "rev-parse", "HEAD").Trim();
        await File.WriteAllTextAsync(
            Path.Combine(sourcePath, "extension", "CHANGELOG.md"),
            $"<!-- aspire-ext-changelog from={fromSha} to={toSha} base=1.0.0 -->\n");
        GitCli.Run(sourcePath, "add", "extension/CHANGELOG.md");
        GitCli.Run(sourcePath, "commit", "-q", "-m", "Add pending changelog marker");

        var originPath = Path.Combine(workspace.Path, "origin.git");
        GitCli.Run(workspace.Path, "clone", "-q", "--bare", sourcePath, originPath);
        var checkoutPath = Path.Combine(workspace.Path, "checkout");
        GitCli.Run(
            workspace.Path,
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "main",
            new Uri(originPath).AbsoluteUri,
            checkoutPath);
        Assert.Equal("true", GitCli.Run(checkoutPath, "rev-parse", "--is-shallow-repository").Trim());

        var runnerTemp = workspace.CreateDirectory("runner").FullName;
        var result = await RunBashScriptAsync(
            s_preloadChangelogRangeScriptPath,
            [],
            new Dictionary<string, string?> { ["RUNNER_TEMP"] = runnerTemp },
            checkoutPath);

        Assert.Equal(0, result.ExitCode);
        Assert.Contains("Deepening main by 128 commits", result.Output, StringComparison.Ordinal);
        Assert.Contains("Preloaded authoritative marker range", result.Output, StringComparison.Ordinal);
        var candidatesPath = Path.Combine(runnerTemp, "gh-aw", "extension-changelog-candidates.tsv");
        Assert.Equal(
            $"{toSha}\tfeat: Add extension behavior\n",
            (await File.ReadAllTextAsync(candidatesPath)).ReplaceLineEndings("\n"));
    }

    [Fact]
    [RequiresTools(["bash", "git"])]
    public async Task ChangelogRangePreloadFailsWhenRangeIsAbsentFromOrigin()
    {
        using var workspace = TemporaryWorkspace.Create(testOutput);
        var sourcePath = workspace.CreateDirectory("source").FullName;
        GitCli.Run(sourcePath, "init", "-q", "-b", "main");
        GitCli.Run(sourcePath, "config", "user.email", "test@example.com");
        GitCli.Run(sourcePath, "config", "user.name", "Test");
        GitCli.Run(sourcePath, "config", "commit.gpgsign", "false");
        Directory.CreateDirectory(Path.Combine(sourcePath, "extension"));
        await File.WriteAllTextAsync(Path.Combine(sourcePath, "extension", "feature.txt"), "baseline\n");
        await File.WriteAllTextAsync(
            Path.Combine(sourcePath, "extension", "CHANGELOG.md"),
            $"<!-- aspire-ext-changelog from={new string('1', 40)} to={new string('2', 40)} base=1.0.0 -->\n");
        GitCli.Run(sourcePath, "add", "extension");
        GitCli.Run(sourcePath, "commit", "-q", "-m", "Add pending changelog marker");

        var originPath = Path.Combine(workspace.Path, "origin.git");
        GitCli.Run(workspace.Path, "clone", "-q", "--bare", sourcePath, originPath);
        var checkoutPath = Path.Combine(workspace.Path, "checkout");
        GitCli.Run(
            workspace.Path,
            "clone",
            "-q",
            "--depth",
            "1",
            "--branch",
            "main",
            new Uri(originPath).AbsoluteUri,
            checkoutPath);

        var runnerTemp = workspace.CreateDirectory("runner").FullName;
        var result = await RunBashScriptAsync(
            s_preloadChangelogRangeScriptPath,
            [],
            new Dictionary<string, string?> { ["RUNNER_TEMP"] = runnerTemp },
            checkoutPath);

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("Failed to preload authoritative marker range", result.Output, StringComparison.Ordinal);
        Assert.False(File.Exists(Path.Combine(runnerTemp, "gh-aw", "extension-changelog-candidates.tsv")));
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task ApplyingTriggerLabelFailsWhenExistingLabelCannotBeRemoved()
    {
        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-label-failure");
        try
        {
            var fakeGh = await CreateFakeGhAsync(tempDirectory.FullName);
            var result = await RunBashScriptAsync(
                s_applyTriggerLabelScriptPath,
                ["123"],
                new Dictionary<string, string?>
                {
                    ["GH_CALL_LOG"] = fakeGh.CallLogPath,
                    ["GH_HAS_LABEL"] = "true",
                    ["GH_REMOVE_LABEL_EXIT_CODE"] = "1",
                    ["PATH"] = fakeGh.PathEnvironment,
                });

            Assert.NotEqual(0, result.ExitCode);
            Assert.Contains("Failed to remove existing 'vscode-extension-release' label", result.Output, StringComparison.Ordinal);
            Assert.False(
                (await File.ReadAllTextAsync(fakeGh.CallLogPath)).Contains("--add-label vscode-extension-release", StringComparison.Ordinal),
                "The helper must not re-add the label after a failed removal.");
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task ApplyingTriggerLabelAddsLabelWithoutRemovingWhenMissing()
    {
        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-label-add");
        try
        {
            var fakeGh = await CreateFakeGhAsync(tempDirectory.FullName);
            var result = await RunBashScriptAsync(
                s_applyTriggerLabelScriptPath,
                ["123"],
                new Dictionary<string, string?>
                {
                    ["GH_CALL_LOG"] = fakeGh.CallLogPath,
                    ["GH_HAS_LABEL"] = "false",
                    ["PATH"] = fakeGh.PathEnvironment,
                });

            Assert.Equal(0, result.ExitCode);

            var callLog = await File.ReadAllTextAsync(fakeGh.CallLogPath);
            Assert.Contains("pr view 123 --json labels --jq .labels[].name", callLog, StringComparison.Ordinal);
            Assert.DoesNotContain("--remove-label vscode-extension-release", callLog, StringComparison.Ordinal);
            Assert.Contains("pr edit 123 --add-label vscode-extension-release", callLog, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    [Fact]
    [RequiresTools(["bash"])]
    public async Task ApplyingTriggerLabelReAddsExistingLabelAfterSuccessfulRemoval()
    {
        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-label-readd");
        try
        {
            var fakeGh = await CreateFakeGhAsync(tempDirectory.FullName);
            var result = await RunBashScriptAsync(
                s_applyTriggerLabelScriptPath,
                ["123"],
                new Dictionary<string, string?>
                {
                    ["GH_CALL_LOG"] = fakeGh.CallLogPath,
                    ["GH_HAS_LABEL"] = "true",
                    ["PATH"] = fakeGh.PathEnvironment,
                });

            Assert.Equal(0, result.ExitCode);

            var callLog = (await File.ReadAllTextAsync(fakeGh.CallLogPath)).ReplaceLineEndings("\n");
            var removeIndex = callLog.IndexOf("pr edit 123 --remove-label vscode-extension-release", StringComparison.Ordinal);
            var addIndex = callLog.IndexOf("pr edit 123 --add-label vscode-extension-release", StringComparison.Ordinal);

            Assert.True(removeIndex >= 0, "Expected the helper to remove the existing trigger label before re-adding it.");
            Assert.True(addIndex > removeIndex, "Expected the helper to re-add the trigger label after removing it.");
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task DeterministicFallbackIncludesEveryAcceptedCommitOnWindows()
        => DeterministicFallbackIncludesEveryAcceptedCommit("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task DeterministicFallbackIncludesEveryAcceptedCommitOnUnix()
        => DeterministicFallbackIncludesEveryAcceptedCommit("python3");

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task DeterministicFallbackAllowsRenderedOutputLargerThanEightThousandBytesOnWindows()
        => DeterministicFallbackAllowsRenderedOutputLargerThanEightThousandBytes("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task DeterministicFallbackAllowsRenderedOutputLargerThanEightThousandBytesOnUnix()
        => DeterministicFallbackAllowsRenderedOutputLargerThanEightThousandBytes("python3");

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task DeterministicFallbackSanitizesSplitlinesControlCharactersWithoutSplittingCommitsOnWindows()
        => DeterministicFallbackSanitizesSplitlinesControlCharactersWithoutSplittingCommits("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task DeterministicFallbackSanitizesSplitlinesControlCharactersWithoutSplittingCommitsOnUnix()
        => DeterministicFallbackSanitizesSplitlinesControlCharactersWithoutSplittingCommits("python3");

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task DeterministicFallbackStripsPrSuffixFromCrLfInputOnWindows()
        => DeterministicFallbackStripsPrSuffixFromCrLfInput("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task DeterministicFallbackStripsPrSuffixFromCrLfInputOnUnix()
        => DeterministicFallbackStripsPrSuffixFromCrLfInput("python3");

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task GitHubPullRequestBodyValidatorAcceptsBodiesAtLimitOnWindows()
        => GitHubPullRequestBodyValidatorAcceptsBodiesAtLimit("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task GitHubPullRequestBodyValidatorAcceptsBodiesAtLimitOnUnix()
        => GitHubPullRequestBodyValidatorAcceptsBodiesAtLimit("python3");

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task GitHubPullRequestBodyValidatorRejectsBodiesOverLimitOnWindows()
        => GitHubPullRequestBodyValidatorRejectsBodiesOverLimit("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task GitHubPullRequestBodyValidatorRejectsBodiesOverLimitOnUnix()
        => GitHubPullRequestBodyValidatorRejectsBodiesOverLimit("python3");

    private async Task DeterministicFallbackIncludesEveryAcceptedCommit(string pythonExecutable)
    {
        var output = await GenerateDeterministicReleaseNotesAsync(
            pythonExecutable,
            [
                "1111111\tfeat: Add Aspire dashboard tree view (#100)",
                "2222222\tfix(settings): Preserve <b>launch</b> profile labels (#101)",
                "3333333\tdocs: Explain terminal attach behavior (#102)",
                "4444444\tchore(extension): Surface package warnings\a in output (#103)",
                "5555555\trefactor: Improve project detection after restore (#104)",
                "6666666\tperf: Speed up solution scanning (#105)",
                "7777777\tfeat(tree): Show Azure resources in explorer (#106)",
                "8888888\tfix: Respect multi-root workspaces (#107)",
                "9999999\tfeat: Add walkthrough links (#108)",
                "aaaaaaa\tfix(debug): Keep env vars when reloading (#109)",
                "bbbbbbb\tfeat(commands): Support remove service action (#110)",
                "ccccccc\tchore: Handle workspace rename notifications (#111)",
                "ddddddd\tRelease 1.10.1",
                "eeeeeee\tBump package-lock.json",
                "fffffff\tUpdate yarn.lock"
            ]);

        var bulletLines = output.ReplaceLineEndings("\n")
            .Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Where(line => line.StartsWith("- ", StringComparison.Ordinal))
            .ToArray();

        Assert.Equal(
            [
                "- Add Aspire dashboard tree view",
                "- Preserve launch profile labels",
                "- Explain terminal attach behavior",
                "- Surface package warnings in output",
                "- Improve project detection after restore",
                "- Speed up solution scanning",
                "- Show Azure resources in explorer",
                "- Respect multi-root workspaces",
                "- Add walkthrough links",
                "- Keep env vars when reloading",
                "- Support remove service action",
                "- Handle workspace rename notifications"
            ],
            bulletLines);
    }

    private async Task DeterministicFallbackAllowsRenderedOutputLargerThanEightThousandBytes(string pythonExecutable)
    {
        var commitLines = Enumerable.Range(1, 12)
            .Select(index =>
            {
                var message = $"candidate note {index:D2} " + new string((char)('a' + (index % 26)), 720);
                return $"{index:x7}\tfeat: {message}";
            })
            .ToArray();

        var output = await GenerateDeterministicReleaseNotesAsync(pythonExecutable, commitLines);
        var lastMessage = $"candidate note 12 {new string('m', 720)}";

        Assert.True(
            Encoding.UTF8.GetByteCount(output) > 8000,
            $"Expected rendered fallback to exceed 8000 bytes, but it was {Encoding.UTF8.GetByteCount(output)} bytes.");
        Assert.Contains($"- {lastMessage}", output, StringComparison.Ordinal);
    }

    private async Task DeterministicFallbackSanitizesSplitlinesControlCharactersWithoutSplittingCommits(string pythonExecutable)
    {
        var output = await GenerateDeterministicReleaseNotesAsync(
            pythonExecutable,
            [
                "1111111\tfeat: Alpha\rBeta (#100)",
                "2222222\tfix: Gamma\vDelta (#101)"
            ]);

        var bulletLines = output.ReplaceLineEndings("\n")
            .Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Where(line => line.StartsWith("- ", StringComparison.Ordinal))
            .ToArray();

        Assert.Equal(
            [
                "- AlphaBeta",
                "- GammaDelta"
            ],
            bulletLines);
    }

    private async Task DeterministicFallbackStripsPrSuffixFromCrLfInput(string pythonExecutable)
    {
        var output = await GenerateDeterministicReleaseNotesAsync(
            pythonExecutable,
            ["1111111\tfeat: Alpha (#100)"],
            lineEnding: "\r\n");

        var bulletLines = output.ReplaceLineEndings("\n")
            .Split('\n', StringSplitOptions.RemoveEmptyEntries)
            .Where(line => line.StartsWith("- ", StringComparison.Ordinal))
            .ToArray();

        Assert.Equal(["- Alpha"], bulletLines);
    }

    private async Task GitHubPullRequestBodyValidatorAcceptsBodiesAtLimit(string pythonExecutable)
    {
        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-pr-body-limit-pass");
        try
        {
            var bodyPath = Path.Combine(tempDirectory.FullName, "pr_body.md");
            await File.WriteAllTextAsync(bodyPath, new string('a', 65_536));

            var result = await RunPythonScriptAsync(pythonExecutable, s_prBodyValidatorPath, [bodyPath]);

            Assert.Equal(0, result.ExitCode);
            Assert.Contains("GitHub pull request body length", result.Output, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    private async Task GitHubPullRequestBodyValidatorRejectsBodiesOverLimit(string pythonExecutable)
    {
        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-pr-body-limit-fail");
        try
        {
            var bodyPath = Path.Combine(tempDirectory.FullName, "pr_body.md");
            await File.WriteAllTextAsync(bodyPath, new string('a', 65_537));

            var result = await RunPythonScriptAsync(pythonExecutable, s_prBodyValidatorPath, [bodyPath]);

            Assert.NotEqual(0, result.ExitCode);
            Assert.Contains("exceeds GitHub's 65536-character pull request body limit", result.Output, StringComparison.Ordinal);
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    private async Task<string> GenerateDeterministicReleaseNotesAsync(string pythonExecutable, IEnumerable<string> commitLines, string? lineEnding = null)
    {
        Assert.True(File.Exists(s_releaseNotesGeneratorPath), $"Expected release notes generator at '{s_releaseNotesGeneratorPath}'.");

        var tempDirectory = Directory.CreateTempSubdirectory("extension-release-notes");
        try
        {
            var commitsPath = Path.Combine(tempDirectory.FullName, "commits.txt");
            var outputPath = Path.Combine(tempDirectory.FullName, "release_notes.md");

            await File.WriteAllTextAsync(
                commitsPath,
                string.Join(lineEnding ?? Environment.NewLine, commitLines) + (lineEnding ?? Environment.NewLine));

            var startInfo = new ProcessStartInfo(pythonExecutable)
            {
                WorkingDirectory = RepoRoot.Path,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                UseShellExecute = false,
            };
            startInfo.ArgumentList.Add(s_releaseNotesGeneratorPath);
            startInfo.ArgumentList.Add(commitsPath);
            startInfo.ArgumentList.Add(outputPath);

            using var process = Process.Start(startInfo)
                ?? throw new InvalidOperationException($"Failed to start {pythonExecutable}.");

            // Read both streams concurrently to avoid deadlock when a pipe buffer fills.
            var stdoutTask = process.StandardOutput.ReadToEndAsync();
            var stderrTask = process.StandardError.ReadToEndAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(1));

            try
            {
                await process.WaitForExitAsync(timeout.Token);
            }
            catch (OperationCanceledException)
            {
                process.Kill(entireProcessTree: true);
                throw;
            }

            var stdout = await stdoutTask;
            var stderr = await stderrTask;
            testOutput.WriteLine(stdout);
            testOutput.WriteLine(stderr);

            Assert.True(
                process.ExitCode == 0,
                $"{pythonExecutable} exited with code {process.ExitCode}.{Environment.NewLine}{stdout}{Environment.NewLine}{stderr}");
            return await File.ReadAllTextAsync(outputPath);
        }
        finally
        {
            Directory.Delete(tempDirectory.FullName, recursive: true);
        }
    }

    private async Task<CommandResult> RunPythonScriptAsync(string pythonExecutable, string scriptPath, IEnumerable<string> args)
    {
        Assert.True(File.Exists(scriptPath), $"Expected helper script at '{scriptPath}'.");

        using var process = new Process();
        process.StartInfo = new ProcessStartInfo(pythonExecutable)
        {
            WorkingDirectory = RepoRoot.Path,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            UseShellExecute = false,
        };

        process.StartInfo.ArgumentList.Add(scriptPath);
        foreach (var arg in args)
        {
            process.StartInfo.ArgumentList.Add(arg);
        }

        process.Start();

        // Read both streams concurrently to avoid deadlock when a helper prints diagnostics.
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(1));
        await process.WaitForExitAsync(timeout.Token);

        var output = await stdoutTask + await stderrTask;
        testOutput.WriteLine(output);

        return new CommandResult(process.ExitCode, output);
    }

    private static YamlMappingNode LoadAgenticWorkflowSource(string path)
    {
        var contents = File.ReadAllText(path);
        const string delimiter = "---";
        var end = contents.IndexOf($"\n{delimiter}", delimiter.Length, StringComparison.Ordinal);
        Assert.StartsWith(delimiter, contents, StringComparison.Ordinal);
        Assert.True(end > delimiter.Length);
        return LoadYaml(contents[delimiter.Length..end]);
    }

    private static YamlMappingNode LoadYamlWorkflow(string path)
        => LoadYaml(File.ReadAllText(path));

    private static YamlMappingNode LoadYaml(string contents)
    {
        var yaml = new YamlStream();
        using var reader = new StringReader(contents);
        yaml.Load(reader);
        return Assert.IsType<YamlMappingNode>(Assert.Single(yaml.Documents).RootNode);
    }

    private static List<YamlMappingNode> GetJobSteps(YamlMappingNode root, string jobName)
        => Sequence(
                Assert.IsType<YamlMappingNode>(
                    Assert.IsType<YamlMappingNode>(root.Children[new YamlScalarNode("jobs")])
                        .Children[new YamlScalarNode(jobName)]),
                "steps")
            .Children
            .Cast<YamlMappingNode>()
            .ToList();

    private static YamlMappingNode Step(IReadOnlyList<YamlMappingNode> steps, string name)
        => Assert.Single(steps, step => Scalar(step, "name") == name);

    private async Task<CommandResult> RunBashScriptAsync(
        string scriptPath,
        IEnumerable<string> args,
        IReadOnlyDictionary<string, string?> environment,
        string? workingDirectory = null)
    {
        Assert.True(File.Exists(scriptPath), $"Expected helper script at '{scriptPath}'.");

        using var process = new Process();
        process.StartInfo = new ProcessStartInfo("bash")
        {
            WorkingDirectory = workingDirectory ?? RepoRoot.Path,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            UseShellExecute = false,
        };
        process.StartInfo.ArgumentList.Add(scriptPath);
        foreach (var arg in args)
        {
            process.StartInfo.ArgumentList.Add(arg);
        }

        foreach (var (key, value) in environment)
        {
            process.StartInfo.Environment[key] = value ?? string.Empty;
        }

        process.Start();

        // Read both streams concurrently to avoid deadlock when bash emits diagnostics.
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(1));
        await process.WaitForExitAsync(timeout.Token);

        var output = await stdoutTask + await stderrTask;
        testOutput.WriteLine(output);

        return new CommandResult(process.ExitCode, output);
    }

    private async Task<FakeGhFixture> CreateFakeGhAsync(string rootDirectory)
    {
        var binDirectory = Path.Combine(rootDirectory, "bin");
        Directory.CreateDirectory(binDirectory);

        var callLogPath = Path.Combine(rootDirectory, "gh-call-log.txt");
        var fakeGhPath = Path.Combine(binDirectory, "gh");
        await File.WriteAllTextAsync(
            fakeGhPath,
            """
            #!/usr/bin/env bash
            set -euo pipefail

            printf '%s\n' "$*" >> "${GH_CALL_LOG}"

            if [[ "$1" == "pr" && "$2" == "view" ]]; then
              if [[ "${GH_HAS_LABEL:-false}" == "true" ]]; then
                printf '%s\n' "vscode-extension-release"
              fi
              exit 0
            fi

            if [[ "$1" == "pr" && "$2" == "edit" && "$4" == "--remove-label" ]]; then
              exit "${GH_REMOVE_LABEL_EXIT_CODE:-0}"
            fi

            if [[ "$1" == "pr" && "$2" == "edit" && "$4" == "--add-label" ]]; then
              exit "${GH_ADD_LABEL_EXIT_CODE:-0}"
            fi

            exit 0
            """);

        var chmodResult = await RunBashCommandAsync($"chmod +x \"{fakeGhPath}\"");
        Assert.Equal(0, chmodResult.ExitCode);

        return new FakeGhFixture(callLogPath, $"{binDirectory}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}");
    }

    private async Task<CommandResult> RunBashCommandAsync(string command)
    {
        using var process = new Process();
        process.StartInfo = new ProcessStartInfo("bash")
        {
            WorkingDirectory = RepoRoot.Path,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            UseShellExecute = false,
        };
        process.StartInfo.ArgumentList.Add("-c");
        process.StartInfo.ArgumentList.Add(command);

        process.Start();

        // Read both streams concurrently to avoid deadlock when bash emits diagnostics.
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(1));
        await process.WaitForExitAsync(timeout.Token);

        var output = await stdoutTask + await stderrTask;
        testOutput.WriteLine(output);

        return new CommandResult(process.ExitCode, output);
    }

    private static string? Scalar(YamlMappingNode node, string key)
        => node.Children.TryGetValue(new YamlScalarNode(key), out var value) && value is YamlScalarNode scalar
            ? scalar.Value
            : null;

    private static YamlSequenceNode Sequence(YamlMappingNode node, string key)
        => Assert.IsType<YamlSequenceNode>(node.Children[new YamlScalarNode(key)]);

    private sealed record FakeGhFixture(string CallLogPath, string PathEnvironment);
    private sealed record CommandResult(int ExitCode, string Output);
}

// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text.Json;
using Aspire.TestUtilities;
using Xunit;
using YamlDotNet.RepresentationModel;

namespace Infrastructure.Tests;

public sealed class AgenticWorkflowTests
{
    private static readonly string s_workflowsPath = Path.Combine(RepoRoot.Path, ".github", "workflows");
    private readonly ITestOutputHelper _testOutput;

    public AgenticWorkflowTests(ITestOutputHelper testOutput)
    {
        _testOutput = testOutput;
    }

    [Fact]
    public void GeneratedWorkflowsMatchBootstrapCompiler()
    {
        var bootstrap = LoadWorkflow("copilot-setup-steps.yml");
        var setup = Assert.Single(Mappings(bootstrap), node => Scalar(node, "uses").StartsWith("github/gh-aw-actions/setup-cli@", StringComparison.Ordinal));
        var version = Scalar(Mapping(setup, "with"), "version");
        var setupSha = Scalar(setup, "uses").Split('@')[1];
        Assert.Matches(@"^v\d+\.\d+\.\d+$", version);
        Assert.Matches("^[a-f0-9]{40}$", setupSha);

        var sources = Directory.EnumerateFiles(s_workflowsPath, "*.md")
            .Where(path => File.ReadLines(path).First() == "---")
            .ToArray();
        Assert.NotEmpty(sources);
        foreach (var source in sources)
        {
            var compiledName = Path.GetFileNameWithoutExtension(source) + ".lock.yml";
            var metadataLine = File.ReadLines(Path.Combine(s_workflowsPath, compiledName)).First();
            const string prefix = "# gh-aw-metadata: ";
            Assert.StartsWith(prefix, metadataLine);
            using var metadata = JsonDocument.Parse(metadataLine[prefix.Length..]);
            Assert.Equal(version, metadata.RootElement.GetProperty("compiler_version").GetString());

            var setupSteps = Mappings(LoadWorkflow(compiledName))
                .Where(node => Scalar(node, "uses").StartsWith("github/gh-aw-actions/setup@", StringComparison.Ordinal))
                .ToArray();
            Assert.NotEmpty(setupSteps);
            Assert.All(setupSteps, step => Assert.Equal("github/gh-aw-actions/setup@" + setupSha, Scalar(step, "uses")));
        }

        var maintenanceHeader = File.ReadLines(Path.Combine(s_workflowsPath, "agentics-maintenance-microsoft-aspire.dev.yml")).First();
        Assert.Contains($"side_repo_maintenance.go ({version})", maintenanceHeader, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(".md")]
    [InlineData(".lock.yml")]
    public void CiAnalysisTransfersOnlyItsPublishedFiles(string extension)
    {
        var root = LoadWorkflow("analyze-ci-failure" + extension);
        var upload = Step(root, "Upload CI analysis files");
        AssertArtifact(upload, "actions/upload-artifact", "ci-analysis-output");
        Assert.Equal(
            ["/tmp/gh-aw/agent/analysis-result.json", "/tmp/gh-aw/agent/causes/*.json"],
            Scalar(Mapping(upload, "with"), "path").Split('\n', StringSplitOptions.RemoveEmptyEntries));
        Assert.Equal("error", Scalar(Mapping(upload, "with"), "if-no-files-found"));

        var download = Step(root, "Download CI analysis files");
        AssertArtifact(download, "actions/download-artifact", "ci-analysis-output");
        Assert.Equal("download-analysis", Scalar(download, "id"));
        Assert.Equal("${{ runner.temp }}/ci-analysis-output", Scalar(Mapping(download, "with"), "path"));

        var validation = Step(root, "Validate analysis scope");
        Assert.Equal("${{ steps.download-analysis.outputs.download-path }}", Scalar(Mapping(validation, "env"), "ANALYSIS_DIR"));
        Assert.Contains("analyze-ci-failure-validation.sh", Scalar(validation, "run"), StringComparison.Ordinal);

        var publish = Step(root, "Publish analysis data and comment on PR");
        Assert.Equal("${{ steps.download-analysis.outputs.download-path }}", Scalar(Mapping(publish, "env"), "ANALYSIS_DIR"));
        var script = Scalar(publish, "run");
        Assert.Contains("ANALYSIS_FILE=\"$ANALYSIS_DIR/analysis-result.json\"", script, StringComparison.Ordinal);
        Assert.Contains("CAUSES_DIR=\"$ANALYSIS_DIR/causes\"", script, StringComparison.Ordinal);

        // The comment step runs in the same job but needs its own env wiring; without it the
        // analysis file is unreadable and the step fails before any comment is posted.
        var comment = Step(root, "Comment on PR");
        Assert.Equal("${{ steps.download-analysis.outputs.download-path }}", Scalar(Mapping(comment, "env"), "ANALYSIS_DIR"));
        Assert.Contains(
            "ANALYSIS_FILE=\"$ANALYSIS_DIR/analysis-result.json\"",
            Scalar(comment, "run"),
            StringComparison.Ordinal);

        // The rerun job is a separate job, so it must download the artifact itself.
        var rerunDownload = Step(root, "Download CI analysis files for rerun");
        AssertArtifact(rerunDownload, "actions/download-artifact", "ci-analysis-output");
        Assert.Equal("download-analysis", Scalar(rerunDownload, "id"));
        Assert.Equal("${{ runner.temp }}/ci-analysis-output", Scalar(Mapping(rerunDownload, "with"), "path"));

        var rerun = Step(root, "Rerun failed jobs");
        Assert.Equal("${{ steps.download-analysis.outputs.download-path }}", Scalar(Mapping(rerun, "env"), "ANALYSIS_DIR"));

        AssertUploadOrdering(root, extension, upload, download, publish);
    }

    [Theory]
    [InlineData(".md")]
    [InlineData(".lock.yml")]
    public void MilestoneChangelogTransfersBodyAndMemory(string extension)
    {
        var root = LoadWorkflow("milestone-changelog" + extension);
        var upload = Step(root, "Upload changelog files");
        AssertArtifact(upload, "actions/upload-artifact", "changelog-output");
        Assert.Equal(
            ["/tmp/gh-aw/agent/new-body.md", "/tmp/gh-aw/agent/memory/${{ env.MILESTONE }}/"],
            Scalar(Mapping(upload, "with"), "path").Split('\n', StringSplitOptions.RemoveEmptyEntries));
        Assert.Equal("error", Scalar(Mapping(upload, "with"), "if-no-files-found"));

        var download = Step(root, "Download changelog files");
        AssertArtifact(download, "actions/download-artifact", "changelog-output");
        Assert.Equal("download-changelog", Scalar(download, "id"));
        Assert.Equal("${{ runner.temp }}/changelog-output", Scalar(Mapping(download, "with"), "path"));

        var publish = Step(root, "Publish changelog and update memory branch");
        Assert.Equal("${{ steps.download-changelog.outputs.download-path }}", Scalar(Mapping(publish, "env"), "CHANGELOG_DIR"));
        var script = Scalar(publish, "run");
        Assert.Contains("BODY_FILE=\"$CHANGELOG_DIR/new-body.md\"", script, StringComparison.Ordinal);
        Assert.Contains("MEMORY_DIR=\"$CHANGELOG_DIR/memory/$MILESTONE\"", script, StringComparison.Ordinal);
        AssertUploadOrdering(root, extension, upload, download, publish);
    }

    [Theory]
    [InlineData(".md")]
    [InlineData(".lock.yml")]
    public void TestSelectionAuditCollectsBoundedEvidenceBeforeTheAgentRuns(string extension)
    {
        var root = LoadWorkflow("test-selection-audit" + extension);
        var compactMemory = Step(root, "Compact test-selection memory");
        var collector = Step(root, "Collect test-selection evidence");
        var compactScript = ReadTestSelectionAuditFile("compact_memory.cjs");
        var collectorScript = ReadTestSelectionAuditFile("collect_evidence.py");

        Assert.Equal("/tmp/gh-aw/repo-memory/default", Scalar(Mapping(compactMemory, "env"), "MEMORY_ROOT"));
        Assert.Equal("14", Scalar(Mapping(compactMemory, "env"), "RETENTION_DAYS"));
        Assert.Equal("${{ runner.temp }}/gh-aw/test-selection-audit/audit-date.txt", Scalar(Mapping(compactMemory, "env"), "AUDIT_DATE_PATH"));
        Assert.Equal("node .github/workflows/test-selection-audit/compact_memory.cjs", Scalar(compactMemory, "run"));
        Assert.Contains("row.seen >= cutoff", compactScript, StringComparison.Ordinal);
        Assert.Contains("row.verdict === \"watch\"", compactScript, StringComparison.Ordinal);
        Assert.Equal("${{ runner.temp }}/gh-aw/test-selection-audit/evidence.json", Scalar(Mapping(collector, "env"), "OUTPUT_PATH"));
        Assert.Equal("/tmp/gh-aw/repo-memory/default/processed-runs.jsonl", Scalar(Mapping(collector, "env"), "PROCESSED_RUNS_PATH"));
        Assert.Equal("${{ runner.temp }}/gh-aw/test-selection-audit/processed-runs-before.jsonl", Scalar(Mapping(collector, "env"), "PROCESSED_BASELINE_PATH"));
        Assert.Equal("/tmp/gh-aw/repo-memory/default/watchlist.jsonl", Scalar(Mapping(collector, "env"), "WATCHLIST_PATH"));
        Assert.Equal("${{ runner.temp }}/gh-aw/test-selection-audit/watchlist-before.jsonl", Scalar(Mapping(collector, "env"), "WATCHLIST_BASELINE_PATH"));
        Assert.Equal("${{ runner.temp }}/gh-aw/test-selection-audit/audit-date.txt", Scalar(Mapping(collector, "env"), "AUDIT_DATE_PATH"));
        Assert.Equal("python3 .github/workflows/test-selection-audit/collect_evidence.py", Scalar(collector, "run"));
        Assert.Contains("MAX_COMPRESSED_BYTES", collectorScript, StringComparison.Ordinal);
        Assert.Contains("MAX_EXPANDED_BYTES", collectorScript, StringComparison.Ordinal);
        Assert.Contains("http.client.IncompleteRead", collectorScript, StringComparison.Ordinal);
        Assert.Contains("for attempt in range(3)", collectorScript, StringComparison.Ordinal);
        Assert.Contains("ThreadPoolExecutor(max_workers=8)", collectorScript, StringComparison.Ordinal);
        Assert.Contains("entry.filename == ARTIFACT_MEMBER", collectorScript, StringComparison.Ordinal);
        Assert.Contains("stream.read(MAX_EXPANDED_BYTES + 1)", collectorScript, StringComparison.Ordinal);
        Assert.Contains("authorization_prefix + token", collectorScript, StringComparison.Ordinal);
        Assert.Contains("redirected.remove_header(\"Authorization\")", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"untrusted-fork-artifact\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"artifact-head-mismatch\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"selection-outside-lookback\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"pr-attribution-ambiguous\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"collector-error\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"recorded\"", collectorScript, StringComparison.Ordinal);
        Assert.Contains("len(numbers) > MAX_PRS", collectorScript, StringComparison.Ordinal);
        Assert.Contains("normalized[\"sourceHeadSha\"] == head_sha", collectorScript, StringComparison.Ordinal);
        Assert.Contains("except Exception as error:", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"sourceBaseSha\": source_base_sha", collectorScript, StringComparison.Ordinal);
        Assert.Contains("\"sourceHasDiff\": diff_match is not None", collectorScript, StringComparison.Ordinal);
        Assert.Contains("output_path.chmod(0o444)", collectorScript, StringComparison.Ordinal);

        if (extension == ".md")
        {
            Assert.Equal("30", Scalar(root, "timeout-minutes"));
            var tools = Mapping(root, "tools");
            var bash = Assert.IsType<YamlSequenceNode>(tools.Children[new YamlScalarNode("bash")])
                .Children.Select(node => node.ToString()).ToArray();
            Assert.DoesNotContain("curl", bash);
            Assert.DoesNotContain("unzip", bash);

            var githubToolsets = Assert.IsType<YamlSequenceNode>(
                    Mapping(tools, "github").Children[new YamlScalarNode("toolsets")])
                .Children.Select(node => node.ToString()).ToArray();
            Assert.DoesNotContain("actions", githubToolsets);

            var repoMemory = Mapping(tools, "repo-memory");
            Assert.Equal([".jsonl"], Assert.IsType<YamlSequenceNode>(
                repoMemory.Children[new YamlScalarNode("allowed-extensions")]).Children.Select(node => node.ToString()));
            var validation = Scalar(Mapping(repoMemory, "validation"), "script");
            Assert.Contains("Invalid test-selection audit memory", validation, StringComparison.Ordinal);
            Assert.Contains("counter ${actualCount} does not match ${expectedCount}", validation, StringComparison.Ordinal);
            Assert.Contains("entry.name === \".git\"", validation, StringComparison.Ordinal);
            Assert.Contains("does not match trusted selection evidence", validation, StringComparison.Ordinal);
            Assert.Contains("is not an unchanged baseline or trusted selection", validation, StringComparison.Ordinal);
            Assert.Contains("changes a watch row without trusted current evidence", validation, StringComparison.Ordinal);
            Assert.Contains("requires selection-time diff attribution", validation, StringComparison.Ordinal);
            Assert.Contains("provenance files are incomplete", validation, StringComparison.Ordinal);
            Assert.Contains("must be a UTC date <=", validation, StringComparison.Ordinal);
        }
        else
        {
            var mappings = Mappings(root).ToList();
            Assert.True(mappings.IndexOf(Step(root, "Clone repo-memory branch (default)")) < mappings.IndexOf(compactMemory));
            Assert.True(mappings.IndexOf(compactMemory) < mappings.IndexOf(collector));
            var compiledWorkflow = File.ReadAllText(Path.Combine(s_workflowsPath, "test-selection-audit.lock.yml"));
            var restoreBaseIndex = compiledWorkflow.IndexOf(
                "Restore agent config folders from base branch",
                StringComparison.Ordinal);
            var compactScriptIndex = compiledWorkflow.IndexOf(
                "node .github/workflows/test-selection-audit/compact_memory.cjs",
                StringComparison.Ordinal);
            Assert.True(
                restoreBaseIndex >= 0 && compactScriptIndex > restoreBaseIndex,
                "The trusted base .github tree must be restored before extracted scripts execute.");
            var validation = Step(root, "Validate repo-memory domain content (default)");
            Assert.NotEmpty(Scalar(Mapping(validation, "env"), "VALIDATION_SCRIPT_B64"));
            var agentRun = Scalar(Step(root, "Execute GitHub Copilot CLI"), "run");
            Assert.Contains("--mount \"${RUNNER_TEMP}/gh-aw:${RUNNER_TEMP}/gh-aw:ro\"", agentRun, StringComparison.Ordinal);
            Assert.DoesNotContain("--mount \"${RUNNER_TEMP}/gh-aw:${RUNNER_TEMP}/gh-aw:rw\"", agentRun, StringComparison.Ordinal);
            Assert.Contains("--mount \"${RUNNER_TEMP}/gh-aw:/host${RUNNER_TEMP}/gh-aw:ro\"", agentRun, StringComparison.Ordinal);
            Assert.DoesNotContain("--mount \"${RUNNER_TEMP}/gh-aw:/host${RUNNER_TEMP}/gh-aw:rw\"", agentRun, StringComparison.Ordinal);
        }
    }

    [Fact]
    [RequiresTools(["node"])]
    public async Task TestSelectionAuditRejectsMemoryCountersNotBackedByProcessedHeads()
    {
        var root = LoadWorkflow("test-selection-audit.md");
        var validationScript = Scalar(Mapping(Mapping(Mapping(root, "tools"), "repo-memory"), "validation"), "script");
        using var workspace = TemporaryWorkspace.Create(_testOutput);
        var memoryPath = Path.Combine(workspace.Path, "memory");
        Directory.CreateDirectory(memoryPath);
        Directory.CreateDirectory(Path.Combine(memoryPath, ".git"));

        var provenancePath = Path.Combine(workspace.Path, "gh-aw", "test-selection-audit");
        Directory.CreateDirectory(provenancePath);
        var evidencePath = Path.Combine(provenancePath, "evidence.json");
        var baselinePath = Path.Combine(provenancePath, "processed-runs-before.jsonl");
        var watchBaselinePath = Path.Combine(provenancePath, "watchlist-before.jsonl");
        var auditDatePath = Path.Combine(provenancePath, "audit-date.txt");
        var validationPath = Path.Combine(workspace.Path, "validation.js");
        await File.WriteAllTextAsync(validationPath, validationScript);
        var harnessPath = Path.Combine(workspace.Path, "validate-memory.js");
        await File.WriteAllTextAsync(
            harnessPath,
            """
            const fs = require("fs");
            const path = require("path");
            const vm = require("vm");
            const memoryRoot = process.argv[2];
            const script = fs.readFileSync(process.argv[3], "utf8");
            vm.runInNewContext(script, { fs, path, memoryRoot, process });
            """);

        var auditDate = DateTime.UtcNow.ToString("yyyy-MM-dd");
        var processed = new Dictionary<string, object?>
        {
            ["pr"] = 42,
            ["sha"] = new string('a', 40),
            ["run"] = 100,
            ["attempt"] = 1,
            ["all"] = true,
            ["over_paths"] = new[] { ".gitattributes" },
            ["miss_edges"] = Array.Empty<object>(),
            ["seen"] = auditDate
        };
        var watch = new Dictionary<string, object?>
        {
            ["path"] = ".gitattributes",
            ["rule"] = ".gitattributes",
            ["rule_ref"] = "eng/github-ci/test-trigger-map.yml@abcdef1",
            ["path_ref"] = ".gitattributes@abcdef1",
            ["kind"] = "over-selection",
            ["verdict"] = "watch",
            ["all_runs"] = 1,
            ["first_seen"] = auditDate,
            ["last_seen"] = auditDate,
            ["example_prs"] = new[] { 42 },
            ["ref"] = null
        };
        var processedPath = Path.Combine(memoryPath, "processed-runs.jsonl");
        var watchPath = Path.Combine(memoryPath, "watchlist.jsonl");
        object evidence = new
        {
            auditDate,
            generatedAt = DateTime.UtcNow.ToString("O"),
            records = new[]
            {
                new
                {
                    pr = 42,
                    headSha = new string('a', 40),
                    selection = new
                    {
                        status = "resolved",
                        creditable = true,
                        run = 100,
                        attempt = 1,
                        result = new
                        {
                            selectsAll = true,
                            sourceHasDiff = true,
                            changedFiles = new[] { ".gitattributes" },
                            excludedFiles = Array.Empty<string>(),
                            unattributedFiles = Array.Empty<string>(),
                            testProjects = Array.Empty<string>(),
                            jobs = Array.Empty<string>()
                        }
                    }
                }
            }
        };
        await File.WriteAllTextAsync(evidencePath, JsonSerializer.Serialize(evidence));
        await File.WriteAllTextAsync(baselinePath, "");
        await File.WriteAllTextAsync(watchBaselinePath, "");
        await File.WriteAllTextAsync(auditDatePath, auditDate + Environment.NewLine);
        await File.WriteAllTextAsync(processedPath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        await File.WriteAllTextAsync(watchPath, JsonSerializer.Serialize(watch) + Environment.NewLine);

        using var command = new NodeCommand(_testOutput, "test-selection-memory-validation");
        command
            .WithTimeout(TimeSpan.FromSeconds(30))
            .WithEnvironmentVariable("RUNNER_TEMP", workspace.Path);
        var valid = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.True(valid.ExitCode == 0, valid.Output);

        File.Delete(evidencePath);
        var partialProvenance = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, partialProvenance.ExitCode);
        Assert.Contains("provenance files are incomplete", partialProvenance.Output, StringComparison.Ordinal);
        await File.WriteAllTextAsync(evidencePath, JsonSerializer.Serialize(evidence));

        File.Delete(evidencePath);
        File.Delete(baselinePath);
        File.Delete(watchBaselinePath);
        var noProvenance = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.True(noProvenance.ExitCode == 0, noProvenance.Output);
        await File.WriteAllTextAsync(evidencePath, JsonSerializer.Serialize(evidence));
        await File.WriteAllTextAsync(baselinePath, "");
        await File.WriteAllTextAsync(watchBaselinePath, "");

        processed["seen"] = "2026-02-30";
        await File.WriteAllTextAsync(processedPath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        var invalidCalendarDate = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, invalidCalendarDate.ExitCode);
        Assert.Contains("must be a UTC date", invalidCalendarDate.Output, StringComparison.Ordinal);

        processed["seen"] = DateTime.UtcNow.AddDays(2).ToString("yyyy-MM-dd");
        await File.WriteAllTextAsync(processedPath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        var futureDate = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, futureDate.ExitCode);
        Assert.Contains("must be a UTC date", futureDate.Output, StringComparison.Ordinal);

        processed["seen"] = DateTime.UtcNow.AddDays(-1).ToString("yyyy-MM-dd");
        await File.WriteAllTextAsync(processedPath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        var backdatedTrustedRow = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, backdatedTrustedRow.ExitCode);
        Assert.Contains("seen must match the protected audit date", backdatedTrustedRow.Output, StringComparison.Ordinal);

        processed["seen"] = auditDate;
        await File.WriteAllTextAsync(processedPath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        watch["all_runs"] = 2;
        await File.WriteAllTextAsync(watchPath, JsonSerializer.Serialize(watch) + Environment.NewLine);
        var invalid = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, invalid.ExitCode);
        Assert.Contains("counter 2 does not match 1 processed heads", invalid.Output, StringComparison.Ordinal);

        watch["all_runs"] = 1;
        await File.WriteAllTextAsync(watchPath, JsonSerializer.Serialize(watch) + Environment.NewLine);
        evidence = new
        {
            auditDate,
            generatedAt = DateTime.UtcNow.ToString("O"),
            records = new[]
            {
                new
                {
                    pr = 42,
                    headSha = new string('a', 40),
                    selection = new
                    {
                        status = "untrusted-fork-artifact",
                        creditable = false,
                        run = 100,
                        attempt = 1,
                        result = new { selectsAll = true }
                    }
                }
            }
        };
        await File.WriteAllTextAsync(evidencePath, JsonSerializer.Serialize(evidence));
        var untrusted = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, untrusted.ExitCode);
        Assert.Contains("is not an unchanged baseline or trusted selection", untrusted.Output, StringComparison.Ordinal);

        await File.WriteAllTextAsync(baselinePath, JsonSerializer.Serialize(processed) + Environment.NewLine);
        watch["verdict"] = "correct-by-design";
        await File.WriteAllTextAsync(watchBaselinePath, JsonSerializer.Serialize(new Dictionary<string, object?>(watch)
        {
            ["verdict"] = "watch"
        }) + Environment.NewLine);
        await File.WriteAllTextAsync(watchPath, JsonSerializer.Serialize(watch) + Environment.NewLine);
        var poisonedWatch = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, poisonedWatch.ExitCode);
        Assert.Contains("changes a watch row without trusted current evidence", poisonedWatch.Output, StringComparison.Ordinal);

        var pendingWatch = new Dictionary<string, object?>(watch)
        {
            ["verdict"] = "pending-filed",
            ["note"] = "[test-selection-audit] Fix gitattributes selection",
            ["ref"] = null
        };
        var filedWatch = new Dictionary<string, object?>(pendingWatch)
        {
            ["verdict"] = "filed",
            ["ref"] = 123
        };
        filedWatch.Remove("note");
        await File.WriteAllTextAsync(watchBaselinePath, JsonSerializer.Serialize(pendingWatch) + Environment.NewLine);
        await File.WriteAllTextAsync(watchPath, JsonSerializer.Serialize(filedWatch) + Environment.NewLine);
        var lifecycleTransition = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.True(lifecycleTransition.ExitCode == 0, lifecycleTransition.Output);

        await File.WriteAllTextAsync(processedPath, "");
        var missingBaseline = await command.ExecuteScriptAsync(harnessPath, memoryPath, validationPath);
        Assert.NotEqual(0, missingBaseline.ExitCode);
        Assert.Contains("missing baseline processed row", missingBaseline.Output, StringComparison.Ordinal);
    }

    [Fact]
    [RequiresTools(["node"])]
    public async Task TestSelectionAuditCompactsRawMemoryToTheLookbackWindow()
    {
        using var workspace = TemporaryWorkspace.Create(_testOutput);
        var memoryPath = Path.Combine(workspace.Path, "memory");
        Directory.CreateDirectory(memoryPath);
        var scriptPath = Path.Combine(s_workflowsPath, "test-selection-audit", "compact_memory.cjs");
        var auditDatePath = Path.Combine(workspace.Path, "gh-aw", "test-selection-audit", "audit-date.txt");

        var recent = DateTime.UtcNow.ToString("yyyy-MM-dd");
        var expired = DateTime.UtcNow.AddDays(-20).ToString("yyyy-MM-dd");
        var processed = new object[]
        {
            new
            {
                pr = 1,
                sha = new string('a', 40),
                run = 1,
                attempt = 1,
                all = true,
                over_paths = new[] { "expired.txt" },
                miss_edges = Array.Empty<object>(),
                seen = expired
            },
            new
            {
                pr = 2,
                sha = new string('b', 40),
                run = 2,
                attempt = 1,
                all = true,
                over_paths = new[] { "retained.txt" },
                miss_edges = Array.Empty<object>(),
                seen = recent
            },
            new
            {
                pr = 2,
                sha = new string('c', 40),
                run = 3,
                attempt = 1,
                all = true,
                over_paths = new[] { "retained.txt" },
                miss_edges = Array.Empty<object>(),
                seen = recent
            }
        };
        var watch = new object[]
        {
            new
            {
                path = "expired.txt",
                rule = "expired.txt",
                rule_ref = "eng/github-ci/test-trigger-map.yml@abcdef1",
                path_ref = "expired.txt@abcdef1",
                kind = "over-selection",
                verdict = "watch",
                all_runs = 1,
                first_seen = expired,
                last_seen = expired,
                example_prs = new[] { 1 },
                @ref = (int?)null
            },
            new
            {
                path = "retained.txt",
                rule = "retained.txt",
                rule_ref = "eng/github-ci/test-trigger-map.yml@abcdef1",
                path_ref = "retained.txt@abcdef1",
                kind = "over-selection",
                verdict = "watch",
                all_runs = 99,
                first_seen = expired,
                last_seen = expired,
                example_prs = new[] { 1 },
                @ref = (int?)null
            },
            new
            {
                path = "settled.txt",
                rule = "settled.txt",
                rule_ref = "eng/github-ci/test-trigger-map.yml@abcdef1",
                path_ref = "settled.txt@abcdef1",
                kind = "over-selection",
                verdict = "correct-by-design",
                all_runs = 4,
                first_seen = expired,
                last_seen = expired,
                example_prs = new[] { 1 },
                @ref = (int?)null
            }
        };
        await File.WriteAllTextAsync(
            Path.Combine(memoryPath, "processed-runs.jsonl"),
            string.Join(Environment.NewLine, processed.Select(value => JsonSerializer.Serialize(value))) + Environment.NewLine);
        await File.WriteAllTextAsync(
            Path.Combine(memoryPath, "watchlist.jsonl"),
            string.Join(Environment.NewLine, watch.Select(value => JsonSerializer.Serialize(value))) + Environment.NewLine);

        using var command = new NodeCommand(_testOutput, "test-selection-memory-compaction");
        command
            .WithTimeout(TimeSpan.FromSeconds(30))
            .WithEnvironmentVariable("MEMORY_ROOT", memoryPath)
            .WithEnvironmentVariable("RETENTION_DAYS", "14")
            .WithEnvironmentVariable("AUDIT_DATE_PATH", auditDatePath);
        var result = await command.ExecuteScriptAsync(scriptPath);
        Assert.True(result.ExitCode == 0, result.Output);

        var retainedProcessed = File.ReadLines(Path.Combine(memoryPath, "processed-runs.jsonl"))
            .Select(line => JsonDocument.Parse(line).RootElement.Clone())
            .ToArray();
        Assert.Equal(2, retainedProcessed.Length);
        Assert.All(retainedProcessed, row => Assert.Equal(2, row.GetProperty("pr").GetInt32()));

        var retainedWatch = File.ReadLines(Path.Combine(memoryPath, "watchlist.jsonl"))
            .Select(line => JsonDocument.Parse(line).RootElement.Clone())
            .ToArray();
        Assert.DoesNotContain(retainedWatch, row => row.GetProperty("path").GetString() == "expired.txt");
        var active = Assert.Single(retainedWatch, row => row.GetProperty("path").GetString() == "retained.txt");
        Assert.Equal(2, active.GetProperty("all_runs").GetInt32());
        Assert.Equal([2], active.GetProperty("example_prs").EnumerateArray().Select(value => value.GetInt32()));
        Assert.Equal(recent, active.GetProperty("first_seen").GetString());
        Assert.Equal(recent, active.GetProperty("last_seen").GetString());

        var settled = Assert.Single(retainedWatch, row => row.GetProperty("path").GetString() == "settled.txt");
        Assert.Equal(0, settled.GetProperty("all_runs").GetInt32());
        Assert.Empty(settled.GetProperty("example_prs").EnumerateArray());
        Assert.Equal("correct-by-design", settled.GetProperty("verdict").GetString());

        var invalidProcessed = new
        {
            pr = 3,
            sha = new string('d', 40),
            run = 4,
            attempt = 1,
            all = true,
            over_paths = Array.Empty<string>(),
            miss_edges = Array.Empty<object>(),
            seen = "2026-02-30"
        };
        await File.WriteAllTextAsync(
            Path.Combine(memoryPath, "processed-runs.jsonl"),
            JsonSerializer.Serialize(invalidProcessed) + Environment.NewLine);
        File.Delete(auditDatePath);
        var invalidCalendarDate = await command.ExecuteScriptAsync(scriptPath);
        Assert.NotEqual(0, invalidCalendarDate.ExitCode);
        Assert.Contains("must be a real UTC date", invalidCalendarDate.Output, StringComparison.Ordinal);

        var futureProcessed = new
        {
            pr = 4,
            sha = new string('e', 40),
            run = 5,
            attempt = 1,
            all = true,
            over_paths = Array.Empty<string>(),
            miss_edges = Array.Empty<object>(),
            seen = DateTime.UtcNow.AddDays(2).ToString("yyyy-MM-dd")
        };
        await File.WriteAllTextAsync(
            Path.Combine(memoryPath, "processed-runs.jsonl"),
            JsonSerializer.Serialize(futureProcessed) + Environment.NewLine);
        File.Delete(auditDatePath);
        var futureDate = await command.ExecuteScriptAsync(scriptPath);
        Assert.NotEqual(0, futureDate.ExitCode);
        Assert.Contains("must be a real UTC date", futureDate.Output, StringComparison.Ordinal);
    }

    [Fact]
    public void TestSelectionAuditDisclosesIssueContractOnlyWhenFiling()
    {
        var workflow = File.ReadAllText(Path.Combine(s_workflowsPath, "test-selection-audit.md"));
        var instructions = ReadTestSelectionAuditFile("issue_instructions.md");

        Assert.Contains(
            ".github/workflows/test-selection-audit/issue_instructions.md",
            workflow,
            StringComparison.Ordinal);
        Assert.Contains("The body must contain:", instructions, StringComparison.Ordinal);
        Assert.Contains("**Required validation**", instructions, StringComparison.Ordinal);
        Assert.Contains(
            "<sub>Automated by the weekly CI test-selection audit workflow.</sub>",
            instructions,
            StringComparison.Ordinal);
    }

    [Fact]
    [RequiresTools(["python"])]
    [SkipOnPlatform(TestPlatforms.Linux | TestPlatforms.OSX | TestPlatforms.FreeBSD, "Uses the Windows Python executable.")]
    public Task TestSelectionAuditPythonTestsPassOnWindows() => TestSelectionAuditPythonTestsPass("python");

    [Fact]
    [RequiresTools(["python3"])]
    [SkipOnPlatform(TestPlatforms.Windows, "Uses the Unix Python executable.")]
    public Task TestSelectionAuditPythonTestsPassOnUnix() => TestSelectionAuditPythonTestsPass("python3");

    private async Task TestSelectionAuditPythonTestsPass(string python)
    {
        var startInfo = new ProcessStartInfo(python)
        {
            WorkingDirectory = RepoRoot.Path,
            RedirectStandardError = true,
            RedirectStandardOutput = true,
            UseShellExecute = false,
        };
        startInfo.ArgumentList.Add("-m");
        startInfo.ArgumentList.Add("unittest");
        startInfo.ArgumentList.Add("discover");
        startInfo.ArgumentList.Add("-s");
        startInfo.ArgumentList.Add(".github/workflows/test-selection-audit");
        startInfo.ArgumentList.Add("-p");
        startInfo.ArgumentList.Add("test_*.py");
        startInfo.ArgumentList.Add("-v");

        using var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException($"Failed to start {python}.");

        // Read both streams concurrently to avoid deadlock when a pipe buffer fills.
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(2));
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
        _testOutput.WriteLine(stdout);
        _testOutput.WriteLine(stderr);

        Assert.True(
            process.ExitCode == 0,
            $"{python} exited with code {process.ExitCode}.{Environment.NewLine}{stdout}{Environment.NewLine}{stderr}");
    }

    [Fact]
    public void WorkflowAppTokensUseClientId()
    {
        var workflows = Directory.EnumerateFiles(s_workflowsPath)
            .Where(path => path.EndsWith(".yml", StringComparison.Ordinal) ||
                path.EndsWith(".md", StringComparison.Ordinal) && File.ReadLines(path).First() == "---");
        var inputs = workflows.SelectMany(path => Mappings(LoadWorkflow(Path.GetFileName(path))))
            .SelectMany(node => node.Children
                .Where(pair => pair.Key.ToString() == "github-app" ||
                    pair.Key.ToString() == "with" && Scalar(node, "uses").StartsWith("actions/create-github-app-token@", StringComparison.Ordinal))
                .Select(pair => Assert.IsType<YamlMappingNode>(pair.Value)))
            .ToArray();
        Assert.NotEmpty(inputs);
        Assert.All(inputs, input =>
        {
            Assert.Equal(
                ["client-id"],
                input.Children.Keys.Select(key => key.ToString()).Where(key => key is "client-id" or "app-id"));
            Assert.NotEmpty(Scalar(input, "client-id"));
            Assert.NotEmpty(Scalar(input, "private-key"));
        });
    }

    private static void AssertArtifact(YamlMappingNode step, string action, string artifactName)
    {
        Assert.StartsWith(action + "@", Scalar(step, "uses"));
        Assert.Equal(artifactName, Scalar(Mapping(step, "with"), "name"));
    }

    private static void AssertUploadOrdering(YamlMappingNode root, string extension, YamlMappingNode upload, YamlMappingNode download, YamlMappingNode publish)
    {
        var mappings = Mappings(root).ToList();
        Assert.True(mappings.IndexOf(download) < mappings.IndexOf(publish));
        if (extension == ".lock.yml")
        {
            var redaction = Step(root, "Redact secrets in logs");
            Assert.True(mappings.IndexOf(redaction) < mappings.IndexOf(upload));
            Assert.Equal("${{ runner.temp }}/gh-aw/safe-jobs/agent_output.json", Scalar(Mapping(publish, "env"), "GH_AW_AGENT_OUTPUT"));
        }
        else
        {
            Assert.Contains(upload, Assert.IsType<YamlSequenceNode>(root.Children[new YamlScalarNode("post-steps")]).Children);
        }
    }

    private static YamlMappingNode LoadWorkflow(string fileName)
    {
        var content = File.ReadAllText(Path.Combine(s_workflowsPath, fileName)).ReplaceLineEndings("\n");
        if (fileName.EndsWith(".md", StringComparison.Ordinal))
        {
            content = content[4..content.IndexOf("\n---", 4, StringComparison.Ordinal)];
        }

        var yaml = new YamlStream();
        yaml.Load(new StringReader(content));
        return Assert.IsType<YamlMappingNode>(Assert.Single(yaml.Documents).RootNode);
    }

    private static YamlMappingNode Step(YamlMappingNode root, string name) =>
        Assert.Single(Mappings(root), node => Scalar(node, "name") == name &&
            (node.Children.ContainsKey(new YamlScalarNode("uses")) || node.Children.ContainsKey(new YamlScalarNode("run"))));

    private static YamlMappingNode Mapping(YamlMappingNode node, string key) =>
        Assert.IsType<YamlMappingNode>(node.Children[new YamlScalarNode(key)]);

    private static string Scalar(YamlMappingNode node, string key) =>
        node.Children.TryGetValue(new YamlScalarNode(key), out var value) ? value.ToString() : "";

    private static string ReadTestSelectionAuditFile(string fileName) =>
        File.ReadAllText(Path.Combine(s_workflowsPath, "test-selection-audit", fileName));

    private static IEnumerable<YamlMappingNode> Mappings(YamlNode node)
    {
        if (node is YamlMappingNode mapping)
        {
            yield return mapping;
        }

        var children = node switch
        {
            YamlMappingNode parent => parent.Children.Values,
            YamlSequenceNode sequence => sequence.Children,
            _ => Enumerable.Empty<YamlNode>()
        };
        foreach (var child in children.SelectMany(Mappings))
        {
            yield return child;
        }
    }
}

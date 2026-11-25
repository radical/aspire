// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using System.Net;
using static AnalyzeCiFailure.CiFailureJson;

namespace AnalyzeCiFailure;

/// <summary>Explicit operator snapshot authorizing replacement of one existing cause issue body.</summary>
internal sealed record IssueRegeneration
{
    public required long Number { get; init; }
    [JsonPropertyName("updated_at")]
    public required string UpdatedAt { get; init; }
    public required string Body { get; init; }
}

internal sealed partial class AnalysisPublisher
{
    private async Task RegenerateIssueAsync(ValidatedAnalysis analysis, IssueRegeneration snapshot)
    {
        if (snapshot.Number <= 0 || string.IsNullOrEmpty(snapshot.UpdatedAt) || snapshot.Body is null)
        {
            throw new ValidationException("Regeneration requires a positive issue number, updated_at and body.");
        }
        var endpoint = $"repos/{_repository}/issues/{snapshot.Number}";
        var issue = await ReadUnchangedIssueAsync();
        var matches = analysis.Causes.Where(cause => MatchesIssue(issue, cause)).ToArray();
        if (matches.Length != 1 || matches[0].Type != CauseType.FlakyTest)
        {
            throw new ValidationException("Regeneration requires exactly one validated flaky-test cause matching the existing issue.");
        }
        // Maintenance registers this cause, not a diagnosis of every failed job in the run.
        // Do not overwrite run summaries, publish adjacent issues, or comment on PRs.
        var scoped = analysis with { Causes = [matches[0]] };
        var history = new CauseHistoryStore(_command, _repository, _historyDirectory, _diagnostics);
        await history.OpenForPublicationAsync();
        var runId = JsonNode.Parse(analysis.Evidence.Context.RunId!.Value.GetRawText())!;
        var prepared = PrepareCauses(scoped, runId, history);
        var item = prepared.Single();
        var occurrence = ((JsonArray)item.History["occurrences"]!).Single(value =>
            ReadNormalizedNumber(value!["run_id"]) == ReadNormalizedNumber(runId))!;
        occurrence["run_scope"] = EnumText(analysis.Run.Scope);
        if (Text(item.History["issue_url"]) is { } linked &&
            linked != $"https://github.com/{_repository}/issues/{snapshot.Number}")
        {
            throw new ValidationException("Stored cause links to a different issue; regeneration is not authorized.");
        }
        var canonical = BuildIssueUpdate(scoped, analysis.CapturedPublication, item.Cause, item.History, issue: null, runId, item.Replayed);
        var body = CauseIssueRenderer.RegenerateIssue(canonical.Body!, item.History, IssueFooter)
            ?? throw new ValidationException("Canonical regenerated body exceeds the publication budget.");
        _ = await ReadUnchangedIssueAsync();
        history.CreateRecordDirectories();
        Write(item.Path, item.History);
        await history.CommitAndPushAsync(GetValueText(runId), [item.Path]);

        // GitHub's issue PATCH has no atomic compare-and-swap contract. Recheck after
        // the history push and immediately before PATCH; scoped writer ownership is
        // still required to exclude a writer racing this final read.
        _ = await ReadUnchangedIssueAsync();
        await ExecuteEffectAsync("issue-regenerate", item.Cause.Id,
            $"https://github.com/{_repository}/issues/{snapshot.Number}", HttpMethod.Patch, endpoint,
            new JsonObject { ["body"] = body }, HttpStatusCode.OK);
        item.History["issue_url"] = $"https://github.com/{_repository}/issues/{snapshot.Number}";
        Write(item.Path, item.History);
        await history.CommitAndPushAsync(GetValueText(runId), [item.Path]);

        async Task<JsonObject> ReadUnchangedIssueAsync()
        {
            var current = await _github.GetAsync(endpoint) as JsonObject;
            if (current is null || !GitHubJson.TryReadId(current["number"], out var number) || number != snapshot.Number ||
                current["pull_request"] is not null || Text(current["state"]) != "open" || GetValueText(current["locked"]) == "true")
            {
                throw new ValidationException("Regeneration destination must be an open, unlocked issue.");
            }
            if (Text(current["body"]) != snapshot.Body || Text(current["updated_at"]) != snapshot.UpdatedAt)
            {
                throw new ValidationException("Issue changed since the regeneration snapshot; aborting without overwriting it.");
            }
            return current;
        }
    }
}

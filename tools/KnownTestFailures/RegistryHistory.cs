// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace KnownTestFailures;

public static class RegistryHistory
{
    public static IReadOnlyDictionary<string, Entry> Replay(Registry registry)
    {
        Format.Require(registry.SchemaVersion == 1, "Unsupported registry schemaVersion.");
        Format.Require(registry.Environment == "experimental", "Only experimental registries are supported.");
        var entries = new Dictionary<string, Entry>(StringComparer.Ordinal);
        var lastAt = DateTimeOffset.MinValue;
        foreach (var (item, index) in registry.History.Select((item, index) => (item, index)))
        {
            Format.Require(item.Sequence == index + 1, "History sequence must be contiguous from 1.");
            Format.Require(item.ContentRevision > 0, "contentRevision must be positive.");
            Format.Text(item.Id, "id");
            Format.Text(item.Actor, "actor");
            Format.Text(item.Reason, "reason");
            Format.Utc(item.At, "at");
            Format.Require(item.At >= lastAt, "History timestamps must be nondecreasing.");
            lastAt = item.At;
            entries.TryGetValue(item.Id, out var previous);
            if (item.Action is "add" or "update")
            {
                var content = item.Content ?? throw new InvalidDataException("add/update requires content.");
                Validate(content);
                Format.Require(content.CreatedAt <= item.At && item.At < content.ExpiresAt, "Content must be live at mutation time.");
                if (item.Action == "add")
                {
                    Format.Require(previous is null && item.ContentRevision == 1, "add requires an unused ID and contentRevision 1.");
                }
                else
                {
                    Format.Require(previous is { State: "active" } && item.At < previous.Content.ExpiresAt,
                        "Only live, nonterminal entries can be updated; use a new ID.");
                    Format.Require(item.ContentRevision == previous.ContentRevision + 1 &&
                        content.CreatedAt == previous.Content.CreatedAt, "update must increment contentRevision and preserve createdAt.");
                    Format.Require(content != previous.Content, "update must change content.");
                }
                entries[item.Id] = new(item.Id, item.ContentRevision, content, "active", null);
            }
            else
            {
                Format.Require(item.Content is null, "Only add/update may carry content.");
                Format.Require(previous is { State: "active" }, "Operation requires an active, nonterminal entry.");
                Format.Require(item.ContentRevision == previous.ContentRevision, "Operation must identify the current contentRevision.");
                switch (item.Action)
                {
                    case "approve":
                        Format.Require(item.At >= previous.Content.CreatedAt && item.At < previous.Content.ExpiresAt,
                            "Approval must be within the content lifetime.");
                        entries[item.Id] = previous with { Approval = new(item.Actor, item.At, item.ContentRevision) };
                        break;
                    case "revoke":
                    case "remove":
                    case "expire":
                        entries[item.Id] = previous with { State = item.Action, Approval = null };
                        break;
                    default:
                        throw new InvalidDataException($"Unknown action '{item.Action}'.");
                }
            }
            // Overlap is rejected even before approval, so admission cannot accidentally create
            // two active allowances for the same exact failure during intersecting lifetimes.
            var active = entries.Values.Where(e => e.State == "active").ToArray();
            for (var i = 0; i < active.Length; i++)
            {
                for (var j = i + 1; j < active.Length; j++)
                {
                    var a = active[i].Content;
                    var b = active[j].Content;
                    Format.Require(!SameFailure(a, b) || a.ExpiresAt <= b.CreatedAt || b.ExpiresAt <= a.CreatedAt,
                        $"Ambiguous overlapping entries '{active[i].Id}' and '{active[j].Id}'.");
                }
            }
        }
        return entries;
    }

    public static Registry Append(Registry registry, RegistryEvent item)
    {
        var updated = registry with { History = [.. registry.History, item] };
        Replay(updated);
        return updated;
    }

    private static bool SameFailure(Allowance a, Allowance b) =>
        a.Project == b.Project && a.Method == b.Method && a.DisplayName == b.DisplayName &&
        a.CaseId == b.CaseId && a.Lane == b.Lane && a.Platform == b.Platform &&
        a.Ref == b.Ref && a.Message == b.Message;

    private static void Validate(Allowance content)
    {
        Format.Path(content.Project, "project");
        Format.Require(content.Project.EndsWith(".csproj", StringComparison.Ordinal), "project must identify a managed .csproj.");
        Format.Text(content.Method, "method");
        Format.Require(content.Method.Contains('.'), "method must be fully qualified class.method.");
        Format.Text(content.DisplayName, "displayName");
        Format.Text(content.CaseId, "caseId");
        Format.Text(content.Lane, "lane");
        Format.Text(content.Platform, "platform");
        Format.FullRef(content.Ref);
        Format.Text(content.Message, "message");
        Format.Text(content.Owner, "owner");
        Format.Require(Uri.TryCreate(content.Issue, UriKind.Absolute, out var issue) &&
            issue.Scheme == Uri.UriSchemeHttps && issue.Host.Length > 0 && issue.UserInfo.Length == 0,
            "issue must be an absolute HTTPS link without credentials.");
        Format.Utc(content.CreatedAt, "createdAt");
        Format.Utc(content.ExpiresAt, "expiresAt");
        Format.Require(content.CreatedAt < content.ExpiresAt, "expiresAt must follow createdAt.");
    }
}

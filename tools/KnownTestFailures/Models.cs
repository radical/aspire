// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics.CodeAnalysis;
using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace KnownTestFailures;

public sealed record Registry(int SchemaVersion, string Environment, RegistryEvent[] History);

public sealed record RegistryEvent(
    int Sequence, string Id, string Action, string Actor, string Reason, DateTimeOffset At,
    int ContentRevision, Allowance? Content = null);

public sealed record Allowance(
    string Project, string Method, string DisplayName, string CaseId,
    string Lane, string Platform, string Ref, string Message,
    string Issue, string Owner, DateTimeOffset CreatedAt, DateTimeOffset ExpiresAt);

public sealed record Approval(string Approver, DateTimeOffset ApprovedAt, int ContentRevision);

public sealed record Entry(string Id, int ContentRevision, Allowance Content, string State, Approval? Approval);

public sealed record Evidence(
    int SchemaVersion, string Lane, string Platform, string Ref,
    string[] SelectedInvocations, Invocation[] Invocations);

public sealed record Invocation(
    string Id, string Project, string TrxPath, bool DirectMtpProcess,
    int ProcessExitCode, bool SetupSucceeded, bool CompletedNormally,
    string[] Diagnostics, ExpectedCase[] ExpectedCases)
{
    public bool AllowEmpty { get; init; }
}

public sealed record ExpectedCase(string CaseId, string TestId, string Method, string DisplayName);

public sealed record RegistrySource(string Kind, string Path, string? Commit = null, string? Blob = null);

public sealed record RegistryInput(Registry Registry, RegistrySource Source, string RawJson);

public sealed record Artifact(string InvocationId, string Path, string RawTrx);

public sealed record Match(string InvocationId, string CaseId, string AllowanceId, int ContentRevision);

public sealed record Decision(
    string Mode, string Status, int OriginalExitCode, int EffectiveExitCode,
    DateTimeOffset EvaluatedAt, Artifact[] Artifacts, Match[] Matches, string[] Diagnostics,
    RegistrySource? RegistrySource = null, string? RegistryJson = null, Evidence? Evidence = null);

public static class Format
{
    public static JsonSerializerOptions Options { get; } = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow,
        RespectNullableAnnotations = true,
        RespectRequiredConstructorParameters = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        WriteIndented = true
    };

    public static T Read<T>(string json)
    {
        using var document = JsonDocument.Parse(json);
        CheckProperties(document.RootElement);
        return JsonSerializer.Deserialize<T>(json, Options)
            ?? throw new InvalidDataException("JSON must not be null.");
    }

    public static string Write<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static void Require([DoesNotReturnIf(false)] bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidDataException(message);
        }
    }

    public static void Text(string value, string name) =>
        Require(!string.IsNullOrWhiteSpace(value), $"{name} must be nonempty.");

    public static void Utc(DateTimeOffset value, string name) =>
        Require(value.Offset == TimeSpan.Zero && value != default, $"{name} must be an absolute UTC timestamp.");

    public static DateTimeOffset ParseUtc(string value, string name)
    {
        Require(value.Contains('T') && (value.EndsWith('Z') || value.EndsWith("+00:00", StringComparison.Ordinal)),
            $"{name} must explicitly include an absolute UTC offset (Z or +00:00).");
        Require(DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed),
            $"{name} is not a valid UTC timestamp.");
        Utc(parsed, name);
        return parsed;
    }

    public static void Path(string value, string name)
    {
        Text(value, name);
        Require(!value.StartsWith('/') && !value.Contains('\\') &&
            value.Split('/').All(part => part.Length > 0 && part is not "." and not ".." &&
                part.All(c => char.IsAsciiLetterOrDigit(c) || c is '.' or '_' or '-')),
            $"{name} must be a normalized repository-relative path.");
    }

    public static void FullRef(string value)
    {
        Text(value, "ref");
        Require((value.StartsWith("refs/heads/", StringComparison.Ordinal) ||
            value.StartsWith("refs/remotes/", StringComparison.Ordinal)) &&
            !value.Contains("..", StringComparison.Ordinal) && !value.Contains("@{", StringComparison.Ordinal) &&
            value.Split('/').All(p => p.Length > 0 && !p.StartsWith('.') && !p.EndsWith('.') &&
                !p.EndsWith(".lock", StringComparison.Ordinal) &&
                p.All(c => char.IsAsciiLetterOrDigit(c) || c is '.' or '_' or '-')),
            "ref must be an exact refs/heads/ or refs/remotes/ ref.");
    }

    private static void CheckProperties(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            var names = new HashSet<string>(StringComparer.Ordinal);
            foreach (var property in element.EnumerateObject())
            {
                Require(names.Add(property.Name), $"Duplicate JSON property '{property.Name}'.");
                Require(property.Value.ValueKind != JsonValueKind.Null, $"Property '{property.Name}' must not be null; omit optional fields.");
                if (property.Name is "at" or "createdAt" or "expiresAt" or "approvedAt" or "evaluatedAt")
                {
                    Require(property.Value.ValueKind == JsonValueKind.String, $"{property.Name} must be a UTC timestamp string.");
                    ParseUtc(property.Value.GetString()!, property.Name);
                }
                CheckProperties(property.Value);
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                Require(item.ValueKind != JsonValueKind.Null, "Array items must not be null.");
                CheckProperties(item);
            }
        }
    }
}

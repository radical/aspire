// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.IO.Compression;
using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task ArtifactSelectionUsesExactJobContractAndAttemptWindow()
    {
        var input = ArtifactFixture();
        var old = input["artifacts"]![0]!.DeepClone();
        old["id"] = 49;
        old["created_at"] = "2026-10-21T09:59:59Z";
        input["artifacts"]!.AsArray().Add(old);
        var unrelated = input["artifacts"]![0]!.DeepClone();
        unrelated["id"] = 51;
        unrelated["name"] = "logs-Other-8-core-ubuntu-latest";
        input["artifacts"]!.AsArray().Add(unrelated);

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        var artifact = Assert.Single(result.Output["artifacts"]!.AsArray())!;
        Assert.Equal(50, artifact["id"]!.GetValue<int>());
        Assert.Equal(TestJob, artifact["job"]!.GetValue<string>());
        Assert.Equal("trx", artifact["format"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("2026-10-21T10:00:00Z", false)]
    [InlineData("2026-10-21T10:00:01Z", true)]
    [InlineData("2026-10-21T11:00:00Z", true)]
    [InlineData("2026-10-21T11:00:01Z", false)]
    public async Task ArtifactSelectionUsesOpenStartAndClosedEnd(string createdAt, bool accepted)
    {
        var input = ArtifactFixture();
        input["artifacts"]![0]!["created_at"] = createdAt;
        var result = await ExecuteAsync("select-artifacts", input);

        if (accepted)
        {
            AssertSuccess(result);
            Assert.Single(result.Output["artifacts"]!.AsArray());
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("expired")]
    [InlineData("ambiguous")]
    [InlineData("malformed-job")]
    [InlineData("shared-producer")]
    public async Task RequiredArtifactProvenanceFailsClosed(string mutation)
    {
        var input = ArtifactFixture();
        switch (mutation)
        {
            case "missing":
                input["artifacts"] = new JsonArray();
                break;
            case "expired":
                input["artifacts"]![0]!["expired"] = true;
                break;
            case "ambiguous":
                var duplicate = input["artifacts"]![0]!.DeepClone();
                duplicate["id"] = 51;
                input["artifacts"]!.AsArray().Add(duplicate);
                break;
            case "malformed-job":
                input["jobs"]![0]!["name"] = "Tests / No-package tests / malformed";
                break;
            case "shared-producer":
                var secondJob = input["jobs"]![0]!.DeepClone();
                secondJob["id"] = 2;
                input["jobs"]!.AsArray().Add(secondJob);
                break;
        }

        AssertRejected(await ExecuteAsync("select-artifacts", input));
    }

    [Theory]
    [InlineData("id", "0")]
    [InlineData("id", "1.5")]
    [InlineData("id", "\"50\"")]
    [InlineData("size_in_bytes", "-1")]
    [InlineData("size_in_bytes", "1.5")]
    [InlineData("size_in_bytes", "\"10\"")]
    [InlineData("size_in_bytes", "101")]
    public async Task RequiredArtifactMetadataMustBeValidAndBounded(string field, string value)
    {
        var input = ArtifactFixture();
        input["budgets"] = Object("""{"artifact_bytes":100,"total_bytes":1000,"artifact_count":20}""");
        input["artifacts"]![0]![field] = JsonNode.Parse(value);

        AssertRejected(await ExecuteAsync("select-artifacts", input));
    }

    [Fact]
    public async Task ForceKilledTestJobIsRecognizedWithoutUploadStep()
    {
        var input = ArtifactFixture();
        input["jobs"]![0]!["steps"] = new JsonArray();
        input["artifacts"] = new JsonArray();

        AssertRejected(await ExecuteAsync("select-artifacts", input));
    }

    [Theory]
    [InlineData("Linux", "linux-x64")]
    [InlineData("Windows", "win-x64")]
    public async Task ExtensionArtifactsUseExactShardRidAndAttempt(string os, string rid)
    {
        var input = ArtifactFixture();
        input["run"]!["run_attempt"] = 2;
        input["context"]!["run_attempt"] = 2;
        input["jobs"]![0]!["name"] = $"Tests / VS Code extension E2E tests / VS Code extension E2E ({os}, debug)";
        input["jobs"]![0]!["steps"] = new JsonArray();
        input["artifacts"]![0]!["name"] = $"extension-e2e-diagnostics-{rid}-debug-attempt2";
        var earlier = input["artifacts"]![0]!.DeepClone();
        earlier["id"] = 49;
        earlier["name"] = $"extension-e2e-diagnostics-{rid}-debug-attempt1";
        input["artifacts"]!.AsArray().Add(earlier);

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        var selected = Assert.Single(result.Output["artifacts"]!.AsArray())!;
        Assert.Equal(50, selected["id"]!.GetValue<int>());
        Assert.Equal("mocha", selected["format"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("missing")]
    [InlineData("oversized")]
    public async Task OptionalExtensionArtifactCanBeUnavailable(string mutation)
    {
        var input = ArtifactFixture();
        input["jobs"]![0]!["name"] = "Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, debug)";
        input["jobs"]![0]!["steps"] = new JsonArray();
        input["artifacts"]![0]!["name"] = "extension-e2e-diagnostics-linux-x64-debug-attempt1";
        if (mutation == "missing")
        {
            input["artifacts"] = new JsonArray();
        }
        else
        {
            input["artifacts"]![0]!["size_in_bytes"] = 104857601;
        }

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["artifacts"]!.AsArray());
    }

    [Fact]
    public async Task OptionalArtifactDoesNotDisplaceRequiredEvidenceFromSharedBudget()
    {
        var input = ArtifactFixture();
        input["budgets"] = Object("""{"artifact_count":1,"total_bytes":100,"artifact_bytes":100}""");
        input["jobs"]!.AsArray().Insert(0, Object("""
            {"id":2,"name":"Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, debug)","steps":[]}
            """));
        input["artifacts"]!.AsArray().Insert(0, Object("""
            {"id":49,"name":"extension-e2e-diagnostics-linux-x64-debug-attempt1",
             "size_in_bytes":100,"expired":false,"created_at":"2026-10-21T10:30:00Z"}
            """));

        var result = await ExecuteAsync("select-artifacts", input);

        AssertSuccess(result);
        Assert.Equal(50, Assert.Single(result.Output["artifacts"]!.AsArray())!["id"]!.GetValue<int>());
    }

    [Theory]
    [InlineData(1, 100, true)]
    [InlineData(0, 100, false)]
    [InlineData(1, 99, false)]
    public async Task RequiredArtifactBudgetsHaveExactBoundaries(int count, int totalBytes, bool accepted)
    {
        var input = ArtifactFixture();
        input["artifacts"]![0]!["size_in_bytes"] = 100;
        input["budgets"] = new JsonObject
        {
            ["artifact_count"] = count, ["total_bytes"] = totalBytes, ["artifact_bytes"] = 100,
        };
        var result = await ExecuteAsync("select-artifacts", input);

        if (accepted)
        {
            AssertSuccess(result);
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Theory]
    [InlineData("trx")]
    [InlineData("mocha")]
    public async Task ExtractionReadsOnlySelectedResultFormatAndFlattensPaths(string format)
    {
        var input = ExtractionFixture(
            ("nested/results.trx", "<TestRun/>", 0),
            ("other/mocha.json", """{"tests":[],"failures":[]}""", 0),
            ("large-recording.cast", new string('x', 10000), 0));
        input["format"] = format;
        input["budgets"]!["uncompressed_bytes"] = 100;

        var result = await ExecuteAsync("extract-artifact", input);

        AssertSuccess(result);
        var file = Assert.Single(result.Output["files"]!.AsObject());
        Assert.Equal(format == "trx" ? "00001.trx" : "00001.json", file.Key);
        Assert.Equal(format == "trx" ? "<TestRun/>" : """{"tests":[],"failures":[]}""", file.Value!.GetValue<string>());
    }

    [Theory]
    [InlineData("../escape.trx")]
    [InlineData("/absolute.trx")]
    [InlineData("\\absolute.trx")]
    [InlineData("nested\\escape.trx")]
    [InlineData("folder/../../escape.txt")]
    public async Task ExtractionRejectsUnsafePathsEvenInUnselectedFiles(string path)
    {
        var input = ExtractionFixture(("ok.trx", "<TestRun/>", 0), (path, "bad", 0));

        var result = await ExecuteAsync("extract-artifact", input);

        AssertRejected(result);
        Assert.Empty(result.Output["files"]!.AsObject());
    }

    [Theory]
    [InlineData(0xA1FF)]
    [InlineData(0x11FF)]
    public async Task ExtractionRejectsSymlinksAndSpecialFiles(int mode)
    {
        var input = ExtractionFixture(("results.trx", "target", unchecked(mode << 16)));

        AssertRejected(await ExecuteAsync("extract-artifact", input));
    }

    [Theory]
    [InlineData("archive_bytes", 1)]
    [InlineData("entry_count", 1)]
    [InlineData("uncompressed_bytes", 9)]
    public async Task ExtractionEnforcesDownloadEntryAndActualWrittenByteBudgets(string field, int budget)
    {
        var input = ExtractionFixture(("first.trx", "12345", 0), ("second.trx", "67890", 0));
        input["budgets"]![field] = budget;

        var result = await ExecuteAsync("extract-artifact", input);

        AssertRejected(result);
        Assert.Empty(result.Output["files"]!.AsObject());
    }

    [Fact]
    public async Task ExtractionAcceptsActualWrittenBytesExactlyAtBudget()
    {
        var input = ExtractionFixture(("first.trx", "12345", 0), ("second.trx", "67890", 0));
        input["budgets"]!["uncompressed_bytes"] = 10;

        var result = await ExecuteAsync("extract-artifact", input);

        AssertSuccess(result);
        Assert.Equal(2, result.Output["files"]!.AsObject().Count);
    }

    [Fact]
    public async Task ExtractionRejectsDownloadedSizeDifferentFromApiMetadata()
    {
        var input = ExtractionFixture(("results.trx", "<TestRun/>", 0));
        input["expected_bytes"] = input["expected_bytes"]!.GetValue<int>() + 1;

        AssertRejected(await ExecuteAsync("extract-artifact", input));
    }

    [Fact]
    public async Task ExtractionRejectsMalformedArchiveWithoutLeavingPartialResults()
    {
        var input = ExtractionFixture(("results.trx", "<TestRun/>", 0));
        var bytes = Convert.FromBase64String(input["archive_base64"]!.GetValue<string>());
        input["archive_base64"] = Convert.ToBase64String(bytes.AsSpan(0, bytes.Length - 12));
        input["expected_bytes"] = bytes.Length - 12;

        var result = await ExecuteAsync("extract-artifact", input);

        AssertRejected(result);
        Assert.Empty(result.Output["files"]!.AsObject());
    }

    [Theory]
    [InlineData("trx", "required")]
    [InlineData("mocha", "optional")]
    public async Task CollectionDistinguishesRequiredAndOptionalArtifactDownloadFailure(string format, string requirement)
    {
        var input = ArtifactFixture();
        if (format == "mocha")
        {
            input["jobs"]![0]!["name"] = "Tests / VS Code extension E2E tests / VS Code extension E2E (Linux, debug)";
            input["jobs"]![0]!["steps"] = Array("""
                [{"name":"Download prerequisites","conclusion":"failure"},{"name":"Test","conclusion":"skipped"}]
                """);
            input["artifacts"]![0]!["name"] = "extension-e2e-diagnostics-linux-x64-debug-attempt1";
        }
        input["faults"]!["download-artifact:50"] = "HTTP 404";

        var result = await ExecuteAsync("collect", input);

        AssertSuccess(result);
        Assert.Equal(requirement == "required" ? "unavailable" : "complete",
            result.Output["test_evidence"]!["state"]!.GetValue<string>());
        Assert.Empty(result.Output["test_failures"]!.AsArray());
        Assert.NotEmpty(result.Diagnostics);
    }

    private static JsonObject ArtifactFixture()
    {
        var input = Fixture();
        input["jobs"]![0]!["name"] = TestJob;
        input["jobs"]![0]!["steps"] = Array("""
            [{"name":"Run tests","conclusion":"failure"},{"name":"Upload logs, and test results","conclusion":"success"}]
            """);
        input["artifacts"] = Array("""
            [{"id":50,"name":"logs-Infrastructure-8-core-ubuntu-latest","size_in_bytes":10,
              "expired":false,"created_at":"2026-10-21T10:30:00Z"}]
            """);

        return input;
    }

    private static JsonObject ExtractionFixture(params (string Name, string Content, int Attributes)[] files)
    {
        using var buffer = new MemoryStream();
        using (var zip = new ZipArchive(buffer, ZipArchiveMode.Create, leaveOpen: true))
        {
            foreach (var (name, content, attributes) in files)
            {
                var entry = zip.CreateEntry(name);
                entry.ExternalAttributes = attributes;
                using var writer = new StreamWriter(entry.Open());
                writer.Write(content);
            }
        }

        return new JsonObject
        {
            ["archive_base64"] = Convert.ToBase64String(buffer.ToArray()),
            ["expected_bytes"] = checked((int)buffer.Length), ["format"] = "trx",
            ["budgets"] = Object("""{"archive_bytes":104857600,"entry_count":10000,"uncompressed_bytes":1073741824}"""),
        };
    }
}

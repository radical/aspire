// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Theory]
    [InlineData("Authorization: Bearer fake-credential", "Authorization: Bearer [REDACTED]")]
    [InlineData("Authorization: Basic fake-credential", "Authorization: Basic [REDACTED]")]
    [InlineData("x-api-key: fake-credential", "x-api-key: [REDACTED]")]
    [InlineData("https://user:password@example.test/path", "https://[REDACTED]:[REDACTED]@example.test/path")]
    [InlineData("https://user@example.test/path", "https://[REDACTED]@example.test/path")]
    [InlineData("https://example.test?sig=fake-credential&mode=1", "https://example.test?sig=[REDACTED]&mode=1")]
    [InlineData("password=\"fake value\"", "password=\"[REDACTED]\"")]
    [InlineData("'client_secret': 'fake value'", "'client_secret': '[REDACTED]'")]
    [InlineData("\"api_key\": \"fake value\"", "\"api_key\": \"[REDACTED]\"")]
    [InlineData("--token \"fake value\"", "--token \"[REDACTED]\"")]
    [InlineData("--password fake-value", "--password [REDACTED]")]
    [InlineData("ConnectionStrings__db=Server=host;Password=fake", "ConnectionStrings__db=[REDACTED]")]
    [InlineData("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----", "[REDACTED]")]
    [InlineData("ghp_abcdefghijklmnopqrstuvwxyz", "[REDACTED]")]
    [InlineData("github_pat_abcdefghijklmnopqrstuvwxyz", "[REDACTED]")]
    [InlineData("AKIAABCDEFGHIJKLMNOP", "[REDACTED]")]
    public async Task SanitizerRedactsRecognizableCredentials(string text, string expected)
    {
        var result = await ExecuteAsync("sanitize-text", TextFixture(text));

        AssertSuccess(result);
        Assert.Equal(expected, result.Output["text"]!.GetValue<string>());
    }

    [Fact]
    public async Task RedactionPrecedesTruncationSoLongCredentialCannotLeakPrefix()
    {
        var input = TextFixture("token=" + new string('x', 10000));
        input["max_length"] = 30;

        var result = await ExecuteAsync("sanitize-text", input);

        AssertSuccess(result);
        Assert.Equal("token=[REDACTED]", result.Output["text"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("\u001b[31mred\u001b[0m", "red")]
    [InlineData("a\u0000b\u0007c", "abc")]
    [InlineData("a\u202eb\u200bc", "abc")]
    [InlineData("a\u2028b\u2029c", "abc")]
    [InlineData("a\ufe0fb", "ab")]
    [InlineData("a\U000E0100b", "ab")]
    [InlineData("line1\r\nline2\rline3", "line1\nline2\nline3")]
    public async Task SanitizerRemovesTerminalAndUnicodeControlsAndNormalizesNewlines(string text, string expected)
    {
        var result = await ExecuteAsync("sanitize-text", TextFixture(text));

        AssertSuccess(result);
        Assert.Equal(expected, result.Output["text"]!.GetValue<string>());
    }

    [Fact]
    public async Task SanitizerBoundsUnicodeByScalarsWithoutSplittingSupplementaryCharacter()
    {
        var input = TextFixture("a\U0001F600b");
        input["max_length"] = 2;

        var result = await ExecuteAsync("sanitize-text", input);

        AssertSuccess(result);
        Assert.Equal("a\U0001F600", result.Output["text"]!.GetValue<string>());
    }

    [Fact]
    public async Task RenderingUntrustedTextIndentsEveryLineIncludingFenceBreakouts()
    {
        var input = TextFixture("first\n```\n@reviewers\n<!-- pretend marker -->\n");

        var result = await ExecuteAsync("render-text", input);

        AssertSuccess(result);
        Assert.Equal(
            "    first\n    ```\n    @reviewers\n    <!-- pretend marker -->\n    \n",
            result.Output["text"]!.GetValue<string>());
    }

    [Fact]
    public async Task DiagnosticRebindingSanitizesAllTrustedStreamsBeforePublishing()
    {
        var input = FlakyFixture();
        foreach (var field in new[] { "error", "stack_trace", "standard_output", "standard_error" })
        {
            input["test_failures"]![0]![field] = "Authorization: Bearer fake-credential\r\nsafe";
        }

        var result = await ExecuteAsync("validate", input);

        AssertSuccess(result);
        foreach (var field in new[] { "error", "stack_trace", "standard_output", "standard_error" })
        {
            Assert.Equal("Authorization: Bearer [REDACTED]\nsafe",
                result.Output["analysis"]!["failed_tests"]![0]![field]!.GetValue<string>());
        }
    }

    [Theory]
    [InlineData("error", 1000)]
    [InlineData("stack_trace", 2000)]
    [InlineData("standard_output", 4000)]
    [InlineData("standard_error", 4000)]
    public async Task TrustedDiagnosticRebindingEnforcesExactFieldBudget(string field, int limit)
    {
        var input = FlakyFixture();
        input["test_failures"]![0]![field] = new string('x', limit + 1);

        var result = await ExecuteAsync("validate", input);

        AssertSuccess(result);
        Assert.Equal(new string('x', limit), result.Output["analysis"]!["failed_tests"]![0]![field]!.GetValue<string>());
    }

    [Fact]
    public async Task RejectedUntrustedValuesCannotInjectWorkflowCommands()
    {
        var input = Fixture();
        input["analysis"]!["verdict"] = "invalid\n::add-mask::injected";

        var result = await ExecuteAsync("validate", input);

        AssertRejected(result);
        Assert.All(result.Diagnostics, diagnostic =>
        {
            Assert.Single(diagnostic.ReplaceLineEndings("\n").Split('\n'));
            Assert.False(diagnostic.Contains("\n::", StringComparison.Ordinal));
        });
    }

    [Fact]
    public async Task CauseMetadataCannotEscapeCodeSpanOrAddTableColumns()
    {
        var input = Fixture("main");
        input["jobs"]![0]!["name"] = "Build | [link](https://evil.example) @reviewers `quoted`";

        var result = await ExecuteAsync("publish", input);

        AssertSuccess(result);
        var section = ManagedOccurrences(OnlyCall(result, "create-issue").Data["body"]!.GetValue<string>());
        Assert.Contains("`` Build \\| [link](https://evil.example) @reviewers `quoted` ``", section, StringComparison.Ordinal);
    }

    private static JsonObject TextFixture(string text)
        => new() { ["text"] = text, ["max_length"] = 10485760 };
}

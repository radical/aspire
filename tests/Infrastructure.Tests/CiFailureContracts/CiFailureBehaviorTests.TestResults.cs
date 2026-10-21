// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Nodes;
using Xunit;

namespace Infrastructure.Tests.CiFailureContracts;

public abstract partial class CiFailureBehaviorTests
{
    [Fact]
    public async Task TrxCollectorReadsAllFailuresAndDiagnosticsWithTrustedJobProvenance()
    {
        var input = ResultsFixture("trx", """
            <TestRun xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010">
              <Results>
                <UnitTestResult testName="Tests.First" outcome="Failed"><Output>
                  <ErrorInfo><Message>first error</Message><StackTrace>first frame</StackTrace></ErrorInfo>
                  <StdOut>output</StdOut><StdErr>stderr</StdErr>
                </Output></UnitTestResult>
                <UnitTestResult testName="Tests.Passed" outcome="Passed"/>
                <UnitTestResult testName="Tests.Skipped" outcome="NotExecuted"/>
                <UnitTestResult testName="Tests.Second" outcome="Failed"/>
              </Results>
            </TestRun>
            """);

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Collection(result.Output["test_failures"]!.AsArray(),
            first =>
            {
                Assert.Equal("Tests.First", first!["test"]!.GetValue<string>());
                Assert.Equal("Build", first["job"]!.GetValue<string>());
                Assert.Equal("first error", first["error"]!.GetValue<string>());
                Assert.Equal("first frame", first["stack_trace"]!.GetValue<string>());
                Assert.Equal("output", first["standard_output"]!.GetValue<string>());
                Assert.Equal("stderr", first["standard_error"]!.GetValue<string>());
            },
            second =>
            {
                Assert.Equal("Tests.Second", second!["test"]!.GetValue<string>());
                Assert.Equal("Build", second["job"]!.GetValue<string>());
                Assert.Equal("", second["error"]!.GetValue<string>());
            });
    }

    [Fact]
    public async Task TrxCollectorSupportsSingleResultAndCdata()
    {
        var input = ResultsFixture("trx", """
            <TestRun><Results><UnitTestResult testName="Tests.First" outcome="Failed"><Output>
            <ErrorInfo><Message><![CDATA[expected < actual & value]]></Message></ErrorInfo>
            </Output></UnitTestResult></Results></TestRun>
            """);

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Equal("expected < actual & value",
            Assert.Single(result.Output["test_failures"]!.AsArray())!["error"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("<broken")]
    [InlineData("<NotTrx/>")]
    public async Task AnyMalformedTrxInvalidatesWholeBatch(string malformed)
    {
        var input = ResultsFixture("trx", """<TestRun><Results><UnitTestResult testName="Good" outcome="Failed"/></Results></TestRun>""");
        input["files"]!["second.trx"] = malformed;

        var result = await ExecuteAsync("read-test-results", input);

        AssertRejected(result);
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Theory]
    [InlineData("trx", false)]
    [InlineData("mocha", true)]
    public async Task NoResultFileIsOnlyAllowedForOptionalMocha(string format, bool accepted)
    {
        var input = ResultsFixture(format, "");
        input["files"] = new JsonObject();
        var result = await ExecuteAsync("read-test-results", input);

        if (accepted)
        {
            AssertSuccess(result);
            Assert.Empty(result.Output["test_failures"]!.AsArray());
        }
        else
        {
            AssertRejected(result);
        }
    }

    [Fact]
    public async Task ValidTrxWithNoFailedResultsIsCompleteEmptyEvidence()
    {
        var input = ResultsFixture("trx", """<TestRun><Results><UnitTestResult testName="Passed" outcome="Passed"/></Results></TestRun>""");
        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Theory]
    [InlineData("Unknown")]
    [InlineData("")]
    public async Task TestResultProvenanceMustNameExactlyOneTrustedJob(string job)
    {
        var input = ResultsFixture("trx", "<TestRun/>");
        input["job"] = job;

        AssertRejected(await ExecuteAsync("read-test-results", input));
    }

    [Fact]
    public async Task TestResultsCannotBindToAmbiguousDuplicateJobName()
    {
        var input = ResultsFixture("trx", "<TestRun/>");
        var duplicate = input["jobs"]![0]!.DeepClone();
        duplicate["id"] = 2;
        input["jobs"]!.AsArray().Add(duplicate);

        AssertRejected(await ExecuteAsync("read-test-results", input));
    }

    [Theory]
    [InlineData("fullTitle")]
    [InlineData("title")]
    public async Task MochaCollectorRequiresCompletedTestAndProvidesErrorAndStack(string titleField)
    {
        var report = new JsonObject
        {
            ["tests"] = new JsonArray(new JsonObject { [titleField] = "suite test" }),
            ["failures"] = new JsonArray(new JsonObject
            {
                [titleField] = "suite test",
                ["err"] = Object("""{"name":"AssertionError","message":"expected true","stack":"frame"}"""),
            }),
        };
        var input = ResultsFixture("mocha", report.ToJsonString());

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        var test = Assert.Single(result.Output["test_failures"]!.AsArray())!;
        Assert.Equal("suite test", test["test"]!.GetValue<string>());
        Assert.Equal("Build", test["job"]!.GetValue<string>());
        Assert.Equal("expected true", test["error"]!.GetValue<string>());
        Assert.Equal("frame", test["stack_trace"]!.GetValue<string>());
        Assert.Equal("", test["standard_output"]!.GetValue<string>());
        Assert.Equal("", test["standard_error"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("InvalidSessionIdError", "session")]
    [InlineData("NoSuchSessionError", "session")]
    [InlineData("NoSuchWindowError", "window")]
    [InlineData("SessionNotCreatedError", "session")]
    [InlineData("WebDriverError", "session deleted because of page crash")]
    [InlineData("WebDriverError", "disconnected: not connected to devtools")]
    [InlineData("WebDriverError", "chrome not reachable")]
    public async Task MochaHarnessFailureDoesNotPartiallyAuthenticateOtherFailures(string name, string message)
    {
        var report = Object("""
            {"tests":[{"fullTitle":"first"},{"fullTitle":"second"}],"failures":[
              {"fullTitle":"first","err":{"name":"AssertionError","message":"assertion"}},
              {"fullTitle":"second","err":{}}]}
            """);
        report["failures"]![1]!["err"]!["name"] = name;
        report["failures"]![1]!["err"]!["message"] = message;

        var result = await ExecuteAsync("read-test-results", ResultsFixture("mocha", report.ToJsonString()));

        AssertSuccess(result);
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Fact]
    public async Task MochaHookFailureAbsentFromCompletedTestsIsNotTestEvidence()
    {
        var input = ResultsFixture("mocha", """
            {"tests":[{"fullTitle":"first"}],"failures":[
              {"fullTitle":"first","err":{"name":"AssertionError","message":"assertion"}},
              {"fullTitle":"before all hook","err":{"message":"setup failed"}}]}
            """);

        var result = await ExecuteAsync("read-test-results", input);

        AssertSuccess(result);
        Assert.Empty(result.Output["test_failures"]!.AsArray());
    }

    [Theory]
    [InlineData("not json")]
    [InlineData("{}")]
    [InlineData("""{"tests":[],"failures":{}}""")]
    [InlineData("""{"tests":[{}],"failures":[]}""")]
    [InlineData("""{"tests":[{"fullTitle":"test"}],"failures":[{"fullTitle":"test","err":{"message":42}}]}""")]
    public async Task MalformedMochaReportFailsClosed(string report)
        => AssertRejected(await ExecuteAsync("read-test-results", ResultsFixture("mocha", report)));

    [Fact]
    public async Task MultipleMochaReportsAreAmbiguous()
    {
        var input = ResultsFixture("mocha", """{"tests":[],"failures":[]}""");
        input["files"]!["second.json"] = """{"tests":[],"failures":[]}""";

        AssertRejected(await ExecuteAsync("read-test-results", input));
    }

    private static JsonObject ResultsFixture(string format, string content)
    {
        var input = Fixture();
        input["format"] = format;
        input["job"] = "Build";
        input["files"] = new JsonObject { [format == "trx" ? "results.trx" : "mocha.json"] = content };

        return input;
    }
}

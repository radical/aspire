// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Globalization;
using System.Xml;
using System.Xml.Linq;

namespace KnownTestFailures;

public sealed record TestResult(string TestId, string Method, string DisplayName, string Outcome, string? Message)
{
    public string? SkipReason { get; init; }
}

public static class StrictTrx
{
    private static readonly XNamespace s_ns = "http://microsoft.com/schemas/VisualStudio/TeamTest/2010";

    public static TestResult[] Read(string xml, bool allowSkipped = false, bool allowEmpty = false)
    {
        using var text = new StringReader(xml);
        using var reader = XmlReader.Create(text, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });
        var root = XDocument.Load(reader).Root ?? throw new InvalidDataException("TRX has no root.");
        Format.Require(root.Name == s_ns + "TestRun", "TRX must have a TeamTest 2010 TestRun root.");
        Format.Require(root.DescendantsAndSelf().All(e => e.Name.Namespace == s_ns), "Foreign TRX namespaces are unsupported.");
        Format.Require(root.Elements().All(e => e.Name.LocalName is
            "Times" or "TestSettings" or "TestDefinitions" or "Results" or "TestEntries" or "TestLists" or "ResultSummary"),
            "Unsupported TRX root section.");
        var definitions = One(root, "TestDefinitions");
        var results = One(root, "Results");
        var summary = One(root, "ResultSummary");
        Format.Require(root.Descendants(s_ns + "ErrorInfo").All(e => e.Parent?.Parent?.Name == s_ns + "UnitTestResult"),
            "Run-level ErrorInfo blocks evaluation.");
        Format.Require(!root.Descendants(s_ns + "RunInfo").Any(), "RunInfos diagnostics are unsupported.");
        Format.Require(!root.Descendants(s_ns + "InnerResults").Any() &&
            !root.Descendants(s_ns + "TestResultAggregation").Any(), "Nested TRX results are unsupported.");
        var summaryOutcome = (string?)summary.Attribute("outcome");
        Format.Require(summaryOutcome is "Passed" or "Failed" or "Completed", "TRX run summary must indicate normal completion.");
        var methods = new Dictionary<string, (string Method, string Display)>(StringComparer.Ordinal);
        foreach (var definition in definitions.Elements())
        {
            Format.Require(definition.Name == s_ns + "UnitTest", "Only UnitTest definitions are supported.");
            var method = One(definition, "TestMethod");
            var className = Attribute(method, "className");
            var methodName = Attribute(method, "name");
            // MTP can emit either "Method" or "Namespace.Class.Method" as TestMethod/@name.
            var canonical = methodName.StartsWith(className + ".", StringComparison.Ordinal)
                ? methodName : className + "." + methodName;
            Format.Require(methods.TryAdd(Attribute(definition, "id"), (canonical, Attribute(definition, "name"))),
                "Duplicate TRX definition testId.");
        }
        var parsed = new List<TestResult>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var result in results.Elements())
        {
            Format.Require(result.Name == s_ns + "UnitTestResult", "Only concrete UnitTestResult results are supported.");
            Format.Require(result.Attribute("resultType") is null &&
                result.Elements().All(e => e.Name == s_ns + "Output"), "Unsupported aggregate or extended result shape.");
            var id = Attribute(result, "testId");
            Format.Require(ids.Add(id) && methods.ContainsKey(id), "Duplicate or unresolved TRX result testId.");
            var outcome = Attribute(result, "outcome");
            Format.Require(outcome is "Passed" or "Failed" || (allowSkipped && outcome == "NotExecuted"),
                $"Unsupported TRX outcome '{outcome}'.");
            var display = Attribute(result, "testName");
            Format.Require(display == methods[id].Display, "TRX definition/result display mismatch.");
            var outputs = result.Elements(s_ns + "Output").ToArray();
            Format.Require(outputs.Length <= 1, "Duplicate TRX Output.");
            if (outputs.Length == 1)
            {
                Format.Require(outputs[0].Elements().All(e => e.Name.LocalName is "ErrorInfo" or "StdOut" or "StdErr" or "TextMessages") &&
                    outputs[0].Elements().GroupBy(e => e.Name).All(g => g.Count() == 1),
                    "Unsupported TRX Output shape.");
                Format.Require(outputs[0].Elements().Where(e => e.Name.LocalName is "StdOut" or "StdErr").All(e => !e.HasElements),
                    "TRX stdout/stderr must be text.");
                // xUnit's native writer emits test output as
                // <TextMessages><Message>ordinary test output</Message></TextMessages>,
                // distinct from ErrorInfo and run-level diagnostics.
                // https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.runner.common/Results/Builtin/TrxResultWriterMesageHandler.cs
                foreach (var messages in outputs[0].Elements(s_ns + "TextMessages"))
                {
                    Format.Require(messages.HasElements &&
                        messages.Elements().All(e => e.Name == s_ns + "Message" && !e.HasElements),
                        "Unsupported TRX text messages.");
                }
            }
            var errors = outputs.SingleOrDefault()?.Elements(s_ns + "ErrorInfo").ToArray() ?? [];
            Format.Require(errors.Length <= 1, "Duplicate TRX ErrorInfo.");
            string? message = null;
            if (errors.Length == 1)
            {
                Format.Require(errors[0].Elements().All(e => e.Name.LocalName is "Message" or "StackTrace") &&
                    errors[0].Elements().All(e => !e.HasElements) &&
                    errors[0].Elements(s_ns + "StackTrace").Count() <= 1, "Unsupported TRX ErrorInfo shape.");
                message = One(errors[0], "Message").Value;
                Format.Text(message, "ErrorInfo.Message");
            }
            Format.Require((outcome == "Failed") == (message is not null), "TRX failure/message inconsistency.");
            var reason = outcome == "NotExecuted" ? (string?)outputs.SingleOrDefault()?.Element(s_ns + "StdOut") : null;
            if (outcome == "NotExecuted")
            {
                Format.Text(reason!, "TRX skip reason");
            }
            parsed.Add(new(id, methods[id].Method, display, outcome, message) { SkipReason = reason });
        }
        Format.Require((allowEmpty || parsed.Count > 0) && ids.SetEquals(methods.Keys),
            "TRX definitions/results must be nonempty and complete.");
        var entries = root.Elements(s_ns + "TestEntries").ToArray();
        Format.Require(entries.Length <= 1, "Duplicate TRX TestEntries.");
        if (entries.Length == 1)
        {
            var listed = entries[0].Elements().ToArray();
            Format.Require(listed.Length == parsed.Count &&
                listed.All(e => e.Name == s_ns + "TestEntry" && !e.HasElements) &&
                listed.Select(e => Attribute(e, "testId")).ToHashSet(StringComparer.Ordinal).SetEquals(ids),
                "TRX TestEntries must list every result exactly once.");
            foreach (var entry in listed)
            {
                var result = results.Elements().Single(e => Attribute(e, "testId") == Attribute(entry, "testId"));
                var definition = definitions.Elements().Single(e => Attribute(e, "id") == Attribute(entry, "testId"));
                Format.Require(Attribute(entry, "executionId") == Attribute(result, "executionId") &&
                    Attribute(entry, "executionId") == Attribute(One(definition, "Execution"), "id") &&
                    Attribute(entry, "testListId") == Attribute(result, "testListId"),
                    "TRX TestEntry execution/list linkage mismatch.");
            }
        }
        Format.Require(!root.Descendants(s_ns + "UnitTestResult").Except(results.Elements()).Any(),
            "Unexpected nested results.");
        var counters = One(summary, "Counters");
        var expected = new Dictionary<string, int>(StringComparer.Ordinal)
        {
            ["total"] = parsed.Count,
            ["executed"] = parsed.Count(r => r.Outcome != "NotExecuted"),
            ["passed"] = parsed.Count(r => r.Outcome == "Passed"),
            ["failed"] = parsed.Count(r => r.Outcome == "Failed"),
            ["notExecuted"] = parsed.Count(r => r.Outcome == "NotExecuted")
        };
        foreach (var attribute in counters.Attributes())
        {
            Format.Require(int.TryParse(attribute.Value, NumberStyles.None, CultureInfo.InvariantCulture, out var value) &&
                value == expected.GetValueOrDefault(attribute.Name.LocalName),
                $"Inconsistent or unsupported TRX counter '{attribute.Name}'.");
        }
        Format.Require(expected.Where(pair => pair.Key != "notExecuted" || pair.Value != 0)
            .All(pair => counters.Attribute(pair.Key) is not null), "Missing essential TRX counters.");
        Format.Require(summaryOutcome != "Passed" || expected["failed"] == 0, "Passed summary contradicts failures.");
        Format.Require(summaryOutcome != "Failed" || expected["failed"] > 0, "Failed summary has no failures.");
        return [.. parsed];
    }

    private static XElement One(XElement parent, string name)
    {
        var children = parent.Elements(s_ns + name).ToArray();
        Format.Require(children.Length == 1, $"TRX requires exactly one {parent.Name.LocalName}/{name}.");
        return children[0];
    }

    private static string Attribute(XElement element, string name)
    {
        var value = (string?)element.Attribute(name);
        Format.Text(value!, $"{element.Name.LocalName}/@{name}");
        return value!;
    }
}

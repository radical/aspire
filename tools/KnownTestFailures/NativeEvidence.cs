// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Xml;
using System.Xml.Linq;

namespace KnownTestFailures;

public static class NativeEvidence
{
    public static string Identity(string project, string framework) => $"aspire-tests-v1:{project}:{framework}";

    public static DiscoveredCase[] Discover(string json)
    {
        // Native MTP 2.4 JSON, including individual serializable Theory rows:
        // {"schemaVersion":1,"tests":[{"uid":"...","displayName":"N.C.M(value: 1)",
        // "type":{"namespace":"N","typeName":"C","methodName":"M",...}}]}.
        // https://github.com/xunit/xunit/blob/8bf043c053/src/common/MicrosoftTestingPlatform/TestPlatformDiscoveryMessageSink.cs
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        static void UniqueProperties(JsonElement element)
        {
            if (element.ValueKind == JsonValueKind.Object)
            {
                var names = new HashSet<string>(StringComparer.Ordinal);
                foreach (var property in element.EnumerateObject())
                {
                    Format.Require(names.Add(property.Name), "Duplicate discovery JSON property.");
                    UniqueProperties(property.Value);
                }
            }
            else if (element.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in element.EnumerateArray())
                {
                    UniqueProperties(item);
                }
            }
        }
        UniqueProperties(root);
        Format.Require(root.GetProperty("schemaVersion").GetInt32() == 1, "Unsupported discovery schema.");
        var cases = root.GetProperty("tests").EnumerateArray().Select(test =>
        {
            var type = test.GetProperty("type");
            var ns = type.GetProperty("namespace").GetString();
            var name = type.GetProperty("typeName").GetString();
            var method = type.GetProperty("methodName").GetString();
            var id = test.GetProperty("uid").GetString()!;
            var display = test.GetProperty("displayName").GetString()!;
            Format.Text(name!, "discovered type");
            Format.Text(method!, "discovered method");
            Format.Text(id, "discovered UID");
            Format.Text(display, "discovered display");
            return new DiscoveredCase(id, (string.IsNullOrEmpty(ns) ? "" : ns + ".") + name + "." + method, display);
        }).OrderBy(c => c.CaseId, StringComparer.Ordinal).ToArray();
        Format.Require(cases.Select(c => c.CaseId).Distinct(StringComparer.Ordinal).Count() == cases.Length &&
            cases.Select(c => (c.Method, c.DisplayName)).Distinct().Count() == cases.Length,
            "Discovery has duplicate UIDs or ambiguous method/display identities.");
        return cases;
    }

    public static ExpectedCase[] Bind(DiscoveredCase[] inventory, TestResult[] results)
    {
        Format.Require(inventory.Length == results.Length, "Discovery inventory and execution differ (missing/extra cases).");
        return inventory.Select(item =>
        {
            var matching = results.Where(r => r.Method == item.Method && r.DisplayName == item.DisplayName).ToArray();
            Format.Require(matching.Length == 1, "Discovered identity has no unique execution binding.");
            return new ExpectedCase(item.CaseId, matching[0].TestId, item.Method, item.DisplayName);
        }).ToArray();
    }

    public static void ValidateLifecycle(string xml, TestResult[] results)
    {
        using var text = new StringReader(xml);
        using var reader = XmlReader.Create(text, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });
        var root = XDocument.Load(reader).Root ?? throw new InvalidDataException("Missing lifecycle report.");
        Format.Require(root.DescendantsAndSelf().All(e => e.Name.Namespace == XNamespace.None),
            "Foreign lifecycle namespaces are unsupported.");
        Format.Require(root.Name == "assemblies" && (string?)root.Attribute("schema-version") == "3" &&
            root.Elements().Count() == 1 && root.Attribute("finish-rtf") is not null, "Incomplete lifecycle root.");
        var assembly = root.Element("assembly") ?? throw new InvalidDataException("Missing assembly lifecycle.");
        static void Finished(XElement element)
        {
            Format.Require(DateTimeOffset.TryParse((string?)element.Attribute("start-rtf"), CultureInfo.InvariantCulture,
                DateTimeStyles.None, out var start) &&
                DateTimeOffset.TryParse((string?)element.Attribute("finish-rtf"), CultureInfo.InvariantCulture,
                DateTimeStyles.None, out var finish) && finish >= start,
                "Missing or invalid lifecycle start/finish timestamps.");
        }
        Finished(root);
        Finished(assembly);
        // finish-rtf/totals are written on ITestAssemblyFinished, unlike arbitrary console text.
        // Ordinary text <output>, <failure><message>... and <reason> are native test output,
        // not framework errors. NotRun is NOT a skip.
        // https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.runner.common/Results/Builtin/XmlV2ResultWriterMessageHandler.cs
        Format.Require(assembly.Attribute("finish-rtf") is not null &&
            (string?)assembly.Attribute("errors") == "0" && (string?)assembly.Attribute("not-run") == "0" &&
            !root.Descendants("errors").Any(), "Assembly did not finish normally or reported framework errors.");
        Format.Require(assembly.Elements().All(c => c.Name == "collection" && c.Elements().All(t => t.Name == "test")),
            "Unsupported lifecycle assembly/collection sections.");
        var tests = assembly.Elements("collection").SelectMany(c => c.Elements("test")).ToArray();
        static void Totals(XElement element, TestResult[] selected)
        {
            var expected = new Dictionary<string, int>
            {
                ["total"] = selected.Length, ["passed"] = selected.Count(r => r.Outcome == "Passed"),
                ["failed"] = selected.Count(r => r.Outcome == "Failed"),
                ["skipped"] = selected.Count(r => r.Outcome == "NotExecuted"), ["not-run"] = 0
            };
            foreach (var (name, count) in expected)
            {
                Format.Require((string?)element.Attribute(name) == count.ToString(CultureInfo.InvariantCulture),
                    $"Lifecycle {name} contradicts execution.");
            }
        }
        Format.Require(tests.Length == results.Length, "Lifecycle case count contradicts execution.");
        Totals(assembly, results);
        foreach (var collection in assembly.Elements("collection"))
        {
            var names = collection.Elements("test").Select(t => (string?)t.Attribute("name")).ToHashSet(StringComparer.Ordinal);
            Totals(collection, results.Where(r => names.Contains(LifecycleText(r.DisplayName))).ToArray());
        }
        foreach (var result in results)
        {
            var matching = tests.Where(t => (string?)t.Attribute("type") + "." + (string?)t.Attribute("method") == result.Method &&
                (string?)t.Attribute("name") == LifecycleText(result.DisplayName)).ToArray();
            Format.Require(matching.Length == 1, "Lifecycle test identity is ambiguous.");
            var test = matching[0];
            Finished(test);
            var outcome = result.Outcome switch { "Passed" => "Pass", "Failed" => "Fail", "NotExecuted" => "Skip", _ => "" };
            Format.Require(outcome.Length > 0 && (string?)test.Attribute("result") == outcome &&
                test.Attribute("finish-rtf") is not null, "Lifecycle outcome/completion contradicts execution.");
            Format.Require(test.Elements().All(e => e.Name.LocalName is "output" or "failure" or "reason" or "traits") &&
                test.Elements().GroupBy(e => e.Name).All(g => g.Count() == 1), "Unsupported lifecycle test children.");
            Format.Require(test.Elements("output").All(e => !e.HasElements) &&
                test.Elements("reason").All(e => !e.HasElements && !string.IsNullOrWhiteSpace(e.Value)),
                "Lifecycle output/reason must be native text.");
            Format.Require(test.Elements("reason").Count() == (outcome == "Skip" ? 1 : 0) &&
                test.Elements("failure").Count() == (outcome == "Fail" ? 1 : 0),
                "Lifecycle failure/skip details contradict outcome.");
            Format.Require(outcome != "Skip" || test.Element("reason")!.Value == LifecycleText(result.SkipReason!, false),
                "Lifecycle skip reason contradicts TRX.");
            foreach (var traits in test.Elements("traits"))
            {
                Format.Require(traits.Elements().All(t => t.Name == "trait" && !t.HasElements &&
                    t.Attribute("name") is not null && t.Attribute("value") is not null), "Unsupported lifecycle traits.");
            }
            if (test.Element("failure") is { } failure)
            {
                Format.Require(failure.Elements().All(e => e.Name.LocalName is "message" or "stack-trace" && !e.HasElements) &&
                    failure.Elements("message").Count() == 1 && failure.Elements("stack-trace").Count() <= 1 &&
                    failure.Element("message")!.Value == LifecycleText(result.Message!, false), "Lifecycle failure contradicts TRX.");
            }
        }
    }

    private static string LifecycleText(string value, bool escapeNewlines = true)
    {
        // xUnit's XML attributes/text add a reversible escape layer beyond XML:
        // display `M(value: "C:\work")` becomes `M(value: \"C:\\work\")`.
        // Compare the expected escaped text, never heuristically unescape identities.
        // https://github.com/xunit/xunit/blob/8bf043c053/src/xunit.v3.runner.common/Utility/XmlUtility.cs
        return string.Concat(value.Select((c, index) => c switch
        {
            '"' => "\\\"", '\\' => "\\\\", '\0' => "\\0", '\a' => "\\a",
            '\b' => "\\b", '\f' => "\\f", '\t' => "\\t", '\v' => "\\v",
            '\n' => escapeNewlines ? "\\n" : "\n", '\r' => escapeNewlines ? "\\r" : "\r",
            < ' ' => "\\x" + ((int)c).ToString("x2", CultureInfo.InvariantCulture),
            '\ufffe' or '\uffff' => "\\x" + ((int)c).ToString("x4", CultureInfo.InvariantCulture),
            _ when char.IsHighSurrogate(c) && (index + 1 == value.Length || !char.IsLowSurrogate(value[index + 1])) ||
                char.IsLowSurrogate(c) && (index == 0 || !char.IsHighSurrogate(value[index - 1])) =>
                    "\\x" + ((int)c).ToString("x4", CultureInfo.InvariantCulture),
            _ => c.ToString()
        }));
    }

    public static string[] Arguments(string text, bool powerShell = false)
    {
        // Trusted workflow/MSBuild option strings, e.g.:
        // --filter-class "*.Cases" --filter-not-trait 'category=failing'
        // Examples: --filter-class "*.Tests" /p:OutDir="C:\work tree\"
        // Windows workflow literals use PowerShell quotes/backtick escaping, where
        // backslashes are ordinary characters; Unix literals use backslash escaping.
        // No variable, command or pathname expansion occurs. Tokens use ArgumentList.
        // https://learn.microsoft.com/powershell/module/microsoft.powershell.core/about/about_quoting_rules
        var args = new List<string>();
        var token = new StringBuilder();
        char quote = '\0';
        var started = false;
        for (var index = 0; index < text.Length; index++)
        {
            var c = text[index];
            if (char.IsWhiteSpace(c) && quote == '\0')
            {
                if (started)
                {
                    args.Add(token.ToString());
                    token.Clear();
                    started = false;
                }
                continue;
            }
            started = true;
            if (powerShell && quote != '\0' && c == quote && index + 1 < text.Length && text[index + 1] == quote)
            {
                token.Append(c);
                index++;
                continue;
            }
            if (c is '\'' or '"' && (quote == '\0' || quote == c))
            {
                quote = quote == '\0' ? c : '\0';
            }
            else if (c == (powerShell ? '`' : '\\') && quote != '\'')
            {
                Format.Require(++index < text.Length, "Trailing argument escape.");
                var next = text[index];
                if (!powerShell && quote == '"' && next is not ('"' or '\\' or '$' or '`'))
                {
                    token.Append('\\');
                }
                token.Append(powerShell ? next switch
                {
                    '0' => '\0', 'a' => '\a', 'b' => '\b', 'e' => '\u001b',
                    'f' => '\f', 'n' => '\n', 'r' => '\r', 't' => '\t', 'v' => '\v',
                    _ => next
                } : next);
            }
            else
            {
                token.Append(c);
            }
        }
        Format.Require(quote == '\0', "Unterminated argument quote.");
        if (started)
        {
            args.Add(token.ToString());
        }
        return [.. args];
    }
}

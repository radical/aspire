// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using Xunit;

namespace NativeForwarding.Validation;

public sealed class NativeForwardingTests
{
    [Fact]
    [Trait("Partition", "x&whoami")]
    [Trait("Literal", "$(whoami)")]
    [Trait("Spaced", "two words")]
    public void BuildPropertiesReachTheCompiledTestUnchanged()
    {
        WriteReceipt("native-build-inputs.json");

        Assert.True(OperatingSystem.IsWindows());
        Assert.Equal("$(whoami)", ReadBuildInput("NativeBuildProbe"));
        Assert.Equal("x&whoami", ReadBuildInput("NativeCmdProbe"));
        Assert.Equal("two words", ReadBuildInput("NativeSpaceProbe"));
    }

    [Fact]
    [Trait("Partition", "x&whoami")]
    [Trait("Literal", "$(whoami)")]
    [Trait("Spaced", "two words")]
    public void FilterArgumentsReachTheFinalNativeTestProcessUnchanged()
    {
        WriteReceipt("native-test-argv.json");

        Assert.True(OperatingSystem.IsWindows());
        Assert.Equal(
            ["Partition=x&whoami", "Literal=$(whoami)", "Spaced=two words"],
            ReadFilterTraits());
    }

    private static string? ReadBuildInput(string key)
    {
        return typeof(NativeForwardingTests).Assembly
            .GetCustomAttributes<AssemblyMetadataAttribute>()
            .Single(attribute => attribute.Key == key).Value;
    }

    private static string[] ReadFilterTraits()
    {
        var arguments = Environment.GetCommandLineArgs();
        var values = new List<string>();
        for (var index = 0; index < arguments.Length; index++)
        {
            if (arguments[index] == "--filter-trait")
            {
                Assert.True(index + 1 < arguments.Length, "Missing native filter-trait value.");
                values.Add(arguments[++index]);
            }
        }

        return values.ToArray();
    }

    private static void WriteReceipt(string name)
    {
        var workspace = Environment.GetEnvironmentVariable("GITHUB_WORKSPACE")
            ?? throw new InvalidOperationException("This validation requires a GitHub Windows runner.");
        var results = Path.Combine(workspace, "testresults");
        Directory.CreateDirectory(results);
        var receipt = new
        {
            ProductionHead = "86505c0cd58cc13f08baa1d93b5b8bdea08b78db",
            ValidationHead = Environment.GetEnvironmentVariable("GITHUB_SHA"),
            IsWindows = OperatingSystem.IsWindows(),
            OS = RuntimeInformation.OSDescription,
            Runtime = RuntimeInformation.FrameworkDescription,
            ProcessPath = Environment.ProcessPath,
            Arguments = Environment.GetCommandLineArgs(),
            FilterTraits = ReadFilterTraits(),
            BuildInputs = new Dictionary<string, string?>
            {
                ["NativeBuildProbe"] = ReadBuildInput("NativeBuildProbe"),
                ["NativeCmdProbe"] = ReadBuildInput("NativeCmdProbe"),
                ["NativeSpaceProbe"] = ReadBuildInput("NativeSpaceProbe")
            }
        };
        File.WriteAllText(
            Path.Combine(results, name),
            JsonSerializer.Serialize(receipt, new JsonSerializerOptions { WriteIndented = true }));
    }
}

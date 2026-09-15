// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;

namespace Aspire.Templates.Tests;

public class DotNetCommandTests(ITestOutputHelper testOutput)
{
    [Fact]
    [Trait("category", "basic-build")]
    public async Task ChildDotNetProcessesDoNotInheritParentSdkPaths()
    {
        const string msbuildExtensionsPath = "MSBuildExtensionsPath";
        const string msbuildSdksPath = "MSBuildSDKsPath";
        string? originalExtensionsPath = Environment.GetEnvironmentVariable(msbuildExtensionsPath);
        string? originalSdksPath = Environment.GetEnvironmentVariable(msbuildSdksPath);

        try
        {
            Environment.SetEnvironmentVariable(msbuildExtensionsPath, "parent-sdk");
            Environment.SetEnvironmentVariable(msbuildSdksPath, "parent-sdks");

            using var command = new DotNetCommand(testOutput, useDefaultArgs: false, buildEnv: BuildEnvironment.ForNet10SdkOnly);
            var result = await command.ExecuteAsync("--version");

            result.EnsureSuccessful();
            Assert.False(result.StartInfo.Environment.ContainsKey(msbuildExtensionsPath));
            Assert.False(result.StartInfo.Environment.ContainsKey(msbuildSdksPath));
        }
        finally
        {
            Environment.SetEnvironmentVariable(msbuildExtensionsPath, originalExtensionsPath);
            Environment.SetEnvironmentVariable(msbuildSdksPath, originalSdksPath);
        }
    }
}

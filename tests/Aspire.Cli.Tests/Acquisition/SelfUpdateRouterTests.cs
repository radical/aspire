// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;

namespace Aspire.Cli.Tests.Acquisition;

public class SelfUpdateRouterTests
{
    [Fact]
    public void RunsInProcess_Script_ReturnsTrue()
    {
        Assert.True(SelfUpdateRouter.RunsInProcess(InstallRoute.Script));
    }

    [Theory]
    [InlineData(InstallRoute.Unknown)]
    [InlineData(InstallRoute.Pr)]
    [InlineData(InstallRoute.Winget)]
    [InlineData(InstallRoute.Brew)]
    [InlineData(InstallRoute.DotnetTool)]
    internal void RunsInProcess_NonScriptRoute_ReturnsFalse(InstallRoute route)
    {
        Assert.False(SelfUpdateRouter.RunsInProcess(route));
    }

    [Fact]
    public void RunsInProcess_EnumExhaustiveness_ScriptIsOnlyTrueRoute()
    {
        var allRoutes = Enum.GetValues<InstallRoute>();

        foreach (var route in allRoutes)
        {
            var expected = route == InstallRoute.Script;
            Assert.Equal(expected, SelfUpdateRouter.RunsInProcess(route));
        }
    }
}

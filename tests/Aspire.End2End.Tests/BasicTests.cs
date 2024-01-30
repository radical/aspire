// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;

namespace Aspire.End2End.Tests;

public class BasicTests
{
    private static readonly BuildEnvironment s_buildEnv = new();

    [Fact]
    public void SimpleTemplate()
    {
        Console.WriteLine ($"{s_buildEnv.DotNet}");
        // set up PATH, DOTNET_ROOT - which should be effective even when running from IDE
        // BuildEnvironment kinda stuff can provide that
        // but should be able to figure out the paths by itself
        //

        // then test:
        // 1. dotnet new aspire-starter
        // 2. dotnet run - should run the app
        // 3. connect to the webapp, and try hitting other APIs to check that everything is running

    }

}

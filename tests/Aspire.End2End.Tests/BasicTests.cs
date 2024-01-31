// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;
using Xunit.Abstractions;

namespace Aspire.End2End.Tests;

public class BasicTests : End2EndTestBase
{
    public BasicTests(ITestOutputHelper output) : base(output)
    {
    }

    [Fact]
    public async Task SimpleTemplate()
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

        // FIXME: temp path
        string id = GetRandomId();
        InitPaths(id);
        InitProjectDir(_projectDir);

        //string projectDir = Path.Combine(BuildEnvironment.TmpPath, Path.GetRandomFileName());
        //Directory.CreateDirectory(projectDir);
        var res = await new DotNetCommand(s_buildEnv, TestOutput)
                            .WithWorkingDirectory(_projectDir)
                            .ExecuteAsync("new", "aspire-starter");
        res.EnsureSuccessful();
        TestOutput.WriteLine(res.Output);

        res = await new DotNetCommand(s_buildEnv, TestOutput)
                            .WithWorkingDirectory(_projectDir)
                            .ExecuteAsync("build", $"-bl:{Path.Combine(_logPath, "build.binlog")}");
        res.EnsureSuccessful();
        TestOutput.WriteLine(res.Output);
        

        await new RunCommand(s_buildEnv, TestOutput)
                        .WithWorkingDirectory(Path.Combine(_projectDir, $"{id}.AppHost"))
                        .WithTimeout(TimeSpan.FromMinutes(1))
                        .ExecuteAsync("run", "-v", "n");
    }
}

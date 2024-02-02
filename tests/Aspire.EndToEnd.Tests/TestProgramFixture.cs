// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text;
using System.Text.Json;
using Xunit;
using Aspire.End2End.Tests;

namespace Aspire.EndToEnd.Tests;

/// <summary>
/// This fixture ensures the TestProgram application is started before a test is executed.
/// </summary>
public abstract class TestProgramFixture : IAsyncLifetime
{
    private Dictionary<string, ProjectInfo>? _projects;
    private ToolCommand? _runCommand;

    public Dictionary<string, ProjectInfo> Projects => _projects!;

    public HttpClient HttpClient { get; } = new HttpClient();

    // FIXME: either make BE a singleton, or make it static
    public BuildEnvironment BuildEnvironment { get; } = new();

    public async Task InitializeAsync()
    {
        Console.WriteLine ($"-- InitializeAsync direct to Console.WriteLine ");
        var appHostDirectory = Path.Combine(BuildEnvironment.TestProjectPath, "TestProject.AppHost");

        await BuildAppHostAsync(appHostDirectory);

        var appRunning = new TaskCompletionSource();
        var projectsParsed = new TaskCompletionSource();

        var _testOutput = new TestOutputWrapper(null);
        var timeout = TimeSpan.FromMinutes(5);
        _runCommand = new RunCommand(BuildEnvironment, _testOutput, "app-run")
                        .WithWorkingDirectory(appHostDirectory)
                        .WithOutputDataReceived(data =>
                        {
                            if (data?.StartsWith("$ENDPOINTS: ") == true)
                            {
                                _projects = ParseProjectInfo(data.Substring("$ENDPOINTS: ".Length));
                                projectsParsed.SetResult();
                            }

                            if (data?.Contains("Distributed application started") == true)
                            {
                                appRunning.SetResult();
                            }
                        })
                        .WithTimeout(TimeSpan.FromMinutes(5));

        CancellationTokenSource cts = new CancellationTokenSource();
        // FIXME: also watch for run command exiting or failing
        var cmdTask = _runCommand.ExecuteAsync("run --no-build");
        _ = cmdTask.ContinueWith(cmdTask =>
        {
            if (cmdTask.IsFaulted)
            {
                appRunning.SetException(cmdTask.Exception!);
                projectsParsed.SetException(cmdTask.Exception!);
            }
            else if (cmdTask.IsCanceled)
            {
                appRunning.SetCanceled();
                projectsParsed.SetCanceled();
            }
        }, TaskScheduler.Default);

        await Task.WhenAll(
            appRunning.Task.WaitAsync(timeout),
            projectsParsed.Task.WaitAsync(timeout));
    }

    private static Dictionary<string, ProjectInfo> ParseProjectInfo(string json) =>
        JsonSerializer.Deserialize<Dictionary<string, ProjectInfo>>(json)!;

    private async Task BuildAppHostAsync(string appHostDirectory)
    {
        var output = new StringBuilder();
        var outputDone = new ManualResetEvent(false);

        var _testOutput = new TestOutputWrapper(null);
        _testOutput.WriteLine($"-- BuildAppHostAsync via wrapper");
        var res = await new DotNetCommand(BuildEnvironment, _testOutput, "build")
                        .WithWorkingDirectory(appHostDirectory)
                        .WithOutputDataReceived(data =>
                        {
                            if (data == null)
                            {
                                outputDone.Set();
                            }
                            else
                            {
                                output.AppendLine(data);
                            }
                        })
                        .WithTimeout(TimeSpan.FromMilliseconds(180_000))
                        .ExecuteAsync("build");

        res.EnsureSuccessful();
//        Assert.True(buildProcess.WaitForExit(milliseconds: 180_000), "dotnet build command timed out after 3 minutes.");
        //Assert.True(buildProcess.ExitCode == 0, $"Build failed: {Environment.NewLine}{output}");

        //Assert.True(outputDone.WaitOne(millisecondsTimeout: 60_000), "Timed out waiting for output to complete.");
    }

    public async Task DisposeAsync()
    {
        Console.WriteLine ($"TestProgramFixture.DisposeAsync");
        _runCommand?.Dispose();
        await Task.CompletedTask;
    }
}

public sealed class ProjectInfo
{
    public EndpointInfo[] Endpoints { get; set; } = default!;

    /// <summary>
    /// Sends a GET request to the specified resource and returns the response message.
    /// </summary>
    public Task<HttpResponseMessage> HttpGetAsync(HttpClient client, string bindingName, string path, CancellationToken cancellationToken)
    {
        var allocatedEndpoint = Endpoints.Single(e => e.Name == bindingName);
        var url = $"{allocatedEndpoint.Uri}{path}";

        return client.GetAsync(url, cancellationToken);
    }

    /// <summary>
    /// Sends a GET request to the specified resource and returns the response body as a string.
    /// </summary>
    public Task<string> HttpGetStringAsync(HttpClient client, string bindingName, string path, CancellationToken cancellationToken)
    {
        var allocatedEndpoint = Endpoints.Single(e => e.Name == bindingName);
        var url = $"{allocatedEndpoint.Uri}{path}";

        return client.GetStringAsync(url, cancellationToken);
    }

    public async Task WaitForHealthyStatusAsync(HttpClient client, string bindingName, CancellationToken cancellationToken)
    {
        while (true)
        {
            try
            {
                await HttpGetStringAsync(client, bindingName, "/health", cancellationToken);
                return;
            }
            catch
            {
                await Task.Delay(100, cancellationToken);
            }
        }
    }
}

public record EndpointInfo(string Name, string Uri);

/// <summary>
/// TestProgram with integration services but no dashboard or node app.
/// </summary>
/// <remarks>
/// Use <c>[Collection("IntegrationServices")]</c> to inject this fixture in test constructors.
/// </remarks>
public class IntegrationServicesFixture : TestProgramFixture
{
    public ProjectInfo IntegrationServiceA => Projects["integrationservicea"];

}

[CollectionDefinition("IntegrationServices")]
public class IntegrationServicesCollection : ICollectionFixture<IntegrationServicesFixture>
{
    // This class has no code, and is never created. Its purpose is simply
    // to be the place to apply [CollectionDefinition] and all the
    // ICollectionFixture<> interfaces.
}

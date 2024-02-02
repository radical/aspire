// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Aspire.EndToEnd.Tests;

/// <summary>
/// This fixture ensures the TestProgram application is started before a test is executed.
/// </summary>
public abstract class TestProgramFixture : IAsyncLifetime
{
    private Process? _appHostProcess;
    private Dictionary<string, ProjectInfo>? _projects;

    public Dictionary<string, ProjectInfo> Projects => _projects!;

    public HttpClient HttpClient { get; } = new HttpClient();

    public async Task InitializeAsync()
    {
        var appHostDirectory = Path.Combine(GetRepoRoot(), "tests", "testproject", "TestProject.AppHost");

        BuildAppHost(appHostDirectory);

        var appRunning = new TaskCompletionSource();
        var projectsParsed = new TaskCompletionSource();
        _appHostProcess = new Process();
        _appHostProcess.StartInfo = new ProcessStartInfo("dotnet", "run --no-build")
        {
            RedirectStandardOutput = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = appHostDirectory
        };
        _appHostProcess.OutputDataReceived += (sender, e) =>
        {
            if (e.Data?.StartsWith("$ENDPOINTS: ") == true)
            {
                _projects = ParseProjectInfo(e.Data.Substring("$ENDPOINTS: ".Length));
                projectsParsed.SetResult();
            }

            if (e.Data?.Contains("Distributed application started") == true)
            {
                appRunning.SetResult();
            }
        };
        _appHostProcess.Start();
        _appHostProcess.BeginOutputReadLine();

        var timeout = TimeSpan.FromMinutes(5);
        await Task.WhenAll(
            appRunning.Task.WaitAsync(timeout),
            projectsParsed.Task.WaitAsync(timeout));
    }

    private static Dictionary<string, ProjectInfo> ParseProjectInfo(string json) =>
        JsonSerializer.Deserialize<Dictionary<string, ProjectInfo>>(json)!;

    private static void BuildAppHost(string appHostDirectory)
    {
        var output = new StringBuilder();
        var outputDone = new ManualResetEvent(false);
        var buildProcess = new Process();
        buildProcess.StartInfo = new ProcessStartInfo("dotnet", "build")
        {
            RedirectStandardOutput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = appHostDirectory
        };
        buildProcess.OutputDataReceived += (sender, e) =>
        {
            if (e.Data == null)
            {
                outputDone.Set();
            }
            else
            {
                output.AppendLine(e.Data);
            }
        };
        buildProcess.Start();
        buildProcess.BeginOutputReadLine();

        Assert.True(buildProcess.WaitForExit(milliseconds: 180_000), "dotnet build command timed out after 3 minutes.");
        Assert.True(buildProcess.ExitCode == 0, $"Build failed: {Environment.NewLine}{output}");

        Assert.True(outputDone.WaitOne(millisecondsTimeout: 60_000), "Timed out waiting for output to complete.");
    }

    public async Task DisposeAsync()
    {
        if (_appHostProcess is not null)
        {
            _appHostProcess.StandardInput.WriteLine("Stop");
            await _appHostProcess.WaitForExitAsync();
        }
    }

    private static string GetRepoRoot()
    {
        var directory = AppContext.BaseDirectory;

        while (directory != null && !Directory.Exists(Path.Combine(directory, ".git")))
        {
            directory = Directory.GetParent(directory)!.FullName;
        }

        return directory!;
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

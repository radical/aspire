// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Xunit;

namespace Aspire.EndToEnd.Tests;

/// <summary>
/// This fixture ensures the TestProject.AppHost application is started before a test is executed.
///
/// Represents the the IntegrationServiceA project in the test application used to send HTTP requests
/// to the project's endpoints.
/// </summary>
public sealed class IntegrationServicesFixture : IAsyncLifetime
{
    private Process? _appHostProcess;
    private Dictionary<string, ProjectInfo>? _projects;

    public Dictionary<string, ProjectInfo> Projects => _projects!;

    public BuildEnvironment BuildEnvironment { get; } = new();
    public ProjectInfo IntegrationServiceA => Projects["integrationservicea"];

    public async Task InitializeAsync()
    {
        var appHostDirectory = Path.Combine(BuildEnvironment.TestProjectPath, "TestProject.AppHost");

        var testOutput = new TestOutputWrapper(null);
        var output = new StringBuilder();
        var appExited = new TaskCompletionSource();
        var projectsParsed = new TaskCompletionSource();
        var appRunning = new TaskCompletionSource();
        var stdoutComplete = new TaskCompletionSource();
        var stderrComplete = new TaskCompletionSource();
        _appHostProcess = new Process();
        _appHostProcess.StartInfo = new ProcessStartInfo(BuildEnvironment.DotNet, "run -- --disable-dashboard")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            CreateNoWindow = true,
            WorkingDirectory = appHostDirectory
        };
        _appHostProcess.OutputDataReceived += (sender, e) =>
        {
            if (e.Data is null)
            {
                stdoutComplete.SetResult();
                return;
            }

            output.AppendLine(e.Data);
            testOutput.WriteLine($"[apphost] {e.Data}");

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
        _appHostProcess.ErrorDataReceived += (sender, e) =>
        {
            if (e.Data is null)
            {
                stderrComplete.SetResult();
                return;
            }

            output.AppendLine(e.Data);
            testOutput.WriteLine($"[apphost] {e.Data}");
        };

        EventHandler appExitedCallback = (sender, e) => appExited.SetResult();
        _ = appExited.Task.ContinueWith(cmdTask =>
        {
            Console.WriteLine($"cmdTask.continueWith, status: {cmdTask.Status}");
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
            else
            {
                //var res = cmdTask.Result;
                appRunning.SetException(new ArgumentException($"dotnet run exited: {output}"));
                projectsParsed.SetException(new ArgumentException($"dotnet run exited: {output}"));
            }
        }, TaskScheduler.Default);
        _appHostProcess.EnableRaisingEvents = true;
        _appHostProcess.Exited += appExitedCallback;

        testOutput.WriteLine($"Starting the process");
        _appHostProcess.Start();
        _appHostProcess.BeginOutputReadLine();
        _appHostProcess.BeginErrorReadLine();

        var successfulTask = Task.WhenAll(appRunning.Task, projectsParsed.Task);
        var failedTask = appExited.Task;
        var timeoutTask = Task.Delay(TimeSpan.FromMinutes(2));

        testOutput.WriteLine($"- whenany..");
        var resultTask = await Task.WhenAny(successfulTask, failedTask, timeoutTask);
        testOutput.WriteLine($"- whenany.. awake");
        if (resultTask == failedTask)
        {
            // wait for all the output to be read
            var allOutputComplete = Task.WhenAll(stdoutComplete.Task, stderrComplete.Task);
            await Task.WhenAny(allOutputComplete, timeoutTask);
        }
        Assert.True(resultTask == successfulTask, $"App run failed: {Environment.NewLine}{output}");

        _appHostProcess.Exited -= appExitedCallback;

        var client = CreateHttpClient();
        foreach (var project in Projects.Values)
        {
            project.Client = client;
        }
    }

    private static HttpClient CreateHttpClient()
    {
        var services = new ServiceCollection();
        services.AddHttpClient()
            .ConfigureHttpClientDefaults(b =>
            {
                b.UseSocketsHttpHandler((handler, sp) =>
                {
                    handler.PooledConnectionLifetime = TimeSpan.FromSeconds(5);
                    handler.ConnectTimeout = TimeSpan.FromSeconds(5);
                });

                // Ensure transient errors are retried for up to 5 minutes
                b.AddStandardResilienceHandler(options =>
                {
                    options.AttemptTimeout.Timeout = TimeSpan.FromMinutes(1);
                    options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(2); // needs to be at least double the AttemptTimeout to pass options validation
                    options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(5);
                });
            });

        return services.BuildServiceProvider().GetRequiredService<IHttpClientFactory>().CreateClient();
    }

    private static Dictionary<string, ProjectInfo> ParseProjectInfo(string json) =>
        JsonSerializer.Deserialize<Dictionary<string, ProjectInfo>>(json)!;

    public async Task DisposeAsync()
    {
        if (_appHostProcess is not null)
        {
            if (!_appHostProcess.HasExited)
            {
                _appHostProcess.StandardInput.WriteLine("Stop");
            }
            await _appHostProcess.WaitForExitAsync();
        }
    }
}

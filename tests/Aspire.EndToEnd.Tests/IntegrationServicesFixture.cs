// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Xunit;
using Xunit.Abstractions;

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
    private readonly IMessageSink _diagnosticMessageSink;
    private TestOutputWrapper? _testOutput;

    public IntegrationServicesFixture(IMessageSink messageSink)
    {
        _diagnosticMessageSink = messageSink;
    }

    public async Task InitializeAsync()
    {
        var appHostDirectory = Path.Combine(BuildEnvironment.TestProjectPath, "TestProject.AppHost");

        _testOutput = new TestOutputWrapper(null, _diagnosticMessageSink);
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
        foreach (var item in BuildEnvironment.EnvVars)
        {
            _testOutput.WriteLine($"\t[{item.Key}] = {item.Value}");
            _appHostProcess.StartInfo.Environment[item.Key] = item.Value;
        }
        _testOutput.WriteLine($"Starting the process: {BuildEnvironment.DotNet} run -v n -- --disable-dashboard in {_appHostProcess.StartInfo.WorkingDirectory}");
        _appHostProcess.OutputDataReceived += (sender, e) =>
        {
            if (e.Data is null)
            {
                stdoutComplete.SetResult();
                return;
            }

            output.AppendLine(e.Data);
            _testOutput.WriteLine($"[{DateTime.Now}][apphost] {e.Data}");

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
            _testOutput.WriteLine($"[{DateTime.Now}][apphost] {e.Data}");
        };

        //_ = appExited.Task.ContinueWith(cmdTask =>
        //{
            //Console.WriteLine($"cmdTask.continueWith, status: {cmdTask.Status}");
            //if (cmdTask.IsFaulted)
            //{
                //appRunning.SetException(cmdTask.Exception!);
                //projectsParsed.SetException(cmdTask.Exception!);
            //}
            //else if (cmdTask.IsCanceled)
            //{
                //appRunning.SetCanceled();
                //projectsParsed.SetCanceled();
            //}
            //else
            //{
                ////var res = cmdTask.Result;
                //appRunning.SetException(new ArgumentException($"dotnet run exited: {output}"));
                //projectsParsed.SetException(new ArgumentException($"dotnet run exited: {output}"));
            //}
        //}, TaskScheduler.Default);
        EventHandler appExitedCallback = (sender, e) =>
        {
            _testOutput.WriteLine($"[{DateTime.Now}] ");
            _testOutput.WriteLine($"[{DateTime.Now}] ----------- app has exited -------------");
            _testOutput.WriteLine($"[{DateTime.Now}] ");
            appExited.SetResult();
        };
        _appHostProcess.EnableRaisingEvents = true;
        _appHostProcess.Exited += appExitedCallback;

        _appHostProcess.EnableRaisingEvents = true;

        _appHostProcess.Start();
        _appHostProcess.BeginOutputReadLine();
        _appHostProcess.BeginErrorReadLine();

        var successfulTask = Task.WhenAll(appRunning.Task, projectsParsed.Task);
        var failedAppTask = appExited.Task;
        var timeoutTask = Task.Delay(TimeSpan.FromMinutes(5));

        var resultTask = await Task.WhenAny(successfulTask, failedAppTask, timeoutTask);
        if (resultTask == failedAppTask)
        {
            _testOutput.WriteLine($"resultTask == failedAppTask");
            // wait for all the output to be read
            var allOutputComplete = Task.WhenAll(stdoutComplete.Task, stderrComplete.Task);
            var appExitTimeout = Task.Delay(TimeSpan.FromSeconds(5));
            var t = await Task.WhenAny(allOutputComplete, appExitTimeout);
            if (t == appExitTimeout)
            {
                _testOutput.WriteLine($"\tand timed out waiting for the full output");
            }
            else
            {
                _testOutput.WriteLine($"\tall output completed");
            }

            string outputMessage = output.ToString();
            string exceptionMessage = $"App run failed: {Environment.NewLine}{outputMessage}";
            if (outputMessage.Contains("docker was found but appears to be unhealthy", StringComparison.OrdinalIgnoreCase))
            {
                exceptionMessage = "Docker was found but appears to be unhealthy. " + exceptionMessage;
            }
            // should really fail and quit after this
            throw new ArgumentException(exceptionMessage);
        }
        Assert.True(resultTask == successfulTask, $"App run failed: {Environment.NewLine}{output}");

        // FIXME: don't remove this.. fail the whole thing is the app exits early!
        //_appHostProcess.Exited -= appExitedCallback;

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
                b.ConfigureHttpClient(client =>
                {
                    // Disable the HttpClient timeout to allow the timeout strategies to control the timeout.
                    client.Timeout = Timeout.InfiniteTimeSpan;
                });

                b.UseSocketsHttpHandler((handler, sp) =>
                {
                    handler.PooledConnectionLifetime = TimeSpan.FromSeconds(5);
                    handler.ConnectTimeout = TimeSpan.FromSeconds(5);
                });

                // Ensure transient errors are retried for up to 5 minutes
                b.AddStandardResilienceHandler(options =>
                {
                    options.AttemptTimeout.Timeout = TimeSpan.FromMinutes(2);
                    options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(5); // needs to be at least double the AttemptTimeout to pass options validation
                    options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(10);
                    options.Retry.OnRetry = async (args) =>
                    {
                        Console.WriteLine($"IntegrationServicesFixture: ################## [{DateTime.Now}] Retry {args.AttemptNumber+1} due to outcome: {args.Outcome} {args.Outcome.Exception}");
                        await Task.CompletedTask;
                    };
                    options.Retry.MaxRetryAttempts = 20;
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
            var cts = new CancellationTokenSource();
            //cts.CancelAfter(TimeSpan.FromMinutes(1));

            using var cmd = new ToolCommand("docker", _testOutput!, "list-all");
            (await cmd.ExecuteAsync(cts.Token, $"container list --all"))
                .EnsureSuccessful();

            if (!_appHostProcess.HasExited)
            {
                _appHostProcess.StandardInput.WriteLine("Stop");
            }
            await _appHostProcess.WaitForExitAsync();
        }
    }
}

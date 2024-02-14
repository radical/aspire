// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Aspire.V1;
using Grpc.Core;
using Grpc.Net.Client;
using Grpc.Net.Client.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
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
    private GrpcChannel? _channel;
    private readonly ILoggerFactory _loggerFactory;
    private DashboardService.DashboardServiceClient? _client;
    private readonly CancellationTokenSource _cts = new();
    private readonly Regex _resourceUriRegex = new("^resource uri: (?<resourceUri>.*)");

    public IntegrationServicesFixture(IMessageSink messageSink)
    {
        _diagnosticMessageSink = messageSink;
        _loggerFactory = LoggerFactory.Create(builder =>
        {
            builder.AddSimpleConsole(options =>
            {
                options.SingleLine = true;
                options.TimestampFormat = "[hh:mm:ss] ";
            });
        });
    }

    public async Task InitializeAsync()
    {
        var appHostDirectory = Path.Combine(BuildEnvironment.TestProjectPath, "TestProject.AppHost");

        var testOutput = new TestOutputWrapper(null, _diagnosticMessageSink);
        var output = new StringBuilder();
        var appExited = new TaskCompletionSource();
        var projectsParsed = new TaskCompletionSource();
        var appRunning = new TaskCompletionSource();
        var resourceUriReceived = new TaskCompletionSource<string>();
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
            testOutput.WriteLine($"\t[{item.Key}] = {item.Value}");
            _appHostProcess.StartInfo.Environment[item.Key] = item.Value;
        }
        testOutput.WriteLine($"Starting the process: {BuildEnvironment.DotNet} run -v n -- --disable-dashboard in {_appHostProcess.StartInfo.WorkingDirectory}");
        _appHostProcess.OutputDataReceived += (sender, e) =>
        {
            if (e.Data is null)
            {
                stdoutComplete.SetResult();
                return;
            }

            output.AppendLine(e.Data);
            testOutput.WriteLine($"[{DateTime.Now}][apphost] {e.Data}");

            if (e.Data?.StartsWith("$ENDPOINTS: ") == true)
            {
                _projects = ParseProjectInfo(e.Data.Substring("$ENDPOINTS: ".Length));
                projectsParsed.SetResult();
            }

            if (e.Data?.Contains("Distributed application started") == true)
            {
                appRunning.SetResult();
            }

            if (e.Data is not null && _resourceUriRegex.Match(e.Data) is Match match && match.Success)
            {
                testOutput.WriteLine($"ResourceUri: {match.Groups["resourceUri"].Value}");
                resourceUriReceived.SetResult(match.Groups["resourceUri"].Value);
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
            testOutput.WriteLine($"[{DateTime.Now}][apphost] {e.Data}");
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
        EventHandler appExitedCallback = (sender, e) => appExited.SetResult();
        _appHostProcess.EnableRaisingEvents = true;
        _appHostProcess.Exited += appExitedCallback;

        _appHostProcess.EnableRaisingEvents = true;

        _appHostProcess.Start();
        _appHostProcess.BeginOutputReadLine();
        _appHostProcess.BeginErrorReadLine();

        var successfulTask = Task.WhenAll(appRunning.Task, projectsParsed.Task, resourceUriReceived.Task);
        var failedTask = appExited.Task;
        var timeoutTask = Task.Delay(TimeSpan.FromMinutes(5));

        var resultTask = await Task.WhenAny(successfulTask, failedTask, timeoutTask);
        if (resultTask == failedTask)
        {
            testOutput.WriteLine($"resultTask == failedTask");
            // wait for all the output to be read
            var allOutputComplete = Task.WhenAll(stdoutComplete.Task, stderrComplete.Task);
            var appExitTimeout = Task.Delay(TimeSpan.FromSeconds(5));
            var t = await Task.WhenAny(allOutputComplete, appExitTimeout);
            if (t == appExitTimeout)
            {
                testOutput.WriteLine($"\tand timed out waiting for the full output");
            }
            else
            {
                testOutput.WriteLine($"\tall output completed");
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
        _appHostProcess.Exited -= appExitedCallback;

        var client = CreateHttpClient();
        foreach (var project in Projects.Values)
        {
            project.Client = client;
        }

        _ = Task.Run(() => ExecuteAsync(resourceUriReceived.Task.Result, _cts.Token))
                .ContinueWith(t =>
                {
                    if (t.IsFaulted)
                    {
                        testOutput.WriteLine($"ExecuteAsync faulted: {t.Exception}");
                    }
                    else if (t.IsCanceled)
                    {
                        testOutput.WriteLine($"ExecuteAsync canceled");
                    }
                    else
                    {
                        testOutput.WriteLine($"ExecuteAsync completed");
                    }
                }, TaskScheduler.Default);
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
                    options.AttemptTimeout.Timeout = TimeSpan.FromMinutes(1);
                    options.CircuitBreaker.SamplingDuration = TimeSpan.FromMinutes(2); // needs to be at least double the AttemptTimeout to pass options validation
                    options.TotalRequestTimeout.Timeout = TimeSpan.FromMinutes(10);
                });
            });

        return services.BuildServiceProvider().GetRequiredService<IHttpClientFactory>().CreateClient();
    }

    private async Task ExecuteAsync(string uri, CancellationToken cancellationToken)
    {
        Console.WriteLine($"-- DashboardService startasync with {uri}");
        //var address = new Uri(await _dashboardEndpointProvider.GetResourceServiceUriAsync(cancellationToken));
        var address = new Uri(uri);// """configuration.GetUri(ResourceServiceUrlVariableName);

        _channel = CreateChannel(address);
        //_client = new DashboardServiceForTests.DashboardServiceForTestsClient(_channel);
        _client = new DashboardService.DashboardServiceClient(_channel);

        GrpcChannel CreateChannel(Uri address)
        {
            var httpHandler = new SocketsHttpHandler
            {
                EnableMultipleHttp2Connections = true,
                KeepAlivePingDelay = TimeSpan.FromSeconds(20),
                KeepAlivePingTimeout = TimeSpan.FromSeconds(10),
                KeepAlivePingPolicy = HttpKeepAlivePingPolicy.WithActiveRequests
            };

            // https://learn.microsoft.com/aspnet/core/grpc/retries

            var methodConfig = new MethodConfig
            {
                Names = { MethodName.Default },
                RetryPolicy = new RetryPolicy
                {
                    MaxAttempts = 5,
                    InitialBackoff = TimeSpan.FromSeconds(1),
                    MaxBackoff = TimeSpan.FromSeconds(5),
                    BackoffMultiplier = 1.5,
                    RetryableStatusCodes = { StatusCode.Unavailable }
                }
            };

            // https://learn.microsoft.com/aspnet/core/grpc/diagnostics#grpc-client-logging

            return GrpcChannel.ForAddress(
                address,
                channelOptions: new()
                {
                    HttpHandler = httpHandler,
                    ServiceConfig = new() { MethodConfigs = { methodConfig } },
                    LoggerFactory = _loggerFactory,
                    ThrowOperationCanceledOnCancellation = true
                });
        }
        //await base.StartAsync(cancellationToken);

        await foreach (var x in SubscribeConsoleLogs("integrationservicea", cancellationToken)!)
        {
            Console.WriteLine($"-- {x}");
        }
    }

    async IAsyncEnumerable<IReadOnlyList<(string Content, bool IsErrorMessage)>>? SubscribeConsoleLogs(string resourceName, [EnumeratorCancellation] CancellationToken cancellationToken)
    {
        //EnsureInitialized();

        using var combinedTokens = CancellationTokenSource.CreateLinkedTokenSource(_cts.Token, cancellationToken);

        var call = _client!.WatchResourceConsoleLogs(
            new WatchResourceConsoleLogsRequest() { ResourceName = resourceName },
            cancellationToken: combinedTokens.Token);

        await foreach (var response in call.ResponseStream.ReadAllAsync(cancellationToken: combinedTokens.Token))
        {
            var logLines = new (string Content, bool IsErrorMessage)[response.LogLines.Count];

            for (var i = 0; i < logLines.Length; i++)
            {
                logLines[i] = (response.LogLines[i].Text, response.LogLines[i].IsStdErr);
            }

            yield return logLines;
        }
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

using Grpc.Net.Client;
//using Aspire.V1;
using Grpc.Net.Client.Configuration;
using Grpc.Core;
//using Grpc.Net.Client;
using Microsoft.Extensions.Logging;
//using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Hosting;
using Aspire.Hosting.Dcp;
using Aspire.Tests.Client;

internal sealed class DashboardClientHostedService : BackgroundService
{
    //private const string ResourceServiceUrlVariableName = "DOTNET_RESOURCE_SERVICE_ENDPOINT_URL";
    private GrpcChannel? _channel;
    private DashboardServiceForTests.DashboardServiceForTestsClient? _client;
    private readonly ILoggerFactory _loggerFactory;
    private readonly ILogger<DashboardClientHostedService> _logger;
    private readonly IDashboardEndpointProvider _dashboardEndpointProvider;

    public DashboardClientHostedService(ILoggerFactory loggerFactory, IDashboardEndpointProvider dashboardEndpointProvider)
	{
        _loggerFactory = loggerFactory;
        _logger = loggerFactory.CreateLogger<DashboardClientHostedService>();
        _dashboardEndpointProvider = dashboardEndpointProvider;

	}

    public override async Task StartAsync(CancellationToken cancellationToken)
    {
        Console.WriteLine($"-- DashboardService startasync");
        var address = new Uri(await _dashboardEndpointProvider.GetResourceServiceUriAsync(cancellationToken));
        //var address = new Uri("");// """configuration.GetUri(ResourceServiceUrlVariableName);
        _channel = CreateChannel(address);
        _client = new DashboardServiceForTests.DashboardServiceForTestsClient(_channel);

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
       await base.StartAsync(cancellationToken);
    }

    public override Task StopAsync(CancellationToken cancellationToken)
    {
        Console.WriteLine($"-- DashboardService stopasync");
        return base.StopAsync(cancellationToken);
    }   

    //protected override Task ExecuteAsync(CancellationToken stoppingToken)
    //{
    //    throw new NotImplementedException();
    //}
}

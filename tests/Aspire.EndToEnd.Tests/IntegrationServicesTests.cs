// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using Xunit;
using Xunit.Abstractions;
using Xunit.Sdk;

namespace Aspire.EndToEnd.Tests;

public class IntegrationServicesTests : IClassFixture<IntegrationServicesFixture>, IAsyncLifetime
{
    private readonly IntegrationServicesFixture _integrationServicesFixture;
    //private readonly IMessageSink _diagnosticMessageSink;
    private readonly TestOutputWrapper _testOutput;

    // BUILD_BUILDID is defined by Azure Dev Ops
    public static bool CanRunTestsOnCI { get; } = !RuntimeInformation.IsOSPlatform(OSPlatform.Windows) && Environment.GetEnvironmentVariable("BUILD_BUILDID") == null;

    public IntegrationServicesTests(ITestOutputHelper testOutput, IntegrationServicesFixture integrationServicesFixture)
    {
        _integrationServicesFixture = integrationServicesFixture;
        _testOutput = new TestOutputWrapper(testOutput, null);
        //_diagnosticMessageSink = messageSink;
    }

    [LocalOnlyTheory]
    [InlineData("mongodb")]
    [InlineData("mysql")]
    [InlineData("pomelo")]
    [InlineData("oracledatabase")]
    [InlineData("postgres")]
    [InlineData("rabbitmq")]
    [InlineData("redis")]
    [InlineData("sqlserver")]
    [InlineData("cosmos")]
    public async Task VerifyComponentWorks(string component)
    {
        if (component == "cosmos" && RuntimeInformation.IsOSPlatform(OSPlatform.OSX) && RuntimeInformation.ProcessArchitecture == Architecture.Arm64)
        {
            throw new SkipException("Skipping 'cosmos' test because the emulator isn't supported on macOS ARM64.");
        }

        _integrationServicesFixture.EnsureAppHostRunning();

        _testOutput.WriteLine ($"[{DateTime.Now}] >>>> Starting VerifyComponentWorks for {component} --");
        try
        {
            var response = await _integrationServicesFixture.IntegrationServiceA.HttpGetAsync("http", $"/{component}/verify");
            var responseContent = await response.Content.ReadAsStringAsync();

            Assert.True(response.IsSuccessStatusCode, responseContent);
            _testOutput.WriteLine ($"[{DateTime.Now}] <<<< Done VerifyComponentWorks for {component} --");
        }
        catch
        {
            _testOutput.WriteLine ($"[{DateTime.Now}] <<<< FAILED VerifyComponentWorks for {component} --");

            var cts = new CancellationTokenSource();
            //cts.CancelAfter(TimeSpan.FromMinutes(1));

            string containerName;
            {
                using var cmd = new ToolCommand("docker", _testOutput!);
                var res = (await cmd.ExecuteAsync(cts.Token, $"container list --all --filter name={component} --format {{{{.Names}}}}"))
                    .EnsureSuccessful();
                // _testOutput.WriteLine($"output: {res.Output}");
                containerName = res.Output;
            }

            if (string.IsNullOrEmpty(containerName))
            {
                _testOutput.WriteLine($"No container found for {component}");
            }
            else
            {
                using var cmd2 = new ToolCommand("docker", _testOutput!, label: component);
                (await cmd2.ExecuteAsync(cts.Token, $"container logs {containerName} -n 50"))
                    .EnsureSuccessful();
            }

            await _integrationServicesFixture.DumpDockerInfo();

            throw;
        }
    }

    [LocalOnlyFact]
    public async Task KafkaComponentCanProduceAndConsume()
    {
        _testOutput.WriteLine($"[{DateTime.Now}] >>>> Starting KafkaComponentCanProduceAndConsume --");
        try
        {
            _integrationServicesFixture.EnsureAppHostRunning();

            string topic = $"topic-{Guid.NewGuid()}";

            var response = await _integrationServicesFixture.IntegrationServiceA.HttpGetAsync("http", $"/kafka/produce/{topic}");
            var responseContent = await response.Content.ReadAsStringAsync();
            Assert.True(response.IsSuccessStatusCode, responseContent);

            response = await _integrationServicesFixture.IntegrationServiceA.HttpGetAsync("http", $"/kafka/consume/{topic}");
            responseContent = await response.Content.ReadAsStringAsync();
            Assert.True(response.IsSuccessStatusCode, responseContent);
        }
        catch
        {
            _testOutput.WriteLine($"[{DateTime.Now}] <<<< FAILED KafkaComponentCanProduceAndConsume --");
            throw;
        }
    }

    [LocalOnlyFact]
    public async Task VerifyHealthyOnIntegrationServiceA()
    {
        _testOutput.WriteLine ($"[{DateTime.Now}] >>>> Starting VerifyHealthyOnIntegrationServiceA --");

        try
        {
            _integrationServicesFixture.EnsureAppHostRunning();

            // We wait until timeout for the /health endpoint to return successfully. We assume
            // that components wired up into this project have health checks enabled.
            await _integrationServicesFixture.IntegrationServiceA.WaitForHealthyStatusAsync("http");
        }
        catch
        {
            _testOutput.WriteLine ($"[{DateTime.Now}] <<<< FAILED VerifyHealthyOnIntegrationServiceA --");
            throw;
        }
    }

    public Task InitializeAsync() => _integrationServicesFixture.DumpDockerInfo();

    public Task DisposeAsync() => Task.CompletedTask;
}

// TODO: remove these attributes when the above tests are running in CI

public class LocalOnlyFactAttribute : FactAttribute
{
    public override string Skip => IntegrationServicesTests.CanRunTestsOnCI
                                    ? null!
                                    : $"{nameof(LocalOnlyFactAttribute)} tests are not run as part of CI.";
}

public class LocalOnlyTheoryAttribute : TheoryAttribute
{
    public override string Skip => IntegrationServicesTests.CanRunTestsOnCI
                                    ? null!
                                    : $"{nameof(LocalOnlyTheoryAttribute)} tests are not run as part of CI.";
}

public static class TestComponents
{
    public static string Cosmos => "cosmos";
    public static string Mongodb => "mongodb";
    public static string Mysql => "mysql";
    public static string Pomelo => "pomelo";
    public static string Oracledatabase => "oracledatabase";
    public static string Postgres => "postgres";
    public static string Rabbitmq => "rabbitmq";
    public static string Redis => "redis";
    public static string Sqlserver => "sqlserver";
    public static string Kafka => "kafka";

}

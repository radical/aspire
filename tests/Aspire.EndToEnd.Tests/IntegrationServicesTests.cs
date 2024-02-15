// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Xunit;
using Xunit.Abstractions;

namespace Aspire.EndToEnd.Tests;

public class IntegrationServicesTests : IClassFixture<IntegrationServicesFixture>
{
    private readonly IntegrationServicesFixture _integrationServicesFixture;
    //private readonly IMessageSink _diagnosticMessageSink;
    private readonly TestOutputWrapper _testOutput;

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
        _integrationServicesFixture.EnsureAppHostRunning();

        _testOutput.WriteLine ($"[{DateTime.Now}] >>>> Starting VerifyComponentWorks for {component} --");
        try
        {
            var response = await _integrationServicesFixture.IntegrationServiceA.HttpGetAsync("http", $"/{component}/verify");
            var responseContent = await response.Content.ReadAsStringAsync();

            _testOutput.WriteLine($"response for {component}: {responseContent}");
            Assert.True(response.IsSuccessStatusCode, responseContent);
            _testOutput.WriteLine ($"[{DateTime.Now}] <<<< Done VerifyComponentWorks for {component} --");
        } catch
        {
            _testOutput.WriteLine ($"[{DateTime.Now}] <<<< FAILED VerifyComponentWorks for {component} --");

            var cts = new CancellationTokenSource();
            //cts.CancelAfter(TimeSpan.FromMinutes(1));

            using var cmd3 = new ToolCommand("docker", _testOutput!, "list-all");
            (await cmd3.ExecuteAsync(cts.Token, $"container list --all"))
                .EnsureSuccessful();

            using var cmd = new ToolCommand("docker", _testOutput!);
            var res = (await cmd.ExecuteAsync(cts.Token, $"container list --all --filter name={component} --format {{{{.Names}}}}"))
                .EnsureSuccessful();
            _testOutput.WriteLine($"output: {res.Output}");

            if (string.IsNullOrEmpty(res.Output))
            {
                _testOutput.WriteLine($"No container found for {component}");
                using var cmd4 = new ToolCommand("docker", _testOutput!, "list-all");
                (await cmd4.ExecuteAsync(cts.Token, $"image ls"))
                    .EnsureSuccessful();
            }
            else
            {
                using var cmd2 = new ToolCommand("docker", _testOutput!, label: component);
                (await cmd2.ExecuteAsync(cts.Token, $"container logs {res.Output}"))
                        .EnsureSuccessful();
            }

            throw;
        }
    }

    [LocalOnlyFact]
    public async Task KafkaComponentCanProduceAndConsume()
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

    [LocalOnlyFact]
    public async Task VerifyHealthyOnIntegrationServiceA()
    {
        _integrationServicesFixture.EnsureAppHostRunning();

        // We wait until timeout for the /health endpoint to return successfully. We assume
        // that components wired up into this project have health checks enabled.
        await _integrationServicesFixture.IntegrationServiceA.WaitForHealthyStatusAsync("http");
    }
}

// TODO: remove these attributes when the above tests are running in CI

public class LocalOnlyFactAttribute : FactAttribute
{
    public override string Skip
    {
        get
        {
            // BUILD_BUILDID is defined by Azure Dev Ops

            if (Environment.GetEnvironmentVariable("BUILD_BUILDID") != null)
            {
                return "LocalOnlyFactAttribute tests are not run as part of CI.";
            }

            return null!;
        }
    }
}

public class LocalOnlyTheoryAttribute : TheoryAttribute
{
    public override string Skip
    {
        get
        {
            // BUILD_BUILDID is defined by Azure Dev Ops

            if (Environment.GetEnvironmentVariable("BUILD_BUILDID") != null)
            {
                return "LocalOnlyTheoryAttribute tests are not run as part of CI.";
            }

            return null!;
        }
    }
}

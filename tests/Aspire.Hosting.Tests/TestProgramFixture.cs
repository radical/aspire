// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Hosting.Testing.Tests;
using Aspire.Hosting.Tests.Utils;
using Microsoft.AspNetCore.InternalTesting;

namespace Aspire.Hosting.Tests;

/// <summary>
/// This fixture ensures the TestProgram application is started before a test is executed.
/// </summary>
public abstract class TestProgramFixture : IAsyncLifetime
{
    private DistributedApplication? _app;
    private TestProgram? _testProgram;

    public TestProgram TestProgram => _testProgram ?? throw new InvalidOperationException("TestProgram is not initialized.");

    public DistributedApplication App => _app ?? throw new InvalidOperationException("DistributedApplication is not initialized.");

    public abstract TestProgram CreateTestProgram();

    public abstract Task WaitReadyStateAsync(CancellationToken cancellationToken = default);

    public async ValueTask InitializeAsync()
    {
        _testProgram = CreateTestProgram();

        _app = _testProgram.Build();

        using var startCts = AsyncTestHelpers.CreateDefaultTimeoutTokenSource(TestConstants.LongTimeoutDuration);
        await _app.StartAsync(startCts.Token);

        // Use a separate timeout for readiness checks so that time spent in StartAsync
        // does not reduce the time available for services to become ready.
        using var readyCts = AsyncTestHelpers.CreateDefaultTimeoutTokenSource(TestConstants.LongTimeoutDuration);
        await WaitReadyStateAsync(readyCts.Token);
    }

    public async ValueTask DisposeAsync()
    {
        if (_app != null)
        {
            await _app.StopAsync();
            await _app.DisposeAsync();
        }

        _testProgram?.Dispose();
    }
}

/// <summary>
/// TestProgram with no dashboard, node app or integration services.
/// </summary>
/// <remarks>
/// Use <c>[Collection("SlimTestProgram")]</c> to inject this fixture in test constructors.
/// </remarks>
public class SlimTestProgramFixture : TestProgramFixture
{
    public override TestProgram CreateTestProgram()
    {
        return TestProgram.Create<DistributedApplicationTests>(randomizePorts: false);
    }

    public override async Task WaitReadyStateAsync(CancellationToken cancellationToken = default)
    {
        // Wait for all services in parallel to avoid sequential timeout accumulation.
        await Task.WhenAll(
            WaitForServiceReadyAsync("servicea", TestProgram.ServiceABuilder.Resource.Name, cancellationToken),
            WaitForServiceReadyAsync("serviceb", TestProgram.ServiceBBuilder.Resource.Name, cancellationToken),
            WaitForServiceReadyAsync("servicec", TestProgram.ServiceCBuilder.Resource.Name, cancellationToken));
    }

    private async Task WaitForServiceReadyAsync(string logResourceName, string resourceName, CancellationToken cancellationToken)
    {
        await App.WaitForTextAsync("Application started.", logResourceName, cancellationToken);
        using var client = App.CreateHttpClientWithResilience(resourceName, "http");
        await client.GetStringAsync("/", cancellationToken);
    }
}

[CollectionDefinition("SlimTestProgram")]
public class SlimTestProgramCollection : ICollectionFixture<SlimTestProgramFixture>
{
    // This class has no code, and is never created. Its purpose is simply
    // to be the place to apply [CollectionDefinition] and all the
    // ICollectionFixture<> interfaces.
}

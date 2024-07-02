// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.EndToEnd.Tests;
using Xunit;
using Xunit.Abstractions;

namespace Aspire.Playground.Tests;

public class NatsTests : PlaygroundTestsBase, IClassFixture<MysqlPlaygroundFixture>
{
    private readonly MysqlPlaygroundFixture _testFixture;

    public NatsTests(MysqlPlaygroundFixture testFixture, ITestOutputHelper testOutput) : base(testOutput)
    {
        _testFixture = testFixture;
    }

    [Theory]
    [InlineData("/health")]
    [InlineData("/alive")]
    public Task ApiServiceIsHealthy(string path)
        => _testFixture.Projects["apiservice"].WaitForHealthyStatusAsync("http", _testOutput, path, CancellationToken.None);

    [Fact]
    [ActiveIssue("https://github.com/dotnet/aspire/issues/4623", typeof(PlaywrightProvider), nameof(PlaywrightProvider.DoesNotHavePlaywrightSupport))]k
    public async Task ResourcesShowUpOnDashboad()
    {
        await using var context = await CreateNewBrowserContextAsync();
        await CheckDashboardHasResourcesAsync(
            await _testFixture.Project!.OpenDashboardPageAsync(context),
            GetExpectedResources(_testFixture.Project),
            timeoutSecs: 1_000);
    }

    private static List<ResourceRow> GetExpectedResources(AspireProject project)
    {
        _ = project;
        List<ResourceRow> expectedResources =
        [
            new ResourceRow(
                Type: "Project",
                Name: "api",
                State: "Running",
                Source: "Mongo.ApiService.csproj",
                Endpoints: ["http://localhost:\\d+"]),

            new ResourceRow(
                Type: "Container",
                Name: "mongo",
                State: "Running",
                Source: $"{MongoDBContainerImageTags.Registry}/{MongoDBContainerImageTags.Image}:{MongoDBContainerImageTags.Tag}",
                Endpoints: ["tcp://localhost:\\d+"]),

            new ResourceRow(
                Type: "Container",
                Name: "mongo-mongoexpress",
                State: "Running",
                Source: $"{MongoDBContainerImageTags.MongoExpressRegistry}/{MongoDBContainerImageTags.MongoExpressImage}:{MongoDBContainerImageTags.MongoExpressTag}",
                Endpoints: ["http://localhost:\\d+"]),

            new ResourceRow(
                Type: "Executable",
                Name: "aspire-dashboard",
                State: "Running",
                Source: null,
                Endpoints: ["None"])
        ];

        return expectedResources;
    }
}

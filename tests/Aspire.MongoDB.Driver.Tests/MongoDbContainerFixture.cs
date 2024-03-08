// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Components.Common.Tests;
// using DotNet.Testcontainers.Builders;
using MongoDB.Bson;
using MongoDB.Driver;
using Testcontainers.MongoDb;
using Xunit;

namespace Aspire.MongoDB.Driver.Tests;

public sealed class MongoDbContainerFixture : IAsyncLifetime
{
    public MongoDbContainer? Container { get; private set; }

    public string GetConnectionString() => Container?.GetConnectionString() ??
        throw new InvalidOperationException("The test container was not initialized.");

    public async Task InitializeAsync()
    {
        if (RequiresDockerTheoryAttribute.IsSupported)
        {
            Container = new MongoDbBuilder()
                            .Build();
            await Container.StartAsync();
            while (true)
            {

                try
                {
                    if (await CheckHealthAsync())
                    {
                        break;
                    }
                }
                catch (System.Exception ex)
                {
                    Console.WriteLine ($"CheckHealthAsync failed: {ex}");

                    throw;
                }
                await Task.Delay(10000);
            }
        }
    }

    public async Task<bool> CheckHealthAsync()
    {
        CancellationTokenSource cts = new();
        cts.CancelAfter(10000);
        Console.WriteLine ($"-- CheckHealthAsync");
        string connectionString = Container!.GetConnectionString();
        Console.WriteLine ($"\t{connectionString}");
        var mongoClient = new MongoClient(connectionString);
        BsonDocumentCommand<BsonDocument> _command = new(BsonDocument.Parse("{ping:1}"));
        try
        {
            // var mongoClient = _mongoClient.GetOrAdd(_mongoClientSettings.ToString(), _ => new MongoClient(_mongoClientSettings));

            var _specifiedDatabase = MongoUrl.Create(connectionString)?.DatabaseName;
            Console.WriteLine ($"\tSpecifiedDatabase: {_specifiedDatabase}");

            if (!string.IsNullOrEmpty(_specifiedDatabase))
            {
                // some users can't list all databases depending on database privileges, with
                // this you can check a specified database.
                // Related with issue #43 and #617

                await mongoClient
                    .GetDatabase(_specifiedDatabase)
                    .RunCommandAsync(_command, cancellationToken: cts.Token)
                    .ConfigureAwait(false);
            }
            else
            {
                using var cursor = await mongoClient.ListDatabaseNamesAsync(cts.Token).ConfigureAwait(false);
                await cursor.FirstOrDefaultAsync(cts.Token).ConfigureAwait(false);
            }

            // return HealthCheckResult.Healthy();
            Console.WriteLine ($"\tHealthy");
            return true;
        }
        catch (Exception ex)
        {
            Console.WriteLine ($"Error: {ex.Message}");
            // throw;
            return false;
            // return new HealthCheckResult(context.Registration.FailureStatus, exception: ex);
        }
    }

    public async Task DisposeAsync()
    {
        if (Container is not null)
        {
            await Container.DisposeAsync();
        }
    }
}

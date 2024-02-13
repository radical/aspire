// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Microsoft.Azure.Cosmos;
using Polly;

public static class CosmosExtensions
{
    public static void MapCosmosApi(this WebApplication app)
    {
        app.MapGet("/cosmos/verify", VerifyCosmosAsync);
    }

    private static async Task<IResult> VerifyCosmosAsync(CosmosClient cosmosClient)
    {
        Console.WriteLine ($"---- [{DateTime.Now}] VerifyCosmosAsync");
        string last = "";
        try
        {
            Polly.Retry.AsyncRetryPolicy corePolicy = Policy
                .Handle<CosmosException>()
                // retry 60 times with a 1 second delay between retries
                .WaitAndRetryAsync(60, retryAttempt => TimeSpan.FromSeconds(1));
            Polly.Timeout.AsyncTimeoutPolicy outerTimeout = Policy.TimeoutAsync(TimeSpan.FromMinutes(4));
            Polly.Wrap.AsyncPolicyWrap policy = outerTimeout.WrapAsync(corePolicy);

            last = "calling CreateDatabaseIfNotExistsAsync";
            Database db = await policy.ExecuteAsync(
                async () => (await cosmosClient.CreateDatabaseIfNotExistsAsync("db")).Database);

            last = "calling CreateContainerIfNotExistsAsync";
            Container container = (await db.CreateContainerIfNotExistsAsync("todos", "/id")).Container;

            var id = Guid.NewGuid().ToString();
            var title = "Do some work.";

            last = "calling container.CreateItemAsync";
            var item = await container.CreateItemAsync(new
            {
                id,
                title
            });

            return item.Resource.id == id ? Results.Ok() : Results.Problem();
        }
        catch (Exception e)
        {
            return Results.Problem(e.ToString() + $"****** LAST: {last}");
        }
    }
}

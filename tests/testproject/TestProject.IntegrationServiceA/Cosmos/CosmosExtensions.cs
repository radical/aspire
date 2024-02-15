// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Microsoft.Azure.Cosmos;
using System.Text;
// using Polly;
using System.Globalization;
using Polly;
using System.Dynamic;

public static class CosmosExtensions
{
    private static int s_counter;
    private static readonly StringBuilder s_sb = new();

    public static void MapCosmosApi(this WebApplication app)
    {
        app.MapGet("/cosmos/verify", VerifyCosmosAsync);
        //app.MapGet("/cosmos/verify", DummyVerifyCosmosAsync);
    }

#if false
    private static async Task<IResult> DummyVerifyCosmosAsync(CosmosClient cosmosClient)
    {
        await Task.Delay(15000);
        return s_counter < 100 ? Results.Problem("failing _counter: " + s_counter++) : Results.Ok();
    }
#endif

    private static async Task<IResult> VerifyCosmosAsync(CosmosClient cosmosClient)
    {
        s_sb.AppendLine(CultureInfo.InvariantCulture, $"---- [{DateTime.Now}] IntegrationServiceA.VerifyCosmosAsync: Let's try to connect now");
        string last = "";
        try
        {
            Polly.Retry.AsyncRetryPolicy policy = Policy
                .Handle<HttpRequestException>()
                // retry 60 times with a 1 second delay between retries
                .WaitAndRetryAsync(60, retryAttempt => TimeSpan.FromSeconds(1));

            last = "calling CreateDatabaseIfNotExistsAsync";
            // Database db = (await cosmosClient.CreateDatabaseIfNotExistsAsync("db")).Database;
            Database db = (await policy.ExecuteAsync(
                async () =>
                {
                    try
                    {
                        return await cosmosClient.CreateDatabaseIfNotExistsAsync("db");
                    }
                    catch (Exception e)
                    {
                        s_sb.AppendLine(CultureInfo.InvariantCulture, $"--- [{DateTime.Now}] IntegrationServiceA.VerifyCosmosAsync: {e.Message}");
                        throw;
                    }
                })).Database;

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
            return Results.Problem($"[{s_counter}] Failed during {last}.{Environment.NewLine}" + e.Message + $"{Environment.NewLine}output: {s_sb}");
        }
        finally
        {
            s_counter++;
        }
    }
}

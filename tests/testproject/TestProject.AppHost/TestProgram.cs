// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using Aspire.Hosting.Lifecycle;
using Aspire.TestProject;
using Microsoft.Extensions.DependencyInjection;

public class TestProgram : IDisposable
{
    private const string AspireTestContainerRegistry = "netaspireci.azurecr.io";

    private TestProgram(
        string[] args,
        string assemblyName,
        bool disableDashboard,
        bool includeIntegrationServices,
        bool includeNodeApp,
        bool allowUnsecuredTransport,
        bool randomizePorts)
    {
        TestResourceNames resourcesToSkip = TestResourceNames.None;
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i].StartsWith("--skip-resources", StringComparison.InvariantCultureIgnoreCase))
            {
                if (args.Length > i + 1)
                {
                    resourcesToSkip = TestResourceNamesExtensions.Parse(args[i + 1].Split(','));
                    break;
                }
                else
                {
                    throw new ArgumentException("Missing argument to --skip-resources option.");
                }
            }
        }
        if (resourcesToSkip.HasFlag(TestResourceNames.dashboard))
        {
            disableDashboard = true;
        }

        var builder = DistributedApplication.CreateBuilder(new DistributedApplicationOptions()
        {
            Args = args,
            DisableDashboard = disableDashboard,
            AssemblyName = assemblyName,
            AllowUnsecuredTransport = allowUnsecuredTransport,
            // do this override ProjectDirectory being set to Aspire.Hosting.Tests
            ProjectDirectory = Path.GetDirectoryName(Projects.TestProject_AppHost.ProjectPath)
        });

        builder.Configuration["DcpPublisher:DeleteResourcesOnShutdown"] = "true";
        builder.Configuration["DcpPublisher:ResourceNameSuffix"] = $"{Random.Shared.Next():x}";
        builder.Configuration["DcpPublisher:RandomizePorts"] = randomizePorts.ToString(CultureInfo.InvariantCulture);

        AppBuilder = builder;

        // System.Console.WriteLine($"** TestProgram: ASPIRE_PROJECT_ROOT: {Environment.GetEnvironmentVariable("ASPIRE_PROJECT_ROOT")}");
        // System.Console.WriteLine($"** TestProgram: TestProject_AppHost.ProjectPath: {Projects.TestProject_AppHost.ProjectPath}");
        var serviceAPath = Path.Combine(Path.GetDirectoryName(Projects.TestProject_AppHost.ProjectPath)!, @"..\TestProject.ServiceA\TestProject.ServiceA.csproj");

        ServiceABuilder = AppBuilder.AddProject("servicea", serviceAPath, launchProfileName: "http");
        ServiceBBuilder = AppBuilder.AddProject<Projects.ServiceB>("serviceb", launchProfileName: "http");
        ServiceCBuilder = AppBuilder.AddProject<Projects.ServiceC>("servicec", launchProfileName: "http");
        WorkerABuilder = AppBuilder.AddProject<Projects.WorkerA>("workera");

        if (includeNodeApp)
        {
            // Relative to this project so that it doesn't changed based on
            // where this code is referenced from.
            var path = Path.Combine(Path.GetDirectoryName(Projects.TestProject_AppHost.ProjectPath)!, "..", "nodeapp");
            var scriptPath = Path.Combine(path, "app.js");

            NodeAppBuilder = AppBuilder.AddNodeApp("nodeapp", scriptPath)
                .WithHttpEndpoint(port: 5031, env: "PORT");

            NpmAppBuilder = AppBuilder.AddNpmApp("npmapp", path)
                .WithHttpEndpoint(port: 5032, env: "PORT");
        }

        if (includeIntegrationServices)
        {
            IntegrationServiceABuilder = AppBuilder.AddProject<Projects.IntegrationServiceA>("integrationservicea");
            IntegrationServiceABuilder = IntegrationServiceABuilder.WithEnvironment("SKIP_RESOURCES", string.Join(',', resourcesToSkip));

            if (!resourcesToSkip.HasFlag(TestResourceNames.sqlserver) || !resourcesToSkip.HasFlag(TestResourceNames.efsqlserver))
            {
                var sqlserverDbName = "tempdb";
                var sqlserver = AppBuilder.AddSqlServer("sqlserver")
                    .AddDatabase(sqlserverDbName);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(sqlserver);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.mysql) || !resourcesToSkip.HasFlag(TestResourceNames.efmysql))
            {
                var mysqlDbName = "mysqldb";
                var mysql = AppBuilder.AddMySql("mysql")
                    .WithImageRegistry(AspireTestContainerRegistry)
                    .WithEnvironment("MYSQL_DATABASE", mysqlDbName)
                    .AddDatabase(mysqlDbName);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(mysql);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.redis))
            {
                var redis = AppBuilder.AddRedis("redis")
                    .WithImageRegistry(AspireTestContainerRegistry);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(redis);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.garnet))
            {
                var garnet = AppBuilder.AddGarnet("garnet");
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(garnet);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.postgres) || !resourcesToSkip.HasFlag(TestResourceNames.efnpgsql))
            {
                var postgresDbName = "postgresdb";
                var postgres = AppBuilder.AddPostgres("postgres")
                    .WithImageRegistry(AspireTestContainerRegistry)
                    .WithEnvironment("POSTGRES_DB", postgresDbName)
                    .AddDatabase(postgresDbName);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(postgres);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.rabbitmq))
            {
                var rabbitmq = AppBuilder.AddRabbitMQ("rabbitmq")
                    .WithImageRegistry(AspireTestContainerRegistry);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(rabbitmq);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.mongodb))
            {
                var mongoDbName = "mymongodb";
                var mongodb = AppBuilder.AddMongoDB("mongodb")
                    .WithImageRegistry(AspireTestContainerRegistry)
                    .AddDatabase(mongoDbName);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(mongodb);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.oracledatabase))
            {
                var oracleDbName = "freepdb1";
                var oracleDatabase = AppBuilder.AddOracle("oracledatabase")
                    .AddDatabase(oracleDbName);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(oracleDatabase);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.cosmos) || !resourcesToSkip.HasFlag(TestResourceNames.efcosmos))
            {
                var cosmos = AppBuilder.AddAzureCosmosDB("cosmos").RunAsEmulator();
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(cosmos);
            }
            if (!resourcesToSkip.HasFlag(TestResourceNames.eventhubs))
            {
                var eventHub = AppBuilder.AddAzureEventHubs("eventhubns").RunAsEmulator().AddEventHub("hub");
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(eventHub);
            }

            if (!resourcesToSkip.HasFlag(TestResourceNames.milvus))
            {
                builder.Configuration["Parameters:milvusApiKey"] = "root:Milvus";

                var milvusApiKey = builder.AddParameter("milvusApiKey");

                var milvus = AppBuilder.AddMilvus("milvus", milvusApiKey)
                    .WithImageRegistry(AspireTestContainerRegistry);
                IntegrationServiceABuilder = IntegrationServiceABuilder.WithReference(milvus);
            }
        }

        AppBuilder.Services.AddLifecycleHook<EndPointWriterHook>();
        AppBuilder.Services.AddHttpClient();
    }

    public static TestProgram Create<T>(
        string[]? args = null,
        bool includeIntegrationServices = false,
        bool includeNodeApp = false,
        bool disableDashboard = true,
        bool allowUnsecuredTransport = true,
        bool randomizePorts = true)
    {
        return new TestProgram(
            args ?? [],
            assemblyName: typeof(T).Assembly.FullName!,
            disableDashboard: disableDashboard,
            includeIntegrationServices: includeIntegrationServices,
            includeNodeApp: includeNodeApp,
            allowUnsecuredTransport: allowUnsecuredTransport,
            randomizePorts: randomizePorts);
    }

    public IDistributedApplicationBuilder AppBuilder { get; private set; }
    public IResourceBuilder<ProjectResource> ServiceABuilder { get; private set; }
    public IResourceBuilder<ProjectResource> ServiceBBuilder { get; private set; }
    public IResourceBuilder<ProjectResource> ServiceCBuilder { get; private set; }
    public IResourceBuilder<ProjectResource> WorkerABuilder { get; private set; }
    public IResourceBuilder<ProjectResource>? IntegrationServiceABuilder { get; private set; }
    public IResourceBuilder<NodeAppResource>? NodeAppBuilder { get; private set; }
    public IResourceBuilder<NodeAppResource>? NpmAppBuilder { get; private set; }
    public DistributedApplication? App { get; private set; }

    public List<IResourceBuilder<ProjectResource>> ServiceProjectBuilders => [ServiceABuilder, ServiceBBuilder, ServiceCBuilder];

    public async Task RunAsync(CancellationToken cancellationToken = default)
    {
        var app = Build();
        await app.RunAsync(cancellationToken);
    }

    public DistributedApplication Build()
    {
        return App ??= AppBuilder.Build();
    }

    public void Run()
    {
        var app = Build();
        app.Run();
    }

    public void Dispose() => App?.Dispose();

    /// <summary>
    /// Writes the allocated endpoints to the console in JSON format.
    /// This allows for easier consumption by the external test process.
    /// </summary>
    private sealed class EndPointWriterHook : IDistributedApplicationLifecycleHook
    {
        public async Task AfterEndpointsAllocatedAsync(DistributedApplicationModel appModel, CancellationToken cancellationToken)
        {
            var root = new JsonObject();
            foreach (var project in appModel.Resources.OfType<ProjectResource>())
            {
                var projectJson = new JsonObject();
                root[project.Name] = projectJson;

                var endpointsJsonArray = new JsonArray();
                projectJson["Endpoints"] = endpointsJsonArray;

                foreach (var endpoint in project.Annotations.OfType<EndpointAnnotation>())
                {
                    var allocatedEndpoint = endpoint.AllocatedEndpoint;
                    if (allocatedEndpoint is null)
                    {
                        continue;
                    }

                    var endpointJsonObject = new JsonObject
                    {
                        ["Name"] = endpoint.Name,
                        ["Uri"] = allocatedEndpoint.UriString
                    };
                    endpointsJsonArray.Add(endpointJsonObject);
                }
            }

            // write the whole json in a single line so it's easier to parse by the external process
            await Console.Out.WriteLineAsync("$ENDPOINTS: " + JsonSerializer.Serialize(root, JsonSerializerOptions.Default));
        }
    }
}


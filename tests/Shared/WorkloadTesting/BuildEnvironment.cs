// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;
using Xunit.Sdk;

namespace Aspire.Workload.Tests;

public class BuildEnvironment
{
    public string                           DotNet                        { get; init; }
    public string                           DefaultBuildArgs              { get; init; }
    public IDictionary<string, string>      EnvVars                       { get; init; }
    public string                           LogRootPath                   { get; init; }

    public string                           BuiltNuGetsPath               { get; init; }
    public bool                             UsesCustomDotNet      { get; init; }
    public bool                             UsesSystemDotNet => !UsesCustomDotNet;
    public string?                          NuGetPackagesPath             { get; init; }
    public DirectoryInfo?                   RepoRoot                      { get; init; }
    public TemplatesCustomHive?             TemplatesCustomHive           { get; init; }

    public static readonly TestTargetFramework DefaultTargetFramework = ComputeDefaultTargetFramework();
    public static readonly string           TestAssetsPath = Path.Combine(AppContext.BaseDirectory, "testassets");
    public static readonly string           TestRootPath = Path.Combine(Path.GetTempPath(), "testroot");

    public static bool IsRunningOnHelix => Environment.GetEnvironmentVariable("HELIX_WORKITEM_ROOT") is not null;
    public static bool IsRunningOnCIBuildMachine => Environment.GetEnvironmentVariable("BUILD_BUILDID") is not null;
    public static bool IsRunningOnCI => IsRunningOnHelix || IsRunningOnCIBuildMachine;

    private static readonly Lazy<BuildEnvironment> s_instance_80 = new(() =>
        new BuildEnvironment(
            templatesCustomHive: TemplatesCustomHive.With9_0_Net8,
            sdkDirName: "dotnet-8"));

    private static readonly Lazy<BuildEnvironment> s_instance_90 = new(() =>
        new BuildEnvironment(
            templatesCustomHive: TemplatesCustomHive.With9_0_Net9,
            sdkDirName: "dotnet-latest"));

    private static readonly Lazy<BuildEnvironment> s_instance_90_80 = new(() =>
        new BuildEnvironment(
            templatesCustomHive: TemplatesCustomHive.With9_0_Net9_And_Net8,
            sdkDirName: "dotnet-9+8"));

    public static BuildEnvironment ForPreviousTFM => s_instance_80.Value;
    public static BuildEnvironment ForCurrentTFM => s_instance_90.Value;
    public static BuildEnvironment ForCurrentAndPreviousTFM => s_instance_90_80.Value;

    public static BuildEnvironment ForDefaultFramework { get; } = DefaultTargetFramework switch
    {
        TestTargetFramework.PreviousTFM => ForPreviousTFM,
        TestTargetFramework.CurrentTFM => ForCurrentTFM,
        _ => throw new ArgumentOutOfRangeException(nameof(DefaultTargetFramework))
    };

    public BuildEnvironment(bool useSystemDotNet = false, TestTargetFramework? targetFramework = default, TemplatesCustomHive? templatesCustomHive = default, string sdkDirName = "dotnet-latest")
    {
        UsesCustomDotNet = !useSystemDotNet;
        RepoRoot = TestUtils.FindRepoRoot();

        string sdkForWorkloadPath;
        if (RepoRoot is not null)
        {
            // Local run
            if (!useSystemDotNet)
            {
                // var sdkDirName = string.IsNullOrEmpty(EnvironmentVariables.SdkDirName) ? "dotnet-latest" : EnvironmentVariables.SdkDirName;
                var sdkFromArtifactsPath = Path.Combine(RepoRoot!.FullName, "artifacts", "bin", sdkDirName);
                if (Directory.Exists(sdkFromArtifactsPath))
                {
                    sdkForWorkloadPath = Path.GetFullPath(sdkFromArtifactsPath);
                }
                else
                {
                    // FIXME: update
                    string buildCmd = RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? ".\\build.cmd" : "./build.sh";
                    string workloadsProjString = Path.Combine("tests", "workloads.proj");
                    throw new XunitException(
                        $"Could not find a sdk with the workload installed at {sdkFromArtifactsPath} computed from {nameof(RepoRoot)}={RepoRoot}." +
                        $" Build all the packages with '{buildCmd} -pack'." +
                        $" Then install the sdk+workload with 'dotnet build {workloadsProjString}'." +
                        " See https://github.com/dotnet/aspire/tree/main/tests/Aspire.Workload.Tests#readme for more details.");
                }
            }
            else
            {
                string? dotnetPath = Environment.GetEnvironmentVariable("PATH")!
                    .Split(Path.PathSeparator)
                    .Select(path => Path.Combine(path, RuntimeInformation.IsOSPlatform(OSPlatform.Windows) ? "dotnet.exe" : "dotnet"))
                    .FirstOrDefault(File.Exists);
                if (dotnetPath is null)
                {
                    throw new ArgumentException($"Could not find dotnet.exe in PATH={Environment.GetEnvironmentVariable("PATH")}");
                }
                sdkForWorkloadPath = Path.GetDirectoryName(dotnetPath)!;
            }

            BuiltNuGetsPath = Path.Combine(RepoRoot.FullName, "artifacts", "packages", EnvironmentVariables.BuildConfiguration, "Shipping");

            PlaywrightProvider.DetectAndSetInstalledPlaywrightDependenciesPath(RepoRoot);
        }
        else
        {
            // CI - helix
            if (string.IsNullOrEmpty(EnvironmentVariables.SdkForWorkloadTestingPath) || !Directory.Exists(EnvironmentVariables.SdkForWorkloadTestingPath))
            {
                throw new ArgumentException($"Cannot find 'SDK_FOR_WORKLOAD_TESTING_PATH={EnvironmentVariables.SdkForWorkloadTestingPath}'");
            }
            sdkForWorkloadPath = EnvironmentVariables.SdkForWorkloadTestingPath;

            if (string.IsNullOrEmpty(EnvironmentVariables.BuiltNuGetsPath) || !Directory.Exists(EnvironmentVariables.BuiltNuGetsPath))
            {
                throw new ArgumentException($"Cannot find 'BUILT_NUGETS_PATH={EnvironmentVariables.BuiltNuGetsPath}' or {BuiltNuGetsPath}");
            }
            BuiltNuGetsPath = EnvironmentVariables.BuiltNuGetsPath;
        }

        if (!Directory.Exists(TestAssetsPath))
        {
            throw new ArgumentException($"Cannot find TestAssetsPath={TestAssetsPath}");
        }

        if (!string.IsNullOrEmpty(EnvironmentVariables.SdkForWorkloadTestingPath))
        {
            // always allow overridding the dotnet used for testing
            sdkForWorkloadPath = EnvironmentVariables.SdkForWorkloadTestingPath;
        }

        sdkForWorkloadPath = Path.GetFullPath(sdkForWorkloadPath);
        DefaultBuildArgs = string.Empty;
        NuGetPackagesPath = UsesCustomDotNet ? Path.Combine(AppContext.BaseDirectory, $"nuget-cache-{Guid.NewGuid()}") : null;
        EnvVars = new Dictionary<string, string>();
        if (UsesCustomDotNet)
        {
            EnvVars["DOTNET_ROOT"] = sdkForWorkloadPath;
            EnvVars["DOTNET_INSTALL_DIR"] = sdkForWorkloadPath;
            EnvVars["DOTNET_MULTILEVEL_LOOKUP"] = "0";
            EnvVars["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1";
            EnvVars["PATH"] = $"{sdkForWorkloadPath}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}";
        }
        EnvVars["NUGET_PACKAGES"] = NuGetPackagesPath!;
        EnvVars["BUILT_NUGETS_PATH"] = BuiltNuGetsPath;
        EnvVars["TreatWarningsAsErrors"] = "true";
        // Set DEBUG_SESSION_PORT='' to avoid the app from the tests connecting
        // to the IDE
        EnvVars["DEBUG_SESSION_PORT"] = "";
        // Avoid using the msbuild terminal logger, so the output can be read
        // in the tests
        EnvVars["_MSBUILDTLENABLED"] = "0";
        // .. and disable new output style for vstest
        EnvVars["VsTestUseMSBuildOutput"] = "false";
        EnvVars["SkipAspireWorkloadManifest"] = "true";

        DotNet = Path.Combine(sdkForWorkloadPath!, "dotnet");
        if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows))
        {
            DotNet += ".exe";
        }

        if (!string.IsNullOrEmpty(EnvironmentVariables.TestLogPath))
        {
            LogRootPath = Path.GetFullPath(EnvironmentVariables.TestLogPath);
            if (!Directory.Exists(LogRootPath))
            {
                Directory.CreateDirectory(LogRootPath);
            }
        }
        else
        {
            LogRootPath = Path.Combine(AppContext.BaseDirectory, "logs");
        }

        Console.WriteLine($"*** Using path for projects: {TestRootPath}");
        CleanupTestRootPath();
        Directory.CreateDirectory(TestRootPath);

        // Console.WriteLine($"*** [{TargetFramework}] Using workload path: {sdkForWorkloadPath}");
        if (UsesCustomDotNet)
        {
            if (EnvironmentVariables.IsRunningOnCI)
            {
                Console.WriteLine($"*** Using NuGet cache: {NuGetPackagesPath}");
                if (Directory.Exists(NuGetPackagesPath))
                {
                    Directory.Delete(NuGetPackagesPath, recursive: true);
                }
            }
            else
            {
                if (NuGetPackagesPath is not null && Directory.Exists(NuGetPackagesPath))
                {
                    foreach (var dir in Directory.GetDirectories(NuGetPackagesPath, "aspire*"))
                    {
                        Directory.Delete(dir, recursive: true);
                    }
                }
                Console.WriteLine($"*** Using NuGet cache (never deleted automatically): {NuGetPackagesPath}");
            }
        }

        // FIXME: this will happen for E2E tests which don't need it
        TemplatesCustomHive = templatesCustomHive;
        TemplatesCustomHive?.InstallAsync(this).Wait();

        static void CleanupTestRootPath()
        {
            if (!Directory.Exists(TestRootPath))
            {
                return;
            }

            try
            {
                Directory.Delete(TestRootPath, recursive: true);
            }
            catch (IOException) when (!EnvironmentVariables.IsRunningOnCI)
            {
                // there might be lingering processes that are holding onto the files
                // try deleting the subdirectories instead
                Console.WriteLine($"\tFailed to delete {TestRootPath} . Deleting subdirectories.");
                foreach (var dir in Directory.GetDirectories(TestRootPath))
                {
                    try
                    {
                        Directory.Delete(dir, recursive: true);
                    }
                    catch (IOException ioex)
                    {
                        // ignore
                        Console.WriteLine($"\tFailed to delete {dir} : {ioex.Message}. Ignoring.");
                    }
                }

            }
            catch (Exception ex)
            {
                throw new InvalidOperationException($"Error deleting '{TestRootPath}'.", ex);
            }
        }
    }

    public BuildEnvironment(BuildEnvironment otherBuildEnvironment)
    {
        DotNet = otherBuildEnvironment.DotNet;
        DefaultBuildArgs = otherBuildEnvironment.DefaultBuildArgs;
        EnvVars = new Dictionary<string, string>(otherBuildEnvironment.EnvVars);
        LogRootPath = otherBuildEnvironment.LogRootPath;
        BuiltNuGetsPath = otherBuildEnvironment.BuiltNuGetsPath;
        UsesCustomDotNet = otherBuildEnvironment.UsesCustomDotNet;
        NuGetPackagesPath = otherBuildEnvironment.NuGetPackagesPath;
        // TargetFramework = otherBuildEnvironment.TargetFramework;
        RepoRoot = otherBuildEnvironment.RepoRoot;
        TemplatesCustomHive = otherBuildEnvironment.TemplatesCustomHive;
    }

    private static TestTargetFramework ComputeDefaultTargetFramework()
    {
        if (EnvironmentVariables.DefaultTFMForTesting is not null)
        {
            return EnvironmentVariables.DefaultTFMForTesting switch
            {
                // FIXME: string tfm mapping here
                "" or "net9.0" => TestTargetFramework.CurrentTFM,
                "net8.0" => TestTargetFramework.PreviousTFM,
                _ => throw new ArgumentOutOfRangeException(nameof(EnvironmentVariables.DefaultTFMForTesting), EnvironmentVariables.DefaultTFMForTesting, "Invalid value")
            };
        }
        else
        {
            return TestTargetFramework.CurrentTFM;
        }
    }

}

public enum TestTargetFramework
{
    PreviousTFM,
    CurrentTFM
}

public static class TestTargetFrameworkExtensions
{
    public static string ToTFMString(this TestTargetFramework tfm) => tfm switch
    {
        TestTargetFramework.PreviousTFM => "net8.0",
        TestTargetFramework.CurrentTFM => "net9.0",
        _ => throw new ArgumentOutOfRangeException(nameof(tfm))
    };
}

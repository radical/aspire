// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Runtime.InteropServices;

namespace Aspire.End2End.Tests;

public class BuildEnvironment
{
    public string                           DotNet                        { get; init; }
    public bool                             IsWorkload                    { get; init; }
    public string                           DefaultBuildArgs              { get; init; }
    public IDictionary<string, string>      EnvVars                       { get; init; }
    // public string                           DirectoryBuildPropsContents   { get; init; }
    // public string                           DirectoryBuildTargetsContents { get; init; }
    public string                           LogRootPath                   { get; init; }

    public string                           WorkloadPacksDir              { get; init; }
    public string                           BuiltNuGetsPath               { get; init; }

    public bool IsRunningOnCI => EnvironmentVariables.IsRunningOnCI;

    public static readonly string           RelativeTestAssetsPath = @"..\testassets\";
    public static readonly string           TestAssetsPath = Path.Combine(AppContext.BaseDirectory, "testassets");
    public static readonly string           TestDataPath = Path.Combine(AppContext.BaseDirectory, "data");
    public static readonly string           TmpPath = Path.Combine(AppContext.BaseDirectory, "wbt artifacts");

    public BuildEnvironment()
    {
        DirectoryInfo? solutionRoot = new(AppContext.BaseDirectory);
        while (solutionRoot != null)
        {
            if (Directory.Exists(Path.Combine(solutionRoot.FullName, ".git")))
            {
                break;
            }

            solutionRoot = solutionRoot.Parent;
        }

        string? sdkForWorkloadPath = EnvironmentVariables.SdkForWorkloadTestingPath;
        if (string.IsNullOrEmpty(sdkForWorkloadPath))
        {
            // Is this a "local run?
            string sdkDirName = string.IsNullOrEmpty(EnvironmentVariables.SdkDirName) ? "dotnet-latest" : EnvironmentVariables.SdkDirName;
            string probePath = Path.Combine(solutionRoot!.FullName, "artifacts", "bin", sdkDirName);
            /* string probePath = Path.Combine(Path.GetDirectoryName(typeof(BuildEnvironment).Assembly.Location)!,
                                            "..",
                                            "..",
                                            "..",
                                            sdkDirName); */
            if (Directory.Exists(probePath))
            {
                sdkForWorkloadPath = Path.GetFullPath(probePath);
            }
            else
            {
                throw new ArgumentException($"Environment variable SDK_FOR_WORKLOAD_TESTING_PATH not set, and could not find it at {probePath}");
            }
        }
        if (!Directory.Exists(sdkForWorkloadPath))
        {
            throw new ArgumentException($"Could not find SDK_FOR_WORKLOAD_TESTING_PATH={sdkForWorkloadPath}");
        }

        sdkForWorkloadPath = Path.GetFullPath(sdkForWorkloadPath);

        DefaultBuildArgs = string.Empty;
        WorkloadPacksDir = Path.Combine(sdkForWorkloadPath, "packs");
        EnvVars = new Dictionary<string, string>();
        // bool workloadInstalled = EnvironmentVariables.SdkHasWorkloadInstalled != null && EnvironmentVariables.SdkHasWorkloadInstalled == "true";
        // if (workloadInstalled)
        // {
        //     DirectoryBuildPropsContents = s_directoryBuildPropsForWorkloads;
        //     DirectoryBuildTargetsContents = s_directoryBuildTargetsForWorkloads;
        //     IsWorkload = true;
        // }
        // else
        // {
        //     DirectoryBuildPropsContents = s_directoryBuildPropsForLocal;
        //     DirectoryBuildTargetsContents = s_directoryBuildTargetsForLocal;
        // }

        if (EnvironmentVariables.BuiltNuGetsPath is null || !Directory.Exists(EnvironmentVariables.BuiltNuGetsPath))
        {
            // FIXME: try auto-compute
            if (solutionRoot is not null)
            {
                BuiltNuGetsPath = Path.Combine(solutionRoot.FullName, "artifacts", "packages", EnvironmentVariables.BuildConfiguration, "Shipping");
            }
            if (!Directory.Exists(BuiltNuGetsPath))
            {
                throw new ArgumentException($"Cannot find 'BUILT_NUGETS_PATH={EnvironmentVariables.BuiltNuGetsPath} or {BuiltNuGetsPath}");
            }
        }
        else
        {
            BuiltNuGetsPath = EnvironmentVariables.BuiltNuGetsPath;
        }

        // `runtime` repo's build environment sets these, and they
        // mess up the build for the test project, which is using a different
        // dotnet
        EnvVars["DOTNET_ROOT"] = sdkForWorkloadPath;
        EnvVars["DOTNET_INSTALL_DIR"] = sdkForWorkloadPath;
        EnvVars["DOTNET_MULTILEVEL_LOOKUP"] = "0";
        EnvVars["DOTNET_SKIP_FIRST_TIME_EXPERIENCE"] = "1";
        EnvVars["PATH"] = $"{sdkForWorkloadPath}{Path.PathSeparator}{Environment.GetEnvironmentVariable("PATH")}";

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
            LogRootPath = Environment.CurrentDirectory;
        }

        if (Directory.Exists(TmpPath))
        {
            Directory.Delete(TmpPath, recursive: true);
        }

        Directory.CreateDirectory(TmpPath);
    }

    // protected static readonly string s_directoryBuildPropsForWorkloads = File.ReadAllText(Path.Combine(TestDataPath, "Workloads.Directory.Build.props"));
    // protected static readonly string s_directoryBuildTargetsForWorkloads = File.ReadAllText(Path.Combine(TestDataPath, "Workloads.Directory.Build.targets"));

    // protected static readonly string s_directoryBuildPropsForLocal = File.ReadAllText(Path.Combine(TestDataPath, "Local.Directory.Build.props"));
    // protected static readonly string s_directoryBuildTargetsForLocal = File.ReadAllText(Path.Combine(TestDataPath, "Local.Directory.Build.targets"));
}

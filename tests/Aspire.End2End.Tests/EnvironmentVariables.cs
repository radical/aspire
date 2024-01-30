// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.End2End.Tests;

public static class EnvironmentVariables
{
    public static readonly string? SdkForWorkloadTestingPath = Environment.GetEnvironmentVariable("SDK_FOR_WORKLOAD_TESTING_PATH");
    public static readonly string? SdkHasWorkloadInstalled   = Environment.GetEnvironmentVariable("SDK_HAS_WORKLOAD_INSTALLED");
    public static readonly string? WorkloadPacksVersion      = Environment.GetEnvironmentVariable("WORKLOAD_PACKS_VER");
    public static readonly string? TestLogPath               = Environment.GetEnvironmentVariable("TEST_LOG_PATH");
    public static readonly string? SkipProjectCleanup        = Environment.GetEnvironmentVariable("SKIP_PROJECT_CLEANUP");
    public static readonly string? XHarnessCliPath           = Environment.GetEnvironmentVariable("XHARNESS_CLI_PATH");
    public static readonly string? BuiltNuGetsPath           = Environment.GetEnvironmentVariable("BUILT_NUGETS_PATH");
    public static readonly string? BrowserPathForTests       = Environment.GetEnvironmentVariable("BROWSER_PATH_FOR_TESTS");
    public static readonly string? V8PathForTests            = Environment.GetEnvironmentVariable("V8_PATH_FOR_TESTS");
    public static readonly bool    ShowBuildOutput           = Environment.GetEnvironmentVariable("SHOW_BUILD_OUTPUT") is not null;
    public static readonly bool UseWebcil                    = Environment.GetEnvironmentVariable("USE_WEBCIL_FOR_TESTS") is "true";
    public static readonly string? SdkDirName                = Environment.GetEnvironmentVariable("SDK_DIR_NAME");
    public static readonly string? WasiSdkPath               = Environment.GetEnvironmentVariable("WASI_SDK_PATH");
    public static readonly bool    IsRunningOnCI             = Environment.GetEnvironmentVariable("IS_RUNNING_ON_CI") is "true";
    public static readonly string  BuildConfiguration        = Environment.GetEnvironmentVariable("BUILD_CONFIGURATION") ?? "Debug";
}

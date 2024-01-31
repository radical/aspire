// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics.CodeAnalysis;
using Xunit.Abstractions;

namespace Aspire.End2End.Tests;

public class End2EndTestBase
{
    public const string DefaultTargetFramework = "net8.0";
    private const string NuGetInsertionTag = "<!-- TEST_RESTORE_SOURCES_INSERTION_LINE -->";

    public static readonly BuildEnvironment s_buildEnv = new();
    public static string GetNuGetConfigPathFor(string targetFramework)
        => Path.Combine(BuildEnvironment.TestDataPath, targetFramework == "net9.0" ? "nuget9.config" : "nuget8.config");

    public ITestOutputHelper TestOutput { get; init; }
    protected string? _projectDir;
    protected string? _logPath;
    protected string? _nugetPackagesDir;

    public End2EndTestBase(ITestOutputHelper testOutput)
    {
        TestOutput = testOutput;
    }

    [MemberNotNull(nameof(_projectDir), nameof(_logPath))]
    protected void InitPaths(string id)
    {
        if (_projectDir == null)
        {
            _projectDir = Path.Combine(BuildEnvironment.TmpPath, id);
        }
        _logPath = Path.Combine(s_buildEnv.LogRootPath, id);
        _nugetPackagesDir = Path.Combine(BuildEnvironment.TmpPath, "nuget", id);

        if (Directory.Exists(_nugetPackagesDir))
        {
            Directory.Delete(_nugetPackagesDir, recursive: true);
        }

        Directory.CreateDirectory(_nugetPackagesDir!);
        Directory.CreateDirectory(_logPath);
    }

    protected void InitProjectDir(string dir, bool addNuGetSourceForLocalPackages = true, string targetFramework = DefaultTargetFramework)
    {
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "Directory.Build.props"), "<Project><PropertyGroup><ManagePackageVersionsCentrally>false</ManagePackageVersionsCentrally></PropertyGroup></Project>");//s_buildEnv.DirectoryBuildPropsContents);
        File.WriteAllText(Path.Combine(dir, "Directory.Build.targets"), "<Project/>");//s_buildEnv.DirectoryBuildTargetsContents);
        File.WriteAllText(Path.Combine(dir, "Directory.Packages.props"), "<Project/>");//s_buildEnv.DirectoryBuildPropsContents);

        string targetNuGetConfigPath = Path.Combine(dir, "nuget.config");
        if (addNuGetSourceForLocalPackages)
        {
            File.WriteAllText(targetNuGetConfigPath,
                                GetNuGetConfigWithLocalPackagesPath(
                                            GetNuGetConfigPathFor(targetFramework),
                                            s_buildEnv.BuiltNuGetsPath));
        }
        else
        {
            File.Copy(GetNuGetConfigPathFor(targetFramework), targetNuGetConfigPath);
        }
    }

    protected static string GetNuGetConfigWithLocalPackagesPath(string templatePath, string localNuGetsPath)
    {
        string contents = File.ReadAllText(templatePath);
        if (contents.IndexOf(NuGetInsertionTag, StringComparison.InvariantCultureIgnoreCase) < 0)
        {
            throw new ArgumentException($"Could not find {NuGetInsertionTag} in {templatePath}");
        }

        return contents.Replace(NuGetInsertionTag, $@"<add key=""nuget-local"" value=""{localNuGetsPath}"" />");
    }

}

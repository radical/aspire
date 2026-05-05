// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Tests.Acquisition.Fakes;

/// <summary>
/// Test fixture that creates an isolated temporary directory and writes either a v3 sidecar
/// layout (<see cref="WithSidecar"/>) or a v2 IdentityManifest layout (<see cref="WithIdentityManifest"/>)
/// into it. Cleans up on disposal.
/// </summary>
/// <remarks>
/// Callers pick exactly one <c>With*</c> method; calling either more than once or mixing both
/// is unsupported and will throw.
/// </remarks>
internal sealed class FakeRouteFixture : IDisposable
{
    private readonly TestTempDirectory _tempDir = new();
    private bool _configured;

    /// <summary>The root of the isolated temporary directory.</summary>
    public string Root => _tempDir.Path;

    /// <summary>The prefix directory that contains the <c>.aspire-install.json</c> sidecar or <c>installs/</c> tree.</summary>
    public string Prefix { get; private set; } = string.Empty;

    /// <summary>The path to the placeholder <c>aspire[.exe]</c> binary.</summary>
    public string BinaryPath { get; private set; } = string.Empty;

    /// <summary>
    /// Configures a v3 sidecar layout.  <paramref name="mode"/> controls whether
    /// Mode A (<c>prefix/bin/aspire</c>) or Mode B (<c>prefix/aspire</c>) is used.
    /// </summary>
    /// <returns><see langword="this"/> for fluent chaining.</returns>
    public FakeRouteFixture WithSidecar(SidecarBuilder sidecar, InstallMode mode = InstallMode.A)
    {
        EnsureNotConfigured();

        var prefixDir = Path.Combine(_tempDir.Path, "prefix");
        (Prefix, BinaryPath) = mode switch
        {
            InstallMode.A => SidecarBuilder.BuildModeA(prefixDir, sidecar),
            InstallMode.B => SidecarBuilder.BuildModeB(prefixDir, sidecar),
            _ => throw new ArgumentOutOfRangeException(nameof(mode), mode, null),
        };

        return this;
    }

    /// <summary>
    /// Configures a v2 IdentityManifest layout for testing the back-compat code path.
    /// </summary>
    /// <returns><see langword="this"/> for fluent chaining.</returns>
    public FakeRouteFixture WithIdentityManifest(IdentityManifestBuilder builder)
    {
        EnsureNotConfigured();

        var installDir = builder.WriteTo(_tempDir.Path);
        Prefix = installDir;

        var binDir = Path.Combine(installDir, "bin");
        BinaryPath = Path.Combine(binDir, OperatingSystem.IsWindows() ? "aspire.exe" : "aspire");

        return this;
    }

    /// <inheritdoc/>
    public void Dispose() => _tempDir.Dispose();

    private void EnsureNotConfigured()
    {
        if (_configured)
        {
            throw new InvalidOperationException("FakeRouteFixture has already been configured. Call WithSidecar or WithIdentityManifest exactly once.");
        }

        _configured = true;
    }
}

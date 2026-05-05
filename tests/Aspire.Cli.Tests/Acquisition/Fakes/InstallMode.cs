// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli.Tests.Acquisition.Fakes;

// TODO(PR2-subtask2): Replace with Aspire.Cli.Acquisition.InstallMode once Linus adds it.
// Mode A: sidecar lives one level above the binary directory (prefix/.aspire-install.json, binary at prefix/bin/aspire).
// Mode B: sidecar is a sibling of the binary (prefix/.aspire-install.json, binary at prefix/aspire).
internal enum InstallMode
{
    /// <summary>Mode A — sidecar at <c>prefix/.aspire-install.json</c>; binary under <c>prefix/bin/</c>.</summary>
    A,

    /// <summary>Mode B — sidecar at <c>prefix/.aspire-install.json</c>; binary at the prefix root.</summary>
    B,
}

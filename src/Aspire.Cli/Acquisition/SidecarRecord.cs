// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Serialization;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// JSON-deserialization shape for the <c>.aspire-install.json</c> sidecar file.
/// </summary>
internal sealed record SidecarRecord
{
    /// <summary>Gets the install route name (e.g. <c>script</c>, <c>winget</c>, <c>brew</c>, <c>dotnet-tool</c>, <c>pr</c>).</summary>
    [JsonPropertyName("route")]
    public string? Route { get; init; }

    /// <summary>Gets the user-facing update command hint to print when the route delegates self-update.</summary>
    [JsonPropertyName("updateCommand")]
    public string? UpdateCommand { get; init; }
}

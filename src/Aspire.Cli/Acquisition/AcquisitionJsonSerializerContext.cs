// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Text.Json.Serialization;

namespace Aspire.Cli.Acquisition;

/// <summary>
/// Source-generation JSON context for the acquisition subsystem.
/// Required for Native AOT compatibility — all JSON-bound types must be registered here.
/// </summary>
[JsonSourceGenerationOptions(
    PropertyNameCaseInsensitive = true,
    AllowTrailingCommas = true,
    ReadCommentHandling = System.Text.Json.JsonCommentHandling.Skip)]
[JsonSerializable(typeof(SidecarRecord))]
internal sealed partial class AcquisitionJsonSerializerContext : JsonSerializerContext
{
}

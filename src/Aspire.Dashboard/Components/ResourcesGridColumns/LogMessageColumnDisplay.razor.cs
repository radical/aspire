// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics.CodeAnalysis;
using Aspire.Dashboard.Otlp.Model;
using Aspire.Dashboard.Resources;
using Microsoft.JSInterop;

namespace Aspire.Dashboard.Components;

public partial class LogMessageColumnDisplay
{
    private bool TryGetErrorInformation([NotNullWhen(true)] out string? errorInfo)
    {
        // exception.stacktrace includes the exception message and type.
        // https://opentelemetry.io/docs/specs/semconv/attributes-registry/exception/
        if (GetProperty("exception.stacktrace") is { Length: > 0 } stackTrace)
        {
            errorInfo = stackTrace;
            return true;
        }
        if (GetProperty("exception.message") is { Length: > 0 } message)
        {
            if (GetProperty("exception.type") is { Length: > 0 } type)
            {
                errorInfo = $"{type}: {message}";
                return true;
            }

            errorInfo = message;
            return true;
        }

        errorInfo = null;
        return false;

        string? GetProperty(string propertyName)
        {
            return LogEntry.Attributes.GetValue(propertyName);
        }
    }

    private async Task CopyTextToClipboardAsync(string? text, string id)
        => await JS.InvokeVoidAsync("copyTextToClipboard", id, text, ControlsStringsLoc[nameof(ControlsStrings.GridValueCopyToClipboard)].ToString(), ControlsStringsLoc[nameof(ControlsStrings.GridValueCopied)].ToString());

}

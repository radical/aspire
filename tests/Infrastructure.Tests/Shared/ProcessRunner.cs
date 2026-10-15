// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.Diagnostics;
using Xunit;

namespace Infrastructure.Tests;

internal static class ProcessRunner
{
    public static async Task<ProcessResult> RunAsync(
        ITestOutputHelper output,
        string fileName,
        IReadOnlyList<string> arguments,
        string workingDirectory,
        TimeSpan timeout,
        IReadOnlyDictionary<string, string>? environment = null)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = fileName,
            WorkingDirectory = workingDirectory
        };

        foreach (var (name, value) in environment ?? new Dictionary<string, string>())
        {
            startInfo.Environment[name] = value;
        }
        foreach (var argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        return await RunAsync(output, startInfo, timeout).ConfigureAwait(false);
    }

    public static Task<ProcessResult> RunAsync(ProcessStartInfo startInfo, TimeSpan timeout)
        => RunAsync(null, startInfo, timeout);

    public static async Task<ProcessResult> RunAsync(
        ITestOutputHelper? output,
        ProcessStartInfo startInfo,
        TimeSpan timeout)
    {
        ArgumentOutOfRangeException.ThrowIfLessThanOrEqual(timeout, TimeSpan.Zero);

        startInfo.RedirectStandardOutput = true;
        startInfo.RedirectStandardError = true;
        startInfo.UseShellExecute = false;

        var arguments = string.Join(' ', startInfo.ArgumentList);
        output?.WriteLine($"Executing: {startInfo.FileName} {arguments} in {startInfo.WorkingDirectory}");

        using var process = new Process { StartInfo = startInfo };
        process.Start();

        // Read both streams concurrently to avoid deadlock when a pipe buffer fills.
        var stdoutTask = process.StandardOutput.ReadToEndAsync();
        var stderrTask = process.StandardError.ReadToEndAsync();

        using var cancellationTokenSource = new CancellationTokenSource(timeout);
        try
        {
            await process.WaitForExitAsync(cancellationTokenSource.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException exception) when (cancellationTokenSource.IsCancellationRequested)
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }

            await Task.WhenAll(stdoutTask, stderrTask).ConfigureAwait(false);
            throw new TimeoutException($"Process timed out after {timeout}: {startInfo.FileName} {arguments}", exception);
        }

        var result = new ProcessResult(
            process.ExitCode,
            await stdoutTask.ConfigureAwait(false),
            await stderrTask.ConfigureAwait(false));
        if (result.Output.Length > 0)
        {
            output?.WriteLine(result.Output);
        }

        return result;
    }
}

internal readonly record struct ProcessResult(int ExitCode, string StandardOutput, string StandardError)
{
    public string Output => StandardOutput + StandardError;
}

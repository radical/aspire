// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

namespace Aspire.Cli;

/// <summary>
/// Common command-line option names used for manual argument checks.
/// </summary>
internal static class CommonOptionNames
{
    public const string Version = "--version";
    public const string VersionShort = "-v";
    public const string Help = "--help";
    public const string HelpShort = "-h";
    public const string HelpAlt = "-?";
    public const string NoLogo = "--nologo";
    public const string Banner = "--banner";
    public const string Debug = "--debug";
    public const string DebugShort = "-d";
    public const string NonInteractive = "--non-interactive";
    public const string WaitForDebugger = "--wait-for-debugger";
    public const string CliWaitForDebugger = "--cli-wait-for-debugger";
    public const string StartDebugSession = "--start-debug-session";
    public const string Info = "--info";

    /// <summary>
    /// Help/version options that are recognized as informational wherever they appear in the raw
    /// arguments (including after a subcommand, e.g. "doctor --help"). None of these take a value.
    /// </summary>
    private static readonly string[] s_helpVersionOptionNames = [Version, VersionShort, Help, HelpShort, HelpAlt];

    /// <summary>
    /// Options that represent informational commands (e.g. --version, --help, --info) which should
    /// opt out of telemetry and suppress first-run experience. Unlike the help/version options,
    /// <see cref="Info"/> is only informational when it is a genuine root option — use
    /// <see cref="IsInformationalInvocation"/> to classify raw arguments correctly rather than a
    /// naive <c>Contains()</c> check against this list.
    /// </summary>
    public static readonly string[] InformationalOptionNames = [.. s_helpVersionOptionNames, Info];

    // Root options declared by RootCommand (src/Aspire.Cli/Commands/RootCommand.cs) that consume
    // a following token as their value. Kept in sync manually: DebugLevelOption ("--log-level"/"-l"),
    // CaptureProfileOutputOption ("--capture-profile-output"), CaptureProfileDelayOption
    // ("--capture-profile-delay"), and FormatOption ("--format"). Every other root option is a bare
    // bool flag and never consumes a following token. This list exists so a value that is literally
    // "--info" (e.g. `--log-level --info run`) is recognized as that option's value rather than a
    // distinct --info flag.
    private static readonly string[] s_valueTakingRootOptionNames =
    [
        "--log-level", "-l",
        "--capture-profile-output",
        "--capture-profile-delay",
        "--format",
    ];

    /// <summary>
    /// Determines whether raw process arguments represent an informational invocation that
    /// should opt out of telemetry and suppress first-run/startup output (banner, telemetry
    /// notice, etc.).
    /// </summary>
    /// <remarks>
    /// Help/version options (<see cref="Version"/>, <see cref="VersionShort"/>, <see cref="Help"/>,
    /// <see cref="HelpShort"/>, <see cref="HelpAlt"/>) are recognized wherever they appear before the
    /// "--" app-argument delimiter — including after a subcommand, e.g. "doctor --help" — matching
    /// existing CLI behavior. They are bare flags that never take a value, so no value-consumption
    /// tracking is required for them, and unlike <see cref="Info"/> they are not sensitive to the
    /// subcommand boundary (only to "--").
    ///
    /// <see cref="Info"/> (<c>--info</c>) is different: it is only a real root option, so it is
    /// recognized *only* when it appears before any subcommand/positional token or the "--"
    /// app-argument delimiter. "doctor --info" is the doctor subcommand's own (nonexistent)
    /// argument, not root --info, and "run -- --info" is an app argument passed through to the
    /// child process — neither should be treated as informational.
    /// </remarks>
    public static bool IsInformationalInvocation(IReadOnlyList<string> args)
    {
        foreach (var arg in args)
        {
            if (arg == "--")
            {
                // Stop scanning entirely: everything from here on is an app/positional argument.
                break;
            }

            if (Array.IndexOf(s_helpVersionOptionNames, arg) >= 0)
            {
                return true;
            }
        }

        return IsRootInfoInvocation(args);
    }

    private static bool IsRootInfoInvocation(IReadOnlyList<string> args)
    {
        for (var i = 0; i < args.Count; i++)
        {
            var token = args[i];

            if (token == "--")
            {
                // Everything after "--" is an app/positional argument, never a root option.
                return false;
            }

            // "--option=value" is a single token, so the value portion can never become a
            // separate "--info" token; only the option-name portion (before '=') matters here.
            var separatorIndex = token.IndexOf('=');
            var optionName = separatorIndex >= 0 ? token[..separatorIndex] : token;

            if (optionName == Info)
            {
                return true;
            }

            if (separatorIndex < 0 && Array.IndexOf(s_valueTakingRootOptionNames, optionName) >= 0)
            {
                // Consume the next token as this option's value so it is never reconsidered as
                // a distinct --info flag (e.g. `--log-level --info run`).
                i++;
                continue;
            }

            if (!token.StartsWith('-'))
            {
                // The first positional token marks the subcommand boundary; any --info from
                // this point on belongs to the subcommand, not the root command.
                return false;
            }
        }

        return false;
    }
}

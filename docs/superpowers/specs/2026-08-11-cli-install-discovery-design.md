# CLI installation discovery surface

## Goal

Resolve [#18970](https://github.com/microsoft/aspire/issues/18970) by moving
Aspire CLI installation discovery out of `aspire doctor` and into a dedicated
root-level `aspire --info` surface.

`aspire doctor` will return to one responsibility: actionable environment
health checks. `aspire --info` will report the running CLI identity, discovered
CLI installations, PATH state, install source, and orphaned hives in human and
machine-readable forms.

## Decision history

The command shape changed as the responsibilities became clearer:

1. [#17105](https://github.com/microsoft/aspire/pull/17105) originally added an
   `InfoCommand`. It moved the inventory into `doctor` after review asked why a
   separate command was needed.
2. During [#17334](https://github.com/microsoft/aspire/pull/17334), reviewers
   identified that installation inventory did not form a cohesive health
   report. The relevant thread concluded that the inventory should move to a
   separate surface.
3. [#17457](https://github.com/microsoft/aspire/pull/17457) proposed
   `aspire installs`, but closed unmerged.
4. [#17461](https://github.com/microsoft/aspire/pull/17461) superseded that
   proposal with root-level `aspire --info`. Maintainer review treated it as a
   `dotnet --info`-style experience and focused on rendering, API shape, and
   test coverage.
5. #18970 records `aspire --info` as the current design and requires that peer
   discovery move with a documented cross-version contract.

The implementation will therefore use `aspire --info`, not an `installs`
command and not a retained installation section in `doctor`.

## Scope

### In scope

- Remove installation discovery, installation rendering, and the hidden
  `doctor --self` option from `DoctorCommand`.
- Remove `installations` from `aspire doctor --format json`.
- Add root-only `aspire --info` human and JSON output.
- Move peer self-description to the new information surface.
- Preserve rich discovery against CLI versions that only support the legacy
  `doctor --self --format json` contract.
- Fall back to `--version` for older peers that expose neither rich contract.
- Report orphaned hives.
- Keep peer and aggregate discovery bounded.
- Update localized CLI strings and the machine-readable output specification.

### Out of scope

- Renaming internal `Route` APIs to `Source`.
- Renaming `InstallSource.Brew` to `Homebrew`.
- Changing installer scripts or the install sidecar format.
- Refactoring WinGet sidecar creation.
- Changing executable casing behavior in `PathLookupHelper`.
- Adding install or uninstall mutation commands.

These changes appeared in draft #17461 but are not required to resolve #18970.

## Command surface

`--info` is a root-only, non-recursive informational option:

```text
aspire --info
aspire --info --format json
```

It suppresses telemetry, update notifications, first-run notices, and other
output that would corrupt the information contract.

Two companion root options support the information action:

- `--format list|json` selects human or JSON output. The default is `list`.
- `--self` limits output to the running CLI. It remains hidden because it exists
  for peer discovery, not as a primary user workflow.

`--self` and the root `--format` option are invalid without `--info`.
Subcommands keep their existing local `--format` options; the root information
options do not become recursive.

## Human output

The text form follows the `dotnet --info` style requested in review:

- Use an unbordered Spectre Console grid or table.
- Use a two-character indentation column and a fixed-width label column so
  wrapped values remain aligned.
- Render headings with Spectre markup rather than Markdown.
- Escape all user- and environment-derived markup.
- Render user-facing status values as localized words such as `Not probed`,
  not wire values such as `notProbed`.

The first section identifies the running CLI with its version and channel.
Subsequent sections show each discovered installation and orphaned hive,
including all available values from:

- status and diagnostic reason
- version and channel
- install source
- binary path
- PATH status
- associated hive path

Missing values are omitted or rendered with a localized diagnostic placeholder
when their absence is significant.

## JSON contracts

### Full information

`aspire --info --format json` emits one object:

```json
{
  "version": "13.5.0",
  "channel": "stable",
  "installs": [
    {
      "kind": "installation",
      "path": "/usr/local/bin/aspire",
      "canonicalPath": "/usr/local/bin/aspire",
      "version": "13.5.0",
      "channel": "stable",
      "source": "script",
      "hive": "/home/user/.aspire/hives/stable",
      "pathStatus": "active",
      "status": "ok"
    },
    {
      "kind": "orphan-hive",
      "hive": "/home/user/.aspire/hives/pr-12345",
      "pathStatus": "notOnPath",
      "status": "noInstallFound"
    }
  ]
}
```

The output DTO maps the existing internal `InstallationInfo.Route` value to the
new contract's `source` field. Acquisition APIs, sidecars, and scripts do not
need to be renamed.

`status` and `pathStatus` remain separate axes. `statusReason` carries
diagnostic text without being concatenated into `status`. Nullable properties
are omitted.

### Peer self-description

`aspire --info --self --format json` emits a one-element array containing the
same installation row shape used by `installs[]`. A bare array keeps the peer
contract small and avoids wrapping it in unrelated command state.

The contract and field meanings will be documented in
`docs/specs/cli-output-formats.md`.

## Discovery and compatibility

`PeerInstallProbe` will probe each trusted peer in this order:

1. `--info --self --format json`
2. `doctor --self --format json`
3. `--version`

The second step preserves rich version, channel, and source data for currently
supported CLI binaries that predate `--info`. The third step is the compatibility
floor for binaries that support neither machine-readable self-description
surface.

The parser accepts the new bare installation array and the legacy doctor object
with an `installations` array. The locally read sidecar remains authoritative
for install source when a legacy or version-only peer omits it.

Identity override environment variables are stripped before launching peers so
each binary describes its on-disk identity rather than inheriting the caller's
emulated identity.

## Hive correlation

A focused hive enumerator reads directories directly beneath
`CliExecutionContext.HivesDirectory`.

For each discovered installation with a valid channel, the information output
attaches the corresponding hive path when it exists. Remaining hive directories
become `orphan-hive` rows. Files at the hives root are ignored.

Hive enumeration is read-only and uses the repository's safe filesystem
enumeration pattern so inaccessible or concurrently removed entries do not
abort the entire information command.

## Failure and timeout behavior

The information surface is diagnostic, so partial failures should remain
visible:

- A peer probe failure produces a `failed` row with `statusReason`.
- A sidecar-less candidate is listed as `notProbed` and is not executed.
- A full discovery failure produces a `discovery-failed` row.
- Cancellation propagates instead of being converted into diagnostic success.

All compatibility attempts for one peer share one five-second budget. The full
discovery operation keeps the existing 30-second aggregate budget. A hung peer
is terminated with its process tree. Moving discovery out of `doctor` ensures
normal doctor invocations no longer pay either budget.

Non-cancellation discovery failures return successful command completion with
diagnostic rows. The command's purpose is to explain a damaged installation,
and failing before emitting that explanation would make it less useful.

## Implementation boundaries

- `DoctorCommand` owns only environment check execution and rendering.
- A dedicated information action owns root option handling and output.
- `InstallationInfoOutput` owns tolerant discovery, hive correlation, DTO
  shaping, and human rendering.
- `PeerInstallProbe` owns cross-version process invocation and parsing.
- `RootCommand` only declares and wires the root options; discovery logic does
  not move into `RootCommand` or `Program`.

This keeps `Program` from accumulating command-specific behavior, addressing
the review feedback on draft #17461.

## Test strategy

Targeted tests will prove:

- `doctor` does not resolve or invoke installation discovery.
- Human doctor output has no installation section.
- Doctor JSON contains only `checks` and `summary`.
- `--info` is root-only and suppresses unrelated startup output and telemetry.
- Root `--self` and `--format` are rejected without `--info`.
- Human information output aligns wrapped values, uses readable statuses, and
  preserves literal Windows paths and markup characters.
- Full JSON matches the documented wrapper and row contract.
- Self JSON emits exactly one bare-array row.
- Hives are correlated to installs and unmatched directories become orphan rows.
- New peers use the new contract.
- Legacy peers fall back to `doctor --self --format json` without losing rich
  metadata.
- Older peers fall back to `--version`.
- Malformed, failed, oversized, and timed-out peer output becomes bounded
  diagnostic failure rather than a hang.
- Reverting the probe move would fail the new-contract test; reverting doctor
  cleanup would fail the non-invocation and output-shape tests.

## Documentation and localization

- Update `docs/specs/cli-output-formats.md` with both `--info` JSON shapes.
- Update install-discovery comments that currently name `doctor`.
- Move installation-specific resources out of `DoctorCommandStrings` and add
  the corresponding information-surface resources.
- Update generated resource designer members manually.
- Regenerate affected XLIFF files with the existing `UpdateXlf` target.

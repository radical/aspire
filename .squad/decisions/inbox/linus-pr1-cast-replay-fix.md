# PR1 cast-replay fix — `aspire new` channel selection on local-build CLI

**Author:** Linus
**Wave:** 14 (cast-replay)
**Status:** FIX APPLIED on `ankj/v3-pr1-channel`
**Run:** 25477301939 attempt 2 (HEAD `123f54b01c`)

## Verdict

**(A) Real regression introduced by the PR1 series.** Specifically: latent bug **unmasked** by Basher's hive-label autodetect (`4e9ea689cc`).

Retracts wave-12's (C) flake classification of:
- `Aspire.Cli.EndToEnd.Tests.ConfigDiscoveryTests.RunFromParentDirectory_UsesExistingConfigNearAppHost`
- `Aspire.Cli.EndToEnd.Tests.TypeScriptStarterTemplateTests.CreateAndRunTypeScriptStarterProject`

Two attempts on the same SHA, identical error signature → deterministic regression.

## Root cause (cast-replay confirmed)

The CLI builds with `AspireCliChannel=pr`, so:
- `CliExecutionContext.Channel = "pr-16820"`
- Hive at `~/.aspire/hives/pr-16820/packages` contains only the PR prerelease `13.4.0-pr.16820.g123f54b0`
- A `pr-16820` `PackageChannel` is registered with `PinnedVersion` set to that prerelease

But `NewCommand.ResolveCliTemplateVersionAsync` (line 329-331, pre-fix) ignores `ExecutionContext.Channel` and always selects the **Implicit** (nuget.org) channel when `--channel` isn't passed. `dotnet package search` returns the latest stable `Aspire.ProjectTemplates@13.2.4`. That flows into:

```
inputs.Version = "13.2.4"
  → ScaffoldContext.SdkVersion = "13.2.4"
  → aspire.config.json#sdk.version = "13.2.4"
  → AspireConfigFile.GetIntegrationReferences yields Aspire.Hosting@13.2.4
  → RestoreCommand.BuildPackageSpec calls VersionRange.Parse("13.2.4") → ">= 13.2.4" (stable-only)
  → PSM routes to /root/.aspire/hives/pr-16820/packages
  → Hive only has 13.4.0-pr.16820.g123f54b0 (prerelease)
  → NuGet refuses prerelease for stable range → "Unable to find a stable package …" → restore fails → exit 4 (config) / 6 (ts-starter)
```

Pre-Basher: hive was `local`, didn't match the `pr-16820` PSM emission, so restore fell through to nuget.org and either succeeded or failed with a different signature. Basher's correct hive alignment exposed the latent version-range bug — both legs were necessary for the failure.

## Fix

`src/Aspire.Cli/Commands/NewCommand.cs:327-345` — when `--channel` is not supplied and `VersionHelper.IsLocalBuildChannel(ExecutionContext.Channel)` returns true, prefer the channel whose `Name == ExecutionContext.Channel`. Falls back to the existing Implicit→first-of-list chain otherwise. ~12 lines added, no removals.

The PR channel's `PinnedVersion` short-circuits `GetTemplatePackagesAsync` to return the exact PR-prerelease, `TryGetCurrentCliVersionMatch` finds it, `inputs.Version` becomes `13.4.0-pr.16820.g123f54b0`, and the restore range `>= 13.4.0-pr.16820.g123f54b0` matches the hive content.

## Verification

- `dotnet build` clean (0 warnings, 0 errors).
- `NewCommandTests`: 54/54 passing (`dotnet test --project tests/Aspire.Cli.Tests/Aspire.Cli.Tests.csproj --no-build --filter-class "*.NewCommandTests"`).
- The fix is gated on `IsLocalBuildChannel(ExecutionContext.Channel)`, so existing stable/daily-channel test paths are untouched.

End-to-end CI re-run will confirm the two failing tests now pass.

## Other call sites — audited, all OK

- `AddCommand.cs:137-140` — when `hasHives`, includes ALL channels (defers to `Parallel.ForEachAsync`).
- `TemplateNuGetConfigService.cs:147-167` — same pattern.
- `UpdateCommand.cs:206-220` — prompts user when hives exist.

Only `NewCommand.ResolveCliTemplateVersionAsync` had the blind Implicit pick.

## Emergent rule (channel coherence, expanded to 4 axes)

> A locally-built CLI channel must stay coherent across:
> 1. build-time identity (`AspireCliChannel`)
> 2. execution-context channel (`CliExecutionContext.Channel`)
> 3. hive directory layout (`~/.aspire/hives/{Channel}/packages`)
> 4. **runtime channel selection at every consumer site** (template-version resolution, integration discovery, NuGet config emission).
>
> Closing axes 1–3 without 4 yields a latent "stable range vs prerelease package" failure once PSM is correctly aligned. Whenever a new local-build channel is introduced, audit every `PackageChannelType.Implicit` selection site and confirm each one defers to `ExecutionContext.Channel` when `IsLocalBuildChannel` is true.

## Lesson — single-failure flake calls are uncalled

Two consecutive attempts on the same SHA with identical signatures is a deterministic regression, period. Wave-12's "(C) flake — passed before" was incorrect; "passed before" only proves the regression was introduced between then and now. Ratifies the strengthened flake-classification rule recorded in history.md.

## Files touched

- `src/Aspire.Cli/Commands/NewCommand.cs` (one method, ~12 lines)
- `.squad/agents/linus/history.md` (wave-14 entry)
- `.squad/decisions/inbox/linus-pr1-cast-replay-fix.md` (this file)

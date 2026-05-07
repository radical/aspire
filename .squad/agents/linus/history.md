# Linus Agent History

## Learnings

### Comments Must Stand Alone (Part 1): Design-Doc References Are Public Liability

**Date:** 2026-05-06

**Rule:** Code and test comments MUST stand on their own without referencing internal design documents, design-spec sections (§), internal goal-group labels, or internal task labels.

**Why:** Comments and assertion messages are part of the final repository artifact visible to all future maintainers and external contributors. Once committed, they become searchable history. References to internal design docs create confusion, break links if docs are reorganized, and presume a shared understanding that future readers won't have.

**Forbidden Patterns:**
- `PR1-S<N>` / `PR1-spec` / `PR1 G<N>` (design-phase labels)
- `Spec §<section>` or `§<section>` (section numbers in design docs)
- `Acquisition v3`, `the v3 spec`, `PR1 design contract` (spec-era terminology)
- Goal-group prose: `cross-route channel contamination`, `route-aware update`, `sidecar primitive`
- Filenames: `agreed-design-v3.md`

**Example transformation:**
```csharp
// ❌ "PR1-S7 removed the global-channel read fallback."
// ✅ "Channel resolution uses per-project aspire.config.json only, never the global config."
```

---

### Comments Must Stand Alone (Part 2): No Removal/Negation Framing

**Date:** 2026-05-06 (strengthened rule)

**Rule:** Comments must describe what the code DOES, not what was removed, deferred, or "no longer" present. The diff is against `origin/main` — from that perspective, removed code was never there. Explaining its absence is meaningless to a fresh reader.

**Why:** When reviewing code without knowledge of the design doc or prior state, a comment like "we removed X" creates confusion. The reader doesn't see what was removed, so the comment doesn't clarify the current behavior — it only documents ancient history.

**ALSO FORBIDDEN (in addition to Part 1):**
- `"no longer reads ..."`, `"no longer consults ..."`
- `"was removed"`, `"was deleted"`, `"fell back to"`
- `"we removed"`, `"we don't do"`, `"we chose not to"`
- Comments that only make sense if reader knows what we deleted
- XML doc text like `"removed in PR1-S10 ..."` or `"now-removed global-channel fallback"`

**Replacement rule:** Either **DELETE the comment entirely** (the absence speaks for itself), OR **rewrite as a POSITIVE statement of CURRENT behavior** (what the code DOES now).

**Examples:**

```csharp
// ❌ "PR1-S7 removed the global-channel read fallback."
// ✅ "Channel resolution uses per-project aspire.config.json only."

// ❌ "The global-channel read fallback was removed..."
// ✅ "Channel resolution queries per-project aspire.config.json only, never the global ~/.aspire/aspire.config.json."

// ❌ "We removed the IConfigurationService dependency. It was deleted here."
// ✅ (Just delete the comment—the missing dependency speaks for itself. TemplateNuGetConfigService.Ctor
//     will not accept IConfigurationService; the constraint is enforced structurally.)

// ❌ Comment block explaining deleted test: "Pre-existing test X was deleted: it exercised the now-removed
//     global-channel fallback (FakeConfigurationServiceWithChannel → TemplateNuGetConfigService) that PR1 G1
//     forbids. With ResolveTemplatePackageAsync no longer reading the global config, the only way init can
//     pick up a non-implicit channel is via an explicit query parameter..."
// ✅ "Channel resolution uses explicit input or per-project aspire.config.json only; coverage in TemplateNuGetConfigServiceTests."

// ❌ XML doc: "Spec-derived regression tests for PR1-S10: project-channel reseed sites read the value to persist
//     from CliExecutionContext.Channel (option-(a) resolved label — pr-<N> for PR builds..."
// ✅ "Regression tests for project-channel reseed sites, ensuring that the resolved channel label from
//     CliExecutionContext.Channel (pr-<N> for PR builds, identity verbatim otherwise) is correctly persisted."
```

**Scope:**
- Apply to: `src/`, `tests/`, `eng/` (all production and test code, including YAML/script comments)
- Exempt: `.squad/`, `docs/specs/`, internal design docs (those ARE where labels and removal history belong)
- Include: Test assertion messages (they appear in failure output that lands in CI logs)
- Exclude: Commit message bodies (those are committer notes, not in-code material)

**Verification (comprehensive pattern):**
```bash
git --no-pager diff origin/main..HEAD -- src/ tests/ eng/ | grep -nE '^\+.*\b(PR1-S[0-9]|PR1-spec|PR1 G[0-9]|Spec §|§[0-9]\.[0-9]|§G[0-9]|Acquisition v3|agreed-design-v3|per spec §|G[0-9] \(|cross-route channel contamination|route-aware update|the v3 spec|PR1 design contract|sidecar primitive|no longer reads|no longer consults|fallback was removed|we removed|chose not to)'
```
Should return **zero hits** after scrub is complete.



## 2026-05-06 — N1/N2/N3 fold-in (channel default → local)

Same drift class as C3, missed in the sweep. Three test-side spots referenced `daily` as default channel. Folded fixes:
- N1: Aspire.Cli.Tests.csproj `AspireCliChannel` default → `local` + lockstep comment updated.
- N2: CliBootstrapTests.s_validChannels now includes `"local"`.
- N3: For `WhenIdentityChannelIsNotLocal_HiveKeepsDirectoryName`, preserved the test's original *intent* (covering the non-local hive path) by passing `channel: "daily"` explicitly rather than renaming. Renaming would silently drop test coverage of the non-local branch, since the local case is already covered by the preceding test. Insight: when a test name and setup diverge after a default flips, choose the option that preserves *coverage*, not the option that minimizes diff.


## 2026-05-07 — Wave-14 CI triage: retraction of wave-12 (C) classification

**Run:** 25477301939 (HEAD `123f54b01c`), both attempts.

**Tests reclassified:**
- `Aspire.Cli.EndToEnd.Tests.ConfigDiscoveryTests.RunFromParentDirectory_UsesExistingConfigNearAppHost` (Polyglot variant)
- `Aspire.Cli.EndToEnd.Tests.TypeScriptStarterTemplateTests.CreateAndRunTypeScriptStarterProject` (DotNet variant)

**Wave-12 verdict:** (C) hex1b flake. **Reclassified:** (A) PR1-caused regression.

**Evidence forcing retraction:**
- Both tests fail in 2/2 attempts of the same SHA with **identical error signature**: `[10 ERR:*]` at `Hex1bAutomatorTestHelpers.DeclineAgentInitPromptAsync:488`, called from `AspireNewAsync:657`. Counter stuck at 10 → `aspire new` exits non-zero before "configure AI agent environments" prompt appears.
- Identical install strategy: `LocalArchive (/home/runner/work/aspire/aspire/cli-archives) [expected=13.4.0-pr.16820.g123f54b0]`.
- Both tests share the same helper (`AspireNewAsync` → `DeclineAgentInitPromptAsync`), so single root cause for both.
- Pre-PR1 baseline (25422767716) and earliest PR1 run (25469878546): GREEN. First red: 25471316418 (after `4e9ea689cc`, basher's hive-label autodetect). Then red in 25477301939 attempts 1+2.

**Why wave-12 was wrong:**
A test that fails 1× does not prove flakiness. Flake classification requires either (a) ≥3 data points showing pass/fail oscillation on the same SHA, or (b) a plausible timing/contention theory with a confirmed window. I had neither — I only had "passed before, failed once" and inferred (C) from the absence of obvious code-path linkage. That's lazy.

**Lesson — flake classification rule, strengthened:**
> When triaging a "this test failed but passed before" case, NEVER classify (C) flake without:
> 1. ≥2 attempts on the SAME SHA showing oscillation, OR
> 2. ≥3 data points across SHAs showing intermittent passes interleaved with fails, OR
> 3. A specific timing/contention theory tied to evidence in the failure log (race-on-readiness, port collision, etc).
>
> Otherwise the only honest classifications are (A) regression-caused or (B) pre-existing-bug-revealed. **Single failure = single regression hypothesis until proved otherwise. Two consecutive identical failures on the same SHA = deterministic regression, period.** "It passed once before" is not evidence of flakiness; it's evidence that the regression introduced between then and now.

**Why this matters:** wave-12's (C) classification gave a green-light feel to "wave through" failures that were actually deterministic regressions caused by my own series. If owner had relied on that verdict to merge PR1, two real regressions would have shipped to main as known-broken tests. That is exactly the failure mode the candor instruction is meant to prevent.

**Verdict scope (this wave):** Do not wave through. Investigation continues in `linus-pr1-ci-triage-wave14.md`. Most likely mechanism is that `4e9ea689cc`'s autodetect changed `hive_label` from `"local"` to `"pr-16820"` for the LocalArchive path used by Cli.EndToEnd tests, and *something else* in that code path still expects `"local"` (or fails on the new label). Polyglot validation tests, which now pass, were the *intended* beneficiary; Cli.EndToEnd Docker tests were collateral. Exact mechanism requires inspection of the test recording (`.cast`) artifact, which I could not retrieve in this session.


## 2026-05-08 — Wave-14 cast replay → fix (NewCommand channel selection)

**Run replayed:** 25477301939 attempt 2. Cast artifacts pulled (per-attempt API endpoint 404s; used `/runs/{id}/artifacts` and picked the larger/later artifact IDs):
- `cli-e2e-recordings-Cli.EndToEnd-ConfigDiscoveryTests` (id 6847728742)
- `cli-e2e-recordings-Cli.EndToEnd-TypeScriptStarterTemplateTests` (id 6847724362)

**Smoking gun (identical in both casts), tail of `aspire new`:**
```
Using hive label: pr-16820
NuGet packages successfully installed to: /root/.aspire/hives/pr-16820/packages
Package version suffix: pr.16820.g123f54b0
...
Package source mapping matches found for package ID 'Aspire.Hosting' are: '/root/.aspire/hives/pr-16820/packages'.
ERROR: Unable to find a stable package Aspire.Hosting with version (>= 13.2.4)
  - Found 1 version(s) in /root/.aspire/hives/pr-16820/packages [ Nearest version: 13.4.0-pr.16820.g123f54b0 ]
  - Versions from https://api.nuget.org/v3/index.json were not considered
```

**Root cause (verified end-to-end through the source):**

Channel-coherence triangle now has a **fourth axis** that wasn't previously aligned:
1. ✓ build-time channel: `AspireCliChannel=pr` baked at compile time
2. ✓ execution context: `CliExecutionContext.Channel = "pr-16820"`
3. ✓ hive layout: Basher's `4e9ea689cc` autodetect places packages at `~/.aspire/hives/pr-16820/packages`
4. ✗ **template-version channel selection in `aspire new`**: still picks Implicit (nuget.org) regardless of execution-context channel.

Path: `NewCommand.ResolveCliTemplateVersionAsync` (line 329-331, pre-fix) hard-coded:
```csharp
var selectedChannel = string.IsNullOrWhiteSpace(configuredChannelName)
    ? channels.FirstOrDefault(c => c.Type is PackageChannelType.Implicit) ?? channels.FirstOrDefault()
    : channels.FirstOrDefault(c => string.Equals(c.Name, configuredChannelName, StringComparison.OrdinalIgnoreCase));
```
With no `--channel` (the headline `aspire new` UX) the Implicit (nuget.org) channel wins. `dotnet package search` returns the latest stable `Aspire.ProjectTemplates@13.2.4`. That `13.2.4` flows: `inputs.Version` → `ScaffoldContext.SdkVersion` → `aspire.config.json#sdk.version` → `AspireConfigFile.GetIntegrationReferences("Aspire.Hosting", sdkVersion="13.2.4")` → `RestoreCommand.BuildPackageSpec` → `VersionRange.Parse("13.2.4")` → `>= 13.2.4` (stable-only range). PSM correctly routes `Aspire.Hosting` to the PR hive (because of build-time PSM emission), but the hive only contains the PR prerelease (`13.4.0-pr.16820.g123f54b0`), and NuGet refuses prerelease for stable ranges. Restore fails.

**Why pre-Basher this wasn't visible:** before `4e9ea689cc` the hive was `local` (default), which didn't match the `pr-16820` PSM target, so restore fell back to nuget.org and either succeeded with stable packages (different error path) or failed with a different error. Once Basher correctly aligned the hive label, the version-range mismatch became the dominant failure. **Fixing the hive unmasked the version-range bug.**

**Other call sites — already correct, by accident or design:**
- `AddCommand.cs:137-140` — when `hasHives`, includes ALL channels.
- `TemplateNuGetConfigService.cs:147-167` — when `hasPrHives`, includes ALL channels.
- `UpdateCommand.cs:206-220` — when `hasHives`, prompts user.
Only `NewCommand.ResolveCliTemplateVersionAsync` blindly picked Implicit.

**Fix applied (small, confined):** `NewCommand.cs:327-345`. When `--channel` isn't supplied AND `IsLocalBuildChannel(ExecutionContext.Channel)`, prefer the channel whose `Name == ExecutionContext.Channel`. Falls back to the existing Implicit→first-of-list chain if no match. The PR channel has `PinnedVersion = 13.4.0-pr.16820.g123f54b0` set by `PackagingService.GetLocalHivePinnedVersion`, so `GetTemplatePackagesAsync` short-circuits to that version, `TryGetCurrentCliVersionMatch` finds it, and `inputs.Version` becomes the PR-prerelease that the hive actually contains.

**Verification:** 54/54 `NewCommandTests` pass with no behavioral regression (the fix only fires when `IsLocalBuildChannel(Channel)` returns true, which the tests don't exercise unless explicitly configured).

**Wave-14 verdict update:** retraction of (C) classification confirmed. These were always (A) regressions. Same SHA + identical error signature × 2 attempts is a deterministic regression, full stop. The "it passed once before" datum was useless because that earlier run hit the wrong hive and silently fell through to a different code path.

**Emergent rule (channel coherence, full statement):**
> A locally-built CLI channel must be coherent across **four** axes, not three:
> 1. build-time identity (`AspireCliChannel`)
> 2. execution-context channel (`CliExecutionContext.Channel`)
> 3. installed hive directory layout (`~/.aspire/hives/{Channel}/packages`)
> 4. **runtime channel selection at every consumer** (template version resolution, integration discovery, NuGet config emission).
>
> Closing axes 1–3 without 4 produces a *latent* failure mode: PSM routes correctly to the hive, but the version range emitted by the consumer doesn't match what the hive contains. The error surface is "stable range vs prerelease package" — a NuGet semantic that has nothing to do with channel routing yet is fully *caused* by mis-routed version selection. Whenever a new "local-build channel" is introduced, audit every channel-selection site (`grep -rn "PackageChannelType.Implicit"` and friends) and confirm each one defers to `ExecutionContext.Channel` when running on a local-build channel.

**Files touched:** `src/Aspire.Cli/Commands/NewCommand.cs` (one method, ~12 lines added).

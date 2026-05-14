# Aspire CLI Acquisition — Validation Runbook

> Living doc. Sections marked ✅ have been executed against a real install on the listed OS. Sections marked 🚧 are documented but not yet validated by this runbook.

Pairs with `docs/specs/install-routes.md` (sidecar contract) and `docs/specs/bundle.md` (bundle layout).

## Purpose

Validates PR2 sidecar-driven bundle extraction across all CLI acquisition routes and operating systems, by running one common scenario template per route. The template asserts that:

1. The install route writes the correct `.aspire-install.json` next to the binary.
2. The first `aspire run` extracts the bundle to the route's expected prefix as a real versioned directory tree (`versions/<v>/{dcp,managed}/`), with `<prefix>/bundle/` as a reparse point to the active version.

## Scope

- **PR-under-test focus** (today): a single in-flight PR. The PR-script and dotnet-tool routes are the primary install vectors for PR builds.
- **Release-channel routes** (`script`, `winget`, `brew`) are covered by the same template against a published channel — same shape, different source string.
- Brew is not yet wired into Aspire's release pipeline at the time of writing.

## Routes — quick reference

| Route         | Install tool                          | Sidecar `source` | Default install prefix shape                 | Real bundle path                                    |
|---------------|---------------------------------------|------------------|----------------------------------------------|------------------------------------------------------|
| `script`      | `get-aspire-cli.{sh,ps1}`             | `script`         | `<user-home>/.aspire/`                       | `<user-home>/.aspire/versions/<v>/{dcp,managed}/`   |
| `pr`          | `get-aspire-cli-pr.{sh,ps1}`          | `pr`             | `<user-home>/.aspire/dogfood/pr-<N>/`        | `…/dogfood/pr-<N>/versions/<v>/{dcp,managed}/`      |
| `dotnet-tool` | `dotnet tool install -g Aspire.Cli`   | `dotnet-tool`    | `<store-leaf>` (deep — see route section)    | `<store-leaf>/versions/<v>/{dcp,managed}/`          |
| `winget`      | winget catalog or local manifest      | `winget`         | per-winget portable Packages dir             | `<winget-pkg-dir>/versions/<v>/{dcp,managed}/`      |
| `brew`        | Homebrew cask                         | `brew`           | Homebrew Cellar/Caskroom                     | (route-equivalent flat-layout payload dir)          |

Cross-cutting facts:

- The binary lives at:
  - `script`, `pr`: `<prefix>/bin/aspire{,.exe}` (bin-layout — bundle is sibling of `bin/`).
  - `winget`, `brew`: `<prefix>/aspire{,.exe}` (flat-layout — bundle is co-located with binary).
  - `dotnet-tool`: `<dotnet-tools-dir>/aspire{.cmd,.exe}` is a **shim** that dispatches to the **store leaf** `<dotnet-tools-dir>/.store/aspire.cli/<ver>/aspire.cli.<rid>/<ver>/tools/<tfm>/<rid>/`. The sidecar and bundle live with the actual binary at the store leaf, **not** at the shim dir.
- `<prefix>/bundle/` is **always** a reparse point (NTFS Junction on Windows; symlink on Linux/macOS) pointing into the active `versions/<v>/`. Real payload is under `versions/<v>/`, never under `bundle/` itself.
- Active version directory names embed the CLI version and a fingerprint hash (e.g. `13.4.0-pr.16817.g790d6fa3-8f500a0045b967ce`). Enumerate `versions/*` and skip `.tmp.*`, `.bad.*`, `.old.*` siblings.

## Scenario template

Run the same prep + assertions for each route under test.

### Prep

1. **Clean state.** Run [Cleanup (between scenarios)](#cleanup-between-scenarios). Confirm `Get-Command aspire` / `command -v aspire` is empty and `<user-home>/.aspire` does not exist.
2. **Install via the route.** See the per-route section below.
3. **Prepend the route's bin dir to session PATH only.** Never write to persistent PATH during testing.
4. **Smoke test** (skip only if told otherwise):
   1. `aspire --version` — record version string.
   2. `aspire new aspire-empty -n RB-<short-id> -o <tempProjectDir> --non-interactive --language csharp`.
   3. In `<tempProjectDir>`, launch `aspire run` in the **same shell** as the eventual stop (see Finding F4). Wait for the line containing `Dashboard:` on stdout.
   4. `aspire stop --apphost <tempProjectDir>/apphost.cs`, bounded to 30s. **Trust AppHost-process-exit as authoritative success** (Finding F1) — record stop-client exit code as diagnostic only.

### Assertions

- **Sidecar present** at `<binaryDir>/.aspire-install.json` with `{ "source": "<expected>" }` matching the route table above.
- **Versioned payload present**, after `aspire run`, at `<prefix>/versions/<v>/dcp/` and `<prefix>/versions/<v>/managed/`, each containing route-appropriate executables:
  - Windows: `dcp/dcp.exe`, `managed/aspire-managed.exe`.
  - Linux/macOS: `dcp/dcp`, `managed/aspire-managed`, executable bit set.
- **`<prefix>/bundle/` is a reparse point** pointing into the active `versions/<v>/`.

### Cleanup

After each scenario, run [Cleanup (between scenarios)](#cleanup-between-scenarios) and confirm clean state before the next scenario.

## Per-route execution

### Route: `pr` (PR archive via `get-aspire-cli-pr`) ✅

| OS           | Install command                                                                                                  |
|--------------|------------------------------------------------------------------------------------------------------------------|
| Linux/macOS  | `bash eng/scripts/get-aspire-cli-pr.sh <PR-N> --skip-extension --skip-path [--workflow-run-id <id>]`             |
| Windows      | `pwsh -File eng\scripts\get-aspire-cli-pr.ps1 <PR-N> -SkipExtension -SkipPath [-WorkflowRunId <id>]`             |

Notes:

- `--skip-extension` (`-SkipExtension`) skips the VS Code extension wiring — irrelevant for CLI route validation.
- `--skip-path` (`-SkipPath`) prevents the script from writing to persistent PATH; the script still emits a "prepend this to PATH" line that you should apply to the **session** only.
- The script downloads three CI artifacts: `cli-native-archives-<rid>` (CLI binary), `built-nugets`, `built-nugets-for-<rid>` (templates + integration packages). The nupkgs go to `<user-home>/.aspire/hives/pr-<N>/packages`.

Expected install state:

- Binary: `<user-home>/.aspire/dogfood/pr-<N>/bin/aspire{,.exe}`
- Sidecar: `<user-home>/.aspire/dogfood/pr-<N>/bin/.aspire-install.json` → `{ "source": "pr" }`
- After `aspire run`:
  - Real payload at `<user-home>/.aspire/dogfood/pr-<N>/versions/<v>/{dcp,managed}/`.
  - Reparse point at `<user-home>/.aspire/dogfood/pr-<N>/bundle/` → `versions/<v>/`.
- After `aspire new`: project-local `NuGet.config` references `<user-home>/.aspire/hives/pr-<N>/packages` (see Finding F5).

### Route: `dotnet-tool` (PR build via `dotnet tool install`) ✅

The PR CI workflow uploads two relevant artifact bundles to each run:

- `built-nugets-for-<rid>` (one per RID built by GH Actions: `win-x64`, `win-arm64`, `linux-x64`, `linux-arm64`, `osx-arm64`). Flat layout. **Contains both the RID-specific pack `Aspire.Cli.<rid>.<ver>.nupkg` and the cross-platform pointer pack `Aspire.Cli.<ver>.nupkg`** — a single `--add-source` against this directory is sufficient.
- `built-nugets` (cross-platform). Optional for this route; extracts with a `Debug/Shipping/` subtree.

Two RIDs (`linux-musl-x64`, `osx-x64`) are only produced by AzDO weekly/official builds, not GH Actions PR runs. Alpine and Intel-Mac users must fall back to the `pr` route.

#### Step 1 — Download the per-RID artifact

`gh run download --name <name>` on a busy repo paginates through **all** repo artifacts (encountered HTTP 502 around page 501 against `microsoft/aspire`). Use the run-scoped API + direct download instead:

```bash
# 1. Resolve artifact id within the run.
gh api "repos/microsoft/aspire/actions/runs/<runId>/artifacts?per_page=100&page=<n>" \
  | jq '.artifacts[] | select(.name == "built-nugets-for-<rid>") | .id'

# 2. Download by id (binary zip on stdout).
gh api "repos/microsoft/aspire/actions/artifacts/<id>/zip" > built-nugets-for-<rid>.zip

# 3. Extract.
unzip -d nugets-for-<rid> built-nugets-for-<rid>.zip   # Linux/macOS
# or PowerShell: Expand-Archive built-nugets-for-<rid>.zip -DestinationPath nugets-for-<rid>
```

#### Step 2 — Verify the embedded sidecar (optional but recommended)

The RID-specific nupkg must carry `tools/<tfm>/<rid>/.aspire-install.json` with body `{"source":"dotnet-tool"}`. Peek inside the zip without extracting:

| OS | Command |
|----|---------|
| Linux/macOS | `unzip -p nugets-for-<rid>/Aspire.Cli.<rid>.<ver>.nupkg tools/<tfm>/<rid>/.aspire-install.json` |
| Windows     | `Add-Type -AssemblyName System.IO.Compression.FileSystem; [System.IO.Compression.ZipFile]::OpenRead(<path>).Entries.Where({ $_.FullName -eq "tools/<tfm>/<rid>/.aspire-install.json" })` then read via `StreamReader` |

#### Step 3 — `dotnet tool install`

```bash
dotnet tool install -g Aspire.Cli \
    --add-source <artifactRoot>/nugets-for-<rid> \
    --version 13.4.0-pr.<PR-N>.g<short-sha>
```

`--prerelease` is required if you omit `--version`. PR builds carry a prerelease suffix `pr.<PR>.g<short-sha>`, so the resolver ignores them without one of the two flags. Pinning `--version` is recommended for runbook use because it fails fast on the wrong artifact set rather than installing a stable from nuget.org.

#### Where things land

`dotnet tool install -g` writes into the user's global-tool store. The shim is what's on PATH; the **actual** binary plus sidecar live deep in the store:

```
~/.dotnet/tools/
├── aspire[.cmd|.exe]                          # shim (the only thing on PATH)
└── .store/aspire.cli/<ver>/
    └── aspire.cli.<rid>/<ver>/tools/<tfm>/<rid>/
        ├── aspire[.exe]                       # ACTUAL binary
        ├── .aspire-install.json               # ACTUAL sidecar
        └── (after first run)
            ├── versions/<v>/{dcp,managed}/    # real bundle payload
            └── bundle/                        # Junction|symlink → versions/<v>/
```

Note the inner pack id is `aspire.cli.<rid>` (the RID-specific package), not `aspire.cli` (the pointer). The pointer's `DotnetToolSettings.xml` dispatches to the RID pack at install time, so the RID's `tfm`/`rid` segment is what `BundleService` reads.

#### Verification (post-install + post-first-run)

| Check | Where | Expected |
|-------|-------|----------|
| Shim on PATH | `aspire --version` | matches `--version` passed to install |
| Embedded sidecar | `<storeLeaf>/.aspire-install.json` | `{"source":"dotnet-tool"}` |
| Bundle payload | `<storeLeaf>/versions/<v>/{dcp,managed}/` | exists after first `aspire run` |
| Bundle reparse | `<storeLeaf>/bundle/` | Junction (Windows) / symlink (Unix) → `versions/<v>/` |

Where `<storeLeaf>` = `<dotnet-tools-dir>/.store/aspire.cli/<ver>/aspire.cli.<rid>/<ver>/tools/<tfm>/<rid>`.

#### Uninstall

`dotnet tool uninstall -g Aspire.Cli` removes the shim plus the entire `.store/aspire.cli/<ver>/` subtree. Anything the CLI wrote under `~/.aspire/` (logs, hives) is left alone and must be removed by the cleanup section.

### Route: `script` (release channel) 🚧 by this runbook

Same shape as `pr`, but:
- `<prefix>` = `<user-home>/.aspire/` (no `dogfood/pr-<N>/` segment).
- Sidecar source = `script`.
- Install command:

| OS           | Install command                                                                                          |
|--------------|----------------------------------------------------------------------------------------------------------|
| Linux/macOS  | `bash eng/scripts/get-aspire-cli.sh --skip-path --quality dev`                                          |
| Windows      | `pwsh -File eng\scripts\get-aspire-cli.ps1 -SkipPath -Quality dev`                                       |

### Route: `winget` (Windows release builds or local manifest) 🚧

- Binary lands flat inside the winget portable Packages dir.
- Sidecar is **not** written by the installer — `WingetFirstRunProbe` stamps `{"source":"winget"}` on the first `aspire …` invocation if it can match the running binary's path against the winget portable ARP registry entry for `Microsoft.Aspire`.
- For PR builds: build a local manifest from the per-RID archive in `cli-native-archives-win-<arch>` and feed it to `winget install --manifest <path> --accept-source-agreements`.

## Common helpers

### Cleanup (between scenarios)

Per scenario, in order:

1. Stop live processes by PID: `aspire`, `aspire-managed`, any test AppHost `dotnet` processes spawned from the scenario's project dir.
2. Uninstall via every channel that could be present from the previous scenario:
   - dotnet-tool: `dotnet tool uninstall -g Aspire.Cli` (ignore "not installed" error).
   - winget (Windows): `winget uninstall --id Microsoft.Aspire --silent` (ignore "not installed").
   - brew (macOS): `brew uninstall aspire` (ignore "not installed") when the cask is added.
3. Remove install-prefix dirs:
   - `<user-home>/.aspire/` (covers `script`, `pr` routes; also dotnet-tool bundle if it landed there during testing).
4. Remove scenario temp project dirs.
5. **Re-read session PATH from Machine + User** so any stale entry from a prior install isn't masked (this only mutates the current shell — persistent PATH is untouched).
6. Assert: `Get-Command aspire` (Windows) / `command -v aspire` (Unix) empty, `<user-home>/.aspire` absent.

### Cleanup (end-of-day, additional)

At the end of a testing session, scrub stale persistent PATH entries pointing at `<user-home>/.aspire/...` or `<user-home>/.dotnet/tools` that earlier installs (or earlier non-`--skip-path` runs) may have written.

- Windows: edit User PATH via `[System.Environment]::SetEnvironmentVariable('PATH', <filtered>, 'User')`. Filter only entries matching the install-prefix patterns; preserve everything else verbatim. This is a persistent mutation — note it in the run log.
- Linux/macOS: remove matching `export PATH=…` / `case ":${PATH}:" …` blocks from `~/.bashrc`, `~/.zshrc`, `~/.profile` that point at `~/.aspire/` or `~/.dotnet/tools`.

Avoid the need for this scrub during the day by always passing `--skip-path` / `-SkipPath` to install scripts.

## Findings

Numbered findings recorded while running this runbook. New findings get appended; existing entries are not renumbered.

### F1 — `aspire stop` client exit is unreliable as a "stop succeeded" signal

Two failure modes observed for the stop client even when the AppHost actually stops:

- Stop client hangs indefinitely past the stop signal (observed against a daily-channel build).
- Stop client returns non-zero and prints `❌ Failed to stop …apphost.cs.` (observed against a PR build), while AppHost stdout contains `🛑 Stopping Aspire.` and the AppHost PID is already gone.

**Rule for this runbook:** bound the stop client to 30s. Treat AppHost-process-exit-within-30s as the authoritative success signal. Record stop-client exit + output as diagnostic, never as an assertion.

### F2 — Real bundle payload lives under `versions/<v>/`, not under `bundle/`

`<prefix>/bundle/` is always a reparse point. Asserting against `bundle/dcp/dcp.exe` succeeds but hides the layout-is-real-and-versioned guarantee. Assert against `versions/<v>/dcp/...` for payload presence, and use `bundle/` to additionally confirm the reparse-point flip succeeded.

### F3 — Reparse-point type per OS

- Windows: `(Get-Item <prefix>/bundle).LinkType` returns `Junction`. NTFS junction; works without admin or developer-mode privileges.
- Linux/macOS: a regular symlink. Verify with `readlink -f` / `test -L`.

### F4 — `aspire run` spawned with inherited console dies when the parent shell exits

On Windows, `Start-Process -NoNewWindow aspire run` (or any background-spawn that inherits the parent console) ties the AppHost lifetime to the parent shell. If the parent exits between `run` and `stop`, the AppHost dies with it and the stop client has nothing to talk to.

**Run `run → wait-for-dashboard → assertions → stop` inside a single shell invocation.** Detached spawns are possible but require redirecting stdout/stderr explicitly and disowning before the parent exits.

Linux/macOS: `aspire run &` is similarly subject to SIGHUP if the parent shell exits without `disown`/`nohup`. Same single-shell rule applies; `nohup aspire run >run.log 2>&1 &` is the detached form.

### F5 — `aspire new` writes a project-local NuGet.config for PR-script installs

For installs via `get-aspire-cli-pr`, `aspire new` writes (or updates) `<projectDir>/NuGet.config` to add `<user-home>/.aspire/hives/pr-<N>/packages` as a feed. The smoke test should:
- Assert the file exists in the new project dir.
- Confirm the route-specific hive path is in it (forward or back slashes either accepted).
- For other routes, confirm this feed is **not** present (would indicate cross-route contamination).

### F6 — Stale persistent User PATH entries survive `<prefix>` deletion

Earlier installs without `--skip-path` / `-SkipPath` leave entries pointing at install dirs in persistent User PATH (Windows) or shell profile files (Linux/macOS). Deleting `<prefix>` does not remove them, and a fresh session shell will pick them up, masking failures. Always use `--skip-path` / `-SkipPath` for test installs; otherwise run the end-of-day persistent-PATH scrub before declaring a clean state.

### F7 — Dotnet-tool route does NOT write a project-local NuGet.config

For the `dotnet-tool` route, `aspire new` does **not** write `<projectDir>/NuGet.config`. This is the inverse of F5: the PR-script route writes the route-specific hive feed into the project; the dotnet-tool route doesn't (no per-PR hive — tool install resolves through the user's ambient NuGet configuration). Assertions that depend on the hive-feed pattern (e.g. integration tests verifying which feeds will be consulted by `dotnet restore`) should branch on route.

### F8 — `gh run download --name <name>` is unusable on busy repos

`gh run download` paginates through **all** repo artifacts when filtering by name, and hits HTTP 502 around page 501 against `microsoft/aspire`. The reliable substitute is two-step: resolve the artifact id from the run-scoped API, then download the zip directly via `gh api`.

```
gh api "repos/<owner>/<repo>/actions/runs/<runId>/artifacts?per_page=100&page=<n>"   # find id
gh api "repos/<owner>/<repo>/actions/artifacts/<id>/zip" > artifact.zip                # download
```

Paginate by name until either total is reached or the artifact is found.

### F9 — `aspire.exe` shim may not exist after `dotnet tool install` on Windows

If a prior install left an `aspire.exe` shim that was locked at the time `dotnet tool install -g Aspire.Cli` ran (held by an unrelated standalone `aspire.exe` running elsewhere), the install completes successfully but only the `aspire.cmd` shim is written; the previous `aspire.exe` is renamed to `aspire.exe.old.<id>`. `where.exe aspire` returns `aspire.cmd` in this state and `aspire --version` still works via the .cmd. Detection: after install, check for `aspire.exe.old.*` siblings of the shim. (Related to F6 — fresh installs onto machines with prior testing leftover are the common case.)

### F10 — `dotnet tool uninstall -g Aspire.Cli` errors when the bundle reparse point exists

Observed on Windows: `dotnet tool uninstall -g Aspire.Cli` fails with `Unhandled exception: Access to the path 'bundle' is denied.` and a non-zero exit. The store subtree (including the Junction's target `versions/<v>/`) is nonetheless removed in most cases, but the failure is not benign — it leaves a partially-uninstalled global tool registration that the next install may collide with. Cleanup must:

1. Treat a non-zero exit from `dotnet tool uninstall` as a warning, not a stop-the-run failure.
2. Force-remove `<dotnet-tools-dir>/.store/aspire.cli` afterward regardless of the uninstall exit code.
3. Verify the global-tool listing (`dotnet tool list -g`) no longer mentions `aspire.cli` before declaring clean.

Root cause is the SDK's recursive-delete routine not unwinding the reparse point correctly; a fix in the SDK or in Aspire's bundle layout (deleting the Junction before the target) would close this. Until then, this is the documented behavior to handle in test fixtures and cleanup scripts.

## Per-route validation log

| Route         | OS          | Date       | Verdict | Version installed                    | Notes                                                                                |
|---------------|-------------|------------|---------|--------------------------------------|--------------------------------------------------------------------------------------|
| `pr`          | windows-x64 | 2026-05-14 | 🟢 PASS | `13.4.0-pr.16817.g790d6fa3`          | Versioned payload + reparse Junction both observed. F1, F5 noted in logs.            |
| `dotnet-tool` | windows-x64 | 2026-05-14 | 🟢 PASS | `13.4.0-pr.16817.g790d6fa3`          | Bundle landed at `<storeLeaf>/versions/<v>/{dcp,managed}/`. F1 hit again (stop client hung silently). F7 (no NuGet.config), F8 (gh artifact API), F9 (aspire.cmd-only shim) added. |

# Move Windows CLI NativeAOT Build to build_sign_native

## Status: Implementation Complete ✅

Branch: `dotnet-tool-naot-split-windows` (11 commits, 7 core files, +630/-98 lines)

## Problem

Windows CLI package creation (NativeAOT build, signing, nupkg creation) currently
happens in the main `BuildAndTest.yml` Windows job. macOS and Linux already use
`build_sign_native.yml`. This creates inconsistency — Windows has per-RID publish,
sign, bundle, and verify steps inline in the managed build, while other platforms
do all of that in a dedicated template.

### Goals

1. Move Windows CLI NativeAOT builds into `build_sign_native.yml` alongside
   macOS and Linux
2. Produce dotnet tool nupkgs (RID-specific + pointer) from the native build
3. Remove all per-RID Windows logic from `BuildAndTest.yml`
4. Add build-time validation to catch unsigned, missing, or broken packages

## Architecture: Before → After

### Before

```
build_sign_native stage          build stage (Windows)
┌────────────────────┐          ┌──────────────────────────────────────┐
│ macOS:             │          │ BuildAndTest.yml:                    │
│   osx-arm64        │          │   1. Download native archives        │
│   osx-x64          │          │   2. Download CLI tool nupkgs        │
│ Linux:             │──────────│   3. Per-RID: publish aspire-managed │
│   linux-x64        │          │   4. Per-RID: sign aspire-managed    │
│   linux-arm64      │          │   5. Per-RID: create bundle layout   │
│   linux-musl-x64   │          │   6. Main build (TargetRids=...,     │
│                    │          │      PackCliTool=true)                │
│ Pointer pkg:       │          │   7. Verify CLI archives (win-x64)   │
│   REMOVED (all)    │          │   8. Verify CLI tool nupkgs          │
└────────────────────┘          │   9. Stage/publish native_archives   │
                                │  10. Pack+sign+publish all packages  │
                                └──────────────────────────────────────┘
```

### After

```
build_sign_native stage          build stage (Windows)
┌────────────────────┐          ┌──────────────────────────────────────┐
│ macOS:             │          │ BuildAndTest.yml:                    │
│   osx-arm64        │          │   1. Download native archives        │
│   osx-x64          │          │   2. Download CLI tool nupkgs        │
│ Linux:             │──────────│   3. Copy tool nupkgs to Shipping/   │
│   linux-x64        │          │   4. Validate completeness (all 8)   │
│   linux-arm64      │          │   5. Main build (SkipNativeBuild=    │
│   linux-musl-x64   │          │      true, -sign)                    │
│ Windows:           │          │   6. dotnet nuget verify (signatures)│
│   win-x64    ←NEW  │          │   7. Pack+publish all packages       │
│   win-arm64  ←NEW  │          └──────────────────────────────────────┘
│                    │
│ Pointer pkg:       │
│   KEPT (win-x64)   │
│   REMOVED (others) │
└────────────────────┘
```

## Files Changed (7 core files, +630/-98)

### 1. `eng/clipack/Common.projitems` (+65 lines) — NEW

The `_PackCliTool` MSBuild target that produces dotnet tool nupkgs from the
pre-built NativeAOT binary. Runs `AfterTargets="_PublishProject"` during the
`--build` phase of clipack.

Produces two packages per RID:
- `Aspire.Cli.<RID>.nupkg` — RID-specific package containing the native binary
- `Aspire.Cli.nupkg` — pointer/meta package that references all RID packages

The target uses `dotnet pack` on `Aspire.Cli.Tool.csproj` with the pre-built
native binary as input, avoiding double-compilation and signing order issues.

### 2. `eng/pipelines/templates/build_sign_native.yml` (+67/-10)

**Container conditional** — Windows doesn't use build containers:
```yaml
${{ if ne(parameters.agentOs, 'windows') }}:
  container: ${{ replace(targetRid, '-', '_') }}
```

**Windows CLI archive verification** — new block for win-x64 using
`verify-cli-archive.ps1` (mirrors existing macOS/Linux `.tar.gz` verification).

**Functional test coverage** — added win-x64 to `runFunctional` logic:
```powershell
$runFunctional = ... -or
    ('${{ targetRid }}' -eq 'win-x64' -and '${{ parameters.agentOs }}' -eq 'windows')
```

**Tool nupkg verification** — runs `verify-cli-tool-nupkg.ps1` with
`-VerifySignature` when `codeSign: true` (macOS/Windows).

**Pointer package strategy** — conditional removal, kept only from win-x64:
```yaml
${{ if ne(targetRid, 'win-x64') }}:
  - pwsh: |  # Remove pointer packages...
```

### 3. `eng/pipelines/templates/BuildAndTest.yml` (+34/-98)

**Removed** (~120 lines):
- `targetRids` parameter
- Per-RID `dotnet publish aspire-managed` steps
- `TargetRids` and `PackCliTool=true` from main build command
- CLI archive verification (win-x64)
- CLI tool nupkg verification (per-RID)
- `native_archives_win_x64` / `native_archives_win_arm64` staging and publishing

**Added:**
- `SkipNativeBuild=true` to main build command
- `dotnet nuget verify --all` step after build+sign (conditioned on `_SignType == 'real'`)

### 4. `eng/Build.props` (+9 lines)

Excludes `Aspire.Cli.Tool.csproj` when `SkipNativeBuild=true`:
```xml
<ItemGroup Condition="'$(SkipNativeBuild)' == 'true'">
  <ProjectToBuild Remove="$(RepoRoot)src\Aspire.Cli\Aspire.Cli.Tool.csproj" />
</ItemGroup>
```

Without this, `PackAsTool=true` in Tool.csproj would trigger NativeAOT compilation
during the main build's `-pack` step, producing conflicting nupkgs.

### 5. `eng/pipelines/azure-pipelines.yml` (+81/-5)

- Added Windows invocation of `build_sign_native` template (win-x64, win-arm64)
- Added CLI tool nupkg download+copy-to-Shipping steps
- Added completeness validation step (dynamically discovers expected RIDs)
- Removed `targetRids` from BuildAndTest invocation
- Updated comments

### 6. `eng/pipelines/azure-pipelines-unofficial.yml` (+81/-3)

Mirror of official pipeline changes. Added Windows native build invocation,
tool nupkg download+copy, and completeness validation.

### 7. `eng/scripts/verify-cli-tool-nupkg.ps1` (+293 lines) — NEW

Comprehensive per-RID tool nupkg verification script:
1. Finds the RID-specific nupkg and validates >5MB size
2. Extracts and validates the native binary (PE header for Windows, ELF for Linux, Mach-O for macOS)
3. Runs functional tests (`aspire --version`, `aspire new`) for native-architecture RIDs
4. Validates pointer package references the correct RID package
5. Signature verification (`.signature.p7s` check) when `-VerifySignature` is passed

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Pointer package from win-x64 only** | The pointer package is metadata-only and identical from all platforms. win-x64 is guaranteed to run. |
| **No container for Windows** | Windows uses pool VMs directly (`windows.vs2026preview.scout.amd64`), not Docker. |
| **`SkipNativeBuild=true`** | Prevents clipack projects from being discovered/built in the managed build job. |
| **`ProjectToBuild Remove` for Tool.csproj** | Without this, `PackAsTool=true` triggers NativeAOT during `-pack`. |
| **Linux `codeSign: false` unchanged** | Linux ELF binaries don't need signing. Linux nupkgs get signed by MicroBuild in BuildAndTest. |
| **Pre-built binary as pack input** | Avoids double-compilation and ensures the signed binary goes into the nupkg. |
| **RID discovery from csproj glob** | `eng/clipack/Aspire.Cli.*.csproj` glob auto-adapts when RIDs are added/removed. |

## Signing Flow

```
build_sign_native:
  macOS  (codeSign:true)  → binary signed → nupkg signed → .signature.p7s verified ✓
  Windows (codeSign:true) → binary signed → nupkg signed → .signature.p7s verified ✓
  Linux  (codeSign:false) → binary NOT signed → nupkg NOT signed

BuildAndTest (Windows):
  1. All 8 nupkgs copied to Shipping/ (mix of signed + unsigned)
  2. Main build with -sign → MicroBuild sweeps Shipping/, signs unsigned Linux nupkgs
  3. dotnet nuget verify --all → authoritative final gate on ALL nupkgs
```

Risk: if MicroBuild only signs items produced by the current build (not pre-copied
files), Linux nupkgs ship unsigned. The `dotnet nuget verify` gate catches this.

## Build-Time Validation Checks

See `docs/cli-tool-nupkg-validation.md` for the full validation checklist.

Summary of checks added:

| Check | Where | Catches |
|-------|-------|---------|
| `.signature.p7s` smoke test | `build_sign_native` (macOS/Windows) | Signing failures in native build |
| Completeness validation | `azure-pipelines.yml` after copy | Missing nupkgs (build failures, glob issues) |
| `dotnet nuget verify --all` | `BuildAndTest.yml` after sign | Unsigned packages (especially Linux) |
| Size >5MB | `verify-cli-tool-nupkg.ps1` | Managed fallback instead of NativeAOT |
| PE/ELF/Mach-O header check | `verify-cli-tool-nupkg.ps1` | Wrong binary format |
| Functional tests | `verify-cli-tool-nupkg.ps1` (3 RIDs) | Runtime crashes, bad binary |

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| First Windows run in build_sign_native | 🔴 HIGH | Validation steps surface failures immediately. Monitor first run. |
| MicroBuild doesn't sign pre-copied Linux nupkgs | 🟡 MED | `dotnet nuget verify` final gate. Fallback: enable `codeSign: true` for Linux. |
| Double-sign corrupts macOS/Windows nupkgs | 🟡 MED | `dotnet nuget verify` confirms final state is valid regardless. |
| win-arm64 PE validation weak (MZ only) | 🟢 LOW | Pre-existing across all cross-compiled RIDs. Not introduced here. |
| `_PackCliTool` adds ~30-60s per native job | 🟢 LOW | Acceptable trade-off for uniform build path. |

## Monitoring: First Pipeline Run

1. Windows `build_sign_native` job completes without pool/container errors
2. Tool nupkg verify passes with `-VerifySignature` on macOS/Windows
3. Completeness validation finds all 8 nupkgs (7 RID + 1 pointer)
4. `dotnet nuget verify` passes for ALL nupkgs including Linux
5. Final artifact sizes are consistent with previous builds (~30-50MB per RID nupkg)
6. Pointer package contains correct dependency references
7. `aspire --version` functional test passes on win-x64

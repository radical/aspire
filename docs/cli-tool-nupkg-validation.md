# Build-Time Validation for CLI Tool Nupkg Pipeline

## Problem

Moving Windows CLI NativeAOT builds from the `BuildAndTest` Windows job into
`build_sign_native.yml` introduces risks around unsigned packages, missing nupkgs,
and untested code paths. This document describes the build-time checks added to
catch those problems, and the signing flow they validate.

## Signing Flow

```
build_sign_native stage:
  macOS  (codeSign:true)  → tool nupkgs SIGNED (MicroBuild via --sign)
  Windows (codeSign:true) → tool nupkgs SIGNED (MicroBuild via --sign)
  Linux  (codeSign:false) → tool nupkgs UNSIGNED (no --sign, ELF needs no signing)

build stage (BuildAndTest Windows):
  1. Download ALL tool nupkgs from native_archives_*
  2. Copy to Shipping/ (mix of signed macOS/Windows + unsigned Linux)
  3. Main build with -sign → MicroBuild sweeps Shipping/, signs unsigned Linux nupkgs
  4. ← This is where Linux nupkgs get signed
```

The risk: if MicroBuild only signs items produced by the current build (not
pre-copied files), Linux nupkgs ship unsigned. The `dotnet nuget verify` gate
catches this.

## Validation Checks Added

### 1. Signature smoke test — `verify-cli-tool-nupkg.ps1 -VerifySignature`

- Opens nupkg as zip, checks for `.signature.p7s` entry
- Validates both the RID-specific nupkg and the pointer package
- Invoked by `build_sign_native.yml` when `codeSign: true` (macOS, Windows)
- Linux skips this — signing is deferred to BuildAndTest

### 2. Completeness validation — `azure-pipelines.yml` + `azure-pipelines-unofficial.yml`

- Runs after tool nupkgs are copied to Shipping/
- Discovers expected RIDs dynamically from `eng/clipack/Aspire.Cli.*.csproj`
- Verifies each RID-specific nupkg exists + exactly 1 pointer package
- Fails with detailed diagnostics listing any missing packages

### 3. Authoritative signature gate — `BuildAndTest.yml`

- Runs `dotnet nuget verify --all` on every `Aspire.Cli.*.nupkg` in Shipping/
- Condition: `_SignType == 'real'` (only on signed builds)
- This is the final gate — catches unsigned Linux nupkgs and any signing failures

### 4. `-VerifySignature` wiring — `build_sign_native.yml`

- Passes `-VerifySignature` to the verify script only when `codeSign` is `True`
- No signature checks on unsigned builds (Linux) — avoids false negatives

## Validation Checklist

### Fixed (converted to build-time checks)

| Risk | Issue | Build-time check |
|------|-------|------------------|
| 🔴 HIGH | Linux tool nupkgs produced unsigned | `dotnet nuget verify` in BuildAndTest catches this |
| 🔴 HIGH | First Windows run in build_sign_native | All new validation steps surface failures immediately |
| 🟡 MED | Double-sign concern (macOS/Windows re-processed by BuildAndTest -sign) | `dotnet nuget verify` confirms final state is valid |
| 🟡 MED | Nupkg count not validated | Completeness step checks all expected RIDs + pointer |
| 🟡 MED | _PackCliTool timing (nupkgs produced before signing) | `-VerifySignature` smoke test confirms timing is correct |

### Already OK

| Risk | Item |
|------|------|
| 🟢 LOW | Pointer package dedup (win-x64 only keeps it) |
| 🟢 LOW | Tool.csproj excluded via Build.props when SkipNativeBuild=true |
| 🟢 LOW | Functional tests gated correctly (win-x64, osx-arm64, linux-x64) |
| 🟢 LOW | Size check >5MB for NativeAOT nupkgs |
| 🟢 LOW | Native .zip archive verification on win-x64 |

### Known / Accepted

| Risk | Item |
|------|------|
| 🟢 LOW | win-arm64 PE validation only checks MZ header, not ARM64 machine type (pre-existing) |
| 🟢 LOW | _PackCliTool runs 2 restores per clipack job (+30-60s, acceptable) |

## Monitoring: First Pipeline Run

Watch for:

1. Windows `build_sign_native` job completes without pool/container errors
2. `🟣Verify CLI tool nupkg` steps pass with `-VerifySignature` on macOS/Windows
3. `🟣Validate CLI tool nupkg completeness` finds all 8 nupkgs (7 RID + 1 pointer)
4. `🟣Verify CLI tool nupkg signatures` — ALL nupkgs pass `dotnet nuget verify`
   - If Linux nupkgs fail here, MicroBuild isn't sweeping pre-copied files
   - Fix: either enable `codeSign: true` for Linux, or add an explicit signing step

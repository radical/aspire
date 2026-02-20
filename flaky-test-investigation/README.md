# Flaky Test Investigation

## Test
- **Method**: `Aspire.Hosting.Azure.Tests.AzureDeployerTests.DeployAsync_WithMultipleComputeEnvironments_Works`
- **Issue**: https://github.com/dotnet/aspire/issues/13287
- **Project**: `tests/Aspire.Hosting.Azure.Tests/`

## Failure Data

| OS | Failure Rate (quarantine) |
|---|---|
| Linux | ~53% |
| macOS | ~58% |
| Windows | ~84% |
| Overall | ~99.4% |

## Error Message
```
Assert.Contains() Failure: Filter not matched in collection
Collection: [Tuple ("acaregistry.azurecr.io", "00000000-0000-0000-0000-000000000000", "fake-refresh-token")]
```

The test expects TWO entries in `FakeContainerRuntime.LoginToRegistryCalls` (one for `acaregistry.azurecr.io` and one for `aasregistry.azurecr.io`), but only ONE entry is present when the assertion runs.

## Status
- [x] Reproduced on CI
- [x] Root cause identified
- [x] Fix applied
- [x] Fix verified on CI
- [x] CI config reset

## Root Cause

`FakeContainerRuntime` and `MockImageBuilder` use `List<T>` for tracking concurrent calls
(`LoginToRegistryCalls`, `PushImageCalls`, `BuildImageResources`). The deployment pipeline
runs ACR logins and image pushes for multiple compute environments concurrently. Two
concurrent `List<T>.Add()` calls race, causing one item to be lost.

## Fix

Replaced `List<T>` with `ConcurrentBag<T>` for the properties that are accessed concurrently:
- `FakeContainerRuntime.LoginToRegistryCalls`
- `FakeContainerRuntime.PushImageCalls`
- `MockImageBuilder.PushImageCalls`
- `MockImageBuilder.BuildImageResources`

## Verification

| Run | Config | Result |
|-----|--------|--------|
| Pre-fix (22209352679) | 3 runners × 3 iters × 2 OSes | 5 failures on Windows ❌ |
| Post-fix (22209506059) | 3 runners × 3 iters × 2 OSes | All passed ✅ |

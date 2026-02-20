# Flaky Test Investigation: DeployAsync_WithMultipleComputeEnvironments_Works

## Test Info
- **Method**: `Aspire.Hosting.Azure.Tests.AzureDeployerTests.DeployAsync_WithMultipleComputeEnvironments_Works`
- **Issue**: https://github.com/dotnet/aspire/issues/13287
- **Project**: `tests/Aspire.Hosting.Azure.Tests/`
- **Status**: ✅ Fixed

## Failure Data (from quarantine tracking)

| OS | Failure Rate (last 100 runs) |
|---|---|
| Windows | 84/100 (84%) |
| Linux | 47/100 (47%) |
| macOS | 42/100 (42%) |

**Overall failure rate**: 99.4% — nearly guaranteed to fail per run.

## Investigation Timeline

### Attempt 1: Reproduce on CI (no fix)

**Config**: 5 runners × 5 iterations × 2 OSes (Linux + Windows) = 50 runs

**Result**: 5/5 Windows runners failed ❌, 5/5 Linux runners passed ✅

**Error from Windows logs**:
```
Assert.Contains() Failure: Filter not matched in collection
Collection: [Tuple ("aasregistry.azurecr.io", "00000000-...", "fake-refresh-token")]
```
The test expects two registry logins (`acaregistry` + `aasregistry`) but only one appears.

**Key finding**: Windows CI logs are UTF-16LE encoded — needed `iconv -f UTF-16LE -t UTF-8` to read.

### Root Cause Analysis

The deploy pipeline in `AzureDeployer` processes multiple compute environments concurrently (ACA + AAS). Each calls `LoginToRegistryAsync` on `FakeContainerRuntime` from separate threads.

`FakeContainerRuntime.LoginToRegistryCalls` was a `List<T>` — **not thread-safe**. Concurrent `List.Add()` calls race and silently drop items.

This is a **test infrastructure bug**, not a production bug. The production code is correct.

**Affected file**: `tests/Aspire.Hosting.Tests/Publishing/FakeContainerRuntime.cs`
```csharp
// BEFORE — NOT thread-safe
public List<(string, string, string)> LoginToRegistryCalls { get; } = [];

// AFTER — thread-safe
public ConcurrentBag<(string, string, string)> LoginToRegistryCalls { get; } = [];
```

### Attempt 2: Verify fix on CI

**Config**: 5 runners × 5 iterations × 2 OSes = 50 runs

**Result**: All 14 jobs passed, 0 failures ✅

## Fix Summary

**Files changed**:
- `tests/Aspire.Hosting.Tests/Publishing/FakeContainerRuntime.cs`: `List<T>` → `ConcurrentBag<T>` for all 5 call-tracking collections
- `tests/Aspire.Hosting.Tests/Publishing/ResourceContainerImageManagerTests.cs`: `BuildImageCalls[0]` → `BuildImageCalls.Single()` (ConcurrentBag doesn't support indexing)

**PR**: https://github.com/dotnet/aspire/pull/14586

## Lessons Learned

1. Windows logs are UTF-16LE — use `iconv` to convert
2. `ConcurrentBag` doesn't support indexing — use `.Single()` or `.First()`
3. QuarantineTools may remove `using` directives still needed by other attributes — always build-verify after unquarantining
4. Thread-unsafe collections in test fakes are a common root cause pattern — any test fake tracking concurrent calls with `List<T>` is suspect

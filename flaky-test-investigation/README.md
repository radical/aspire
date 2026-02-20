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
- [ ] Reproduced on CI
- [ ] Root cause identified
- [ ] Fix applied
- [ ] Fix verified on CI
- [ ] CI config reset

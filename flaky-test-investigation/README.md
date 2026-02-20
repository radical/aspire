# Flaky Test Investigation

## Test
- **Method**: `Aspire.Hosting.Tests.SlimTestProgramTests.TestProjectStartsAndStopsCleanly`
- **Issue**: https://github.com/dotnet/aspire/issues/9672
- **Project**: `tests/Aspire.Hosting.Tests/`

## Failure Data
| OS | Failure Rate (last 100) |
|---|---|
| Linux | 16% |
| Windows | 16% |
| macOS | 0% |
| Overall | 23.1% |

## Error
```
Collection fixture type 'Aspire.Hosting.Tests.SlimTestProgramFixture' threw in InitializeAsync
---- System.Threading.Tasks.TaskCanceledException : A task was canceled.
   at SlimTestProgramFixture.WaitReadyStateAsync
   at TestProgramFixture.InitializeAsync
```

## Status
- [ ] Reproduce on CI
- [ ] Analyze failure logs
- [ ] Apply fix
- [ ] Verify fix on CI
- [ ] Clean up

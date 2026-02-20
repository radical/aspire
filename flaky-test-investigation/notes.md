# Investigation Notes

## Hypothesis

The `FakeContainerRuntime` class uses `List<T>` for all call tracking collections:
- `LoginToRegistryCalls` — `List<(string, string, string)>`
- `PushImageCalls` — `List<IResource>`
- `TagImageCalls`, `RemoveImageCalls`, `BuildImageCalls` — all `List<T>`

`List<T>.Add()` is NOT thread-safe. If the deployment pipeline runs ACR logins
for multiple compute environments concurrently (aca-env and aas-env), two
concurrent `Add()` calls can race, causing one item to be lost.

This matches the "Thread-unsafe collections" pattern from the flaky test patterns table.

## Key Observations
- Error shows collection has only 1 of 2 expected entries
- Very high failure rate (~99.4%) suggests a fundamental concurrency issue
- Fails on all OSes but more on Windows (typically worse for race conditions)

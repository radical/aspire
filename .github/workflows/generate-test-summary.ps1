# This script parses a .trx file and generates a summary of failed tests in GitHub Markdown format.

param (
    [string]$TrxFilePath,
    [string]$OutputFilePath = "test-summary.md"
)

if (-not (Test-Path $TrxFilePath)) {
    Write-Error "The specified .trx file does not exist: $TrxFilePath"
    exit 1
}

# Load the .trx file as XML
[xml]$trx = Get-Content $TrxFilePath

# Extract test run summary
$totalTestCount = $trx.TestRun.ResultSummary.Counters.total
$passedTestCount = $trx.TestRun.ResultSummary.Counters.passed
$failedTestCount = $trx.TestRun.ResultSummary.Counters.failed
$skippedTestCount = $trx.TestRun.ResultSummary.Counters.executed - $passedTestCount - $failedTestCount

# Parse start and finish times as DateTime
$startTime = [DateTime]::Parse($trx.TestRun.Times.start)
$finishTime = [DateTime]::Parse($trx.TestRun.Times.finish)
$elapsedTime = $finishTime - $startTime

# Format elapsed time as a human-readable string
$elapsedTimeFormatted = $elapsedTime.ToString("hh\:mm\:ss")

$markdown = ""

# Start building the markdown summary
$markdown += "# Test Summary`n`n"

$markdown += "<table><th width=`"99999`">✓&nbsp;&nbsp;Passed</th><th width=`"99999`">✘&nbsp;&nbsp;Failed</th><th width=`"99999`">↷&nbsp;&nbsp;Skipped</th><th width=`"99999`">∑&nbsp;&nbsp;Total</th><th width=`"99999`">⧗&nbsp;&nbsp;Elapsed</th><tr><td align=`"center`">$passedTestCount</td><td align=`"center`">$failedTestCount</td><td align=`"center`">$skippedTestCount</td><td align=`"center`">$totalTestCount</td><td align=`"center`">$elapsedTimeFormatted</td></tr></table>`n"

$markdown += "`n## Failed Tests`n`n"

$failedResults = $trx.TestRun.Results.UnitTestResult | Where-Object { $_.outcome -eq "Failed" }

$markdown += "<ul>"
foreach ($result in $failedResults) {
    $testName = $result.testName

    $markdown += "<li>
<details><summary><b>
🔴 <a href=`"https://github.com/dotnet/aspire/blob/fee34350dd7d6a436153b9c2ff889b7eadf9b8d0/tests/Aspire.Hosting.Redis.Tests/RedisFunctionalTests.cs#L471`">$testName</a> </summary>`n`n"

    $errorMsg = $result.Output.ErrorInfo.Message# -replace "\r?\n", " "

    $fullMsg = $errorMsg + $result.Output.ErrorInfo.StackTrace

    # Truncate long error messages for readability
    if ($fullMsg.Length -gt 50000) {
        $fullMsg = $fullMsg.Substring(0, 50000) + "..."
    }

    $markdown += "``````yml`n$fullMsg`n```````n`n"
    $markdown += "</li>"
}

# Write the markdown to the output file
Set-Content -Path $OutputFilePath -Value $markdown

Write-Output "Test summary generated at: $OutputFilePath for $TrxFilePath"

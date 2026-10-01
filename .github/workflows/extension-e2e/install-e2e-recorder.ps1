# ffmpeg only powers diagnostic screen recordings, and run-e2e.js already skips
# recording with a warning when the binary is missing. This script must never
# fail or stall a shard.
if (Get-Command ffmpeg -ErrorAction SilentlyContinue)
{
    ffmpeg -version | Select-Object -First 1
}
else
{
    $installed = $false
    $attempts = 2

    for ($attempt = 1; $attempt -le $attempts; $attempt++)
    {
        # Plain `timeout` only sends SIGTERM, which apt/dpkg can defer around
        # critical sections. Escalate to SIGKILL so the child cannot outlive
        # the bounded installation attempt.
        sudo timeout --kill-after=30 180 apt-get update

        # Only the install decides success. A timed-out update can still leave
        # enough package metadata on the runner image for installation.
        sudo timeout --kill-after=30 180 apt-get install -y --no-install-recommends ffmpeg
        if ($LASTEXITCODE -eq 0)
        {
            $installed = $true
            break
        }

        if ($attempt -lt $attempts)
        {
            Write-Host "::warning::Attempt $attempt to install ffmpeg failed; retrying."
            Start-Sleep -Seconds 15
        }
    }

    if (-not $installed)
    {
        Write-Host '::warning::ffmpeg could not be installed; E2E screen recordings are disabled for this shard.'
    }
}

# GitHub's pwsh wrapper propagates a leftover native-process exit code. Hold
# the diagnostic-only contract explicitly after a failed apt-get attempt.
# https://docs.github.com/actions/reference/workflows-and-actions/workflow-syntax#exit-codes-and-error-action-preference
exit 0

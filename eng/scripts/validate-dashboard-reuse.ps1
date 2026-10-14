param(
    [Parameter(Mandatory)]
    [ValidateSet('Capture', 'Verify', 'VerifySaved', 'Guard', 'Smoke', 'Compare')]
    [string]$Stage,
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Rid = 'win-x64',
    [string]$Configuration = 'Release',
    [switch]$Reuse
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Set-Location $repo
$evidence = Join-Path $repo 'artifacts/log/dashboard-reuse'
$null = New-Item -ItemType Directory -Force -Path $evidence
$publish = Join-Path $repo "artifacts/bin/Aspire.Dashboard/$Rid/$Configuration/net11.0/$Rid/publish"
$sdkVersion = (Get-Content global.json -Raw | ConvertFrom-Json).sdk.version
$sdk = Join-Path $repo ".dotnet/sdk/$sdkVersion"

function Get-Manifest([string]$Directory) {
    # The SDK's netframework-compatible copy also loads in the runner's PowerShell runtime.
    Add-Type -Path (Join-Path $sdk 'Sdks/Microsoft.Build.Tasks.Git/tools/netframework/System.IO.Hashing.dll')
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        throw "Manifest directory is missing: $Directory"
    }
    $root = (Resolve-Path -LiteralPath $Directory).Path
    @(
        Get-ChildItem -LiteralPath $root -Recurse -File -Force | ForEach-Object {
            $hash = [System.IO.Hashing.XxHash3]::new()
            $stream = [IO.File]::OpenRead($_.FullName)
            try {
                $hash.Append($stream)
                [pscustomobject]@{
                    path = [IO.Path]::GetRelativePath($root, $_.FullName).Replace('\', '/')
                    length = $_.Length
                    xxh3 = [Convert]::ToHexString($hash.GetCurrentHash())
                }
            }
            finally {
                $stream.Dispose()
            }
        } | Sort-Object path
    )
}

function Save-Manifest([object[]]$Manifest, [string]$Name) {
    ConvertTo-Json -InputObject @($Manifest) -Depth 5 | Set-Content (Join-Path $evidence $Name)
}

function Compare-Manifests([object[]]$Expected, [object[]]$Actual) {
    @(Compare-Object -ReferenceObject @($Expected) -DifferenceObject @($Actual) -Property path, length, xxh3)
}

function Get-PayloadManifest([object[]]$Manifest) {
    # Production dashboardpack and CreateLayout intentionally exclude only these debug symbols.
    @($Manifest | Where-Object { $_.path -notmatch '(^|/)[^/]+\.dSYM/|\.(pdb|dbg)$' })
}

function Assert-NativeMachine([string]$Directory) {
    $expected = if ($Rid -eq 'win-arm64') { 0xAA64 } else { 0x8664 }
    foreach ($file in @('Aspire.Dashboard.exe', 'e_sqlite3.dll')) {
        $reader = [IO.BinaryReader]::new([IO.File]::OpenRead((Join-Path $Directory $file)))
        try {
            $reader.BaseStream.Position = 0x3C
            $offset = $reader.ReadUInt32()
            $reader.BaseStream.Position = $offset
            if ($reader.ReadUInt32() -ne 0x4550 -or $reader.ReadUInt16() -ne $expected) {
                throw "$file is not the expected native PE machine for $Rid."
            }
        }
        finally {
            $reader.Dispose()
        }
    }
}

switch ($Stage) {
    'VerifySaved' {
        $identity = Get-Content (Join-Path $evidence 'identity.json') -Raw | ConvertFrom-Json
        if ($identity.head -ne 'b4e053c2be8049823cf96303882d3360a3c30ebc' -or $identity.rid -ne $Rid -or $identity.configuration -ne $Configuration -or $identity.reuse -ne $Reuse.IsPresent) {
            throw 'Saved payload identity does not match the approved original experiment.'
        }
        $archive = Join-Path $repo "artifacts/bundle/aspire-ci-bundlepayload-$Rid.tar.gz"
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            throw "Original bundle payload archive is missing: $archive"
        }
        $extracted = Join-Path $evidence 'bundle-extracted'
        $null = New-Item -ItemType Directory -Force -Path $extracted
        & tar -xzf $archive -C $extracted
        if ($LASTEXITCODE -ne 0) { throw 'Could not extract the original bundle payload archive.' }
        $bundleDashboard = Join-Path $extracted "$Rid/dashboard"
        Assert-NativeMachine $bundleDashboard
        $bundle = @(Get-Manifest $bundleDashboard)
        $recorded = @(Get-Content (Join-Path $evidence 'final-dashboard.json') -Raw | ConvertFrom-Json)
        if ((Compare-Manifests $recorded $bundle).Count -ne 0) {
            throw 'Extracted original Dashboard differs from its original recorded complete content manifest.'
        }
        $first = @(Get-Content (Join-Path $evidence 'first-publish.json') -Raw | ConvertFrom-Json)
        $package = @(Get-Content (Join-Path $evidence 'first-package.json') -Raw | ConvertFrom-Json)
        $actualPackage = @(Get-Manifest (Join-Path $evidence 'dashboard-package/tools'))
        if ((Compare-Manifests $package $actualPackage).Count -ne 0) {
            throw 'Retained first Dashboard package payload differs from its original manifest.'
        }
        $cli = @(Get-Manifest (Join-Path $evidence 'cli-extracted'))
        $recordedCli = @(Get-Content (Join-Path $evidence 'final-cli.json') -Raw | ConvertFrom-Json)
        if ((Compare-Manifests $recordedCli $cli).Count -ne 0) {
            throw 'Retained native CLI payload differs from its original manifest.'
        }
        if ($Reuse -and ((Compare-Manifests (Get-PayloadManifest $first) $bundle).Count -ne 0 -or (Compare-Manifests $package $bundle).Count -ne 0)) {
            throw 'Reused original Dashboard differs from its first publish or package manifest.'
        }
        Save-Manifest (Get-Manifest (Join-Path $extracted $Rid)) 'complete-bundle.json'
        [pscustomobject]@{
            payloadSourceCommit = $identity.head
            originalRun = '38630516871'
            harnessCommit = (& git --no-pager rev-parse HEAD)
            rid = $Rid
            reuse = $Reuse.IsPresent
            dashboardVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $bundleDashboard 'Aspire.Dashboard.exe')).ProductVersion
            limitation = 'Original raw shipping archive containers are unavailable. Unpacked native CLI and first Dashboard package payloads and their original manifests are retained.'
        } | ConvertTo-Json | Set-Content (Join-Path $evidence 'validation-identity.json')
        "ASPIRE_NATIVE_DASHBOARD_PATH=$(Join-Path $bundleDashboard 'Aspire.Dashboard.exe')" >> $env:GITHUB_ENV
    }
    'Capture' {
        Assert-NativeMachine $publish
        Save-Manifest (Get-Manifest $publish) 'first-publish.json'
        [pscustomobject]@{
            base = 'd751daa037a850428cd12ebf1efe309a8690df10'
            head = (& git --no-pager rev-parse HEAD)
            rid = $Rid
            configuration = $Configuration
            reuse = $Reuse.IsPresent
            runner = $env:RUNNER_NAME
            architecture = [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
            sdk = $sdkVersion
            productVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $publish 'Aspire.Dashboard.exe')).ProductVersion
        } | ConvertTo-Json | Set-Content (Join-Path $evidence 'identity.json')
    }
    'Guard' {
        $held = Join-Path $evidence 'held-first-publish'
        Move-Item -LiteralPath $publish -Destination $held
        try {
            $arguments = @('msbuild', 'eng/Bundle.proj', '/t:_ValidatePrebuiltDashboard',
                "/p:TargetRid=$Rid", "/p:Configuration=$Configuration", '/p:ReuseDashboardPublish=true')
            $output = & ./dotnet.cmd @arguments 2>&1
            $exitCode = $LASTEXITCODE
            $output | Set-Content (Join-Path $evidence 'missing-prebuilt.log')
            if ($exitCode -eq 0 -or "$output" -notlike '*Prebuilt Dashboard executable is missing:*') {
                throw "Missing prebuilt Dashboard did not produce the expected build failure: $output"
            }
            # The opt-in guard must not change standalone/default bundle semantics.
            & ./dotnet.cmd msbuild eng/Bundle.proj /t:_ValidatePrebuiltDashboard `
                "/p:TargetRid=$Rid" "/p:Configuration=$Configuration" /p:ReuseDashboardPublish=false
            if ($LASTEXITCODE -ne 0) {
                throw 'Default bundle mode unexpectedly requires a prebuilt Dashboard.'
            }
        }
        finally {
            Move-Item -LiteralPath $held -Destination $publish
        }
    }
    'Verify' {
        $first = @(Get-Content (Join-Path $evidence 'first-publish.json') -Raw | ConvertFrom-Json)
        $after = @(Get-Manifest $publish)
        Save-Manifest $after 'after-publish.json'
        $changes = Compare-Manifests $first $after
        ConvertTo-Json -InputObject $changes -Depth 5 | Set-Content (Join-Path $evidence 'first-vs-after.json')
        if ($Reuse -and $changes.Count -ne 0) {
            throw "Candidate modified first Dashboard output: $($changes | ConvertTo-Json -Depth 5)"
        }

        $extracted = Join-Path $evidence 'bundle-extracted'
        $null = New-Item -ItemType Directory -Force -Path $extracted
        & tar -xzf "artifacts/bundle/aspire-ci-bundlepayload-$Rid.tar.gz" -C $extracted
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not extract the actual bundle payload archive.'
        }
        $bundleDashboard = Join-Path $extracted "$Rid/dashboard"
        Assert-NativeMachine $bundleDashboard
        $bundle = @(Get-Manifest $bundleDashboard)
        Save-Manifest $bundle 'final-dashboard.json'
        $changes = Compare-Manifests (Get-PayloadManifest $after) $bundle
        if ($changes.Count -ne 0) {
            throw "Bundle differs from selected Dashboard publish: $($changes | ConvertTo-Json -Depth 5)"
        }

        $packages = @(Get-ChildItem "artifacts/packages/$Configuration/Shipping" -Filter "Aspire.Dashboard.Sdk.$Rid.*.nupkg")
        if ($packages.Count -ne 1) {
            throw "Expected one first Dashboard package, found $($packages.Count)."
        }
        $packageDirectory = Join-Path $evidence 'dashboard-package'
        [IO.Compression.ZipFile]::ExtractToDirectory($packages[0].FullName, $packageDirectory)
        $package = @(Get-Manifest (Join-Path $packageDirectory 'tools'))
        Save-Manifest $package 'first-package.json'
        $changes = Compare-Manifests (Get-PayloadManifest $first) $package
        if ($changes.Count -ne 0) {
            throw "First Dashboard package differs from first publish: $($changes | ConvertTo-Json -Depth 5)"
        }
        $changes = Compare-Manifests $package $bundle
        ConvertTo-Json -InputObject $changes -Depth 5 | Set-Content (Join-Path $evidence 'package-vs-bundle.json')
        if ($Reuse -and $changes.Count -ne 0) {
            throw "Reused bundle differs from first Dashboard package: $($changes | ConvertTo-Json -Depth 5)"
        }

        $archives = @(Get-ChildItem "artifacts/packages/$Configuration" -Filter "aspire-cli-$Rid-*.zip" -Recurse -File)
        if ($archives.Count -ne 1) {
            throw "Expected one CLI archive, found $($archives.Count)."
        }
        $cliDirectory = Join-Path $evidence 'cli-extracted'
        [IO.Compression.ZipFile]::ExtractToDirectory($archives[0].FullName, $cliDirectory)
        Save-Manifest (Get-Manifest $cliDirectory) 'final-cli.json'
        Save-Manifest (Get-Manifest "artifacts/packages/$Configuration/Shipping") 'raw-shipping.json'
        [pscustomobject]@{
            dashboardVersion = [Diagnostics.FileVersionInfo]::GetVersionInfo((Join-Path $bundleDashboard 'Aspire.Dashboard.exe')).ProductVersion
            cliVersion = (& (Join-Path $cliDirectory 'aspire.exe') --version)
        } | ConvertTo-Json | Set-Content (Join-Path $evidence 'final-identity.json')
        "ASPIRE_NATIVE_DASHBOARD_PATH=$(Join-Path $bundleDashboard 'Aspire.Dashboard.exe')" >> $env:GITHUB_ENV
    }
    'Smoke' {
        $dashboard = $env:ASPIRE_NATIVE_DASHBOARD_PATH
        if (-not (Test-Path -LiteralPath $dashboard -PathType Leaf)) {
            throw 'ASPIRE_NATIVE_DASHBOARD_PATH must be the extracted bundle Dashboard executable.'
        }
        $smoke = Join-Path $evidence 'native-smoke'
        $data = Join-Path $smoke 'data'
        $null = New-Item -ItemType Directory -Force -Path $data
        $stdout = Join-Path $smoke 'stdout.log'
        $stderr = Join-Path $smoke 'stderr.log'
        $process = Start-Process -FilePath $dashboard -PassThru `
            -WorkingDirectory (Split-Path $dashboard -Parent) `
            -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -ArgumentList @('--ASPNETCORE_URLS=http://127.0.0.1:0',
                '--ASPIRE_DASHBOARD_UNSECURED_ALLOW_ANONYMOUS=true',
                '--Dashboard:Otlp:HttpEndpointUrl=http://127.0.0.1:0',
                '--Dashboard:Otlp:GrpcEndpointUrl=http://127.0.0.1:0',
                '--Dashboard:Otlp:AuthMode=Unsecured',
                '--Dashboard:Api:AuthMode=Unsecured',
                '--Dashboard:Data:PersistenceMode=Run', "--Dashboard:Data:Directory=$data")
        try {
            $deadline = [DateTime]::UtcNow.AddMinutes(1)
            $frontend = $null
            $otlp = $null
            while ([DateTime]::UtcNow -lt $deadline) {
                if ($process.HasExited) {
                    throw "Bundled Dashboard exited: $(Get-Content $stderr -Raw)"
                }
                # Kestrel reports ephemeral endpoints as:
                #   Now listening on: http://127.0.0.1:54321
                #   OTLP/HTTP listening on: http://127.0.0.1:54322
                $log = Get-Content $stdout -Raw
                if ($log -match '(?m)^\s*Now listening on:\s+(http://\S+)') { $frontend = $Matches[1] }
                if ($log -match '(?m)^\s*OTLP/HTTP listening on:\s+(http://\S+)') { $otlp = $Matches[1] }
                if ($frontend -and $otlp) { break }
                Start-Sleep -Milliseconds 250
            }
            if (-not $frontend -or -not $otlp) {
                throw 'Bundled Dashboard did not expose frontend and OTLP endpoints.'
            }
            foreach ($path in @('/', '/_framework/blazor.web.js')) {
                $response = Invoke-WebRequest "$frontend$path" -TimeoutSec 15
                if ($response.StatusCode -ne 200 -or $response.RawContentLength -eq 0) {
                    throw "Bundled Dashboard did not serve $path."
                }
            }
            # Match the OTLP/JSON shape covered by Integration/OtlpHttpJsonTests.cs.
            $start = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000).ToString()
            $finish = ([long]$start + 1000000).ToString()
            $body = @{
                resourceSpans = @(@{
                    resource = @{ attributes = @(@{ key = 'service.name'; value = @{ stringValue = 'dashboard-reuse-validation' } }) }
                    scopeSpans = @(@{
                        scope = @{ name = 'dashboard-reuse-validation' }
                        spans = @(@{ traceId = '5B8EFFF798038103D269B633813FC60C'; spanId = 'EEE19B7EC3C1B174'
                            name = 'dashboard-reuse-span'; startTimeUnixNano = $start; endTimeUnixNano = $finish; kind = 2 })
                    })
                })
            } | ConvertTo-Json -Depth 12
            $null = Invoke-RestMethod "$otlp/v1/traces" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 15
            $found = $false
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            while ([DateTime]::UtcNow -lt $deadline) {
                $response = Invoke-WebRequest "$frontend/api/telemetry/spans" -TimeoutSec 5
                if ($response.Content -like '*dashboard-reuse-span*') {
                    $response.Content | Set-Content (Join-Path $smoke 'stored-span.json')
                    $found = $true
                    break
                }
                Start-Sleep -Milliseconds 250
            }
            if (-not $found) { throw 'Bundled Dashboard did not persist and return the submitted OTLP span.' }
            if (@(Get-ChildItem $data -Recurse -File -Filter '*.db').Count -eq 0) {
                throw 'Bundled Dashboard did not create its SQLite database.'
            }
            Write-Host 'Bundled native Dashboard HTTP, static assets, SQLite and telemetry smoke passed.'
        }
        finally {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force
                $process.WaitForExit()
            }
        }
    }
    'Compare' {
        $different = $false
        foreach ($platform in @('win-x64', 'win-arm64')) {
            foreach ($name in @('first-publish.json', 'final-dashboard.json', 'final-cli.json')) {
                $baselineFile = @(Get-ChildItem "artifacts/reuse-comparison/dashboard-reuse-evidence-$platform-baseline" -Recurse -File -Filter $name)
                $candidateFile = @(Get-ChildItem "artifacts/reuse-comparison/dashboard-reuse-evidence-$platform-candidate" -Recurse -File -Filter $name)
                if ($baselineFile.Count -ne 1 -or $candidateFile.Count -ne 1) {
                    throw "Expected one $name manifest per variant for $platform."
                }
                $baseline = @(Get-Content $baselineFile[0].FullName -Raw | ConvertFrom-Json)
                $candidate = @(Get-Content $candidateFile[0].FullName -Raw | ConvertFrom-Json)
                $changes = Compare-Manifests $baseline $candidate
                ConvertTo-Json -InputObject $changes -Depth 5 | Set-Content (Join-Path $evidence "$platform-$name")
                Write-Host "$platform $name byte-content differences: $($changes.Count)"
                $different = $different -or $changes.Count -ne 0
            }
            foreach ($identityFile in Get-ChildItem artifacts/reuse-comparison -Recurse -File -Filter '*identity.json') {
                Write-Host "$($identityFile.FullName): $(Get-Content $identityFile.FullName -Raw)"
            }
        }
        if ($different) {
            Write-Warning 'Baseline and candidate payload bytes differ. Runtime success must not be reported as artifact equivalence.'
        }
        "## Dashboard reuse experiment`n`nPayload byte-equivalent: $(-not $different). Runtime success is a separate result; this experiment does not approve shipping changes." >> $env:GITHUB_STEP_SUMMARY
    }
}

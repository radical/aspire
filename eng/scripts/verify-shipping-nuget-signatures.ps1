<#
.SYNOPSIS
Verify every release NuGet package after final Shipping staging.
#>
param(
  [Parameter(Mandatory = $true)]
  [string]$ShippingDir
)

$ErrorActionPreference = 'Stop'

# Arcade stages native symbol blobs alongside release packages, but publishes
# them through the symbol-upload route rather than the release NuGet feed.
$packages = @(Get-ChildItem -LiteralPath $ShippingDir -Filter '*.nupkg' -File -Recurse |
  Where-Object { -not $_.Name.EndsWith('.symbols.nupkg', [StringComparison]::OrdinalIgnoreCase) })
if ($packages.Count -eq 0) {
  throw "No shipping NuGet packages found for signature verification in '$ShippingDir'."
}

$failures = [System.Collections.Generic.List[string]]::new()
foreach ($package in $packages) {
  & dotnet nuget verify `
    $package.FullName `
    --all
  if ($LASTEXITCODE -ne 0) {
    $failures.Add($package.FullName)
  }
}
if ($failures.Count -gt 0) {
  throw "NuGet signature verification failed for $($failures.Count) shipping package(s): $($failures -join ', ')"
}

Write-Host "Verified signatures for all $($packages.Count) shipping NuGet packages."

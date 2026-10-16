param(
    [switch]$Apply
)

$ErrorActionPreference = "Continue"
$results = @()

function Get-TreeBytes([string]$Path) {
    [int64](
        (Get-ChildItem -LiteralPath $Path -Force -File -Recurse -ErrorAction SilentlyContinue |
            Measure-Object Length -Sum).Sum
    )
}

function Assert-ChildPath([string]$Root, [string]$Target) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd("\")
    $targetPath = [IO.Path]::GetFullPath($Target)
    if (-not $targetPath.StartsWith($rootPath + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe cleanup target: $targetPath"
    }
    $targetPath
}

function Remove-SafeTree([string]$Root, [System.IO.DirectoryInfo]$Item) {
    $target = Assert-ChildPath $Root $Item.FullName
    $bytes = Get-TreeBytes $target
    if ($Apply) {
        Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue
    }
    $script:results += [pscustomobject]@{
        Target = $target
        Status = if (-not $Apply) { "preview" } elseif (Test-Path -LiteralPath $target) { "partial-or-locked" } else { "removed" }
        Bytes = $bytes
    }
}

function Remove-SafeFiles([string]$Root, [double]$AgeDays) {
    if (-not (Test-Path -LiteralPath $Root)) { return }
    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path.TrimEnd("\")
    $threshold = (Get-Date).AddDays(-$AgeDays)
    $files = @(
        Get-ChildItem -LiteralPath $resolvedRoot -Force -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.LastWriteTime -lt $threshold }
    )
    [int64]$bytes = ($files | Measure-Object Length -Sum).Sum
    if ($Apply) {
        foreach ($file in $files) {
            $target = Assert-ChildPath $resolvedRoot $file.FullName
            Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        }
    }
    $script:results += [pscustomobject]@{
        Target = $resolvedRoot
        Status = if ($Apply) { "cache-files-cleaned" } else { "preview" }
        Bytes = $bytes
    }
}

$before = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace
$windowsTemp = (Resolve-Path -LiteralPath "C:\Windows\Temp").Path.TrimEnd("\")
$cutoff = (Get-Date).AddHours(-6)
$tempCandidates = Get-ChildItem -LiteralPath $windowsTemp -Force -Directory -ErrorAction SilentlyContinue |
    Where-Object {
        $_.LastWriteTime -lt $cutoff -and
        $_.Name -match "^(evox-|evomap-|puppeteer_dev_chrome_profile-)"
    }
foreach ($item in $tempCandidates) {
    Remove-SafeTree $windowsTemp $item
}

# These locations contain only reproducible download, shader, and crash caches.
# Keep _npx because active browser tooling may be executing from it.
Remove-SafeFiles "$env:LOCALAPPDATA\npm-cache\_cacache" 0
Remove-SafeFiles "$env:LOCALAPPDATA\NVIDIA\DXCache" 30
Remove-SafeFiles "$env:LOCALAPPDATA\CrashDumps" 1

$after = (Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'").FreeSpace
[pscustomobject]@{
    Applied = [bool]$Apply
    FreeBeforeBytes = [int64]$before
    FreeAfterBytes = [int64]$after
    ReclaimedBytes = [int64]($after - $before)
    Items = @($results | Sort-Object Bytes -Descending)
} | ConvertTo-Json -Depth 5

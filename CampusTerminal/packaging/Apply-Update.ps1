# SPDX-License-Identifier: GPL-3.0-or-later
param([Parameter(Mandatory=$true)][Alias('Plan')][string]$PlanPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$plan = $null
$root = $null
$stage = $null
trap {
    $message = $_.Exception.Message
    if ($stage) { try { [IO.File]::WriteAllText((Join-Path $stage 'update-error.txt'), $message, [Text.Encoding]::UTF8) } catch { } }
    if ($root -and (Test-Path -LiteralPath (Join-Path $root 'CampusTerminal.exe')) -and
        (-not $plan -or -not (Test-IdentityAlive $plan.gui_process))) {
        try { Start-Process -FilePath (Join-Path $root 'CampusTerminal.exe') -WorkingDirectory $root -WindowStyle Hidden } catch { }
    }
    exit 1
}

function Get-Canonical([string]$Path) { [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Get-Sha256Hex([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
function Test-IdentityAlive($Identity) {
    if (-not $Identity -or -not $Identity.pid) { return $false }
    $proc = Get-Process -Id ([int]$Identity.pid) -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    try {
        $started = $proc.StartTime.ToUniversalTime()
        $expected = [DateTimeOffset]::FromUnixTimeMilliseconds([long]([double]$Identity.created * 1000)).UtcDateTime
        return [Math]::Abs(($started - $expected).TotalSeconds) -lt 2
    } catch { return $false }
}
function Test-RootUnlocked([string]$Root) {
    foreach ($file in Get-ChildItem -LiteralPath $Root -File -Recurse -Force -ErrorAction SilentlyContinue) {
        $relative = $file.FullName.Substring($Root.Length).TrimStart('\')
        if ($relative -match '^(state|logs|payload|\.git)([\\/]|$)' -or $relative -eq 'edition.txt') { continue }
        try { $handle = [IO.File]::Open($file.FullName, 'Open', 'ReadWrite', 'None'); $handle.Dispose() }
        catch { return $false }
    }
    return $true
}
function Assert-NoReparseTree([string]$Path) {
    if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse point rejected: $Path" }
    foreach ($item in Get-ChildItem -LiteralPath $Path -Force -Recurse -ErrorAction Stop) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Reparse point rejected: $($item.FullName)" }
    }
}

$planPath = [IO.Path]::GetFullPath($PlanPath)
$plan = Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
$root = Get-Canonical ([string]$plan.root)
$stage = Get-Canonical (Split-Path -Parent $planPath)
if ($plan.schema -ne 1 -or $plan.edition -notin @('portable','installed')) { throw 'Unsupported update plan' }
if ((Get-Canonical ([string]$plan.helper)) -ne (Get-Canonical $PSCommandPath) -or
    (Split-Path -Parent $root) -ne (Split-Path -Parent $stage)) { throw 'Invalid staging or target path' }
if ($root -eq $stage -or (Test-Path -LiteralPath (Join-Path $root '.git'))) { throw 'Refusing unsafe target root' }
if ($plan.edition -eq 'portable' -and -not (Test-Path -LiteralPath (Join-Path $root 'CampusTerminal.portable'))) { throw 'Portable marker missing' }
if ($plan.edition -eq 'installed' -and -not (Test-Path -LiteralPath (Join-Path $root 'edition.txt'))) { throw 'Installed marker missing' }
if (-not (Test-Path -LiteralPath $plan.asset -PathType Leaf)) { throw 'Staged asset missing' }
Assert-NoReparseTree $root
$hash = Get-Sha256Hex ([string]$plan.asset)
if ($hash -ne ([string]$plan.sha256).ToLowerInvariant()) { throw 'Staged asset digest mismatch' }

$deadline = [DateTime]::UtcNow.AddSeconds([Math]::Min(600, [Math]::Max(10, [int]$plan.wait_timeout_seconds)))
do {
    if (Test-Path -LiteralPath (Join-Path $stage 'cancel-update')) { throw 'Update cancelled before target changes' }
    $alive = $false
    if (Test-IdentityAlive $plan.gui_process) { $alive = $true }
    foreach ($identity in $plan.core_processes) { if (Test-IdentityAlive $identity) { $alive = $true } }
    if (-not $alive -and (Test-RootUnlocked $root)) { break }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $deadline)
if ([DateTime]::UtcNow -ge $deadline) { throw 'Timed out waiting for GUI/core exit and file locks; no process was terminated' }
if (Test-Path -LiteralPath (Join-Path $stage 'cancel-update')) { throw 'Update cancelled before target changes' }

if ($plan.edition -eq 'installed') {
    $installer = (Resolve-Path -LiteralPath $plan.asset).Path
    $arguments = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /DIR="' + $root + '"'
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Installer failed with exit code $($process.ExitCode)" }
    Start-Process -FilePath (Join-Path $root 'CampusTerminal.exe') -WorkingDirectory $root -WindowStyle Hidden
    return
}

$payload = Get-Canonical ([string]$plan.payload)
if (-not $payload.StartsWith($stage + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath (Join-Path $payload 'CampusTerminal.portable'))) { throw 'Invalid portable payload' }
Assert-NoReparseTree $payload
$expectedFiles = @{}
foreach ($entry in $plan.payload_manifest.PSObject.Properties) { $expectedFiles[$entry.Name] = [string]$entry.Value }
$actualFiles = @(Get-ChildItem -LiteralPath $payload -File -Recurse -Force)
if ($actualFiles.Count -ne $expectedFiles.Count) { throw 'Staged payload file manifest changed' }
foreach ($file in $actualFiles) {
    $manifestPath = $file.FullName.Substring($payload.Length).TrimStart('\').Replace('\','/')
    if (-not $expectedFiles.ContainsKey($manifestPath) -or
        (Get-Sha256Hex $file.FullName) -ne $expectedFiles[$manifestPath]) {
        throw "Staged payload integrity check failed: $manifestPath"
    }
}
$backup = Join-Path $stage 'rollback'
New-Item -ItemType Directory -Force -Path $backup | Out-Null
$changed = [Collections.Generic.List[string]]::new()
try {
    foreach ($source in Get-ChildItem -LiteralPath $payload -File -Recurse -Force) {
        if (($source.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse point in staged payload' }
        $relative = $source.FullName.Substring($payload.Length).TrimStart('\')
        if ($relative -match '^(state|logs|payload)([\\/]|$)' -or $relative -eq 'edition.txt') { continue }
        if ($relative.Contains(':') -or $relative.StartsWith('\')) { throw 'Unsafe payload path' }
        $destination = Join-Path $root $relative
        $canonicalDestination = Get-Canonical $destination
        if (-not $canonicalDestination.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Payload escaped target root' }
        $parent = Split-Path -Parent $destination
        $cursor = $root
        foreach ($part in $relative.Split('\')) {
            $cursor = Join-Path $cursor $part
            if (Test-Path -LiteralPath $cursor -PathType Any) {
                if (([IO.File]::GetAttributes($cursor) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "Reparse point in target path: $cursor"
                }
            }
        }
        if (Test-Path -LiteralPath $destination -PathType Container) { throw "File path conflicts with local directory: $destination" }
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $old = Join-Path $backup $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $old) | Out-Null
            Copy-Item -LiteralPath $destination -Destination $old -Force
            $changed.Add($destination)
        } else { $changed.Add($destination) }
        Copy-Item -LiteralPath $source.FullName -Destination $destination -Force
    }
} catch {
    $rollbackOrder = $changed.ToArray()
    [Array]::Reverse($rollbackOrder)
    foreach ($destination in $rollbackOrder) {
        $relative = $destination.Substring($root.Length).TrimStart('\')
        $old = Join-Path $backup $relative
        if (Test-Path -LiteralPath $old -PathType Leaf) { Copy-Item -LiteralPath $old -Destination $destination -Force }
        elseif (Test-Path -LiteralPath $destination -PathType Leaf) { Remove-Item -LiteralPath $destination -Force }
    }
    throw
}
Start-Process -FilePath (Join-Path $root 'CampusTerminal.exe') -WorkingDirectory $root -WindowStyle Hidden

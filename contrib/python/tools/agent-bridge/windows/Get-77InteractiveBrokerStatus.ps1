#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Config
)

# SSH-side status reader. It reports whether the interactive broker heartbeat is
# fresh without exposing hostnames, Windows user names, paths, credentials or rows.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Assert-Keys($Object, [string[]] $Keys, [string] $Context) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { throw "$Context must be an object." }
    foreach ($property in $Object.PSObject.Properties) {
        if ($Keys -cnotcontains $property.Name) { throw "$Context has an unknown key." }
    }
}

function Read-ClientConfig([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Client config was not found.' }
    $configObject = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-Keys $configObject @('schemaVersion','spoolRoot','timeoutSeconds','pollMilliseconds','requestMaxBytes','responseMaxBytes','statusPath','statusStaleSeconds') 'config'
    if ($configObject.schemaVersion -cne '1.0') { throw 'Unsupported client config schema.' }
    if ([string]::IsNullOrWhiteSpace([string] $configObject.statusPath) -or
        -not [System.IO.Path]::IsPathRooted([string] $configObject.statusPath) -or
        [string] $configObject.statusPath -match '["\r\n]') {
        throw 'Status path must be an absolute local path.'
    }
    $statusStale = 0
    if (-not [int]::TryParse([string] $configObject.statusStaleSeconds, [ref] $statusStale) -or
        $statusStale -lt 10 -or $statusStale -gt 86400) {
        throw 'Status stale interval is invalid.'
    }
    return $configObject
}

function New-StatusReceipt([string] $State, $AgeSeconds, [int] $StaleAfterSeconds, [string] $FileStatus) {
    $result = [ordered]@{
        state = $State
        statusFile = $FileStatus
        lastHeartbeatAgeSeconds = $AgeSeconds
        staleAfterSeconds = $StaleAfterSeconds
    }
    return [ordered]@{
        schemaVersion = '1.0'
        operation = 'get_77_interactive_broker_status'
        status = 'ok'
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        privacy = 'sanitized-json; no hostname, Windows user name, local path, credential, accounting row or source value'
        result = $result
    }
}

function Assert-PlainStatusFile([string] $Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Status file must not be a reparse point.'
    }
    if ($item.Length -gt 32768) {
        throw 'Status file is too large.'
    }
}

try {
    $local = Read-ClientConfig $Config
    $statusPath = [string] $local.statusPath
    $staleAfterSeconds = [int] $local.statusStaleSeconds
    if (-not (Test-Path -LiteralPath $statusPath -PathType Leaf)) {
        $receipt = New-StatusReceipt 'missing' $null $staleAfterSeconds 'missing'
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Depth 20 -Compress))
        exit 0
    }

    try {
        Assert-PlainStatusFile $statusPath
        $statusObject = Get-Content -LiteralPath $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Assert-Keys $statusObject @('schemaVersion','kind','state','generatedAtUtc','privacy','heartbeat') 'status'
        Assert-Keys $statusObject.heartbeat @('lastHeartbeatUtc','intervalSeconds','staleAfterSeconds') 'status.heartbeat'
        if ($statusObject.schemaVersion -cne '1.0' -or
            $statusObject.kind -cne 'interactive-broker-status' -or
            [string] $statusObject.state -cne 'running') {
            throw 'Status contract mismatch.'
        }
        $lastHeartbeat = [DateTime]::Parse(
            [string] $statusObject.heartbeat.lastHeartbeatUtc,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).ToUniversalTime()
        $ageSeconds = [int] [Math]::Floor(([DateTime]::UtcNow - $lastHeartbeat).TotalSeconds)
        if ($ageSeconds -lt 0) { $ageSeconds = 0 }
        $state = if ($ageSeconds -le $staleAfterSeconds) { 'running' } else { 'stale' }
        $receipt = New-StatusReceipt $state $ageSeconds $staleAfterSeconds 'present'
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Depth 20 -Compress))
        exit 0
    } catch {
        $receipt = New-StatusReceipt 'invalid' $null $staleAfterSeconds 'invalid'
        [Console]::Out.WriteLine(($receipt | ConvertTo-Json -Depth 20 -Compress))
        exit 0
    }
} catch {
    $response = [ordered]@{
        schemaVersion = '1.0'
        operation = 'get_77_interactive_broker_status'
        status = 'error'
        generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        privacy = 'sanitized-json; no hostname, Windows user name, local path, credential, accounting row or source value'
        error = [ordered]@{
            code = 'BROKER_STATUS_FAILED'
            message = 'Interactive broker status failed without exposing local details'
        }
    }
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 20 -Compress))
    exit 0
}

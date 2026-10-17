#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Config
)

# SSH-side local client. It sends one bounded JSON request to the interactive
# broker spool and prints exactly one Invoke-77Bridge-compatible JSON response.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Get-PropertyValue($Object, [string] $Name) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function New-BridgeError([object] $Request, [string] $Code, [string] $Message) {
    $operation = [string] (Get-PropertyValue $Request 'operation')
    if (@('health','schema','metadata_inventory','document_presence','acquire_document_identity','read_document_by_native','read_page','read_documents_page','read_document_lines_page','read_document_lines_batch','read_constants_page','read_periodic_history_page','read_operations_page','read_operation_entries_page') -cnotcontains $operation) {
        $operation = 'unknown'
    }
    $response = [ordered]@{
        schemaVersion = '1.0'
        status = 'error'
        operation = $operation
        error = [ordered]@{ code = $Code; message = $Message }
    }
    $requestId = [string] (Get-PropertyValue $Request 'requestId')
    if ($requestId -cmatch '^[A-Za-z0-9._-]{1,80}$') { $response['requestId'] = $requestId }
    return $response
}

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
    if ([string]::IsNullOrWhiteSpace([string] $configObject.spoolRoot) -or
        -not [System.IO.Path]::IsPathRooted([string] $configObject.spoolRoot) -or
        [string] $configObject.spoolRoot -match '["\r\n]') {
        throw 'Spool root must be an absolute local path.'
    }
    $timeout = 0
    if (-not [int]::TryParse([string] $configObject.timeoutSeconds, [ref] $timeout) -or $timeout -lt 1 -or $timeout -gt 600) {
        throw 'Timeout must be 1..600 seconds.'
    }
    $poll = 0
    if (-not [int]::TryParse([string] $configObject.pollMilliseconds, [ref] $poll) -or $poll -lt 50 -or $poll -gt 5000) {
        throw 'Poll interval must be 50..5000 milliseconds.'
    }
    $requestMax = 0
    if (-not [int]::TryParse([string] $configObject.requestMaxBytes, [ref] $requestMax) -or $requestMax -lt 1024 -or $requestMax -gt 1000000) {
        throw 'Request size limit is invalid.'
    }
    $responseMax = 0
    if (-not [int]::TryParse([string] $configObject.responseMaxBytes, [ref] $responseMax) -or $responseMax -lt 1024 -or $responseMax -gt 32000000) {
        throw 'Response size limit is invalid.'
    }
    if ($null -ne $configObject.PSObject.Properties['statusPath']) {
        if ([string]::IsNullOrWhiteSpace([string] $configObject.statusPath) -or
            -not [System.IO.Path]::IsPathRooted([string] $configObject.statusPath) -or
            [string] $configObject.statusPath -match '["\r\n]') {
            throw 'Status path must be an absolute local path.'
        }
    }
    if ($null -ne $configObject.PSObject.Properties['statusStaleSeconds']) {
        $statusStale = 0
        if (-not [int]::TryParse([string] $configObject.statusStaleSeconds, [ref] $statusStale) -or $statusStale -lt 10 -or $statusStale -gt 86400) {
            throw 'Status stale interval is invalid.'
        }
    }
    return $configObject
}

function Assert-PlainDirectory([string] $Path) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Broker spool directory must not be a reparse point.'
    }
}

function Write-JsonFileExclusive($Value, [string] $Path) {
    $json = $Value | ConvertTo-Json -Depth 30 -Compress
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $writer = New-Object System.IO.StreamWriter -ArgumentList $stream, $utf8
        $stream = $null
        try { $writer.Write($json) }
        finally { $writer.Dispose() }
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

$requestObject = $null
try {
    $local = Read-ClientConfig $Config
    $line = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) { throw 'One JSON request line is required.' }
    $requestBytes = [System.Text.Encoding]::UTF8.GetByteCount($line)
    if ($requestBytes -gt [int] $local.requestMaxBytes) { throw 'Request exceeds the local size limit.' }
    try { $requestObject = $line | ConvertFrom-Json }
    catch { throw 'Request is not valid JSON.' }

    $spoolRoot = [string] $local.spoolRoot
    $requests = Join-Path $spoolRoot 'requests'
    $responses = Join-Path $spoolRoot 'responses'
    if (-not (Test-Path -LiteralPath $requests -PathType Container) -or
        -not (Test-Path -LiteralPath $responses -PathType Container)) {
        throw 'Interactive broker spool is unavailable.'
    }
    Assert-PlainDirectory $spoolRoot
    Assert-PlainDirectory $requests
    Assert-PlainDirectory $responses

    $brokerRequestId = [Guid]::NewGuid().ToString('N')
    $requestPath = Join-Path $requests ($brokerRequestId + '.json')
    $responsePath = Join-Path $responses ($brokerRequestId + '.json')
    $envelope = [ordered]@{
        schemaVersion = '1.0'
        kind = 'interactive-broker-request'
        requestId = $brokerRequestId
        createdAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        payload = $requestObject
    }
    Write-JsonFileExclusive $envelope $requestPath

    $deadline = [DateTime]::UtcNow.AddSeconds([int] $local.timeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
            $info = Get-Item -LiteralPath $responsePath
            if (($info.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Broker response must not be a reparse point.'
            }
            if ($info.Length -gt [int64] $local.responseMaxBytes) {
                Remove-Item -LiteralPath $responsePath -Force -ErrorAction SilentlyContinue
                throw 'Broker response exceeds the local size limit.'
            }
            $answer = Get-Content -LiteralPath $responsePath -Raw -Encoding UTF8 | ConvertFrom-Json
            Remove-Item -LiteralPath $responsePath -Force -ErrorAction SilentlyContinue
            Assert-Keys $answer @('schemaVersion','kind','requestId','status','generatedAtUtc','payload','error') 'response'
            if ($answer.schemaVersion -cne '1.0' -or
                $answer.kind -cne 'interactive-broker-response' -or
                $answer.requestId -cne $brokerRequestId) {
                throw 'Broker response contract mismatch.'
            }
            $payload = Get-PropertyValue $answer 'payload'
            if ($null -eq $payload) { throw 'Broker response payload is missing.' }
            [Console]::Out.WriteLine(($payload | ConvertTo-Json -Depth 30 -Compress))
            exit 0
        }
        Start-Sleep -Milliseconds ([int] $local.pollMilliseconds)
    }
    Remove-Item -LiteralPath $requestPath -Force -ErrorAction SilentlyContinue
    $response = New-BridgeError $requestObject 'BROKER_TIMEOUT' 'Interactive 1C broker did not answer in time'
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 20 -Compress))
    exit 0
} catch {
    $response = New-BridgeError $requestObject 'BROKER_CLIENT_FAILED' 'Interactive broker client failed without exposing local details'
    [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 20 -Compress))
    exit 0
}

#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$broker = Join-Path $repoRoot 'tools\agent-bridge\windows\Start-77InteractiveBroker.ps1'
$tempRoot = Join-Path $env:TEMP ('onecbroker-stream-test-' + [guid]::NewGuid().ToString('N'))
$utf8 = [System.Text.UTF8Encoding]::new($false)

function Write-JsonFile($Value, [string] $Path) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 30 -Compress), $utf8)
}

function New-TestRequest([string] $SpoolRoot, [string] $Mode) {
    $id = [guid]::NewGuid().ToString('N')
    $payload = [ordered]@{
        schemaVersion = '1.0'
        operation = 'health'
        requestId = $Mode
        mode = $Mode
    }
    if ($Mode -eq 'never-read-stdin') {
        $payload['padding'] = ('P' * 18000)
    }
    $request = [ordered]@{
        schemaVersion = '1.0'
        kind = 'interactive-broker-request'
        requestId = $id
        payload = $payload
    }
    $requestPath = Join-Path (Join-Path $SpoolRoot 'requests') ($id + '.json')
    Write-JsonFile $request $requestPath
    return $id
}

function Invoke-BrokerStreamCase([string] $Mode, [int] $ResponseMaxBytes) {
    $caseRoot = Join-Path $tempRoot $Mode
    $spoolRoot = Join-Path $caseRoot 'spool'
    $requestRoot = Join-Path $spoolRoot 'requests'
    [void] (New-Item -ItemType Directory -Path $requestRoot -Force)

    $fakeBridge = Join-Path $caseRoot 'Fake-77Bridge.ps1'
    $fakeConfig = Join-Path $caseRoot 'fake-bridge-config.json'
    $brokerConfig = Join-Path $caseRoot 'broker.local.json'
    $statusPath = Join-Path $caseRoot 'broker.status.json'

    $fakeBridgeSource = @'
#Requires -Version 5.1
param([string] $Config)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$local = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$mode = [string] $local.mode
if ($mode -eq 'never-read-stdin') {
    Start-Sleep -Seconds 30
    exit 0
}
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace($mode)) { $mode = [string] $request.mode }
if ($mode -eq 'ok') {
    $answer = [ordered]@{
        schemaVersion = '1.0'
        status = 'ok'
        operation = 'health'
        requestId = [string] $request.requestId
        synthetic = 'bounded-stream'
    }
    [Console]::Out.Write(($answer | ConvertTo-Json -Depth 10 -Compress))
} elseif ($mode -eq 'stdout-too-large') {
    [Console]::Out.Write(('X' * 2048))
} elseif ($mode -eq 'stderr-too-large') {
    [Console]::Error.Write(('E' * 5000))
    $answer = [ordered]@{
        schemaVersion = '1.0'
        status = 'ok'
        operation = 'health'
        requestId = [string] $request.requestId
    }
    [Console]::Out.Write(($answer | ConvertTo-Json -Depth 10 -Compress))
} else {
    throw 'Unknown synthetic mode'
}
'@
    [IO.File]::WriteAllText($fakeBridge, $fakeBridgeSource, $utf8)
    Write-JsonFile ([ordered]@{ mode = $Mode }) $fakeConfig

    $powerShell32 = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShell32 -PathType Leaf)) {
        $powerShell32 = (Get-Command powershell.exe -ErrorAction Stop).Source
    }

    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $config = [ordered]@{
        schemaVersion = '1.0'
        spoolRoot = $spoolRoot
        bridgeScript = $fakeBridge
        bridgeConfig = $fakeConfig
        powerShell32 = $powerShell32
        bridgeAccountSid = $sid
        invocationTimeoutSeconds = 5
        pollMilliseconds = 50
        requestMaxBytes = 20000
        responseMaxBytes = $ResponseMaxBytes
        requestTtlSeconds = 600
        statusPath = $statusPath
        statusIntervalSeconds = 1
        statusStaleSeconds = 10
    }
    Write-JsonFile $config $brokerConfig

    $requestId = New-TestRequest $spoolRoot $Mode
    & $broker -Config $brokerConfig -Once | Out-Null
    $responsePath = Join-Path (Join-Path $spoolRoot 'responses') ($requestId + '.json')
    if (-not (Test-Path -LiteralPath $responsePath -PathType Leaf)) {
        throw "Broker did not write a response for $Mode"
    }
    return ([IO.File]::ReadAllText($responsePath, $utf8) | ConvertFrom-Json)
}

function Assert-BrokerStatusReplacesExistingFile {
    $caseRoot = Join-Path $tempRoot 'status-replace'
    $spoolRoot = Join-Path $caseRoot 'spool'
    $requestRoot = Join-Path $spoolRoot 'requests'
    [void] (New-Item -ItemType Directory -Path $requestRoot -Force)

    $fakeBridge = Join-Path $caseRoot 'Fake-77Bridge.ps1'
    $fakeConfig = Join-Path $caseRoot 'fake-bridge-config.json'
    $brokerConfig = Join-Path $caseRoot 'broker.local.json'
    $statusPath = Join-Path $caseRoot 'broker.status.json'

    $fakeBridgeSource = @'
#Requires -Version 5.1
param([string] $Config)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$answer = [ordered]@{
    schemaVersion = '1.0'
    status = 'ok'
    operation = 'health'
    requestId = [string] $request.requestId
    synthetic = 'status-replace'
}
[Console]::Out.Write(($answer | ConvertTo-Json -Depth 10 -Compress))
'@
    [IO.File]::WriteAllText($fakeBridge, $fakeBridgeSource, $utf8)
    Write-JsonFile ([ordered]@{ mode = 'status-replace' }) $fakeConfig

    $powerShell32 = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShell32 -PathType Leaf)) {
        $powerShell32 = (Get-Command powershell.exe -ErrorAction Stop).Source
    }

    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $config = [ordered]@{
        schemaVersion = '1.0'
        spoolRoot = $spoolRoot
        bridgeScript = $fakeBridge
        bridgeConfig = $fakeConfig
        powerShell32 = $powerShell32
        bridgeAccountSid = $sid
        invocationTimeoutSeconds = 5
        pollMilliseconds = 50
        requestMaxBytes = 20000
        responseMaxBytes = 4096
        requestTtlSeconds = 600
        statusPath = $statusPath
        statusIntervalSeconds = 1
        statusStaleSeconds = 10
    }
    Write-JsonFile $config $brokerConfig

    $statusStamps = @()
    for ($index = 0; $index -lt 2; $index++) {
        $requestId = New-TestRequest $spoolRoot 'ok'
        & $broker -Config $brokerConfig -Once | Out-Null
        $responsePath = Join-Path (Join-Path $spoolRoot 'responses') ($requestId + '.json')
        if (-not (Test-Path -LiteralPath $responsePath -PathType Leaf)) {
            throw 'Broker did not answer the status replacement case'
        }
        $response = [IO.File]::ReadAllText($responsePath, $utf8) | ConvertFrom-Json
        if ($response.payload.status -ne 'ok' -or $response.payload.synthetic -ne 'status-replace') {
            throw 'Status replacement case returned the wrong fake bridge JSON'
        }
        $status = [IO.File]::ReadAllText($statusPath, $utf8) | ConvertFrom-Json
        if ($status.kind -ne 'interactive-broker-status' -or $status.state -ne 'running') {
            throw 'Broker status replacement case wrote the wrong status envelope'
        }
        $statusStamps += [string] $status.heartbeat.lastHeartbeatUtc
        Start-Sleep -Milliseconds 25
    }

    if ($statusStamps.Count -ne 2 -or $statusStamps[0] -eq $statusStamps[1]) {
        throw 'Broker did not update the existing status file'
    }
}

function Assert-StaleProcessingRecovery {
    $caseRoot = Join-Path $tempRoot 'stale-processing'
    $spoolRoot = Join-Path $caseRoot 'spool'
    $requestRoot = Join-Path $spoolRoot 'requests'
    $processingRoot = Join-Path $spoolRoot 'processing'
    $responseRoot = Join-Path $spoolRoot 'responses'
    [void] (New-Item -ItemType Directory -Path $requestRoot -Force)
    [void] (New-Item -ItemType Directory -Path $processingRoot -Force)
    [void] (New-Item -ItemType Directory -Path $responseRoot -Force)

    $fakeBridge = Join-Path $caseRoot 'Fake-77Bridge.ps1'
    $fakeConfig = Join-Path $caseRoot 'fake-bridge-config.json'
    $brokerConfig = Join-Path $caseRoot 'broker.local.json'
    $statusPath = Join-Path $caseRoot 'broker.status.json'

    $fakeBridgeSource = @'
#Requires -Version 5.1
throw 'stale processing recovery must not invoke the bridge'
'@
    [IO.File]::WriteAllText($fakeBridge, $fakeBridgeSource, $utf8)
    Write-JsonFile ([ordered]@{ mode = 'stale-processing' }) $fakeConfig

    $powerShell32 = Join-Path $env:WINDIR 'SysWOW64\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $powerShell32 -PathType Leaf)) {
        $powerShell32 = (Get-Command powershell.exe -ErrorAction Stop).Source
    }

    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $config = [ordered]@{
        schemaVersion = '1.0'
        spoolRoot = $spoolRoot
        bridgeScript = $fakeBridge
        bridgeConfig = $fakeConfig
        powerShell32 = $powerShell32
        bridgeAccountSid = $sid
        invocationTimeoutSeconds = 5
        pollMilliseconds = 50
        requestMaxBytes = 20000
        responseMaxBytes = 4096
        requestTtlSeconds = 600
        statusPath = $statusPath
        statusIntervalSeconds = 1
        statusStaleSeconds = 10
    }
    Write-JsonFile $config $brokerConfig

    $requestId = [guid]::NewGuid().ToString('N')
    $payload = [ordered]@{
        schemaVersion = '1.0'
        operation = 'health'
        requestId = 'stale-processing'
    }
    $request = [ordered]@{
        schemaVersion = '1.0'
        kind = 'interactive-broker-request'
        requestId = $requestId
        payload = $payload
    }
    $processingPath = Join-Path $processingRoot ($requestId + '.processing')
    Write-JsonFile $request $processingPath

    & $broker -Config $brokerConfig -Once | Out-Null

    if (Test-Path -LiteralPath $processingPath -PathType Leaf) {
        throw 'Stale processing file was not removed'
    }
    $responsePath = Join-Path $responseRoot ($requestId + '.json')
    if (-not (Test-Path -LiteralPath $responsePath -PathType Leaf)) {
        throw 'Stale processing response was not written'
    }
    $response = [IO.File]::ReadAllText($responsePath, $utf8) | ConvertFrom-Json
    if ($response.kind -ne 'interactive-broker-response' -or $response.requestId -ne $requestId) {
        throw 'Stale processing recovery returned the wrong envelope'
    }
    if ($response.payload.status -ne 'error' -or
        $response.payload.error.code -ne 'BROKER_RECOVERED_STALE_PROCESSING' -or
        $response.payload.requestId -ne 'stale-processing') {
        throw 'Stale processing recovery returned the wrong payload'
    }
}

try {
    Assert-BrokerStatusReplacesExistingFile
    Assert-StaleProcessingRecovery

    $ok = Invoke-BrokerStreamCase 'ok' 4096
    if ($ok.kind -ne 'interactive-broker-response') { throw 'OK case returned the wrong envelope kind' }
    if ($ok.payload.status -ne 'ok' -or $ok.payload.synthetic -ne 'bounded-stream') {
        throw 'OK case did not return the fake bridge JSON'
    }

    $stdoutLarge = Invoke-BrokerStreamCase 'stdout-too-large' 1024
    if ($stdoutLarge.payload.status -ne 'error' -or
        $stdoutLarge.payload.error.code -ne 'BROKER_BRIDGE_RESPONSE_TOO_LARGE') {
        throw 'Oversized stdout was not rejected by the broker'
    }

    $stderrLarge = Invoke-BrokerStreamCase 'stderr-too-large' 4096
    if ($stderrLarge.payload.status -ne 'error' -or
        $stderrLarge.payload.error.code -ne 'BROKER_BRIDGE_ERROR_TOO_LARGE') {
        throw 'Oversized stderr was not rejected by the broker'
    }

    $stdinTimeout = Invoke-BrokerStreamCase 'never-read-stdin' 4096
    if ($stdinTimeout.payload.status -ne 'error' -or
        $stdinTimeout.payload.error.code -ne 'BROKER_BRIDGE_TIMEOUT') {
        throw 'Unread stdin did not time out through the broker deadline'
    }

    'Interactive broker bounded stream harness: PASS'
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

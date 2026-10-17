#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Config,

    [switch] $Once
)

# Interactive-session broker. Run this as the Windows user that can create and
# initialize the 1C 7.7 COM object. It accepts only JSON requests from the local
# spool and delegates all 1C behavior to Invoke-77Bridge.ps1.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

function Stop-Broker([string] $Code, [string] $Message) {
    throw ($Code + '|' + $Message)
}

function Get-PropertyValue($Object, [string] $Name) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Assert-Keys($Object, [string[]] $Keys, [string] $Context) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { Stop-Broker 'INVALID_BROKER_CONFIG' "$Context must be an object" }
    foreach ($property in $Object.PSObject.Properties) {
        if ($Keys -cnotcontains $property.Name) { Stop-Broker 'INVALID_BROKER_CONFIG' "$Context has an unknown key" }
    }
}

function Assert-AbsolutePath([string] $Value, [string] $Name, [switch] $Leaf) {
    if ([string]::IsNullOrWhiteSpace($Value) -or
        -not [System.IO.Path]::IsPathRooted($Value) -or
        $Value -match '["\r\n]') {
        Stop-Broker 'INVALID_BROKER_CONFIG' "$Name must be an absolute local path"
    }
    if ($Leaf -and -not (Test-Path -LiteralPath $Value -PathType Leaf)) {
        Stop-Broker 'INVALID_BROKER_CONFIG' "$Name file is unavailable"
    }
}

function Convert-ToBoundedInt($Value, [int] $Minimum, [int] $Maximum, [string] $Name) {
    $parsed = 0
    if (-not [int]::TryParse([string] $Value, [ref] $parsed) -or $parsed -lt $Minimum -or $parsed -gt $Maximum) {
        Stop-Broker 'INVALID_BROKER_CONFIG' "$Name is outside the supported range"
    }
    return $parsed
}

function Read-BrokerConfig([string] $Path) {
    Assert-AbsolutePath $Path 'Config' -Leaf
    try { $local = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Stop-Broker 'INVALID_BROKER_CONFIG' 'Broker config is not valid JSON' }
    Assert-Keys $local @('schemaVersion','spoolRoot','bridgeScript','bridgeConfig','powerShell32','bridgeAccountSid','invocationTimeoutSeconds','pollMilliseconds','requestMaxBytes','responseMaxBytes','requestTtlSeconds','statusPath','statusIntervalSeconds','statusStaleSeconds') 'config'
    if ($local.schemaVersion -cne '1.0') { Stop-Broker 'INVALID_BROKER_CONFIG' 'Unsupported broker config schema' }
    Assert-AbsolutePath ([string] $local.spoolRoot) 'spoolRoot'
    Assert-AbsolutePath ([string] $local.bridgeScript) 'bridgeScript' -Leaf
    Assert-AbsolutePath ([string] $local.bridgeConfig) 'bridgeConfig' -Leaf
    Assert-AbsolutePath ([string] $local.powerShell32) 'powerShell32' -Leaf
    Assert-AbsolutePath ([string] $local.statusPath) 'statusPath'
    try { [void] (New-Object System.Security.Principal.SecurityIdentifier([string] $local.bridgeAccountSid)) }
    catch { Stop-Broker 'INVALID_BROKER_CONFIG' 'bridgeAccountSid is invalid' }
    [void] (Convert-ToBoundedInt $local.invocationTimeoutSeconds 5 600 'invocationTimeoutSeconds')
    [void] (Convert-ToBoundedInt $local.pollMilliseconds 50 5000 'pollMilliseconds')
    [void] (Convert-ToBoundedInt $local.requestMaxBytes 1024 1000000 'requestMaxBytes')
    [void] (Convert-ToBoundedInt $local.responseMaxBytes 1024 32000000 'responseMaxBytes')
    [void] (Convert-ToBoundedInt $local.requestTtlSeconds 30 86400 'requestTtlSeconds')
    [void] (Convert-ToBoundedInt $local.statusIntervalSeconds 1 300 'statusIntervalSeconds')
    [void] (Convert-ToBoundedInt $local.statusStaleSeconds 10 86400 'statusStaleSeconds')
    return $local
}

function New-BridgeErrorResponse($Request, [string] $Code, [string] $Message) {
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

function Test-ReadOnlyBrokerOperation($Payload) {
    $operation = [string] (Get-PropertyValue $Payload 'operation')
    return @(
        'health',
        'schema',
        'metadata_inventory',
        'document_presence',
        'acquire_document_identity',
        'read_document_by_native',
        'read_page',
        'read_documents_page',
        'read_document_lines_page',
        'read_document_lines_batch',
        'read_constants_page',
        'read_periodic_history_page',
        'read_operations_page',
        'read_operation_entries_page'
    ) -ccontains $operation
}

function Quote-Argument([string] $Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '["\r\n]') {
        Stop-Broker 'INVALID_BROKER_CONFIG' 'A configured process path is invalid'
    }
    return '"' + $Value + '"'
}

function New-BoundedStreamState([string] $Name, [System.IO.Stream] $Stream, [int] $LimitBytes, [bool] $Capture) {
    $output = $null
    if ($Capture) { $output = New-Object System.IO.MemoryStream }
    return [pscustomobject]@{
        Name = $Name
        Stream = $Stream
        LimitBytes = [int64] $LimitBytes
        Capture = $Capture
        Buffer = (New-Object byte[] 4096)
        Output = $output
        Bytes = [int64] 0
        Task = $null
        Complete = $false
        TooLarge = $false
        Failed = $false
    }
}

function Start-BoundedStreamRead($State) {
    if ($State.Complete -or $State.TooLarge -or $State.Failed) { return }
    try { $State.Task = $State.Stream.ReadAsync($State.Buffer, 0, $State.Buffer.Length) }
    catch { $State.Failed = $true }
}

function Complete-BoundedStreamRead($State) {
    if ($State.Complete -or $State.TooLarge -or $State.Failed) { return }
    try {
        if ($State.Task.IsFaulted -or $State.Task.IsCanceled) {
            $State.Failed = $true
            return
        }
        $count = [int] $State.Task.Result
    } catch {
        $State.Failed = $true
        return
    }
    if ($count -le 0) {
        $State.Complete = $true
        return
    }

    $nextBytes = [int64] $State.Bytes + [int64] $count
    if ($nextBytes -gt [int64] $State.LimitBytes) {
        $State.TooLarge = $true
        $State.Complete = $true
        return
    }
    if ($State.Capture) { $State.Output.Write($State.Buffer, 0, $count) }
    $State.Bytes = $nextBytes
    Start-BoundedStreamRead $State
}

function New-BridgeInputState([System.IO.Stream] $Stream, [byte[]] $Bytes) {
    return [pscustomobject]@{
        Stream = $Stream
        Bytes = $Bytes
        Task = $null
        Complete = $false
        Failed = $false
        Closed = $false
    }
}

function Close-BridgeInputStream($State) {
    if ($State.Closed) { return }
    try { $State.Stream.Close() } catch {}
    $State.Closed = $true
}

function Start-BridgeInputWrite($State) {
    if ($State.Complete -or $State.Failed) { return }
    try { $State.Task = $State.Stream.WriteAsync($State.Bytes, 0, $State.Bytes.Length) }
    catch {
        $State.Failed = $true
        Close-BridgeInputStream $State
    }
}

function Complete-BridgeInputWrite($State) {
    if ($State.Complete -or $State.Failed) { return }
    try {
        if ($State.Task.IsFaulted -or $State.Task.IsCanceled) {
            $State.Failed = $true
            return
        }
    } catch {
        $State.Failed = $true
        return
    } finally {
        Close-BridgeInputStream $State
    }
    $State.Complete = $true
}

function Stop-BridgeProcessIfRunning($Process) {
    try { if (-not $Process.HasExited) { $Process.Kill() } } catch {}
}

function Wait-BridgeProcessAndStreams($Process, $StdoutState, $StderrState, $StdinState,
        [DateTime] $ProcessStartedUtc,
        [int] $ProcessTimeoutMilliseconds, [int] $StreamWaitMilliseconds,
        [System.Text.Encoding] $Encoding) {
    $processDeadline = $ProcessStartedUtc.AddMilliseconds($ProcessTimeoutMilliseconds)
    $streamDeadline = $null
    $exitCode = $null

    while (-not ($StdoutState.Complete -and $StderrState.Complete -and $StdinState.Complete)) {
        if ($StdinState.Failed) {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'stdin-failed'; exitCode = $null; stdout = '' }
        }
        if ($StdoutState.Failed -or $StderrState.Failed) {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'stream-failed'; exitCode = $null; stdout = '' }
        }
        if ($StdoutState.TooLarge) {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'stdout-too-large'; exitCode = $null; stdout = '' }
        }
        if ($StderrState.TooLarge) {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'stderr-too-large'; exitCode = $null; stdout = '' }
        }

        $now = [DateTime]::UtcNow
        if ($null -eq $streamDeadline) {
            $hasExited = $false
            try { $hasExited = $Process.HasExited }
            catch {
                Stop-BridgeProcessIfRunning $Process
                return [pscustomobject]@{ status = 'process-failed'; exitCode = $null; stdout = '' }
            }
            if ($hasExited) {
                try { $exitCode = $Process.ExitCode } catch { $exitCode = $null }
                $streamDeadline = $now.AddMilliseconds($StreamWaitMilliseconds)
            } elseif ($now -ge $processDeadline) {
                Stop-BridgeProcessIfRunning $Process
                return [pscustomobject]@{ status = 'process-timeout'; exitCode = $null; stdout = '' }
            }
        } elseif ($now -ge $streamDeadline) {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'stream-timeout'; exitCode = $null; stdout = '' }
        }

        $tasks = @()
        $kinds = @()
        $states = @()
        if (-not $StdinState.Complete -and $null -ne $StdinState.Task) {
            $tasks += $StdinState.Task
            $kinds += 'stdin'
            $states += $StdinState
        }
        if (-not $StdoutState.Complete -and $null -ne $StdoutState.Task) {
            $tasks += $StdoutState.Task
            $kinds += 'stdout'
            $states += $StdoutState
        }
        if (-not $StderrState.Complete -and $null -ne $StderrState.Task) {
            $tasks += $StderrState.Task
            $kinds += 'stderr'
            $states += $StderrState
        }
        if ($tasks.Count -eq 0) { break }

        $deadline = $processDeadline
        if ($null -ne $streamDeadline) { $deadline = $streamDeadline }
        $remainingMilliseconds = [int] [Math]::Max(1, [Math]::Min(250, ($deadline - [DateTime]::UtcNow).TotalMilliseconds))
        $completedIndex = [System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]] $tasks, $remainingMilliseconds)
        if ($completedIndex -ge 0) {
            switch ($kinds[$completedIndex]) {
                'stdin' { Complete-BridgeInputWrite $states[$completedIndex] }
                default { Complete-BoundedStreamRead $states[$completedIndex] }
            }
        }
    }

    if ($StdinState.Failed) {
        Stop-BridgeProcessIfRunning $Process
        return [pscustomobject]@{ status = 'stdin-failed'; exitCode = $null; stdout = '' }
    }
    if ($StdoutState.Failed -or $StderrState.Failed) {
        Stop-BridgeProcessIfRunning $Process
        return [pscustomobject]@{ status = 'stream-failed'; exitCode = $null; stdout = '' }
    }
    if ($StdoutState.TooLarge) {
        Stop-BridgeProcessIfRunning $Process
        return [pscustomobject]@{ status = 'stdout-too-large'; exitCode = $null; stdout = '' }
    }
    if ($StderrState.TooLarge) {
        Stop-BridgeProcessIfRunning $Process
        return [pscustomobject]@{ status = 'stderr-too-large'; exitCode = $null; stdout = '' }
    }
    if ($null -eq $exitCode) {
        try {
            $remainingExitMilliseconds = [int] [Math]::Max(0, ($processDeadline - [DateTime]::UtcNow).TotalMilliseconds)
            if (-not $Process.HasExited -and -not $Process.WaitForExit($remainingExitMilliseconds)) {
                Stop-BridgeProcessIfRunning $Process
                return [pscustomobject]@{ status = 'process-timeout'; exitCode = $null; stdout = '' }
            }
            $exitCode = $Process.ExitCode
        } catch {
            Stop-BridgeProcessIfRunning $Process
            return [pscustomobject]@{ status = 'process-failed'; exitCode = $null; stdout = '' }
        }
    }

    $stdoutBytes = [byte[]] $StdoutState.Output.ToArray()
    return [pscustomobject]@{
        status = 'ok'
        exitCode = $exitCode
        stdout = $Encoding.GetString($stdoutBytes)
    }
}

function Invoke-BridgeRequest($Payload, $Local) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = [string] $Local.powerShell32
    $startInfo.Arguments = '-NoProfile -File ' + (Quote-Argument ([string] $Local.bridgeScript)) +
        ' -Config ' + (Quote-Argument ([string] $Local.bridgeConfig))
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    try { $startInfo.StandardOutputEncoding = $utf8 } catch {}
    try { $startInfo.StandardErrorEncoding = $utf8 } catch {}

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $responseLimit = [int] $Local.responseMaxBytes
    $errorLimit = 4096
    $streamWaitMilliseconds = [int]([Math]::Min(10000, [Math]::Max(1000, ([int] $Local.invocationTimeoutSeconds) * 1000)))
    $processTimeoutMilliseconds = ([int] $Local.invocationTimeoutSeconds) * 1000
    $stdoutText = ''

    $exitCode = $null
    try {
        try { [void] $process.Start() }
        catch { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_START_FAILED' 'Interactive broker could not start the 1C adapter') }
        $processStartedUtc = [DateTime]::UtcNow
        $stdoutState = New-BoundedStreamState 'stdout' $process.StandardOutput.BaseStream $responseLimit $true
        $stderrState = New-BoundedStreamState 'stderr' $process.StandardError.BaseStream $errorLimit $false
        Start-BoundedStreamRead $stdoutState
        Start-BoundedStreamRead $stderrState
        if ($stdoutState.Failed -or $stderrState.Failed) {
            Stop-BridgeProcessIfRunning $process
            return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_STREAM_FAILED' 'Interactive 1C adapter stream read failed')
        }
        $json = $Payload | ConvertTo-Json -Depth 30 -Compress
        $inputBytes = $utf8.GetBytes($json + [Environment]::NewLine)
        $stdinState = New-BridgeInputState $process.StandardInput.BaseStream $inputBytes
        Start-BridgeInputWrite $stdinState
        if ($stdinState.Failed) {
            Stop-BridgeProcessIfRunning $process
            return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_PROCESS_FAILED' 'Interactive 1C adapter process failed')
        }
        $bridgeResult = Wait-BridgeProcessAndStreams $process $stdoutState $stderrState $stdinState $processStartedUtc $processTimeoutMilliseconds $streamWaitMilliseconds $utf8
        switch ($bridgeResult.status) {
            'process-timeout' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_TIMEOUT' 'Interactive 1C adapter timed out') }
            'stream-timeout' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_STREAM_TIMEOUT' 'Interactive 1C adapter stream read timed out') }
            'stream-failed' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_STREAM_FAILED' 'Interactive 1C adapter stream read failed') }
            'stdin-failed' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_PROCESS_FAILED' 'Interactive 1C adapter process failed') }
            'process-failed' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_PROCESS_FAILED' 'Interactive 1C adapter process failed') }
            'stdout-too-large' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_RESPONSE_TOO_LARGE' 'Interactive 1C adapter response exceeded the local size limit') }
            'stderr-too-large' { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_ERROR_TOO_LARGE' 'Interactive 1C adapter error output exceeded the local size limit') }
            'ok' {}
            default { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_PROCESS_FAILED' 'Interactive 1C adapter process failed') }
        }
        $exitCode = $bridgeResult.exitCode
        $stdoutText = [string] $bridgeResult.stdout
    } finally {
        try { $process.Dispose() } catch {}
    }

    if ($null -eq $exitCode -or $exitCode -ne 0) {
        return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_PROCESS_FAILED' 'Interactive 1C adapter process failed')
    }
    $text = $stdoutText.Trim()
    if ([string]::IsNullOrWhiteSpace($text)) {
        return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_EMPTY_RESPONSE' 'Interactive 1C adapter returned no JSON')
    }
    try { return ($text | ConvertFrom-Json) }
    catch { return (New-BridgeErrorResponse $Payload 'BROKER_BRIDGE_INVALID_RESPONSE' 'Interactive 1C adapter returned invalid JSON') }
}

function New-FileRule($Sid, [System.Security.AccessControl.FileSystemRights] $Rights) {
    return New-Object System.Security.AccessControl.FileSystemAccessRule(
        $Sid,
        $Rights,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
}

function Set-ResponseFileAcl([string] $Path, [string] $BridgeAccountSid) {
    $systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $adminsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $bridgeSid = New-Object System.Security.Principal.SecurityIdentifier($BridgeAccountSid)
    $bridgeRights = [System.Security.AccessControl.FileSystemRights](
        [System.Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
        [System.Security.AccessControl.FileSystemRights]::Delete
    )
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    [void] $acl.AddAccessRule((New-FileRule $systemSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $adminsSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $currentSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $bridgeSid $bridgeRights))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-StatusFileAcl([string] $Path, [string] $BridgeAccountSid) {
    $systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $adminsSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $bridgeSid = New-Object System.Security.Principal.SecurityIdentifier($BridgeAccountSid)
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    [void] $acl.AddAccessRule((New-FileRule $systemSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $adminsSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $currentSid ([System.Security.AccessControl.FileSystemRights]::FullControl)))
    [void] $acl.AddAccessRule((New-FileRule $bridgeSid ([System.Security.AccessControl.FileSystemRights]::ReadAndExecute)))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-JsonAtomic($Value, [string] $Path, [string] $TempDirectory, [string] $BridgeAccountSid) {
    Assert-PlainDirectory $TempDirectory 'temporary response directory'
    $temp = Join-Path $TempDirectory (([Guid]::NewGuid().ToString('N')) + '.response.tmp')
    $json = $Value | ConvertTo-Json -Depth 40 -Compress
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($temp, $json, $utf8)
    Set-ResponseFileAcl $temp $BridgeAccountSid
    [System.IO.File]::Move($temp, $Path)
}

function Assert-PlainFileIfPresent([string] $Path, [string] $Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Broker 'BROKER_STATUS_REPARSE_POINT' "$Name must not be a reparse point"
    }
}

function Write-BrokerStatusAtomic($Value, [string] $Path, [string] $BridgeAccountSid) {
    $directory = Split-Path -Parent $Path
    Assert-PlainDirectory $directory 'status directory'
    Assert-PlainFileIfPresent $Path 'status file'
    $temp = Join-Path $directory (([Guid]::NewGuid().ToString('N')) + '.status.tmp')
    try {
        $json = $Value | ConvertTo-Json -Depth 20 -Compress
        $utf8 = [System.Text.UTF8Encoding]::new($false)
        [System.IO.File]::WriteAllText($temp, $json, $utf8)
        Set-StatusFileAcl $temp $BridgeAccountSid
        Move-Item -LiteralPath $temp -Destination $Path -Force -ErrorAction Stop
    } catch {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Write-BrokerStatus($Local, [switch] $Force) {
    try {
        $now = (Get-Date).ToUniversalTime()
        if (-not $Force -and $script:LastBrokerStatusWriteUtc -ne [DateTime]::MinValue) {
            if (($now - $script:LastBrokerStatusWriteUtc).TotalSeconds -lt [int] $Local.statusIntervalSeconds) { return }
        }
        $stamp = $now.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
        $status = [ordered]@{
            schemaVersion = '1.0'
            kind = 'interactive-broker-status'
            state = 'running'
            generatedAtUtc = $stamp
            privacy = 'sanitized-json; no hostname, Windows user name, local path, credential, accounting row or source value'
            heartbeat = [ordered]@{
                lastHeartbeatUtc = $stamp
                intervalSeconds = [int] $Local.statusIntervalSeconds
                staleAfterSeconds = [int] $Local.statusStaleSeconds
            }
        }
        Write-BrokerStatusAtomic $status ([string] $Local.statusPath) ([string] $Local.bridgeAccountSid)
        $script:LastBrokerStatusWriteUtc = $now
    } catch {
        Stop-Broker 'BROKER_STATUS_WRITE_FAILED' 'Interactive broker heartbeat failed without exposing local details'
    }
}

function Assert-PlainDirectory([string] $Path, [string] $Name) {
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Stop-Broker 'BROKER_SPOOL_REPARSE_POINT' "$Name must not be a reparse point"
    }
}

function Remove-ExpiredFiles([string[]] $Directories, [int] $TtlSeconds) {
    $threshold = (Get-Date).ToUniversalTime().AddSeconds(-1 * $TtlSeconds)
    foreach ($directory in $Directories) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) { continue }
        Get-ChildItem -LiteralPath $directory -File -ErrorAction SilentlyContinue | ForEach-Object {
            if ($_.LastWriteTimeUtc -lt $threshold) {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Recover-StaleProcessingFiles($Local, [string] $ProcessingDirectory, [string] $ResponseDirectory, [int] $MinimumAgeSeconds) {
    Assert-PlainDirectory $ProcessingDirectory 'processing directory'
    Assert-PlainDirectory $ResponseDirectory 'response directory'
    $threshold = (Get-Date).ToUniversalTime().AddSeconds(-1 * $MinimumAgeSeconds)
    foreach ($file in @(Get-ChildItem -LiteralPath $ProcessingDirectory -Filter '*.processing' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc, Name)) {
        if ($file.LastWriteTimeUtc -gt $threshold) { continue }
        if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $id = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
        if ($id -cnotmatch '^[a-f0-9]{32}$') { continue }
        $responsePath = Join-Path $ResponseDirectory ($id + '.json')
        if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
            Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
            continue
        }
        $parsed = Get-RequestPayload $file.FullName ([int] $Local.requestMaxBytes)
        $payload = $parsed.payload
        if ($null -eq $parsed.error -and -not (Test-ReadOnlyBrokerOperation $payload)) {
            continue
        }
        $errorCode = 'BROKER_RECOVERED_STALE_PROCESSING'
        $errorMessage = 'Interactive broker recovered a stale read-only processing request after restart'
        if ($null -ne $parsed.error) {
            $errorCode = 'BROKER_INVALID_ENVELOPE'
            $errorMessage = 'Interactive broker request envelope is invalid'
        }
        $responseEnvelope = [ordered]@{
            schemaVersion = '1.0'
            kind = 'interactive-broker-response'
            requestId = $id
            status = 'ok'
            generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
            payload = (New-BridgeErrorResponse $payload $errorCode $errorMessage)
        }
        Write-JsonAtomic $responseEnvelope $responsePath $ProcessingDirectory ([string] $Local.bridgeAccountSid)
        Remove-Item -LiteralPath $file.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Get-RequestPayload([string] $Path, [int] $MaxBytes) {
    $info = Get-Item -LiteralPath $Path
    if ($info.Length -gt $MaxBytes) {
        return [pscustomobject]@{ payload = $null; error = 'request-too-large' }
    }
    try { $envelope = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return [pscustomobject]@{ payload = $null; error = 'invalid-json' } }
    if ($envelope.schemaVersion -cne '1.0' -or
        $envelope.kind -cne 'interactive-broker-request' -or
        [string] $envelope.requestId -cnotmatch '^[a-f0-9]{32}$' -or
        $null -eq $envelope.payload) {
        return [pscustomobject]@{ payload = $null; error = 'invalid-envelope' }
    }
    return [pscustomobject]@{ payload = $envelope.payload; error = $null }
}

function Invoke-OneBrokerFile($RequestFile, $Local, [string] $ProcessingDirectory, [string] $ResponseDirectory) {
    $id = [System.IO.Path]::GetFileNameWithoutExtension($RequestFile.Name)
    if ($id -cnotmatch '^[a-f0-9]{32}$') { return }
    if (($RequestFile.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return }
    Assert-PlainDirectory $ProcessingDirectory 'processing directory'
    Assert-PlainDirectory $ResponseDirectory 'response directory'
    $processingPath = Join-Path $ProcessingDirectory ($id + '.processing')
    $responsePath = Join-Path $ResponseDirectory ($id + '.json')
    try { [System.IO.File]::Move($RequestFile.FullName, $processingPath) }
    catch { return }

    $payload = $null
    $bridgeResponse = $null
    try {
        $parsed = Get-RequestPayload $processingPath ([int] $Local.requestMaxBytes)
        $payload = $parsed.payload
        if ($null -ne $parsed.error) {
            $bridgeResponse = New-BridgeErrorResponse $payload 'BROKER_INVALID_ENVELOPE' 'Interactive broker request envelope is invalid'
        } else {
            $bridgeResponse = Invoke-BridgeRequest $payload $Local
        }
        $responseEnvelope = [ordered]@{
            schemaVersion = '1.0'
            kind = 'interactive-broker-response'
            requestId = $id
            status = 'ok'
            generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
            payload = $bridgeResponse
        }
        Write-JsonAtomic $responseEnvelope $responsePath $ProcessingDirectory ([string] $Local.bridgeAccountSid)
    } catch {
        $responseEnvelope = [ordered]@{
            schemaVersion = '1.0'
            kind = 'interactive-broker-response'
            requestId = $id
            status = 'ok'
            generatedAtUtc = (Get-Date).ToUniversalTime().ToString('o', [Globalization.CultureInfo]::InvariantCulture)
            payload = (New-BridgeErrorResponse $payload 'BROKER_FAILED' 'Interactive broker failed without exposing local details')
        }
        Write-JsonAtomic $responseEnvelope $responsePath $ProcessingDirectory ([string] $Local.bridgeAccountSid)
    } finally {
        Remove-Item -LiteralPath $processingPath -Force -ErrorAction SilentlyContinue
    }
}

$local = Read-BrokerConfig $Config
$spoolRoot = [string] $local.spoolRoot
$requestDirectory = Join-Path $spoolRoot 'requests'
$processingDirectory = Join-Path $spoolRoot 'processing'
$responseDirectory = Join-Path $spoolRoot 'responses'
$statusDirectory = Split-Path -Parent ([string] $local.statusPath)
foreach ($directory in @($requestDirectory, $processingDirectory, $responseDirectory, $statusDirectory)) {
    [void] (New-Item -ItemType Directory -Force -Path $directory)
    Assert-PlainDirectory $directory 'spool directory'
}
Assert-PlainDirectory $spoolRoot 'spool root'
$script:LastBrokerStatusWriteUtc = [DateTime]::MinValue
Write-BrokerStatus $local -Force
Recover-StaleProcessingFiles $local $processingDirectory $responseDirectory 0

do {
    Write-BrokerStatus $local
    Assert-PlainDirectory $requestDirectory 'request directory'
    Assert-PlainDirectory $processingDirectory 'processing directory'
    Assert-PlainDirectory $responseDirectory 'response directory'
    Recover-StaleProcessingFiles $local $processingDirectory $responseDirectory ([int] $local.requestTtlSeconds)
    Remove-ExpiredFiles @($requestDirectory, $responseDirectory) ([int] $local.requestTtlSeconds)
    $files = @(Get-ChildItem -LiteralPath $requestDirectory -Filter '*.json' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc, Name)
    foreach ($file in $files) {
        Write-BrokerStatus $local -Force
        Invoke-OneBrokerFile $file $local $processingDirectory $responseDirectory
        Write-BrokerStatus $local -Force
        if ($Once) { break }
    }
    if ($Once) { break }
    Start-Sleep -Milliseconds ([int] $local.pollMilliseconds)
} while ($true)

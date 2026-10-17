# Exercise production AST functions with synthetic spool entries and no disk/COM effects.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$worker = Join-Path $PSScriptRoot '../tools/agent-bridge/windows/Start-77InteractiveBroker.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($worker, [ref] $tokens, [ref] $errors)
if ($errors.Count) { throw 'TEST-broker-parse' }
foreach ($name in @('Get-PropertyValue','New-BridgeErrorResponse','Test-ReadOnlyBrokerOperation','Recover-StaleProcessingFiles')) {
    $defs = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true))
    if ($defs.Count -ne 1) { throw 'TEST-helper-unique' }
    . ([scriptblock]::Create($defs[0].Extent.Text))
}
function Check($Value, $Label) { if (-not $Value) { throw ('TEST-' + $Label) } }
function Assert-PlainDirectory($Path, $Name) { }
function Get-ChildItem($LiteralPath, $Filter, [switch] $File, $ErrorAction) { return $script:entries }
function Test-Path($LiteralPath, $PathType) { return $script:responseExists }
function Get-RequestPayload($Path, $MaximumBytes) { return $script:parsed }
function Write-JsonAtomic($Value, $Path, $Directory, $Sid) { [void] $script:responses.Add($Value) }
function Remove-Item($LiteralPath, [switch] $Force, $ErrorAction) { [void] $script:removed.Add($LiteralPath) }

$id = '0123456789abcdef0123456789abcdef'
$local = [pscustomobject] @{requestMaxBytes=4096; bridgeAccountSid='synthetic-sid'}
foreach ($operation in @('read_documents_page','acquire_document_identity','read_document_by_native','create_document','Post','ExecuteBatch','unknown','READ_DOCUMENT_BY_NATIVE')) {
    $script:entries = @([pscustomobject] @{Name=($id+'.processing'); FullName=('/synthetic-processing/'+$id+'.processing');
        LastWriteTimeUtc=[DateTime]::UtcNow.AddMinutes(-10); Attributes=[System.IO.FileAttributes]::Normal})
    $payload = [pscustomobject] @{operation=$operation; requestId='synthetic-private-request'}
    $script:parsed = [pscustomobject] @{payload=$payload; error=$null}
    $script:responseExists=$false
    $script:responses=New-Object Collections.ArrayList; $script:removed=New-Object Collections.ArrayList
    $allowed = $operation -cin @('read_documents_page','acquire_document_identity','read_document_by_native')
    Check ((Test-ReadOnlyBrokerOperation $payload) -eq $allowed) ('predicate-'+$operation)
    Recover-StaleProcessingFiles $local '/synthetic-processing' '/synthetic-response' 60
    Check ($script:responses.Count -eq [int] $allowed -and $script:removed.Count -eq [int] $allowed) ('effects-'+$operation)
    if ($allowed) {
        $response = $script:responses[0]
        Check ($response.requestId -ceq $id -and $response.kind -ceq 'interactive-broker-response') 'response-envelope'
        Check ($response.payload.operation -ceq $operation -and $response.payload.requestId -ceq $payload.requestId) 'preserved-private-request-binding'
        Check ($response.payload.status -ceq 'error' -and $response.payload.error.code -ceq 'BROKER_RECOVERED_STALE_PROCESSING') 'recovery-response'
    }
}
# Existing conservative behavior: fresh entries are untouched and malformed envelopes get a fixed error.
$script:parsed = [pscustomobject] @{payload=[pscustomobject] @{operation='read_document_by_native'}; error=$null}
$script:entries[0].LastWriteTimeUtc=[DateTime]::UtcNow
$script:responses.Clear(); $script:removed.Clear()
Recover-StaleProcessingFiles $local '/synthetic-processing' '/synthetic-response' 60
Check ($script:responses.Count -eq 0 -and $script:removed.Count -eq 0) 'fresh-entry-preserved'
$script:entries[0].LastWriteTimeUtc=[DateTime]::UtcNow.AddMinutes(-10)
$script:parsed.error='synthetic-invalid-envelope'
Recover-StaleProcessingFiles $local '/synthetic-processing' '/synthetic-response' 60
Check ($script:responses.Count -eq 1 -and $script:removed.Count -eq 1) 'invalid-envelope-recovered'
Check ($script:responses[0].payload.error.code -ceq 'BROKER_INVALID_ENVELOPE') 'fixed-invalid-envelope-error'
Write-Output 'PASS: actual broker AST recovers both native Document reads, preserves fresh entries, denies writes; synthetic spool only'

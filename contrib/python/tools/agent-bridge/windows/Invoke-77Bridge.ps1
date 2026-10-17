#Requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $Config
)

# A single request/response process. Keep this source ASCII for Windows PowerShell 5.1.
# The Windows caller owns transport and must never publish stdout containing source rows.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:BridgeErrorDetail = $null
$script:NativeProbeStage = $null
$script:NativeProbeCaughtDetail = $null
$InlineDocumentLineLimit = 1000
$InlineDocumentLinePageBudget = 1000
$DocumentLinePageLimitMax = 100
$DocumentLineCursorMax = 1000000
$DocumentPresencePageLimitMax = 5000
$ConstantCursorMax = 10000
$OperationCursorMax = 10000000
$EntryCursorMax = 1000000
$PeriodicOwnerCursorMax = 10000000
$PeriodicHistoryCursorMax = 1000000
$PeriodicOwnerScanBudget = 1000

function Fail([string] $Code, [string] $Message) {
    throw "$Code|$Message"
}

function Get-ExceptionHResult($Exception) {
    try {
        if ($null -ne $Exception) { return ('0x{0:X8}' -f ($Exception.HResult -band 0xffffffff)) }
    } catch {}
    return $null
}

function New-SanitizedExceptionNode($Exception) {
    $node = [ordered]@{}
    try {
        if ($null -ne $Exception) {
            $node['exceptionType'] = [string] $Exception.GetType().FullName
            $hresult = Get-ExceptionHResult $Exception
            if ($null -ne $hresult) { $node['hresult'] = $hresult }
        }
    } catch {}
    return [pscustomobject] $node
}

function Get-SanitizedExceptionDetail($ErrorRecord, [string] $Stage) {
    $detail = [ordered]@{ stage = $Stage }
    try {
        if ($null -ne $ErrorRecord.Exception) {
            $root = New-SanitizedExceptionNode $ErrorRecord.Exception
            if ($null -ne $root.PSObject.Properties['exceptionType']) {
                $detail['exceptionType'] = [string] $root.exceptionType
            }
            if ($null -ne $root.PSObject.Properties['hresult']) {
                $detail['hresult'] = [string] $root.hresult
            }
            $inner = New-Object System.Collections.ArrayList
            $cursor = $ErrorRecord.Exception.InnerException
            $remaining = 3
            while ($null -ne $cursor -and $remaining -gt 0) {
                [void] $inner.Add((New-SanitizedExceptionNode $cursor))
                $cursor = $cursor.InnerException
                $remaining--
            }
            if ($inner.Count -gt 0) { $detail['innerExceptions'] = @($inner.ToArray()) }
        }
    } catch {}
    return [pscustomobject] $detail
}

# Probe diagnostics contain no exception messages, method arguments or source values.
function Set-NativeProbeStage([string] $Stage) {
    $script:NativeProbeStage = $Stage
    $script:NativeProbeCaughtDetail = $null
}

function New-NativeProbeExceptionNode($Exception) {
    $node = [ordered]@{ exceptionType = 'other' }
    try {
        $name = [string] $Exception.GetType().FullName
        if (@(
            'System.Exception','System.InvalidOperationException','System.ArgumentException',
            'System.NullReferenceException','System.InvalidCastException','System.MissingMemberException',
            'System.MissingMethodException','System.UnauthorizedAccessException','System.IO.IOException',
            'System.Runtime.InteropServices.COMException','System.Reflection.TargetInvocationException',
            'System.Management.Automation.RuntimeException','System.Management.Automation.MethodInvocationException',
            'System.Management.Automation.PropertyNotFoundException','System.Management.Automation.CommandNotFoundException'
        ) -ccontains $name) { $node['exceptionType'] = $name }
        $hr = Get-ExceptionHResult $Exception
        if ($null -ne $hr) { $node['hresult'] = $hr }
    } catch {}
    return [pscustomobject] $node
}

function Get-NativeProbeExceptionDetail($ErrorRecord) {
    $detail = [ordered]@{ stage = $script:NativeProbeStage }
    try {
        $root = New-NativeProbeExceptionNode $ErrorRecord.Exception
        $detail['exceptionType'] = $root.exceptionType
        if ($null -ne $root.PSObject.Properties['hresult']) { $detail['hresult'] = $root.hresult }
        $inner = New-Object System.Collections.ArrayList
        $cursor = $ErrorRecord.Exception.InnerException
        for ($count = 0; $null -ne $cursor -and $count -lt 3; $count++) {
            [void] $inner.Add((New-NativeProbeExceptionNode $cursor))
            $cursor = $cursor.InnerException
        }
        if ($inner.Count -gt 0) { $detail['innerExceptions'] = @($inner.ToArray()) }
    } catch {}
    return [pscustomobject] $detail
}

function Assert-Keys($Object, [string[]] $Keys, [string] $Context) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { Fail 'INVALID_REQUEST' "$Context must be an object" }
    foreach ($property in $Object.PSObject.Properties) {
        if ($Keys -cnotcontains $property.Name) { Fail 'INVALID_REQUEST' "$Context has an unknown key" }
    }
}

function Get-OptionalProperty($Object, [string] $Name) {
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Assert-AbsentKeys($Object, [string[]] $Keys, [string] $Context) {
    foreach ($key in $Keys) {
        if ($null -ne $Object.PSObject.Properties[$key]) {
            Fail 'INVALID_REQUEST' "$Context has an unsupported key"
        }
    }
}

function Invoke-77AutomationMember($App, [string] $Name, [System.Reflection.BindingFlags] $BindingFlags, [object[]] $Arguments) {
    if ($null -eq $Arguments) { $Arguments = [object[]] @() }
    # Calling GetType() on a 1C aggregate COM proxy is dispatched into 1C and
    # can return E_NOTIMPL. Invoke System.Object.GetType directly instead.
    $runtimeType = [object].GetMethod('GetType').Invoke($App, [object[]] @())
    return $runtimeType.InvokeMember($Name, $BindingFlags, $null, $App, $Arguments)
}

function Invoke-77Method($Object, [string] $Name, [object[]] $Arguments) {
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod -bor [System.Reflection.BindingFlags]::IgnoreCase
    return Invoke-77AutomationMember $Object $Name $flags $Arguments
}

function New-77StringFromCodes([int[]] $Codes) {
    return (-join ($Codes | ForEach-Object { [char] $_ }))
}

function Get-77RuName([string] $Name) {
    switch ($Name) {
        'Selected' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x043D)) }
        'Empty' { return (New-77StringFromCodes @(0x041F,0x0443,0x0441,0x0442,0x0430,0x044F)) }
        'IsGroup' { return (New-77StringFromCodes @(0x042D,0x0442,0x043E,0x0413,0x0440,0x0443,0x043F,0x043F,0x0430)) }
        'Code' { return (New-77StringFromCodes @(0x041A,0x043E,0x0434)) }
        'Description' { return (New-77StringFromCodes @(0x041D,0x0430,0x0438,0x043C,0x0435,0x043D,0x043E,0x0432,0x0430,0x043D,0x0438,0x0435)) }
        'FullCode' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x043D,0x044B,0x0439,0x041A,0x043E,0x0434)) }
        'FullDescr' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x043D,0x043E,0x0435,0x041D,0x0430,0x0438,0x043C,0x0435,0x043D,0x043E,0x0432,0x0430,0x043D,0x0438,0x0435)) }
        'Parent' { return (New-77StringFromCodes @(0x0420,0x043E,0x0434,0x0438,0x0442,0x0435,0x043B,0x044C)) }
        'Owner' { return (New-77StringFromCodes @(0x0412,0x043B,0x0430,0x0434,0x0435,0x043B,0x0435,0x0446)) }
        'IsTransacted' { return (New-77StringFromCodes @(0x041F,0x0440,0x043E,0x0432,0x0435,0x0434,0x0435,0x043D)) }
        'GetAttrib' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x0410,0x0442,0x0440,0x0438,0x0431,0x0443,0x0442)) }
        'LineNum' { return (New-77StringFromCodes @(0x041D,0x043E,0x043C,0x0435,0x0440,0x0421,0x0442,0x0440,0x043E,0x043A,0x0438)) }
        'LinesCnt' { return (New-77StringFromCodes @(0x041A,0x043E,0x043B,0x0438,0x0447,0x0435,0x0441,0x0442,0x0432,0x043E,0x0421,0x0442,0x0440,0x043E,0x043A)) }
        'SelectLines' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x0421,0x0442,0x0440,0x043E,0x043A,0x0438)) }
        'GetLine' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x0421,0x0442,0x0440,0x043E,0x043A,0x0443)) }
        'SelectDocuments' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442,0x044B)) }
        'GetDocument' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442)) }
        'DocDate' { return (New-77StringFromCodes @(0x0414,0x0430,0x0442,0x0430,0x0414,0x043E,0x043A)) }
        'DocNum' { return (New-77StringFromCodes @(0x041D,0x043E,0x043C,0x0435,0x0440,0x0414,0x043E,0x043A)) }
        'Kind' { return (New-77StringFromCodes @(0x0412,0x0438,0x0434)) }
        'KindPresent' { return (New-77StringFromCodes @(0x041F,0x0440,0x0435,0x0434,0x0441,0x0442,0x0430,0x0432,0x043B,0x0435,0x043D,0x0438,0x0435,0x0412,0x0438,0x0434,0x0430)) }
        'UseDate' { return (New-77StringFromCodes @(0x0418,0x0441,0x043F,0x043E,0x043B,0x044C,0x0437,0x043E,0x0432,0x0430,0x0442,0x044C,0x0414,0x0430,0x0442,0x0443)) }
        'SelectItems' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x042D,0x043B,0x0435,0x043C,0x0435,0x043D,0x0442,0x044B)) }
        'GetItem' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x042D,0x043B,0x0435,0x043C,0x0435,0x043D,0x0442)) }
        'FindItem' { return (New-77StringFromCodes @(0x041D,0x0430,0x0439,0x0442,0x0438,0x042D,0x043B,0x0435,0x043C,0x0435,0x043D,0x0442)) }
        'CurrentItem' { return (New-77StringFromCodes @(0x0422,0x0435,0x043A,0x0443,0x0449,0x0438,0x0439,0x042D,0x043B,0x0435,0x043C,0x0435,0x043D,0x0442)) }
        'UseObject' { return (New-77StringFromCodes @(0x0418,0x0441,0x043F,0x043E,0x043B,0x044C,0x0437,0x043E,0x0432,0x0430,0x0442,0x044C,0x041E,0x0431,0x044A,0x0435,0x043A,0x0442)) }
        'SelectValues' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x0417,0x043D,0x0430,0x0447,0x0435,0x043D,0x0438,0x044F)) }
        'GetValue' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x0417,0x043D,0x0430,0x0447,0x0435,0x043D,0x0438,0x0435)) }
        'BackwardOrder' { return (New-77StringFromCodes @(0x041E,0x0431,0x0440,0x0430,0x0442,0x043D,0x044B,0x0439,0x041F,0x043E,0x0440,0x044F,0x0434,0x043E,0x043A)) }
        'FindDocument' { return (New-77StringFromCodes @(0x041D,0x0430,0x0439,0x0442,0x0438,0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442)) }
        'CurrentDocument' { return (New-77StringFromCodes @(0x0422,0x0435,0x043A,0x0443,0x0449,0x0438,0x0439,0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442)) }
        'CurrentObject' { return (New-77StringFromCodes @(0x0422,0x0435,0x043A,0x0443,0x0449,0x0438,0x0439,0x041E,0x0431,0x044A,0x0435,0x043A,0x0442)) }
        'CurrentAttribute' { return (New-77StringFromCodes @(0x0422,0x0435,0x043A,0x0443,0x0449,0x0438,0x0439,0x0420,0x0435,0x043A,0x0432,0x0438,0x0437,0x0438,0x0442)) }
        'Value' { return (New-77StringFromCodes @(0x0417,0x043D,0x0430,0x0447,0x0435,0x043D,0x0438,0x0435)) }
        'DateVal' { return (New-77StringFromCodes @(0x0414,0x0430,0x0442,0x0430,0x0417,0x043D,0x0430,0x0447)) }
        'OperationType' { return (New-77StringFromCodes @(0x041E,0x043F,0x0435,0x0440,0x0430,0x0446,0x0438,0x044F)) }
        'SelectOpers' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x041E,0x043F,0x0435,0x0440,0x0430,0x0446,0x0438,0x0438)) }
        'GetOper' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x041E,0x043F,0x0435,0x0440,0x0430,0x0446,0x0438,0x044E)) }
        'OperDate' { return (New-77StringFromCodes @(0x0414,0x0430,0x0442,0x0430,0x041E,0x043F,0x0435,0x0440,0x0430,0x0446,0x0438,0x0438)) }
        'OperationDescription' { return (New-77StringFromCodes @(0x0421,0x043E,0x0434,0x0435,0x0440,0x0436,0x0430,0x043D,0x0438,0x0435)) }
        'OperSum' { return (New-77StringFromCodes @(0x0421,0x0443,0x043C,0x043C,0x0430,0x041E,0x043F,0x0435,0x0440,0x0430,0x0446,0x0438,0x0438)) }
        'Document' { return (New-77StringFromCodes @(0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442)) }
        'EntriesCount' { return (New-77StringFromCodes @(0x041A,0x043E,0x043B,0x0438,0x0447,0x0435,0x0441,0x0442,0x0432,0x043E,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043E,0x043A)) }
        'SelectEntries' { return (New-77StringFromCodes @(0x0412,0x044B,0x0431,0x0440,0x0430,0x0442,0x044C,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043A,0x0438)) }
        'GetEntry' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043A,0x0443)) }
        'EntryNo' { return (New-77StringFromCodes @(0x041D,0x043E,0x043C,0x0435,0x0440,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043A,0x0438)) }
        'AccountsChart' { return (New-77StringFromCodes @(0x041F,0x043B,0x0430,0x043D,0x0421,0x0447,0x0435,0x0442,0x043E,0x0432)) }
        'CorrespondenceNo' { return (New-77StringFromCodes @(0x041D,0x043E,0x043C,0x0435,0x0440,0x041A,0x043E,0x0440,0x0440,0x0435,0x0441,0x043F,0x043E,0x043D,0x0434,0x0435,0x043D,0x0446,0x0438,0x0438)) }
        'ComplexEntry' { return (New-77StringFromCodes @(0x0421,0x043B,0x043E,0x0436,0x043D,0x0430,0x044F,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043A,0x0430)) }
        'DocLineNum' { return (New-77StringFromCodes @(0x041D,0x043E,0x043C,0x0435,0x0440,0x0421,0x0442,0x0440,0x043E,0x043A,0x0438,0x0414,0x043E,0x043A,0x0443,0x043C,0x0435,0x043D,0x0442,0x0430)) }
        'EntryPresentation' { return (New-77StringFromCodes @(0x041F,0x0440,0x0435,0x0434,0x0441,0x0442,0x0430,0x0432,0x043B,0x0435,0x043D,0x0438,0x0435,0x041F,0x0440,0x043E,0x0432,0x043E,0x0434,0x043A,0x0438)) }
        'SubcontoPresentation' { return (New-77StringFromCodes @(0x041F,0x0440,0x0435,0x0434,0x0441,0x0442,0x0430,0x0432,0x043B,0x0435,0x043D,0x0438,0x0435,0x0421,0x0443,0x0431,0x043A,0x043E,0x043D,0x0442,0x043E)) }
        'GetTime' { return (New-77StringFromCodes @(0x041F,0x043E,0x043B,0x0443,0x0447,0x0438,0x0442,0x044C,0x0412,0x0440,0x0435,0x043C,0x044F)) }
        'Sum' { return (New-77StringFromCodes @(0x0421,0x0443,0x043C,0x043C,0x0430)) }
        'Currency' { return (New-77StringFromCodes @(0x0412,0x0430,0x043B,0x044E,0x0442,0x0430)) }
        'CurSum' { return (New-77StringFromCodes @(0x0412,0x0430,0x043B,0x0421,0x0443,0x043C,0x043C,0x0430)) }
        'Amount' { return (New-77StringFromCodes @(0x041A,0x043E,0x043B,0x0438,0x0447,0x0435,0x0441,0x0442,0x0432,0x043E)) }
        'Debit' { return (New-77StringFromCodes @(0x0414,0x0435,0x0431,0x0435,0x0442)) }
        'Credit' { return (New-77StringFromCodes @(0x041A,0x0440,0x0435,0x0434,0x0438,0x0442)) }
        'Account' { return (New-77StringFromCodes @(0x0421,0x0447,0x0435,0x0442)) }
        'Subconto' { return (New-77StringFromCodes @(0x0421,0x0443,0x0431,0x043A,0x043E,0x043D,0x0442,0x043E)) }
        default { return $null }
    }
}

function Get-77EnglishAliases([string] $Name) {
    switch ($Name) {
        'OperationDescription' { return @('Description') }
        'CurrencyAmount' { return @('CurSum') }
        'EntryNumber' { return @('EntryNo') }
        'AccountPlan' { return @('AccountsChart') }
        'CorrespondenceNumber' { return @('CorrespondenceNo') }
        default { return @() }
    }
}

function Get-77NameCandidates([string] $Name) {
    $candidates = New-Object System.Collections.ArrayList
    [void] $candidates.Add($Name)
    foreach ($alias in (Get-77EnglishAliases $Name)) {
        if (-not [string]::IsNullOrWhiteSpace($alias)) { [void] $candidates.Add($alias) }
    }
    $ru = Get-77RuName $Name
    if (-not [string]::IsNullOrWhiteSpace($ru)) { [void] $candidates.Add($ru) }
    return @($candidates.ToArray())
}

function Test-77MemberMissing($ErrorRecord) {
    $exception = $ErrorRecord.Exception
    for ($depth = 0; $null -ne $exception -and $depth -lt 4; $depth++) {
        if ($exception -is [System.MissingMemberException] -or $exception -is [System.MissingMethodException]) {
            return $true
        }
        $hresult = '0x{0:X8}' -f ($exception.HResult -band 0xffffffff)
        if ($hresult -eq '0x80020003' -or $hresult -eq '0x80020006') {
            return $true
        }
        $exception = $exception.InnerException
    }
    return $false
}

function Invoke-77MethodAny($Object, [string] $Name, [object[]] $Arguments) {
    $lastError = $null
    foreach ($candidate in (Get-77NameCandidates $Name)) {
        try { return Invoke-77Method $Object $candidate $Arguments }
        catch {
            if (-not (Test-77MemberMissing $_)) { throw }
            $lastError = $_
        }
    }
    if ($null -ne $lastError) { throw $lastError }
    Fail 'COM_MEMBER_UNAVAILABLE' '1C OLE method is not exposed'
}

function Get-77Property($Object, [string] $Name) {
    $lastError = $null
    foreach ($flags in @(
        ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::GetProperty -bor [System.Reflection.BindingFlags]::IgnoreCase),
        ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod -bor [System.Reflection.BindingFlags]::IgnoreCase)
    )) {
        try { return (Invoke-77AutomationMember $Object $Name $flags ([object[]] @())) }
        catch {
            if (-not (Test-77MemberMissing $_)) { throw }
            $lastError = $_
        }
    }
    if ($null -ne $lastError) { throw $lastError }
    Fail 'COM_MEMBER_UNAVAILABLE' '1C OLE member is not exposed'
}

function Get-77PropertyAny($Object, [string] $Name) {
    $lastError = $null
    foreach ($candidate in (Get-77NameCandidates $Name)) {
        try { return Get-77Property $Object $candidate }
        catch {
            if (-not (Test-77MemberMissing $_)) { throw }
            $lastError = $_
        }
    }
    if ($null -ne $lastError) { throw $lastError }
    Fail 'COM_MEMBER_UNAVAILABLE' '1C OLE property is not exposed'
}

function Resolve-77InitializeMode($App, [string] $ModeName) {
    foreach ($flags in @(
        ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod),
        ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::GetProperty)
    )) {
        try { return (Invoke-77AutomationMember $App $ModeName $flags ([object[]] @())) } catch {}
    }
    Fail 'RUN_MODE_UNAVAILABLE' 'Configured 1C 7.7 run mode is not exposed by the OLE server'
}

function Invoke-77Initialize($App, $Mode, [string] $Argument) {
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod
    $arguments = [object[]] @($Mode, $Argument, 'NO_SPLASH_SHOW')
    return Invoke-77AutomationMember $App 'Initialize' $flags $arguments
}

function Invoke-77CreateObject($App, [string] $TypeName) {
    $flags = [System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod
    $arguments = [object[]] @($TypeName)
    try { return Invoke-77AutomationMember $App 'CreateObject' $flags $arguments } catch {}
    $createObjectRu = New-77StringFromCodes @(0x0421,0x043E,0x0437,0x0434,0x0430,0x0442,0x044C,0x041E,0x0431,0x044A,0x0435,0x043A,0x0442)
    return Invoke-77AutomationMember $App $createObjectRu $flags $arguments
}

function Invoke-77UseDateIfAvailable($Object, [DateTime] $Date, [bool] $UseNow) {
    try {
        [void] (Invoke-77MethodAny $Object 'UseDate' ([object[]] @($Date, [int] $UseNow)))
        return 'applied'
    } catch {
        if (Test-77MemberMissing $_) { return 'not-exposed' }
        return 'failed'
    }
}

function Get-77ObjectTypeName([string] $Kind, [string] $Type) {
    $documentPrefix = -join @(
        [char] 0x0414, [char] 0x043E, [char] 0x043A, [char] 0x0443,
        [char] 0x043C, [char] 0x0435, [char] 0x043D, [char] 0x0442
    )
    $referencePrefix = -join @(
        [char] 0x0421, [char] 0x043F, [char] 0x0440, [char] 0x0430,
        [char] 0x0432, [char] 0x043E, [char] 0x0447, [char] 0x043D,
        [char] 0x0438, [char] 0x043A
    )
    $prefix = if ($Kind -eq 'document') {
        $documentPrefix
    } else {
        $referencePrefix
    }
    return $prefix + '.' + $Type
}

function Get-77GenericDocumentTypeName {
    return -join @(
        [char] 0x0414, [char] 0x043E, [char] 0x043A, [char] 0x0443,
        [char] 0x043C, [char] 0x0435, [char] 0x043D, [char] 0x0442
    )
}

function Get-77OperationTypeName {
    return (Get-77RuName 'OperationType')
}

function Assert-RequiredKeys($Object, [string[]] $Keys, [string] $Code, [string] $Context) {
    foreach ($key in $Keys) {
        if ($null -eq $Object.PSObject.Properties[$key]) { Fail $Code "$Context is missing a required key" }
    }
}

function Assert-Identifier([string] $Value, [string] $Context) {
    if ($Value -cnotmatch '^[A-Za-z_\p{L}][A-Za-z0-9_\p{L}]{0,79}$') {
        Fail 'INVALID_CONFIG' "$Context has an invalid identifier"
    }
}

function Assert-Alias([string] $Value) {
    if ($Value -cnotmatch '^[A-Za-z][A-Za-z0-9_-]{0,79}$') { Fail 'INVALID_REQUEST' 'Source alias is invalid' }
}

function Get-DatasetMetadataAllFields($Dataset) {
    $value = Get-OptionalProperty $Dataset 'metadataAllFields'
    return ($null -ne $value -and [bool] $value)
}

function Select-MetadataAttributes([object[]] $Attributes, [object[]] $Fields) {
    if (@($Fields).Count -eq 0) { return @() }
    $allowed = @{}
    foreach ($field in @($Fields)) {
        $allowed[[string] $field] = $true
    }
    $selected = New-Object System.Collections.ArrayList
    foreach ($attribute in @($Attributes)) {
        if ($allowed.ContainsKey([string] $attribute.name)) {
            [void] $selected.Add($attribute)
        }
    }
    return @($selected.ToArray())
}

function Read-Config([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { Fail 'CONFIG_NOT_FOUND' 'Local configuration is unavailable' }
    try { $local = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Fail 'INVALID_CONFIG' 'Local configuration is not valid JSON' }
    Assert-Keys $local @('schemaVersion','progId','sources') 'config'
    Assert-RequiredKeys $local @('schemaVersion','progId','sources') 'INVALID_CONFIG' 'Config'
    if ($local.schemaVersion -cne '1.0') { Fail 'INVALID_CONFIG' 'Unsupported configuration schema' }
    if (@('V77.Application','V77L.Application','V77M.Application','V77S.Application') -cnotcontains $local.progId) {
        Fail 'INVALID_CONFIG' 'ProgID is not in the fixed 1C 7.7 allowlist'
    }
    if ($null -eq $local.sources -or @($local.sources).Count -lt 1) { Fail 'INVALID_CONFIG' 'No source aliases configured' }
    $aliases = @{}
    foreach ($source in $local.sources) {
        Assert-Keys $source @('alias','databasePath','userName','credentialFile','runMode','datasets') 'source'
        Assert-RequiredKeys $source @('alias','databasePath','userName','runMode','datasets') 'INVALID_CONFIG' 'Source'
        Assert-Alias ([string] $source.alias)
        if ($aliases.ContainsKey($source.alias)) { Fail 'INVALID_CONFIG' 'Duplicate source alias' }
        $aliases[$source.alias] = $true
        if ([string]::IsNullOrWhiteSpace([string] $source.databasePath) -or
            [string] $source.databasePath -match '["\r\n]' -or
            -not [System.IO.Path]::IsPathRooted([string] $source.databasePath)) {
            Fail 'INVALID_CONFIG' 'Source database path must be an absolute path without quotes'
        }
        if ([string] $source.userName -match '["\r\n]' -or [string]::IsNullOrWhiteSpace([string] $source.userName)) {
            Fail 'INVALID_CONFIG' 'Source user name is invalid'
        }
        $credentialFile = Get-OptionalProperty $source 'credentialFile'
        if ($null -ne $credentialFile -and
            (-not [System.IO.Path]::IsPathRooted([string] $source.credentialFile) -or
             [string] $source.credentialFile -match '["\r\n]')) {
            Fail 'INVALID_CONFIG' 'Credential file path is invalid'
        }
        if (@('RMTrade','RMAccount') -cnotcontains $source.runMode) {
            Fail 'INVALID_CONFIG' 'Unsupported 1C run mode'
        }
        if ($null -eq $source.datasets) { Fail 'INVALID_CONFIG' 'Source datasets must be listed' }
        $datasetKeys = @{}
        foreach ($dataset in $source.datasets) {
            Assert-Keys $dataset @('kind','type','fields','metadataAllFields') 'dataset'
            Assert-RequiredKeys $dataset @('kind','type','fields') 'INVALID_CONFIG' 'Dataset'
            if (@('document','reference') -cnotcontains $dataset.kind) { Fail 'INVALID_CONFIG' 'Dataset kind is not implemented' }
            Assert-Identifier ([string] $dataset.type) 'Dataset type'
            $key = [string] $dataset.kind + ':' + [string] $dataset.type
            if ($datasetKeys.ContainsKey($key)) { Fail 'INVALID_CONFIG' 'Duplicate dataset' }
            $datasetKeys[$key] = $true
            if ($null -eq $dataset.fields) { Fail 'INVALID_CONFIG' 'Dataset fields must be listed' }
            $metadataAllFields = Get-OptionalProperty $dataset 'metadataAllFields'
            if ($null -ne $metadataAllFields -and $metadataAllFields -isnot [bool]) {
                Fail 'INVALID_CONFIG' 'Dataset metadataAllFields must be a boolean'
            }
            $fields = @{}
            foreach ($field in $dataset.fields) {
                Assert-Identifier ([string] $field) 'Dataset field'
                if ($fields.ContainsKey([string] $field)) { Fail 'INVALID_CONFIG' 'Duplicate field' }
                $fields[[string] $field] = $true
            }
        }
    }
    return $local
}

function Find-Source($LocalConfig, [string] $Alias) {
    foreach ($source in $LocalConfig.sources) {
        if ($source.alias -ceq $Alias) { return $source }
    }
    Fail 'SOURCE_DENIED' 'Source alias is not configured'
}

function Find-Dataset($Source, [string] $Kind, [string] $Type) {
    foreach ($dataset in $Source.datasets) {
        if ($dataset.kind -ceq $Kind -and $dataset.type -ceq $Type) { return $dataset }
    }
    Fail 'DATASET_DENIED' 'Dataset is not configured for this source'
}

function Find-MetadataReaderScript {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        Fail 'METADATA_READER_UNAVAILABLE' 'Local metadata reader script is unavailable'
    }
    $candidate = Join-Path $PSScriptRoot 'Read-77Metadata.ps1'
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        return (Resolve-Path -LiteralPath $candidate).Path
    }
    Fail 'METADATA_READER_UNAVAILABLE' 'Local metadata reader script is unavailable'
}

function Get-MetadataInventory($Source) {
    if (-not (Test-Path -LiteralPath $Source.databasePath -PathType Container)) {
        Fail 'SOURCE_UNAVAILABLE' 'Configured source directory is unavailable'
    }
    $metadataPath = Join-Path ([string] $Source.databasePath) '1Cv7.MD'
    if (-not (Test-Path -LiteralPath $metadataPath -PathType Leaf)) {
        Fail 'SOURCE_UNAVAILABLE' '1C 7.7 metadata file is unavailable'
    }
    $readerScript = Find-MetadataReaderScript
    try {
        $inventory = @(& $readerScript -MetadataPath $metadataPath)
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'METADATA_READ_FAILED' '1C 7.7 metadata inventory failed'
    }
    if ($inventory.Count -ne 1 -or $inventory[0].status -cne 'metadata_catalog') {
        Fail 'METADATA_READ_FAILED' '1C 7.7 metadata inventory contract mismatch'
    }
    return $inventory[0]
}

function Find-MetadataDocument($Inventory, [string] $DocumentType) {
    foreach ($document in @($Inventory.documents)) {
        if ([string] $document.name -ceq $DocumentType) { return $document }
    }
    Fail 'METADATA_DOCUMENT_UNAVAILABLE' 'Configured document type is not present in metadata inventory'
}

function Get-MetadataDocumentMap($Inventory) {
    $map = @{}
    foreach ($document in @($Inventory.documents)) {
        $map[[string] $document.name] = $document
    }
    return $map
}

function Find-MetadataCatalog($Inventory, [string] $CatalogType) {
    foreach ($catalog in @($Inventory.catalogs)) {
        if ([string] $catalog.name -ceq $CatalogType) { return $catalog }
    }
    Fail 'METADATA_CATALOG_UNAVAILABLE' 'Configured catalog type is not present in metadata inventory'
}

function Find-MetadataConstant($Inventory, [string] $ConstantName) {
    foreach ($constant in @($Inventory.constants)) {
        if ([string] $constant.name -ceq $ConstantName) { return $constant }
    }
    Fail 'METADATA_CONSTANT_UNAVAILABLE' 'Requested constant is not present in metadata inventory'
}

function Find-MetadataCatalogAttribute($Inventory, [string] $CatalogType, [string] $AttributeName) {
    $catalog = Find-MetadataCatalog $Inventory $CatalogType
    foreach ($attribute in @($catalog.attributes)) {
        if ([string] $attribute.name -ceq $AttributeName) {
            return [pscustomobject] [ordered] @{
                catalog = $catalog
                attribute = $attribute
            }
        }
    }
    Fail 'METADATA_ATTRIBUTE_UNAVAILABLE' 'Requested catalog attribute is not present in metadata inventory'
}

function Assert-NativeDocumentRequest($Request) {
    $common = @('schemaVersion','operation','sourceAlias','requestId','metadataKind','documentType','documentMetadataId')
    Assert-RequiredKeys $Request @('metadataKind','documentType','documentMetadataId') 'INVALID_REQUEST' 'Native document'
    Assert-Identifier ([string] $Request.documentType) 'Document type'
    if ($Request.metadataKind -cne 'document' -or $Request.documentType -isnot [string] -or
        $Request.documentMetadataId -isnot [string] -or $Request.documentMetadataId -cnotmatch '^[1-9][0-9]{0,9}$') {
        Fail 'INVALID_REQUEST' 'Native document requires explicit document kind/type/metadata'
    }
    if ($Request.operation -ceq 'acquire_document_identity') {
        Assert-Keys $Request ($common + @('period','documentCursor')) 'Document acquisition'
        Assert-RequiredKeys $Request @('period','documentCursor') 'INVALID_REQUEST' 'Document acquisition'
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue; $to = [DateTime]::MinValue; $offset = 0L
        if ($Request.period.from -isnot [string] -or $Request.period.toExclusive -isnot [string] -or
            -not [DateTime]::TryParseExact($Request.period.from,'yyyy-MM-dd',[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::None,[ref] $from) -or
            -not [DateTime]::TryParseExact($Request.period.toExclusive,'yyyy-MM-dd',[Globalization.CultureInfo]::InvariantCulture,[Globalization.DateTimeStyles]::None,[ref] $to) -or $from -ge $to -or
            $Request.documentCursor -isnot [string] -or $Request.documentCursor -cnotmatch '^[0-9]{1,8}$' -or
            -not [long]::TryParse($Request.documentCursor,[ref] $offset) -or $offset -gt 100) {
            Fail 'INVALID_REQUEST' 'Document acquisition requires an ordered ISO period and bounded seed 0..100'
        }
    } else {
        Assert-Keys $Request ($common + @('nativeReference','sourceNamespace','attributeNames','tableAttributeNames','lineCursor','limit')) 'Addressed document'
        Assert-RequiredKeys $Request @('nativeReference','sourceNamespace','attributeNames','tableAttributeNames','lineCursor','limit') 'INVALID_REQUEST' 'Addressed document'
        if ($Request.nativeReference -isnot [string] -or $Request.sourceNamespace -isnot [string] -or
            $Request.sourceNamespace -cnotmatch '^sha256:[0-9a-f]{64}$') {
            Fail 'INVALID_REQUEST' 'Addressed document requires saved token and source namespace'
        }
        [void] (ConvertTo-NativeProbeStringExpression $Request.nativeReference)
        foreach ($fields in @('attributeNames','tableAttributeNames')) {
            $names = $Request.$fields
            if ($names -isnot [Array] -or $names.Count -gt 32) { Fail 'INVALID_REQUEST' 'Document fields are bounded arrays' }
            $unique = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            foreach ($name in $names) {
                if ($name -isnot [string] -or -not $unique.Add($name)) { Fail 'INVALID_REQUEST' 'Document fields must be unique identifiers' }
                Assert-Identifier $name 'Document field'
            }
        }
        $offset = 0L; $limit = 0
        if ($Request.lineCursor -isnot [string] -or $Request.lineCursor -cnotmatch '^[0-9]{1,8}$' -or
            -not [long]::TryParse($Request.lineCursor,[ref] $offset) -or $offset -gt 10000 -or
            $Request.limit -is [string] -or $Request.limit -is [bool] -or
            -not [int]::TryParse([string] $Request.limit,[ref] $limit) -or $limit -lt 1 -or $limit -gt 100) {
            Fail 'INVALID_REQUEST' 'Addressed document requires bounded lines'
        }
    }
}

function Assert-Request($Request) {
    Assert-Keys $Request @('schemaVersion','operation','sourceAlias','kind','type','attributeName','period','cursor','ownerCursor','historyCursor','documentType','documentCursor','lineCursor','operationCursor','entryCursor','items','limit','catalogMetadataId','nativeReference','asOf','attributeNames','metadataKind','documentMetadataId','sourceNamespace','tableAttributeNames','requestId') 'request'
    Assert-RequiredKeys $Request @('schemaVersion','operation','sourceAlias') 'INVALID_REQUEST' 'Request'
    if ($Request.schemaVersion -cne '1.0') { Fail 'INVALID_REQUEST' 'Unsupported request schema' }
    if (@('health','schema','metadata_inventory','document_presence','probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native','read_page','read_documents_page','read_document_lines_page','read_document_lines_batch','read_constants_page','read_periodic_history_page','read_operations_page','read_operation_entries_page') -cnotcontains $Request.operation) {
        Fail 'INVALID_REQUEST' 'Operation is not supported'
    }
    Assert-Alias ([string] $Request.sourceAlias)
    $requestId = Get-OptionalProperty $Request 'requestId'
    if ($null -ne $requestId -and [string] $requestId -cnotmatch '^[A-Za-z0-9._-]{1,80}$') {
        Fail 'INVALID_REQUEST' 'Request ID has an invalid format'
    }
    if ($Request.operation -cne 'read_document_lines_batch') {
        Assert-AbsentKeys $Request @('items') 'Request'
    }
    if ($Request.operation -cnotin @('read_reference_by_native','acquire_document_identity','read_document_by_native')) {
        Assert-AbsentKeys $Request @('catalogMetadataId','nativeReference','asOf','attributeNames') 'Request'
    }
    if ($Request.operation -cnotin @('acquire_document_identity','read_document_by_native')) {
        Assert-AbsentKeys $Request @('metadataKind','documentMetadataId','tableAttributeNames') 'Request'
        if ($Request.operation -cne 'read_reference_by_native') { Assert-AbsentKeys $Request @('sourceNamespace') 'Request' }
    }
    if ($Request.operation -cin @('acquire_document_identity','read_document_by_native')) {
        Assert-NativeDocumentRequest $Request
    } elseif ($Request.operation -eq 'read_reference_by_native') {
        Assert-Keys $Request @('schemaVersion','operation','sourceAlias','type','catalogMetadataId','nativeReference','asOf','attributeNames','sourceNamespace','requestId') 'addressed reference'
        Assert-RequiredKeys $Request @('type','catalogMetadataId','nativeReference','asOf','attributeNames') 'INVALID_REQUEST' 'Addressed reference'
        Assert-Identifier ([string] $Request.type) 'Catalog type'
        if ($Request.catalogMetadataId -isnot [string] -or $Request.catalogMetadataId -cnotmatch '^[1-9][0-9]{0,9}$' -or $Request.nativeReference -isnot [string]) {
            Fail 'INVALID_REQUEST' 'Addressed reference requires typed metadata and opaque token'
        }
        [void] (ConvertTo-NativeProbeStringExpression $Request.nativeReference)
        $expectedNamespace = Get-OptionalProperty $Request 'sourceNamespace'
        if ($null -ne $Request.PSObject.Properties['sourceNamespace'] -and
            ($expectedNamespace -isnot [string] -or $expectedNamespace -cnotmatch '^sha256:[0-9a-f]{64}$')) {
            Fail 'INVALID_REQUEST' 'Addressed reference source namespace is invalid'
        }
        $asOfDate = [DateTime]::MinValue
        if ($Request.asOf -isnot [string] -or -not [DateTime]::TryParseExact($Request.asOf, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $asOfDate)) {
            Fail 'INVALID_REQUEST' 'Addressed reference requires an explicit ISO as-of date'
        }
        if ($Request.attributeNames -isnot [Array] -or $Request.attributeNames.Count -gt 32) {
            Fail 'INVALID_REQUEST' 'Addressed reference requires at most 32 attribute names'
        }
        $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        foreach ($name in $Request.attributeNames) {
            if ($name -isnot [string] -or -not $names.Add($name)) { Fail 'INVALID_REQUEST' 'Attribute names must be unique strings' }
            Assert-Identifier $name 'Attribute name'
        }
    } elseif ($Request.operation -eq 'probe_reference_identity') {
        Assert-Keys $Request @('schemaVersion','operation','sourceAlias','type','cursor','requestId') 'identity probe'
        Assert-RequiredKeys $Request @('type','cursor') 'INVALID_REQUEST' 'Identity probe'
        Assert-Identifier ([string] $Request.type) 'Catalog type'
        $probeOffset = 0L
        if ([string] $Request.cursor -cnotmatch '^\d+$' -or -not [long]::TryParse([string] $Request.cursor, [ref] $probeOffset) -or $probeOffset -gt 100) {
            Fail 'INVALID_REQUEST' 'Identity seed cursor must be an offset from 0 to 100'
        }
    } elseif ($Request.operation -eq 'read_page' -or $Request.operation -eq 'read_documents_page') {
        if ($Request.operation -eq 'read_page') {
            Assert-RequiredKeys $Request @('kind','type','limit') 'INVALID_REQUEST' 'Page request'
            Assert-AbsentKeys $Request @('documentType','attributeName','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor') 'Page request'
        } else {
            Assert-RequiredKeys $Request @('documentType','period','limit') 'INVALID_REQUEST' 'Document request'
            Assert-AbsentKeys $Request @('kind','type','attributeName','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor') 'Document request'
        }
        if ($Request.operation -eq 'read_documents_page') { $kind = 'document' }
        else { $kind = [string] $Request.kind }
        if (@('document','reference') -cnotcontains $kind) { Fail 'INVALID_REQUEST' 'Dataset kind is not supported' }
        $datasetType = if ($Request.operation -eq 'read_documents_page') { [string] $Request.documentType } else { [string] $Request.type }
        Assert-Identifier $datasetType 'Dataset type'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt 100) {
            Fail 'INVALID_REQUEST' 'Limit must be between 1 and 100'
        }
        $offsetValue = 0L
        $cursor = Get-OptionalProperty $Request 'cursor'
        if ($null -ne $cursor -and
            ([string] $Request.cursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.cursor, [ref] $offsetValue) -or
             $offsetValue -gt 10000000)) {
            Fail 'INVALID_REQUEST' 'Cursor must be a bounded nonnegative offset'
        }
        if ($kind -eq 'document') {
            Assert-RequiredKeys $Request @('period') 'INVALID_REQUEST' 'Document request'
            Assert-Keys $Request.period @('from','toExclusive') 'period'
            Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
            $from = [DateTime]::MinValue
            $to = [DateTime]::MinValue
            if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
                [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
                -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
                [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
                $from -ge $to) {
                Fail 'INVALID_REQUEST' 'Document period must be an ordered pair of ISO dates'
            }
        } elseif ($null -ne (Get-OptionalProperty $Request 'period')) {
            Fail 'INVALID_REQUEST' 'Period is only supported for documents'
        }
    } elseif ($Request.operation -eq 'read_document_lines_page') {
        Assert-RequiredKeys $Request @('documentType','period','documentCursor','lineCursor','limit') 'INVALID_REQUEST' 'Document line request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','cursor','ownerCursor','historyCursor','operationCursor','entryCursor') 'Document line request'
        Assert-Identifier ([string] $Request.documentType) 'Document type'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt $DocumentLinePageLimitMax) {
            Fail 'INVALID_REQUEST' 'Line limit must be between 1 and 100'
        }
        $documentOffset = 0L
        if ([string] $Request.documentCursor -cnotmatch '^\d+$' -or
            -not [long]::TryParse([string] $Request.documentCursor, [ref] $documentOffset) -or
            $documentOffset -gt 10000000) {
            Fail 'INVALID_REQUEST' 'Document cursor must be a bounded nonnegative offset'
        }
        $lineOffset = 0L
        if ([string] $Request.lineCursor -cnotmatch '^\d+$' -or
            -not [long]::TryParse([string] $Request.lineCursor, [ref] $lineOffset) -or
            $lineOffset -gt $DocumentLineCursorMax) {
            Fail 'INVALID_REQUEST' 'Line cursor must be a bounded nonnegative offset'
        }
    } elseif ($Request.operation -eq 'read_document_lines_batch') {
        Assert-RequiredKeys $Request @('documentType','period','items') 'INVALID_REQUEST' 'Document line batch request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','cursor','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor','limit') 'Document line batch request'
        Assert-Identifier ([string] $Request.documentType) 'Document type'
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue
        $to = [DateTime]::MinValue
        if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
            -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
            $from -ge $to) {
            Fail 'INVALID_REQUEST' 'Document line batch period must be an ordered pair of ISO dates'
        }
        if ($Request.items -isnot [System.Array] -or $Request.items.Count -lt 1 -or $Request.items.Count -gt 10) {
            Fail 'INVALID_REQUEST' 'Document line batch must contain between 1 and 10 items'
        }
        $seenDocumentCursors = @{}
        foreach ($item in $Request.items) {
            Assert-Keys $item @('documentCursor','lineCursor','limit') 'Document line batch item'
            Assert-RequiredKeys $item @('documentCursor','lineCursor','limit') 'INVALID_REQUEST' 'Document line batch item'
            $documentOffset = 0L
            if ([string] $item.documentCursor -cnotmatch '^\d+$' -or
                -not [long]::TryParse([string] $item.documentCursor, [ref] $documentOffset) -or
                $documentOffset -gt 10000000) {
                Fail 'INVALID_REQUEST' 'Document cursor must be a bounded nonnegative offset'
            }
            $lineOffset = 0L
            if ([string] $item.lineCursor -cnotmatch '^\d+$' -or
                -not [long]::TryParse([string] $item.lineCursor, [ref] $lineOffset) -or
                $lineOffset -gt $DocumentLineCursorMax) {
                Fail 'INVALID_REQUEST' 'Line cursor must be a bounded nonnegative offset'
            }
            $itemLimit = 0
            if (-not [int]::TryParse([string] $item.limit, [ref] $itemLimit) -or
                $itemLimit -lt 1 -or $itemLimit -gt $DocumentLinePageLimitMax) {
                Fail 'INVALID_REQUEST' 'Line limit must be between 1 and 100'
            }
            $documentCursorKey = $documentOffset.ToString([Globalization.CultureInfo]::InvariantCulture)
            if ($seenDocumentCursors.ContainsKey($documentCursorKey)) {
                Fail 'INVALID_REQUEST' 'Document cursors in a batch must be unique'
            }
            $seenDocumentCursors[$documentCursorKey] = $true
        }
    } elseif ($Request.operation -eq 'read_constants_page') {
        Assert-RequiredKeys $Request @('limit') 'INVALID_REQUEST' 'Constants request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','documentType','period','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor') 'Constants request'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt 100) {
            Fail 'INVALID_REQUEST' 'Limit must be between 1 and 100'
        }
        $offsetValue = 0L
        $cursor = Get-OptionalProperty $Request 'cursor'
        if ($null -ne $cursor -and
            ([string] $Request.cursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.cursor, [ref] $offsetValue) -or
             $offsetValue -gt $ConstantCursorMax)) {
            Fail 'INVALID_REQUEST' 'Cursor must be a bounded nonnegative offset'
        }
    } elseif ($Request.operation -eq 'document_presence') {
        Assert-RequiredKeys $Request @('period','limit') 'INVALID_REQUEST' 'Document presence request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','documentType','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor') 'Document presence request'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt $DocumentPresencePageLimitMax) {
            Fail 'INVALID_REQUEST' 'Presence limit must be between 1 and 5000'
        }
        $offsetValue = 0L
        $cursor = Get-OptionalProperty $Request 'cursor'
        if ($null -ne $cursor -and
            ([string] $Request.cursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.cursor, [ref] $offsetValue) -or
             $offsetValue -gt 10000000)) {
            Fail 'INVALID_REQUEST' 'Cursor must be a bounded nonnegative offset'
        }
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue
        $to = [DateTime]::MinValue
        if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
            -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
            $from -ge $to) {
            Fail 'INVALID_REQUEST' 'Document period must be an ordered pair of ISO dates'
        }
    } elseif ($Request.operation -eq 'read_operations_page') {
        Assert-RequiredKeys $Request @('period','limit') 'INVALID_REQUEST' 'Operations request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','documentType','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor') 'Operations request'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt 100) {
            Fail 'INVALID_REQUEST' 'Limit must be between 1 and 100'
        }
        $offsetValue = 0L
        $cursor = Get-OptionalProperty $Request 'cursor'
        if ($null -ne $cursor -and
            ([string] $Request.cursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.cursor, [ref] $offsetValue) -or
             $offsetValue -gt $OperationCursorMax)) {
            Fail 'INVALID_REQUEST' 'Cursor must be a bounded nonnegative offset'
        }
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue
        $to = [DateTime]::MinValue
        if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
            -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
            $from -ge $to) {
            Fail 'INVALID_REQUEST' 'Operation period must be an ordered pair of ISO dates'
        }
    } elseif ($Request.operation -eq 'read_operation_entries_page') {
        Assert-RequiredKeys $Request @('period','operationCursor','entryCursor','limit') 'INVALID_REQUEST' 'Operation entries request'
        Assert-AbsentKeys $Request @('kind','type','attributeName','documentType','cursor','ownerCursor','historyCursor','documentCursor','lineCursor') 'Operation entries request'
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt 100) {
            Fail 'INVALID_REQUEST' 'Entry limit must be between 1 and 100'
        }
        $operationOffset = 0L
        if ([string] $Request.operationCursor -cnotmatch '^\d+$' -or
            -not [long]::TryParse([string] $Request.operationCursor, [ref] $operationOffset) -or
            $operationOffset -gt $OperationCursorMax) {
            Fail 'INVALID_REQUEST' 'Operation cursor must be a bounded nonnegative offset'
        }
        $entryOffset = 0L
        if ([string] $Request.entryCursor -cnotmatch '^\d+$' -or
            -not [long]::TryParse([string] $Request.entryCursor, [ref] $entryOffset) -or
            $entryOffset -gt $EntryCursorMax) {
            Fail 'INVALID_REQUEST' 'Entry cursor must be a bounded nonnegative offset'
        }
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue
        $to = [DateTime]::MinValue
        if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
            -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
            $from -ge $to) {
            Fail 'INVALID_REQUEST' 'Operation period must be an ordered pair of ISO dates'
        }
    } elseif ($Request.operation -eq 'read_periodic_history_page') {
        Assert-RequiredKeys $Request @('kind','type','period','limit') 'INVALID_REQUEST' 'Periodic history request'
        Assert-AbsentKeys $Request @('documentType','documentCursor','lineCursor','operationCursor','entryCursor') 'Periodic history request'
        if (@('constant','catalog_attribute') -cnotcontains [string] $Request.kind) {
            Fail 'INVALID_REQUEST' 'Periodic target kind is not supported'
        }
        Assert-Identifier ([string] $Request.type) 'Periodic target type'
        if ([string] $Request.kind -eq 'catalog_attribute') {
            Assert-RequiredKeys $Request @('attributeName') 'INVALID_REQUEST' 'Periodic catalog attribute request'
            Assert-Identifier ([string] $Request.attributeName) 'Periodic attribute name'
        } else {
            Assert-AbsentKeys $Request @('attributeName','ownerCursor') 'Periodic constant request'
        }
        $limitValue = 0
        if ($null -eq $Request.limit -or -not [int]::TryParse([string] $Request.limit, [ref] $limitValue) -or
            $limitValue -lt 1 -or $limitValue -gt 100) {
            Fail 'INVALID_REQUEST' 'Limit must be between 1 and 100'
        }
        $historyOffset = 0L
        $historyCursor = Get-OptionalProperty $Request 'historyCursor'
        if ($null -ne $historyCursor -and
            ([string] $Request.historyCursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.historyCursor, [ref] $historyOffset) -or
             $historyOffset -gt $PeriodicHistoryCursorMax)) {
            Fail 'INVALID_REQUEST' 'History cursor must be a bounded nonnegative offset'
        }
        $ownerOffset = 0L
        $ownerCursor = Get-OptionalProperty $Request 'ownerCursor'
        if ($null -ne $ownerCursor -and
            ([string] $Request.ownerCursor -cnotmatch '^\d+$' -or
             -not [long]::TryParse([string] $Request.ownerCursor, [ref] $ownerOffset) -or
             $ownerOffset -gt $PeriodicOwnerCursorMax)) {
            Fail 'INVALID_REQUEST' 'Owner cursor must be a bounded nonnegative offset'
        }
        $legacyCursor = Get-OptionalProperty $Request 'cursor'
        if ($null -ne $legacyCursor) {
            Fail 'INVALID_REQUEST' 'Periodic history uses explicit historyCursor and ownerCursor'
        }
        Assert-Keys $Request.period @('from','toExclusive') 'period'
        Assert-RequiredKeys $Request.period @('from','toExclusive') 'INVALID_REQUEST' 'Period'
        $from = [DateTime]::MinValue
        $to = [DateTime]::MinValue
        if (-not [DateTime]::TryParseExact([string] $Request.period.from, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $from) -or
            -not [DateTime]::TryParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref] $to) -or
            $from -ge $to) {
            Fail 'INVALID_REQUEST' 'Periodic period must be an ordered pair of ISO dates'
        }
    } else {
        Assert-AbsentKeys $Request @('kind','type','attributeName','documentType','period','cursor','ownerCursor','historyCursor','documentCursor','lineCursor','operationCursor','entryCursor','limit') 'Metadata request'
    }
}

function Open-Application($LocalConfig, $Source) {
    if ([IntPtr]::Size -ne 4) {
        Fail 'WRONG_BITNESS' 'Use 32-bit Windows PowerShell 5.1'
    }
    if (-not (Test-Path -LiteralPath $Source.databasePath -PathType Container)) {
        Fail 'SOURCE_UNAVAILABLE' 'Configured source directory is unavailable'
    }
    $registration = 'Registry::HKEY_CLASSES_ROOT\' + [string] $LocalConfig.progId + '\CLSID'
    if (-not (Test-Path -LiteralPath $registration)) { Fail 'COM_NOT_REGISTERED' '1C 7.7 OLE ProgID is not registered in the 32-bit registry view' }
    try { $app = New-Object -ComObject $LocalConfig.progId }
    catch {
        if ($script:NativeProbeStage -ceq 'open-application') { $script:NativeProbeCaughtDetail = Get-NativeProbeExceptionDetail $_ }
        $hresult = '0x{0:X8}' -f ($_.Exception.HResult -band 0xffffffff)
        if ($hresult -eq '0x80070005') {
            Fail 'COM_ACCESS_DENIED' 'Windows account cannot launch the registered 1C 7.7 OLE server; grant local COM/DCOM launch rights or use an authorized Windows account'
        }
        Fail 'COM_CREATE_FAILED' 'Registered 1C 7.7 OLE server could not be created in this session'
    }
    try {
        $mode = Resolve-77InitializeMode $app ([string] $Source.runMode)
        # Passwords may only come from a user-bound DPAPI PSCredential file.
        # This string stays in-process and is never part of powershell.exe args.
        $argument = '/D"' + [string] $Source.databasePath + '" /N"' + [string] $Source.userName + '"'
        $password = $null
        $credentialFile = Get-OptionalProperty $Source 'credentialFile'
        if (-not [string]::IsNullOrWhiteSpace([string] $credentialFile)) {
            try {
                $credential = Import-Clixml -LiteralPath $Source.credentialFile
                if ($credential -isnot [pscredential] -or $credential.UserName -cne $Source.userName) {
                    Fail 'CREDENTIAL_UNAVAILABLE' 'Local credential cannot be used by this source account'
                }
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($credential.Password)
                try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
                finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
                if ($password -match '["\r\n]') { Fail 'CREDENTIAL_UNAVAILABLE' 'Credential contains unsupported characters' }
                $argument += ' /P"' + $password + '"'
            } catch {
                if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
                Fail 'CREDENTIAL_UNAVAILABLE' 'Local credential cannot be read by this Windows account'
            }
        }
        try { $initialized = Invoke-77Initialize $app $mode $argument }
        finally { $password = $null; $argument = $null }
        # COM VARIANT_BOOL true may be -1; 1C examples also use 1.
        if ([int] $initialized -eq 0) { Fail 'INITIALIZE_FAILED' '1C 7.7 rejected initialization' }
        return $app
    } catch {
        if ($script:NativeProbeStage -ceq 'open-application' -and $null -eq $script:NativeProbeCaughtDetail) {
            $script:NativeProbeCaughtDetail = Get-NativeProbeExceptionDetail $_
        }
        try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) } catch {}
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'INITIALIZE_FAILED' '1C 7.7 could not initialize this source and session'
    }
}

function Convert-Scalar($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [string] -or $Value -is [bool] -or
        $Value -is [int] -or $Value -is [long] -or $Value -is [decimal] -or $Value -is [double]) {
        return $Value
    }
    if ($Value -is [DateTime]) { return $Value.ToString('o', [Globalization.CultureInfo]::InvariantCulture) }
    return $null
}

function Get-ValuePresentation($Value) {
    if ($null -eq $Value) { return $null }
    try {
        $text = [string] $Value
        if ([string]::IsNullOrWhiteSpace($text) -or $text -ceq 'System.__ComObject') { return $null }
        return $text
    } catch {
        return $null
    }
}

function Add-UnsupportedAttribute($Unsupported, [string] $Scope, $Attribute, [string] $Reason) {
    [void] $Unsupported.Add([pscustomobject] [ordered] @{
        scope = $Scope
        name = [string] $Attribute.name
        typeCode = [string] $Attribute.typeCode
        refTypeId = [string] $Attribute.refTypeId
        reason = $Reason
    })
}

function New-UnsupportedValue($Attribute, [string] $Reason) {
    return [pscustomobject] [ordered] @{
        valueKind = 'unsupported'
        typeCode = [string] $Attribute.typeCode
        refTypeId = [string] $Attribute.refTypeId
        serializationStatus = $Reason
    }
}

function Test-77ReferenceAttribute($Attribute) {
    return ([string] $Attribute.refTypeId -cmatch '^[1-9][0-9]*$')
}

function Get-Fixed77StringProperty($Value, [string] $Name) {
    try {
        $result = Get-77PropertyAny $Value $Name
        if ($null -eq $result) { return $null }
        return [string] $result
    } catch {
        return $null
    }
}

function Get-Fixed77MethodValue($Value, [string] $Name) {
    try { return Invoke-77MethodAny $Value $Name ([object[]] @()) } catch {
        if ($script:NativeProbeStage -ceq 'check-selected') {
            $script:NativeProbeCaughtDetail = Get-NativeProbeExceptionDetail $_
        }
        return $null
    }
}

function Get-Fixed77MemberValue($Value, [string] $Name) {
    try { return Get-77PropertyAny $Value $Name } catch {}
    try { return Invoke-77MethodAny $Value $Name ([object[]] @()) } catch {}
    return $null
}

function New-StandardAttribute([string] $Name, [string] $TypeCode, [string] $RefTypeId) {
    return [pscustomobject] [ordered] @{
        name = $Name
        typeCode = $TypeCode
        refTypeId = $RefTypeId
    }
}

function Get-Optional77Boolean($Value, [string] $Name) {
    $raw = Get-Fixed77MemberValue $Value $Name
    if ($null -eq $raw) { return $null }
    try { return ([int] $raw -ne 0) } catch {}
    if ($raw -is [bool]) { return $raw }
    return $null
}

function Get-Optional77String($Value, [string] $Name) {
    $raw = Get-Fixed77MemberValue $Value $Name
    if ($null -eq $raw) { return $null }
    try { return [string] $raw } catch { return $null }
}

function Get-Optional77Reference($Value, [string] $Name, $Unsupported, [string] $Scope) {
    $raw = Get-Fixed77MemberValue $Value $Name
    if ($null -eq $raw) { return $null }
    if ($raw -is [string] -and [string]::IsNullOrWhiteSpace([string] $raw)) { return $null }
    try {
        if (-not [Runtime.InteropServices.Marshal]::IsComObject($raw) -and [int] $raw -eq 0) { return $null }
    } catch {}
    if (-not [Runtime.InteropServices.Marshal]::IsComObject($raw)) {
        $attribute = New-StandardAttribute $Name 'standard-reference' ''
        Add-UnsupportedAttribute $Unsupported $Scope $attribute 'standard-reference-not-com-object'
        return New-UnsupportedValue $attribute 'unsupported-reference-shape'
    }
    return Convert-ReferenceValue $raw (New-StandardAttribute $Name 'standard-reference' '') $Unsupported $Scope
}

$script:NativeIdentityApp = $null

function Get-NativeReferenceToken($Value) {
    if ($null -eq $script:NativeIdentityApp -or $null -eq $Value) { return $null }
    try {
        $token = Invoke-77MethodAny $script:NativeIdentityApp 'ValueToStringInternal' ([object[]] @($Value))
        if ($token -isnot [string] -or [string]::IsNullOrWhiteSpace($token) -or $token.Length -gt 4096) { return $null }
        return $token
    } catch {
        if ($script:NativeProbeStage -in @('serialize-seed','serialize-resolved')) {
            $script:NativeProbeCaughtDetail = Get-NativeProbeExceptionDetail $_
        }
        return $null
    }
}

function ConvertTo-NativeProbeStringExpression([string] $Token) {
    # Encode opaque token data in a closed expression literal; no executable caller input.
    if ([string]::IsNullOrWhiteSpace($Token) -or $Token.Length -gt 4096) {
        Fail 'NATIVE_IDENTITY_UNAVAILABLE' 'Internal reference serialization is unavailable'
    }
    $parts = New-Object System.Collections.Generic.List[string]
    $literal = New-Object System.Text.StringBuilder
    foreach ($character in $Token.ToCharArray()) {
        $code = [int] $character
        # Reject unsupported shapes; never normalize the opaque token.
        if ($code -eq 0 -or ($code -gt 127 -and [char]::IsControl($character)) -or
            $code -in @(0x2028,0x2029) -or [char]::IsSurrogate($character)) {
            Fail 'NATIVE_IDENTITY_UNAVAILABLE' 'Internal reference serialization is unavailable'
        }
        if ($code -eq 34 -or $code -lt 32 -or $code -eq 127) {
            [void] $parts.Add(('"' + $literal.ToString() + '"'))
            [void] $literal.Clear()
            [void] $parts.Add(('Chr(' + $code.ToString([Globalization.CultureInfo]::InvariantCulture) + ')'))
        } else {
            [void] $literal.Append($character)
        }
    }
    [void] $parts.Add(('"' + $literal.ToString() + '"'))
    return '(' + ($parts -join '+') + ')'
}

function Resolve-NativeProbeReference($App, [string] $Token) {
    Set-NativeProbeStage 'resolve-seed'
    $literal = ConvertTo-NativeProbeStringExpression $Token
    $inverse = 'ValueFromStringInternal(' + $literal + ')'
    # Native inverse/serialization first returns only a string across OLE.
    # Scalar equality alone is not selected-reference proof.
    Set-NativeProbeStage 'resolve-native-scalar'
    $roundtrip = Invoke-77MethodAny $App 'EvalExpr' ([object[]] @('ValueToStringInternal(' + $inverse + ')'))
    if ($roundtrip -isnot [string] -or -not ([string]::Equals($Token, $roundtrip, [StringComparison]::Ordinal))) {
        Fail 'NATIVE_IDENTITY_ROUNDTRIP_FAILED' 'Native expression roundtrip did not preserve the seed identity'
    }
    # Aggregate return remains required for existing Selected/row/attribute
    # checks. No direct-call retry, global variable or batch execution.
    Set-NativeProbeStage 'resolve-native-reference'
    return Invoke-77MethodAny $App 'EvalExpr' ([object[]] @($inverse))
}

function Probe-ReferenceIdentity($App, $Request, $Dataset, $MetadataObject) {
    Set-NativeProbeStage 'create-catalog'
    $obj = Invoke-77CreateObject $App (Get-77ObjectTypeName 'reference' ([string] $Dataset.type))
    try {
        Set-NativeProbeStage 'select-items'
        [void] (Invoke-77MethodAny $obj 'SelectItems' ([object[]] @(0)))
        for ($index = 0; $index -le [long] $Request.cursor; $index++) {
            Set-NativeProbeStage 'get-item'
            if ([int] (Invoke-77MethodAny $obj 'GetItem' ([object[]] @())) -eq 0) {
                Fail 'IDENTITY_SEED_NOT_FOUND' 'The bounded seed row was not found'
            }
        }
        # CurrentItem is required. Do not fall back to catalog object or Code.
        Set-NativeProbeStage 'current-item'
        $current = Invoke-77MethodAny $obj 'CurrentItem' ([object[]] @())
        Set-NativeProbeStage 'serialize-seed'
        $token = Get-NativeReferenceToken $current
        if ($null -eq $token) { Fail 'NATIVE_IDENTITY_UNAVAILABLE' 'Internal reference serialization is unavailable' }
        # Deserialize only the token generated here from the allowed catalog.
        # Requests cannot supply ValueFromStringInternal input or executable code.
        $resolved = Resolve-NativeProbeReference $App $token
        Set-NativeProbeStage 'serialize-resolved'
        $resolvedToken = Get-NativeReferenceToken $resolved
        $resolvedSerializationDetail = $script:NativeProbeCaughtDetail
        Set-NativeProbeStage 'check-selected'
        $selected = Get-Fixed77MethodValue $resolved 'Selected'
        if ($null -eq $resolvedToken -or -not ([string]::Equals($token, $resolvedToken, [StringComparison]::Ordinal)) -or $null -eq $selected -or [int] $selected -ne 1) {
            if ($null -eq $resolvedToken) {
                $script:NativeProbeStage = 'serialize-resolved'
                $script:NativeProbeCaughtDetail = $resolvedSerializationDetail
            }
            Fail 'NATIVE_IDENTITY_ROUNDTRIP_FAILED' 'Addressed reference roundtrip did not resolve the same selected identity'
        }
        $unsupported = New-Object System.Collections.ArrayList
        Set-NativeProbeStage 'metadata-attributes'
        $attributes = if (Get-DatasetMetadataAllFields $Dataset) { @($MetadataObject.attributes) } else { Select-MetadataAttributes @($MetadataObject.attributes) @($Dataset.fields) }
        Set-NativeProbeStage 'serialize-rows'
        $seedRow = Convert-ReferenceValue $current (New-StandardAttribute 'seed' 'U' ([string] $MetadataObject.id)) $unsupported 'identity-seed'
        $resolvedRow = Convert-ReferenceValue $resolved (New-StandardAttribute 'resolved' 'U' ([string] $MetadataObject.id)) $unsupported 'identity-resolved'
        Set-NativeProbeStage 'serialize-attributes'
        $attributeValues = Convert-AttributeMap $resolved $attributes $unsupported 'identity-resolved'
        return @{
            kind = 'reference_identity_probe'; type = [string] $Dataset.type
            catalogMetadataId = [string] $MetadataObject.id
            seedCursor = [string] $Request.cursor
            nativeReference = $token; resolvedNativeReference = $resolvedToken
            roundtripStatus = 'matched-selected'; stableIdentity = 'unverified'; snapshotStatus = 'none'
            identityScope = 'same-information-base-only; base continuity and lifetime unverified'
            seed = $seedRow
            resolved = $resolvedRow
            attributes = $attributeValues
            unsupported = @($unsupported.ToArray())
        }
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

# Namespace identifies configured source location, not immutable database continuity.
function Get-NativeSourceNamespace($Source) {
    $path = [IO.Path]::GetFullPath([string] $Source.databasePath).TrimEnd('\').ToLowerInvariant()
    $bytes = [Text.Encoding]::UTF8.GetBytes([string] $Source.alias + [char] 0 + $path)
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return 'sha256:' + ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Assert-NativeDocumentMetadata($Request, $Dataset, $MetadataObject) {
    Set-NativeProbeStage 'metadata-document'
    if ($Request.metadataKind -cne 'document' -or
        -not [string]::Equals([string] $Request.documentMetadataId, [string] $MetadataObject.id, [StringComparison]::Ordinal) -or
        -not [string]::Equals([string] $Request.documentType, [string] $Dataset.type, [StringComparison]::Ordinal) -or
        -not [string]::Equals([string] $MetadataObject.name, [string] $Dataset.type, [StringComparison]::Ordinal)) {
        Fail 'NATIVE_DOCUMENT_MISMATCH' 'Requested metadata does not match the allowed document'
    }
}

function Get-NativeDocumentHeader($Object, [string] $Token, $MetadataObject) {
    Set-NativeProbeStage 'serialize-header'
    $rawDate = Get-77PropertyAny $Object 'DocDate'
    $rawNumber = Get-77PropertyAny $Object 'DocNum'
    if ($null -eq $rawDate -or $null -eq $rawNumber) { Fail 'NATIVE_DOCUMENT_MISMATCH' 'Native document header is unavailable' }
    $date = [DateTime] $rawDate
    return [pscustomobject] [ordered] @{
        valueKind = 'document'; selected = $true; nativeReference = $Token
        documentType = [string] $MetadataObject.name; documentMetadataId = [string] $MetadataObject.id
        date = $date.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        number = [string] $rawNumber
        posted = Get-Optional77Boolean $Object 'IsTransacted'
    }
}

function Position-NativeDocument($App, [string] $Token, $Object, [string] $DocumentType) {
    $resolved = Resolve-NativeProbeReference $App $Token
    Set-NativeProbeStage 'check-document-kind'
    $kind = Invoke-77MethodAny $resolved 'Kind' ([object[]] @())
    if ($kind -isnot [string] -or -not [string]::Equals($kind, $DocumentType, [StringComparison]::Ordinal)) {
        Fail 'NATIVE_DOCUMENT_MISMATCH' 'Native value does not have the requested document kind'
    }
    Set-NativeProbeStage 'check-selected'
    $resolvedSelected = Get-Fixed77MethodValue $resolved 'Selected'
    if ($null -eq $resolvedSelected -or [int] $resolvedSelected -ne 1) {
        Fail 'NATIVE_DOCUMENT_NOT_FOUND' 'Native document is not selected'
    }
    Set-NativeProbeStage 'find-document'
    $found = Invoke-77MethodAny $Object 'FindDocument' ([object[]] @($resolved))
    if ($null -eq $found -or [int] $found -ne 1) {
        Fail 'NATIVE_DOCUMENT_NOT_FOUND' 'Native document could not be positioned in the allowed document context'
    }
    Set-NativeProbeStage 'current-document'
    $current = Invoke-77MethodAny $Object 'CurrentDocument' ([object[]] @())
    Set-NativeProbeStage 'serialize-resolved'
    $currentToken = Get-NativeReferenceToken $current
    $resolvedToken = Get-NativeReferenceToken $resolved
    Set-NativeProbeStage 'check-selected'
    $selected = Get-Fixed77MethodValue $current 'Selected'
    $contextSelected = Get-Fixed77MethodValue $Object 'Selected'
    Set-NativeProbeStage 'check-document-kind'
    $currentKind = Invoke-77MethodAny $current 'Kind' ([object[]] @())
    $contextKind = Invoke-77MethodAny $Object 'Kind' ([object[]] @())
    if ($null -eq $currentToken -or $null -eq $resolvedToken -or
        -not [string]::Equals($Token, $currentToken, [StringComparison]::Ordinal) -or
        -not [string]::Equals($Token, $resolvedToken, [StringComparison]::Ordinal) -or
        -not [string]::Equals($DocumentType, [string] $currentKind, [StringComparison]::Ordinal) -or
        -not [string]::Equals($DocumentType, [string] $contextKind, [StringComparison]::Ordinal) -or
        $null -eq $selected -or [int] $selected -ne 1 -or $null -eq $contextSelected -or [int] $contextSelected -ne 1) {
        Fail 'NATIVE_IDENTITY_ROUNDTRIP_FAILED' 'Typed document lookup did not preserve the selected native identity'
    }
    return $currentToken
}

function New-NativeSourceNamespaceProof($Request, [string] $Namespace) {
    Set-NativeProbeStage 'source-namespace'
    $expected = Get-OptionalProperty $Request 'sourceNamespace'
    if ($Namespace -cnotmatch '^sha256:[0-9a-f]{64}$' -or
        ($null -ne $expected -and -not [string]::Equals($expected, $Namespace, [StringComparison]::Ordinal))) {
        Fail 'NATIVE_SOURCE_MISMATCH' 'Saved native namespace does not match the configured source'
    }
    return @{scheme='configured-alias-windows-path-sha256-v1'; expected=$expected; observed=$Namespace
        status=$(if ($null -ne $expected) {'matched-request'} else {'observed-unbound'})}
}

function New-NativeDocumentProof($Request, $MetadataObject, [string] $Token, [string] $Namespace) {
    return @{
        metadataKind = 'document'; documentType = [string] $MetadataObject.name
        observedDocumentType = [string] $MetadataObject.name; documentMetadataId = [string] $MetadataObject.id
        nativeReference = $Token; sourceNamespace = $Namespace; roundtripStatus = 'matched-typed-selected'
        stableIdentity = 'unverified'; snapshotStatus = 'none'
        identityScope = 'same-information-base-only; base continuity and lifetime unverified'
        historyLinkStatus = 'unsupported-hold'; historyLinkReason = 'periodic-document-native-transition-unimplemented'
    }
}

function Acquire-DocumentIdentity($App, $Request, $Dataset, $MetadataObject, [string] $Namespace) {
    Assert-NativeDocumentMetadata $Request $Dataset $MetadataObject
    Set-NativeProbeStage 'create-document'
    $obj = Invoke-77CreateObject $App (Get-77ObjectTypeName 'document' ([string] $Dataset.type))
    try {
        $start = [DateTime]::ParseExact($Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact($Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        Set-NativeProbeStage 'select-documents'
        [void] (Invoke-77MethodAny $obj 'SelectDocuments' ([object[]] @($start, $end.AddDays(-1))))
        for ($index = 0; $index -le [long] $Request.documentCursor; $index++) {
            Set-NativeProbeStage 'get-document'
            if ([int] (Invoke-77MethodAny $obj 'GetDocument' ([object[]] @())) -eq 0) {
                Fail 'IDENTITY_SEED_NOT_FOUND' 'The bounded document seed was not found'
            }
        }
        Set-NativeProbeStage 'check-selected'
        $selected = Get-Fixed77MethodValue $obj 'Selected'
        if ($null -eq $selected -or [int] $selected -ne 1) { Fail 'NATIVE_DOCUMENT_NOT_FOUND' 'Seed document is not selected' }
        Set-NativeProbeStage 'current-document'
        $current = Invoke-77MethodAny $obj 'CurrentDocument' ([object[]] @())
        Set-NativeProbeStage 'serialize-seed'
        $token = Get-NativeReferenceToken $current
        if ($null -eq $token) { Fail 'NATIVE_IDENTITY_UNAVAILABLE' 'Internal document serialization is unavailable' }
        $seed = Get-NativeDocumentHeader $obj $token $MetadataObject
        if ([DateTime]::ParseExact($seed.date, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) -lt $start -or
            [DateTime]::ParseExact($seed.date, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) -ge $end) {
            Fail 'NATIVE_DOCUMENT_MISMATCH' 'Seed document is outside the requested period'
        }
        $resolvedToken = Position-NativeDocument $App $token $obj ([string] $Dataset.type)
        $resolved = Get-NativeDocumentHeader $obj $resolvedToken $MetadataObject
        foreach ($name in @('date','number','posted')) {
            if (-not [object]::Equals($seed.$name, $resolved.$name)) {
                Fail 'NATIVE_IDENTITY_ROUNDTRIP_FAILED' 'Seed and resolved document headers differ'
            }
        }
        $result = New-NativeDocumentProof $Request $MetadataObject $token $Namespace
        $result['kind'] = 'document_identity'; $result['seedCursor'] = [string] $Request.documentCursor
        $result['period'] = @{from=$Request.period.from; toExclusive=$Request.period.toExclusive}
        $result['resolvedNativeReference'] = $resolvedToken; $result['seed'] = $seed; $result['resolved'] = $resolved
        return $result
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Select-NativeDocumentAttributes([object[]] $Allowed, [object[]] $Names) {
    $attributes = @()
    foreach ($name in $Names) {
        $matched = @($Allowed | Where-Object { $null -ne $_ -and [string]::Equals([string] $_.name, [string] $name, [StringComparison]::Ordinal) })
        if ($matched.Count -ne 1) { Fail 'DATASET_DENIED' 'Requested document field is not allowed by metadata' }
        $attributes += $matched[0]
    }
    return ,$attributes
}

function Read-NativeDocumentAttributes($Object, [object[]] $Attributes, $Inventory, $Unsupported, [string] $Namespace, [string] $Scope) {
    Set-NativeProbeStage 'serialize-attributes'
    $supported = @($Attributes | Where-Object { $null -ne $_ -and [string] $_.typeCode -cin @('S','N','D','U') })
    $values = Convert-AttributeMap $Object $supported $Unsupported $Scope
    foreach ($attribute in $Attributes) {
        if ([string] $attribute.typeCode -cnotin @('S','N','D','U')) {
            Add-UnsupportedAttribute $Unsupported $Scope $attribute 'metadata-value-type-unverified'
            $values[[string] $attribute.name] = New-UnsupportedValue $attribute 'metadata-value-type-unverified'
        }
    }
    Set-NativeProbeStage 'serialize-relations'
    $relations = [ordered] @{}
    foreach ($attribute in $Attributes) {
        $value = $values[[string] $attribute.name]
        if (-not (Test-ReferenceAttribute $attribute) -and [string] $attribute.typeCode -cne 'U') { continue }
        $relation = [ordered] @{status='unsupported-hold'; reason='native-relation-unavailable'}
        $catalogs = @($Inventory.catalogs | Where-Object { [string]::Equals([string] $_.id, [string] $attribute.refTypeId, [StringComparison]::Ordinal) })
        # Document/enum/ambiguous metadata are explicit gaps, never current-master substitutes.
        if ($null -ne $value -and (Get-OptionalProperty $value 'valueKind') -ceq 'reference' -and
            $value.selected -eq $true -and $value.empty -eq $false -and $null -ne $value.nativeReference -and $catalogs.Count -eq 1) {
            try {
                $raw = Invoke-77MethodAny $Object 'GetAttrib' ([object[]] @([string] $attribute.name))
                $nativeKind = Invoke-77MethodAny $raw 'Kind' ([object[]] @())
                $relationToken = Get-NativeReferenceToken $raw
                $ambiguousDocuments = @($Inventory.documents | Where-Object { [string]::Equals([string] $_.name, [string] $nativeKind, [StringComparison]::Ordinal) })
                if ($ambiguousDocuments.Count -eq 0 -and $nativeKind -is [string] -and
                    [string]::Equals($nativeKind, [string] $catalogs[0].name, [StringComparison]::Ordinal) -and
                    $relationToken -is [string] -and [string]::Equals($relationToken, [string] $value.nativeReference, [StringComparison]::Ordinal)) {
                    $relation = [ordered] @{status='native-typed-unverified'; metadataKind='reference'; sourceNamespace=$Namespace
                        catalogType=$nativeKind; observedCatalogType=$nativeKind
                        catalogMetadataId=[string] $attribute.refTypeId; nativeReference=$relationToken}
                } else { $relation['reason'] = 'document-or-ambiguous-relation-unverified' }
            } catch {}
        }
        if ($relation.status -ceq 'unsupported-hold') {
            Add-UnsupportedAttribute $Unsupported $Scope $attribute ([string] $relation.reason)
        }
        $relations[[string] $attribute.name] = [pscustomobject] $relation
    }
    # Compute field completeness after all relations, preserving other complex gaps.
    $partial = $false
    foreach ($name in $values.Keys) {
        $value = $values[$name]
        if ($null -ne $value -and $value -isnot [string] -and $value -isnot [ValueType]) {
            if (-not $relations.Contains($name) -or $relations[$name].status -cne 'native-typed-unverified') { $partial = $true }
        }
    }
    return @{attributes=$values; relations=$relations; partial=$partial}
}

function Read-DocumentByNative($App, $Request, $Dataset, $MetadataObject, $Inventory, [string] $Namespace) {
    Assert-NativeDocumentMetadata $Request $Dataset $MetadataObject
    Set-NativeProbeStage 'source-namespace'
    if (-not [string]::Equals($Request.sourceNamespace, $Namespace, [StringComparison]::Ordinal)) {
        Fail 'NATIVE_SOURCE_MISMATCH' 'Saved document namespace does not match the configured source'
    }
    Set-NativeProbeStage 'metadata-attributes'
    $headAllowed = if (Get-DatasetMetadataAllFields $Dataset) { @($MetadataObject.headAttributes) } else { Select-MetadataAttributes @($MetadataObject.headAttributes) @($Dataset.fields) }
    $tableAllowed = if (Get-DatasetMetadataAllFields $Dataset) { @($MetadataObject.tableAttributes) } else { @() }
    $head = Select-NativeDocumentAttributes $headAllowed $Request.attributeNames
    $table = Select-NativeDocumentAttributes $tableAllowed $Request.tableAttributeNames
    Set-NativeProbeStage 'create-document'
    $obj = Invoke-77CreateObject $App (Get-77ObjectTypeName 'document' ([string] $Dataset.type))
    try {
        $token = Position-NativeDocument $App $Request.nativeReference $obj ([string] $Dataset.type)
        $header = Get-NativeDocumentHeader $obj $token $MetadataObject
        $unsupported = New-Object System.Collections.ArrayList
        $headerValues = Read-NativeDocumentAttributes $obj $head $Inventory $unsupported $Namespace 'native-document-header'
        $partial = $headerValues.partial
        $declared = $null
        try {
            $rawCount = Invoke-77MethodAny $obj 'LinesCnt' ([object[]] @())
            $declared = [int] $rawCount
            if ($null -eq $rawCount -or $rawCount -is [bool] -or [double] $rawCount -ne $declared -or $declared -lt 0 -or $declared -gt 1000000) { $declared = $null }
        } catch {}
        if ($null -eq $declared) { $partial = $true; [void] $unsupported.Add(@{name='lineCount'; reason='declared-line-count-unavailable'}) }
        if ($null -eq $header.posted) { $partial = $true; [void] $unsupported.Add(@{name='posted'; reason='posted-status-unavailable'}) }
        Set-NativeProbeStage 'select-lines'
        [void] (Invoke-77MethodAny $obj 'SelectLines' ([object[]] @()))
        $offset = [long] $Request.lineCursor
        $exhausted = $false
        for ($index = 0L; $index -lt $offset; $index++) {
            Set-NativeProbeStage 'get-line'
            if ([int] (Invoke-77MethodAny $obj 'GetLine' ([object[]] @())) -eq 0) { $exhausted = $true; break }
        }
        $lines = New-Object System.Collections.ArrayList
        $storedNumbers = [Collections.Generic.HashSet[int]]::new()
        while (-not $exhausted -and $lines.Count -lt [int] $Request.limit) {
            Set-NativeProbeStage 'get-line'
            if ([int] (Invoke-77MethodAny $obj 'GetLine' ([object[]] @())) -eq 0) { $exhausted = $true; break }
            $fields = Read-NativeDocumentAttributes $obj $table $Inventory $unsupported $Namespace 'native-document-line'
            $partial = $partial -or $fields.partial
            $ordinal = [int] ($offset + $lines.Count + 1)
            $storedNumber = $null
            try { $storedNumber = Get-77PropertyAny $obj 'LineNum' } catch {}
            $storedStatus = 'unavailable'
            if ($null -ne $storedNumber) {
                $parsedNumber = 0
                if ($storedNumber -is [bool] -or -not [int]::TryParse([string] $storedNumber, [ref] $parsedNumber) -or
                    $parsedNumber -lt 1 -or -not $storedNumbers.Add($parsedNumber)) {
                    Fail 'NATIVE_LINE_IDENTITY_INVALID' 'Native document stored line identities are contradictory'
                }
                $storedNumber = $parsedNumber; $storedStatus = 'observed'
            }
            [void] $lines.Add(@{lineNumber=$ordinal; sourceLineNumber=$storedNumber; sourceLineNumberStatus=$storedStatus
                attributes=$fields.attributes; relations=$fields.relations})
        }
        $hasMore = $false
        if (-not $exhausted) {
            Set-NativeProbeStage 'get-line'
            $hasMore = [int] (Invoke-77MethodAny $obj 'GetLine' ([object[]] @())) -ne 0
        }
        $next = $null
        if ($hasMore) {
            $next = [string] ($offset + $lines.Count)
            if ([long] $next -gt 10000) { Fail 'DATASET_DENIED' 'Native document line continuation exceeds the bounded budget' }
        }
        if ($null -ne $declared -and ((-not $hasMore -and $offset + $lines.Count -ne $declared) -or
            ($hasMore -and $offset + $lines.Count -ge $declared))) {
            $partial = $true; [void] $unsupported.Add(@{name='lineCount'; reason='line-count-changed'})
        }
        $partial = $partial -or $unsupported.Count -gt 0
        $result = New-NativeDocumentProof $Request $MetadataObject $token $Namespace
        $result['kind'] = 'document_by_native'; $result['document'] = $header
        $result['attributes'] = $headerValues.attributes; $result['relations'] = $headerValues.relations
        $result['lineCursor'] = [string] $Request.lineCursor; $result['lineNumberSemantics'] = 'ordinal-traversal'; $result['lines'] = @($lines.ToArray())
        $result['hasMore'] = $hasMore; $result['nextLineCursor'] = $next; $result['declaredLineCount'] = $declared
        $result['unsupported'] = @($unsupported.ToArray())
        $result['readStatus'] = $(if ($partial) {'partial-hold'} elseif ($hasMore) {'page-complete-unverified'} else {'complete-unverified'})
        return $result
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Convert-ReferenceValue($Value, $Attribute, $Unsupported, [string] $Scope) {
    $result = [ordered] @{
        valueKind = 'reference'
        nativeReference = Get-NativeReferenceToken $Value
        nativeIdentityStatus = 'unverified'
        typeCode = [string] $Attribute.typeCode
        refTypeId = [string] $Attribute.refTypeId
        serializationStatus = 'unverified'
        selected = $null
        empty = $null
        code = $null
        description = $null
        fullCode = $null
        fullDescription = $null
        isGroup = $null
        presentation = $null
    }
    if ($null -eq $Value) {
        $result['serializationStatus'] = 'null'
        return [pscustomobject] $result
    }
    if (-not [Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        Add-UnsupportedAttribute $Unsupported $Scope $Attribute 'reference-value-not-com-object'
        $result['serializationStatus'] = 'unsupported-reference-shape'
        return [pscustomobject] $result
    }

    $selected = Get-Fixed77MethodValue $Value 'Selected'
    if ($null -ne $selected) { $result['selected'] = ([int] $selected -ne 0) }
    $empty = Get-Fixed77MethodValue $Value 'Empty'
    if ($null -ne $empty) { $result['empty'] = ([int] $empty -ne 0) }
    $isGroup = Get-Fixed77MethodValue $Value 'IsGroup'
    if ($null -ne $isGroup) { $result['isGroup'] = ([int] $isGroup -ne 0) }
    $result['code'] = Get-Fixed77StringProperty $Value 'Code'
    $result['description'] = Get-Fixed77StringProperty $Value 'Description'
    $fullCode = Get-Fixed77MethodValue $Value 'FullCode'
    if ($null -ne $fullCode) { $result['fullCode'] = [string] $fullCode }
    $fullDescription = Get-Fixed77MethodValue $Value 'FullDescr'
    if ($null -ne $fullDescription) { $result['fullDescription'] = [string] $fullDescription }
    $result['presentation'] = Get-ValuePresentation $Value

    if ($null -eq $result['selected'] -and $null -eq $result['empty'] -and $null -eq $result['code'] -and
        $null -eq $result['description'] -and $null -eq $result['fullCode'] -and
        $null -eq $result['fullDescription'] -and $null -eq $result['isGroup'] -and
        $null -eq $result['presentation']) {
        Add-UnsupportedAttribute $Unsupported $Scope $Attribute 'reference-identity-unavailable'
        $result['serializationStatus'] = 'unsupported-reference-shape'
    } elseif ($result['empty'] -eq $true -and $null -eq $result['selected'] -and $null -eq $result['code'] -and
        $null -eq $result['description'] -and $null -eq $result['fullCode'] -and
        $null -eq $result['fullDescription'] -and $null -eq $result['isGroup'] -and
        $null -eq $result['presentation']) {
        $result['serializationStatus'] = 'empty-reference-unverified'
    } elseif ($null -eq $result['selected'] -and $null -eq $result['empty'] -and $null -eq $result['code'] -and
        $null -eq $result['description'] -and $null -eq $result['fullCode'] -and
        $null -eq $result['fullDescription'] -and $null -eq $result['isGroup']) {
        $result['serializationStatus'] = 'presentation-only-unverified'
    } else {
        $result['serializationStatus'] = 'structural-unverified'
    }
    return [pscustomobject] $result
}

function Convert-ComplexComValue($Value, $Attribute, $Unsupported, [string] $Scope) {
    $result = [ordered] @{
        valueKind = 'com-object'
        typeCode = [string] $Attribute.typeCode
        refTypeId = [string] $Attribute.refTypeId
        serializationStatus = 'unverified'
        selected = $null
        code = $null
        description = $null
        fullCode = $null
        fullDescription = $null
        isGroup = $null
        date = $null
        number = $null
        posted = $null
        presentation = $null
    }
    $selected = Get-Fixed77MethodValue $Value 'Selected'
    if ($null -ne $selected) { $result['selected'] = ([int] $selected -ne 0) }
    $isGroup = Get-Fixed77MethodValue $Value 'IsGroup'
    if ($null -ne $isGroup) { $result['isGroup'] = ([int] $isGroup -ne 0) }
    $result['code'] = Get-Fixed77StringProperty $Value 'Code'
    $result['description'] = Get-Fixed77StringProperty $Value 'Description'
    $fullCode = Get-Fixed77MethodValue $Value 'FullCode'
    if ($null -ne $fullCode) { $result['fullCode'] = [string] $fullCode }
    $fullDescription = Get-Fixed77MethodValue $Value 'FullDescr'
    if ($null -ne $fullDescription) { $result['fullDescription'] = [string] $fullDescription }
    $date = Get-Fixed77MemberValue $Value 'DocDate'
    if ($date -is [DateTime]) { $result['date'] = $date.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) }
    $number = Get-Fixed77MemberValue $Value 'DocNum'
    if ($null -ne $number) { $result['number'] = [string] $number }
    $posted = Get-Optional77Boolean $Value 'IsTransacted'
    if ($null -ne $posted) { $result['posted'] = $posted }
    $result['presentation'] = Get-ValuePresentation $Value

    $populated = @('selected','code','description','fullCode','fullDescription','isGroup','date','number','posted','presentation') |
        Where-Object { $null -ne $result[$_] }
    if (@($populated).Count -eq 0) {
        Add-UnsupportedAttribute $Unsupported $Scope $Attribute 'com-object-identity-unavailable'
        $result['serializationStatus'] = 'unsupported-com-shape'
    } else {
        $result['serializationStatus'] = 'structural-unverified'
    }
    return [pscustomobject] $result
}

function Test-ReferenceAttribute($Attribute) {
    $refTypeId = [string] $Attribute.refTypeId
    return (-not [string]::IsNullOrWhiteSpace($refTypeId) -and $refTypeId -cne '0')
}

function Convert-BridgeValue($Value, $Attribute, $Unsupported, [string] $Scope) {
    if ((Test-ReferenceAttribute $Attribute) -or [string] $Attribute.typeCode -ceq 'U') {
        return Convert-ReferenceValue $Value $Attribute $Unsupported $Scope
    }
    $scalar = Convert-Scalar $Value
    if ($null -ne $scalar -or $null -eq $Value) { return $scalar }
    if ([Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        return Convert-ComplexComValue $Value $Attribute $Unsupported $Scope
    }
    # Returning ToString() on a COM or other complex value can leak a nonportable rendering.
    Add-UnsupportedAttribute $Unsupported $Scope $Attribute 'non-scalar-value'
    return New-UnsupportedValue $Attribute 'unsupported-non-scalar'
}

function Convert-ConstantValue($Value, $Attribute, $Unsupported) {
    if ($null -ne $Value -and [Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        foreach ($arguments in @(
            ([object[]] @([DateTime]::Now)),
            ([object[]] @())
        )) {
            try {
                $current = Invoke-77MethodAny $Value 'GetValue' $arguments
                return Convert-BridgeValue $current $Attribute $Unsupported 'constant'
            } catch {}
        }
    }
    return Convert-BridgeValue $Value $Attribute $Unsupported 'constant'
}

function Convert-AttributeMap($Object, [object[]] $Attributes, $Unsupported, [string] $Scope) {
    $values = [ordered] @{}
    foreach ($attribute in @($Attributes)) {
        try {
            $raw = Invoke-77MethodAny $Object 'GetAttrib' ([object[]] @([string] $attribute.name))
            $values[[string] $attribute.name] = Convert-BridgeValue $raw $attribute $Unsupported $Scope
        } catch {
            Add-UnsupportedAttribute $Unsupported $Scope $attribute 'attribute-read-failed'
            $values[[string] $attribute.name] = New-UnsupportedValue $attribute 'attribute-read-failed'
        }
    }
    return $values
}

function Read-ReferenceByNative($App, $Request, $Dataset, $MetadataObject, $Inventory, [string] $Namespace) {
    $sourceProof = $null
    if (-not [string]::IsNullOrEmpty($Namespace) -or $null -ne (Get-OptionalProperty $Request 'sourceNamespace')) {
        $sourceProof = New-NativeSourceNamespaceProof $Request $Namespace
    }
    Set-NativeProbeStage 'metadata-catalog'
    if (-not ([string]::Equals([string] $Request.catalogMetadataId, [string] $MetadataObject.id, [StringComparison]::Ordinal)) -or
        -not ([string]::Equals([string] $Request.type, [string] $Dataset.type, [StringComparison]::Ordinal)) -or
        -not ([string]::Equals([string] $MetadataObject.name, [string] $Dataset.type, [StringComparison]::Ordinal))) {
        Fail 'NATIVE_CATALOG_MISMATCH' 'Requested catalog metadata does not match the allowed catalog'
    }
    Set-NativeProbeStage 'metadata-attributes'
    $allowed = if (Get-DatasetMetadataAllFields $Dataset) { @($MetadataObject.attributes) } else { Select-MetadataAttributes @($MetadataObject.attributes) @($Dataset.fields) }
    $attributes = @()
    foreach ($name in $Request.attributeNames) {
        $matchedAttributes = @($allowed | Where-Object { [string]::Equals([string] $_.name, $name, [StringComparison]::Ordinal) })
        if ($matchedAttributes.Count -ne 1) { Fail 'DATASET_DENIED' 'Requested attribute is not allowed by catalog metadata' }
        $attributes += $matchedAttributes[0]
    }
    # Caller token is data through the same closed encoder, never an expression.
    $resolved = Resolve-NativeProbeReference $App $Request.nativeReference
    Set-NativeProbeStage 'check-catalog-kind'
    $resolvedKind = Invoke-77MethodAny $resolved 'Kind' ([object[]] @())
    if ($resolvedKind -isnot [string] -or -not ([string]::Equals($resolvedKind, [string] $Dataset.type, [StringComparison]::Ordinal))) {
        Fail 'NATIVE_CATALOG_MISMATCH' 'Native reference does not have the requested catalog kind'
    }
    Set-NativeProbeStage 'create-catalog'
    $obj = Invoke-77CreateObject $App (Get-77ObjectTypeName 'reference' ([string] $Dataset.type))
    try {
        Set-NativeProbeStage 'use-date'
        $asOfDate = [DateTime]::ParseExact($Request.asOf, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        try {
            [void] (Invoke-77MethodAny $obj 'UseDate' ([object[]] @($asOfDate, 0)))
            $dateStatus = 'applied'
        } catch {
            $script:NativeProbeCaughtDetail = Get-NativeProbeExceptionDetail $_
            Fail 'ASOF_CONTEXT_UNAVAILABLE' 'Requested periodic date context could not be applied'
        }
        # Documented next-selection semantics: date -> typed FindItem -> GetAttrib.
        Set-NativeProbeStage 'find-item'
        $found = Invoke-77MethodAny $obj 'FindItem' ([object[]] @($resolved))
        if ($null -eq $found -or [int] $found -ne 1) { Fail 'NATIVE_REFERENCE_NOT_FOUND' 'Native reference could not be positioned in the allowed catalog' }
        Set-NativeProbeStage 'current-item'
        $current = Invoke-77MethodAny $obj 'CurrentItem' ([object[]] @())
        Set-NativeProbeStage 'serialize-resolved'
        $currentToken = Get-NativeReferenceToken $current
        Set-NativeProbeStage 'check-selected'
        $selected = Get-Fixed77MethodValue $current 'Selected'
        $contextSelected = Get-Fixed77MethodValue $obj 'Selected'
        if ($null -eq $currentToken -or -not ([string]::Equals($Request.nativeReference, $currentToken, [StringComparison]::Ordinal)) -or
            $null -eq $selected -or [int] $selected -ne 1 -or $null -eq $contextSelected -or [int] $contextSelected -ne 1) {
            Fail 'NATIVE_IDENTITY_ROUNDTRIP_FAILED' 'Typed catalog lookup did not preserve the selected native reference'
        }
        $unsupported = New-Object System.Collections.ArrayList
        Set-NativeProbeStage 'serialize-rows'
        $row = Convert-ReferenceValue $current (New-StandardAttribute 'reference' 'U' ([string] $MetadataObject.id)) $unsupported 'addressed-reference'
        Set-NativeProbeStage 'serialize-attributes'
        # Values must come from the date-bound positioned context, not $resolved.
        $values = Convert-AttributeMap $obj $attributes $unsupported 'addressed-reference'
        Set-NativeProbeStage 'serialize-relations'
        $relations = [ordered] @{}
        foreach ($attribute in $attributes) {
            if (-not (Test-ReferenceAttribute $attribute) -and [string] $attribute.typeCode -cne 'U') { continue }
            $value = $values[[string] $attribute.name]
            $catalogs = @($Inventory.catalogs | Where-Object { [string]::Equals([string] $_.id, [string] $attribute.refTypeId, [StringComparison]::Ordinal) })
            $relation = [ordered] @{ status = 'unsupported-hold' }
            # A metadata type stamp alone is not actual native type proof.
            if ($null -ne $value -and $value.valueKind -ceq 'reference' -and $value.selected -eq $true -and $value.empty -eq $false -and
                $null -ne $value.nativeReference -and $catalogs.Count -eq 1) {
                try {
                    $raw = Invoke-77MethodAny $obj 'GetAttrib' ([object[]] @([string] $attribute.name))
                    $nativeKind = Invoke-77MethodAny $raw 'Kind' ([object[]] @())
                    $relationToken = Get-NativeReferenceToken $raw
                    if ($nativeKind -is [string] -and [string]::Equals($nativeKind, [string] $catalogs[0].name, [StringComparison]::Ordinal) -and
                        $relationToken -is [string] -and [string]::Equals($relationToken, [string] $value.nativeReference, [StringComparison]::Ordinal)) {
                        $relation = [ordered] @{ status = 'native-typed-unverified'; catalogType = $nativeKind
                            observedCatalogType = $nativeKind; catalogMetadataId = [string] $attribute.refTypeId; nativeReference = $relationToken }
                    }
                } catch { }
            }
            if ($relation.status -ceq 'unsupported-hold') { Add-UnsupportedAttribute $unsupported 'addressed-relation' $attribute 'native-relation-unavailable' }
            $relations[[string] $attribute.name] = [pscustomobject] $relation
        }
        $partial = $unsupported.Count -gt 0
        foreach ($value in $values.Values) {
            if ($null -ne $value -and $value -isnot [string] -and $value -isnot [ValueType] -and (Get-OptionalProperty $value 'valueKind') -cne 'reference') { $partial = $true }
        }
        $result = @{
            kind = 'reference_by_native'; type = [string] $Dataset.type; observedCatalogType = $resolvedKind
            catalogMetadataId = [string] $MetadataObject.id; nativeReference = $currentToken
            roundtripStatus = 'matched-typed-selected'; stableIdentity = 'unverified'; snapshotStatus = 'none'
            identityScope = 'same-information-base-only; base continuity and lifetime unverified'
            readContext = @{ asOf = $Request.asOf; useDate = $dateStatus; selection = 'FindItem'; periodicStatus = 'date-context-applied-unverified' }
            readStatus = $(if ($partial) { 'partial-hold' } else { 'complete-unverified' })
            reference = $row; attributes = $values; relations = $relations; unsupported = @($unsupported.ToArray())
        }
        if ($null -ne $sourceProof) { $result['sourceNamespace']=$Namespace; $result['sourceNamespaceProof']=$sourceProof }
        return $result
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Add-UnsupportedField($Unsupported, [string] $Scope, [string] $Name, [string] $Reason) {
    [void] $Unsupported.Add([pscustomobject] [ordered] @{
        scope = $Scope
        name = $Name
        reason = $Reason
    })
}

function New-UnsupportedStandardValue([string] $Reason) {
    return [pscustomobject] [ordered] @{
        valueKind = 'unsupported'
        serializationStatus = $Reason
    }
}

function Convert-ReferenceLikeObject($Value, $Unsupported, [string] $Scope, [string] $Name) {
    $result = [ordered] @{
        valueKind = 'reference-like'
        serializationStatus = 'unverified'
        selected = $null
        code = $null
        description = $null
        fullCode = $null
        fullDescription = $null
        isGroup = $null
    }
    if ($null -eq $Value) {
        $result['serializationStatus'] = 'null'
        return [pscustomobject] $result
    }
    if (-not [Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        $scalar = Convert-Scalar $Value
        if ($null -ne $scalar -or $null -eq $Value) { return $scalar }
        Add-UnsupportedField $Unsupported $Scope $Name 'reference-like-value-not-com-object'
        $result['serializationStatus'] = 'unsupported-reference-shape'
        return [pscustomobject] $result
    }

    $selected = Get-Fixed77MethodValue $Value 'Selected'
    if ($null -ne $selected) { $result['selected'] = ([int] $selected -ne 0) }
    $isGroup = Get-Fixed77MethodValue $Value 'IsGroup'
    if ($null -ne $isGroup) { $result['isGroup'] = ([int] $isGroup -ne 0) }
    $result['code'] = Get-Fixed77StringProperty $Value 'Code'
    $result['description'] = Get-Fixed77StringProperty $Value 'Description'
    $fullCode = Get-Fixed77MethodValue $Value 'FullCode'
    if ($null -ne $fullCode) { $result['fullCode'] = [string] $fullCode }
    $fullDescription = Get-Fixed77MethodValue $Value 'FullDescr'
    if ($null -ne $fullDescription) { $result['fullDescription'] = [string] $fullDescription }

    if ($null -eq $result['selected'] -and $null -eq $result['code'] -and
        $null -eq $result['description'] -and $null -eq $result['fullCode'] -and
        $null -eq $result['fullDescription'] -and $null -eq $result['isGroup']) {
        Add-UnsupportedField $Unsupported $Scope $Name 'reference-like-identity-unavailable'
        $result['serializationStatus'] = 'unsupported-reference-shape'
    } else {
        $result['serializationStatus'] = 'structural-unverified'
    }
    return [pscustomobject] $result
}

function Convert-StandardValue($Value, $Unsupported, [string] $Scope, [string] $Name) {
    $scalar = Convert-Scalar $Value
    if ($null -ne $scalar -or $null -eq $Value) { return $scalar }
    if ([Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        return Convert-ReferenceLikeObject $Value $Unsupported $Scope $Name
    }
    Add-UnsupportedField $Unsupported $Scope $Name 'non-scalar-value'
    return New-UnsupportedStandardValue 'unsupported-non-scalar'
}

function Get-OptionalStandardProperty($Object, [string] $Name, $Unsupported, [string] $Scope, [string] $JsonName) {
    try {
        $value = Get-77PropertyAny $Object $Name
        return Convert-StandardValue $value $Unsupported $Scope $JsonName
    } catch {
        Add-UnsupportedField $Unsupported $Scope $JsonName 'standard-field-unavailable'
        return New-UnsupportedStandardValue 'standard-field-unavailable'
    }
}

function Get-OptionalStandardMethodValue($Object, [string] $Name, [object[]] $Arguments, $Unsupported, [string] $Scope, [string] $JsonName) {
    try {
        $value = Invoke-77MethodAny $Object $Name $Arguments
        return Convert-StandardValue $value $Unsupported $Scope $JsonName
    } catch {
        Add-UnsupportedField $Unsupported $Scope $JsonName 'standard-method-unavailable'
        return New-UnsupportedStandardValue 'standard-method-unavailable'
    }
}

function Get-OptionalStandardIntMethod($Object, [string] $Name, $Unsupported, [string] $Scope, [string] $JsonName) {
    try { return [int] (Invoke-77MethodAny $Object $Name ([object[]] @())) }
    catch {
        Add-UnsupportedField $Unsupported $Scope $JsonName 'standard-method-unavailable'
        return $null
    }
}

function Get-OptionalOperationTime($OperationObject, $Unsupported) {
    try {
        return [string] (Invoke-77MethodAny $OperationObject 'GetTime' ([object[]] @()))
    } catch {
        Add-UnsupportedField $Unsupported 'operation' 'time' 'standard-method-unavailable'
        return $null
    }
}

function Get-77OperationDocument($OperationObject) {
    $firstNonNull = $null
    foreach ($name in @('Document', (Get-77RuName 'Document'))) {
        if ([string]::IsNullOrWhiteSpace([string] $name)) { continue }
        try {
            $value = Get-77Property $OperationObject ([string] $name)
            if ($null -ne $value) {
                if ([Runtime.InteropServices.Marshal]::IsComObject($value)) { return $value }
                if ($null -eq $firstNonNull) { $firstNonNull = $value }
            }
        } catch {
            if (-not (Test-77MemberMissing $_)) { throw }
        }
    }
    foreach ($name in @('GetDocument', (Get-77RuName 'GetDocument'))) {
        if ([string]::IsNullOrWhiteSpace([string] $name)) { continue }
        try {
            $value = Invoke-77Method $OperationObject ([string] $name) ([object[]] @())
            if ($null -ne $value) {
                if ([Runtime.InteropServices.Marshal]::IsComObject($value)) { return $value }
                if ($null -eq $firstNonNull) { $firstNonNull = $value }
            }
        } catch {
            if (-not (Test-77MemberMissing $_)) { throw }
        }
    }
    return $firstNonNull
}

function Convert-SourceDocumentLink($OperationObject, $Unsupported) {
    $document = $null
    try { $document = Get-77OperationDocument $OperationObject } catch {}
    $result = [ordered] @{
        serializationStatus = 'unverified'
        date = $null
        number = $null
        selected = $null
        code = $null
        description = $null
        identityStatus = 'unverified'
        type = 'unknown'
    }
    if ($null -eq $document) {
        Add-UnsupportedField $Unsupported 'operation-document' 'document' 'document-link-unavailable'
        $result['serializationStatus'] = 'unsupported-document-link'
        return [pscustomobject] $result
    }
    if (-not [Runtime.InteropServices.Marshal]::IsComObject($document)) {
        Add-UnsupportedField $Unsupported 'operation-document' 'document' 'document-link-not-com-object'
        $result['serializationStatus'] = 'unsupported-document-link'
        return [pscustomobject] $result
    }
    try {
        $docDate = [DateTime] (Get-77PropertyAny $document 'DocDate')
        $result['date'] = $docDate.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    } catch {
        Add-UnsupportedField $Unsupported 'operation-document' 'date' 'document-date-unavailable'
    }
    try { $result['number'] = [string] (Get-77PropertyAny $document 'DocNum') }
    catch { Add-UnsupportedField $Unsupported 'operation-document' 'number' 'document-number-unavailable' }
    try { $result['type'] = [string] (Invoke-77MethodAny $document 'Kind' ([object[]] @())) }
    catch { Add-UnsupportedField $Unsupported 'operation-document' 'type' 'document-kind-unavailable' }
    $selected = Get-Fixed77MethodValue $document 'Selected'
    if ($null -ne $selected) { $result['selected'] = ([int] $selected -ne 0) }
    $result['code'] = Get-Fixed77StringProperty $document 'Code'
    $result['description'] = Get-Fixed77StringProperty $document 'Description'
    if ($null -eq $result['date'] -and $null -eq $result['number'] -and
        $null -eq $result['code'] -and $null -eq $result['description'] -and
        ([string] $result['type']) -ceq 'unknown') {
        $result['serializationStatus'] = 'unsupported-document-link'
    } else {
        $result['serializationStatus'] = 'structural-unverified'
    }
    return [pscustomobject] $result
}

function Convert-OperationStandardFields($OperationObject, [long] $Offset, $Unsupported) {
    $date = Get-OptionalStandardProperty $OperationObject 'OperDate' $Unsupported 'operation' 'date'
    if ($date -is [string] -and $date.Length -ge 10) { $date = $date.Substring(0, 10) }
    return [ordered] @{
        operationCursor = [string] $Offset
        date = $date
        time = (Get-OptionalOperationTime $OperationObject $Unsupported)
        description = (Get-OptionalStandardProperty $OperationObject 'OperationDescription' $Unsupported 'operation' 'description')
        operationSum = (Get-OptionalStandardProperty $OperationObject 'OperSum' $Unsupported 'operation' 'operationSum')
        document = (Convert-SourceDocumentLink $OperationObject $Unsupported)
        entryCount = (Get-OptionalStandardIntMethod $OperationObject 'EntriesCount' $Unsupported 'operation' 'entryCount')
    }
}

function Convert-EntrySide($SideObject, $Unsupported, [string] $Scope) {
    $result = [ordered] @{
        account = $null
        subconto = @()
        serializationStatus = 'unverified'
    }
    if ($null -eq $SideObject -or -not [Runtime.InteropServices.Marshal]::IsComObject($SideObject)) {
        Add-UnsupportedField $Unsupported $Scope 'side' 'entry-side-unavailable'
        $result['serializationStatus'] = 'unsupported-entry-side'
        return [pscustomobject] $result
    }
    try {
        $account = Get-77PropertyAny $SideObject 'Account'
        $result['account'] = Convert-ReferenceLikeObject $account $Unsupported $Scope 'account'
    } catch {
        Add-UnsupportedField $Unsupported $Scope 'account' 'account-unavailable'
        $result['account'] = New-UnsupportedStandardValue 'account-unavailable'
    }
    $subconto = New-Object System.Collections.ArrayList
    for ($index = 1; $index -le 3; $index++) {
        try {
            $value = Invoke-77MethodAny $SideObject 'Subconto' ([object[]] @($index))
            [void] $subconto.Add([pscustomobject] [ordered] @{
                index = $index
                value = (Convert-StandardValue $value $Unsupported $Scope ('subconto' + [string] $index))
            })
        } catch {
            [void] $subconto.Add([pscustomobject] [ordered] @{
                index = $index
                value = (New-UnsupportedStandardValue 'subconto-unavailable')
            })
        }
    }
    $result['subconto'] = @($subconto.ToArray())
    $result['serializationStatus'] = 'structural-unverified'
    return [pscustomobject] $result
}

function Convert-OperationEntry($OperationObject, [long] $EntryOffset, $Unsupported) {
    $entry = [ordered] @{
        entryCursor = [string] $EntryOffset
        number = (Get-OptionalStandardIntMethod $OperationObject 'EntryNumber' $Unsupported 'entry' 'number')
        correspondenceNumber = (Get-OptionalStandardIntMethod $OperationObject 'CorrespondenceNumber' $Unsupported 'entry' 'correspondenceNumber')
        complexEntry = $null
        docLineNumber = (Get-OptionalStandardIntMethod $OperationObject 'DocLineNum' $Unsupported 'entry' 'docLineNumber')
        sum = (Get-OptionalStandardProperty $OperationObject 'Sum' $Unsupported 'entry' 'sum')
        currency = (Get-OptionalStandardProperty $OperationObject 'Currency' $Unsupported 'entry' 'currency')
        currencyAmount = (Get-OptionalStandardProperty $OperationObject 'CurrencyAmount' $Unsupported 'entry' 'currencyAmount')
        amount = (Get-OptionalStandardProperty $OperationObject 'Amount' $Unsupported 'entry' 'amount')
        debit = $null
        credit = $null
        presentation = (Get-OptionalStandardMethodValue $OperationObject 'EntryPresentation' ([object[]] @(1)) $Unsupported 'entry' 'presentation')
        subcontoPresentation = (Get-OptionalStandardMethodValue $OperationObject 'SubcontoPresentation' ([object[]] @()) $Unsupported 'entry' 'subcontoPresentation')
    }
    try {
        $entry['complexEntry'] = ([int] (Invoke-77MethodAny $OperationObject 'ComplexEntry' ([object[]] @())) -ne 0)
    } catch {
        Add-UnsupportedField $Unsupported 'entry' 'complexEntry' 'standard-method-unavailable'
    }
    try {
        $entry['debit'] = Convert-EntrySide (Get-77PropertyAny $OperationObject 'Debit') $Unsupported 'entry-debit'
    } catch {
        Add-UnsupportedField $Unsupported 'entry-debit' 'side' 'entry-side-unavailable'
        $entry['debit'] = [pscustomobject] [ordered] @{ account = $null; subconto = @(); serializationStatus = 'unsupported-entry-side' }
    }
    try {
        $entry['credit'] = Convert-EntrySide (Get-77PropertyAny $OperationObject 'Credit') $Unsupported 'entry-credit'
    } catch {
        Add-UnsupportedField $Unsupported 'entry-credit' 'side' 'entry-side-unavailable'
        $entry['credit'] = [pscustomobject] [ordered] @{ account = $null; subconto = @(); serializationStatus = 'unsupported-entry-side' }
    }
    return [pscustomobject] $entry
}

function Get-DocumentLineNumber($DocumentObject, [int] $Fallback) {
    try { return [int] (Get-77PropertyAny $DocumentObject 'LineNum') } catch { return $Fallback }
}

function Read-InlineDocumentLines($DocumentObject, [object[]] $TableAttributes, $Unsupported, [int] $RemainingLineBudget) {
    $lines = New-Object System.Collections.ArrayList
    $status = 'not-applicable-no-table-attributes'
    $declaredLineCount = $null
    if (@($TableAttributes).Count -eq 0) {
        return [pscustomobject] [ordered] @{
            lines = @()
            status = $status
            declaredLineCount = $declaredLineCount
            nextLineCursor = $null
        }
    }
    if ($RemainingLineBudget -lt 1) {
        return [pscustomobject] [ordered] @{
            lines = @()
            status = 'requires-line-page-page-budget'
            declaredLineCount = $null
            nextLineCursor = '0'
        }
    }
    try {
        $declaredLineCount = [int] (Invoke-77MethodAny $DocumentObject 'LinesCnt' ([object[]] @()))
    } catch {
        $declaredLineCount = $null
    }
    if ($null -eq $declaredLineCount) {
        return [pscustomobject] [ordered] @{
            lines = @()
            status = 'requires-line-page-count-unverified'
            declaredLineCount = $null
            nextLineCursor = '0'
        }
    }
    $inlineLimit = [Math]::Min($InlineDocumentLineLimit, $RemainingLineBudget)
    if ($declaredLineCount -gt $inlineLimit) {
        return [pscustomobject] [ordered] @{
            lines = @()
            status = 'requires-line-page'
            declaredLineCount = $declaredLineCount
            nextLineCursor = '0'
        }
    }
    try {
        [void] (Invoke-77MethodAny $DocumentObject 'SelectLines' ([object[]] @()))
        $status = 'ok-unverified'
        while ($true) {
            $exists = Invoke-77MethodAny $DocumentObject 'GetLine' ([object[]] @())
            if ([int] $exists -eq 0) { break }
            if ($lines.Count -ge $inlineLimit) {
                return [pscustomobject] [ordered] @{
                    lines = @()
                    status = 'requires-line-page'
                    declaredLineCount = $declaredLineCount
                    nextLineCursor = '0'
                }
            }
            $lineNumber = Get-DocumentLineNumber $DocumentObject ($lines.Count + 1)
            $lineAttributes = Convert-AttributeMap $DocumentObject $TableAttributes $Unsupported 'document-line'
            [void] $lines.Add([pscustomobject] [ordered] @{
                lineNumber = $lineNumber
                attributes = $lineAttributes
            })
        }
    } catch {
        Add-UnsupportedAttribute $Unsupported 'document-line' ([pscustomobject] @{ name = '*'; typeCode = ''; refTypeId = '' }) 'line-read-failed'
        $status = 'unsupported-line-read-failed'
    }
    return [pscustomobject] [ordered] @{
        lines = @($lines.ToArray())
        status = $status
        declaredLineCount = $declaredLineCount
        nextLineCursor = $(if ($status -eq 'ok-unverified') { $null } else { '0' })
    }
}

function Invoke-ReadStage([string] $Code, [scriptblock] $Action) {
    try { return (& $Action) }
    catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        $stage = if ($Code -ceq 'READ_CREATE_OBJECT_FAILED') { 'create-object' } else { $Code }
        $script:BridgeErrorDetail = Get-SanitizedExceptionDetail $_ $stage
        Fail $Code '1C 7.7 read failed at a sanitized adapter stage'
    }
}

function Read-Page($App, $Dataset, $Request, [string] $Kind, $MetadataObject) {
    $obj = $null
    $readStage = 'start'
    try {
        $readStage = 'create-object'
        $typeName = Get-77ObjectTypeName $Kind ([string] $Dataset.type)
        $obj = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App $typeName }
        if ($null -eq $obj) { Fail 'DATASET_UNAVAILABLE' '1C object could not be created' }
        if ($Kind -eq 'document') {
            $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
            $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
            $readStage = 'select-documents'
            Invoke-ReadStage 'READ_SELECT_DOCUMENTS_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectDocuments' ([object[]] @($start, $end))) }
        } else {
            $readStage = 'select-reference'
            $referenceUseDateStatus = Invoke-77UseDateIfAvailable $obj ([DateTime]::Today) $false
            Invoke-ReadStage 'READ_SELECT_ITEMS_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectItems' ([object[]] @(0))) }
        }
        $cursor = Get-OptionalProperty $Request 'cursor'
        $offset = if ($null -eq $cursor) { 0L } else { [long] $cursor }
        $seen = 0L
        $rows = New-Object System.Collections.ArrayList
        $unsupported = New-Object System.Collections.ArrayList
        $hasMore = $false
        $metadataAllFields = Get-DatasetMetadataAllFields $Dataset
        $selectedFields = @($Dataset.fields)
        $headAttributes = @()
        $tableAttributes = @()
        if ($Kind -eq 'document') {
            if ($metadataAllFields) {
                $headAttributes = @($MetadataObject.headAttributes)
                $tableAttributes = @($MetadataObject.tableAttributes)
            } else {
                $headAttributes = Select-MetadataAttributes @($MetadataObject.headAttributes) $selectedFields
                $tableAttributes = @()
            }
        } else {
            if ($metadataAllFields) {
                $headAttributes = @($MetadataObject.attributes)
            } else {
                $headAttributes = Select-MetadataAttributes @($MetadataObject.attributes) $selectedFields
            }
        }
        $remainingInlineLineBudget = $InlineDocumentLinePageBudget
        $linesRequired = $false
        while ($true) {
            $readStage = if ($Kind -eq 'document') { 'get-document' } else { 'get-reference-item' }
            $exists = if ($Kind -eq 'document') {
                Invoke-ReadStage 'READ_GET_DOCUMENT_FAILED' { Invoke-77MethodAny $obj 'GetDocument' ([object[]] @()) }
            } else {
                Invoke-ReadStage 'READ_GET_ITEM_FAILED' { Invoke-77MethodAny $obj 'GetItem' ([object[]] @()) }
            }
            if ([int] $exists -eq 0) { break }
            if ($seen -lt $offset) { $seen++; continue }
            if ($rows.Count -ge [int] $Request.limit) { $hasMore = $true; break }
            $row = [ordered]@{}
            if ($Kind -eq 'document') {
                $readStage = 'document-standard-fields'
                $docDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'DocDate') }
                if ($docDate -lt $start -or $docDate -ge $end.AddDays(1)) { $seen++; continue }
                $row['documentCursor'] = [string] $seen
                $row['date'] = $docDate.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
                $row['number'] = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [string] (Get-77PropertyAny $obj 'DocNum') }
                $row['posted'] = Get-Optional77Boolean $obj 'IsTransacted'
            } else {
                $readStage = 'reference-standard-fields'
                $row['code'] = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [string] (Get-77PropertyAny $obj 'Code') }
                $row['description'] = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [string] (Get-77PropertyAny $obj 'Description') }
                $row['isGroup'] = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { ([int] (Invoke-77MethodAny $obj 'IsGroup' ([object[]] @())) -ne 0) }
                try { $nativeCurrent = Invoke-77MethodAny $obj 'CurrentItem' ([object[]] @()) } catch { $nativeCurrent = $null }
                $row['nativeReference'] = Get-NativeReferenceToken $nativeCurrent
                $row['nativeIdentityStatus'] = 'unverified'
                $row['fullCode'] = Get-Optional77String $obj 'FullCode'
                $row['fullDescription'] = Get-Optional77String $obj 'FullDescr'
                $row['parent'] = Get-Optional77Reference $obj 'Parent' $unsupported 'reference-standard'
                $row['owner'] = Get-Optional77Reference $obj 'Owner' $unsupported 'reference-standard'
            }
            $attributeScope = if ($Kind -eq 'document') { 'document-header' } else { 'reference' }
            $readStage = $attributeScope + '-attributes'
            $row['attributes'] = Convert-AttributeMap $obj $headAttributes $unsupported $attributeScope
            if ($Kind -eq 'document') {
                $readStage = 'document-inline-lines'
                $lineResult = Read-InlineDocumentLines $obj $tableAttributes $unsupported $remainingInlineLineBudget
                $row['lines'] = @($lineResult.lines)
                $remainingInlineLineBudget -= @($lineResult.lines).Count
                if ($remainingInlineLineBudget -lt 0) { $remainingInlineLineBudget = 0 }
                $row['lineReadStatus'] = [string] $lineResult.status
                $row['declaredLineCount'] = $lineResult.declaredLineCount
                $row['linePageCursor'] = $lineResult.nextLineCursor
                if ($null -ne $lineResult.nextLineCursor) { $linesRequired = $true }
            }
            [void] $rows.Add($row)
            $seen++
        }
        $nextCursor = $null
        if ($hasMore) { $nextCursor = [string] $seen }
        $requestedPeriod = $null
        if ($Kind -eq 'document') {
            $requestedPeriod = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
        }
        $coverageStatus = $(if ($metadataAllFields) { 'metadata-driven-unverified' } else { 'configured-fields-unverified' })
        if ($linesRequired) { $coverageStatus = 'partial-lines-required' }
        if ($unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
        return @{
            kind = $Kind
            type = [string] $Dataset.type
            rows = @($rows.ToArray())
            nextCursor = $nextCursor
            hasMore = $hasMore
            period = $requestedPeriod
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            cursorSemantics = 'provisional-offset; restart if source changes; no snapshot isolation or stable ordering guarantee'
            coverage = @{
                status = $coverageStatus
                metadataOrigin = $(if ($Kind -eq 'document') { '1Cv7.MD document head/table attributes' } else { '1Cv7.MD catalog attributes' })
                headerAttributeCount = @($headAttributes).Count
                tableAttributeCount = @($tableAttributes).Count
                lineReadStatus = $(if ($Kind -eq 'document') { 'attempted-unverified' } else { 'not-applicable' })
                referenceUseDate = $(if ($Kind -eq 'document') { 'not-applicable' } else { $referenceUseDateStatus })
                referencePeriodicAsOf = $(if ($Kind -eq 'document') { $null } else { [DateTime]::Today.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) })
                inlineLineLimit = $(if ($Kind -eq 'document') { $InlineDocumentLineLimit } else { 0 })
                inlineLinePageBudget = $(if ($Kind -eq 'document') { $InlineDocumentLinePageBudget } else { 0 })
                unsupported = @($unsupported.ToArray())
                referenceSerialization = 'structural-unverified; Code/Description/FullCode/FullDescr/IsGroup/Parent/Owner only when exposed by 1C OLE'
            }
            limitations = @('live-read; source may change between pages', 'no verified stable source key or UID for idempotent migration', 'full dataset completeness and chronological uniqueness unverified', 'document line traversal is guarded and unverified on the actual installation', 'large document table parts require read_document_lines_page; read_page does not silently truncate lines')
        }
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'READ_FAILED' ('1C 7.7 read failed at ' + $readStage + '; check dataset, fields and permissions locally')
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function New-DocumentPresenceGroup([string] $DocumentType, [string] $Presentation, $MetadataDocument) {
    $metadata = [ordered] @{
        known = ($null -ne $MetadataDocument)
        id = $null
        headerAttributeCount = 0
        tableAttributeCount = 0
    }
    if ($null -ne $MetadataDocument) {
        $metadata['id'] = [string] $MetadataDocument.id
        $metadata['headerAttributeCount'] = @($MetadataDocument.headAttributes).Count
        $metadata['tableAttributeCount'] = @($MetadataDocument.tableAttributes).Count
    }
    return [ordered] @{
        documentType = $DocumentType
        presentation = $(if ([string]::IsNullOrWhiteSpace($Presentation)) { $null } else { $Presentation })
        count = 0
        firstDate = $null
        lastDate = $null
        postedCount = 0
        unpostedCount = 0
        unknownPostedCount = 0
        metadata = $metadata
    }
}

function Read-DocumentPresencePage($App, $Request, $MetadataInventory) {
    $obj = $null
    try {
        $obj = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App (Get-77GenericDocumentTypeName) }
        if ($null -eq $obj) { Fail 'DATASET_UNAVAILABLE' '1C generic document object could not be created' }
        $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
        Invoke-ReadStage 'READ_SELECT_DOCUMENTS_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectDocuments' ([object[]] @($start, $end))) }
        $metadataByName = Get-MetadataDocumentMap $MetadataInventory
        $cursor = Get-OptionalProperty $Request 'cursor'
        $offset = if ($null -eq $cursor) { 0L } else { [long] $cursor }
        $seen = 0L
        $scanned = 0
        $hasMore = $false
        $groups = @{}
        while ($true) {
            $exists = Invoke-ReadStage 'READ_GET_DOCUMENT_FAILED' { Invoke-77MethodAny $obj 'GetDocument' ([object[]] @()) }
            if ([int] $exists -eq 0) { break }
            $docDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'DocDate') }
            if ($docDate -lt $start -or $docDate -ge $end.AddDays(1)) { continue }
            if ($seen -lt $offset) { $seen++; continue }
            if ($scanned -ge [int] $Request.limit) {
                $hasMore = $true
                break
            }
            $documentType = Invoke-ReadStage 'READ_DOCUMENT_KIND_FAILED' { [string] (Invoke-77MethodAny $obj 'Kind' ([object[]] @())) }
            if ([string]::IsNullOrWhiteSpace($documentType)) {
                Fail 'READ_DOCUMENT_KIND_FAILED' '1C document kind was empty'
            }
            $presentation = Get-Optional77String $obj 'KindPresent'
            $metadataDocument = $null
            if ($metadataByName.ContainsKey($documentType)) {
                $metadataDocument = $metadataByName[$documentType]
            }
            if (-not $groups.ContainsKey($documentType)) {
                $groups[$documentType] = New-DocumentPresenceGroup $documentType $presentation $metadataDocument
            }
            $group = $groups[$documentType]
            $dateText = $docDate.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
            $group['count'] = [int] $group['count'] + 1
            if ($null -eq $group['firstDate'] -or [string] $group['firstDate'] -gt $dateText) { $group['firstDate'] = $dateText }
            if ($null -eq $group['lastDate'] -or [string] $group['lastDate'] -lt $dateText) { $group['lastDate'] = $dateText }
            $posted = Get-Optional77Boolean $obj 'IsTransacted'
            if ($posted -eq $true) { $group['postedCount'] = [int] $group['postedCount'] + 1 }
            elseif ($posted -eq $false) { $group['unpostedCount'] = [int] $group['unpostedCount'] + 1 }
            else { $group['unknownPostedCount'] = [int] $group['unknownPostedCount'] + 1 }
            $scanned++
            $seen++
        }
        $rows = @($groups.Keys | Sort-Object | ForEach-Object { [pscustomobject] $groups[$_] })
        $nextCursor = $null
        if ($hasMore) { $nextCursor = [string] $seen }
        return @{
            kind = 'document_presence'
            groups = $rows
            documentsScanned = $scanned
            nextCursor = $nextCursor
            hasMore = $hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            cursorSemantics = 'provisional-offset-over-generic-document-selection; merge groups by documentType across pages'
            coverage = @{
                status = 'presence-page-unverified'
                metadataOrigin = '1Cv7.MD document list plus live generic document selection'
                unsupported = @()
            }
            limitations = @('live-read; source may change between pages', 'presence page returns aggregate counts only; no document numbers, attributes or lines', 'merge pages by documentType before using counts as source scope')
        }
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'READ_FAILED' '1C 7.7 document presence read failed; check generic document access and permissions locally'
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Read-DocumentLinesPage($App, $Request, $MetadataDocument, $Dataset,
        $SharedDocumentObject = $null, [bool] $DocumentAlreadySelected = $false) {
    $obj = $null
    try {
        if (-not (Get-DatasetMetadataAllFields $Dataset)) {
            Fail 'METADATA_ALL_FIELDS_DISABLED' 'Dataset must explicitly enable metadataAllFields before document line metadata reads'
        }
        $typeName = Get-77ObjectTypeName 'document' ([string] $Request.documentType)
        $obj = if ($null -ne $SharedDocumentObject) { $SharedDocumentObject } else {
            Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App $typeName }
        }
        if ($null -eq $obj) { Fail 'DATASET_UNAVAILABLE' '1C document object could not be created' }
        $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
        if ($null -eq $SharedDocumentObject) {
            Invoke-ReadStage 'READ_SELECT_DOCUMENTS_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectDocuments' ([object[]] @($start, $end))) }
        }
        $documentOffset = [long] $Request.documentCursor
        $lineOffset = [long] $Request.lineCursor
        $documentSeen = 0L
        $unsupported = New-Object System.Collections.ArrayList
        while (-not $DocumentAlreadySelected) {
            $exists = Invoke-ReadStage 'READ_GET_DOCUMENT_FAILED' { Invoke-77MethodAny $obj 'GetDocument' ([object[]] @()) }
            if ([int] $exists -eq 0) {
                return @{
                    kind = 'document_lines'
                    type = [string] $Request.documentType
                    document = $null
                    lines = @()
                    nextCursor = $null
                    hasMore = $false
                    period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
                    snapshotStatus = 'none'
                    stableIdentity = 'unverified'
                    coverage = @{ status = 'document-not-found'; unsupported = @() }
                }
            }
            $docDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'DocDate') }
            if ($docDate -lt $start -or $docDate -ge $end.AddDays(1)) { continue }
            if ($documentSeen -lt $documentOffset) { $documentSeen++; continue }
            break
        }

        $declaredLineCount = $null
        try { $declaredLineCount = [int] (Invoke-77MethodAny $obj 'LinesCnt' ([object[]] @())) } catch {}
        $documentDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'DocDate') }
        $documentNumber = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [string] (Get-77PropertyAny $obj 'DocNum') }
        $documentPosted = Get-Optional77Boolean $obj 'IsTransacted'
        $documentAttributes = Convert-AttributeMap $obj @($MetadataDocument.headAttributes) $unsupported 'document-header'
        Invoke-ReadStage 'READ_SELECT_LINES_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectLines' ([object[]] @())) }
        $skipped = 0L
        while ($skipped -lt $lineOffset) {
            $skipExists = Invoke-ReadStage 'READ_GET_LINE_FAILED' { Invoke-77MethodAny $obj 'GetLine' ([object[]] @()) }
            if ([int] $skipExists -eq 0) { break }
            $skipped++
        }
        $lines = New-Object System.Collections.ArrayList
        $hasMore = $false
        while ($lines.Count -lt [int] $Request.limit) {
            $lineExists = Invoke-ReadStage 'READ_GET_LINE_FAILED' { Invoke-77MethodAny $obj 'GetLine' ([object[]] @()) }
            if ([int] $lineExists -eq 0) { break }
            $lineNumber = Get-DocumentLineNumber $obj ([int] ($lineOffset + $lines.Count + 1))
            $lineAttributes = Convert-AttributeMap $obj @($MetadataDocument.tableAttributes) $unsupported 'document-line'
            [void] $lines.Add([pscustomobject] [ordered] @{
                lineNumber = $lineNumber
                attributes = $lineAttributes
            })
        }
        if ($lines.Count -eq [int] $Request.limit) {
            $probe = Invoke-ReadStage 'READ_GET_LINE_FAILED' { Invoke-77MethodAny $obj 'GetLine' ([object[]] @()) }
            if ([int] $probe -ne 0) { $hasMore = $true }
        }
        $nextCursor = $null
        if ($hasMore) {
            $nextCursor = @{
                documentCursor = [string] $documentOffset
                lineCursor = [string] ($lineOffset + $lines.Count)
            }
        }
        $coverageStatus = 'metadata-driven-unverified'
        if ($unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
        return @{
            kind = 'document_lines'
            type = [string] $Request.documentType
            document = @{
                offset = [string] $documentOffset
                date = $documentDate.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
                number = $documentNumber
                posted = $documentPosted
                attributes = $documentAttributes
                declaredLineCount = $declaredLineCount
            }
            lines = @($lines.ToArray())
            nextCursor = $nextCursor
            hasMore = $hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            coverage = @{
                status = $coverageStatus
                metadataOrigin = '1Cv7.MD document table attributes'
                tableAttributeCount = @($MetadataDocument.tableAttributes).Count
                lineLimit = [int] $Request.limit
                unsupported = @($unsupported.ToArray())
            }
            limitations = @('live-read; source may change between pages', 'line cursor is a provisional offset within one document', 'document identity remains unverified until live proof establishes a stable source key')
        }
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'READ_FAILED' '1C 7.7 document line read failed; check dataset, fields and permissions locally'
    } finally {
        if ($null -eq $SharedDocumentObject -and $null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Read-DocumentLinesBatch($App, $Request, $MetadataDocument, $Dataset) {
    $results = New-Object System.Collections.ArrayList
    $typeName = Get-77ObjectTypeName 'document' ([string] $Request.documentType)
    $sharedObject = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App $typeName }
    try {
        if ($null -eq $sharedObject) { Fail 'DATASET_UNAVAILABLE' '1C document object could not be created' }
        $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        Invoke-ReadStage 'READ_SELECT_DOCUMENTS_FAILED' { [void] (Invoke-77MethodAny $sharedObject 'SelectDocuments' ([object[]] @($start, $end.AddDays(-1)))) }
        $orderedItems = @($Request.items | Sort-Object { [long] $_.documentCursor })
        $currentOffset = -1L
        foreach ($item in $orderedItems) {
            $currentFound = $false
            $itemRequest = [pscustomobject] [ordered]@{
                documentType = [string] $Request.documentType
                period = $Request.period
                documentCursor = [string] $item.documentCursor
                lineCursor = [string] $item.lineCursor
                limit = [int] $item.limit
            }
            try {
                $targetOffset = [long] $item.documentCursor
                while ($currentOffset -lt $targetOffset) {
                    $exists = Invoke-ReadStage 'READ_GET_DOCUMENT_FAILED' { Invoke-77MethodAny $sharedObject 'GetDocument' ([object[]] @()) }
                    if ([int] $exists -eq 0) { break }
                    $docDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $sharedObject 'DocDate') }
                    if ($docDate -lt $start -or $docDate -ge $end) { continue }
                    $currentOffset++
                    $currentFound = $true
                }
                if (-not $currentFound -or $currentOffset -ne $targetOffset) {
                    $page = @{ kind = 'document_lines'; type = [string] $Request.documentType; document = $null; lines = @(); nextCursor = $null; hasMore = $false; period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }; snapshotStatus = 'none'; stableIdentity = 'unverified'; coverage = @{ status = 'document-not-found'; unsupported = @() } }
                } else {
                    $page = Read-DocumentLinesPage $App $itemRequest $MetadataDocument $Dataset $sharedObject $true
                }
                [void] $results.Add([ordered]@{
                    documentCursor = [string] $item.documentCursor
                    lineCursor = [string] $item.lineCursor
                    limit = [int] $item.limit
                    status = 'ok'
                    data = $page
                })
            } catch {
                # Expose only a fixed, coarse error enum. Exception text can contain
                # COM details, local paths or source-specific information.
                $internalCode = ''
                $match = [regex]::Match([string] $_.Exception.Message, '^([A-Z][A-Z0-9_]{0,79})\|')
                if ($match.Success) { $internalCode = $match.Groups[1].Value }
                $publicCode = switch -CaseSensitive ($internalCode) {
                    { $_ -in @('READ_CREATE_OBJECT_FAILED','DATASET_UNAVAILABLE','READ_SELECT_DOCUMENTS_FAILED','READ_GET_DOCUMENT_FAILED','READ_BASE_FIELD_FAILED') } { 'DOCUMENT_READ_FAILED'; break }
                    { $_ -in @('READ_SELECT_LINES_FAILED','READ_GET_LINE_FAILED') } { 'LINE_READ_FAILED'; break }
                    'UNSUPPORTED_VALUE' { 'UNSUPPORTED_VALUE'; break }
                    default { 'ITEM_READ_FAILED' }
                }
                [void] $results.Add([ordered]@{
                    documentCursor = [string] $item.documentCursor
                    lineCursor = [string] $item.lineCursor
                    limit = [int] $item.limit
                    status = 'error'
                    error = @{ code = [string] $publicCode }
                })
            }
        }
    } finally {
        if ($null -ne $sharedObject -and [Runtime.InteropServices.Marshal]::IsComObject($sharedObject)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($sharedObject) } catch {}
        }
    }
    $resultByCursor = @{}
    foreach ($result in $results) { $resultByCursor[[string] $result.documentCursor] = $result }
    return @{ items = @($Request.items | ForEach-Object { $resultByCursor[[string] $_.documentCursor] }) }
}

function New-PeriodicMetadataLink([string] $Kind, $MetadataCatalog, $MetadataAttribute) {
    if ($Kind -ceq 'constant') {
        return [pscustomobject] [ordered] @{
            kind = 'constant'
            constantId = [string] $MetadataAttribute.id
            constantName = [string] $MetadataAttribute.name
            typeCode = [string] $MetadataAttribute.typeCode
            length = $MetadataAttribute.length
            precision = $MetadataAttribute.precision
            refTypeId = [string] $MetadataAttribute.refTypeId
        }
    }
    return [pscustomobject] [ordered] @{
        kind = 'catalog_attribute'
        catalogId = [string] $MetadataCatalog.id
        catalogName = [string] $MetadataCatalog.name
        attributeId = [string] $MetadataAttribute.id
        attributeName = [string] $MetadataAttribute.name
        typeCode = [string] $MetadataAttribute.typeCode
        length = $MetadataAttribute.length
        precision = $MetadataAttribute.precision
        refTypeId = [string] $MetadataAttribute.refTypeId
    }
}

function Add-PeriodicUnsupported($Unsupported, [string] $Scope, [string] $Reason,
        [string] $OwnerCursor, $Attribute) {
    $item = [ordered] @{
        scope = $Scope
        reason = $Reason
        name = [string] $Attribute.name
        typeCode = [string] $Attribute.typeCode
        refTypeId = [string] $Attribute.refTypeId
    }
    if ($null -ne $OwnerCursor) { $item['ownerCursor'] = $OwnerCursor }
    [void] $Unsupported.Add([pscustomobject] $item)
}

function Convert-ReferenceIdentity($Value) {
    $result = [ordered] @{
        serializationStatus = 'unverified'
        selected = $null
        empty = $null
        code = $null
        description = $null
        fullCode = $null
        fullDescription = $null
        isGroup = $null
    }
    if ($null -eq $Value -or -not [Runtime.InteropServices.Marshal]::IsComObject($Value)) {
        $result['serializationStatus'] = 'unsupported-reference-shape'
        return [pscustomobject] $result
    }
    $selected = Get-Fixed77MethodValue $Value 'Selected'
    if ($null -ne $selected) { $result['selected'] = ([int] $selected -ne 0) }
    $empty = Get-Fixed77MethodValue $Value 'Empty'
    if ($null -ne $empty) { $result['empty'] = ([int] $empty -ne 0) }
    $isGroup = Get-Fixed77MethodValue $Value 'IsGroup'
    if ($null -ne $isGroup) { $result['isGroup'] = ([int] $isGroup -ne 0) }
    $result['code'] = Get-Fixed77StringProperty $Value 'Code'
    $result['description'] = Get-Fixed77StringProperty $Value 'Description'
    $fullCode = Get-Fixed77MethodValue $Value 'FullCode'
    if ($null -ne $fullCode) { $result['fullCode'] = [string] $fullCode }
    $fullDescription = Get-Fixed77MethodValue $Value 'FullDescr'
    if ($null -ne $fullDescription) { $result['fullDescription'] = [string] $fullDescription }
    if ($null -eq $result['selected'] -and $null -eq $result['code'] -and
        $null -eq $result['description'] -and $null -eq $result['fullCode'] -and
        $null -eq $result['fullDescription'] -and $null -eq $result['isGroup']) {
        $result['serializationStatus'] = 'unsupported-reference-shape'
    } else {
        $result['serializationStatus'] = 'structural-unverified'
    }
    return [pscustomobject] $result
}

function Get-CurrentReferenceObject($CatalogObject) {
    try {
        $current = Invoke-77MethodAny $CatalogObject 'CurrentItem' ([object[]] @())
        if ($null -ne $current) { return $current }
    } catch {}
    return $CatalogObject
}

function Convert-PeriodicDocumentIdentity($PeriodicObject) {
    try { $document = Invoke-77MethodAny $PeriodicObject 'CurrentDocument' ([object[]] @()) }
    catch { return $null }
    if ($null -eq $document -or -not [Runtime.InteropServices.Marshal]::IsComObject($document)) { return $null }
    $selected = Get-Fixed77MethodValue $document 'Selected'
    $result = [ordered] @{
        serializationStatus = 'structural-unverified'
        nativeTransitionStatus = 'unsupported-hold'
        nativeTransitionReason = 'periodic-document-native-transition-unimplemented'
        selected = $(if ($null -ne $selected) { ([int] $selected -ne 0) } else { $null })
        date = $null
        number = $null
    }
    try {
        $docDate = Get-77PropertyAny $document 'DocDate'
        if ($null -ne $docDate) {
            $result['date'] = ([DateTime] $docDate).ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        }
    } catch {}
    try {
        $docNumber = Get-77PropertyAny $document 'DocNum'
        if ($null -ne $docNumber) { $result['number'] = [string] $docNumber }
    } catch {}
    if ($null -eq $result['selected'] -and $null -eq $result['date'] -and $null -eq $result['number']) {
        return $null
    }
    return [pscustomobject] $result
}

function New-PeriodicCoverage($Unsupported, [string] $MetadataOrigin, $MetadataLink,
        [bool] $ContextUnavailable, [bool] $OwnerScanBudgetExhausted) {
    $coverageStatus = 'metadata-linked-complete-unverified'
    if ($OwnerScanBudgetExhausted) { $coverageStatus = 'partial-owner-scan-budget' }
    if ($Unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
    if ($ContextUnavailable) { $coverageStatus = 'unsupported-periodic-context' }
    return @{
        status = $coverageStatus
        metadataOrigin = $MetadataOrigin
        metadataLink = $MetadataLink
        unsupported = @($Unsupported.ToArray())
        periodicObject = '1C 7.7 Periodic; UseObject, SelectValues, GetValue, Value and DateVal only'
        completeness = 'bounded-page; complete for requested metadata target only when hasMore is false'
        periodicityMetadata = 'unavailable in current 1Cv7.MD parser; link proves metadata existence, not periodic flag'
    }
}

function Open-PeriodicSelection($App, $Attribute, $Owner, [DateTime] $Start, [DateTime] $EndInclusive) {
    $periodic = Invoke-ReadStage 'READ_CREATE_PERIODIC_FAILED' { Invoke-77CreateObject $App 'Periodic' }
    if ($null -eq $periodic) { Fail 'READ_CREATE_PERIODIC_FAILED' '1C Periodic object could not be created' }
    try {
        $useObjectResult = $null
        if ($null -eq $Owner) {
            $useObjectResult = Invoke-ReadStage 'READ_PERIODIC_USE_OBJECT_FAILED' {
                Invoke-77MethodAny $periodic 'UseObject' ([object[]] @([string] $Attribute.name))
            }
        } else {
            $useObjectResult = Invoke-ReadStage 'READ_PERIODIC_USE_OBJECT_FAILED' {
                Invoke-77MethodAny $periodic 'UseObject' ([object[]] @([string] $Attribute.name, $Owner))
            }
        }
        if ([int] $useObjectResult -eq 0) {
            Fail 'READ_PERIODIC_USE_OBJECT_UNSUPPORTED' '1C Periodic object rejected this metadata-linked target'
        }
        Invoke-ReadStage 'READ_PERIODIC_ORDER_FAILED' {
            [void] (Invoke-77MethodAny $periodic 'BackwardOrder' ([object[]] @(0)))
        }
        [void] (Invoke-ReadStage 'READ_PERIODIC_SELECT_FAILED' {
            Invoke-77MethodAny $periodic 'SelectValues' ([object[]] @($Start, $EndInclusive))
        })
        return $periodic
    } catch {
        if ($null -ne $periodic -and [Runtime.InteropServices.Marshal]::IsComObject($periodic)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($periodic) } catch {}
        }
        throw
    }
}

function Read-PeriodicValues($PeriodicObject, $Attribute, $Unsupported, [string] $Scope,
        $Target, $Owner, [string] $OwnerCursor, [long] $HistoryOffset, [int] $Limit,
        $Rows, [string] $NextCursorKind) {
    $valueSeen = 0L
    while ($true) {
        $exists = Invoke-ReadStage 'READ_PERIODIC_GET_VALUE_FAILED' {
            Invoke-77MethodAny $PeriodicObject 'GetValue' ([object[]] @())
        }
        if ([int] $exists -eq 0) { break }
        if ($valueSeen -lt $HistoryOffset) {
            $valueSeen++
            continue
        }
        if ($Rows.Count -ge $Limit) {
            if ($NextCursorKind -ceq 'constant') {
                return [pscustomobject] @{ hasMore = $true; nextCursor = @{ historyCursor = [string] $valueSeen } }
            }
            return [pscustomobject] @{ hasMore = $true; nextCursor = @{ ownerCursor = $OwnerCursor; historyCursor = [string] $valueSeen } }
        }
        $dateValue = Invoke-ReadStage 'READ_PERIODIC_DATE_FAILED' {
            [DateTime] (Get-77PropertyAny $PeriodicObject 'DateVal')
        }
        try {
            $raw = Get-77PropertyAny $PeriodicObject 'Value'
            $value = Convert-BridgeValue $raw $Attribute $Unsupported $Scope
        } catch {
            Add-PeriodicUnsupported $Unsupported $Scope 'periodic-value-read-failed' $OwnerCursor $Attribute
            $value = New-UnsupportedValue $Attribute 'periodic-value-read-failed'
        }
        $row = [ordered] @{
            target = $Target
            owner = $Owner
            ownerCursor = $OwnerCursor
            historyCursor = [string] $valueSeen
            date = $dateValue.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
            value = $value
        }
        $sourceDocument = Convert-PeriodicDocumentIdentity $PeriodicObject
        if ($null -ne $sourceDocument) { $row['sourceDocument'] = $sourceDocument }
        [void] $Rows.Add([pscustomobject] $row)
        $valueSeen++
    }
    return [pscustomobject] @{ hasMore = $false; nextCursor = $null }
}

function Read-PeriodicConstantHistoryPage($App, $Request, $MetadataConstant) {
    $periodic = $null
    $unsupported = New-Object System.Collections.ArrayList
    $rows = New-Object System.Collections.ArrayList
    $metadataLink = New-PeriodicMetadataLink 'constant' $null $MetadataConstant
    $contextUnavailable = $false
    try {
        $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
        $historyCursor = Get-OptionalProperty $Request 'historyCursor'
        $historyOffset = if ($null -eq $historyCursor) { 0L } else { [long] $historyCursor }
        try {
            $periodic = Open-PeriodicSelection $App $MetadataConstant $null $start $end
        } catch {
            Add-PeriodicUnsupported $unsupported 'periodic-constant' 'periodic-context-unavailable' $null $MetadataConstant
            $contextUnavailable = $true
        }
        $result = [pscustomobject] @{ hasMore = $false; nextCursor = $null }
        if (-not $contextUnavailable) {
            $result = Read-PeriodicValues $periodic $MetadataConstant $unsupported 'periodic-constant' $metadataLink $null $null $historyOffset ([int] $Request.limit) $rows 'constant'
        }
        return @{
            kind = 'periodic_history'
            target = $metadataLink
            rows = @($rows.ToArray())
            nextCursor = $result.nextCursor
            hasMore = [bool] $result.hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            coverage = New-PeriodicCoverage $unsupported '1Cv7.MD constants' $metadataLink $contextUnavailable $false
            limitations = @('live-read; source may change between pages', 'constant periodicity flag is not yet exposed by metadata parser', 'history cursor is a provisional offset within one Periodic selection')
        }
    } finally {
        if ($null -ne $periodic -and [Runtime.InteropServices.Marshal]::IsComObject($periodic)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($periodic) } catch {}
        }
    }
}

function Read-PeriodicCatalogAttributeHistoryPage($App, $Request, $MetadataCatalog, $MetadataAttribute) {
    $catalogObject = $null
    $periodic = $null
    $unsupported = New-Object System.Collections.ArrayList
    $rows = New-Object System.Collections.ArrayList
    $metadataLink = New-PeriodicMetadataLink 'catalog_attribute' $MetadataCatalog $MetadataAttribute
    $contextUnavailable = $false
    $ownerScanBudgetExhausted = $false
    $hasMore = $false
    $nextCursor = $null
    try {
        $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
        $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
        $ownerCursor = Get-OptionalProperty $Request 'ownerCursor'
        $ownerOffset = if ($null -eq $ownerCursor) { 0L } else { [long] $ownerCursor }
        $historyCursor = Get-OptionalProperty $Request 'historyCursor'
        $initialHistoryOffset = if ($null -eq $historyCursor) { 0L } else { [long] $historyCursor }

        $typeName = Get-77ObjectTypeName 'reference' ([string] $MetadataCatalog.name)
        $catalogObject = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App $typeName }
        if ($null -eq $catalogObject) { Fail 'DATASET_UNAVAILABLE' '1C catalog object could not be created' }
        Invoke-ReadStage 'READ_SELECT_ITEMS_FAILED' { [void] (Invoke-77MethodAny $catalogObject 'SelectItems' ([object[]] @(0))) }

        $ownerSeen = 0L
        $ownersVisited = 0
        while ($true) {
            $exists = Invoke-ReadStage 'READ_GET_ITEM_FAILED' { Invoke-77MethodAny $catalogObject 'GetItem' ([object[]] @()) }
            if ([int] $exists -eq 0) { break }
            if ($ownerSeen -lt $ownerOffset) {
                $ownerSeen++
                continue
            }
            if ($rows.Count -ge [int] $Request.limit) {
                $hasMore = $true
                $nextCursor = @{ ownerCursor = [string] $ownerSeen; historyCursor = '0' }
                break
            }
            if ($ownersVisited -ge $PeriodicOwnerScanBudget) {
                $hasMore = $true
                $nextCursor = @{ ownerCursor = [string] $ownerSeen; historyCursor = '0' }
                $ownerScanBudgetExhausted = $true
                break
            }
            $ownersVisited++
            $currentOwner = Get-CurrentReferenceObject $catalogObject
            $ownerIdentity = Convert-ReferenceIdentity $catalogObject
            $historyOffset = if ($ownerSeen -eq $ownerOffset) { $initialHistoryOffset } else { 0L }
            try {
                $periodic = Open-PeriodicSelection $App $MetadataAttribute $currentOwner $start $end
                $valuePage = Read-PeriodicValues $periodic $MetadataAttribute $unsupported 'periodic-catalog-attribute' $metadataLink $ownerIdentity ([string] $ownerSeen) $historyOffset ([int] $Request.limit) $rows 'catalog_attribute'
                if ($valuePage.hasMore) {
                    $hasMore = $true
                    $nextCursor = $valuePage.nextCursor
                    break
                }
            } catch {
                Add-PeriodicUnsupported $unsupported 'periodic-catalog-attribute' 'periodic-context-unavailable' ([string] $ownerSeen) $MetadataAttribute
            } finally {
                if ($null -ne $periodic -and [Runtime.InteropServices.Marshal]::IsComObject($periodic)) {
                    try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($periodic) } catch {}
                }
                $periodic = $null
            }
            $ownerSeen++
        }
        if ($rows.Count -eq 0 -and $unsupported.Count -gt 0 -and -not $hasMore) {
            $contextUnavailable = $true
        }
        return @{
            kind = 'periodic_history'
            target = $metadataLink
            rows = @($rows.ToArray())
            nextCursor = $nextCursor
            hasMore = $hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            coverage = New-PeriodicCoverage $unsupported '1Cv7.MD catalog attributes' $metadataLink $contextUnavailable $ownerScanBudgetExhausted
            limitations = @('live-read; source may change between pages', 'catalog attribute periodicity flag is not yet exposed by metadata parser', 'ownerCursor and historyCursor are provisional offsets without snapshot isolation', 'catalog owner scan is bounded per request')
        }
    } finally {
        if ($null -ne $periodic -and [Runtime.InteropServices.Marshal]::IsComObject($periodic)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($periodic) } catch {}
        }
        if ($null -ne $catalogObject -and [Runtime.InteropServices.Marshal]::IsComObject($catalogObject)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($catalogObject) } catch {}
        }
    }
}

function Read-PeriodicHistoryPage($App, $Request, $Inventory) {
    if ([string] $Request.kind -ceq 'constant') {
        $metadataConstant = Find-MetadataConstant $Inventory ([string] $Request.type)
        return Read-PeriodicConstantHistoryPage $App $Request $metadataConstant
    }
    $resolved = Find-MetadataCatalogAttribute $Inventory ([string] $Request.type) ([string] $Request.attributeName)
    return Read-PeriodicCatalogAttributeHistoryPage $App $Request $resolved.catalog $resolved.attribute
}

function Select-OperationRange($OperationObject, $Request) {
    $start = [DateTime]::ParseExact([string] $Request.period.from, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    $end = [DateTime]::ParseExact([string] $Request.period.toExclusive, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture).AddDays(-1)
    Invoke-ReadStage 'READ_SELECT_OPERATIONS_FAILED' { [void] (Invoke-77MethodAny $OperationObject 'SelectOpers' ([object[]] @($start, $end))) }
    return @{ start = $start; end = $end }
}

function Read-OperationsPage($App, $Request) {
    $obj = $null
    try {
        $obj = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App (Get-77OperationTypeName) }
        if ($null -eq $obj) { Fail 'DATASET_UNAVAILABLE' '1C operation object could not be created' }
        $range = Select-OperationRange $obj $Request
        $cursor = Get-OptionalProperty $Request 'cursor'
        $offset = if ($null -eq $cursor) { 0L } else { [long] $cursor }
        $seen = 0L
        $rows = New-Object System.Collections.ArrayList
        $unsupported = New-Object System.Collections.ArrayList
        $hasMore = $false
        while ($true) {
            $exists = Invoke-ReadStage 'READ_GET_OPERATION_FAILED' { Invoke-77MethodAny $obj 'GetOper' ([object[]] @()) }
            if ([int] $exists -eq 0) { break }
            $operationDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'OperDate') }
            if ($operationDate -lt $range.start -or $operationDate -ge $range.end.AddDays(1)) { $seen++; continue }
            if ($seen -lt $offset) { $seen++; continue }
            if ($rows.Count -ge [int] $Request.limit) { $hasMore = $true; break }
            $row = Convert-OperationStandardFields $obj $seen $unsupported
            [void] $rows.Add([pscustomobject] $row)
            $seen++
        }
        $coverageStatus = 'standard-fields-only-unverified'
        if ($unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
        return @{
            kind = 'operation'
            rows = @($rows.ToArray())
            nextCursor = $(if ($hasMore) { [string] $seen } else { $null })
            hasMore = $hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            cursorSemantics = 'provisional-operation-offset; restart if source changes; no snapshot isolation or stable ordering guarantee'
            coverage = @{
                status = $coverageStatus
                standardFields = @('operationCursor','date','time','description','operationSum','document','entryCount')
                customOperationAttributes = 'unknown-not-enumerated'
                entryTraversal = 'read_operation_entries_page'
                unsupported = @($unsupported.ToArray())
            }
            limitations = @('live-read; source may change between pages', 'operation cursor is a provisional offset', 'custom operation requisites are not enumerated', 'stable operation identity is unverified')
        }
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'READ_FAILED' '1C 7.7 operation read failed; check accounting component, fields and permissions locally'
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Read-OperationEntriesPage($App, $Request) {
    $obj = $null
    try {
        $obj = Invoke-ReadStage 'READ_CREATE_OBJECT_FAILED' { Invoke-77CreateObject $App (Get-77OperationTypeName) }
        if ($null -eq $obj) { Fail 'DATASET_UNAVAILABLE' '1C operation object could not be created' }
        $range = Select-OperationRange $obj $Request
        $operationOffset = [long] $Request.operationCursor
        $entryOffset = [long] $Request.entryCursor
        $seen = 0L
        $unsupported = New-Object System.Collections.ArrayList
        while ($true) {
            $exists = Invoke-ReadStage 'READ_GET_OPERATION_FAILED' { Invoke-77MethodAny $obj 'GetOper' ([object[]] @()) }
            if ([int] $exists -eq 0) {
                return @{
                    kind = 'operation_entries'
                    operation = $null
                    entries = @()
                    nextCursor = $null
                    hasMore = $false
                    period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
                    snapshotStatus = 'none'
                    stableIdentity = 'unverified'
                    coverage = @{ status = 'operation-not-found'; unsupported = @() }
                    limitations = @('operation cursor is a provisional offset; restart from operation page when source changes')
                }
            }
            $operationDate = Invoke-ReadStage 'READ_BASE_FIELD_FAILED' { [DateTime] (Get-77PropertyAny $obj 'OperDate') }
            if ($operationDate -lt $range.start -or $operationDate -ge $range.end.AddDays(1)) { $seen++; continue }
            if ($seen -lt $operationOffset) { $seen++; continue }
            break
        }

        $operation = [pscustomobject] (Convert-OperationStandardFields $obj $operationOffset $unsupported)
        Invoke-ReadStage 'READ_SELECT_ENTRIES_FAILED' { [void] (Invoke-77MethodAny $obj 'SelectEntries' ([object[]] @())) }
        $skipped = 0L
        while ($skipped -lt $entryOffset) {
            $skipExists = Invoke-ReadStage 'READ_GET_ENTRY_FAILED' { Invoke-77MethodAny $obj 'GetEntry' ([object[]] @()) }
            if ([int] $skipExists -eq 0) { break }
            $skipped++
        }
        $entries = New-Object System.Collections.ArrayList
        $hasMore = $false
        while ($entries.Count -lt [int] $Request.limit) {
            $entryExists = Invoke-ReadStage 'READ_GET_ENTRY_FAILED' { Invoke-77MethodAny $obj 'GetEntry' ([object[]] @()) }
            if ([int] $entryExists -eq 0) { break }
            [void] $entries.Add((Convert-OperationEntry $obj ($entryOffset + $entries.Count) $unsupported))
        }
        if ($entries.Count -eq [int] $Request.limit) {
            $probe = Invoke-ReadStage 'READ_GET_ENTRY_FAILED' { Invoke-77MethodAny $obj 'GetEntry' ([object[]] @()) }
            if ([int] $probe -ne 0) { $hasMore = $true }
        }
        $coverageStatus = 'standard-fields-only-unverified'
        if ($unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
        return @{
            kind = 'operation_entries'
            operation = $operation
            entries = @($entries.ToArray())
            nextCursor = $(if ($hasMore) {
                @{ operationCursor = [string] $operationOffset; entryCursor = [string] ($entryOffset + $entries.Count) }
            } else { $null })
            hasMore = $hasMore
            period = @{ from = [string] $Request.period.from; toExclusive = [string] $Request.period.toExclusive }
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            cursorSemantics = 'provisional-entry-offset within one provisional operation offset'
            coverage = @{
                status = $coverageStatus
                standardFields = @('number','correspondenceNumber','complexEntry','docLineNumber','sum','currency','currencyAmount','amount','debit','credit','presentation','subcontoPresentation')
                customEntryAttributes = 'unknown-not-enumerated'
                unsupported = @($unsupported.ToArray())
            }
            limitations = @('live-read; source may change between pages', 'entry cursor is a provisional offset within one operation', 'custom posting requisites are not enumerated', 'debit/credit subconto are limited to three positional probes')
        }
    } catch {
        if ($_.Exception.Message -match '^([A-Z_]+)\|') { throw }
        Fail 'READ_FAILED' '1C 7.7 operation entry read failed; check accounting component, fields and permissions locally'
    } finally {
        if ($null -ne $obj -and [Runtime.InteropServices.Marshal]::IsComObject($obj)) {
            try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($obj) } catch {}
        }
    }
}

function Resolve-ConstantContext($App) {
    $constantNames = @(
        'Constant',
        'Const',
        (New-77StringFromCodes @(0x041A,0x043E,0x043D,0x0441,0x0442,0x0430,0x043D,0x0442,0x0430)),
        (New-77StringFromCodes @(0x041A,0x043E,0x043D,0x0441,0x0442,0x0430,0x043D,0x0442,0x044B))
    )
    foreach ($name in $constantNames) {
        foreach ($flags in @(
            ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::GetProperty),
            ([System.Reflection.BindingFlags]::Public -bor [System.Reflection.BindingFlags]::InvokeMethod)
        )) {
            try {
                $context = Invoke-77AutomationMember $App $name $flags ([object[]] @())
                if ($null -ne $context) { return $context }
            } catch {}
        }
    }
    return $null
}

function Read-ConstantsPage($App, [object[]] $Constants, $Request) {
    $cursor = Get-OptionalProperty $Request 'cursor'
    $offset = if ($null -eq $cursor) { 0 } else { [int] $cursor }
    $rows = New-Object System.Collections.ArrayList
    $unsupported = New-Object System.Collections.ArrayList
    if (@($Constants).Count -eq 0 -or $offset -ge @($Constants).Count) {
        return @{
            kind = 'constants'
            rows = @()
            nextCursor = $null
            hasMore = $false
            snapshotStatus = 'none'
            stableIdentity = 'unverified'
            coverage = @{
                status = 'provisional-constant-context'
                metadataOrigin = '1Cv7.MD constants'
                constantCount = @($Constants).Count
                unsupported = @()
                constantContext = 'not-needed-empty-page'
            }
            limitations = @('constant OLE context is guarded and unverified on the actual installation', 'periodic constant history uses read_periodic_history_page')
        }
    }
    $context = Resolve-ConstantContext $App
    $constantContextStatus = 'fixed-allowlist-unverified'
    if ($null -eq $context) { $constantContextStatus = 'unsupported-context-unavailable' }
    $index = $offset
    while ($index -lt @($Constants).Count -and $rows.Count -lt [int] $Request.limit) {
        $constant = $Constants[$index]
        if ($null -eq $context) {
            Add-UnsupportedAttribute $unsupported 'constant' $constant 'constant-context-unavailable'
            $value = New-UnsupportedValue $constant 'constant-context-unavailable'
        } else {
            try {
                $raw = Invoke-77MethodAny $context 'GetAttrib' ([object[]] @([string] $constant.name))
                $value = Convert-ConstantValue $raw $constant $unsupported
            } catch {
                Add-UnsupportedAttribute $unsupported 'constant' $constant 'constant-read-failed'
                $value = New-UnsupportedValue $constant 'constant-read-failed'
            }
        }
        [void] $rows.Add([pscustomobject] [ordered] @{
            name = [string] $constant.name
            typeCode = [string] $constant.typeCode
            refTypeId = [string] $constant.refTypeId
            value = $value
        })
        $index++
    }
    $hasMore = $index -lt @($Constants).Count
    $coverageStatus = 'provisional-constant-context'
    if ($unsupported.Count -gt 0) { $coverageStatus = 'partial-unsupported-values' }
    if ($null -eq $context) { $coverageStatus = 'unsupported-constant-context' }
    return @{
        kind = 'constants'
        rows = @($rows.ToArray())
        nextCursor = $(if ($hasMore) { [string] $index } else { $null })
        hasMore = $hasMore
        snapshotStatus = 'none'
        stableIdentity = 'unverified'
        coverage = @{
            status = $coverageStatus
            metadataOrigin = '1Cv7.MD constants'
            constantCount = @($Constants).Count
            unsupported = @($unsupported.ToArray())
            constantContext = $constantContextStatus
        }
        limitations = @('constant OLE context is guarded and unverified on the actual installation', 'periodic constant history uses read_periodic_history_page')
    }
}

$response = $null
$request = $null
$app = $null
try {
    $line = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($line)) { Fail 'INVALID_REQUEST' 'One JSON request line is required' }
    if ($line.Length -gt 16384) { Fail 'INVALID_REQUEST' 'Request exceeds the local size limit' }
    try { $request = $line | ConvertFrom-Json }
    catch { Fail 'INVALID_REQUEST' 'Request is not valid JSON' }
    if ((Get-OptionalProperty $request 'operation') -cin @('probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native')) { Set-NativeProbeStage 'request-validation' }
    Assert-Request $request
    if ($null -ne $script:NativeProbeStage) { Set-NativeProbeStage 'config-read' }
    $local = Read-Config $Config
    if ($null -ne $script:NativeProbeStage) { Set-NativeProbeStage 'source-lookup' }
    $source = Find-Source $local ([string] $request.sourceAlias)
    if ($request.operation -eq 'schema') {
        $datasets = @($source.datasets | ForEach-Object {
            $metadataAllFields = Get-DatasetMetadataAllFields $_
            @{
                kind = [string] $_.kind
                type = [string] $_.type
                fields = @($_.fields)
                metadataAllFields = $metadataAllFields
                status = 'configured-unverified'
                content = $(if ([string] $_.kind -eq 'document') {
                    @{
                        headerAttributes = $(if ($metadataAllFields) { 'metadata-driven-all' } else { 'configured-fields' })
                        tableAttributes = $(if ($metadataAllFields) { 'metadata-driven-all' } else { 'not-selected' })
                        lineTraversal = $(if ($metadataAllFields) { 'read_document_lines_page' } else { 'metadataAllFields-required' })
                        referenceSerialization = 'structural-unverified'
                    }
                } else {
                    @{
                        attributes = $(if ($metadataAllFields) { 'metadata-driven-all' } else { 'configured-fields' })
                        periodicHistory = 'read_periodic_history_page for one metadata-linked attribute'
                        referenceSerialization = 'structural-unverified'
                    }
                })
            }
        })
        $response = @{
            schemaVersion = '1.0'; status = 'ok'; operation = 'schema'; sourceAlias = $source.alias
            data = @{
                datasets = $datasets
                supportedKinds = @('document','reference','operation','operationEntry')
                provisionalKinds = @('constant','periodic_history')
                readOperations = @('document_presence','probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native','read_page','read_documents_page','read_document_lines_page','read_document_lines_batch','read_constants_page','read_periodic_history_page','read_operations_page','read_operation_entries_page')
                unsupportedKinds = @('enumeration','chartOfAccounts','account','journal','register','calculation','periodicDocumentSelection','customOperationAttributes','customEntryAttributes')
                sourceNamespace = Get-NativeSourceNamespace $source
                nativeDocument = @{metadataKind='document'; seedCursorMax=100; lineCursorMax=10000; lineLimitMax=100; fieldLimitPerSection=32
                    identityScope='same-information-base-only; base continuity and lifetime unverified'
                    historyLinkStatus='unsupported-hold'; historyLinkReason='periodic-document-native-transition-unimplemented'}
                metadataOrigin = 'explicit-local-allowlist; compare with 1Cv7.MD inventory before trusting coverage'
            }
        }
    } elseif ($request.operation -eq 'metadata_inventory') {
        $response = @{
            schemaVersion = '1.0'; status = 'ok'; operation = 'metadata_inventory'; sourceAlias = $source.alias
            data = Get-MetadataInventory $source
        }
    } else {
        $kind = $null
        $dataset = $null
        $metadataObject = $null
        $metadataConstants = @()
        $metadataInventory = $null
        $adapterStopwatch = $null
        if ($request.operation -in @('read_page','read_documents_page','read_document_lines_page','read_document_lines_batch')) {
            $adapterStopwatch = [Diagnostics.Stopwatch]::StartNew()
        }
        if ($request.operation -cin @('acquire_document_identity','read_document_by_native')) {
            Set-NativeProbeStage 'dataset-lookup'
            $dataset = Find-Dataset $source 'document' ([string] $request.documentType)
            Set-NativeProbeStage 'metadata-document'
            $metadataInventory = Get-MetadataInventory $source
            $metadataObject = Find-MetadataDocument $metadataInventory ([string] $request.documentType)
            Assert-NativeDocumentMetadata $request $dataset $metadataObject
            Set-NativeProbeStage 'source-namespace'
            $nativeNamespace = Get-NativeSourceNamespace $source
            if ($request.operation -ceq 'read_document_by_native' -and
                -not [string]::Equals($request.sourceNamespace, $nativeNamespace, [StringComparison]::Ordinal)) {
                Fail 'NATIVE_SOURCE_MISMATCH' 'Saved document namespace does not match the configured source'
            }
        } elseif ($request.operation -cin @('probe_reference_identity','read_reference_by_native')) {
            Set-NativeProbeStage 'dataset-lookup'
            $dataset = Find-Dataset $source 'reference' ([string] $request.type)
            Set-NativeProbeStage 'metadata-catalog'
            $metadataInventory = Get-MetadataInventory $source
            $metadataObject = Find-MetadataCatalog $metadataInventory ([string] $request.type)
            if ($request.operation -ceq 'read_reference_by_native') {
                $nativeNamespace = Get-NativeSourceNamespace $source
                # Reject an unexpected configured location before opening any COM context.
                [void] (New-NativeSourceNamespaceProof $request $nativeNamespace)
            }
        } elseif ($request.operation -eq 'read_page' -or $request.operation -eq 'read_documents_page') {
            $kind = if ($request.operation -eq 'read_documents_page') { 'document' } else { [string] $request.kind }
            $datasetType = if ($request.operation -eq 'read_documents_page') { [string] $request.documentType } else { [string] $request.type }
            $dataset = Find-Dataset $source $kind $datasetType
            $inventory = Get-MetadataInventory $source
            if ($kind -eq 'document') {
                $metadataObject = Find-MetadataDocument $inventory $datasetType
            } else {
                $metadataObject = Find-MetadataCatalog $inventory $datasetType
            }
        } elseif ($request.operation -eq 'read_document_lines_page') {
            $kind = 'document'
            $dataset = Find-Dataset $source 'document' ([string] $request.documentType)
            $metadataObject = Find-MetadataDocument (Get-MetadataInventory $source) ([string] $request.documentType)
        } elseif ($request.operation -eq 'read_document_lines_batch') {
            $kind = 'document'
            $dataset = Find-Dataset $source 'document' ([string] $request.documentType)
            $metadataObject = Find-MetadataDocument (Get-MetadataInventory $source) ([string] $request.documentType)
            if (-not (Get-DatasetMetadataAllFields $dataset)) {
                Fail 'METADATA_ALL_FIELDS_DISABLED' 'Dataset must explicitly enable metadataAllFields before document line metadata reads'
            }
        } elseif ($request.operation -eq 'document_presence') {
            $metadataInventory = Get-MetadataInventory $source
        } elseif ($request.operation -eq 'read_constants_page') {
            $metadataConstants = @((Get-MetadataInventory $source).constants)
        } elseif ($request.operation -eq 'read_periodic_history_page') {
            $metadataInventory = Get-MetadataInventory $source
            if ([string] $request.kind -eq 'catalog_attribute') {
                [void] (Find-Dataset $source 'reference' ([string] $request.type))
                [void] (Find-MetadataCatalogAttribute $metadataInventory ([string] $request.type) ([string] $request.attributeName))
            } else {
                [void] (Find-MetadataConstant $metadataInventory ([string] $request.type))
            }
        }
        if ($request.operation -cin @('probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native')) { Set-NativeProbeStage 'open-application' }
        $app = Open-Application $local $source
        $script:NativeIdentityApp = $app
        if ($request.operation -eq 'health') {
            $response = @{
                schemaVersion = '1.0'; status = 'ok'; operation = 'health'; sourceAlias = $source.alias
                data = @{
                    comCreated = $true; initialized = $true; readProbe = 'not-run'
                    platformVersion = 'unverified'; supportedVersion = '7.70.027 is the intended target; runtime not verified'
                }
            }
        } elseif ($request.operation -eq 'acquire_document_identity') {
            $page = Acquire-DocumentIdentity $app $request $dataset $metadataObject $nativeNamespace
            $response = @{schemaVersion='1.0'; status='ok'; operation=$request.operation; sourceAlias=$source.alias; data=$page}
        } elseif ($request.operation -eq 'read_document_by_native') {
            $page = Read-DocumentByNative $app $request $dataset $metadataObject $metadataInventory $nativeNamespace
            $response = @{schemaVersion='1.0'; status='ok'; operation=$request.operation; sourceAlias=$source.alias; data=$page}
        } elseif ($request.operation -eq 'read_reference_by_native') {
            $page = Read-ReferenceByNative $app $request $dataset $metadataObject $metadataInventory $nativeNamespace
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'probe_reference_identity') {
            $page = Probe-ReferenceIdentity $app $request $dataset $metadataObject
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'document_presence') {
            $page = Read-DocumentPresencePage $app $request $metadataInventory
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_document_lines_page') {
            $page = Read-DocumentLinesPage $app $request $metadataObject $dataset
            $adapterStopwatch.Stop()
            $page['adapterElapsedMs'] = [long] [Math]::Min(600000, $adapterStopwatch.ElapsedMilliseconds)
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_document_lines_batch') {
            $page = Read-DocumentLinesBatch $app $request $metadataObject $dataset
            $adapterStopwatch.Stop()
            $page['adapterElapsedMs'] = [long] [Math]::Min(600000, $adapterStopwatch.ElapsedMilliseconds)
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_constants_page') {
            $page = Read-ConstantsPage $app $metadataConstants $request
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_periodic_history_page') {
            $page = Read-PeriodicHistoryPage $app $request $metadataInventory
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_operations_page') {
            $page = Read-OperationsPage $app $request
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } elseif ($request.operation -eq 'read_operation_entries_page') {
            $page = Read-OperationEntriesPage $app $request
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        } else {
            $page = Read-Page $app $dataset $request $kind $metadataObject
            if ($request.operation -in @('read_page','read_documents_page')) {
                $adapterStopwatch.Stop()
                $page['adapterElapsedMs'] = [long] [Math]::Min(600000, $adapterStopwatch.ElapsedMilliseconds)
            }
            $response = @{ schemaVersion = '1.0'; status = 'ok'; operation = $request.operation; sourceAlias = $source.alias; data = $page }
        }
    }
    $requestId = Get-OptionalProperty $request 'requestId'
    if ($null -ne $requestId) { $response['requestId'] = [string] $requestId }
} catch {
    $code = 'INTERNAL_ERROR'
    $message = 'Adapter failed without exposing local details'
    if ($_.Exception.Message -match '^([A-Z_]+)\|(.+)$') { $code = $Matches[1]; $message = $Matches[2] }
    $errorOperation = 'unknown'
    $requestedOperation = [string] (Get-OptionalProperty $request 'operation')
    if (@('health','schema','metadata_inventory','document_presence','probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native','read_page','read_documents_page','read_document_lines_page','read_document_lines_batch','read_constants_page','read_periodic_history_page','read_operations_page','read_operation_entries_page') -ccontains $requestedOperation) {
        $errorOperation = $requestedOperation
    }
    $response = @{ schemaVersion = '1.0'; status = 'error'; operation = $errorOperation; error = @{ code = $code; message = $message } }
    if (($errorOperation -ceq 'probe_reference_identity' -or $errorOperation -ceq 'read_reference_by_native' -or
        $errorOperation -cin @('acquire_document_identity','read_document_by_native')) -and $null -ne $script:NativeProbeStage) {
        # No raw exception message, dynamic type name or generic detail escapes native reads.
        if (@(
            'INVALID_REQUEST','INVALID_CONFIG','CONFIG_NOT_FOUND','SOURCE_DENIED','SOURCE_UNAVAILABLE',
            'DATASET_DENIED','METADATA_READER_UNAVAILABLE','METADATA_READ_FAILED','METADATA_CATALOG_UNAVAILABLE',
            'WRONG_BITNESS','COM_NOT_REGISTERED','COM_ACCESS_DENIED','COM_CREATE_FAILED','COM_INIT_FAILED',
            'CREDENTIAL_UNAVAILABLE','INITIALIZE_FAILED',
            'COM_MEMBER_UNAVAILABLE','RUN_MODE_UNAVAILABLE','IDENTITY_SEED_NOT_FOUND',
            'NATIVE_IDENTITY_UNAVAILABLE','NATIVE_IDENTITY_ROUNDTRIP_FAILED',
            'NATIVE_CATALOG_MISMATCH','NATIVE_REFERENCE_NOT_FOUND','ASOF_CONTEXT_UNAVAILABLE',
            'NATIVE_DOCUMENT_MISMATCH','NATIVE_DOCUMENT_NOT_FOUND','NATIVE_SOURCE_MISMATCH','METADATA_DOCUMENT_UNAVAILABLE','NATIVE_LINE_IDENTITY_INVALID'
        ) -cnotcontains $code) { $response.error['code'] = 'NATIVE_PROBE_STAGE_FAILED' }
        $response.error['message'] = 'Native reference probe failed at a sanitized adapter stage'
        $probeDetail = $script:NativeProbeCaughtDetail
        if ($null -eq $probeDetail) { $probeDetail = Get-NativeProbeExceptionDetail $_ }
        $response.error['probeDiagnostic'] = $probeDetail
    } elseif ($null -ne $script:BridgeErrorDetail) {
        $response.error['detail'] = $script:BridgeErrorDetail
    }
    $requestId = Get-OptionalProperty $request 'requestId'
    if ($null -ne $requestId -and [string] $requestId -cmatch '^[A-Za-z0-9._-]{1,80}$') {
        $response['requestId'] = [string] $requestId
    }
} finally {
    if ($null -ne $app -and [Runtime.InteropServices.Marshal]::IsComObject($app)) {
        try { [void] [Runtime.InteropServices.Marshal]::FinalReleaseComObject($app) } catch {}
    }
}
[Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 20 -Compress))

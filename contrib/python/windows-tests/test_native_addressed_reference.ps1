# Pure fake tests of AST-extracted production helpers. No worker entry point/COM.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$worker = Join-Path $PSScriptRoot '../tools/agent-bridge/windows/Invoke-77Bridge.ps1'
$errors = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($worker, [ref] $tokens, [ref] $errors)
if ($errors.Count -ne 0) { throw 'TEST-worker-AST' }
$helpers = @('Read-ReferenceByNative','Resolve-NativeProbeReference','ConvertTo-NativeProbeStringExpression',
    'Convert-AttributeMap','Test-ReferenceAttribute','Get-OptionalProperty',
    'Assert-Request','Assert-Keys','Assert-RequiredKeys','Assert-AbsentKeys','Assert-Identifier','Assert-Alias',
    'Get-NativeSourceNamespace','New-NativeSourceNamespaceProof')
foreach ($name in $helpers) {
    $defs = @($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true))
    if ($defs.Count -ne 1) { throw 'TEST-function-unique' }
    . ([scriptblock]::Create($defs[0].Extent.Text))
}
function Check($Value, [string] $Label) { if (-not $Value) { throw ('TEST-' + $Label) } }
function Fail([string] $Code, [string] $Message) { throw $Code }
function Set-NativeProbeStage([string] $Stage) { $script:stage = $Stage }
function Get-NativeProbeExceptionDetail($ErrorRecord) { return [pscustomobject] @{stage=$script:stage; exceptionType='System.Exception'} }
function Get-DatasetMetadataAllFields($Dataset) { return $true }
function Get-77ObjectTypeName($Kind, $Type) { Check ($Kind -eq 'reference' -and $Type -eq 'SyntheticCatalog') 'exact-created-catalog'; return $Type }
function Invoke-77CreateObject($App, $Type) { [void] $script:calls.Add('Create'); return $script:context }
function Get-NativeReferenceToken($Value) { return $Value.token }
function Get-Fixed77MethodValue($Value, $Name) { Check ($Name -eq 'Selected') 'selected-only'; return $Value.selected }
function New-StandardAttribute($Name, $Type, $Id) { return [pscustomobject] @{ name=$Name; typeCode=$Type; refTypeId=$Id } }
function Add-UnsupportedAttribute($List, $Scope, $Attribute, $Reason) { [void] $List.Add([pscustomobject] @{name=$Attribute.name; reason=$Reason}) }
function New-UnsupportedValue($Attribute, $Reason) { return [pscustomobject] @{valueKind='unsupported'; serializationStatus=$Reason} }
# Serialization is replaced by fake values; call/position/date behavior is production code.
function Convert-ReferenceValue($Value, $Attribute, $Unsupported, $Scope) {
    return [pscustomobject] @{ valueKind='reference'; nativeReference=$Value.token; nativeIdentityStatus='unverified'
        selected=($Value.selected -eq 1); empty=$false; refTypeId=$Attribute.refTypeId; code='001' }
}
function Convert-BridgeValue($Value, $Attribute, $Unsupported, $Scope) {
    if ($Value -is [string]) { return $Value }
    return Convert-ReferenceValue $Value $Attribute $Unsupported $Scope
}
function Invoke-77MethodAny($Object, [string] $Name, [object[]] $Arguments) {
    [void] $script:calls.Add($Name)
    switch ($Name) {
        'EvalExpr' {
            $script:evalCount++
            if ($script:evalCount -eq 1) {
                if ($script:mode -eq 'scalar-only') { return 'different-native' }
                return $script:token
            }
            Check ($script:evalCount -eq 2) 'no-expression-retry'
            return $script:resolved
        }
        'Kind' { return $Object.kind }
        'UseDate' {
            Check ([object]::ReferenceEquals($Object, $script:context)) 'date-context'
            Check ($Arguments[0].ToString('yyyy-MM-dd') -eq '2026-10-02' -and $Arguments[1] -eq 0) 'original-asof-next-selection'
            if ($script:mode -in @('date-missing','date-failed')) { throw 'PRIVATE_CANARY' }
            return $null
        }
        'FindItem' {
            Check ([object]::ReferenceEquals($Arguments[0], $script:resolved)) 'addressed-object-not-code'
            Check ($script:calls.IndexOf('UseDate') -lt $script:calls.IndexOf('FindItem')) 'date-before-position'
            if ($script:mode -eq 'not-found') { return 0 }
            return 1
        }
        'CurrentItem' { return $script:current }
        'GetAttrib' {
            Check ([object]::ReferenceEquals($Object, $script:context)) 'attributes-from-positioned-context'
            Check ($script:calls.IndexOf('FindItem') -lt $script:calls.IndexOf('GetAttrib')) 'attributes-after-position'
            if ($Arguments[0] -eq 'Status') { return 'synthetic-original-asof-value' }
            if ($script:mode -eq 'relation-error') { throw 'PRIVATE_CANARY' }
            return $script:master
        }
        default { throw 'TEST-unexpected-member' }
    }
}
$attributes = @([pscustomobject] @{name='Status'; typeCode='S'; refTypeId='0'}, [pscustomobject] @{name='Master'; typeCode='U'; refTypeId='13'})
$metadata = [pscustomobject] @{id='12'; name='SyntheticCatalog'; attributes=$attributes}
$inventory = [pscustomobject] @{catalogs=@($metadata, [pscustomobject] @{id='13'; name='SyntheticMaster'})}
$dataset = [pscustomobject] @{type='SyntheticCatalog'; fields=@('Status','Master')}
# The parsed-body call operator creates a child scope. Fake methods and fixture
# requests must share the same explicit scope, independent of any parent token.
$script:token = 'private-synthetic-caf' + [char] 0x00e9
$request = [pscustomobject] @{schemaVersion='1.0'; operation='read_reference_by_native'; sourceAlias='synthetic77';
    type='SyntheticCatalog'; catalogMetadataId='12'; nativeReference=$script:token; asOf='2026-10-02'; attributeNames=@('Status','Master')}
Assert-Request $request
foreach ($badMode in @('nul-token','nonstring-token','bad-date','duplicate-field','caller-expression','cursor','unknown-key')) {
    $bad = $request.PSObject.Copy()
    switch ($badMode) {
        'nul-token' { $bad.nativeReference = 'a' + [char] 0 + 'b' }
        'nonstring-token' { $bad.nativeReference = 1 }
        'bad-date' { $bad.asOf = '2026-02-30' }
        'duplicate-field' { $bad.attributeNames = @('Status','Status') }
        'caller-expression' { $bad.attributeNames = @('Invoke()') }
        'cursor' { $bad | Add-Member -NotePropertyName cursor -NotePropertyValue '0' }
        'unknown-key' { $bad | Add-Member -NotePropertyName expression -NotePropertyValue 'Write()' }
    }
    $rejected = $false
    try { Assert-Request $bad } catch { $rejected = $true }
    Check $rejected ('input-rejected-' + $badMode)
}
foreach ($mode in @('success','foreign-kind','wrong-metadata','scalar-only','date-missing','date-failed','not-found',
    'wrong-current','current-nul','current-decomposed','unselected','context-unselected','relation-foreign','relation-error','unknown-attribute')) {
    $script:mode=$mode; $script:calls=New-Object System.Collections.ArrayList; $script:evalCount=0
    $script:resolved=[pscustomobject] @{token=$script:token; kind=$(if ($mode -eq 'foreign-kind') {'Foreign'} else {'SyntheticCatalog'}); selected=1}
    $currentToken = switch ($mode) {
        'wrong-current' {'other'}
        'current-nul' {$script:token.Insert(1,[string][char]0)}
        'current-decomposed' {$script:token.Normalize([Text.NormalizationForm]::FormD)}
        default {$script:token}
    }
    $script:current=[pscustomobject] @{token=$currentToken; kind='SyntheticCatalog'; selected=$(if ($mode -eq 'unselected') {0} else {1})}
    $script:context=[pscustomobject] @{selected=$(if ($mode -eq 'context-unselected') {0} else {1})}
    $script:master=[pscustomobject] @{token='private-synthetic-master'; kind=$(if ($mode -eq 'relation-foreign') {'Foreign'} else {'SyntheticMaster'}); selected=1}
    $request.catalogMetadataId=$(if ($mode -eq 'wrong-metadata') {'99'} else {'12'})
    $request.attributeNames=$(if ($mode -eq 'unknown-attribute') {@('Unknown')} else {@('Status','Master')})
    $caught=$false
    try {
        $result=Read-ReferenceByNative $null $request $dataset $metadata $inventory
        Check ($mode -in @('success','relation-foreign','relation-error')) ('unexpected-success-' + $mode)
        $expectedStatus=if ($mode -eq 'success') {'complete-unverified'} else {'partial-hold'}
        Check ($result.readStatus -eq $expectedStatus) ('read-status-' + $mode)
        Check ([string]::Equals($result.nativeReference,$script:token,[StringComparison]::Ordinal)) 'native-ordinal'
        Check ($result.attributes.Status -eq 'synthetic-original-asof-value') 'asof-scalar'
        Check ($result.stableIdentity -eq 'unverified' -and $result.snapshotStatus -eq 'none') 'no-promotion'
    } catch {
        if ($_.Exception.Message.StartsWith('TEST-')) { throw }
        $caught=$true
        $expectedCode = switch ($mode) {
            'foreign-kind' {'NATIVE_CATALOG_MISMATCH'}
            'wrong-metadata' {'NATIVE_CATALOG_MISMATCH'}
            'unknown-attribute' {'DATASET_DENIED'}
            { $_ -in @('date-missing','date-failed') } {'ASOF_CONTEXT_UNAVAILABLE'}
            'not-found' {'NATIVE_REFERENCE_NOT_FOUND'}
            default {'NATIVE_IDENTITY_ROUNDTRIP_FAILED'}
        }
        Check ([string]::Equals($_.Exception.Message,$expectedCode,[StringComparison]::Ordinal)) ('failure-code-' + $mode)
        Check ($mode -notin @('success','relation-foreign','relation-error')) ('unexpected-failure-' + $mode)
    }
    Check ($caught -eq ($mode -notin @('success','relation-foreign','relation-error'))) ('outcome-' + $mode)
    if ($mode -in @('wrong-metadata','unknown-attribute')) { Check ($script:evalCount -eq 0) 'reject-before-resolve' }
    if ($mode -eq 'scalar-only') { Check ($script:evalCount -eq 1 -and -not $script:calls.Contains('Create')) 'no-aggregate-on-scalar-failure' }
    if ($mode -in @('foreign-kind','date-missing','date-failed','not-found','wrong-current','current-nul','current-decomposed','unselected','context-unselected')) {
        Check (-not $script:calls.Contains('GetAttrib')) ('no-attributes-on-failure-' + $mode)
    }
}
$configuredNamespace=Get-NativeSourceNamespace ([pscustomobject] @{alias='synthetic77'; databasePath='C:\SyntheticSource'})
foreach ($scopeMode in @('observed-unbound','matched-request','foreign-namespace','invalid-namespace')) {
    $script:mode='success'; $script:calls=New-Object System.Collections.ArrayList; $script:evalCount=0
    $script:resolved=[pscustomobject] @{token=$script:token; kind='SyntheticCatalog'; selected=1}
    $script:current=[pscustomobject] @{token=$script:token; kind='SyntheticCatalog'; selected=1}
    $script:context=[pscustomobject] @{selected=1}
    $script:master=[pscustomobject] @{token='private-synthetic-master'; kind='SyntheticMaster'; selected=1}
    $scoped=$request.PSObject.Copy(); $scoped.catalogMetadataId='12'; $scoped.attributeNames=@('Status','Master')
    if ($scopeMode -ne 'observed-unbound') {
        $expected=switch ($scopeMode) {'foreign-namespace' {'sha256:'+('b'*64)}; 'invalid-namespace' {'invalid'}; default {$configuredNamespace}}
        $scoped | Add-Member -NotePropertyName sourceNamespace -NotePropertyValue $expected
    }
    $caught=$false
    try {
        Assert-Request $scoped
        $result=Read-ReferenceByNative $null $scoped $dataset $metadata $inventory $configuredNamespace
        Check ($scopeMode -in @('observed-unbound','matched-request')) 'unexpected-scoped-success'
        Check ($result.sourceNamespace -eq $configuredNamespace -and $result.sourceNamespaceProof.observed -eq $configuredNamespace) 'computed-producer-scope'
        Check ($result.sourceNamespaceProof.scheme -eq 'configured-alias-windows-path-sha256-v1' -and $result.sourceNamespaceProof.status -eq $scopeMode) 'producer-scope-marker'
        if ($scopeMode -eq 'observed-unbound') {Check ($null -eq $result.sourceNamespaceProof.expected) 'no-fabricated-expected-scope'}
        else {Check ($result.sourceNamespaceProof.expected -eq $configuredNamespace) 'exact-request-scope'}
    } catch {
        if ($_.Exception.Message.StartsWith('TEST-')) {throw}
        $caught=$true
        $expectedCode=if ($scopeMode -eq 'invalid-namespace') {'INVALID_REQUEST'} else {'NATIVE_SOURCE_MISMATCH'}
        Check ($_.Exception.Message -eq $expectedCode -and $script:evalCount -eq 0 -and -not $script:calls.Contains('Create')) 'scope-rejected-before-native'
    }
    Check ($caught -eq ($scopeMode -in @('foreign-namespace','invalid-namespace'))) 'scoped-outcome'
}
Write-Output 'PASS: actual addressed reader, fake context only; COM/source not executed'

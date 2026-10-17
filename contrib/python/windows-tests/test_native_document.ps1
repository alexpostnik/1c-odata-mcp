# Actual AST-extracted Document helpers with synthetic fake values; no COM/entry point.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$worker=Join-Path $PSScriptRoot '../tools/agent-bridge/windows/Invoke-77Bridge.ps1'
$t=$null; $e=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($worker,[ref]$t,[ref]$e)
if ($e.Count) { throw 'TEST-worker-parse' }
$names=@('Assert-NativeDocumentRequest','Assert-Request','Assert-Keys','Assert-RequiredKeys','Assert-AbsentKeys','Assert-Identifier','Assert-Alias','Get-OptionalProperty',
    'Acquire-DocumentIdentity','Read-DocumentByNative','Assert-NativeDocumentMetadata','Get-NativeDocumentHeader','Position-NativeDocument','New-NativeDocumentProof',
    'Select-NativeDocumentAttributes','Read-NativeDocumentAttributes','Convert-AttributeMap','Test-ReferenceAttribute',
    'Resolve-NativeProbeReference','ConvertTo-NativeProbeStringExpression','Get-NativeSourceNamespace')
foreach ($name in $names) {
    $defs=@($ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true))
    if ($defs.Count -ne 1) { throw 'TEST-helper-unique' }
    . ([scriptblock]::Create($defs[0].Extent.Text))
}
function Check($v,$label) { if (-not $v) { throw ('TEST-'+$label) } }
function Fail($Code,$Message) { $script:lastFailure=$Message; throw $Code }
function Set-NativeProbeStage($Stage) { $script:stage=$Stage }
function Get-DatasetMetadataAllFields($Dataset) { return $script:mode -ne 'table-disabled' }
function Select-MetadataAttributes($Attrs,$Fields) { return $Attrs }
function Get-77ObjectTypeName($Kind,$Type) { Check ($Kind -ceq 'document' -and $Type -ceq 'SyntheticHire') 'typed-create'; return $Type }
function Invoke-77CreateObject($App,$Type) { [void] $script:calls.Add('Create'); return $script:context }
function Get-NativeReferenceToken($Value) { return $Value.token }
function Get-Fixed77MethodValue($Value,$Name) { Check ($Name -eq 'Selected') 'selected-member'; return $Value.selected }
function Get-Optional77Boolean($Value,$Name) { Check ($Name -eq 'IsTransacted') 'posted-member'; return $false }
function Get-77PropertyAny($Object,$Name) {
    Check ([object]::ReferenceEquals($Object,$script:context)) 'header-positioned-context'
    if ($Name -eq 'LineNum') {
        if ($script:mode -eq 'line-num-unavailable') { throw 'synthetic-property-unavailable' }
        if ($script:mode -eq 'line-num-duplicate') { return 7 }
        return [int] ($script:line * 7)
    }
    if ($Name -eq 'DocDate') { return [DateTime] '2000-01-02' }
    if ($Name -eq 'DocNum') { if ($script:mode -eq 'changed-header' -and $script:positioned) {return 'changed'}; return 'synthetic-001' }
    throw 'TEST-unexpected-property'
}
function Get-DocumentLineNumber($Object,$Fallback) { return [int] $script:line }
function Add-UnsupportedAttribute($List,$Scope,$Attribute,$Reason) { [void] $List.Add(@{name=$Attribute.name;reason=$Reason}) }
function New-UnsupportedValue($Attribute,$Reason) { return @{valueKind='unsupported';serializationStatus=$Reason} }
function Convert-BridgeValue($Value,$Attribute,$Unsupported,$Scope) {
    if ($Value -is [string] -or $Value -is [ValueType]) { return $Value }
    if ($script:mode -eq 'enum-hold' -and $Attribute.name -eq 'Category') { return @{valueKind='unsupported';serializationStatus='enum-unverified'} }
    return [pscustomobject] @{valueKind='reference';nativeReference=$Value.token;selected=$true;empty=$false;refTypeId=$Attribute.refTypeId;nativeIdentityStatus='unverified'}
}
function Invoke-77MethodAny($Object,$Name,[object[]] $Arguments) {
    [void] $script:calls.Add($Name)
    switch ($Name) {
        'EvalExpr' {
            $script:evalCount++
            if ($script:evalCount % 2 -eq 1) {if ($script:mode -eq 'scalar-only') { return 'other' };return $script:token}
            return $script:resolved
        }
        'Kind' {return $Object.kind}
        'SelectDocuments' {Check ($Arguments[0].ToString('yyyy-MM-dd') -eq '2000-01-01' -and $Arguments[1].ToString('yyyy-MM-dd') -eq '2000-12-31') 'period-exclusive';return 1}
        'GetDocument' {if ($script:mode -eq 'seed-missing') {return 0};return 1}
        'CurrentDocument' {return $script:current}
        'FindDocument' {
            Check ([object]::ReferenceEquals($Arguments[0],$script:resolved)) 'find-by-value-only'
            if ($script:mode -in @('not-found','wrong-value-kind')) {return 0}
            $script:positioned=$true;return 1
        }
        'GetAttrib' {
            Check ([object]::ReferenceEquals($Object,$script:context) -and $script:positioned) 'attributes-after-addressed-position'
            switch ($Arguments[0]) {
                'Employee' {return $script:employee}
                'Category' {if ($script:mode -eq 'enum-hold') {return $script:employee}; return 1}
                'Hours' {return 8}
            }
            throw 'TEST-unexpected-attribute'
        }
        'LinesCnt' {return 2}
        'SelectLines' {$script:line=0;return 1}
        'GetLine' {$script:line++;if ($script:line -le 2) {return 1};return 0}
        default {throw ('TEST-unexpected-member-'+$Name)}
    }
}
$script:token='private-synthetic-document-caf'+[char]0x00e9
$namespace='sha256:'+('a'*64)
$n1=Get-NativeSourceNamespace ([pscustomobject]@{alias='synthetic77';databasePath='C:\SyntheticSource'})
$n2=Get-NativeSourceNamespace ([pscustomobject]@{alias='synthetic77';databasePath='C:\OtherSource'})
$n3=Get-NativeSourceNamespace ([pscustomobject]@{alias='other77';databasePath='C:\SyntheticSource'})
Check ($n1 -match '^sha256:[0-9a-f]{64}$' -and $n1 -ne $n2 -and $n1 -ne $n3) 'namespace-location-alias-binding'
Check ($n1 -eq (Get-NativeSourceNamespace ([pscustomobject]@{alias='synthetic77';databasePath='C:\SyntheticSource'}))) 'namespace-repeat'

$head=@([pscustomobject]@{name='Employee';typeCode='U';refTypeId='13'},[pscustomobject]@{name='Category';typeCode='N';refTypeId='0'})
$table=@([pscustomobject]@{name='Hours';typeCode='N';refTypeId='0'})
$meta=[pscustomobject]@{id='12';name='SyntheticHire';headAttributes=$head;tableAttributes=$table}
$dataset=[pscustomobject]@{type='SyntheticHire';fields=@('Employee','Category')}
$inventory=[pscustomobject]@{catalogs=@([pscustomobject]@{id='13';name='SyntheticEmployee'});documents=@($meta)}
$request=[pscustomobject]@{schemaVersion='1.0';sourceAlias='synthetic77';operation='read_document_by_native';metadataKind='document';documentType='SyntheticHire';documentMetadataId='12';
    nativeReference=$script:token;sourceNamespace=$namespace;attributeNames=@('Employee','Category');tableAttributeNames=@('Hours');lineCursor='0';limit=1}
$acquire=[pscustomobject]@{schemaVersion='1.0';sourceAlias='synthetic77';operation='acquire_document_identity';metadataKind='document';documentType='SyntheticHire';documentMetadataId='12';
    period=[pscustomobject]@{from='2000-01-01';toExclusive='2001-01-01'};documentCursor='0'}
Assert-Request $request
Assert-Request $acquire
foreach ($badMode in @('wrong-kind','cursor','date','expression','native-number','bad-namespace','duplicate-fields','unbounded-lines','bad-metadata')) {
    $bad=$request.PSObject.Copy()
    switch ($badMode) {
        'wrong-kind' {$bad.metadataKind='reference'}
        'cursor' {$bad | Add-Member -NotePropertyName documentCursor -NotePropertyValue '0'}
        'date' {$bad | Add-Member -NotePropertyName period -NotePropertyValue $acquire.period}
        'expression' {$bad | Add-Member -NotePropertyName expression -NotePropertyValue 'Write()'}
        'native-number' {$bad.nativeReference=1}
        'bad-namespace' {$bad.sourceNamespace='synthetic77'}
        'duplicate-fields' {$bad.attributeNames=@('Employee','Employee')}
        'unbounded-lines' {$bad.lineCursor='10001'}
        'bad-metadata' {$bad.documentMetadataId='0'}
    }
    $rejected=$false
    try {Assert-Request $bad} catch {$rejected=$true}
    Check $rejected ('invalid-request-'+$badMode)
}
function Reset([string] $Mode) {
    $script:mode=$Mode;$script:calls=New-Object System.Collections.ArrayList;$script:evalCount=0;$script:positioned=$false;$script:line=0
    $script:resolved=[pscustomobject]@{token=$script:token;selected=$(if ($Mode -eq 'resolved-unselected') {0} else {1});kind=$(if ($Mode -eq 'foreign-kind') {'Foreign'} else {'SyntheticHire'})}
    $script:current=[pscustomobject]@{token=$(if ($Mode -eq 'wrong-current') {'other'} else {$script:token});selected=$(if ($Mode -eq 'current-unselected') {0} else {1});kind='SyntheticHire'}
    $script:context=[pscustomobject]@{selected=$(if ($Mode -eq 'context-unselected') {0} else {1});kind='SyntheticHire'}
    $script:employee=[pscustomobject]@{token='private-synthetic-employee';selected=1;kind=$(if ($Mode -eq 'relation-foreign') {'Other'} else {'SyntheticEmployee'})}
}
foreach ($mode in @('success','foreign-kind','wrong-metadata','foreign-namespace','scalar-only','not-found','wrong-value-kind','wrong-current',
    'resolved-unselected','current-unselected','context-unselected','relation-foreign','enum-hold','metadata-enum-hold','line-num-unavailable','unknown-attribute','table-disabled')) {
    Reset $mode
    $meta.headAttributes[1].typeCode=$(if ($mode -eq 'metadata-enum-hold') {'E'} else {'N'})
    $request.documentMetadataId=$(if ($mode -eq 'wrong-metadata') {'99'} else {'12'})
    $request.sourceNamespace=$(if ($mode -eq 'foreign-namespace') {'sha256:'+('b'*64)} else {$namespace})
    $request.attributeNames=$(if ($mode -eq 'unknown-attribute') {@('Unknown')} else {@('Employee','Category')})
    $request.lineCursor='0'
    $caught=$false
    try {
        $result=Read-DocumentByNative $null $request $dataset $meta $inventory $namespace
        Check ($mode -in @('success','relation-foreign','enum-hold','metadata-enum-hold','line-num-unavailable')) ('unexpected-success-'+$mode)
        Check ([string]::Equals($result.nativeReference,$script:token,[StringComparison]::Ordinal)) 'exact-token'
        Check ($result.hasMore -and $result.nextLineCursor -eq '1' -and $result.lines.Count -eq 1) 'line-bound'
        $expected=if ($mode -in @('success','line-num-unavailable')) {'page-complete-unverified'} else {'partial-hold'}
        Check ($result.readStatus -eq $expected) ('read-status-'+$mode)
        if ($mode -eq 'success') {
            Check ($result.relations.Employee.status -eq 'native-typed-unverified' -and $result.relations.Employee.sourceNamespace -eq $namespace) 'typed-catalog-relation'
            Check ($result.lineNumberSemantics -eq 'ordinal-traversal' -and $result.lines[0].lineNumber -eq 1 -and $result.lines[0].sourceLineNumber -eq 7) 'stored-number-gap-not-ordinal'
            $request.lineCursor='1'
            $again=Read-DocumentByNative $null $request $dataset $meta $inventory $namespace
            Check ($again.readStatus -eq 'complete-unverified' -and -not $again.hasMore -and $null -eq $again.nextLineCursor) 'last-line-page'
            Check ($again.document.number -eq $result.document.number -and $again.nativeReference -eq $result.nativeReference) 'same-document-repeat'
        }
    } catch {
        if ($_.Exception.Message.StartsWith('TEST-')) {throw}
        $caught=$true
        $code=switch ($mode) {
            {$_ -in @('foreign-kind','wrong-metadata')} {'NATIVE_DOCUMENT_MISMATCH'}
            'foreign-namespace' {'NATIVE_SOURCE_MISMATCH'}
            {$_ -in @('not-found','wrong-value-kind','resolved-unselected')} {'NATIVE_DOCUMENT_NOT_FOUND'}
            {$_ -in @('unknown-attribute','table-disabled')} {'DATASET_DENIED'}
            default {'NATIVE_IDENTITY_ROUNDTRIP_FAILED'}
        }
        Check ($_.Exception.Message -eq $code) ('error-code-'+$mode+' actual='+$_.Exception.Message)
    }
    Check ($caught -eq ($mode -notin @('success','relation-foreign','enum-hold','metadata-enum-hold','line-num-unavailable'))) ('outcome-'+$mode)
    if ($mode -in @('wrong-metadata','foreign-namespace','unknown-attribute','table-disabled')) {Check ($script:evalCount -eq 0) 'reject-before-native-resolve'}
    if ($mode -notin @('success','relation-foreign','enum-hold','metadata-enum-hold','line-num-unavailable')) {Check (-not $script:calls.Contains('GetAttrib')) 'no-attributes-after-failed-proof'}
}
$meta.headAttributes[1].typeCode='N'
foreach ($mode in @('line-num-unavailable','line-num-duplicate')) {
    Reset $mode
    $request.documentMetadataId='12'; $request.sourceNamespace=$namespace
    $request.attributeNames=@('Employee','Category'); $request.lineCursor='0'; $request.limit=2
    $caught=$false
    try {
        $result=Read-DocumentByNative $null $request $dataset $meta $inventory $namespace
        Check ($mode -eq 'line-num-unavailable') 'duplicate-stored-identity-accepted'
        Check ($result.readStatus -eq 'complete-unverified' -and $result.lines.Count -eq 2) 'unavailable-stored-number-traversal-complete'
        Check ($result.lines[0].lineNumber -eq 1 -and $result.lines[1].lineNumber -eq 2) 'unavailable-number-ordinals'
        foreach ($line in $result.lines) { Check ($null -eq $line.sourceLineNumber -and $line.sourceLineNumberStatus -eq 'unavailable') 'no-fabricated-stored-number' }
    } catch {
        if ($_.Exception.Message.StartsWith('TEST-')) {throw}
        $caught=$true
        Check ($mode -eq 'line-num-duplicate' -and $_.Exception.Message -eq 'NATIVE_LINE_IDENTITY_INVALID') 'duplicate-stored-number-fixed-error'
    }
    Check ($caught -eq ($mode -eq 'line-num-duplicate')) 'stored-number-outcome'
}
foreach ($mode in @('success','seed-missing','changed-header','current-unselected')) {
    Reset $mode
    $caught=$false
    try {
        $result=Acquire-DocumentIdentity $null $acquire $dataset $meta $namespace
        Check ($mode -eq 'success') 'unexpected-acquisition-success'
        Check ($result.seed.number -eq $result.resolved.number -and $result.nativeReference -eq $script:token) 'seed-roundtrip-row-match'
        Check (-not $script:calls.Contains('GetAttrib') -and -not $script:calls.Contains('SelectLines')) 'acquisition-identity-only'
    } catch {
        if ($_.Exception.Message.StartsWith('TEST-')) {throw}
        $caught=$true
        $code=if ($mode -eq 'seed-missing') {'IDENTITY_SEED_NOT_FOUND'} else {'NATIVE_IDENTITY_ROUNDTRIP_FAILED'}
        Check ($_.Exception.Message -eq $code) ('acquisition-code-'+$mode)
    }
    Check ($caught -eq ($mode -ne 'success')) ('acquisition-outcome-'+$mode)
}
Write-Output 'PASS: typed Document acquisition/addressing, actual helpers with fake context; no COM/source calls'

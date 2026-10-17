# Offline tests of the actual worker functions. No COM or source connection.
$ErrorActionPreference = 'Stop'
$worker = Join-Path $PSScriptRoot '../tools/agent-bridge/windows/Invoke-77Bridge.ps1'
$parseErrors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($worker, [ref] $tokens, [ref] $parseErrors)
if ($parseErrors.Count -ne 0) { throw 'Worker parse failed' }
foreach ($name in @('ConvertTo-NativeProbeStringExpression','Resolve-NativeProbeReference')) {
    $definition = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true))
    if ($definition.Count -ne 1) { throw 'Worker function not unique' }
    . ([scriptblock]::Create($definition[0].Extent.Text))
}
function Assert-True($Value) { if (-not $Value) { throw 'Synthetic assertion failed' } }
function Fail([string] $Code, [string] $Message) { throw $Code }
function Set-NativeProbeStage([string] $Stage) { $script:stage = $Stage }

# Independent restricted grammar oracle. This does not execute 1C expressions.
function Decode-Literal([string] $Expression) {
    Assert-True ($Expression.StartsWith('(') -and $Expression.EndsWith(')'))
    $text = $Expression.Substring(1, $Expression.Length - 2)
    $result = New-Object System.Text.StringBuilder
    $offset = 0
    while ($offset -lt $text.Length) {
        $match = [regex]::Match($text.Substring($offset), '^(?:"([^"\x00-\x1f\x7f]*)"|Chr\(([0-9]+)\))')
        Assert-True $match.Success
        if ($match.Groups[2].Success) {
            $code = [int] $match.Groups[2].Value
            Assert-True ($code -eq 34 -or ($code -gt 0 -and $code -lt 32) -or $code -eq 127)
            [void] $result.Append([char] $code)
        } else { [void] $result.Append($match.Groups[1].Value) }
        $offset += $match.Length
        if ($offset -lt $text.Length) {
            Assert-True ($text[$offset] -eq '+')
            $offset++
            Assert-True ($offset -lt $text.Length)
        }
    }
    return $result.ToString()
}

# Keep this file ASCII: Windows PowerShell reads BOM-less source as legacy ANSI.
# Build the same Unicode fixture as .NET characters, independent of source codepage.
$unicodeFixture = -join [char[]] @(0x0421, 0x0438, 0x043d, 0x0442, 0x0435, 0x0442,
    0x0438, 0x043a, 0x0430, 0x002d, 0x00e9, 0x002d, 0x0032, 0x0035, 0x0035)
$fixtures = @('private-synthetic-A', '"', '""', '" ); Write(); //',
    'prefix;ExecuteBatch("private-canary");', $unicodeFixture, '  preserve spaces  ',
    "a`r`nb`tend", ('x' * 4096))
foreach ($token in $fixtures) {
    $encoded = ConvertTo-NativeProbeStringExpression $token
    Assert-True ([string]::Equals((Decode-Literal $encoded), $token, [StringComparison]::Ordinal))
}
for ($code = 1; $code -lt 32; $code++) {
    $token = 'x' + [char] $code + 'y'
    Assert-True ([string]::Equals((Decode-Literal (ConvertTo-NativeProbeStringExpression $token)), $token, [StringComparison]::Ordinal))
}
foreach ($token in @('', ' ', ('x' * 4097), ('x' + [char] 0), ('x' + [char] 0x85),
    ('x' + [char] 0x2028), ('x' + [char] 0x2029), ('x' + [char] 0xd800))) {
    $caught = $false
    try { [void] (ConvertTo-NativeProbeStringExpression $token) }
    catch { $caught = $true; Assert-True ($_.Exception.Message -eq 'NATIVE_IDENTITY_UNAVAILABLE') }
    Assert-True $caught
}

$script:expectedToken = 'private-synthetic-caf' + [char] 0xe9 + '-A";Write();//'
$script:reference = [pscustomobject] @{ syntheticReference = 'A' }
function Invoke-77MethodAny($Object, [string] $Name, [object[]] $Arguments) {
    [void] $script:calls.Add(@{ name = $Name; expression = $Arguments[0]; stage = $script:stage })
    Assert-True ($Name -ceq 'EvalExpr' -and $Arguments.Count -eq 1)
    $prefix = if ($script:calls.Count -eq 1) { 'ValueToStringInternal(ValueFromStringInternal(' } else { 'ValueFromStringInternal(' }
    $suffix = if ($script:calls.Count -eq 1) { '))' } else { ')' }
    $expr = [string] $Arguments[0]
    Assert-True ($expr.StartsWith($prefix) -and $expr.EndsWith($suffix))
    $literal = $expr.Substring($prefix.Length, $expr.Length - $prefix.Length - $suffix.Length)
    Assert-True ([string]::Equals((Decode-Literal $literal), $script:expectedToken, [StringComparison]::Ordinal))
    if ($script:mode -eq 'scalar-error' -and $script:calls.Count -eq 1) { throw 'PRIVATE_CANARY' }
    if ($script:mode -eq 'aggregate-error' -and $script:calls.Count -eq 2) { throw 'PRIVATE_CANARY' }
    if ($script:calls.Count -eq 1) { return $script:scalar }
    return $script:reference
}
foreach ($mode in @('success','foreign','unresolved','number','case-change','embedded-nul','decomposed','scalar-error','aggregate-error')) {
    $script:mode = $mode
    $script:calls = New-Object System.Collections.ArrayList
    $script:scalar = switch ($mode) {
        'foreign' { 'private-synthetic-B' }
        'unresolved' { $null }
        'number' { 1 }
        'case-change' { $script:expectedToken.ToUpperInvariant() }
        'embedded-nul' { $script:expectedToken.Insert(1, [string] [char] 0) }
        'decomposed' { $script:expectedToken.Normalize([Text.NormalizationForm]::FormD) }
        default { $script:expectedToken }
    }
    $caught = $false
    try {
        $result = Resolve-NativeProbeReference $null $script:expectedToken
        Assert-True ($mode -eq 'success' -and [object]::ReferenceEquals($result, $script:reference))
    } catch {
        $caught = $true
        Assert-True ($mode -ne 'success')
        if ($mode -notin @('scalar-error','aggregate-error')) {
            Assert-True ($_.Exception.Message -eq 'NATIVE_IDENTITY_ROUNDTRIP_FAILED')
        }
    }
    $expectedCount = if ($mode -in @('success','aggregate-error')) { 2 } else { 1 }
    Assert-True ($script:calls.Count -eq $expectedCount)
    Assert-True ($script:calls[0].stage -eq 'resolve-native-scalar')
    if ($expectedCount -eq 2) { Assert-True ($script:calls[1].stage -eq 'resolve-native-reference') }
    Assert-True ($caught -eq ($mode -ne 'success'))
}
Write-Output 'PASS: actual worker encoding and two-phase resolver, synthetic only; COM not executed'

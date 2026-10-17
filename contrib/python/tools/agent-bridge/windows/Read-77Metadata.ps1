#Requires -Version 5.1
<#
Read the 1C 7.7 Main MetaData Stream from 1Cv7.MD and return a small,
technical catalog for migration mapping. The script opens only the exact OLE
storage path Metadata/Main MetaData Stream and does not enumerate modules,
forms, database tables, or accounting rows.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string] $MetadataPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Add-Native77MetadataTypes {
    $existingType = 'OneC77MetadataRead.OleMetadataReader' -as [type]
    if ($null -ne $existingType) { return }

    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;

namespace OneC77MetadataRead {
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("0000000b-0000-0000-C000-000000000046")]
    interface IStorage {
        void CreateStream([MarshalAs(UnmanagedType.LPWStr)] string name, int mode, int reserved1, int reserved2, out IStream stream);
        void OpenStream([MarshalAs(UnmanagedType.LPWStr)] string name, IntPtr reserved1, int mode, int reserved2, out IStream stream);
        void CreateStorage([MarshalAs(UnmanagedType.LPWStr)] string name, int mode, int reserved1, int reserved2, out IStorage storage);
        void OpenStorage([MarshalAs(UnmanagedType.LPWStr)] string name, IStorage priority, int mode, IntPtr exclude, int reserved, out IStorage storage);
        void CopyTo(int ciidExclude, IntPtr rgiidExclude, IntPtr exclude, IStorage storage);
        void MoveElementTo([MarshalAs(UnmanagedType.LPWStr)] string name, IStorage storage, [MarshalAs(UnmanagedType.LPWStr)] string newName, int flags);
        void Commit(int flags);
        void Revert();
        void EnumElements(int reserved1, IntPtr reserved2, int reserved3, out IntPtr enumStatStg);
        void DestroyElement([MarshalAs(UnmanagedType.LPWStr)] string name);
        void RenameElement([MarshalAs(UnmanagedType.LPWStr)] string oldName, [MarshalAs(UnmanagedType.LPWStr)] string newName);
        void SetElementTimes([MarshalAs(UnmanagedType.LPWStr)] string name, IntPtr ctime, IntPtr atime, IntPtr mtime);
        void SetClass(ref Guid clsid);
        void SetStateBits(int stateBits, int mask);
        void Stat(out System.Runtime.InteropServices.ComTypes.STATSTG stat, int flags);
    }

    public static class OleMetadataReader {
        const int STGM_READ = 0x00000000;
        const int STGM_SHARE_DENY_WRITE = 0x00000020;
        const int STGM_SHARE_EXCLUSIVE = 0x00000010;
        const int STATFLAG_NONAME = 1;

        [DllImport("ole32.dll", CharSet = CharSet.Unicode)]
        static extern int StgOpenStorage(string name, IStorage priority, int mode, IntPtr exclude, int reserved, out IStorage storage);

        public static byte[] ReadMainMetadataStream(string path) {
            IStorage root = null;
            IStorage metadata = null;
            IStream stream = null;
            int hr = StgOpenStorage(path, null, STGM_READ | STGM_SHARE_DENY_WRITE, IntPtr.Zero, 0, out root);
            if (hr != 0) Marshal.ThrowExceptionForHR(hr);
            try {
                root.OpenStorage("Metadata", null, STGM_READ | STGM_SHARE_EXCLUSIVE, IntPtr.Zero, 0, out metadata);
                metadata.OpenStream("Main MetaData Stream", IntPtr.Zero, STGM_READ | STGM_SHARE_EXCLUSIVE, 0, out stream);
                System.Runtime.InteropServices.ComTypes.STATSTG stat;
                stream.Stat(out stat, STATFLAG_NONAME);
                if (stat.cbSize < 0 || stat.cbSize > 67108864) throw new InvalidDataException("Metadata stream size is outside the supported bounds.");
                byte[] result = new byte[(int)stat.cbSize];
                byte[] buffer = new byte[8192];
                IntPtr readPtr = Marshal.AllocCoTaskMem(4);
                try {
                    int offset = 0;
                    while (offset < result.Length) {
                        Marshal.WriteInt32(readPtr, 0);
                        int want = Math.Min(buffer.Length, result.Length - offset);
                        stream.Read(buffer, want, readPtr);
                        int got = Marshal.ReadInt32(readPtr);
                        if (got <= 0) break;
                        Buffer.BlockCopy(buffer, 0, result, offset, got);
                        offset += got;
                    }
                    if (offset != result.Length) Array.Resize(ref result, offset);
                    return result;
                }
                finally {
                    Marshal.FreeCoTaskMem(readPtr);
                }
            }
            finally {
                if (stream != null) Marshal.ReleaseComObject(stream);
                if (metadata != null) Marshal.ReleaseComObject(metadata);
                if (root != null) Marshal.ReleaseComObject(root);
            }
        }
    }

    public sealed class BracketNode {
        public readonly List<string> Values = new List<string>();
        public readonly List<BracketNode> Children = new List<BracketNode>();
        public string FirstValue { get { return Values.Count == 0 ? "" : Values[0]; } }
        public string ValueAt(int index) { return index >= 0 && index < Values.Count ? Values[index] : ""; }
        public BracketNode ChildByFirstValue(string value) {
            foreach (BracketNode child in Children) {
                if (child.Values.Count > 0 && child.Values[0] == value) return child;
            }
            return null;
        }
    }

    public sealed class BracketParser {
        readonly string text;
        int pos;
        BracketParser(string text) { this.text = text ?? ""; }
        public static BracketNode Parse(string text) {
            BracketParser parser = new BracketParser(text);
            parser.SkipWhite();
            if (parser.pos >= parser.text.Length || parser.text[parser.pos] != '{') throw new InvalidDataException("Metadata stream does not start with a bracket node.");
            BracketNode root = parser.ParseNode();
            parser.SkipWhite();
            if (parser.pos != parser.text.Length) throw new InvalidDataException("Unexpected trailing data after metadata root node.");
            return root;
        }
        BracketNode ParseNode() {
            if (text[pos] != '{') throw new InvalidDataException("Expected opening brace.");
            pos++;
            BracketNode node = new BracketNode();
            while (pos < text.Length) {
                SkipWhite();
                if (pos >= text.Length) break;
                char ch = text[pos];
                if (ch == '}') { pos++; return node; }
                if (ch == ',') { pos++; continue; }
                if (ch == '{') node.Children.Add(ParseNode());
                else if (ch == '"') node.Values.Add(ParseQuoted());
                else node.Values.Add(ParseUnquoted());
            }
            throw new InvalidDataException("Unclosed metadata bracket node.");
        }
        string ParseQuoted() {
            pos++;
            StringBuilder builder = new StringBuilder();
            while (pos < text.Length) {
                char ch = text[pos];
                if (ch == '"') {
                    if (pos + 1 < text.Length && text[pos + 1] == '"') {
                        builder.Append('"');
                        pos += 2;
                        continue;
                    }
                    pos++;
                    return builder.ToString();
                }
                builder.Append(ch);
                pos++;
            }
            throw new InvalidDataException("Unclosed quoted metadata value.");
        }
        string ParseUnquoted() {
            int start = pos;
            while (pos < text.Length) {
                char ch = text[pos];
                if (ch == ',' || ch == '{' || ch == '}' || ch == '\r' || ch == '\n') break;
                pos++;
            }
            return text.Substring(start, pos - start).Trim();
        }
        void SkipWhite() {
            while (pos < text.Length) {
                char ch = text[pos];
                if (ch != ' ' && ch != '\t' && ch != '\r' && ch != '\n') return;
                pos++;
            }
        }
    }
}
'@
}

function ConvertTo-MetadataText {
    param([byte[]] $Bytes)

    if ($null -eq $Bytes -or $Bytes.Length -eq 0) {
        throw 'Metadata stream is empty.'
    }

    $offset = 0
    $length = $Bytes.Length
    if ($Bytes[0] -eq 0x7B) {
        return [Text.Encoding]::GetEncoding(1251).GetString($Bytes)
    }

    $ffCount = 0
    while ($ffCount -lt $Bytes.Length -and $Bytes[$ffCount] -eq 0xFF) {
        $ffCount++
    }
    if ($ffCount -eq 0) {
        throw 'Metadata stream must start with "{" or a supported length header.'
    }

    $lengthBytes = $ffCount + 1
    $headerSize = $ffCount + $lengthBytes
    if ($headerSize -ge $Bytes.Length) {
        throw 'Metadata stream length header is incomplete.'
    }

    [Int64] $declaredLength = 0
    for ($i = 0; $i -lt $lengthBytes; $i++) {
        $declaredLength = $declaredLength -bor ([Int64] $Bytes[$ffCount + $i] -shl (8 * $i))
    }
    if ($declaredLength -le 0 -or $declaredLength -gt ($Bytes.Length - $headerSize)) {
        throw 'Metadata stream length header is invalid.'
    }

    $offset = $headerSize
    $length = [int] $declaredLength
    if ($Bytes[$offset] -ne 0x7B) {
        throw 'Metadata stream payload does not start with "{".'
    }
    return [Text.Encoding]::GetEncoding(1251).GetString($Bytes, $offset, $length)
}

function Assert-NumericId {
    param([AllowNull()][string] $Value, [string] $Label, [switch] $AllowEmpty)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        if ($AllowEmpty) { return '' }
        throw "$Label is required."
    }
    if ($Value -notmatch '^[0-9]{1,12}$') {
        throw "$Label must be numeric."
    }
    return $Value
}

function Assert-TechnicalName {
    param([AllowNull()][string] $Value, [string] $Label)

    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.Length -gt 128) {
        throw "$Label is missing or too long."
    }
    if ($Value -notmatch '^(?:[A-Za-z_]|[\u0400-\u04FF])(?:[A-Za-z0-9_]|[\u0400-\u04FF]){0,127}$') {
        throw "$Label contains unsupported characters."
    }
    return $Value
}

function ConvertTo-OptionalInt {
    param([AllowNull()][string] $Value, [string] $Label)

    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    if ($Value -notmatch '^[0-9]{1,7}$') { throw "$Label must be numeric." }
    return [int] $Value
}

function Assert-TypeCode {
    param([AllowNull()][string] $Value, [string] $Label)

    if ($Value -notmatch '^[A-Z]$') {
        throw "$Label must be a single uppercase ASCII letter."
    }
    return $Value
}

function ConvertTo-MetadataAttribute {
    param([object] $Node, [string] $Label)

    if ($Node.Values.Count -lt 7) {
        throw "$Label has an unfamiliar attribute shape."
    }
    $refTypeId = Assert-NumericId -Value $Node.ValueAt(7) -Label "$Label refTypeId" -AllowEmpty
    return [pscustomobject] [ordered] @{
        id = Assert-NumericId -Value $Node.ValueAt(0) -Label "$Label id"
        name = Assert-TechnicalName -Value $Node.ValueAt(1) -Label "$Label name"
        typeCode = Assert-TypeCode -Value $Node.ValueAt(4) -Label "$Label typeCode"
        length = ConvertTo-OptionalInt -Value $Node.ValueAt(5) -Label "$Label length"
        precision = ConvertTo-OptionalInt -Value $Node.ValueAt(6) -Label "$Label precision"
        refTypeId = $refTypeId
    }
}

function ConvertTo-AttributeList {
    param([AllowNull()][object] $Section, [string] $Label)

    $items = New-Object 'System.Collections.Generic.List[object]'
    if ($null -eq $Section) {
        return @()
    }
    foreach ($child in $Section.Children) {
        [void] $items.Add((ConvertTo-MetadataAttribute -Node $child -Label $Label))
    }
    return @($items.ToArray())
}

function Get-MetadataSectionsByFirstValue {
    param([object] $Root, [string[]] $SectionNames)

    $items = New-Object 'System.Collections.Generic.List[object]'
    foreach ($child in $Root.Children) {
        if ($SectionNames -contains $child.FirstValue) {
            [void] $items.Add($child)
        }
    }
    return @($items.ToArray())
}

function ConvertTo-ConstantList {
    param([object] $Root)

    $items = New-Object 'System.Collections.Generic.List[object]'
    $sections = @(Get-MetadataSectionsByFirstValue -Root $Root -SectionNames @('Consts','Const'))
    foreach ($section in $sections) {
        foreach ($child in $section.Children) {
            [void] $items.Add((ConvertTo-MetadataAttribute -Node $child -Label 'constant'))
        }
    }
    return @($items.ToArray())
}

function ConvertTo-CatalogList {
    param([AllowNull()][object] $Section)

    $items = New-Object 'System.Collections.Generic.List[object]'
    if ($null -eq $Section) { return @() }
    foreach ($child in $Section.Children) {
        if ($child.Values.Count -lt 3) {
            throw 'Catalog section has an unfamiliar object shape.'
        }
        $attributes = @(ConvertTo-AttributeList -Section ($child.ChildByFirstValue('Params')) -Label 'catalog attribute')
        [void] $items.Add([pscustomobject] [ordered] @{
            id = Assert-NumericId -Value $child.ValueAt(0) -Label 'catalog id'
            name = Assert-TechnicalName -Value $child.ValueAt(1) -Label 'catalog name'
            attributes = $attributes
        })
    }
    return @($items.ToArray())
}

function ConvertTo-DocumentList {
    param([AllowNull()][object] $Section)

    $items = New-Object 'System.Collections.Generic.List[object]'
    if ($null -eq $Section) { return @() }
    foreach ($child in $Section.Children) {
        if ($child.Values.Count -lt 3) {
            throw 'Document section has an unfamiliar object shape.'
        }
        $headAttributes = @(ConvertTo-AttributeList -Section ($child.ChildByFirstValue('Head Fields')) -Label 'document head attribute')
        $tableAttributes = @(ConvertTo-AttributeList -Section ($child.ChildByFirstValue('Table Fields')) -Label 'document table attribute')
        [void] $items.Add([pscustomobject] [ordered] @{
            id = Assert-NumericId -Value $child.ValueAt(0) -Label 'document id'
            name = Assert-TechnicalName -Value $child.ValueAt(1) -Label 'document name'
            headAttributes = $headAttributes
            tableAttributes = $tableAttributes
        })
    }
    return @($items.ToArray())
}

function Get-CatalogAttributeCount {
    param([object[]] $Catalogs)

    $count = 0
    foreach ($catalog in $Catalogs) { $count += @($catalog.attributes).Count }
    return $count
}

function Get-DocumentHeadAttributeCount {
    param([object[]] $Documents)

    $count = 0
    foreach ($document in $Documents) { $count += @($document.headAttributes).Count }
    return $count
}

function Get-DocumentTableAttributeCount {
    param([object[]] $Documents)

    $count = 0
    foreach ($document in $Documents) { $count += @($document.tableAttributes).Count }
    return $count
}

function Get-AttributeCount {
    param([object[]] $Catalogs, [object[]] $Documents)

    $count = 0
    foreach ($catalog in $Catalogs) { $count += @($catalog.attributes).Count }
    foreach ($document in $Documents) {
        $count += @($document.headAttributes).Count
        $count += @($document.tableAttributes).Count
    }
    return $count
}

function Get-KnownSectionCoverageMap {
    $map = [ordered] @{
        constants = @('Consts','Const')
        catalogs = @('SbCnts')
        documents = @('Documents')
        enumerations = @('Enums','Enum')
        registers = @('Registers')
        journals = @('Journals','JournalsDef','DocJournals','GenJrnlFldDef')
        documentNumerators = @('DocNumRef')
        chartsOfAccounts = @('Accounts','ChartsOfAccounts','BuhAccounts','PlanAccounts')
        reports = @('Reports')
        dataProcessors = @('DataProcessors','DataProcessings')
    }
    return $map
}

function Get-BoundedSectionChildCount {
    param([object] $Root, [string[]] $SectionNames)

    $limit = 100000
    $count = 0
    $sections = @(Get-MetadataSectionsByFirstValue -Root $Root -SectionNames $SectionNames)
    foreach ($section in $sections) {
        $count += $section.Children.Count
        if ($count -gt $limit) { return $limit }
    }
    return $count
}

function New-SectionIndicator {
    param([object] $Root, [string[]] $SectionNames)

    $limit = 100000
    $count = Get-BoundedSectionChildCount -Root $Root -SectionNames $SectionNames
    return [pscustomobject] [ordered] @{
        present = ($count -gt 0)
        boundedObjectCount = [int] $count
        countLimit = $limit
    }
}

function New-SectionCoverageIndicator {
    param([object] $Root, [string[]] $SectionNames)

    $limit = 100000
    $sections = @(Get-MetadataSectionsByFirstValue -Root $Root -SectionNames $SectionNames)
    $count = Get-BoundedSectionChildCount -Root $Root -SectionNames $SectionNames
    return [pscustomobject] [ordered] @{
        present = (@($sections).Count -gt 0)
        rootSectionCount = @($sections).Count
        boundedObjectCount = [int] $count
        countLimit = $limit
        sectionNames = @($SectionNames)
    }
}

function New-UnmappedSectionCoverageIndicator {
    param([object] $Root, [object] $CoverageMap)

    $limit = 100000
    $knownNames = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($sectionNames in $CoverageMap.Values) {
        foreach ($sectionName in $sectionNames) {
            [void] $knownNames.Add($sectionName)
        }
    }

    $rootSectionCount = 0
    $boundedObjectCount = 0
    foreach ($child in $Root.Children) {
        if ([string]::IsNullOrWhiteSpace($child.FirstValue)) { continue }
        if ($knownNames.Contains($child.FirstValue)) { continue }
        $rootSectionCount++
        $boundedObjectCount += $child.Children.Count
        if ($boundedObjectCount -gt $limit) {
            $boundedObjectCount = $limit
            break
        }
    }

    return [pscustomobject] [ordered] @{
        present = ($rootSectionCount -gt 0)
        rootSectionCount = [int] $rootSectionCount
        boundedObjectCount = [int] $boundedObjectCount
        countLimit = $limit
    }
}

function New-SectionCoverage {
    param([object] $Root)

    $map = Get-KnownSectionCoverageMap
    $coverage = [ordered] @{}
    foreach ($purpose in $map.Keys) {
        $coverage[$purpose] = New-SectionCoverageIndicator -Root $Root -SectionNames $map[$purpose]
    }
    $coverage['unmapped'] = New-UnmappedSectionCoverageIndicator -Root $Root -CoverageMap $map
    return [pscustomobject] $coverage
}

if (-not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
    throw 'Metadata file was not found.'
}

Add-Native77MetadataTypes
$resolvedPath = (Resolve-Path -LiteralPath $MetadataPath).Path
$bytes = [OneC77MetadataRead.OleMetadataReader]::ReadMainMetadataStream($resolvedPath)
$text = ConvertTo-MetadataText -Bytes $bytes
$root = [OneC77MetadataRead.BracketParser]::Parse($text)
$constants = @(ConvertTo-ConstantList -Root $root)
$catalogs = @(ConvertTo-CatalogList -Section ($root.ChildByFirstValue('SbCnts')))
$documents = @(ConvertTo-DocumentList -Section ($root.ChildByFirstValue('Documents')))
$sectionCoverage = New-SectionCoverage -Root $root
if (@($constants).Count -eq 0 -and @($catalogs).Count -eq 0 -and @($documents).Count -eq 0) {
    throw 'Metadata stream does not contain constant, catalog, or document sections.'
}

$catalogAttributeCount = Get-CatalogAttributeCount -Catalogs @($catalogs)
$documentHeadAttributeCount = Get-DocumentHeadAttributeCount -Documents @($documents)
$documentTableAttributeCount = Get-DocumentTableAttributeCount -Documents @($documents)
$legacyAttributeCount = Get-AttributeCount -Catalogs @($catalogs) -Documents @($documents)

[pscustomobject] [ordered] @{
    status = 'metadata_catalog'
    constants = $constants
    catalogs = $catalogs
    documents = $documents
    counts = [pscustomobject] [ordered] @{
        constants = @($constants).Count
        catalogs = @($catalogs).Count
        documents = @($documents).Count
        attributes = $legacyAttributeCount
        catalogAttributes = $catalogAttributeCount
        documentHeadAttributes = $documentHeadAttributeCount
        documentTableAttributes = $documentTableAttributeCount
        totalMetadataAttributes = (@($constants).Count + $legacyAttributeCount)
    }
    sectionCoverage = $sectionCoverage
    otherSections = [pscustomobject] [ordered] @{
        constants = New-SectionIndicator -Root $root -SectionNames @('Consts','Const')
        enumerations = New-SectionIndicator -Root $root -SectionNames @('Enums','Enum')
        registers = New-SectionIndicator -Root $root -SectionNames @('Registers')
        journals = New-SectionIndicator -Root $root -SectionNames @('Journals','JournalsDef','DocJournals','GenJrnlFldDef')
        documentNumerators = New-SectionIndicator -Root $root -SectionNames @('DocNumRef')
        chartsOfAccounts = New-SectionIndicator -Root $root -SectionNames @('Accounts','ChartsOfAccounts','BuhAccounts','PlanAccounts')
        reports = New-SectionIndicator -Root $root -SectionNames @('Reports')
        dataProcessors = New-SectionIndicator -Root $root -SectionNames @('DataProcessors','DataProcessings')
    }
}

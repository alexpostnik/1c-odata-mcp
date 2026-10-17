"""Static safety gates for the Windows-only 1C 7.7 adapter.

Live COM execution must be checked on an authorized Windows copy of the base.
"""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "tools" / "agent-bridge" / "windows"


class WindowsAdapterContractTests(unittest.TestCase):
    def test_example_is_placeholder_only_and_has_explicit_dataset_allowlist(self):
        config = json.loads((WINDOWS / "bridge.local.example.json").read_text("utf-8"))
        self.assertEqual(config["schemaVersion"], "1.0")
        self.assertEqual(config["progId"], "V77.Application")
        for source in config["sources"]:
            self.assertEqual(source["runMode"], "RMAccount")
            self.assertIn("REPLACE", source["databasePath"])
            self.assertIn("REPLACE", source["userName"])
            self.assertNotIn("password", json.dumps(source).lower())
            for dataset in source["datasets"]:
                self.assertIn(dataset["kind"], ("document", "reference"))
                self.assertIn("REPLACE", dataset["type"])
                self.assertIs(dataset["metadataAllFields"], False)
                self.assertIsInstance(dataset["fields"], list)

    def test_request_cannot_inject_arbitrary_1c_code_or_mutators(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("Assert-Keys $Request", source)
        self.assertIn("DATASET_DENIED", source)
        self.assertIn("SOURCE_DENIED", source)
        self.assertIn("metadata_inventory", source)
        self.assertIn("Read-77Metadata.ps1", source)
        self.assertIn("METADATA_READER_UNAVAILABLE", source)
        self.assertIn("METADATA_READ_FAILED", source)
        self.assertIn("COM_ACCESS_DENIED", source)
        self.assertIn("0x80070005", source)
        self.assertIn("FinalReleaseComObject", source)
        self.assertIn("New-UnsupportedValue", source)
        self.assertIn("Test-77ReferenceAttribute", source)
        self.assertIn("^[1-9][0-9]*$", source)
        self.assertNotIn("-not [string]::IsNullOrWhiteSpace([string] $Attribute.refTypeId)", source)
        self.assertIn("partial-unsupported-values", source)
        self.assertIn("partial-lines-required", source)
        self.assertIn("unsupported-constant-context", source)
        self.assertIn("provisional-constant-context", source)
        self.assertIn("read_document_lines_page", source)
        self.assertIn("read_document_lines_batch", source)
        self.assertIn("read_constants_page", source)
        self.assertIn("read_periodic_history_page", source)
        self.assertIn("document_presence", source)
        self.assertIn("Read-DocumentPresencePage", source)
        self.assertIn("Get-77GenericDocumentTypeName", source)
        self.assertIn("Invoke-77MethodAny $obj 'Kind'", source)
        self.assertIn("presence-page-unverified", source)
        self.assertIn("[int] $initialized -eq 0", source)
        self.assertIn("[int] $exists -eq 0", source)
        self.assertIn("Resolve-77InitializeMode $app ([string] $Source.runMode)", source)
        self.assertIn("Invoke-77AutomationMember", source)
        self.assertIn("[object].GetMethod('GetType').Invoke($App, [object[]] @())", source)
        self.assertNotIn("$App.GetType().InvokeMember", source)
        self.assertIn("Test-77MemberMissing", source)
        self.assertIn("\'Empty\'", source)
        self.assertIn("empty-reference-unverified", source)
        self.assertIn("'Empty'", source)
        self.assertIn("empty-reference-unverified", source)
        self.assertIn("if (-not (Test-77MemberMissing $_)) { throw }", source)
        self.assertIn("Invoke-77Initialize", source)
        self.assertIn("Invoke-77CreateObject", source)
        self.assertIn("Get-77ObjectTypeName", source)
        self.assertIn("'Initialize' $flags $arguments", source)
        self.assertIn("$arguments = [object[]] @($TypeName)", source)
        self.assertIn("'CreateObject' $flags $arguments", source)
        self.assertIn("return Invoke-77AutomationMember $App 'CreateObject' $flags $arguments", source)
        self.assertIn("RUN_MODE_UNAVAILABLE", source)
        self.assertNotIn("return 16504", source)
        self.assertNotIn("RMAccounting", source)
        for code in (
            "READ_CREATE_OBJECT_FAILED",
            "READ_CREATE_PERIODIC_FAILED",
            "READ_PERIODIC_USE_OBJECT_FAILED",
            "READ_PERIODIC_USE_OBJECT_UNSUPPORTED",
            "READ_PERIODIC_SELECT_FAILED",
            "READ_PERIODIC_GET_VALUE_FAILED",
            "READ_SELECT_ITEMS_FAILED",
            "READ_SELECT_DOCUMENTS_FAILED",
            "READ_GET_ITEM_FAILED",
            "READ_GET_DOCUMENT_FAILED",
            "READ_BASE_FIELD_FAILED",
            "READ_SELECT_LINES_FAILED",
            "READ_GET_LINE_FAILED",
        ):
            self.assertIn(code, source)
        self.assertIn("attribute-read-failed", source)
        self.assertIn("Invoke-ReadStage", source)
        self.assertIn("1C 7.7 read failed at a sanitized adapter stage", source)
        self.assertNotRegex(source, r"\.Initialize\s*\(")
        self.assertNotRegex(source, r"\.CreateObject\s*\(")
        self.assertNotRegex(source, r"EvalExpr\s*\(")
        self.assertNotRegex(source, r"ExecuteBatch\s*\(")
        self.assertNotRegex(source, r"\$obj\.(?:Write|Delete|Post|Unpost|Save|SetAttrib)\s*\(")
        self.assertNotRegex(source, r"Invoke-77MethodAny\s+\$periodic\s+'(?:Write|Delete|SetAttrib)'")
        self.assertNotRegex(source, r"Fail\s+\$Code\s+\$_\.Exception\.Message")
        self.assertIn("SecureStringToBSTR", source)
        self.assertIn("ZeroFreeBSTR", source)
        self.assertIn("Import-Clixml", source)
        self.assertRegex(source, re.escape("'NO_SPLASH_SHOW'"))

    def test_create_object_uses_localized_77_aggregate_type_prefixes(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("$typeName = Get-77ObjectTypeName $Kind ([string] $Dataset.type)", source)
        for codepoint in (0x0414, 0x043E, 0x043A, 0x0443, 0x043C, 0x0435, 0x043D, 0x0442):
            self.assertIn(f"[char] 0x{codepoint:04X}", source)
        for codepoint in (0x0421, 0x043F, 0x0440, 0x0430, 0x0432, 0x043E, 0x0447, 0x043D, 0x0438, 0x043A):
            self.assertIn(f"[char] 0x{codepoint:04X}", source)
        self.assertNotIn("'Document.' + [string] $Dataset.type", source)
        self.assertNotIn("'Reference.' + [string] $Dataset.type", source)

    def test_metadata_reader_contract_requires_explicit_all_fields(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("headerAttributes = $(if ($metadataAllFields) { 'metadata-driven-all' } else { 'configured-fields' })", source)
        self.assertIn("tableAttributes = $(if ($metadataAllFields) { 'metadata-driven-all' } else { 'not-selected' })", source)
        self.assertIn("metadataAllFields = $metadataAllFields", source)
        self.assertIn("Get-DatasetMetadataAllFields", source)
        self.assertIn("Select-MetadataAttributes", source)
        self.assertIn("METADATA_ALL_FIELDS_DISABLED", source)
        self.assertIn("lineTraversal = $(if ($metadataAllFields) { 'read_document_lines_page' } else { 'metadataAllFields-required' })", source)
        self.assertIn("supportedKinds = @('document','reference','operation','operationEntry')", source)
        self.assertIn("provisionalKinds = @('constant','periodic_history')", source)
        self.assertIn("readOperations = @('document_presence','probe_reference_identity','read_reference_by_native','acquire_document_identity','read_document_by_native','read_page','read_documents_page','read_document_lines_page','read_document_lines_batch','read_constants_page','read_periodic_history_page','read_operations_page','read_operation_entries_page')", source)
        self.assertIn("periodicHistory = 'read_periodic_history_page for one metadata-linked attribute'", source)
        self.assertIn("periodicDocumentSelection", source)
        self.assertIn("Find-MetadataDocument", source)
        self.assertIn("Find-MetadataCatalog", source)
        self.assertIn("Find-MetadataConstant", source)
        self.assertIn("Find-MetadataCatalogAttribute", source)
        self.assertIn("$headAttributes = @($MetadataObject.headAttributes)", source)
        self.assertIn("$tableAttributes = @($MetadataObject.tableAttributes)", source)
        self.assertIn("$headAttributes = @($MetadataObject.attributes)", source)
        self.assertIn("fields: []", (WINDOWS / "README.md").read_text("utf-8"))
        self.assertIn("Resolve-ConstantContext", source)
        self.assertIn("function Convert-ConstantValue", source)
        self.assertIn("Invoke-77MethodAny $Value 'GetValue'", source)
        self.assertIn("Convert-ConstantValue $raw $constant $unsupported", source)
        self.assertIn("constant-context-unavailable", source)
        self.assertIn("New-PeriodicMetadataLink", source)
        self.assertIn("Open-PeriodicSelection", source)
        self.assertIn("Invoke-77CreateObject $App 'Periodic'", source)
        self.assertIn("Invoke-77MethodAny $periodic 'UseObject'", source)
        self.assertIn("if ([int] $useObjectResult -eq 0)", source)
        self.assertIn("[void] (Invoke-ReadStage 'READ_PERIODIC_SELECT_FAILED'", source)
        self.assertNotIn("READ_PERIODIC_SELECT_UNSUPPORTED", source)
        self.assertIn("Invoke-77MethodAny $PeriodicObject 'GetValue'", source)
        self.assertIn("Get-77PropertyAny $PeriodicObject 'Value'", source)
        self.assertIn("Get-77PropertyAny $PeriodicObject 'DateVal'", source)
        self.assertIn("Test-ReferenceAttribute", source)
        self.assertIn("$refTypeId -cne '0'", source)
        self.assertIn("if ((Test-ReferenceAttribute $Attribute) -or [string] $Attribute.typeCode -ceq 'U')", source)
        self.assertIn("metadata-linked-complete-unverified", source)
        self.assertIn("unsupported-periodic-context", source)
        self.assertIn("periodicityMetadata", source)
        self.assertIn("Invoke-77MethodAny $DocumentObject 'LinesCnt'", source)
        self.assertIn("Invoke-77MethodAny $DocumentObject 'SelectLines'", source)
        self.assertIn("Invoke-77MethodAny $DocumentObject 'GetLine'", source)
        self.assertNotRegex(source, r"\$DocumentObject\.(?:LinesCnt|SelectLines|GetLine)\s*\(")
        self.assertNotRegex(source, r"\$PeriodicObject\.(?:GetValue|SelectValues|UseObject|Value|DateVal)\b")

    def test_document_line_batch_uses_one_document_traversal_and_sanitizes_errors(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("function Read-DocumentLinesBatch($App, $Request, $MetadataDocument, $Dataset)", source)
        batch_request = source.index("} elseif ($Request.operation -eq 'read_document_lines_batch') {")
        open_application = source.index("$app = Open-Application $local $source")
        self.assertLess(batch_request, open_application)
        self.assertIn("$Request.items.Count -gt 10", source)
        self.assertIn("$seenDocumentCursors.ContainsKey($documentCursorKey)", source)
        self.assertIn("$page = Read-DocumentLinesBatch $app $request $metadataObject $dataset", source)
        self.assertIn("Sort-Object { [long] $_.documentCursor }", source)
        self.assertIn("Read-DocumentLinesPage $App $itemRequest $MetadataDocument $Dataset $sharedObject $true", source)
        self.assertIn("$currentOffset -lt $targetOffset", source)
        self.assertIn("Invoke-77MethodAny $sharedObject 'SelectDocuments'", source)
        batch_start = source.index("function Read-DocumentLinesBatch")
        batch_end = source.index("function ", batch_start + 1)
        batch_function = source[batch_start:batch_end]
        self.assertEqual(batch_function.count("'SelectDocuments'"), 1)
        self.assertEqual(batch_function.count("'GetDocument'"), 1)
        self.assertEqual(batch_function.count("Read-DocumentLinesPage $App"), 1)
        self.assertLess(batch_function.index("$currentOffset -lt $targetOffset"),
                        batch_function.index("Read-DocumentLinesPage $App"))
        date_filter = batch_function.index("if ($docDate -lt $start -or $docDate -ge $end) { continue }")
        offset_advance = batch_function.index("$currentOffset++")
        self.assertLess(date_filter, offset_advance)
        self.assertLess(batch_function.index("$docDate -lt $start"), offset_advance)
        self.assertLess(batch_function.index("$docDate -ge $end"), offset_advance)
        self.assertNotIn("Read-DocumentLinesPage $App $itemRequest $MetadataDocument $Dataset`n", batch_function)
        self.assertIn("$adapterStopwatch = [Diagnostics.Stopwatch]::StartNew()", source)
        self.assertIn("adapterElapsedMs", source)
        self.assertIn("DOCUMENT_READ_FAILED", source)
        self.assertIn("LINE_READ_FAILED", source)
        self.assertIn("ITEM_READ_FAILED", source)
        self.assertIn("$Request.items | Sort-Object { [long] $_.documentCursor }", source)
        self.assertIn("$request.operation -in @('read_page','read_documents_page','read_document_lines_page','read_document_lines_batch')", source)
        self.assertIn("$adapterStopwatch = [Diagnostics.Stopwatch]::StartNew()", source)
        self.assertEqual(source.count("$adapterStopwatch.Stop()"), 3)
        self.assertIn("$page['adapterElapsedMs'] = [long] [Math]::Min(600000, $adapterStopwatch.ElapsedMilliseconds)", source)
        self.assertLess(source.index("$adapterStopwatch = [Diagnostics.Stopwatch]::StartNew()"),
                        source.index("$inventory = Get-MetadataInventory $source"))
        batch_start = source.index("function Read-DocumentLinesBatch")
        batch_end = source.index("function ", batch_start + 1)
        batch_function = source[batch_start:batch_end]
        self.assertNotIn("Exception.Message =", batch_function)
        self.assertNotRegex(batch_function, r"error\s*=\s*\$_\.Exception")
        self.assertLess(batch_function.index("    try {"), batch_function.index("[DateTime]::ParseExact"))
        self.assertLess(batch_function.index("    } finally {"),
                        batch_function.index("FinalReleaseComObject($sharedObject)"))
        dispatch = source.index("$metadataInventory = $null")
        batch_gate = source.index("} elseif ($request.operation -eq 'read_document_lines_batch') {", dispatch)
        dataset_gate = source.index("$dataset = Find-Dataset $source 'document'", batch_gate)
        metadata_gate = source.index("if (-not (Get-DatasetMetadataAllFields $dataset))", dataset_gate)
        open_application = source.index("$app = Open-Application $local $source", dispatch)
        self.assertLess(dataset_gate, metadata_gate)
        self.assertLess(metadata_gate, open_application)
        for bridge_file in ("Invoke-77InteractiveBrokerClient.ps1", "Start-77InteractiveBroker.ps1"):
            broker = (WINDOWS / bridge_file).read_text("utf-8")
            self.assertIn("read_document_lines_batch", broker)

    def test_operation_document_link_uses_native_document_property(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("function Get-77OperationDocument", source)
        self.assertIn("foreach ($name in @('Document', (Get-77RuName 'Document')))", source)
        self.assertIn("$document = Get-77OperationDocument $OperationObject", source)
        self.assertIn("Invoke-77MethodAny $document 'Kind'", source)
        self.assertNotIn("Get-77PropertyAny $OperationObject 'OperationDocument'", source)

    def test_create_object_failure_detail_is_sanitized_and_bounded(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        self.assertIn("$script:BridgeErrorDetail = Get-SanitizedExceptionDetail $_ $stage", source)
        self.assertIn("'READ_CREATE_OBJECT_FAILED') { 'create-object' } else { $Code }", source)
        self.assertIn("$response.error['detail'] = $script:BridgeErrorDetail", source)
        self.assertIn("Get-SanitizedExceptionDetail", source)
        self.assertIn("New-SanitizedExceptionNode", source)
        self.assertIn("exceptionType", source)
        self.assertIn("hresult", source)
        self.assertIn("innerExceptions", source)
        self.assertIn("$remaining = 3", source)
        helper_start = source.index("function Get-SanitizedExceptionDetail")
        helper_end = source.index("function Assert-Keys", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn(".InnerException", helper)
        self.assertNotIn("Exception.Message", helper)
        self.assertNotRegex(helper, r"\[['\"]message['\"]\]")

    def test_denied_read_dataset_is_rejected_before_com_launch(self):
        source = (WINDOWS / "Invoke-77Bridge.ps1").read_text("utf-8")
        metadata_branch = source.index("} elseif ($request.operation -eq 'metadata_inventory') {")
        read_gate = source.index(
            "if ($request.operation -eq 'read_page' -or $request.operation -eq 'read_documents_page') {",
            metadata_branch,
        )
        dataset_gate = source.index("$dataset = Find-Dataset $source $kind $datasetType", read_gate)
        open_application = source.index("$app = Open-Application $local $source", metadata_branch)
        health_response = source.index("operation = 'health'", open_application)
        read_page = source.index("$page = Read-Page $app $dataset $request $kind $metadataObject", open_application)
        self.assertLess(dataset_gate, open_application)
        self.assertLess(open_application, health_response)
        self.assertLess(open_application, read_page)







if __name__ == "__main__":
    unittest.main()

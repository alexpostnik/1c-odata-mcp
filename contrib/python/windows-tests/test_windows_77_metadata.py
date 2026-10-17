"""Contract and Windows execution tests for the 1C 7.7 metadata reader."""
from __future__ import annotations

import base64
import json
import platform
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tools" / "agent-bridge" / "windows" / "Read-77Metadata.ps1"
IS_WINDOWS = platform.system() == "Windows"


def headered(payload: bytes) -> bytes:
    return b"\xff" + len(payload).to_bytes(2, "little") + payload


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class MetadataReaderStaticContractTests(unittest.TestCase):
    def test_reads_only_the_main_metadata_stream(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('OpenStorage("Metadata"', text)
        self.assertIn('OpenStream("Main MetaData Stream"', text)
        self.assertIn("STGM_READ", text)
        self.assertIn("STGM_SHARE_DENY_WRITE", text)
        self.assertIn("67108864", text)
        self.assertNotIn("EnumElements(", text.replace("void EnumElements(", ""))

    def test_contract_is_allowlisted_and_path_free(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for key in (
            "status",
            "constants",
            "catalogs",
            "documents",
            "counts",
            "attributes",
            "headAttributes",
            "tableAttributes",
            "sectionCoverage",
            "unmapped",
        ):
            self.assertIn(key, text)
        for forbidden in (
            "Start-Process",
            "Invoke-Expression",
            "New-Object -ComObject",
            "onecv7",
            "onecv8",
            "SELECT",
            'OpenStream("Config',
            "MD Programm text",
            "Dialog Stream",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), text.lower())

    def test_no_generic_metadata_names_are_emitted(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("otherSections", text)
        self.assertNotIn("topSections", text)
        self.assertNotIn("knownObjects", text)
        self.assertNotIn("ConvertTo-GenericObjectList", text)
        self.assertNotIn("childSections", text)

    def test_script_body_is_ascii_for_windows_powershell_51_parsing(self) -> None:
        SCRIPT.read_text(encoding="ascii")


@unittest.skipUnless(IS_WINDOWS, "1Cv7.MD structured storage tests run only on Windows")
class Windows77MetadataReaderTests(unittest.TestCase):
    def require_powershell51(self) -> str:
        shell = shutil.which("powershell.exe")
        if not shell:
            self.skipTest("powershell.exe is not available")
        self.assertEqual(Path(shell).name.lower(), "powershell.exe")
        version = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(version.returncode, 0, msg=version.stdout + version.stderr)
        if not version.stdout.strip().startswith("5.1"):
            self.skipTest(f"Windows PowerShell 5.1 is required, got {version.stdout.strip()!r}")
        return shell

    def create_fixture(self, shell: str, root: Path, metadata_bytes: bytes, module_text: str = "") -> Path:
        writer = root / "write_fixture.ps1"
        writer.write_text(
            textwrap.dedent(
                r'''
                param(
                    [Parameter(Mandatory=$true)][string] $Path,
                    [Parameter(Mandatory=$true)][string] $MetadataBase64,
                    [string] $ModuleBase64 = ''
                )
                $ErrorActionPreference = 'Stop'
                Add-Type -TypeDefinition @'
                using System;
                using System.IO;
                using System.Runtime.InteropServices;
                using System.Runtime.InteropServices.ComTypes;

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

                public static class OleFixtureBuilder {
                    const int STGM_CREATE = 0x00001000;
                    const int STGM_READWRITE = 0x00000002;
                    const int STGM_SHARE_EXCLUSIVE = 0x00000010;
                    [DllImport("ole32.dll", CharSet = CharSet.Unicode)]
                    static extern int StgCreateDocfile(string name, int mode, int reserved, out IStorage storage);

                    public static void Write(string path, byte[] metadataBytes, byte[] moduleBytes) {
                        if (File.Exists(path)) File.Delete(path);
                        IStorage root = null, metadata = null, typedText = null, moduleStorage = null;
                        IStream stream = null;
                        int mode = STGM_CREATE | STGM_READWRITE | STGM_SHARE_EXCLUSIVE;
                        int hr = StgCreateDocfile(path, mode, 0, out root);
                        if (hr != 0) Marshal.ThrowExceptionForHR(hr);
                        try {
                            root.CreateStorage("Metadata", mode, 0, 0, out metadata);
                            metadata.CreateStream("Main MetaData Stream", mode, 0, 0, out stream);
                            WriteBytes(stream, metadataBytes);
                            Marshal.ReleaseComObject(stream);
                            stream = null;
                            metadata.Commit(0);

                            if (moduleBytes != null && moduleBytes.Length > 0) {
                                root.CreateStorage("TypedText", mode, 0, 0, out typedText);
                                typedText.CreateStorage("ModuleText_Number1", mode, 0, 0, out moduleStorage);
                                moduleStorage.CreateStream("MD Programm text", mode, 0, 0, out stream);
                                WriteBytes(stream, moduleBytes);
                                Marshal.ReleaseComObject(stream);
                                stream = null;
                                moduleStorage.Commit(0);
                                typedText.Commit(0);
                            }
                            root.Commit(0);
                        }
                        finally {
                            if (stream != null) Marshal.ReleaseComObject(stream);
                            if (moduleStorage != null) Marshal.ReleaseComObject(moduleStorage);
                            if (typedText != null) Marshal.ReleaseComObject(typedText);
                            if (metadata != null) Marshal.ReleaseComObject(metadata);
                            if (root != null) Marshal.ReleaseComObject(root);
                        }
                    }

                    static void WriteBytes(IStream stream, byte[] bytes) {
                        if (bytes == null) bytes = new byte[0];
                        stream.Write(bytes, bytes.Length, IntPtr.Zero);
                        stream.Commit(0);
                    }
                }
                '@
                $metadataBytes = [Convert]::FromBase64String($MetadataBase64)
                $moduleBytes = if ([string]::IsNullOrWhiteSpace($ModuleBase64)) { [byte[]] @() } else { [Convert]::FromBase64String($ModuleBase64) }
                [OleFixtureBuilder]::Write($Path, $metadataBytes, $moduleBytes)
                '''
            ),
            encoding="utf-8",
        )
        md_path = root / "1Cv7.MD"
        module_bytes = module_text.encode("cp1251") if module_text else b""
        result = subprocess.run(
            [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(writer),
                "-Path",
                str(md_path),
                "-MetadataBase64",
                base64.b64encode(metadata_bytes).decode("ascii"),
                "-ModuleBase64",
                base64.b64encode(module_bytes).decode("ascii"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        return md_path

    def run_reader(self, shell: str, md_path: Path) -> subprocess.CompletedProcess[str]:
        command = (
            "$ErrorActionPreference='Stop'; [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); "
            f"$result = & {ps_quote(str(SCRIPT))} -MetadataPath {ps_quote(str(md_path))}; "
            "$result | ConvertTo-Json -Depth 30 -Compress"
        )
        return subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            text=True,
            encoding="utf-8",
            capture_output=True,
            check=False,
        )

    def parse_ok(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        return json.loads(result.stdout)

    def assert_allowlisted_keys(self, value: object) -> None:
        allowed = {
            "status",
            "catalogs",
            "documents",
            "counts",
            "attributes",
            "headAttributes",
            "tableAttributes",
            "catalogAttributes",
            "documentHeadAttributes",
            "documentTableAttributes",
            "totalMetadataAttributes",
            "id",
            "name",
            "typeCode",
            "length",
            "precision",
            "refTypeId",
            "otherSections",
            "sectionCoverage",
            "constants",
            "catalogs",
            "documents",
            "enumerations",
            "registers",
            "journals",
            "documentNumerators",
            "chartsOfAccounts",
            "reports",
            "dataProcessors",
            "present",
            "rootSectionCount",
            "boundedObjectCount",
            "countLimit",
            "sectionNames",
            "unmapped",
        }
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertIn(key, allowed)
                self.assert_allowlisted_keys(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_allowlisted_keys(child)

    def sample_metadata(self) -> str:
        return (
            '{"MainDataContDef","8665","10011","7120",'
            '{"TaskItem",{"1","DoNotEmit","1.0"}},'
            '{"Consts",{"5","ОсновнаяВалюта","","","B","0","0","14"},{"6","ПрефиксНомеров","","","S","5","0"}},'
            '{"SbCnts",'
            '{"14","Валюты","ignored {brace}","ignored ""quote""","0","3","1","1","1","10",'
            '{"Params",{"17","Курс","","","N","10","4"},{"18","Владелец","","","B","0","0","538"}}},'
            '{"538","Контрагенты","","","0","3","1","1","1","10",'
            '{"Params",{"539","Банк_1","","","S","50","0"}}}},'
            '{"Documents",'
            '{"100","ПоступлениеТоваров","","","9","","","","7",'
            '{"Head Fields",{"101","Контрагент","","","B","0","0","538"},{"102","ДатаОплаты","","","D","0","0"}},'
            '{"Table Fields",{"103","Номенклатура","","","B","0","0","200"},{"104","Сумма","","","N","15","2"}}},'
            '{"110","БанкПлатеж","","","6","","","","7",'
            '{"Head Fields",{"111","РасчетныйСчет","","","S","30","0"}}}}}'
        )

    def test_reads_headered_cp1251_metadata_and_omits_non_allowlisted_streams(self) -> None:
        shell = self.require_powershell51()
        with tempfile.TemporaryDirectory(prefix="metadata77-") as temp:
            root = Path(temp)
            payload = headered(self.sample_metadata().encode("cp1251"))
            md_path = self.create_fixture(shell, root, payload, module_text="Процедура СекретныйМодуль() КонецПроцедуры")

            before = md_path.read_bytes()
            catalog = self.parse_ok(self.run_reader(shell, md_path))
            self.assertEqual(before, md_path.read_bytes())

            self.assertEqual(catalog["status"], "metadata_catalog")
            self.assertEqual(
                catalog["counts"],
                {
                    "constants": 2,
                    "catalogs": 2,
                    "documents": 2,
                    "attributes": 8,
                    "catalogAttributes": 3,
                    "documentHeadAttributes": 3,
                    "documentTableAttributes": 2,
                    "totalMetadataAttributes": 10,
                },
            )
            self.assertEqual(
                set(catalog["otherSections"]),
                {"constants", "enumerations", "registers", "journals", "documentNumerators", "chartsOfAccounts", "reports", "dataProcessors"},
            )
            for indicator in catalog["otherSections"].values():
                self.assertEqual(set(indicator), {"present", "boundedObjectCount", "countLimit"})
                self.assertLessEqual(indicator["boundedObjectCount"], indicator["countLimit"])
            self.assertEqual(
                set(catalog["sectionCoverage"]),
                {
                    "constants",
                    "catalogs",
                    "documents",
                    "enumerations",
                    "registers",
                    "journals",
                    "documentNumerators",
                    "chartsOfAccounts",
                    "reports",
                    "dataProcessors",
                    "unmapped",
                },
            )
            self.assertEqual(catalog["sectionCoverage"]["constants"]["rootSectionCount"], 1)
            self.assertEqual(catalog["sectionCoverage"]["catalogs"]["sectionNames"], ["SbCnts"])
            self.assertEqual(catalog["sectionCoverage"]["unmapped"]["rootSectionCount"], 1)
            self.assertEqual(catalog["constants"][0]["name"], "ОсновнаяВалюта")
            self.assertEqual(catalog["constants"][0]["refTypeId"], "14")
            self.assertEqual(catalog["catalogs"][0]["name"], "Валюты")
            self.assertEqual(catalog["catalogs"][0]["attributes"][0]["typeCode"], "N")
            self.assertEqual(catalog["catalogs"][0]["attributes"][1]["refTypeId"], "538")
            self.assertEqual(catalog["documents"][0]["headAttributes"][0]["name"], "Контрагент")
            self.assertEqual(catalog["documents"][0]["tableAttributes"][1]["precision"], 2)
            self.assertEqual(catalog["documents"][1]["tableAttributes"], [])
            self.assert_allowlisted_keys(catalog)

            serialized = json.dumps(catalog, ensure_ascii=False)
            self.assertNotIn("СекретныйМодуль", serialized)
            self.assertNotIn("ignored", serialized)
            self.assertNotIn("TaskItem", serialized)

    def test_rejects_malformed_metadata_without_partial_success(self) -> None:
        shell = self.require_powershell51()
        with tempfile.TemporaryDirectory(prefix="metadata77-bad-") as temp:
            root = Path(temp)
            md_path = self.create_fixture(shell, root, b'{"MainDataContDef",{"SbCnts",{"1","Broken"')

            result = self.run_reader(shell, md_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("metadata_catalog", result.stdout)

    def test_rejects_nontechnical_identifiers(self) -> None:
        shell = self.require_powershell51()
        with tempfile.TemporaryDirectory(prefix="metadata77-name-") as temp:
            root = Path(temp)
            text = '{"MainDataContDef",{"SbCnts",{"1","Bad Name","","","0",{"Params",{"2","Поле","","","S","1","0"}}}}}'
            md_path = self.create_fixture(shell, root, text.encode("cp1251"))

            result = self.run_reader(shell, md_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("metadata_catalog", result.stdout)


if __name__ == "__main__":
    unittest.main()

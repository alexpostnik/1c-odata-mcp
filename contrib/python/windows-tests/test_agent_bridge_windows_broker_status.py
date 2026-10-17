"""Static contract checks for the interactive broker heartbeat/status side channel."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "tools" / "agent-bridge" / "windows"


class WindowsBrokerStatusTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pwsh"), "portable PowerShell unavailable")
    def test_actual_ast_recovers_native_document_reads_without_authorizing_writes(self):
        result = subprocess.run(
            ["pwsh", "-NoProfile", "-File", str(ROOT / "windows-tests" / "test_native_document_broker_recovery.ps1")],
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS: actual broker AST", result.stdout)




    def test_broker_writes_sanitized_atomic_heartbeat_readable_by_bridge_account(self):
        server = (WINDOWS / "Start-77InteractiveBroker.ps1").read_text("utf-8")
        self.assertIn("kind = 'interactive-broker-status'", server)
        self.assertIn("Write-BrokerStatusAtomic", server)
        self.assertIn("Recover-StaleProcessingFiles", server)
        self.assertIn("Test-ReadOnlyBrokerOperation", server)
        self.assertIn("BROKER_RECOVERED_STALE_PROCESSING", server)
        self.assertIn("Recover-StaleProcessingFiles $local $processingDirectory $responseDirectory 0", server)
        self.assertIn("Remove-ExpiredFiles @($requestDirectory, $responseDirectory)", server)
        self.assertIn("Move-Item -LiteralPath $temp -Destination $Path -Force -ErrorAction Stop", server)
        self.assertNotIn("[System.IO.File]::Replace", server)
        self.assertNotIn("Set-StatusFileAcl $Path", server)
        self.assertIn("Stop-Broker 'BROKER_STATUS_WRITE_FAILED'", server)
        self.assertIn("Write-BrokerStatus $local -Force", server)
        self.assertIn("lastHeartbeatUtc = $stamp", server)
        self.assertIn("lastHeartbeatAgeSeconds", (WINDOWS / "Get-77InteractiveBrokerStatus.ps1").read_text("utf-8"))
        acl_match = re.search(r"function Set-StatusFileAcl\([\s\S]*?^}", server, re.MULTILINE)
        self.assertIsNotNone(acl_match)
        status_acl = acl_match.group(0)
        self.assertIn("ReadAndExecute", status_acl)
        self.assertNotIn("Delete", status_acl)
        self.assertNotIn("$env:COMPUTERNAME", server)
        self.assertNotIn("[System.Security.Principal.WindowsIdentity]::GetCurrent().Name", server)

    def test_status_reader_reports_only_sanitized_age_based_state(self):
        status_reader = (WINDOWS / "Get-77InteractiveBrokerStatus.ps1").read_text("utf-8")
        self.assertIn("operation = 'get_77_interactive_broker_status'", status_reader)
        self.assertIn("state = $State", status_reader)
        self.assertIn("'running'", status_reader)
        self.assertIn("'stale'", status_reader)
        self.assertIn("'missing'", status_reader)
        self.assertIn("'invalid'", status_reader)
        self.assertIn("lastHeartbeatAgeSeconds = $AgeSeconds", status_reader)
        self.assertIn("Status file must not be a reparse point.", status_reader)
        self.assertIn("BROKER_STATUS_FAILED", status_reader)
        self.assertNotIn("Write-Host", status_reader)
        self.assertNotIn("$env:COMPUTERNAME", status_reader)
        self.assertNotIn("WindowsIdentity", status_reader)
        self.assertNotIn("Get-Credential", status_reader)
        self.assertNotIn("ConvertTo-SecureString", status_reader)
        self.assertNotRegex(status_reader, r"\[Console\]::Out\.WriteLine\(\$.*Path")


if __name__ == "__main__":
    unittest.main()

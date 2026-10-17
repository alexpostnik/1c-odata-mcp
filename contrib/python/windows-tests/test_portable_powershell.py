"""Parse production PowerShell and run AST-extracted fake-context harnesses only."""
from pathlib import Path
import shutil
import os
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
PWSH=shutil.which('pwsh')
pytestmark=pytest.mark.skipif(PWSH is None,reason='portable PowerShell parser/fake-context runtime unavailable')


def test_production_and_synthetic_harnesses_parse_without_execution():
    paths=list((ROOT/'tools/agent-bridge/windows').glob('*.ps1'))+list((ROOT/'windows-tests').rglob('*.ps1'))
    for path in paths:
        code="$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:ONEC_TEST_PARSE_PATH,[ref]$tokens,[ref]$errors);if($errors.Count){$errors|%{[Console]::Error.WriteLine($_.Message)};exit 1}"
        result=subprocess.run([PWSH,'-NoLogo','-NoProfile','-Command',code],capture_output=True,text=True,timeout=20,env={**os.environ,'ONEC_TEST_PARSE_PATH':str(path)})
        assert result.returncode==0,result.stderr


@pytest.mark.parametrize('name',['test_native_addressed_reference.ps1','test_native_document.ps1','test_native_probe_expression.ps1'])
def test_actual_production_functions_in_fake_context(name):
    result=subprocess.run([PWSH,'-NoLogo','-NoProfile','-File',str(ROOT/'windows-tests'/name)],capture_output=True,text=True,timeout=40)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'PASS' in result.stdout

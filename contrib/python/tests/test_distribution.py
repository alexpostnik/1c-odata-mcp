"""A clean synthetic Git repository exercises export/admission without ancestry."""
from pathlib import Path
import hashlib
import importlib.util
import shutil
import subprocess
import sys
import pytest
from onec_bank_graph import bank_statement_rules as rules
from test_reader import make_archive

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classifier_requires_explicit_reviewed_policy():
    row = {'sourceId':'synthetic-fee','direction':'expense','amount':'12.50','purpose':'Комиссия банка','sub1':'Synthetic bank'}
    result = rules.classify_line(row)
    assert result['classification']['decisionRequired'] is True
    assert 'account' not in result['target']
    identity = result['classification']['ruleId']
    enabled = rules.classify_line(row, policy={identity:{'target':{'account':'synthetic-expense'},'review_ref':'synthetic-independent-review'}})
    assert enabled['classification']['decisionRequired'] is False
    assert enabled['target']['account'] == 'synthetic-expense'


def test_export_requires_explicit_archive_and_detects_tampering(tmp_path):
    package = load('synthetic_package',ROOT/'tools/bank-graph/package.py')
    launch = load('synthetic_launch',ROOT/'tools/bank-graph/launch.py')
    repository = tmp_path/'clean-repository'
    repository.mkdir()
    for relative in package.PATHS:
        original = ROOT/relative
        target = repository/'contrib/python'/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if original.is_dir():
            shutil.copytree(original,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        else:
            shutil.copyfile(original,target)
    export_script = repository/'contrib/python/tools/bank-graph/package.py'
    shutil.copyfile(ROOT/'tools/bank-graph/package.py', export_script)
    subprocess.run(['git','init','-q',str(repository)],check=True)
    subprocess.run(['git','-C',str(repository),'add','.'],check=True)
    subprocess.run(['git','-C',str(repository),'-c','user.name=Synthetic Fixture','-c','user.email=synthetic@example.invalid','commit','-qm','synthetic source fixture'],check=True)
    revision = subprocess.check_output(['git','-C',str(repository),'rev-parse','HEAD'],text=True).strip()
    archive = tmp_path/'synthetic-archive'
    archive.mkdir()
    make_archive(archive)
    destination = tmp_path/'release'
    receipt = package.export_package(repository,revision,destination,archive=archive)
    root,binding = launch.verify_package(destination/'bank-graph-binding.json',revision,receipt['binding_sha256'])
    assert root == destination
    assert binding['revision'] == revision
    assert 'archive/manifest.json' in binding['files']
    cli = subprocess.run([sys.executable, str(export_script), '--revision', revision, '--destination', str(tmp_path/'cli-release'), '--archive', str(archive)], capture_output=True, text=True)
    assert cli.returncode == 0, cli.stderr
    nested_receipt = package.export_package(repository/'contrib/python', revision, tmp_path/'subdir-release', archive=archive)
    assert nested_receipt['revision'] == revision
    (root/'onec_bank_graph/reader.py').write_text('# synthetic tamper\n')
    with pytest.raises(launch.PackageAdmissionError,match='digest mismatch'):
        launch.verify_package(destination/'bank-graph-binding.json',revision,receipt['binding_sha256'])

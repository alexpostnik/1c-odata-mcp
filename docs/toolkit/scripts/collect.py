#!/usr/bin/env python3
"""Build a local documentation corpus from article-only browser captures."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
import importlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

BUILDERS = {'developer': 'build_1c_docs', 'upgrade': 'build_upgrade1c', 'additional': 'build_1c_additions'}
MARKER = '.local-documentation-corpus'


def initialize(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text('Local corpus; rights and access belong to its user.\n')
    (root / '.gitignore').write_text('*\n')
    knowledge = root / 'knowledge'
    knowledge.mkdir(exist_ok=True)
    common = dict(schema_version=1, pair_revision=1, source_refs=[], sections=[], relations=[])
    records = [dict(record_id=f'onec-knowledge-{kind}', pair_revision=1, kind=kind,
                    status='declared', md_path=f'knowledge/{kind}.md', json_path=f'knowledge/{kind}.json')
               for kind in ('catalog', 'graph')]
    defaults = {'catalog': dict(common, record_id='onec-knowledge-catalog', records=records),
                'graph': dict(common, record_id='onec-knowledge-graph',
                              nodes=[dict(id='corpus:local', type='corpus', label='Local documentation')],
                              edges=[], hyperedges=[])}
    for kind, data in defaults.items():
        path = knowledge / f'{kind}.json'
        if not path.exists():
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')


@contextmanager
def staged_output(output: Path) -> Iterator[Path]:
    """Keep an accepted corpus unchanged on input/parse/publication errors."""
    output = output.absolute()
    if output.is_symlink() or any(parent.is_symlink() for parent in output.parents):
        raise ValueError('Output must not use symlinks')
    if output.exists() and not (output.is_dir() and (output / MARKER).is_file()):
        raise ValueError('Existing output is not a managed local documentation corpus')
    if output.exists() and any(path.is_symlink() for path in output.rglob('*')):
        raise ValueError('Corpus contains a symlink')
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name('.'+output.name+'.import-lock')
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError('Another import owns this output; inspect a stale lock before removing it') from None
    try:
        with tempfile.TemporaryDirectory(prefix='.'+output.name+'-stage-', dir=output.parent) as temporary:
            work = Path(temporary) / 'corpus'
            if output.exists():
                shutil.copytree(output, work)
            initialize(work)
            yield work
            backup = Path(temporary) / 'previous'
            if output.exists():
                output.rename(backup)
            try:
                work.rename(output)
            except BaseException:
                if backup.exists():
                    backup.rename(output)
                raise
    finally:
        lock.rmdir()


def collect(kind: str, raw: Path, output: Path, observed: str) -> dict:
    date.fromisoformat(observed)
    raw = raw.resolve(strict=True)
    if not raw.is_dir():
        raise ValueError('Capture input must be a directory')
    if output.absolute() == raw or raw.is_relative_to(output.absolute()):
        raise ValueError('Keep raw captures outside the generated corpus')
    builder = importlib.import_module(BUILDERS[kind])
    common = importlib.import_module('build_1c_docs')
    with staged_output(output) as stage:
        old_root, old_common = builder.ROOT, common.ROOT
        old_record = getattr(builder, 'RECORD_DIR', None)
        old_manifest = getattr(builder, 'MANIFEST_PATH', None)
        try:
            builder.ROOT = common.ROOT = stage
            if kind == 'upgrade':
                builder.RECORD_DIR = stage / 'knowledge/records/upgrade1c/accounting8'
                builder.MANIFEST_PATH = stage / 'sources/upgrade1c/manifest.json'
            result = builder.build(raw, observed)
        finally:
            builder.ROOT, common.ROOT = old_root, old_common
            if kind == 'upgrade':
                builder.RECORD_DIR, builder.MANIFEST_PATH = old_record, old_manifest
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=BUILDERS)
    parser.add_argument('raw_dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--observed', default=date.today().isoformat())
    args = parser.parse_args()
    print(json.dumps(collect(args.kind, args.raw_dir, args.output, args.observed), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

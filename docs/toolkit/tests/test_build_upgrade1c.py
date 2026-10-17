"""Regressions for the protected migration-guide importer."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("build_upgrade1c", ROOT / "scripts/build_upgrade1c.py")
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load migration-guide importer")
guide = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guide)


class UpgradeGuideImportTests(unittest.TestCase):
    def test_account_mapping_spans_survive_without_executable_html(self):
        parser = guide.common.ArticleParser()
        parser.feed('<html><body><table><tr><th colspan="2">Счет 7.7</th><th>Счет 8</th></tr>'
                    '<tr><td rowspan="2">01</td><td>Основные средства</td><td>01.01</td></tr>'
                    '<tr><td><script>steal()</script>Выбытие</td><td>01.09</td></tr></table></body></html>')
        table = next(node for node in guide.common.blocks(parser.root) if node.tag == "table")
        result = guide.safe_table_html(table, "https://its.1c.ru/db/upgrade1c/content/27/hdoc")
        self.assertIn('colspan="2"', result)
        self.assertIn('rowspan="2"', result)
        self.assertIn("Выбытие", result)
        self.assertNotIn("steal()", result)
        self.assertNotIn("<script", result)

    def test_table_links_only_emit_http_and_https(self):
        parser = guide.common.ArticleParser()
        parser.feed('<html><body><table><tr><td>'
                    '<a href="vbscript:msgbox(1)">Unsafe</a>'
                    '<a href="https://its.1c.ru/db/upgrade1c">Safe</a>'
                    '<a href="/db/upgrade1c/content/18/hdoc">Relative</a>'
                    '<img src="vbscript:msgbox(2)" alt="Unsafe image">'
                    '</td></tr></table></body></html>')
        table = next(node for node in guide.common.blocks(parser.root) if node.tag == "table")
        result = guide.safe_table_html(table, "https://its.1c.ru/db/upgrade1c/content/27/hdoc")
        self.assertNotIn("vbscript:", result)
        self.assertNotIn("<img", result)
        self.assertIn("Unsafe", result)
        self.assertIn('<a href="https://its.1c.ru/db/upgrade1c">Safe</a>', result)
        self.assertIn('<a href="https://its.1c.ru/db/upgrade1c/content/18/hdoc">Relative</a>', result)

    def test_tampered_capture_length_stops_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            links = "".join(f'<li><a href="/db/upgrade1c/content/{sid}/hdoc">Page {sid}</a></li>'
                            for sid in guide.PAGE_IDS)
            for sid in guide.ALL_IDS:
                title = "Guide" if sid == 17 else f"Page {sid}"
                body = f"<h1>{title}</h1>" + (f"<ul>{links}</ul>" if sid == 17 else "<p>Example source text.</p>")
                html = f"<html><body>{body}{'<p>More text.</p>' * 40}</body></html>"
                data = {"source_id": str(sid), "title": title,
                        "source_url": f"{guide.SOURCE_ROOT}/content/{sid}/hdoc",
                        "doc_html": html, "html_length": len(html) + int(sid == 23)}
                (root / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Captured length mismatch"):
                guide.load_raw(root)

    def test_late_source_error_keeps_previous_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_dir = root / "raw"
            raw_dir.mkdir()
            links = "".join(f'<li><a href="/db/upgrade1c/content/{sid}/hdoc">Page {sid}</a></li>'
                            for sid in guide.PAGE_IDS)
            for sid in guide.ALL_IDS:
                title = "Guide" if sid == 17 else f"Page {sid}"
                heading = "Wrong heading" if sid == 23 else title
                body = f"<h1>{heading}</h1>" + (f"<ul>{links}</ul>" if sid == 17 else "")
                html = f"<html><body>{body}{'<p>Example source text.</p>' * 40}</body></html>"
                data = {"source_id": str(sid), "title": title,
                        "source_url": f"{guide.SOURCE_ROOT}/content/{sid}/hdoc",
                        "doc_html": html, "html_length": len(html)}
                (raw_dir / f"{sid}.json").write_text(json.dumps(data), encoding="utf-8")

            record_dir = root / "knowledge/records/upgrade1c/accounting8"
            record_dir.mkdir(parents=True)
            manifest_path = root / "sources/upgrade1c/manifest.json"
            manifest_path.parent.mkdir(parents=True)
            outputs = {record_dir / "overview.md": b"previous guide\n",
                       record_dir / "overview.json": b'{"record_id":"onec-upgrade1c-guide"}\n',
                       manifest_path: b'{"pages":[]}\n',
                       root / "knowledge/catalog.json": b'{"records":[]}\n',
                       root / "knowledge/graph.json": b'{"nodes":[],"edges":[]}\n'}
            for path, content in outputs.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            with patch.object(guide, "ROOT", root), patch.object(guide, "RECORD_DIR", record_dir), \
                 patch.object(guide, "MANIFEST_PATH", manifest_path):
                with self.assertRaisesRegex(ValueError, "source heading differs from TOC"):
                    guide.build(raw_dir, "2026-09-21")
            self.assertEqual({path: path.read_bytes() for path in outputs}, outputs)
            self.assertEqual(sorted(path.name for path in record_dir.iterdir()), ["overview.json", "overview.md"])

    def test_publish_failure_restores_previous_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            stage = Path(temp) / "stage"
            old = {"knowledge/catalog.json": b"old catalog\n",
                   "knowledge/graph.json": b"old graph\n"}
            for rel, content in old.items():
                for base in (root, stage):
                    path = base / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content if base == root else b"new output\n")

            real_replace = os.replace
            failure_injected = False
            def fail_second_replace(source, target):
                nonlocal failure_injected
                if Path(target) == root / "knowledge/graph.json" and not failure_injected:
                    failure_injected = True
                    raise OSError("simulated replacement failure")
                return real_replace(source, target)

            with patch.object(guide, "ROOT", root), \
                 patch.object(guide, "RECORD_DIR", root / "knowledge/records/upgrade1c/accounting8"), \
                 patch.object(guide.os, "replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(OSError, "simulated replacement failure"):
                    guide.publish_staged(stage)
            self.assertTrue(failure_injected)
            self.assertEqual({rel: (root / rel).read_bytes() for rel in old}, old)


if __name__ == "__main__":
    unittest.main()

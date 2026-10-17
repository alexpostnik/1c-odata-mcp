#!/usr/bin/env python3
"""Build the 1C Accounting 8 migration guide from temporary article captures.

The input directory contains only HTML from the article frame in an authorized
browser. It is local, ignored by Git, and never contains browser credentials.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

import build_1c_docs as common


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = "https://its.1c.ru/db/upgrade1c"
ISSUE = "https://its.1c.ru/"
GUIDE_ID = "onec-upgrade1c-guide"
PAGE_IDS = list(range(18, 28))
ALL_IDS = [17, *PAGE_IDS]
RECORD_DIR = ROOT / "knowledge/records/upgrade1c/accounting8"
MANIFEST_PATH = ROOT / "sources/upgrade1c/manifest.json"
PAGE_RE = re.compile(r"^/db/upgrade1c/content/(\d+)/hdoc(?:/([^/?#]+))?$")
ANCHOR_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def page_record_id(source_id: int) -> str:
    return f"onec-upgrade1c-page-{source_id}"


def source_anchor(node: common.Node) -> str | None:
    for child in node.children:
        if isinstance(child, common.Node) and child.tag == "a":
            anchor = child.attrs.get("id") or child.attrs.get("name")
            if anchor and ANCHOR_RE.fullmatch(anchor):
                return anchor
    return common.heading_anchor(node)


def toc_links(root: common.Node, source_url: str) -> list[dict]:
    """Read the visible nested list, including ul siblings following li nodes."""
    result = []

    def first_link(node: common.Node) -> common.Node | None:
        if node.tag == "a" and node.attrs.get("href"):
            return node
        for child in node.children:
            if isinstance(child, common.Node):
                found = first_link(child)
                if found:
                    return found
        return None

    def walk_list(node: common.Node, depth: int) -> None:
        for child in node.children:
            if not isinstance(child, common.Node):
                continue
            if child.tag == "li":
                link = first_link(child)
                if link:
                    href = common.doc_link(link.attrs["href"], source_url)
                    match = PAGE_RE.fullmatch(urlsplit(href).path)
                    if not match:
                        raise ValueError(f"Unexpected guide TOC link: {href}")
                    result.append({"depth": depth, "title": common.norm(common.plain(link)),
                                   "source_url": href, "source_id": int(match.group(1)),
                                   "anchor": match.group(2)})
            elif child.tag == "ul":
                walk_list(child, depth + 1)

    outer = next((n for n in common.blocks(root) if n.tag == "ul"), None)
    if outer is None:
        raise ValueError("Guide TOC has no list")
    walk_list(outer, 0)
    return result


def load_raw(raw_dir: Path) -> tuple[dict[int, dict], list[dict]]:
    raw = {}
    for sid in ALL_IDS:
        path = raw_dir / f"{sid}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        url = f"{SOURCE_ROOT}/content/{sid}/hdoc"
        html = data.get("doc_html")
        if str(data.get("source_id")) != str(sid) or data.get("source_url") != url:
            raise ValueError(f"Wrong source identity: {path}")
        if not isinstance(html, str) or len(html) < 500 or "<body" not in html.lower() or "</html>" not in html.lower():
            raise ValueError(f"Incomplete article HTML: {path}")
        if len(html) != data.get("html_length"):
            raise ValueError(f"Captured length mismatch: {path}")
        if re.search(r"login\.1c\.ru|(?:authorization|set-cookie):|sessionid=|jsessionid=", html, re.I):
            raise ValueError(f"Potential account data in article: {path}")
        raw[sid] = data

    parser = common.ArticleParser(); parser.feed(raw[17]["doc_html"])
    toc = toc_links(parser.root, raw[17]["source_url"])
    top = [item for item in toc if item["depth"] == 0]
    if [item["source_id"] for item in top] != PAGE_IDS:
        raise ValueError("Guide TOC page count or order changed")
    for item in top:
        if item["title"] != common.norm(raw[item["source_id"]]["title"]):
            raise ValueError(f"TOC title mismatch for {item['source_id']}")
    return raw, toc


def article_sections(items: list[tuple[common.Node, str]]) -> tuple[list[dict], list[dict]]:
    sections = []
    hierarchy = []
    seen = Counter()
    parents: dict[int, str] = {}
    for index, (node, _) in enumerate(items):
        if node.tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            continue
        heading = common.norm(common.plain(node))
        if not heading:
            continue
        level = int(node.tag[1])
        fallback = common.slug(heading)
        seen[fallback] += 1
        anchor = source_anchor(node) or (fallback if seen[fallback] == 1 else f"{fallback}-{seen[fallback]}")
        excerpt = ""
        for following, _ in items[index + 1:]:
            if following.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            excerpt = common.norm(common.plain(following))
            if excerpt:
                break
        excerpt = excerpt[:240].rsplit(" ", 1)[0] if len(excerpt) > 240 else excerpt
        section_id = f"s{len(sections) + 1}"
        sections.append({"id": section_id, "heading": heading, "anchor": anchor,
                         "summary": excerpt or f"Раздел «{heading}» исходного руководства.",
                         "keywords": [heading]})
        parent = next((parents[n] for n in range(level - 1, 0, -1) if n in parents), None)
        hierarchy.append({"section_id": section_id, "level": level, "parent_section_id": parent})
        parents[level] = section_id
        for n in list(parents):
            if n > level:
                del parents[n]
    if not sections:
        raise ValueError("Source article has no heading")
    return sections, hierarchy


safe_table_html = common.safe_table_html


def render_article(items: list[tuple[common.Node, str]], title: str, source_url: str,
                   sections: list[dict], observed: str) -> str:
    anchors = iter(section["anchor"] for section in sections)
    first_anchor = next(anchors)
    lines = [f'<a id="{first_anchor}"></a>', f"# {title}", "",
             f"Источник: [{source_url}]({source_url}) · Получено {observed}.", ""]
    for node, markdown in items:
        if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            if node.tag == "h1":
                continue
            lines.extend([f'<a id="{next(anchors)}"></a>', markdown, ""])
        else:
            lines.extend([markdown, ""])
    return "\n".join(lines)


def article_links(root: common.Node, source_id: int, source_url: str) -> tuple[list[dict], Counter]:
    links = []
    counts = Counter()
    seen = set()

    def preceding_text(target: common.Node) -> str:
        parent = target.parent
        while parent and parent.tag not in {"p", "li", "root"}:
            parent = parent.parent
        if not parent:
            return ""
        pieces = []
        def visit(node: common.Node) -> bool:
            if node is target:
                return True
            for child in node.children:
                if child is target:
                    return True
                if isinstance(child, str):
                    pieces.append(child)
                elif visit(child):
                    return True
            return False
        visit(parent)
        return common.norm("".join(pieces))[-80:].lower()

    def visit(node: common.Node) -> None:
        if node.tag == "a" and "copy_source" not in node.attrs.get("class", "").split():
            href = common.doc_link(node.attrs.get("href", ""), source_url)
            if href:
                parsed = urlsplit(href)
                match = PAGE_RE.fullmatch(parsed.path)
                target_id = int(match.group(1)) if match else None
                if target_id == source_id or parsed.path == urlsplit(source_url).path:
                    kind = "same_page"
                elif target_id in PAGE_IDS:
                    kind = "internal"
                elif parsed.netloc == urlsplit(SOURCE_ROOT).netloc:
                    kind = "site_unmapped"
                else:
                    kind = "external"
                context = preceding_text(node)
                relation = "see_also" if re.search(r"(?:см\.|смотри|подробнее)\s*(?:здесь\s*)?$", context) else "links_to"
                key = (href, relation)
                if key not in seen:
                    seen.add(key)
                    entry = {"href": href, "label": common.norm(common.plain(node))[:200], "type": kind}
                    if kind == "internal":
                        entry |= {"relation": relation, "record_id": page_record_id(target_id)}
                    links.append(entry)
                    counts[kind] += 1
                    if kind == "internal" and relation == "see_also":
                        counts["see_also_internal"] += 1
        for child in node.children:
            if isinstance(child, common.Node):
                visit(child)
    visit(root)
    return links, counts


def write_record_staged(path: Path, data: dict, body: str) -> None:
    """Write a record under the staging root while retaining repository paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    json_rel = Path(data["md_path"]).with_suffix(".json").as_posix()
    markdown = (f"<!-- 1c-record record_id: {data['record_id']} "
                f"pair_revision: {data['pair_revision']} json_path: {json_rel} -->\n"
                + body.rstrip() + "\n")
    size = len(markdown.encode("utf-8"))
    if size > common.MAX_MD_BYTES:
        raise ValueError(f"{data['md_path']}: {size} bytes exceeds 256 KiB")
    path.write_text(markdown, encoding="utf-8")
    data["content_sha256"] = common.sha(markdown)
    common.write_json(path.with_suffix(".json"), data)


def write_indexes(catalog: dict, graph: dict, output_root: Path) -> None:
    common.write_json(output_root / "knowledge/catalog.json", catalog)
    common.write_json(output_root / "knowledge/graph.json", graph)
    cat = [f"<!-- 1c-record record_id: onec-knowledge-catalog pair_revision: {catalog['pair_revision']} json_path: knowledge/catalog.json -->",
           "# Каталог долговечных записей", "", "Машинный реестр: [`catalog.json`](catalog.json).", "",
           "## Текущие записи", "", "| ID | Вид | Статус | Markdown |", "|---|---|---|---|"]
    cat.extend(f"| `{entry['record_id']}` | {entry['kind']} | {entry['status']} | `{entry['md_path']}` |" for entry in catalog["records"])
    cat += ["", "## Правило обновления", "", "Каждая пара меняется вместе с каталогом и графом в одном PR."]
    (output_root / "knowledge/catalog.md").write_text("\n".join(cat) + "\n", encoding="utf-8")
    counts = Counter(edge["type"] for edge in graph["edges"])
    lines = [f"<!-- 1c-record record_id: onec-knowledge-graph pair_revision: {graph['pair_revision']} json_path: knowledge/graph.json -->",
             "# Типизированный граф знаний 1С", "", "Машинное представление: [`graph.json`](graph.json).", "",
             "## Роль", "", "Граф служит навигацией по зарегистрированным записям и не подтверждает соответствие конкретной среде 1С.", "",
             "## Связи", ""]
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(counts.items()))
    lines += ["", "## Hyperedges", "", "Многоместных связей в импортированном материале не выявлено; массив `hyperedges` пуст."]
    (output_root / "knowledge/graph.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_staged(stage_root: Path, records: list[dict], catalog: dict, graph: dict) -> None:
    """Reject an incomplete staged corpus before touching repository outputs."""
    record_paths = {Path(rec["md_path"]) for rec in records}
    expected = (record_paths | {path.with_suffix(".json") for path in record_paths} |
                {Path("sources/upgrade1c/manifest.json"), Path("knowledge/catalog.json"),
                 Path("knowledge/catalog.md"), Path("knowledge/graph.json"), Path("knowledge/graph.md")})
    actual = {path.relative_to(stage_root) for path in stage_root.rglob("*") if path.is_file()}
    if actual != expected:
        raise ValueError(f"Staged output mismatch: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")

    registered = {entry["record_id"] for entry in catalog["records"]}
    node_ids = {node["id"] for node in graph["nodes"]}
    for rec in records:
        md_path = stage_root / rec["md_path"]
        sidecar = json.loads(md_path.with_suffix(".json").read_text(encoding="utf-8"))
        if sidecar != rec or common.sha(md_path.read_text(encoding="utf-8")) != rec["content_sha256"]:
            raise ValueError(f"Staged record pair mismatch: {rec['record_id']}")
        if rec["record_id"] not in registered or "record:" + rec["record_id"] not in node_ids:
            raise ValueError(f"Staged record omitted from catalog or graph: {rec['record_id']}")
    if len(node_ids) != len(graph["nodes"]):
        raise ValueError("Duplicate staged graph node")
    if any(edge["from"] not in node_ids or edge["to"] not in node_ids for edge in graph["edges"]):
        raise ValueError("Staged graph has an unknown edge endpoint")
    manifest = json.loads((stage_root / "sources/upgrade1c/manifest.json").read_text(encoding="utf-8"))
    if len(manifest["pages"]) != len(ALL_IDS):
        raise ValueError("Staged manifest is incomplete")


def publish_staged(stage_root: Path) -> None:
    """Install a fully checked build, restoring previous files if replacement fails."""
    generated = {path.relative_to(stage_root) for path in stage_root.rglob("*") if path.is_file()}
    existing_records = {path.relative_to(ROOT) for path in RECORD_DIR.glob("*")
                        if path.is_file() and path.suffix in {".md", ".json"}}
    stale = existing_records - generated
    affected = generated | stale
    originals = {rel: (ROOT / rel).read_bytes() if (ROOT / rel).exists() else None for rel in affected}
    touched: list[Path] = []
    try:
        for rel in sorted(generated):
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage_root / rel, target)
            touched.append(rel)
        for rel in sorted(stale):
            (ROOT / rel).unlink()
            touched.append(rel)
    except OSError as exc:
        failed_restores = []
        for rel in reversed(touched):
            target = ROOT / rel
            prior = originals[rel]
            try:
                if prior is None:
                    target.unlink(missing_ok=True)
                else:
                    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as backup:
                        backup.write(prior)
                        backup_path = Path(backup.name)
                    try:
                        os.replace(backup_path, target)
                    finally:
                        backup_path.unlink(missing_ok=True)
            except OSError as restore_error:
                failed_restores.append(f"{rel}: {restore_error}")
        if failed_restores:
            raise RuntimeError("Could not restore generated outputs: " + "; ".join(failed_restores)) from exc
        raise


def build_staged(raw_dir: Path, observed: str, stage_root: Path) -> dict:
    raw, toc = load_raw(raw_dir)
    stage_record_dir = stage_root / RECORD_DIR.relative_to(ROOT)
    stage_record_dir.mkdir(parents=True, exist_ok=True)
    previous = {}
    for path in RECORD_DIR.glob("*.json"):
        item = json.loads(path.read_text(encoding="utf-8"))
        previous[item["record_id"]] = item
    try:
        old_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        old_manifest = {}
    hashes = {sid: common.sha(raw[sid]["doc_html"]) for sid in ALL_IDS}
    old_hashes = {int(p["source_id"]): p["source_html_sha256"] for p in old_manifest.get("pages", [])}
    changed = hashes != old_hashes

    records = []
    manifest_pages = []
    linked_edges = set()
    section_nodes = []
    section_edges = []
    overview_source = raw[17]["source_url"]
    for sid in PAGE_IDS:
        data = raw[sid]
        title = common.norm(data["title"])
        source_url = data["source_url"]
        parser = common.ArticleParser(); parser.feed(data["doc_html"])
        parsed_blocks = common.blocks(parser.root)
        heading = next((common.norm(common.plain(n)) for n in parsed_blocks if n.tag == "h1"), None)
        if heading != title:
            raise ValueError(f"Page {sid}: source heading differs from TOC: {heading!r} / {title!r}")
        omitted = common.unrepresented_text(parser.root)
        if omitted:
            raise ValueError(f"Page {sid}: unrepresented text {omitted[:3]}")
        rendered = [(n, safe_table_html(n, source_url) if n.tag == "table" else common.block_markdown(n, source_url))
                    for n in parsed_blocks]
        rendered = [(n, md) for n, md in rendered if md.strip()]
        sections, hierarchy = article_sections(rendered)
        links, counts = article_links(parser.root, sid, source_url)
        record_id = page_record_id(sid)
        record_relpath = (RECORD_DIR / f"{record_id}.md").relative_to(ROOT)
        record_path = stage_root / record_relpath
        rec = common.record_base(record_id, title, record_relpath.as_posix(), source_url, observed)
        rec["pair_revision"] = previous.get(record_id, {}).get("pair_revision", 1) + int(bool(previous.get(record_id)) and previous[record_id].get("provenance", {}).get("source_html_sha256") != hashes[sid])
        rec["summary"] = common.lead_summary(rendered, title)
        rec["keywords"] = list(dict.fromkeys(["1С", "Бухгалтерия 8", "переход 7.7 → 8", title]))
        rec["source_refs"] = [source_url, ISSUE]
        rec["sections"] = sections
        rec["section_hierarchy"] = hierarchy
        rec["relations"] = [{"type": "part_of", "target": GUIDE_ID}]
        targets = sorted({(link["record_id"], link["relation"]) for link in links if link.get("record_id")})
        for target, relation in targets:
            rec["relations"].append({"type": relation, "target": target})
            linked_edges.add((record_id, target, relation))
        rec["provenance"] = {"source_id": sid, "source_version": "not-stated-in-guide",
                             "source_html_sha256": hashes[sid], "observed_at": observed,
                             "capture": "authenticated-browser-article-iframe-clipboard-transfer",
                             "rights_basis": "user-managed-local-access"}
        rec["source_links"] = links
        body = render_article(rendered, title, source_url, sections, observed)
        write_record_staged(record_path, rec, body)
        records.append(rec)
        manifest_pages.append({"source_id": sid, "title": title, "source_url": source_url,
                               "source_html_sha256": hashes[sid], "html_characters": len(data["doc_html"]),
                               "record_ids": [record_id], "links_internal": counts["internal"],
                               "links_same_page": counts["same_page"],
                               "links_site_unmapped": counts["site_unmapped"],
                               "links_external": counts["external"],
                               "see_also_internal": counts["see_also_internal"],
                               "image_references": common.count_tag(parser.root, "img")})
        parent_for_level: dict[int, str] = {}
        for section, position in zip(sections, hierarchy):
            if position["level"] == 1:
                continue
            node_id = f"section:{record_id}:{section['anchor']}"
            section_nodes.append({"id": node_id, "type": "section", "status": "declared", "label": section["heading"]})
            parent_id = parent_for_level.get(position["level"] - 1, "record:" + record_id)
            section_edges.append({"id": f"edge:upgrade1c:section:{record_id}:{section['anchor']}",
                                  "type": "has_section" if parent_id.startswith("record:") else "has_subsection",
                                  "status": "declared", "from": parent_id, "to": node_id,
                                  "meaning": "Иерархия заголовков в исходном руководстве."})
            parent_for_level[position["level"]] = node_id
            for level in list(parent_for_level):
                if level > position["level"]:
                    del parent_for_level[level]

    guide_relpath = (RECORD_DIR / "overview.md").relative_to(ROOT)
    guide_path = stage_root / guide_relpath
    guide = common.record_base(GUIDE_ID, common.norm(raw[17]["title"]), guide_relpath.as_posix(), overview_source, observed)
    guide["pair_revision"] = previous.get(GUIDE_ID, {}).get("pair_revision", 1) + int(bool(previous.get(GUIDE_ID)) and changed)
    guide["kind"] = "source_manifest"
    guide["summary"] = "Полное оглавление десяти материалов руководства по переходу с 1С:Бухгалтерии 7.7 и 1С:УСН 7.7 на 1С:Бухгалтерию 8."
    guide["keywords"] = ["1С", "Бухгалтерия 8", "переход 7.7 → 8", "руководство по переходу"]
    guide["source_refs"] = [overview_source, ISSUE]
    guide["sections"] = [{"id": "scope", "heading": "Объём", "anchor": "объём", "summary": "Состав и статус курса.", "keywords": ["объём"]},
                         {"id": "pages", "heading": "Страницы", "anchor": "страницы", "summary": "Материалы в порядке исходного оглавления.", "keywords": ["страницы"]},
                         {"id": "limits", "heading": "Ограничения", "anchor": "ограничения", "summary": "Неизвестная редакция и зависимость изображений от сайта 1С.", "keywords": ["ограничения"]}]
    guide["relations"] = [{"type": "has_page", "target": page_record_id(sid)} for sid in PAGE_IDS]
    guide["provenance"] = {"source_id": 17, "source_version": "not-stated-in-guide",
                           "source_html_sha256": hashes[17], "observed_at": observed,
                           "rights_basis": "user-managed-local-access", "issue": ISSUE}
    guide["toc"] = [{"depth": item["depth"], "title": item["title"], "source_url": item["source_url"],
                     "record_id": page_record_id(item["source_id"]), "anchor": item["anchor"]} for item in toc]
    lines = ["# Руководство по переходу на 1С:Бухгалтерию 8", "", "## Объём", "",
             "Введение, девять следующих материалов и их вложенные разделы. Статус содержания — `declared`: текст источника не подтверждает состояние рабочей информационной базы.", "",
             f"[Исходное оглавление]({overview_source}) · Получено {observed}.", "", "## Страницы", ""]
    for item in toc:
        target = f"{page_record_id(item['source_id'])}.md"
        if item["anchor"]:
            target += f"#{item['anchor']}"
        lines.append("  " * item["depth"] + f"- [{item['title']}]({target})")
    lines += ["", "## Ограничения", "",
              "Редакция и дата публикации самого руководства не указаны в видимом материале. Иллюстрации остаются ссылками на защищённый источник; соответствие текущей облачной среде 1С не проверялось."]
    write_record_staged(guide_path, guide, "\n".join(lines))
    records.insert(0, guide)
    manifest_pages.insert(0, {"source_id": 17, "title": guide["title"], "source_url": overview_source,
                              "source_html_sha256": hashes[17], "html_characters": len(raw[17]["doc_html"]),
                              "record_ids": [GUIDE_ID], "links_internal": len(PAGE_IDS),
                              "links_same_page": len(toc) - len(PAGE_IDS), "links_site_unmapped": 0,
                              "links_external": 0, "see_also_internal": 0, "image_references": 0})
    manifest = {"schema_version": 1, "corpus": "1c-upgrade-accounting8-guide",
                "source_root": SOURCE_ROOT, "observed_at": observed,
                "rights_basis": "user-managed-local-access", "issue": ISSUE,
                "capture_method": "authenticated-browser-article-iframe-clipboard-transfer",
                "source_page_count": len(ALL_IDS), "material_page_count": len(PAGE_IDS),
                "pages": manifest_pages,
                "link_classification": {"links_internal": "Mapped to another imported course page.",
                                        "links_same_page": "Anchor or link to the same source article.",
                                        "links_site_unmapped": "Same-site link beyond the selected course or without reliable mapping.",
                                        "links_external": "Link to a different host or non-HTTP destination.",
                                        "see_also_internal": "Mapped course link with explicit see-also context."},
                "notes": ["HTML was copied from the document frame, without the portal shell or account data.",
                          "The guide does not state a reliable publication version; current runtime applicability is unknown.",
                          "Image binaries are not included; Markdown points to source URLs where present."]}
    common.write_json(stage_root / MANIFEST_PATH.relative_to(ROOT), manifest)

    catalog_path = ROOT / "knowledge/catalog.json"
    graph_path = ROOT / "knowledge/graph.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    catalog["pair_revision"] += int(changed)
    catalog["updated_at"] = observed
    catalog["summary"] = f"Полный реестр пар Markdown и JSON, включая {len(records)} записей руководства по переходу на 1С:Бухгалтерию 8."
    catalog["source_refs"] = list(dict.fromkeys([*catalog["source_refs"], ISSUE]))
    catalog["records"] = [r for r in catalog["records"] if not r["record_id"].startswith("onec-upgrade1c-")]
    catalog["records"].extend({key: r[key] for key in ("record_id", "pair_revision", "kind", "status", "md_path")} |
                              {"json_path": r["md_path"].removesuffix(".md") + ".json"} for r in records)
    for entry in catalog["records"]:
        if entry["record_id"] == "onec-knowledge-catalog":
            entry["pair_revision"] = catalog["pair_revision"]
        if entry["record_id"] == "onec-knowledge-graph":
            entry["pair_revision"] += int(changed)
    graph["pair_revision"] += int(changed)
    graph["updated_at"] = observed
    graph["summary"] = f"Типизированная карта репозитория и {len(records)} записей руководства по переходу на 1С:Бухгалтерию 8."
    graph["source_refs"] = list(dict.fromkeys([*graph["source_refs"], ISSUE]))
    graph["nodes"] = [n for n in graph["nodes"] if not n["id"].startswith(("record:onec-upgrade1c-", "section:onec-upgrade1c-"))]
    graph["edges"] = [e for e in graph["edges"] if not (e["from"].startswith(("record:onec-upgrade1c-", "section:onec-upgrade1c-")) or
                                                       e["to"].startswith(("record:onec-upgrade1c-", "section:onec-upgrade1c-")))]
    old_edges = len(graph["edges"])
    for rec in records:
        graph["nodes"].append({"id": "record:" + rec["record_id"], "type": "record", "status": rec["status"], "label": rec["title"]})
        graph["edges"].append({"id": "edge:repo-contains-" + rec["record_id"], "type": "contains_record", "status": "verified",
                               "from": "corpus:local", "to": "record:" + rec["record_id"],
                               "meaning": "Пара Markdown и JSON зарегистрирована в каталоге репозитория."})
        for relation in rec["relations"]:
            if relation["type"] in {"has_page", "part_of"}:
                graph["edges"].append({"id": f"edge:upgrade1c:{rec['record_id']}:{relation['type']}:{relation['target']}",
                                       "type": relation["type"], "status": "declared",
                                       "from": "record:" + rec["record_id"], "to": "record:" + relation["target"],
                                       "meaning": "Иерархия опубликованного оглавления."})
    graph["nodes"].extend(section_nodes)
    graph["edges"].extend(section_edges)
    for source, target, relation in sorted(linked_edges):
        graph["edges"].append({"id": f"edge:upgrade1c:{source}:{relation}:{target}", "type": relation,
                               "status": "declared", "from": "record:" + source, "to": "record:" + target,
                               "meaning": "Исходная страница ссылается на другой материал курса."})
    write_indexes(catalog, graph, stage_root)
    validate_staged(stage_root, records, catalog, graph)
    return {"source_pages": len(ALL_IDS), "material_pages": len(PAGE_IDS), "records": len(records),
            "nodes": len(graph["nodes"]), "edges": len(graph["edges"]),
            "imported_edges": dict(Counter(e["type"] for e in graph["edges"][old_edges:])),
            "images": sum(p["image_references"] for p in manifest_pages),
            "html_characters": sum(p["html_characters"] for p in manifest_pages)}


def build(raw_dir: Path, observed: str) -> dict:
    stage_parent = ROOT / ".import_raw"
    stage_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="upgrade1c-stage-", dir=stage_parent) as temp:
        stage_root = Path(temp)
        result = build_staged(raw_dir, observed, stage_root)
        publish_staged(stage_root)
        return result



if __name__ == "__main__":
    raise SystemExit("Use collect.py with an explicit --output directory")

#!/usr/bin/env python3
"""Build a reviewable 1C documentation corpus from locally captured article HTML.

Input is a private, temporary directory of {source_id,title,source_url,doc_html}
JSON files. Only the article iframe HTML is accepted; browser profile data, cookies,
and the portal shell are never inputs. The input directory is not committed.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from html import escape as html_escape
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = [51, *range(52, 81), *range(138, 148), 91, 102]
MAX_MD_BYTES = 256 * 1024
TARGET_MD_BYTES = 225 * 1024
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCKS = {"p", "pre", "table", "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP = {"script", "style", "noscript", "iframe", "form"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent


class ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.in_body = False
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.in_body = True
            return
        if not self.in_body:
            return
        if tag in SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "p" and self.stack[-1].tag == "p":
            self.stack.pop()
        parent = self.stack[-1]
        node = Node(tag, {k: v or "" for k, v in attrs}, parent)
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag == "body":
            self.in_body = False
            return
        if not self.in_body:
            return
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        if self.in_body and not self.skip:
            self.stack[-1].children.append(data)


def plain(node: Node | str) -> str:
    if isinstance(node, str):
        return node
    return "".join(plain(c) for c in node.children)


def count_tag(node: Node, tag: str) -> int:
    return int(node.tag == tag) + sum(count_tag(child, tag) for child in node.children if isinstance(child, Node))


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00a0", " ")).strip()


def escape(value: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", value)


def doc_link(href: str, source_url: str) -> str:
    if not href:
        return ""
    if href.lower().startswith(("javascript:", "data:")):
        return ""
    resolved = urlsplit(urljoin(source_url, href))
    if (resolved.scheme not in {"http", "https"} or resolved.username or resolved.password
            or re.search(r"(?:token|session|auth|password|cookie|signature)", resolved.query, re.I)):
        return ""
    return urlunsplit((resolved.scheme, resolved.netloc,
                       quote(resolved.path, safe="/%:@"),
                       quote(resolved.query, safe="=&%:@/+?,"),
                       quote(resolved.fragment, safe="%-._~")))


def inline(node: Node | str, source_url: str) -> str:
    if isinstance(node, str):
        return escape(re.sub(r"\s+", " ", node.replace("\u00a0", " ")))
    tag = node.tag
    if tag in SKIP:
        return ""
    if tag == "br":
        return "<br>"
    if tag == "img":
        src = doc_link(node.attrs.get("src", ""), source_url)
        alt = norm(node.attrs.get("alt", "")) or "Иллюстрация из документации 1С"
        return f"![{escape(alt)}]({src})" if src else f"[{escape(alt)}]"
    contents = "".join(inline(c, source_url) for c in node.children)
    if tag == "a":
        href = doc_link(node.attrs.get("href", ""), source_url)
        label = contents.strip()
        return f"[{label}]({href})" if href and label else label
    if tag in {"strong", "b"} and contents.strip():
        return f"**{contents.strip()}**"
    if tag in {"em", "i"} and contents.strip():
        return f"*{contents.strip()}*"
    if tag in {"code", "tt"} and contents.strip():
        code = contents.strip().replace("`", "'")
        return f"`{code}`"
    return contents


def safe_table_html(node: Node, source_url: str) -> str:
    """Keep accounting-table col/row spans without retaining source scripts/styles."""
    structural = {"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td"}
    inline = {"p", "div", "ul", "ol", "li", "b", "strong", "i", "em", "u", "code", "sub", "sup", "small"}

    def safe_url(value: str) -> str:
        url = doc_link(value, source_url)
        parsed = urlsplit(url)
        return url if parsed.scheme.lower() in {"http", "https"} and parsed.netloc else ""

    def render(value: Node | str) -> str:
        if isinstance(value, str):
            return html_escape(value)
        tag = value.tag
        if tag in SKIP:
            return ""
        if tag == "br":
            return "<br>"
        if tag == "img":
            src = safe_url(value.attrs.get("src", ""))
            alt = value.attrs.get("alt", "") or "Иллюстрация из документации 1С"
            return f'<img src="{html_escape(src, quote=True)}" alt="{html_escape(alt, quote=True)}">' if src else html_escape(alt)
        children = "".join(map(render, value.children))
        if tag == "a":
            href = safe_url(value.attrs.get("href", ""))
            return f'<a href="{html_escape(href, quote=True)}">{children}</a>' if href else children
        if tag not in structural | inline:
            return children
        attrs = ""
        if tag in {"td", "th"}:
            for name in ("colspan", "rowspan"):
                raw = value.attrs.get(name, "")
                if raw.isdigit() and 1 < int(raw) <= 100:
                    attrs += f' {name}="{raw}"'
        suffix = "\n" if tag in structural else ""
        return f"<{tag}{attrs}>{children}</{tag}>{suffix}"

    return "\n".join(line.rstrip() for line in render(node).splitlines()).rstrip()


def table_markdown(node: Node, source_url: str) -> str:
    def has_spans(value: Node) -> bool:
        return (any(value.attrs.get(name, "1") != "1" for name in ("colspan", "rowspan"))
                or any(has_spans(child) for child in value.children if isinstance(child, Node)))
    if has_spans(node):
        return safe_table_html(node, source_url)
    rows: list[list[str]] = []
    def visit(n: Node) -> None:
        if n.tag == "tr":
            cells = []
            for c in n.children:
                if isinstance(c, Node) and c.tag in {"th", "td"}:
                    value = norm(inline(c, source_url)).replace("|", "\\|")
                    span = c.attrs.get("colspan")
                    if span and span != "1":
                        value += f" (colspan {span})"
                    span = c.attrs.get("rowspan")
                    if span and span != "1":
                        value += f" (rowspan {span})"
                    cells.append(value)
            if cells:
                rows.append(cells)
            return
        for c in n.children:
            if isinstance(c, Node):
                visit(c)
    visit(node)
    if not rows:
        return norm(inline(node, source_url))
    width = max(map(len, rows))
    rows = [r + [""] * (width - len(r)) for r in rows]
    header = rows[0]
    return "\n".join(["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |", *("| " + " | ".join(r) + " |" for r in rows[1:])])


def list_markdown(node: Node, source_url: str, depth: int = 0) -> str:
    ordered = node.tag == "ol"
    lines = []
    n = 0
    for child in node.children:
        if not isinstance(child, Node) or child.tag != "li":
            continue
        n += 1
        parts = []
        nested = []
        for c in child.children:
            if isinstance(c, Node) and c.tag in {"ul", "ol"}:
                nested.append(c)
            else:
                parts.append(inline(c, source_url))
        prefix = f"{n}. " if ordered else "- "
        lines.append("  " * depth + prefix + norm("".join(parts)))
        for inner in nested:
            lines.append(list_markdown(inner, source_url, depth + 1))
    return "\n".join(lines)


def blocks(root: Node) -> list[Node]:
    result: list[Node] = []
    def visit(node: Node) -> None:
        if node.tag in BLOCKS:
            result.append(node)
            return
        if node.tag == "img":
            result.append(node)
            return
        for child in node.children:
            if isinstance(child, Node):
                visit(child)
    visit(root)
    return result


def unrepresented_text(root: Node) -> list[str]:
    missing = []
    def visit(node: Node) -> None:
        if node.tag in BLOCKS or node.tag == "img":
            return
        if node.tag == "a" and "copy_source" in node.attrs.get("class", "").split():
            return  # Portal control injected next to source code examples.
        for child in node.children:
            if isinstance(child, str):
                if norm(child):
                    missing.append(norm(child)[:100])
            else:
                visit(child)
    visit(root)
    return missing


def heading_anchor(node: Node) -> str | None:
    parent = node.parent
    while parent is not None and parent.tag != "root":
        if parent.tag == "a":
            anchor = parent.attrs.get("id") or parent.attrs.get("name")
            if anchor and re.fullmatch(r"[A-Za-z0-9_.-]+", anchor):
                return anchor
        parent = parent.parent
    return None


def block_markdown(node: Node, source_url: str) -> str:
    if node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(node.tag[1])
        title = norm(inline(node, source_url))
        anchor = heading_anchor(node)
        return "\n".join([*([f'<a id="{anchor}"></a>'] if anchor else []), "#" * level + " " + title]) if title else ""
    if node.tag == "pre":
        value = plain(node).strip("\n\r")
        value = value.replace("\r\n", "\n").replace("\r", "\n")
        fence = "`" * (max((len(x.group()) for x in re.finditer(r"`+", value)), default=0) + 1)
        fence = fence if len(fence) >= 3 else "```"
        return f"{fence}\n{value}\n{fence}" if value.strip() else ""
    if node.tag == "table":
        return table_markdown(node, source_url)
    if node.tag in {"ul", "ol"}:
        return list_markdown(node, source_url)
    if node.tag == "blockquote":
        return "> " + norm(inline(node, source_url))
    return norm(inline(node, source_url))


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chapter_number(title: str) -> int | None:
    m = re.search(r"(?:Глава|глава)\s+(\d+)", title)
    return int(m.group(1)) if m else None


def target_id(href: str, text: str, chapter_to_record: dict[int, str]) -> str | None:
    for value in (href, text):
        value = unquote(value).lower()
        m = re.search(r"глава\s*[\s._-]*(\d+)", value)
        if m and int(m.group(1)) in chapter_to_record:
            return chapter_to_record[int(m.group(1))]
    return None


def links(root: Node, source_url: str, chapter_to_record: dict[int, str], current_record_id: str | None = None) -> tuple[list[dict[str, str]], Counter]:
    result = []
    counts: Counter = Counter()
    seen = set()
    def preceding_text(target: Node) -> str:
        parent = target.parent
        while parent is not None and parent.tag not in {"p", "li", "root"}:
            parent = parent.parent
        if parent is None:
            return ""
        pieces = []
        def walk(node: Node) -> bool:
            if node is target:
                return True
            for child in node.children:
                if child is target:
                    return True
                if isinstance(child, str):
                    pieces.append(child)
                elif walk(child):
                    return True
            return False
        walk(parent)
        return norm("".join(pieces))[-60:].lower()

    def visit(n: Node) -> None:
        if n.tag == "a":
            if "copy_source" in n.attrs.get("class", "").split():
                return
            href = doc_link(n.attrs.get("href", ""), source_url)
            label = norm(plain(n))
            if href:
                parsed = urlsplit(href)
                source = urlsplit(source_url)
                in_corpus_site = parsed.netloc == source.netloc and parsed.path.startswith("/db/v8325doc/")
                target = target_id(href, label, chapter_to_record) if in_corpus_site else None
                context = preceding_text(n)
                relation = "see_also" if re.search(r"(?:см\.|смотри|подробнее)\s*(?:здесь\s*)?$", context) else "links_to"
                if (parsed.scheme, parsed.netloc, parsed.path) == (source.scheme, source.netloc, source.path) or (target and target == current_record_id):
                    kind = "same_page"
                    target = None
                elif target:
                    kind = "internal"
                elif parsed.netloc == source.netloc:
                    kind = "site_unmapped"
                else:
                    kind = "external"
                key = (href, target, relation)
                if key not in seen:
                    seen.add(key)
                    result.append({"href": href, "label": label[:200], "type": kind, **({"relation": relation, "record_id": target} if target else {})})
                    counts[kind] += 1
                    if target:
                        counts[relation] += 1
        for c in n.children:
            if isinstance(c, Node):
                visit(c)
    visit(root)
    return result, counts


def slug(value: str) -> str:
    s = value.lower().replace("ё", "е")
    s = re.sub(r"[^\w -]+", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s or "section"


def record_base(record_id: str, title: str, md_path: str, source_url: str, observed: str) -> dict:
    return {
        "schema_version": 1, "record_id": record_id, "pair_revision": 1,
        "title": title, "kind": "knowledge", "status": "declared",
        "updated_at": observed, "language": "ru", "md_path": md_path,
        "summary": f"Раздел документации платформы 1С:Предприятие 8.3.25: {title}.",
        "keywords": ["1С", "8.3.25", title],
        "source_refs": [source_url, "https://its.1c.ru/"],
        "sections": [], "relations": [],
    }


def make_sections(items: list[tuple[Node, str]]) -> list[dict]:
    sections = []
    seen = Counter()
    for index, (node, _) in enumerate(items):
        if node.tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            continue
        heading = norm(plain(node))
        if not heading:
            continue
        key = slug(heading)
        seen[key] += 1
        anchor = heading_anchor(node) or (key if seen[key] == 1 else f"{key}-{seen[key]}")
        excerpt = ""
        for following, _ in items[index + 1:]:
            if following.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            excerpt = norm(plain(following))
            if excerpt:
                break
        excerpt = excerpt[:240].rsplit(" ", 1)[0] if len(excerpt) > 240 else excerpt
        sections.append({"id": f"s{len(sections)+1}", "heading": heading, "anchor": anchor,
                         "summary": excerpt or f"Раздел «{heading}» исходного руководства.", "keywords": [heading]})
    if not sections:
        sections.append({"id": "content", "heading": "Содержание", "anchor": "содержание",
                         "summary": "Текст исходного документа.", "keywords": ["содержание"]})
    return sections


def lead_summary(items: list[tuple[Node, str]], title: str) -> str:
    excerpt = next((norm(plain(node)) for node, _ in items if node.tag == "p" and norm(plain(node))), "")
    excerpt = excerpt[:320].rsplit(" ", 1)[0] if len(excerpt) > 320 else excerpt
    return excerpt or f"Раздел документации платформы 1С:Предприятие 8.3.25: {title}."


def source_body(items: list[tuple[Node, str]], chapter_title: str) -> str:
    rendered = []
    for node, markdown in items:
        if node.tag == "h1" and norm(plain(node)) == chapter_title:
            anchor = heading_anchor(node)
            if anchor:
                rendered.append(f'<a id="{anchor}"></a>')
            continue
        rendered.append(markdown)
    return "\n\n".join(rendered)


def group_items(items: list[tuple[Node, str]], title: str) -> list[list[tuple[Node, str]]]:
    """Split only at document headings, choosing the deepest useful boundary."""
    groups = [items]
    for level in range(2, 7):
        changed = False
        next_groups = []
        for group in groups:
            if len("\n\n".join(text for _, text in group).encode("utf-8")) <= TARGET_MD_BYTES:
                next_groups.append(group)
                continue
            pieces: list[list[tuple[Node, str]]] = []
            current: list[tuple[Node, str]] = []
            for item in group:
                if item[0].tag == f"h{level}" and current:
                    pieces.append(current); current = []
                current.append(item)
            if current:
                pieces.append(current)
            if len(pieces) > 1:
                next_groups.extend(pieces); changed = True
            else:
                next_groups.append(group)
        groups = next_groups
        if all(len("\n\n".join(text for _, text in g).encode("utf-8")) <= TARGET_MD_BYTES for g in groups):
            break
        if not changed and level == 6:
            break
    packed: list[list[tuple[Node, str]]] = []
    for group in groups:
        if packed and len("\n\n".join(t for _, t in packed[-1] + group).encode("utf-8")) <= TARGET_MD_BYTES:
            packed[-1].extend(group)
        else:
            packed.append(group)
    groups = packed
    final = []
    for group in groups:
        if len("\n\n".join(t for _, t in group).encode("utf-8")) <= TARGET_MD_BYTES:
            final.append(group)
            continue
        # A long section without deeper headings is divided at paragraph/list/
        # table boundaries. These are source-authored units, not token chunks.
        current = []
        for item in group:
            if len(item[1].encode("utf-8")) > TARGET_MD_BYTES:
                raise ValueError(f"{title}: one source block exceeds the semantic size limit")
            candidate = current + [item]
            if current and len("\n\n".join(t for _, t in candidate).encode("utf-8")) > TARGET_MD_BYTES:
                final.append(current)
                current = []
            current.append(item)
        if current:
            final.append(current)
    return final


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_record(path: Path, data: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    json_rel = path.with_suffix(".json").relative_to(ROOT).as_posix()
    markdown = f"<!-- 1c-record record_id: {data['record_id']} pair_revision: {data['pair_revision']} json_path: {json_rel} -->\n" + body.rstrip() + "\n"
    size = len(markdown.encode("utf-8"))
    if size > MAX_MD_BYTES:
        raise ValueError(f"{path}: {size} bytes exceeds 256 KiB")
    path.write_text(markdown, encoding="utf-8")
    data["content_sha256"] = sha(markdown)
    write_json(path.with_suffix(".json"), data)


def build(raw_dir: Path, observed: str) -> dict:
    raw = {}
    for sid in EXPECTED_IDS:
        path = raw_dir / f"{sid}.json"
        if not path.exists():
            raise FileNotFoundError(f"Missing article {sid}: {path}")
        d = json.loads(path.read_text(encoding="utf-8"))
        if str(d.get("source_id")) != str(sid) or d.get("source_url") != f"https://its.1c.ru/db/v8325doc/content/{sid}/hdoc":
            raise ValueError(f"Unexpected source identity in {path}")
        source = d.get("doc_html", "")
        if not isinstance(source, str) or len(source) < 5000 or "<body" not in source.lower() or "</html>" not in source.lower():
            raise ValueError(f"Missing or incomplete article HTML: {path}")
        if d.get("html_length") != len(source):
            raise ValueError(f"Captured length mismatch: {path}")
        if re.search(r"(?:Set-Cookie|Authorization:|sessionid=|JSESSIONID=)", source, re.I):
            raise ValueError(f"Possible credential in article: {path}")
        raw[sid] = d

    owned_dir = ROOT / "knowledge/records/1c-8.3.25/developer-guide"
    old_records = {}
    if owned_dir.exists():
        for path in owned_dir.glob("*.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                old_records[item["record_id"]] = item
            except (KeyError, ValueError):
                pass
    old_manifest_path = ROOT / "sources/1c-docs/manifest.json"
    try:
        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        old_manifest = {}
    old_hashes = {int(item["source_id"]): item["source_html_sha256"] for item in old_manifest.get("pages", []) if "source_id" in item and "source_html_sha256" in item}
    corpus_changed = old_hashes != {sid: sha(d["doc_html"]) for sid, d in raw.items()}
    for path in owned_dir.glob("*.md"):
        path.unlink()
    for path in owned_dir.glob("*.json"):
        path.unlink()

    def revision(record_id: str, source_hash: str) -> int:
        previous = old_records.get(record_id)
        if not previous:
            return 1
        old_hash = previous.get("provenance", {}).get("source_html_sha256")
        return previous["pair_revision"] + (old_hash != source_hash)

    # Names are stable across reimports even if the publisher edits a title.
    chapter_to_record = {}
    for sid, d in raw.items():
        n = chapter_number(d["title"])
        if n is not None:
            chapter_to_record[n] = f"onec-8325-developer-chapter-{n:02d}"
    records = []
    manifest_entries = []
    link_edges = set()
    for sid, d in raw.items():
        title = norm(d["title"])
        n = chapter_number(title)
        record_id = chapter_to_record[n] if n is not None else ("onec-8325-developer-intro" if sid == 51 else f"onec-8325-developer-appendix-{1 if sid == 91 else 12:02d}")
        parser = ArticleParser(); parser.feed(d["doc_html"])
        content_blocks = blocks(parser.root)
        if not content_blocks or not any(node.tag == "h1" for node in content_blocks):
            raise ValueError(f"No chapter heading in {sid}")
        source_heading = next(norm(plain(node)) for node in content_blocks if node.tag == "h1")
        if source_heading != title:
            raise ValueError(f"Table-of-contents title and article heading differ in {sid}: {title!r} / {source_heading!r}")
        omitted = unrepresented_text(parser.root)
        if omitted:
            raise ValueError(f"Article {sid} contains unrepresented body text: {omitted[:5]}")
        rendered = [(node, block_markdown(node, d["source_url"])) for node in content_blocks]
        rendered = [(node, text) for node, text in rendered if text.strip()]
        parts = group_items(rendered, title)
        parsed_links, counts = links(parser.root, d["source_url"], chapter_to_record, record_id)
        target_relations = sorted({(link["record_id"], link["relation"]) for link in parsed_links if link.get("record_id") and link["record_id"] != record_id})
        for target, relation in target_relations:
            link_edges.add((record_id, target, relation))
        entries = []
        if len(parts) == 1:
            path = ROOT / "knowledge/records/1c-8.3.25/developer-guide" / f"{record_id}.md"
            data = record_base(record_id, title, path.relative_to(ROOT).as_posix(), d["source_url"], observed)
            data["pair_revision"] = revision(record_id, sha(d["doc_html"]))
            data["summary"] = lead_summary(parts[0], title)
            data["sections"] = make_sections(parts[0]); data["relations"] = [{"type": "part_of", "target": "onec-8325-developer-guide"}] + [{"type": relation, "target": target} for target, relation in target_relations]
            data["provenance"] = {"source_id": sid, "source_version": "8.3.25", "source_html_sha256": sha(d["doc_html"]), "observed_at": observed, "capture": "authenticated-browser-article-iframe-clipboard-transfer", "rights_basis": "user-managed-local-access"}
            data["source_links"] = parsed_links
            body = f"# {title}\n\nИсточник: [{d['source_url']}]({d['source_url']}) · Платформа 8.3.25 · Получено {observed}.\n\n" + source_body(parts[0], title)
            write_record(path, data, body)
            entries.append(data)
        else:
            # Chapter overview links the semantically split sections in source order.
            path = ROOT / "knowledge/records/1c-8.3.25/developer-guide" / f"{record_id}.md"
            overview = record_base(record_id, title, path.relative_to(ROOT).as_posix(), d["source_url"], observed)
            overview["pair_revision"] = revision(record_id, sha(d["doc_html"]))
            overview["summary"] = f"Оглавление главы «{title}»; полный текст разделён по заголовкам исходного документа."
            overview["sections"] = [{"id": "parts", "heading": "Разделы", "anchor": "разделы", "summary": "Упорядоченный список частей главы.", "keywords": ["разделы"]}]
            overview["relations"] = [{"type": "part_of", "target": "onec-8325-developer-guide"}] + [{"type": relation, "target": target} for target, relation in target_relations]
            overview["provenance"] = {"source_id": sid, "source_version": "8.3.25", "source_html_sha256": sha(d["doc_html"]), "observed_at": observed, "capture": "authenticated-browser-article-iframe-clipboard-transfer", "rights_basis": "user-managed-local-access"}
            overview["source_links"] = parsed_links
            lines = [f"# {title}", "", f"Источник: [{d['source_url']}]({d['source_url']}) · Платформа 8.3.25 · Получено {observed}.", "", "## Разделы", ""]
            for idx, group in enumerate(parts, 1):
                part_id = f"{record_id}-part-{idx:02d}"
                part_title = next((norm(plain(node)) for node, _ in group if node.tag in {"h2", "h3", "h4", "h5", "h6"}), f"Часть {idx}")
                part_path = path.with_name(f"{part_id}.md")
                pdata = record_base(part_id, f"{title} — {part_title}", part_path.relative_to(ROOT).as_posix(), d["source_url"], observed)
                pdata["pair_revision"] = revision(part_id, sha(d["doc_html"]))
                pdata["summary"] = lead_summary(group, part_title)
                pdata["sections"] = make_sections(group)
                pdata["relations"] = [{"type": "part_of", "target": record_id}]
                pdata["provenance"] = overview["provenance"] | {"part_index": idx, "part_count": len(parts)}
                pbody = f"# {title} — {part_title}\n\n[Оглавление главы]({path.name}) · [Источник]({d['source_url']})\n\n" + source_body(group, title)
                write_record(part_path, pdata, pbody)
                entries.append(pdata)
                overview["relations"].append({"type": "has_part", "target": part_id})
                lines.append(f"{idx}. [{part_title}]({part_path.name})")
            write_record(path, overview, "\n".join(lines))
            entries.insert(0, overview)
        records.extend(entries)
        manifest_entries.append({"source_id": sid, "title": title, "source_url": d["source_url"], "source_html_sha256": sha(d["doc_html"]), "html_characters": len(d["doc_html"]), "record_ids": [r["record_id"] for r in entries], "links_internal": counts["internal"], "links_same_page": counts["same_page"], "links_site_unmapped": counts["site_unmapped"], "links_external": counts["external"], "see_also_internal": counts["see_also"], "image_references": count_tag(parser.root, "img")})

    overview_id = "onec-8325-developer-guide"
    overview_path = ROOT / "knowledge/records/1c-8.3.25/developer-guide/overview.md"
    guide = record_base(overview_id, "Руководство разработчика 1С:Предприятие 8.3.25", overview_path.relative_to(ROOT).as_posix(), "https://its.1c.ru/db/v8325doc", observed)
    guide["pair_revision"] = old_records.get(overview_id, {}).get("pair_revision", 1) + (1 if old_records.get(overview_id) and corpus_changed else 0)
    guide["kind"] = "source_manifest"
    guide["summary"] = "Структурированный указатель введения, 39 глав и двух приложений руководства разработчика платформы 8.3.25."
    guide["sections"] = [{"id": "scope", "heading": "Объём", "anchor": "объём", "summary": "Состав импортированного руководства.", "keywords": ["объём"]}, {"id": "pages", "heading": "Страницы", "anchor": "страницы", "summary": "Ссылки на документы в порядке источника.", "keywords": ["страницы"]}, {"id": "limits", "heading": "Ограничения", "anchor": "ограничения", "summary": "Неопределённости по исходным активам и версии среды.", "keywords": ["ограничения"]}]
    guide["source_refs"] = ["https://its.1c.ru/db/v8325doc", "https://its.1c.ru/"]
    guide["relations"] = [{"type": "has_page", "target": (chapter_to_record[chapter_number(raw[sid]["title"])] if chapter_number(raw[sid]["title"]) else ("onec-8325-developer-intro" if sid == 51 else f"onec-8325-developer-appendix-{1 if sid == 91 else 12:02d}"))} for sid in EXPECTED_IDS]
    guide["provenance"] = {"source_version": "8.3.25", "observed_at": observed, "rights_basis": "user-managed-local-access", "issue": "https://its.1c.ru/"}
    overview_lines = ["# Руководство разработчика 1С:Предприятие 8.3.25", "", "## Объём", "", "Введение, главы 1–39 и приложения 1 и 12. Статус материалов — `declared`: это содержимое опубликованной документации, а не проверка текущей рабочей среды.", "", "## Страницы", ""]
    for sid in EXPECTED_IDS:
        r = next(r for r in records if r["provenance"]["source_id"] == sid)
        rel = Path(r["md_path"]).name
        overview_lines.append(f"- [{r['title']}]({rel}) ([источник]({raw[sid]['source_url']}))")
    overview_lines += ["", "## Ограничения", "", "Иллюстрации связаны с защищённым источником URL-ссылками; доступность бинарных файлов вне авторизованной сессии не проверена. Соответствие документации конкретной облачной базе 1С не проверялось."]
    write_record(overview_path, guide, "\n".join(overview_lines))
    records.insert(0, guide)

    source_manifest = {"schema_version": 1, "corpus": "1c-enterprise-8.3.25-developer-guide", "source_root": "https://its.1c.ru/db/v8325doc", "observed_at": observed, "rights_basis": "user-managed-local-access", "issue": "https://its.1c.ru/", "capture_method": "authenticated-browser-article-iframe-clipboard-transfer", "source_page_count": len(EXPECTED_IDS), "pages": manifest_entries, "link_classification": {"links_internal": "Links mapped to another selected corpus page.", "links_same_page": "Anchors and other references to the same source article.", "links_site_unmapped": "Links on its.1c.ru outside the selected 42 pages or without a reliable target mapping.", "links_external": "Links to a different host or non-HTTP destination.", "see_also_internal": "Internal links introduced by an explicit see-also reference."}, "excluded_from_scope": ["Other manuals and appendices in the portal table of contents were outside the owner-approved scope.", "Image binaries were not copied; Markdown keeps links to their authenticated source URLs."], "notes": ["Source HTML was captured from the article iframe; portal shell, account information, cookies and credentials are excluded.", "Copy-to-clipboard controls in the article HTML are excluded from the link index."]}
    write_json(ROOT / "sources/1c-docs/manifest.json", source_manifest)

    catalog_path = ROOT / "knowledge/catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["pair_revision"] += int(corpus_changed); catalog["updated_at"] = observed
    catalog["summary"] = f"Реестр долговечных пар, включая {len(records)} записей документации платформы 1С 8.3.25."
    catalog["source_refs"] = list(dict.fromkeys([*catalog["source_refs"], "https://its.1c.ru/"]))
    catalog["records"] = [entry for entry in catalog["records"] if not entry["md_path"].startswith("knowledge/records/1c-8.3.25/developer-guide/")]
    catalog["records"].extend({k: r[k] for k in ("record_id", "pair_revision", "kind", "status", "md_path")} | {"json_path": r["md_path"].removesuffix(".md") + ".json"} for r in records)
    for entry in catalog["records"]:
        if entry["record_id"] == "onec-knowledge-catalog":
            entry["pair_revision"] = catalog["pair_revision"]
        if entry["record_id"] == "onec-knowledge-graph":
            entry["pair_revision"] += int(corpus_changed)
    write_json(catalog_path, catalog)

    graph_path = ROOT / "knowledge/graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["pair_revision"] += int(corpus_changed); graph["updated_at"] = observed
    graph["summary"] = f"Типизированная карта репозитория и {len(records)} записей документации платформы 1С 8.3.25."
    graph["source_refs"] = list(dict.fromkeys([*graph["source_refs"], "https://its.1c.ru/"]))
    graph["sections"] = [
        {"id": "role", "heading": "Роль", "anchor": "роль", "summary": "Граф помогает навигации и не подтверждает состояние рабочей среды.", "keywords": ["navigation", "authority"]},
        {"id": "relations", "heading": "Связи", "anchor": "связи", "summary": "Количество типизированных отношений в графе.", "keywords": ["edges", "relations"]},
        {"id": "hyperedges", "heading": "Hyperedges", "anchor": "hyperedges", "summary": "Многоместные отношения не обнаружены.", "keywords": ["hyperedges"]},
    ]
    graph["nodes"] = [node for node in graph["nodes"] if not node["id"].startswith("record:onec-8325-developer-")]
    graph["edges"] = [edge for edge in graph["edges"] if not edge["id"].startswith("edge:onec-8325-developer-") and not edge["id"].startswith("edge:repo-contains-onec-8325-developer-")]
    baseline_edge_count = len(graph["edges"])
    for r in records:
        graph["nodes"].append({"id": "record:" + r["record_id"], "type": "record", "status": r["status"], "label": r["title"]})
        graph["edges"].append({"id": "edge:repo-contains-" + r["record_id"], "type": "contains_record", "status": "verified", "from": "corpus:local", "to": "record:" + r["record_id"], "meaning": "Пара Markdown и JSON зарегистрирована в каталоге репозитория."})
    for r in records:
        for relation in r["relations"]:
            if relation["type"] in {"part_of", "has_part", "has_page"}:
                graph["edges"].append({"id": f"edge:{r['record_id']}:{relation['type']}:{relation['target']}", "type": relation["type"], "status": "declared", "from": "record:" + r["record_id"], "to": "record:" + relation["target"], "meaning": "Иерархия подтверждена структурой исходного руководства."})
    for source, target, relation in sorted(link_edges):
        graph["edges"].append({"id": f"edge:{source}:{relation}:{target}", "type": relation, "status": "declared", "from": "record:" + source, "to": "record:" + target, "meaning": "Исходный документ содержит ссылку на указанную главу."})
    write_json(graph_path, graph)

    # Rewrite the human indexes and keep metadata aligned with JSON revisions.
    cat_lines = [f"<!-- 1c-record record_id: onec-knowledge-catalog pair_revision: {catalog['pair_revision']} json_path: knowledge/catalog.json -->", "# Каталог долговечных записей", "", "Машинный реестр: [`catalog.json`](catalog.json).", "", "## Текущие записи", "", "| ID | Вид | Статус | Markdown |", "|---|---|---|---|"]
    cat_lines.extend(f"| `{e['record_id']}` | {e['kind']} | {e['status']} | `{e['md_path']}` |" for e in catalog["records"])
    cat_lines += ["", "## Правило обновления", "", "Каждая пара меняется вместе с каталогом и графом в одном PR."]
    (ROOT / "knowledge/catalog.md").write_text("\n".join(cat_lines) + "\n", encoding="utf-8")
    counts = Counter(e["type"] for e in graph["edges"])
    graph_lines = [f"<!-- 1c-record record_id: onec-knowledge-graph pair_revision: {graph['pair_revision']} json_path: knowledge/graph.json -->", "# Типизированный граф знаний 1С", "", "Машинное представление: [`graph.json`](graph.json).", "", "## Роль", "", "Граф служит навигацией по зарегистрированным записям и не подтверждает соответствие конкретной среде 1С.", "", "## Связи", ""]
    graph_lines.extend(f"- `{key}`: {value}" for key, value in sorted(counts.items()))
    graph_lines += ["", "## Hyperedges", "", "Многоместных связей в импортированном материале не выявлено; массив `hyperedges` пуст."]
    (ROOT / "knowledge/graph.md").write_text("\n".join(graph_lines) + "\n", encoding="utf-8")
    return {"source_pages": len(EXPECTED_IDS), "records": len(records), "nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "imported_relations": dict(Counter(e["type"] for e in graph["edges"][baseline_edge_count:])), "relations": dict(counts), "total_html_characters": sum(p["html_characters"] for p in manifest_entries), "image_references": sum(p["image_references"] for p in manifest_entries)}



if __name__ == "__main__":
    raise SystemExit("Use collect.py with an explicit --output directory")

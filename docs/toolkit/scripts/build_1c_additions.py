#!/usr/bin/env python3
"""Import authenticated-browser captures of platform manuals and 1C operation indexes.

The script accepts only local article-frame snapshots. It never signs in, reads browser
profile data, or fetches the portal. Raw captures stay in the ignored .import_raw tree.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import build_1c_docs as common

ROOT = Path(__file__).resolve().parents[1]
ISSUE = "https://its.1c.ru/"
ITS = "https://its.1c.ru"
RAW = ROOT / ".import_raw/additional-1c"
PLATFORM_ROOT = "https://its.1c.ru/db/v8325doc"
HOOSN_ROOT = "https://its.1c.ru/db/hoosn"
PLATFORM_BOOKS = {
    "user-guide": "Руководство пользователя",
    "administrator-guide": "Руководство администратора",
    "client-server-guide": "Клиент-серверный вариант. Руководство администратора",
    "taxi-user-guide": "Руководство пользователя. Интерфейс «Такси»",
}
HOOSN_IDS = [38, 41, 57, 60, 64, 88, 89, 92, 97, 98, 105, 159, 163, 220, 223, 404, 679, 906]
MAX_MD_BYTES = 256 * 1024
TOPIC_PATTERNS = {
    "bank": r"банк|расчетн\w*\s+счет|счет\s+51|платежн\w*\s+поруч|валют",
    "cash": r"касс|наличн",
    "payroll": r"зарплат|оплат\w*\s+труда|ндфл|есн|пфр|сзв|пособи|страхов\w*\s+взнос",
    "purchases": r"поставщик|поступлен\w*\s+товар|приобретен\w*\s+(?:товар|сырья|материал)|материально-производствен",
    "services": r"услуг|выполнен\w*\s+работ",
    "counterparty-settlements": r"контрагент|договор|расчет\w*|акт\s+сверки|покупател|поставщик|аванс",
}
RESTRICTED_OBSERVATIONS = {}  # Supplied captures never inherit another account's access result.
CONFIGURATION_SEARCH_REVIEW = {"status": "not_checked", "result_review": "Check applicability for your selected configuration."}


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha(value: object) -> str:
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_path(rel: str) -> Path:
    return ROOT / rel


def previous_record(path: Path) -> dict | None:
    json_path = path.with_suffix(".json")
    if not json_path.exists():
        return None
    try:
        return read_json(json_path)
    except (OSError, ValueError):
        return None


def revision(path: Path, source_hash: str) -> int:
    old = previous_record(path)
    if not old:
        return 1
    old_hash = old.get("provenance", {}).get("source_sha256")
    return int(old.get("pair_revision", 1)) + int(old_hash != source_hash)


def record_base(record_id: str, title: str, rel: str, kind: str, summary: str,
                source_refs: list[str], observed: str, source_hash: str,
                provenance: dict, sections: list[dict], relations: list[dict],
                keywords: list[str]) -> dict:
    path = record_path(rel)
    return {
        "schema_version": 1,
        "record_id": record_id,
        "pair_revision": revision(path, source_hash),
        "title": title,
        "kind": kind,
        "status": "declared",
        "updated_at": observed,
        "language": "ru",
        "md_path": rel,
        "summary": summary,
        "keywords": list(dict.fromkeys(["1С", *keywords])),
        "source_refs": list(dict.fromkeys([*source_refs, ISSUE])),
        "sections": sections or [{
            "id": "content", "heading": "Содержание", "anchor": "содержание",
            "summary": "Содержание зафиксировано в Markdown-записи.",
            "keywords": ["содержание"],
        }],
        "relations": relations,
        "provenance": provenance | {"source_sha256": source_hash, "observed_at": observed},
    }


def save_record(path: Path, data: dict, body: str) -> dict:
    body = "\n".join(line.rstrip(" \t") for line in body.rstrip().splitlines()).rstrip()
    old = previous_record(path)
    if path.exists() and old:
        old_text = path.read_text(encoding="utf-8")
        old_body = old_text.split("\n", 1)[1].rstrip() if "\n" in old_text else ""
        if old_body != body and data["pair_revision"] == old.get("pair_revision"):
            data["pair_revision"] += 1
    markdown = (
        f"<!-- 1c-record record_id: {data['record_id']} pair_revision: "
        f"{data['pair_revision']} json_path: {path.with_suffix('.json').relative_to(ROOT).as_posix()} -->\n"
        + body.rstrip() + "\n"
    )
    if len(markdown.encode("utf-8")) > MAX_MD_BYTES:
        raise ValueError(f"{path}: Markdown exceeds 256 KiB; split at a source-authored heading")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    data["content_sha256"] = sha(markdown)
    write_json(path.with_suffix(".json"), data)
    return data


def walk(node: common.Node):
    yield node
    for child in node.children:
        if isinstance(child, common.Node):
            yield from walk(child)


def source_links(root: common.Node, source_url: str, current_sid: int,
                 page_id_by_sid: dict[int, str]) -> tuple[list[dict], list[dict], Counter]:
    links: list[dict] = []
    relations: set[tuple[str, str]] = set()
    counts: Counter = Counter()
    seen = set()
    current = urlsplit(source_url)
    for node in walk(root):
        if node.tag != "a":
            continue
        if "copy_source" in node.attrs.get("class", "").split():
            continue
        href = common.doc_link(node.attrs.get("href", ""), source_url)
        label = common.norm(common.plain(node))[:200]
        if not href:
            continue
        parsed = urlsplit(href)
        match = re.search(r"/db/v8325doc/content/(\d+)(?:/|$)", parsed.path)
        target_sid = int(match.group(1)) if match else None
        if parsed.netloc == current.netloc and target_sid == current_sid:
            kind = "same_page"
        elif parsed.netloc == current.netloc and target_sid in page_id_by_sid:
            kind = "internal"
            relations.add((page_id_by_sid[target_sid], "links_to"))
        elif parsed.netloc == current.netloc:
            kind = "site_unmapped"
        else:
            kind = "external"
        key = (href, label, kind)
        if key not in seen:
            seen.add(key)
            links.append({"href": href, "label": label, "type": kind})
            counts[kind] += 1
    return links, [{"type": rel, "target": target} for target, rel in sorted(relations)], counts


def body_without_title_heading(items: list[tuple[common.Node, str]], title: str) -> str:
    rendered = []
    removed = False
    for node, markdown in items:
        if not removed and node.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and common.norm(common.plain(node)) == title:
            removed = True
            continue
        rendered.append(markdown)
    return "\n\n".join(rendered)


def parse_article(source: dict, expected_url: str, min_chars: int = 300,
                  allow_unrepresented: bool = False) -> tuple[common.ArticleParser, list[tuple[common.Node, str]]]:
    if source.get("source_url") != expected_url:
        raise ValueError(f"Unexpected source URL for {source.get('source_id')}: {source.get('source_url')}")
    html = source.get("doc_html")
    if not isinstance(html, str) or len(html) < min_chars or "<body" not in html.lower() or "</html>" not in html.lower():
        raise ValueError(f"Missing or incomplete article-frame HTML for {source.get('source_id')}")
    if source.get("html_length") != len(html):
        raise ValueError(f"Captured HTML length mismatch for {source.get('source_id')}")
    if re.search(r"login\.1c\.ru|(?:authorization|set-cookie):|sessionid=|jsessionid=", html, re.I):
        raise ValueError("Possible credential or login shell in article capture")
    parser = common.ArticleParser()
    parser.feed(html)
    blocks = common.blocks(parser.root)
    if not blocks:
        raise ValueError(f"No article content blocks for {source.get('source_id')}")
    omitted = common.unrepresented_text(parser.root)
    if omitted:
        raise ValueError(f"Unrepresented article text in {source.get('source_id')}: {omitted[:5]}")
    rendered = [(node, common.block_markdown(node, expected_url)) for node in blocks]
    rendered = [(node, value) for node, value in rendered if value.strip()]
    if not rendered:
        raise ValueError(f"No rendered article text for {source.get('source_id')}")
    return parser, rendered


def make_platform_records(raw: Path, observed: str, page_id_by_sid: dict[int, str]) -> tuple[list[dict], dict[str, list[dict]], list[dict]]:
    toc = read_json(raw / "indexes/platform-toc.json")
    if toc.get("observed_at") != observed:
        raise ValueError("Platform table of contents must match the declared capture date")
    records: list[dict] = []
    by_book: dict[str, list[dict]] = {}
    manifest_books: list[dict] = []
    for book in toc.get("books", []):
        key = book["key"]
        if key not in PLATFORM_BOOKS or book.get("title") != PLATFORM_BOOKS[key]:
            raise ValueError(f"Unexpected platform manual in table of contents: {key}")
        pages_meta = book.get("pages", [])
        if len(pages_meta) != book.get("expected"):
            raise ValueError(f"Incomplete table of contents for {key}")
        book_records: list[dict] = []
        manifest_pages: list[dict] = []
        for page_meta in pages_meta:
            sid = int(page_meta["source_id"])
            capture_path = raw / "platform" / key / f"{sid}.json"
            source = read_json(capture_path)
            expected_url = f"{PLATFORM_ROOT}/content/{sid}/hdoc"
            title = common.norm(page_meta["title"])
            if source.get("collection") != f"platform/{key}" or common.norm(source.get("title", "")) != title:
                raise ValueError(f"Capture identity differs from table of contents for {key}/{sid}")
            parser, rendered = parse_article(source, expected_url, 5000)
            first_heading = next((common.norm(common.plain(n)) for n, _ in rendered if n.tag == "h1"), None)
            if first_heading != title:
                raise ValueError(f"Table-of-contents title differs from article heading for {key}/{sid}: {first_heading!r}")
            html = source["doc_html"]
            source_hash = sha(html)
            pid = page_id_by_sid[sid]
            rel_dir = f"knowledge/records/1c-8.3.25/platform-manuals/{key}"
            overview_rel = f"{rel_dir}/{pid}.md"
            links, link_relations, link_counts = source_links(parser.root, expected_url, sid, page_id_by_sid)
            parts = common.group_items(rendered, title)
            source_provenance = {
                "source_id": sid,
                "source_version": "8.3.25",
                "source_collection": key,
                "source_sha256": source_hash,
                "capture": "authenticated-Chrome-article-frame",
                "rights_basis": "user-managed-local-access",
            }
            book_id = f"onec-8325-platform-{key}"
            if len(parts) == 1:
                rels = [{"type": "part_of", "target": book_id}, *link_relations]
                data = record_base(
                    pid, title, overview_rel, "knowledge",
                    common.lead_summary(parts[0], title),
                    [expected_url], observed, source_hash, source_provenance,
                    common.make_sections(parts[0]), rels,
                    ["8.3.25", key.replace("-", " "), title],
                )
                data["source_links"] = links
                body = (
                    f"# {title}\n\nИсточник: [{expected_url}]({expected_url}) · "
                    f"Руководство {PLATFORM_BOOKS[key]} платформы 8.3.25 · Получено {observed}.\n\n"
                    + common.source_body(parts[0], title)
                )
                page_records = [save_record(record_path(overview_rel), data, body)]
            else:
                overview = record_base(
                    pid, title, overview_rel, "knowledge",
                    f"Оглавление статьи «{title}»; полный текст разделён по смысловым заголовкам источника.",
                    [expected_url], observed, source_hash, source_provenance,
                    [{"id": "parts", "heading": "Части", "anchor": "части",
                      "summary": "Упорядоченные смысловые части длинной статьи.",
                      "keywords": ["части", title]}],
                    [{"type": "part_of", "target": book_id}, *link_relations],
                    ["8.3.25", key.replace("-", " "), title],
                )
                overview["source_links"] = links
                lines = [
                    f"# {title}", "",
                    f"Источник: [{expected_url}]({expected_url}) · Руководство {PLATFORM_BOOKS[key]} платформы 8.3.25 · Получено {observed}.",
                    "", "## Части", "",
                ]
                page_records = []
                for number, group in enumerate(parts, 1):
                    part_id = f"{pid}-part-{number:02d}"
                    part_title = next((common.norm(common.plain(node)) for node, _ in group
                                       if node.tag in {"h2", "h3", "h4", "h5", "h6"}),
                                      f"Часть {number}")
                    part_rel = f"{rel_dir}/{part_id}.md"
                    part_data = record_base(
                        part_id, f"{title} — {part_title}", part_rel, "knowledge",
                        common.lead_summary(group, part_title), [expected_url], observed,
                        source_hash, source_provenance | {"part_index": number, "part_count": len(parts)},
                        common.make_sections(group), [{"type": "part_of", "target": pid}],
                        ["8.3.25", key.replace("-", " "), title, part_title],
                    )
                    part_body = (
                        f"# {title} — {part_title}\n\n[{title}]({Path(overview_rel).name}) · "
                        f"[Источник]({expected_url})\n\n" + common.source_body(group, title)
                    )
                    page_records.append(save_record(record_path(part_rel), part_data, part_body))
                    overview["relations"].append({"type": "has_part", "target": part_id})
                    lines.append(f"{number}. [{part_title}]({Path(part_rel).name})")
                page_records.insert(0, save_record(record_path(overview_rel), overview, "\n".join(lines)))
            records.extend(page_records)
            page_entry = {
                "source_id": sid,
                "title": title,
                "source_url": expected_url,
                "source_html_sha256": source_hash,
                "html_characters": len(html),
                "record_ids": [item["record_id"] for item in page_records],
                "link_counts": dict(link_counts),
                "image_references": common.count_tag(parser.root, "img"),
            }
            manifest_pages.append(page_entry)
            book_records.append({
                "source_id": sid, "title": title, "record_id": pid,
                "md_path": overview_rel, "source_url": expected_url,
                "part_count": len(parts),
            })
        by_book[key] = book_records
        manifest_books.append({
            "key": key,
            "title": book["title"],
            "browse_url": ITS + book["browse"],
            "source_page_count": len(manifest_pages),
            "pages": manifest_pages,
        })
    if sum(book["source_page_count"] for book in manifest_books) != 70:
        raise ValueError("Expected all 70 selected platform-manual pages")
    return records, by_book, manifest_books


def make_book_overviews(by_book: dict[str, list[dict]], manifest_books: list[dict],
                        observed: str, raw: Path) -> list[dict]:
    results = []
    for manifest in manifest_books:
        key = manifest["key"]
        book_id = f"onec-8325-platform-{key}"
        rel = f"knowledge/records/1c-8.3.25/platform-manuals/{key}/overview.md"
        toc = read_json(raw / "indexes/platform-toc.json")
        book = next(item for item in toc["books"] if item["key"] == key)
        source_hash = canonical_sha(manifest["pages"])
        source_refs = [manifest["browse_url"], *(p["source_url"] for p in manifest["pages"])]
        provenance = {
            "source_version": "8.3.25",
            "source_collection": key,
            "source_sha256": source_hash,
            "capture": "authenticated-Chrome-article-frame-and-visible-table-of-contents",
            "rights_basis": "user-managed-local-access",
        }
        relations = [{"type": "has_page", "target": p["record_id"]} for p in by_book[key]]
        data = record_base(
            book_id, manifest["title"], rel, "source_manifest",
            f"Навигация по {manifest['source_page_count']} импортированным статьям: {manifest['title']} для платформы 8.3.25.",
            source_refs, observed, source_hash, provenance,
            [{"id": "scope", "heading": "Состав", "anchor": "состав",
              "summary": f"Все {manifest['source_page_count']} статьи видимого содержания этого руководства.",
              "keywords": ["8.3.25", "состав"]},
             {"id": "pages", "heading": "Страницы", "anchor": "страницы",
              "summary": "Статьи идут в порядке содержания ИТС.",
              "keywords": ["страницы", "оглавление"]},
             {"id": "limits", "heading": "Применимость", "anchor": "применимость",
              "summary": "Руководство относится к платформе 8.3.25 и не описывает прикладную конфигурацию.",
              "keywords": ["ограничения", "конфигурация"]}],
            relations, ["8.3.25", "руководство", key.replace("-", " ")],
        )
        lines = [
            f"# {manifest['title']}",
            "",
            f"Источник: [{manifest['browse_url']}]({manifest['browse_url']}) · Платформа 8.3.25 · Получено {observed}.",
            "",
            "## Состав",
            "",
            f"В репозиторий перенесены все {manifest['source_page_count']} статьи, перечисленные в видимом оглавлении этого руководства. Полные тексты разбиты только по смысловым заголовкам источника; изображения сохранены как ссылки на оригинал.",
            "",
            "## Страницы",
            "",
        ]
        for page in by_book[key]:
            lines.append(f"- [{page['title']}]({Path(page['md_path']).name}) ([ИТС]({page['source_url']}))")
        lines += [
            "",
            "## Применимость",
            "",
            "Это руководство описывает платформу 1С:Предприятие 8.3.25. Оно не заменяет инструкции по конфигурации «Бухгалтерия для Беларуси» и не подтверждает настройки конкретной облачной базы.",
        ]
        results.append(save_record(record_path(rel), data, "\n".join(lines)))
    return results


def make_operation_records(raw: Path, observed: str) -> tuple[list[dict], list[dict]]:
    records: list[dict] = []
    manifest_pages = []
    for sid in HOOSN_IDS:
        path = raw / "hoosn-index" / f"{sid}.json"
        source = read_json(path)
        expected = f"{HOOSN_ROOT}/content/{sid}/hdoc"
        if source.get("collection") != "hoosn-index":
            raise ValueError(f"Unexpected operations index collection for {sid}")
        parser, rendered = parse_article(source, expected, allow_unrepresented=True)
        title = common.norm(source["title"])
        first_heading = next((common.norm(common.plain(n)) for n, _ in rendered
                              if n.tag in {"h1", "h2", "h3", "h4", "h5", "h6"}), None)
        if first_heading != title:
            raise ValueError(f"Index title differs from its article heading for {sid}")
        source_hash = sha(source["doc_html"])
        rel = f"knowledge/records/1c-8.3.25/operation-guides/hoosn-index-{sid:04d}.md"
        rid = f"onec-its-hoosn-index-{sid:04d}"
        links = []
        seen = set()
        for node in walk(parser.root):
            if node.tag != "a":
                continue
            href = common.doc_link(node.attrs.get("href", ""), expected)
            label = common.norm(common.plain(node))[:200]
            if not href or (href, label) in seen:
                continue
            seen.add((href, label))
            links.append({"href": href, "label": label, "type": "site_unmapped"})
        data = record_base(
            rid, title, rel, "knowledge",
            f"Навигационный раздел ИТС «Учет хозяйственных операций»: {title}. Сохранены текст раздела и все видимые ссылки на инструкции.",
            [expected, "https://its.1c.ru/db/hoosn"], observed, source_hash,
            {"source_id": sid, "source_version": "1C:Бухгалтерия 8 examples; exact configuration applicability unknown",
             "source_collection": "hoosn-index", "source_sha256": source_hash,
             "capture": "authenticated-Chrome-article-frame",
             "rights_basis": "user-managed-local-access"},
            common.make_sections(rendered), [], ["бухгалтерский учет", "хозяйственные операции", title],
        )
        data["source_links"] = links
        body = (
            f"# {title}\n\nИсточник: [{expected}]({expected}) · "
            f"Раздел «Учет хозяйственных операций» · Получено {observed}.\n\n"
            "Это сохраненный навигационный раздел: перечень тем и ссылки на отдельные инструкции. "
            "Применимость к выбранной конфигурации и доступ к связанным статьям проверяются отдельно.\n\n"
            + body_without_title_heading(rendered, title)
        )
        records.append(save_record(record_path(rel), data, body))
        manifest_pages.append({
            "source_id": sid, "title": title, "source_url": expected,
            "source_html_sha256": source_hash, "html_characters": len(source["doc_html"]),
            "record_id": rid, "md_path": rel, "linked_items": len(links),
        })
    return records, manifest_pages


def topics_for_title(title: str) -> list[str]:
    return [topic for topic, pattern in TOPIC_PATTERNS.items()
            if re.search(pattern, title, re.IGNORECASE)]


def make_77_index(raw: Path, observed: str) -> tuple[dict, dict]:
    source = read_json(raw / "indexes/metod77-accounting-edition-4-5.json")
    items = source.get("items", [])
    if source.get("metadata_only") is not True or len(items) != 102:
        raise ValueError("Expected the complete 102-item 7.7 Accounting edition 4.5 index")
    metod77_observations = {url: title for url, title in RESTRICTED_OBSERVATIONS.items()
                            if url.startswith("https://its.1c.ru/db/metod77/")}
    source_hash = canonical_sha({"index": source, "restricted_observations": metod77_observations})
    rid = "onec-77-accounting-edition-4-5-index"
    rel = "knowledge/records/1c-7.7/accounting-edition-4-5/index.md"
    gated = {
        title: "restricted-observed"
        for url, title in RESTRICTED_OBSERVATIONS.items()
        if url.startswith("https://its.1c.ru/db/metod77/")
    }
    links = []
    by_topic: dict[str, list[int]] = {topic: [] for topic in TOPIC_PATTERNS}
    for number, item in enumerate(items, 1):
        selected_topics = topics_for_title(item["title"])
        if not selected_topics:
            selected_topics = ["unclassified-from-title"]
        entry = {
            "entry_id": f"article-{number:03d}",
            "title": item["title"],
            "source_url": item["source_url"],
            "topic_hints": selected_topics,
            "body_status": gated.get(item["title"], "index-only-not-captured"),
        }
        links.append(entry)
        for topic in selected_topics:
            if topic in by_topic:
                by_topic[topic].append(number)
    data = record_base(
        rid, source["title"], rel, "source_manifest",
        "Каталог всех 102 видимых статей методической поддержки «Бухгалтерский учет», редакция 4.5, для поиска старых операций и проверки миграции. Сохранены названия и URL; тексты статей не подменены краткими описаниями.",
        [source["source_url"], "https://its.1c.ru/db/metod77"], observed, source_hash,
        {"source_id": source["source_id"], "source_version": source["platform_version"],
         "configuration_release": source["configuration_release"],
         "source_collection": "metod77-accounting-edition-4-5",
         "source_sha256": source_hash, "capture": "authenticated-ITS-visible-index",
         "rights_basis": "user-managed-local-access",
         "metadata_only": True},
        [{"id": "version", "heading": "Версия источника", "anchor": "версия-источника",
          "summary": "Версии платформы и конфигурации указаны в локальном входном файле; индекс не доказывает их применимость.",
          "keywords": ["платформа", "конфигурация", "редакция 4.5"]},
         {"id": "topics", "heading": "Поиск по рабочим темам", "anchor": "поиск-по-рабочим-темам",
          "summary": "Подсказки строятся только по названиям статей; это не проверка содержимого или применимости к доработанной базе.",
          "keywords": ["банк", "касса", "зарплата", "закупки", "услуги", "расчеты"]},
         {"id": "access", "heading": "Текущий статус текстов", "anchor": "текущий-статус-текстов",
          "summary": "Индекс содержит названия и ссылки. Наличие текста и право доступа проверяются отдельно.",
          "keywords": ["ограничение доступа", "права", "статьи"]}],
        [],
        ["1С:Предприятие 7.7", "Бухгалтерский учет", "редакция 4.5"],
    )
    data["source_links"] = links
    lines = [
        f"# {source['title']}", "",
        f"Источник: [{source['source_url']}]({source['source_url']}) · Каталог открыт в авторизованном ИТС · Получено {observed}.",
        "",
        "## Версия источника",
        "",
        f"Заявленные во входном файле версии: платформа {source['platform_version']}; конфигурация {source['configuration_release']}. Индекс не доказывает применимость к конкретной базе.",
        "",
        "## Поиск по рабочим темам",
        "",
        "Подборка строится только по заголовкам. Открыв её, проверяйте организацию учета и доработки по фактической базе; старые статьи могут описывать российские налоги и типовую редакцию 4.5.",
        "",
    ]
    for topic, article_numbers in by_topic.items():
        if not article_numbers:
            continue
        lines += [f"### {topic}", ""]
        for number in article_numbers:
            item = items[number - 1]
            lines.append(f"- [{common.escape(item['title'])}](#article-{number:03d})")
        lines.append("")
    lines += [
        "## Доступ к текстам",
        "",
        "Сохранены только названия и ссылки. Пользователь проверяет доступ своей учетной записи к каждой статье отдельно. Наличие ссылки не означает наличие подписки или доступность полного текста.",
        "",
        "## Все материалы каталога (102)",
        "",
    ]
    for number, item in enumerate(items, 1):
        topics = topics_for_title(item["title"]) or ["не классифицировано по заголовку"]
        access = ("доступ ограничен — проверено" if item["title"] in gated
                  else "текст не захвачен")
        lines += [
            f'<a id="article-{number:03d}"></a>',
            f"{number}. [{common.escape(item['title'])}]({item['source_url']}) — темы: {', '.join(topics)}; текст: {access}.",
        ]
    body = "\n".join(lines)
    return save_record(record_path(rel), data, body), {
        "source_id": source["source_id"], "title": source["title"],
        "source_url": source["source_url"], "configuration_release": source["configuration_release"],
        "platform_version": source["platform_version"], "item_count": len(items),
        "metadata_only": True, "items": links,
    }


def make_operations_overview(operation_records: list[dict], observed: str,
                             index_manifest: list[dict]) -> dict:
    rid = "onec-its-accounting-operations-overview"
    rel = "knowledge/records/1c-8.3.25/operation-guides/overview.md"
    hoosn_observations = {url: title for url, title in RESTRICTED_OBSERVATIONS.items()
                          if url.startswith(HOOSN_ROOT + "/")}
    source_hash = canonical_sha({"index_manifest": index_manifest,
                                 "restricted_observations": hoosn_observations})
    groups = [
        ("Услуги", [906, 404], "Выполнение работ и оказание услуг; пакетное выставление документов нескольким контрагентам."),
        ("Банк", [88, 89, 92, 220], "Поступления и списания с расчетного счета, авансы покупателей и поставщикам, банковская выписка."),
        ("Касса", [97, 98, 223], "Кассовые операции, поступления в кассу, кассовая книга."),
        ("Закупки", [38, 41, 57, 60], "Поступления материалов и товаров, предоплата, постоплата, доставка."),
        ("Расчеты с контрагентами", [105, 404], "Договоры, счета покупателям и поставщикам, акты сверки, расчеты и авансы."),
        ("Зарплата", [], "Для старой базы см. индекс методической поддержки 7.7 и закрытые заголовки по зарплате; в сохраненных общих операционных указателях отдельной процедуры расчета зарплаты нет."),
    ]
    pages_by_sid = {int(p["source_id"]): p for p in index_manifest}
    relations = [{"type": "has_page", "target": item["record_id"]} for item in operation_records]
    relations.append({"type": "see_also", "target": "onec-77-accounting-edition-4-5-index"})
    data = record_base(
        rid, "Навигация по операциям для переноса 1С",
        rel, "source_manifest",
        "Карта входа в документацию по услугам, банку, кассе, закупкам, расчетам с контрагентами и зарплате для планируемого переноса.",
        ["https://its.1c.ru/db/hoosn", "https://its.1c.ru/db/metod77"], observed,
        source_hash,
        {"source_version": "platform-related indexes; exact applied configuration varies",
         "source_sha256": source_hash, "capture": "authenticated-ITS-visible-indexes",
         "rights_basis": "user-managed-local-access"},
        [{"id": "scope", "heading": "Рабочие темы", "anchor": "рабочие-темы",
          "summary": "Тематические указатели сгруппированы по общим участкам учета.",
          "keywords": ["услуги", "банк", "касса", "закупки", "контрагенты", "зарплата"]},
         {"id": "limits", "heading": "Ограничения применимости", "anchor": "ограничения-применимости",
          "summary": "Указатели хозяйственных операций не являются руководством к точной редакции целевой конфигурации.",
          "keywords": ["версия", "конфигурация", "проверка"]}],
        relations, ["перенос данных", "операции", "навигация"],
    )
    md_dir = Path(rel).parent
    lines = [
        "# Навигация по операциям для переноса 1С", "",
        f"Снимок справочника: [Учет хозяйственных операций (СХО)](https://its.1c.ru/db/hoosn) · Получено {observed}.",
        "",
        "Эта карта ведет к названиям и отдельным инструкциям в ИТС. Она не утверждает, что интерфейс и проводки совпадают с облачной «Бухгалтерией для Беларуси» 2.1. Используйте ее для поиска нужного участка, затем проверьте версию материала и форму в тестовой базе.",
        "",
    ]
    for heading, sids, description in groups:
        lines += [f"## {heading}", "", description, ""]
        if sids:
            for sid in sids:
                entry = pages_by_sid[sid]
                lines.append(f"- [{entry['title']}]({Path(entry['md_path']).name}) ([ИТС]({entry['source_url']}))")
                if sid in {906, 404}:
                    page = next(x for x in operation_records if x["record_id"] == entry["record_id"])
                    for link in page.get("source_links", []):
                        if ("услуг" in link.get("label", "").lower() or
                            "сверки" in link.get("label", "").lower() or
                            "счет на оплату" in link.get("label", "").lower()):
                            lines.append(f"  - [Связанная статья: {link['label']}]({link['href']})")
        else:
            lines.append("- [Индекс статей по редакции 4.5 для 1С 7.7](../../../../knowledge/records/1c-7.7/accounting-edition-4-5/index.md)")
        lines.append("")
    lines += [
        "## Сохраненные указатели",
        "",
        "Локальный корпус содержит переданные навигационные страницы и их ссылки. Содержимое связанных статей не считается захваченным. Проверка доступа выполняется для текущего пользователя отдельно.",
        "",
        "Для конкретной версии 8 сначала сверяйте окно «О программе» и затем ищите точную редакцию прикладной конфигурации. Для старой 7.7 не переносите без проверки счета, названия разделов и налоговые примеры из типовой или российской версии.",
    ]
    return save_record(record_path(rel), data, "\n".join(lines))


def update_catalog_graph(records: list[dict], observed: str, changed: bool) -> dict:
    new_entries = [
        {key: data[key] for key in ("record_id", "pair_revision", "kind", "status", "md_path")}
        | {"json_path": data["md_path"].removesuffix(".md") + ".json"}
        for data in records
    ]
    catalog_path = ROOT / "knowledge/catalog.json"
    catalog = read_json(catalog_path)
    owned_prefixes = (
        "knowledge/records/1c-8.3.25/platform-manuals/",
        "knowledge/records/1c-8.3.25/operation-guides/",
        "knowledge/records/1c-7.7/",
    )
    catalog["records"] = [item for item in catalog["records"]
                          if not item["md_path"].startswith(owned_prefixes)]
    catalog["records"] = [*catalog["records"], *new_entries]
    catalog["records"].sort(key=lambda item: item["record_id"])
    catalog["pair_revision"] += int(changed)
    catalog["updated_at"] = observed
    catalog["summary"] = (
        f"Реестр пар Markdown и JSON; добавлены руководства 1С 8.3.25, "
        f"указатели хозяйственных операций, каталог 7.7."
    )
    catalog["source_refs"] = list(dict.fromkeys([*catalog["source_refs"], ISSUE]))
    graph_entry = next(item for item in catalog["records"] if item["record_id"] == "onec-knowledge-graph")
    graph_entry["pair_revision"] += int(changed)
    catalog_entry = next(item for item in catalog["records"] if item["record_id"] == "onec-knowledge-catalog")
    catalog_entry["pair_revision"] = catalog["pair_revision"]
    write_json(catalog_path, catalog)

    graph_path = ROOT / "knowledge/graph.json"
    graph = read_json(graph_path)
    new_ids = {item["record_id"] for item in new_entries}
    custom_nodes = {f"topic:{topic}" for topic in TOPIC_PATTERNS}
    owned_record_prefixes = ("record:onec-8325-platform-", "record:onec-its-hoosn-", "record:onec-its-accounting-operations-", "record:onec-77-")
    old_owned_nodes = {n["id"] for n in graph["nodes"] if n["id"].startswith(owned_record_prefixes)} | custom_nodes
    graph["nodes"] = [node for node in graph["nodes"] if node["id"] not in old_owned_nodes]
    graph["edges"] = [edge for edge in graph["edges"]
                      if edge["from"] not in old_owned_nodes and edge["to"] not in old_owned_nodes]
    graph["pair_revision"] += int(changed)
    graph["updated_at"] = observed
    graph["summary"] = "Типизированный граф знаний 1С связывает источники, версии, инструкции, рабочие темы и темы документации."
    graph["source_refs"] = list(dict.fromkeys([*graph["source_refs"], ISSUE]))
    graph["sections"] = [
        {"id": "role", "heading": "Роль", "anchor": "роль",
         "summary": "Граф служит навигацией по записям, версиям и темам; он не подтверждает состояние базы.",
         "keywords": ["navigation", "authority"]},
        {"id": "relations", "heading": "Связи", "anchor": "связи",
         "summary": "Типизированные связи соединяют записи, источники, темы и версии.",
         "keywords": ["edges", "relations"]},
        {"id": "hyperedges", "heading": "Многоместные связи", "anchor": "многоместные-связи",
         "summary": "Многоместные связи задаются только собственным корпусом пользователя.",
         "keywords": ["hyperedges", "migration"]},
    ]
    for data in records:
        graph["nodes"].append({
            "id": "record:" + data["record_id"], "type": "record",
            "status": data["status"], "label": data["title"],
        })
    topic_labels = {
        "bank": "Банковские операции",
        "cash": "Кассовые операции",
        "payroll": "Зарплата",
        "purchases": "Закупки и поступления",
        "services": "Услуги и работы",
        "counterparty-settlements": "Расчеты с контрагентами",
    }
    for topic, label in topic_labels.items():
        graph["nodes"].append({"id": "topic:" + topic, "type": "work_topic", "status": "declared", "label": label})
    all_record_ids = {item["record_id"] for item in catalog["records"]}
    for data in records:
        source_node = "record:" + data["record_id"]
        graph["edges"].append({
            "id": "edge:repo-contains-" + data["record_id"],
            "type": "contains_record", "status": "verified",
            "from": "corpus:local", "to": source_node,
            "meaning": "Парная Markdown/JSON запись зарегистрирована в каталоге репозитория.",
        })
        for relation in data.get("relations", []):
            target = relation["target"]
            if target in all_record_ids:
                target = "record:" + target
            if target not in {node["id"] for node in graph["nodes"]}:
                continue
            graph["edges"].append({
                "id": f"edge:{data['record_id']}:{relation['type']}:{target}",
                "type": relation["type"], "status": "declared",
                "from": source_node, "to": target,
                "meaning": "Связь указана в исходном документе.",
            })
    # Link operation-index pages to the explicitly requested work topics by their titles.
    topic_record_map = {
        "bank": ["88", "89", "92", "220"],
        "cash": ["97", "98", "223"],
        "payroll": [],
        "purchases": ["38", "41", "57", "60"],
        "services": ["906", "404"],
        "counterparty-settlements": ["105", "404"],
    }
    for topic, source_ids in topic_record_map.items():
        for sid in source_ids:
            rid = f"onec-its-hoosn-index-{int(sid):04d}"
            graph["edges"].append({
                "id": f"edge:{rid}:covers-topic:{topic}",
                "type": "covers_topic", "status": "declared",
                "from": "record:" + rid, "to": "topic:" + topic,
                "meaning": "Название навигационного раздела относится к указанной рабочей теме.",
            })
    write_json(graph_path, graph)

    cat_lines = [
        f"<!-- 1c-record record_id: onec-knowledge-catalog pair_revision: {catalog['pair_revision']} json_path: knowledge/catalog.json -->",
        "# Каталог долговечных записей", "",
        "Машинный реестр: [catalog.json](catalog.json).", "",
        "## Текущие записи", "",
        "| ID | Вид | Статус | Markdown |", "|---|---|---|---|",
    ]
    cat_lines.extend(
        f"| {item['record_id']} | {item['kind']} | {item['status']} | {item['md_path']} |"
        for item in catalog["records"]
    )
    cat_lines += ["", "## Правило обновления", "", "Каждая пара меняется вместе с каталогом и графом в одном PR."]
    (ROOT / "knowledge/catalog.md").write_text("\n".join(cat_lines) + "\n", encoding="utf-8")
    counts = Counter(edge["type"] for edge in graph["edges"])
    graph_lines = [
        f"<!-- 1c-record record_id: onec-knowledge-graph pair_revision: {graph['pair_revision']} json_path: knowledge/graph.json -->",
        "# Типизированный граф знаний 1С", "",
        "Машинное представление: [graph.json](graph.json).", "",
        "## Роль", "",
        "Граф служит навигацией по записям, версиям и темам и не подтверждает состояние рабочей базы.", "",
        "## Связи", "",
    ]
    graph_lines.extend(f"- {key}: {value}" for key, value in sorted(counts.items()))
    graph_lines += ["", "## Многоместные связи", "",
                    "Многоместные связи задаются в собственном локальном корпусе пользователя."]
    (ROOT / "knowledge/graph.md").write_text("\n".join(graph_lines) + "\n", encoding="utf-8")
    return catalog


def clean_stale_outputs(records: list[dict]) -> None:
    expected = {item["md_path"] for item in records}
    expected |= {item["md_path"].removesuffix(".md") + ".json" for item in records}
    directories = [
        ROOT / "knowledge/records/1c-8.3.25/platform-manuals",
        ROOT / "knowledge/records/1c-8.3.25/operation-guides",
        ROOT / "knowledge/records/1c-7.7",
    ]
    for directory in directories:
        if not directory.exists():
            continue
        for child in directory.rglob("*"):
            if child.is_file() and child.suffix in {".md", ".json"}:
                if child.relative_to(ROOT).as_posix() not in expected:
                    child.unlink()


def build(raw: Path, observed: str) -> dict:
    # Load existing pair revisions before replacing this import's owned outputs.
    toc = read_json(raw / "indexes/platform-toc.json")
    page_id_by_sid = {}
    for book in toc["books"]:
        for page in book["pages"]:
            page_id_by_sid[int(page["source_id"])] = (
                f"onec-8325-platform-{book['key']}-page-{int(page['source_id']):03d}"
            )
    if len(page_id_by_sid) != 70:
        raise ValueError("The platform TOC does not describe exactly 70 distinct source pages")
    old_manifest_path = ROOT / "sources/1c-additional/manifest.json"
    old_manifest = read_json(old_manifest_path) if old_manifest_path.exists() else {}
    prior_catalog = read_json(ROOT / "knowledge/catalog.json")
    prior_revisions = {item["record_id"]: item["pair_revision"] for item in prior_catalog["records"]}
    input_hashes = {}
    for book in toc["books"]:
        for page in book["pages"]:
            sid = int(page["source_id"])
            source = read_json(raw / "platform" / book["key"] / f"{sid}.json")
            input_hashes[f"platform/{book['key']}/{sid}"] = sha(source["doc_html"])
    for sid in HOOSN_IDS:
        source = read_json(raw / "hoosn-index" / f"{sid}.json")
        input_hashes[f"hoosn-index/{sid}"] = sha(source["doc_html"])
    metod = read_json(raw / "indexes/metod77-accounting-edition-4-5.json")
    input_hashes["metod77-accounting-edition-4-5"] = canonical_sha(metod)
    input_hashes["access-observations"] = canonical_sha(RESTRICTED_OBSERVATIONS)
    input_hashes["configuration-search-review"] = canonical_sha(CONFIGURATION_SEARCH_REVIEW)
    changed = old_manifest.get("input_sha256") != input_hashes
    platform_records, by_book, platform_manifest = make_platform_records(raw, observed, page_id_by_sid)
    book_records = make_book_overviews(by_book, platform_manifest, observed, raw)
    operation_records, operation_manifest = make_operation_records(raw, observed)
    seven_record, seven_manifest = make_77_index(raw, observed)
    operation_overview = make_operations_overview(operation_records, observed, operation_manifest)
    all_new_records = [
        *platform_records, *book_records, *operation_records, seven_record,
        operation_overview,
    ]
    clean_stale_outputs(all_new_records)
    changed = changed or any(prior_revisions.get(item["record_id"]) != item["pair_revision"]
                             for item in all_new_records)
    catalog = update_catalog_graph(all_new_records, observed, changed)
    manifest = {
        "schema_version": 1,
        "corpus": "1c-its-additional-documentation",
        "observed_at": observed,
        "issue": ISSUE,
        "rights_basis": "user-managed-local-access",
        "capture_method": "authenticated-Chrome-article-frame-and-visible-index",
        "input_sha256": input_hashes,
        "platform_manuals": platform_manifest,
        "operation_indexes": {
            "source_root": HOOSN_ROOT,
            "configuration_applicability": "general accounting-operation examples; exact applicability to Belarus edition 2.1 not verified",
            "source_page_count": len(operation_manifest),
            "pages": operation_manifest,
        },
        "configuration_search": CONFIGURATION_SEARCH_REVIEW,
        "accounting_77_index": seven_manifest,
        "excluded": [
            "Browser shell, account profile, credentials, cookies and personal data.",
            "Database files, backups, real accounting records, licenses and screenshots.",
            "Industries outside services, bank, cash, payroll, purchases and counterparty settlements.",
            "Bodies of articles that were not available in the authenticated portal session.",
        ],
        "record_count": len(all_new_records),
        "platform_page_count": sum(x["source_page_count"] for x in platform_manifest),
        "operation_index_page_count": len(operation_manifest),
        "accounting_77_article_index_count": len(seven_manifest["items"]),
    }
    write_json(old_manifest_path, manifest)
    return {
        "platform_pages": manifest["platform_page_count"],
        "platform_records_including_overviews": len(platform_records) + len(book_records),
        "operation_index_pages": len(operation_records),
        "7_7_index_items": len(seven_manifest["items"]),
        "migration_plan_records": 0,
        "new_record_pairs": len(all_new_records),
        "registered_total": len(catalog["records"]),
        "input_changed": changed,
    }



if __name__ == "__main__":
    raise SystemExit("Use collect.py with an explicit --output directory")

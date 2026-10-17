import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_1c_docs.py"
spec = importlib.util.spec_from_file_location("build_1c_docs", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load script at {SCRIPT}")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class Build1CDocsTests(unittest.TestCase):
    def test_article_only_and_structured_content(self) -> None:
        article = '''<html><head><title>Account holder</title><style>secret</style></head>
        <body><script>cookieValue</script><a id="TI01"><h1>Глава 1. Пример</h1></a>
        <p>Первый <b>текст</b> <a href="/db/v8325doc/content/src/руководство разработчика/глава 2. тема.htm_">глава 2</a>.</p>
        <p><a href="#TI01">Текущая страница</a></p><a class="copy_source" href="#copy">Копировать в буфер обмена</a>
        <pre>ВЫБРАТЬ *\nИЗ Таблица</pre><table><tr><th>Имя</th><th>Тип</th></tr><tr><td>A</td><td>Число</td></tr></table>
        <p><img src="_img/example.gif" alt="Схема"></p></body></html>'''
        parser = builder.ArticleParser()
        parser.feed(article)
        self.assertEqual(builder.unrepresented_text(parser.root), [])
        rendered = [builder.block_markdown(node, "https://its.1c.ru/db/v8325doc/content/52/hdoc") for node in builder.blocks(parser.root)]
        text = "\n".join(rendered)
        self.assertIn('<a id="TI01"></a>\n# Глава 1. Пример', text)
        self.assertIn("**текст**", text)
        self.assertIn("%D1%80%D1%83%D0%BA%D0%BE%D0%B2%D0%BE%D0%B4%D1%81%D1%82%D0%B2%D0%BE", text)
        self.assertNotIn("глава 2. тема.htm_", text)
        self.assertIn("```\nВЫБРАТЬ *\nИЗ Таблица\n```", text)
        self.assertIn("| Имя | Тип |", text)
        self.assertIn("https://its.1c.ru/db/v8325doc/content/52/_img/example.gif", text)
        self.assertNotIn("Account holder", text)
        self.assertNotIn("cookieValue", text)
        links, counts = builder.links(parser.root, "https://its.1c.ru/db/v8325doc/content/52/hdoc", {2: "onec-8325-developer-chapter-02"})
        self.assertEqual(counts["internal"], 1)
        self.assertEqual(counts["same_page"], 1)
        self.assertEqual(counts["site_unmapped"], 0)
        self.assertEqual(counts["external"], 0)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["record_id"], "onec-8325-developer-chapter-02")

    def test_large_chapter_splits_at_heading_boundary(self) -> None:
        root = builder.Node("root")
        items = []
        for i in range(3):
            heading = builder.Node("h2", parent=root)
            heading.children.append(f"Раздел {i}")
            para = builder.Node("p", parent=root)
            para.children.append("А" * 95_000)
            items.extend([(heading, "## Раздел " + str(i)), (para, "А" * 95_000)])
        groups = builder.group_items(items, "Глава 1")
        self.assertGreater(len(groups), 1)
        self.assertTrue(all(group[0][0].tag == "h2" for group in groups))
        self.assertTrue(all(len("\n\n".join(t for _, t in group).encode("utf-8")) <= builder.TARGET_MD_BYTES for group in groups))


if __name__ == "__main__":
    unittest.main()

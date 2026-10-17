import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import build_1c_docs as common
import build_1c_additions as additions


class ContentPreservationTests(unittest.TestCase):
    def test_all_renderers_preserve_merged_table_cells_and_strip_attributes(self):
        parser = common.ArticleParser()
        parser.feed('<html><body><table onclick="bad()"><tr><td rowspan="2">A</td><td colspan="2">B</td></tr><tr><td>C</td><td>D</td></tr></table></body></html>')
        table = common.blocks(parser.root)[0]
        rendered = common.block_markdown(table, 'https://its.1c.ru/db/test')
        self.assertIn('rowspan="2"', rendered)
        self.assertIn('colspan="2"', rendered)
        self.assertNotIn('onclick', rendered)

    def test_additional_refuses_unrepresented_text_instead_of_truncating(self):
        url = 'https://its.1c.ru/db/hoosn/content/38/hdoc'
        html = '<html><body><h1>Title</h1><div>' + 'Long unrepresented content. ' * 20 + '</div></body></html>'
        source = dict(source_id=38, source_url=url, doc_html=html, html_length=len(html))
        with self.assertRaisesRegex(ValueError, 'Unrepresented'):
            additions.parse_article(source, url, allow_unrepresented=True)

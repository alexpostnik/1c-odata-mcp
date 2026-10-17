import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { URL } from "node:url";
import { parseHTML } from "linkedom";
import { describe, expect, it } from "vitest";

const script = readFileSync(new URL("../docs/toolkit/examples/capture-article.js", import.meta.url), "utf8");
function browser(html, href = "https://its.1c.ru/db/v8325doc/content/51/hdoc") {
  const { document, Node } = parseHTML(html);
  document.implementation = {
    createHTMLDocument: () => parseHTML("<html><head></head><body></body></html>").document,
  };
  const context = { document, Node, URL, location: { href } };
  runInNewContext(script, context);
  return context.captureArticle;
}

describe("article-only browser capture", () => {
  it("retains Unicode and table spans while excluding shell, scripts, forms and authenticated URLs", () => {
    const capture =
      browser(`<html><body><header>PRIVATE PROFILE</header><main id="article"><h1>Учебный пример 🧪</h1>
      <p>Вымышленный текст.</p><script>PRIVATE SCRIPT</script><form>PRIVATE FORM</form>
      <table><tr><td colspan="2">Ячейка</td></tr></table>
      <a href="/db/v8325doc/content/52/hdoc">Следующая</a>
      <a href="https://its.1c.ru/db/v8325doc/content/53/hdoc?token=SECRET">Ссылка</a>
      <img src="https://its.1c.ru/private?token=SECRET" alt="Схема">
      </main></body></html>`);
    const data = capture({ articleSelector: "#article", confirmArticleOnly: true });
    expect(data.source_id).toBe("51");
    expect(data.title).toBe("Учебный пример 🧪");
    expect(data.html_length).toBe(Array.from(data.doc_html).length);
    expect(data.doc_html).toContain('colspan="2"');
    expect(data.doc_html).toContain("https://its.1c.ru/db/v8325doc/content/52/hdoc");
    expect(data.doc_html).not.toMatch(/PRIVATE|SECRET|token=|<script|<form|src=/);
  });
  it("requires an inspected selector and refuses login pages, missing headings and noncanonical URLs", () => {
    const capture = browser('<html><body><main id="article"><h1>Example</h1></main></body></html>');
    expect(() => capture({ articleSelector: "#article" })).toThrow(/Inspect/);
    expect(() => capture({ articleSelector: "#absent", confirmArticleOnly: true })).toThrow(/not found/);
    for (const href of [
      "https://login.1c.ru/",
      "https://its.1c.ru/db/v8325doc/content/51/hdoc?session=PRIVATE",
    ]) {
      expect(() =>
        browser(
          "<html><body><h1>Example</h1></body></html>",
          href,
        )({ articleSelector: "body", confirmArticleOnly: true }),
      ).toThrow(/canonical/);
    }
    expect(() =>
      browser('<html><body><h1>Example</h1><input type="password"></body></html>')({
        articleSelector: "body",
        confirmArticleOnly: true,
      }),
    ).toThrow(/Login/);
    expect(() =>
      browser("<html><body><p>No heading</p></body></html>")({
        articleSelector: "body",
        confirmArticleOnly: true,
      }),
    ).toThrow(/heading/);
  });
});

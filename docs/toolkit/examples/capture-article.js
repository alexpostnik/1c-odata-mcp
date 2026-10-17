/* global location, document, Node, URL */
/* Run in the already open article frame through your browser connector.
 * This function performs no network requests and never reads browser storage.
 * Inspect the selected element first: the selector must exclude the portal shell.
 */
globalThis.captureArticle = function captureArticle({ articleSelector, collection, confirmArticleOnly = false }) {
  if (!confirmArticleOnly || typeof articleSelector !== 'string' || !articleSelector) {
    throw new Error('Inspect an article-only element and explicitly select it');
  }
  const canonical = new URL(location.href);
  if (canonical.origin !== 'https://its.1c.ru' ||
      !/^\/db\/[A-Za-z0-9_-]+\/content\/\d+\/hdoc\/?$/.test(canonical.pathname) ||
      canonical.username || canonical.password || canonical.search) {
    throw new Error('Open the canonical article frame, without session parameters');
  }
  canonical.hash = '';
  const sourceId = canonical.pathname.match(/\/content\/(\d+)\//)[1];
  const source = document.querySelector(articleSelector);
  if (!source) throw new Error('Article element was not found in this frame');
  if (source.querySelector('input[type="password"], form[action*="login"]')) {
    throw new Error('Login form detected; user must authenticate through the portal');
  }
  const tags = new Set('h1 h2 h3 h4 h5 h6 p div span section article ul ol li dl dt dd pre code blockquote a br hr table caption thead tbody tfoot tr th td strong em b i u s sub sup img'.split(' '));
  const discard = new Set('SCRIPT STYLE FORM INPUT BUTTON IFRAME OBJECT EMBED NAV HEADER FOOTER ASIDE NOSCRIPT'.split(' '));
  const result = document.implementation.createHTMLDocument('');
  const cleanUrl = (value) => {
    if (!value) return null;
    try {
      const url = new URL(value, canonical);
      if (url.protocol !== 'https:' || url.username || url.password || url.search ||
          !['its.1c.ru', 'po.by'].includes(url.hostname)) return null;
      return url.href;
    } catch { return null; }
  };
  const copy = (node, target) => {
    if (node.nodeType === Node.TEXT_NODE) {
      target.appendChild(result.createTextNode(node.nodeValue)); return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || discard.has(node.tagName)) return;
    const tag = node.tagName.toLowerCase();
    const child = tags.has(tag) ? result.createElement(tag) : target;
    if (child !== target) {
      for (const name of ['id', 'name', 'colspan', 'rowspan']) {
        const value = node.getAttribute(name);
        if (value && /^[\p{L}\p{N}_.:-]{1,120}$/u.test(value)) child.setAttribute(name, value);
      }
      if (tag === 'a') {
        const href = cleanUrl(node.getAttribute('href'));
        if (href) child.setAttribute('href', href);
      }
      // Keep image descriptions; do not copy authenticated resource URLs.
      if (tag === 'img') child.setAttribute('alt', node.getAttribute('alt') || '[image omitted]');
      target.appendChild(child);
    }
    for (const nested of node.childNodes) copy(nested, child);
  };
  copy(source, result.body);
  const heading = result.body.querySelector('h1,h2,h3,h4,h5,h6');
  if (!heading || !heading.textContent.trim()) throw new Error('No article heading; check the selected frame');
  const docHtml = result.documentElement.outerHTML;
  if (/login\.1c\.ru|(?:authorization|set-cookie):|sessionid=|jsessionid=/i.test(docHtml)) {
    throw new Error('Authentication material detected; do not save this capture');
  }
  return {
    source_id: sourceId,
    source_url: canonical.href,
    title: heading.textContent.replace(/\s+/g, ' ').trim(),
    ...(collection ? { collection } : {}),
    doc_html: docHtml,
    // Python len(str) counts Unicode code points, not JavaScript UTF-16 units.
    html_length: Array.from(docHtml).length,
  };
};

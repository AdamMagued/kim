"""
Markdown-reconstruction JS for the browser provider's response scrape.

``inner_text()`` renders code blocks as plain <pre> boxes, DROPPING the
```lang fences the codex-bridge salvage depends on.  This script rebuilds
markdown from a response element, restoring ```lang fences around <pre>
code blocks (language from the <code> class or the block header).  It
iterates the top-level children (ChatGPT/Gemini/Claude render <pre> as
siblings of <p>/<h*>, not nested inside them) so prose keeps its innerText
spacing.

The fence-reconstruction contract this encodes is covered by
``tests/test_browser_dom_contract.py`` against recorded DOM fixtures in
``tests/fixtures/dom/``.
"""

MARKDOWN_SERIALIZER_JS = r"""
    (el) => {
      const kids = el.children;
      if (!kids || kids.length === 0) return el.innerText || '';
      const parts = [];
      for (const child of kids) {
        if (child.tagName === 'PRE') {
          const code = child.querySelector('code');
          let lang = '';
          if (code && code.className) {
            const m = code.className.match(/language-([\w+#.-]+)/);
            if (m) lang = m[1];
          }
          if (!lang) {
            const hdr = child.querySelector('div');
            if (hdr) {
              const first = (hdr.innerText || '').trim().split('\n')[0].trim().toLowerCase();
              if (/^[a-z0-9+#.-]{1,15}$/.test(first)) lang = first;
            }
          }
          // innerText preserves rendered line breaks; textContent concatenates
          // ChatGPT's per-line highlight <span>/<div>s with NO newline between
          // them, flattening multi-line code to a single line (breaks any file
          // whose newlines matter — Python, YAML, Makefiles).
          const codeText = (code ? (code.innerText || code.textContent) : child.innerText) || '';
          parts.push('```' + lang + '\n' + codeText.replace(/\n+$/, '') + '\n```');
        } else {
          parts.push(child.innerText || '');
        }
      }
      const joined = parts.join('\n\n');
      return joined.trim() ? joined : (el.innerText || '');
    }
    """

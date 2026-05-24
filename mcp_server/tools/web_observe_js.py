"""
JavaScript blob injected into the live page by web_observe().

Extracted from mcp_server/tools/web.py to keep that file focused on
Python logic. Imported back as:
    from mcp_server.tools.web_observe_js import _OBSERVE_JS
"""

# JS that walks the live DOM and returns interactive elements with stable
# selectors and bounding boxes. Runs entirely in the page so we see exactly
# what the user sees.
_OBSERVE_JS = r"""
() => {
  const elementState = el => {
    const r = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    const s = el ? window.getComputedStyle(el) : null;
    const hidden = !el || !r || r.width <= 1 || r.height <= 1 ||
      !s || s.visibility === 'hidden' || s.display === 'none' ||
      parseFloat(s.opacity || '1') < 0.05;
    const visible = !hidden;
    const inViewport = visible &&
      r.bottom >= 0 && r.top <= window.innerHeight &&
      r.right >= 0 && r.left <= window.innerWidth;
    return {
      hidden,
      visible,
      in_viewport: inViewport,
      bbox: r ? [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)] : [0, 0, 0, 0],
    };
  };

  const cleanText = value => (value || '').replace(/\s+/g, ' ').trim();

  const labelOf = el => {
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return al.trim();
    if (el.placeholder) return ('placeholder: ' + el.placeholder).trim();
    if (el.title) return el.title.trim();
    if (el.alt) return el.alt.trim();
    if (el.value && el.tagName === 'INPUT' && (el.type === 'submit' || el.type === 'button')) return el.value.trim();
    const lid = el.getAttribute && el.getAttribute('aria-labelledby');
    if (lid) {
      const labelled = lid.split(/\s+/).map(id => {
        const r = document.getElementById(id);
        return r ? cleanText(r.textContent) : '';
      }).filter(Boolean).join(' ');
      if (labelled) return labelled;
    }
    if (el.labels && el.labels.length) {
      const labels = Array.from(el.labels).map(l => cleanText(l.textContent)).filter(Boolean);
      if (labels.length) return labels.join(' ');
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab) return cleanText(lab.textContent);
    }
    const parentLabel = el.closest && el.closest('label');
    if (parentLabel) {
      const parentText = cleanText(parentLabel.textContent);
      if (parentText) return parentText;
    }
    const inner = cleanText(el.innerText || el.textContent || '');
    return inner.slice(0, 120);
  };

  const visibleTextOf = el => cleanText(el.innerText || el.textContent || '').slice(0, 180);

  const nearbyTextOf = el => {
    const parts = [];
    const previous = el.previousElementSibling;
    const next = el.nextElementSibling;
    if (previous) parts.push(cleanText(previous.innerText || previous.textContent || ''));
    if (next) parts.push(cleanText(next.innerText || next.textContent || ''));
    const parent = el.parentElement;
    if (parent) parts.push(cleanText(parent.innerText || parent.textContent || ''));
    return parts.filter(Boolean).join(' | ').slice(0, 260);
  };

  const containerOf = el => {
    const container = el.closest && el.closest(
      'form, main, [role="main"], section, article, dialog, [data-testid], [aria-label], .Box, .Layout-main'
    );
    return container || el.parentElement || document.body;
  };

  const cssPath = el => {
    if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
      return '#' + CSS.escape(el.id);
    }
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.body && parts.length < 6) {
      let sel = cur.tagName.toLowerCase();
      if (cur.id) {
        sel += '#' + CSS.escape(cur.id);
        parts.unshift(sel);
        break;
      }
      const parent = cur.parentNode;
      if (parent) {
        const sibs = Array.from(parent.children).filter(n => n.tagName === cur.tagName);
        if (sibs.length > 1) sel += `:nth-of-type(${sibs.indexOf(cur) + 1})`;
      }
      parts.unshift(sel);
      cur = cur.parentNode;
    }
    return parts.join(' > ');
  };

  const SEL = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[role="textbox"]', '[role="searchbox"]',
    '[role="checkbox"]', '[role="radio"]', '[role="combobox"]', '[role="menuitem"]',
    '[role="tab"]', '[contenteditable="true"]', '[contenteditable=""]',
    'summary', '[onclick]', '[tabindex]:not([tabindex="-1"])'
  ].join(',');

  const seen = new Set();
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue;
    seen.add(el);
    const state = elementState(el);
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || tag;
    const form = el.form || el.closest('form');
    const container = containerOf(el);
    const disabled = el.disabled === true || (el.getAttribute && el.getAttribute('aria-disabled') === 'true');
    const required = el.required === true || (el.getAttribute && el.getAttribute('aria-required') === 'true');
    const checked = el.checked === true ||
      (el.getAttribute && el.getAttribute('aria-checked') === 'true') ||
      (el.getAttribute && el.getAttribute('aria-pressed') === 'true') ||
      (el.getAttribute && el.getAttribute('aria-selected') === 'true');
    out.push({
      id: 'w' + (++i),
      tag,
      role,
      label: labelOf(el).slice(0, 140),
      text: visibleTextOf(el),
      aria_label: (el.getAttribute && (el.getAttribute('aria-label') || '')).slice(0, 140),
      placeholder: (el.placeholder || '').slice(0, 140),
      name: (el.getAttribute && (el.getAttribute('name') || '')).slice(0, 140),
      title: (el.getAttribute && (el.getAttribute('title') || '')).slice(0, 140),
      nearby_text: nearbyTextOf(el),
      value: (el.value || '').slice(0, 120),
      href: (tag === 'a' && el.href) ? el.href.slice(0, 120) : '',
      type: el.type || '',
      checked,
      disabled,
      required,
      visible: state.visible,
      hidden: state.hidden,
      in_viewport: state.in_viewport,
      form_id: form ? (form.id || form.getAttribute('name') || cssPath(form)) : '',
      container_id: container ? cssPath(container) : '',
      container_text: container ? cleanText(container.innerText || container.textContent || '').slice(0, 320) : '',
      bbox: state.bbox,
      selector: cssPath(el),
    });
    if (out.length >= 500) break;
  }
  return { url: location.href, title: document.title, elements: out };
}
"""

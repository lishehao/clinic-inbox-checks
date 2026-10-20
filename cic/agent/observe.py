"""Turn the current browser page into a text observation with numbered elements."""

_COLLECT_JS = r"""
() => {
  document.querySelectorAll('[data-cic-id]').forEach(el => el.removeAttribute('data-cic-id'));
  const nodes = Array.from(document.querySelectorAll('a[href], button, input, select, textarea'));
  const items = [];
  let n = 0;
  for (const el of nodes) {
    if (el.type === 'hidden') continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    n += 1;
    const id = 'e' + n;
    el.setAttribute('data-cic-id', id);
    let label = el.getAttribute('aria-label') || '';
    if (!label && el.labels && el.labels.length) label = el.labels[0].innerText;
    if (!label) label = el.innerText || el.value || el.placeholder || '';
    const item = {
      id: id,
      tag: el.tagName.toLowerCase(),
      type: (el.getAttribute('type') || '').toLowerCase(),
      label: label.replace(/\s+/g, ' ').trim().slice(0, 140),
    };
    if (el.tagName === 'INPUT' && (item.type === 'checkbox' || item.type === 'radio')) item.checked = el.checked;
    if (el.tagName === 'SELECT') item.options = Array.from(el.options).map(o => o.text.trim());
    if (el.tagName === 'TEXTAREA' || (el.tagName === 'INPUT' && item.type !== 'checkbox' && item.type !== 'radio')) {
      item.value = (el.value || '').slice(0, 400);
    }
    items.push(item);
  }
  return {
    url: location.pathname + location.search,
    title: document.title,
    text: document.body.innerText.slice(0, 6000),
    elements: items,
  };
}
"""


def observe(page):
    return page.evaluate(_COLLECT_JS)


def element_line(el):
    kind = {"a": "link", "button": "button", "textarea": "textarea", "select": "select"}.get(el["tag"], el["tag"])
    if el["tag"] == "input":
        kind = el.get("type") or "input"
    line = f'[{el["id"]}] {kind} "{el["label"]}"'
    if "checked" in el:
        line += " (checked)" if el["checked"] else " (unchecked)"
    if "options" in el:
        line += " options: " + " | ".join(el["options"])
    if "value" in el:
        line += f' value: "{el["value"]}"'
    return line


def render(obs):
    """The text the model sees for one page."""
    lines = [f"URL: {obs['url']}", f"TITLE: {obs['title']}", "", "PAGE TEXT:", obs["text"].strip(), "", "ELEMENTS:"]
    lines += [element_line(el) for el in obs["elements"]]
    return "\n".join(lines)


def find(obs, label_part=None, kind=None, exact=False):
    """First element whose label contains label_part (case-insensitive)."""
    for el in obs["elements"]:
        if kind and el["tag"] != kind and el.get("type") != kind:
            continue
        label = el["label"].lower()
        if label_part is None:
            return el
        target = label_part.lower()
        if (exact and label == target) or (not exact and target in label):
            return el
    return None

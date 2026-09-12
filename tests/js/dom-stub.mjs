/* A DOM small enough to exercise the viewer's layout maths in Node.
 *
 * It is deliberately not a browser: it models parent/child structure, class
 * names, styles and a settable `offsetHeight`, which is everything the
 * virtual scroller actually reads.
 */

/** One node in the fake tree. */
class StubElement {
  constructor(tag) {
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parent = null;
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.listeners = new Map();
    this._text = '';
    this._className = '';
    this._height = 0;
    this.classList = {
      add: (...names) => { for (const n of names) this._classes().add(n); this._sync(); },
      remove: (...names) => { for (const n of names) this._classes().delete(n); this._sync(); },
      toggle: (name, on) => {
        const set = this._classes();
        const next = on === undefined ? !set.has(name) : Boolean(on);
        if (next) set.add(name); else set.delete(name);
        this._sync();
        return next;
      },
      contains: (name) => this._classes().has(name),
    };
  }

  _classes() {
    if (!this._classSet) this._classSet = new Set(this._className.split(/\s+/).filter(Boolean));
    return this._classSet;
  }

  _sync() { this._className = [...this._classes()].join(' '); }

  get className() { return this._className; }
  set className(value) { this._className = String(value); this._classSet = null; }

  get textContent() {
    return this.children.length
      ? this.children.map((c) => c.textContent).join(' ')
      : this._text;
  }
  set textContent(value) { this._text = String(value); this.children = []; }

  set innerHTML(value) { this._html = String(value); this._text = String(value); }
  get innerHTML() { return this._html || ''; }

  /** Height the viewer will read; tests set this to model real layout. */
  set offsetHeight(value) { this._height = value; }
  get offsetHeight() { return this._height; }

  get isConnected() {
    let node = this;
    while (node.parent) node = node.parent;
    return node.isRoot === true;
  }

  get firstElementChild() { return this.children[0] || null; }

  append(...nodes) {
    for (const node of nodes) {
      if (node === null || node === undefined) continue;
      if (node.isFragment) { this.append(...node.children.slice()); continue; }
      if (node.parent) node.parent.children = node.parent.children.filter((c) => c !== node);
      node.parent = this;
      this.children.push(node);
    }
  }

  prepend(node) { node.parent = this; this.children.unshift(node); }

  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...nodes);
  }

  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
    // A real DOM keeps `className` and the `class` attribute in sync.
    if (name === 'class') this.className = String(value);
  }

  /** Minimal geometry, enough for the chart code that reads it. */
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this._width || 600, height: this._height || 300 };
  }
  getAttribute(name) { return this.attributes[name] ?? null; }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) {
    if (this.listeners.has(type)) this.listeners.get(type).delete(fn);
  }
  /** Invoke listeners registered for `type`. */
  fire(type, event = {}) {
    for (const fn of this.listeners.get(type) || []) fn({ currentTarget: this, ...event });
  }

  /** Depth-first search by class name. */
  querySelector(selector) {
    const want = selector.replace(/^\./, '');
    for (const child of this.children) {
      if (child.nodeType !== 1) continue;
      if (child._classes().has(want)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }

  /** Every descendant carrying a class. */
  all(className) {
    const out = [];
    for (const child of this.children) {
      if (child.nodeType !== 1) continue;
      if (child._classes().has(className)) out.push(child);
      out.push(...child.all(className));
    }
    return out;
  }
}

/**
 * Install the stub globals the viewer expects.
 * @returns {{window: object, host: StubElement, flush: Function}}
 */
export function makeDom() {
  const frames = [];
  /** A text node, distinguishable from an element by `nodeType`. */
  class StubText {
    constructor(value) {
      this.nodeType = 3;
      this.textContent = String(value);
      this.parent = null;
    }
    get isConnected() {
      let node = this;
      while (node.parent) node = node.parent;
      return node.isRoot === true;
    }
    querySelector() { return null; }
    all() { return []; }
  }

  const document = {
    createElement: (tag) => new StubElement(tag),
    createElementNS: (_ns, tag) => new StubElement(tag),
    createTextNode: (value) => new StubText(value),
    createDocumentFragment: () => {
      const fragment = new StubElement('fragment');
      fragment.isFragment = true;
      return fragment;
    },
    addEventListener() {},
  };
  const window = {
    document,
    requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
    md: { render: (text) => String(text), highlight: (t) => String(t), escapeHtml: (t) => String(t) },
  };
  const host = new StubElement('div');
  host.isRoot = true;
  host.clientHeight = 800;
  host.clientWidth = 1200;
  host.scrollTop = 0;
  document.body = host;
  return {
    window,
    document,
    host,
    StubElement,
    /** Run queued animation-frame callbacks. */
    flush(times = 3) {
      for (let i = 0; i < times; i += 1) {
        const queued = frames.splice(0, frames.length);
        for (const fn of queued) fn();
      }
    },
  };
}

export { StubElement };

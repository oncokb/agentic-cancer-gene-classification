// Fake-DOM harness for the curator editing UI (src/static/curation.js plus
// app.js's renderers). Like openevidence_gate_harness.js it loads the real
// sources into a vm context minus app.js's trailing auto-run block, but its
// elements form a real tree: querySelector/querySelectorAll match simple
// selectors (tag, #id, .class, [data-x='y'], comma lists) against appended
// children, textContent aggregates descendants, and click/input/change/keydown
// listeners can be fired. innerHTML is kept as a string; querySelector on an
// innerHTML-built node falls back to a stub child (as the browser would have
// parsed it) so app.js's card templates keep working.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const STATIC = path.join(__dirname, "..", "..", "src", "static");
const TRAILING_INVOCATIONS =
  "initSidebarResize();\nbindEvents();\nrenderGrid();\nupdateExportState();\nloadDevStatus();\nloadSharedRun();\n";

function loadedSource() {
  const curation = fs.readFileSync(path.join(STATIC, "curation.js"), "utf8");
  const app = fs.readFileSync(path.join(STATIC, "app.js"), "utf8");
  const idx = app.lastIndexOf(TRAILING_INVOCATIONS);
  assert.notStrictEqual(idx, -1, "app.js's trailing invocation block changed — update curation_harness.js");
  return `${curation}\n${app.slice(0, idx)}\n` +
    "globalThis.state = state;\nglobalThis.elements = elements;\n" +
    "globalThis.curationState = curationState;\nglobalThis.CURATION_POLL = CURATION_POLL;\n";
}

function parseSelector(selector) {
  const sel = selector.trim();
  const attrs = [];
  const rest = sel.replace(/\[([\w-]+)(?:=['"]?([^'"\]]*)['"]?)?\]/g, (_m, name, value) => {
    attrs.push([name, value]);
    return "";
  });
  const tag = (rest.match(/^[a-zA-Z][\w-]*/) || [""])[0].toLowerCase();
  const id = (rest.match(/#([\w-]+)/) || [])[1];
  const classes = (rest.match(/\.[\w-]+/g) || []).map((c) => c.slice(1));
  return { tag, id, classes, attrs };
}

function datasetKey(attr) {
  return attr.slice(5).replace(/-([a-z])/g, (_m, c) => c.toUpperCase());
}

function matches(el, selector) {
  return selector.split(",").some((part) => {
    const { tag, id, classes, attrs } = parseSelector(part);
    if (tag && el.tagName !== tag) return false;
    if (id && el.id !== id) return false;
    if (classes.some((c) => !el.classList.contains(c))) return false;
    return attrs.every(([name, value]) => {
      const actual = name.startsWith("data-") ? el.dataset[datasetKey(name)] : el.getAttribute(name);
      return value === undefined ? actual !== undefined && actual !== null : String(actual) === value;
    });
  });
}

class FakeClassList {
  constructor() {
    this._set = new Set();
  }
  add(...names) { names.forEach((n) => this._set.add(n)); }
  remove(...names) { names.forEach((n) => this._set.delete(n)); }
  toggle(name, force) {
    const add = force === undefined ? !this._set.has(name) : Boolean(force);
    if (add) this._set.add(name);
    else this._set.delete(name);
    return add;
  }
  contains(name) { return this._set.has(name); }
}

let documentRef = null;

class FakeElement {
  constructor(tag = "div") {
    this.tagName = String(tag).toLowerCase();
    this.id = "";
    this.value = "";
    this.type = "";
    this.disabled = false;
    this.checked = false;
    this.selected = false;
    this.hidden = false;
    this.open = false;
    this.dataset = {};
    this.children = [];
    this.classList = new FakeClassList();
    this._text = "";
    this._html = "";
    this._attrs = new Map();
    this._listeners = new Map();
    this._parent = null;
    this.style = { setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } };
  }
  get className() { return Array.from(this.classList._set).join(" "); }
  set className(value) {
    this.classList = new FakeClassList();
    String(value || "").split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c));
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(value) {
    this._detachAll();
    this._text = String(value ?? "");
    this._html = "";
  }
  get innerHTML() { return this._html; }
  set innerHTML(value) {
    this._detachAll();
    this._text = "";
    this._html = String(value ?? "");
  }
  // Text and markup from the whole subtree, for assertions.
  get allText() {
    return [this._text, this._html, this.getAttribute("aria-label") || "", ...this.children.map((c) => c.allText)].join(" ");
  }
  get parentNode() { return this._parent; }
  get isConnected() {
    for (let node = this; node; node = node._parent) if (node._isRoot) return true;
    return false;
  }
  _detachAll() {
    this.children.forEach((c) => { c._parent = null; });
    this.children = [];
  }
  setAttribute(name, value) { this._attrs.set(name, String(value)); if (name === "id") this.id = String(value); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  removeAttribute(name) { this._attrs.delete(name); }
  addEventListener(type, handler) {
    if (!handler) return;
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(handler);
  }
  removeEventListener() {}
  dispatchEvent() { return true; }
  fire(type, extra = {}) {
    const event = { type, target: this, preventDefault() {}, stopPropagation() {}, ...extra };
    return (this._listeners.get(type) || []).map((handler) => handler(event));
  }
  appendChild(child) {
    if (child._parent) child._parent.removeChild(child);
    this.children.push(child);
    child._parent = this;
    return child;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    if (child._parent === this) child._parent = null;
    return child;
  }
  replaceChild(next, old) {
    const index = this.children.indexOf(old);
    if (index < 0) return this.appendChild(next);
    if (next._parent) next._parent.removeChild(next);
    this.children[index] = next;
    next._parent = this;
    old._parent = null;
    return old;
  }
  replaceChildren(...nodes) {
    this._detachAll();
    this._text = "";
    this._html = "";
    nodes.forEach((n) => this.appendChild(n));
  }
  after(node) {
    const parent = this._parent;
    if (!parent) return;
    if (node._parent) node._parent.removeChild(node);
    parent.children.splice(parent.children.indexOf(this) + 1, 0, node);
    node._parent = parent;
  }
  remove() { if (this._parent) this._parent.removeChild(this); }
  closest(selector) {
    for (let node = this; node; node = node._parent) if (matches(node, selector)) return node;
    return null;
  }
  contains(node) {
    for (let n = node; n; n = n._parent) if (n === this) return true;
    return false;
  }
  querySelectorAll(selector) {
    const found = [];
    const walk = (node) => node.children.forEach((child) => {
      if (matches(child, selector)) found.push(child);
      walk(child);
    });
    walk(this);
    return found;
  }
  querySelector(selector) {
    const found = this.querySelectorAll(selector)[0];
    if (found) return found;
    if (!this._html && !this._isRoot) return null;
    // Stand-in for an element the innerHTML template would have produced.
    const { tag, classes } = parseSelector(selector.split(",")[0]);
    const stub = new FakeElement(tag || "div");
    classes.forEach((c) => stub.classList.add(c));
    stub._stub = true;
    this.children.push(stub);
    stub._parent = this;
    return stub;
  }
  focus() { if (documentRef) documentRef.activeElement = this; }
  blur() {}
  click() { this.fire("click"); }
  select() {}
  scrollIntoView() {}
  getBoundingClientRect() { return { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; }
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
    text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
  };
}

// routes: async (method, url, body) => [status, json] | undefined (-> 404).
function loadCurationApp({ routes = async () => undefined, auth = { can_curate: true } } = {}) {
  const roots = new Map();
  const root = (id) => {
    if (!roots.has(id)) {
      const el = new FakeElement("div");
      el.id = id;
      el._isRoot = true;
      if (["curation-modal", "curation-aux-modal", "curation-status"].includes(id)) el.classList.add("hidden");
      roots.set(id, el);
    }
    return roots.get(id);
  };
  const documentListeners = new Map();
  const fakeDocument = {
    activeElement: null,
    body: root("body"),
    querySelector: (selector) => root(selector.replace(/^#/, "")),
    querySelectorAll: () => [],
    getElementById: (id) => root(id),
    createElement: (tag) => new FakeElement(tag),
    addEventListener(type, handler) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(handler);
    },
    removeEventListener() {},
  };
  documentRef = fakeDocument;
  const requests = [];
  const fetchImpl = async (url, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ method, url, body });
    const result = await routes(method, url, body);
    if (!result) return jsonResponse(404, { detail: "Not found" });
    return jsonResponse(result[0], result[1]);
  };
  const sandbox = {
    document: fakeDocument,
    window: { addEventListener() {}, removeEventListener() {}, location: { search: "", href: "http://localhost/" } },
    location: { search: "", href: "http://localhost/" },
    navigator: { clipboard: { writeText: async () => {} } },
    fetch: fetchImpl,
    URLSearchParams,
    AbortController,
    IntersectionObserver: class { observe() {} disconnect() {} },
    console,
    setTimeout,
    clearTimeout,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  new vm.Script(loadedSource(), { filename: "curation+app.js" }).runInContext(sandbox);
  sandbox.state.auth = { ...sandbox.state.auth, ...auth };
  sandbox.CURATION_POLL.initialDelayMs = 1;
  sandbox.CURATION_POLL.maxDelayMs = 2;
  sandbox.bindCurationEvents();
  sandbox.requests = requests;
  sandbox.root = root;
  sandbox.pressKey = (key) => (documentListeners.get("keydown") || []).forEach((h) => h({ key, preventDefault() {} }));
  return sandbox;
}

function byData(rootEl, name, value) {
  return rootEl.querySelectorAll(`[data-${name}='${value}']`);
}

function oneByData(rootEl, name, value) {
  const found = byData(rootEl, name, value);
  assert.ok(found.length, `no element with data-${name}='${value}'`);
  return found[0];
}

async function flush(times = 20) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setTimeout(resolve, 2));
}

async function waitFor(predicate, message = "condition", timeoutMs = 2000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > timeoutMs) throw new Error(`timed out waiting for ${message}`);
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
}

async function runTests(name, tests) {
  let failures = 0;
  for (const test of tests) {
    try {
      await test();
      console.log(`PASS ${test.name}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL ${test.name}`);
      console.error(err);
    }
  }
  if (failures) {
    console.error(`${failures}/${tests.length} ${name} tests failed`);
    process.exit(1);
  }
  console.log(`${tests.length}/${tests.length} ${name} tests passed`);
}

module.exports = { loadCurationApp, FakeElement, byData, oneByData, flush, waitFor, runTests };

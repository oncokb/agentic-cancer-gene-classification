// Plain Node fake-DOM harness, following test_api_keys_page.js.
"use strict";
const assert = require("assert");
const { createRunsPage, formatDateTime } = require("../../src/static/runs.js");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.textContent = "";
    this.children = [];
    this.listeners = {};
    const classes = new Set();
    this.classList = {
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      contains: (name) => classes.has(name),
    };
  }
  appendChild(child) { this.children.push(child); }
  replaceChildren() { this.children = []; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name) { return this.listeners[name](); }
  get hidden() { return this.classList.contains("hidden"); }
}
const RUN = {
  run_id: "run/1", created_at: "2026-10-26T12:00:00.123456", input_summary: "ALK, TP53",
  input_count: 2, genes_annotated: 2, source: "session", view_url: "/?run=run%2F1",
};
const response = (body, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};
function env(queue) {
  const nodes = {};
  for (const name of ["error", "empty", "loading", "table", "tbody", "load-more", "retry"]) {
    nodes[name] = new Element("div");
  }
  const calls = [];
  const window = { location: { origin: "https://acgc.example.org", href: "/runs" } };
  const app = createRunsPage({
    document: { getElementById: (id) => nodes[id.slice(5)], createElement: (tag) => new Element(tag) },
    window,
    fetch: async (url, init) => {
      calls.push({ url, init });
      assert.ok(queue.length, `Unexpected request: ${url}`);
      const next = queue.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  });
  return { app, nodes, calls, window };
}
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("first-load errors never claim empty history, and retry can reach a real empty state", async () => {
  for (const failure of [response({}, 500), new TypeError("offline"), response({}, 200)]) {
    const e = env([failure, response({ runs: [], next_cursor: null })]);
    await e.app.init();
    assert.ok(!e.nodes.error.hidden);
    assert.ok(e.nodes.empty.hidden);
    assert.ok(e.nodes.loading.hidden);
    assert.ok(!e.nodes.retry.hidden);
    await e.nodes.retry.fire("click");
    assert.ok(e.nodes.error.hidden);
    assert.ok(!e.nodes.empty.hidden);
    assert.ok(e.nodes.retry.hidden);
    assert.ok(e.nodes.table.hidden);
  }
});

test("load more appends in API order, encodes cursors, and blocks concurrent clicks", async () => {
  const first = deferred();
  const second = deferred();
  const e = env([first.promise, second.promise]);
  const init = e.app.init();
  assert.ok(!e.nodes.loading.hidden);
  assert.ok(e.nodes.empty.hidden);
  await e.app.init();
  assert.strictEqual(e.calls.length, 1);
  first.resolve(response({ runs: [RUN], next_cursor: "opaque+/=" }));
  await init;
  assert.ok(!e.nodes["load-more"].hidden);
  const more = e.nodes["load-more"].fire("click");
  await e.nodes["load-more"].fire("click");
  assert.strictEqual(e.calls.length, 2);
  assert.ok(e.nodes["load-more"].disabled);
  assert.strictEqual(e.calls[1].url, "/v1/runs?limit=20&cursor=opaque%2B%2F%3D");
  assert.strictEqual(e.calls[1].init.credentials, "same-origin");
  second.resolve(response({ runs: [{ ...RUN, input_summary: "BRAF" }], next_cursor: null }));
  await more;
  assert.strictEqual(e.nodes.tbody.children.length, 2);
  assert.strictEqual(e.nodes.tbody.children[0].children[1].textContent, "ALK, TP53 (2)");
  assert.strictEqual(e.nodes.tbody.children[1].children[1].textContent, "BRAF (2)");
  assert.ok(e.nodes["load-more"].hidden);
});

test("failed next page retains rows and retry uses the same cursor without duplicates", async () => {
  const e = env([
    response({ runs: [RUN], next_cursor: "next" }), response({}, 503),
    response({ runs: [{ ...RUN, run_id: "two" }], next_cursor: null }),
  ]);
  await e.app.init();
  await e.nodes["load-more"].fire("click");
  assert.ok(!e.nodes.table.hidden);
  assert.ok(e.nodes.empty.hidden);
  assert.ok(!e.nodes.error.hidden);
  assert.strictEqual(e.nodes.tbody.children.length, 1);
  await e.nodes.retry.fire("click");
  assert.strictEqual(e.calls[1].url, e.calls[2].url);
  assert.strictEqual(e.nodes.tbody.children.length, 2);
  assert.ok(e.nodes.error.hidden);
});

test("native anchors reject unsafe API links and render untrusted strings as text", async () => {
  const unsafe = ["javascript:alert(1)", "data:text/html,boom", "https://evil.test/", "//evil.test/", "/\\evil.test/", "\n javascript:alert(1)"];
  for (const view_url of [...unsafe, "/?run=safe", "https://acgc.example.org/?run=safe"]) {
    const e = env([response({ runs: [{ ...RUN, view_url, input_summary: "<img onerror=boom>", source: "<script>boom</script>" }], next_cursor: null })]);
    await e.app.init();
    const row = e.nodes.tbody.children[0];
    const anchor = row.children[0].children[0];
    assert.strictEqual(anchor.tagName, "a");
    const resolved = new URL(anchor.href, e.window.location.origin);
    assert.strictEqual(resolved.origin, e.window.location.origin);
    assert.strictEqual(resolved.protocol, "https:");
    if (unsafe.includes(view_url)) assert.strictEqual(resolved.search, "?run=run%2F1");
    assert.strictEqual(row.children[1].textContent, "<img onerror=boom> (2)");
    assert.strictEqual(row.children[3].textContent, "<script>boom</script>");
    assert.strictEqual(row.children[1].children.length, 0);
    assert.deepStrictEqual(anchor.listeners, {}); // Browser handles Cmd-click/new tabs.
  }
});

test("Safari-compatible timestamps normalize microseconds and absent timezones, with raw fallback", () => {
  const NativeDate = global.Date;
  const inputs = [];
  global.Date = class extends NativeDate {
    constructor(value) {
      inputs.push(value);
      // Simulate Safari refusing extended fractional precision / naive timestamps.
      super(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?(Z|[+-]\d{2}:\d{2})$/.test(value) ? value : NaN);
    }
  };
  try {
    for (const value of ["2026-10-26T12:00:00.123456", "2026-10-26T12:00:00.123456+00:00", "2026-10-26 12:00:00", "2026-10-26T12:00:00.1"]) {
      assert.strictEqual(formatDateTime(value), new NativeDate(value.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1") + (value.includes("+00:00") ? "" : "Z")).toLocaleString());
    }
    assert.strictEqual(inputs[0], "2026-10-26T12:00:00.123Z");
    assert.strictEqual(formatDateTime("unparseable"), "unparseable");
    assert.strictEqual(formatDateTime(null), "");
  } finally {
    global.Date = NativeDate;
  }
});

test("pending approval gets clear guidance; expired sessions redirect back to runs", async () => {
  const pending = env([response({ detail: "internal data" }, 403)]);
  await pending.app.init();
  assert.ok(pending.nodes.error.textContent.includes("pending administrator approval"));
  assert.ok(pending.nodes.error.textContent.includes("contact your AGCG administrator"));
  assert.ok(pending.nodes.empty.hidden);
  assert.ok(!pending.nodes.retry.hidden);
  const expired = env([response({}, 401)]);
  await expired.app.init();
  assert.strictEqual(expired.window.location.href, "/login?redirect_to=%2Fruns");
  assert.ok(expired.nodes.empty.hidden);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); console.log(`ok - ${name}`); }
    catch (error) { failed++; console.error(`not ok - ${name}\n${error.stack}`); }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exitCode = failed ? 1 : 0;
})();

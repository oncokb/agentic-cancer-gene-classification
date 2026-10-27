// Exercises src/static/tracking.js (contract: docs/ui-events.md) with a fake
// clock, IntersectionObserver, DOM, fetch and sendBeacon: logical-key dedupe
// across re-renders, the 1 s dwell and everything that cancels it, job_id ->
// run_id aliasing, OpenEvidence loading vs ready impressions, batching and
// beacons, the disabled switch, privacy, and 429 handling. The last tests
// drive app.js's real call sites through the vm harness.
// Run via tests/test_frontend_ui_tracking.py (invokes this with node).
"use strict";

const assert = require("assert");
const { createTracker } = require("../../src/static/tracking.js");
const { loadApp } = require("./openevidence_gate_harness");
const { createRunsPage } = require("../../src/static/runs.js");

// ---- fakes -----------------------------------------------------------------

function fakeClock() {
  let now = 1_700_000_000_000;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => now,
    setTimeout(fn, ms) {
      const id = nextId++;
      timers.set(id, { fn, at: now + Math.max(0, ms || 0) });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        await flushMicrotasks();
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        timers.delete(due[0]);
        now = due[1].at;
        due[1].fn();
      }
      now = end;
      await flushMicrotasks();
    },
  };
}

const flushMicrotasks = () => new Promise((resolve) => setImmediate(resolve));

class Listeners {
  constructor() {
    this.map = new Map();
  }
  addEventListener(type, fn) {
    if (!this.map.has(type)) this.map.set(type, []);
    this.map.get(type).push(fn);
  }
  dispatch(type, event = {}) {
    (this.map.get(type) || []).forEach((fn) => fn({ type, ...event }));
  }
}

class Node extends Listeners {
  constructor(tag = "div", parent = null) {
    super();
    this.tagName = tag;
    this.parentNode = parent;
    this.isConnected = true;
    this.dataset = {};
    this.rect = { top: 100, left: 0, bottom: 300, right: 400, width: 400, height: 200 };
  }
  getBoundingClientRect() {
    return this.rect;
  }
  closest(selector) {
    for (let node = this; node; node = node.parentNode) {
      if (selector === "a[href]" && node.tagName === "a" && node.href) return node;
    }
    return null;
  }
}

class FakeIO {
  constructor(callback) {
    this.callback = callback;
    this.targets = new Set();
    FakeIO.instances.push(this);
  }
  observe(el) {
    this.targets.add(el);
  }
  unobserve(el) {
    this.targets.delete(el);
  }
  // A real observer only reports elements it observes (impressed ones are
  // unobserved), so reports for anything else are no-ops.
  report(el, ratio, extra = {}) {
    if (!this.targets.has(el)) return;
    this.callback([{
      target: el,
      isIntersecting: ratio > 0,
      intersectionRatio: ratio,
      intersectionRect: { height: ratio * el.rect.height },
      rootBounds: { height: 800 },
      ...extra,
    }]);
  }
}
FakeIO.instances = [];

const response = (status, headers = {}) => ({
  status,
  ok: status >= 200 && status < 300,
  headers: { get: (name) => headers[name] ?? null },
  json: async () => ({}),
});

function setup({ enabled = true, fetchImpl, beacon = true } = {}) {
  FakeIO.instances = [];
  const clock = fakeClock();
  const document = new Listeners();
  document.visibilityState = "visible";
  const window = new Listeners();
  window.innerHeight = 800;
  window.innerWidth = 1200;
  const posts = [];
  const beacons = [];
  let uuid = 0;
  const tracker = createTracker({
    window,
    document,
    navigator: beacon ? { sendBeacon: (url, body) => { beacons.push({ url, body }); return true; } } : {},
    fetch: async (url, init) => {
      posts.push({ url, init, body: init && init.body ? JSON.parse(init.body) : null });
      return fetchImpl ? fetchImpl(url, init) : response(202);
    },
    IntersectionObserver: FakeIO,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    now: clock.now,
    uuid: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, "0")}`,
  });
  if (enabled !== null) tracker.setEnabled(enabled);
  const io = FakeIO.instances[0];
  const queued = () => tracker._debug().queue;
  const sent = () => posts.filter((p) => p.url === "/v1/events").flatMap((p) => p.body.events);
  const all = () => [...sent(), ...queued()];
  const of = (type, section) => all().filter((e) => e.type === type && (!section || e.section === section));
  return { tracker, clock, document, window, io, posts, beacons, queued, sent, all, of };
}

const CARD = { surface: "results", subject_kind: "gene", subject_id: "ALK", section: "gene_card", tab: "results", position: 0 };

// ---- tests -----------------------------------------------------------------

const TESTS = [];
const test = (name, fn) => TESTS.push({ name, fn });

test("surfaced and impression dedupe on the logical key across re-rendered nodes", async () => {
  const t = setup();
  t.tracker.setRunScope("run-1");
  t.tracker.setActiveTab("results");
  let node = new Node("article");
  for (let render = 0; render < 5; render += 1) {
    node.isConnected = false;
    node = new Node("article");
    t.tracker.surfaced(node, CARD);
    await flushMicrotasks();
    assert.strictEqual(t.io.targets.has(node), render === 0, "impressed elements are no longer observed");
    t.io.report(node, 1);
    await t.clock.advance(1500);
  }
  assert.strictEqual(t.of("surfaced").length, 1);
  assert.strictEqual(t.of("impression").length, 1);
  const [impression] = t.of("impression");
  assert.strictEqual(impression.subject_id, "ALK");
  assert.strictEqual(impression.run_id, "run-1");
  assert.strictEqual(impression.position, 0);
  assert.ok(!("tab" in impression), "internal tab field must not be sent");
});

test("impression needs a continuous 1 s dwell at >= 50% visibility", async () => {
  const t = setup();
  t.tracker.setActiveTab("results");
  const node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 0.4);
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "40% is not enough");
  t.io.report(node, 0.6);
  await t.clock.advance(999);
  assert.strictEqual(t.of("impression").length, 0);
  await t.clock.advance(1);
  assert.strictEqual(t.of("impression").length, 1);
});

test("an element taller than the viewport qualifies once it fills half of it", async () => {
  const t = setup();
  t.tracker.setActiveTab("results");
  const node = new Node();
  node.rect = { top: -500, left: 0, bottom: 2500, right: 400, width: 400, height: 3000 };
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 0.27, { intersectionRect: { height: 800 } });
  await t.clock.advance(1000);
  assert.strictEqual(t.of("impression").length, 1);
});

test("dwell cancels on scroll-away, tab switch, hidden document and removal", async () => {
  // Scroll away mid-dwell.
  let t = setup();
  t.tracker.setActiveTab("results");
  let node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 1);
  await t.clock.advance(600);
  t.io.report(node, 0.2);
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "scroll-away");

  // Tab switch mid-dwell; coming back restarts a full second.
  t = setup();
  t.tracker.setActiveTab("results");
  node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 1);
  await t.clock.advance(600);
  t.tracker.setActiveTab("fusion");
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "tab switch");
  t.tracker.setActiveTab("results");
  await t.clock.advance(999);
  assert.strictEqual(t.of("impression").length, 0);
  await t.clock.advance(1);
  assert.strictEqual(t.of("impression").length, 1, "back on the tab");

  // Elements of an inactive tab never start a dwell.
  t = setup();
  t.tracker.setActiveTab("results");
  node = new Node();
  t.tracker.observe(node, { ...CARD, surface: "fusion_evidence", tab: "fusion", section: "fusion_card" });
  await flushMicrotasks();
  t.io.report(node, 1);
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "inactive tab");

  // Document hidden mid-dwell.
  t = setup();
  t.tracker.setActiveTab("results");
  node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 1);
  await t.clock.advance(600);
  t.document.visibilityState = "hidden";
  t.document.dispatch("visibilitychange");
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "hidden");
  t.document.visibilityState = "visible";
  t.document.dispatch("visibilitychange");
  await t.clock.advance(1000);
  assert.strictEqual(t.of("impression").length, 1, "visible again");

  // Removed before expiry.
  t = setup();
  t.tracker.setActiveTab("results");
  node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 1);
  await t.clock.advance(600);
  node.isConnected = false;
  await t.clock.advance(2000);
  assert.strictEqual(t.of("impression").length, 0, "removed");

  // Layout moved it off-screen without an observer report.
  t = setup();
  t.tracker.setActiveTab("results");
  node = new Node();
  t.tracker.observe(node, CARD);
  await flushMicrotasks();
  t.io.report(node, 1);
  node.rect = { top: 900, left: 0, bottom: 1100, right: 400, width: 400, height: 200 };
  await t.clock.advance(1000);
  assert.strictEqual(t.of("impression").length, 0, "re-check at expiry");
});

test("a re-render mid-dwell carries the dwell over to the replacement node", async () => {
  const t = setup();
  t.tracker.setActiveTab("results");
  const first = new Node();
  t.tracker.surfaced(first, CARD);
  await flushMicrotasks();
  t.io.report(first, 1);
  await t.clock.advance(600);
  first.isConnected = false;
  const second = new Node();
  t.tracker.surfaced(second, CARD);
  await flushMicrotasks();
  assert.ok(!t.io.targets.has(first) && t.io.targets.has(second), "observer moved to the new node");
  t.io.report(second, 1);
  await t.clock.advance(400);
  assert.strictEqual(t.of("impression").length, 1, "continuous dwell across the re-render");
  assert.strictEqual(t.of("surfaced").length, 1);
});

test("job_id aliases to run_id without resetting dedupe; a new run resets it", async () => {
  const t = setup();
  t.tracker.setActiveTab("results");
  t.tracker.setRunScope("job-1");
  t.tracker.surfaced(new Node(), CARD);
  await flushMicrotasks();
  t.tracker.aliasRunScope("job-1", "run-1");
  t.tracker.setRunScope("run-1");
  const node = new Node();
  t.tracker.surfaced(node, CARD);
  await flushMicrotasks();
  assert.strictEqual(t.of("surfaced").length, 1);
  assert.strictEqual(t.of("surfaced")[0].run_id, "job-1");
  t.io.report(node, 1);
  await t.clock.advance(1000);
  assert.strictEqual(t.of("impression")[0].run_id, "run-1", "later events carry the final run id");
  t.tracker.aliasRunScope("job-other", "run-x"); // not this run's job: ignored
  t.tracker.setRunScope("run-2");
  t.tracker.surfaced(new Node(), CARD);
  await flushMicrotasks();
  assert.strictEqual(t.of("surfaced").length, 2, "new run surfaces again");
  assert.strictEqual(t.of("surfaced")[1].run_id, "run-2");
});

test("OpenEvidence: loading impression doesn't consume the ready one; states once per transition", async () => {
  const t = setup();
  t.tracker.setActiveTab("results");
  t.tracker.setRunScope("run-1");
  const oe = { ...CARD, section: "openevidence" };
  const card = new Node("article");
  t.tracker.surfaced(card, oe);
  t.tracker.state(card, "loading");
  await flushMicrotasks();
  t.io.report(card, 1);
  await t.clock.advance(1000);
  t.tracker.state(card, "pending");
  // Poll re-render: new card, same lifecycle.
  const card2 = new Node("article");
  t.tracker.surfaced(card2, oe);
  t.tracker.state(card2, "loading");
  t.tracker.state(card2, "pending");
  const body = new Node("div", card2);
  t.tracker.state(card2, "ready");
  t.tracker.surfaced(body, { section: "openevidence_content" });
  await flushMicrotasks();
  t.io.report(body, 1);
  await t.clock.advance(1000);
  // A later re-render answered from the memo reports "cached": same transition.
  t.tracker.state(new Node("article"), "loading");
  t.tracker.state(card2, "cached");

  const impressions = t.of("impression").map((e) => e.section).sort();
  assert.deepStrictEqual(impressions, ["openevidence", "openevidence_content"]);
  assert.deepStrictEqual(t.of("state").map((e) => e.state), ["loading", "pending", "ready"]);
  const content = t.of("surfaced", "openevidence_content")[0];
  assert.strictEqual(content.subject_id, "ALK", "body inherits the card's subject");
  assert.strictEqual(content.position, 0);
});

test("batches of <= 50 with contract headers; timer flush; sendBeacon on pagehide", async () => {
  const t = setup();
  for (let i = 0; i < 120; i += 1) t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: String(i) });
  await flushMicrotasks();
  const first = t.posts.filter((p) => p.url === "/v1/events");
  assert.ok(first.length >= 2, "size-triggered flushes");
  first.forEach((p) => {
    assert.ok(p.body.events.length <= 50);
    assert.strictEqual(p.body.schema_version, 1);
    assert.strictEqual(p.body.page_view_id, t.tracker.pageViewId);
    assert.strictEqual(p.init.method, "POST");
    assert.strictEqual(p.init.keepalive, true);
    assert.strictEqual(p.init.headers["X-ACGC-Events"], "1");
    assert.strictEqual(p.init.headers["Content-Type"], "application/json");
  });
  await t.clock.advance(5000);
  assert.strictEqual(t.sent().length, 120, "remainder flushed on the timer");
  assert.strictEqual(new Set(t.sent().map((e) => e.event_id)).size, 120, "event ids are unique");

  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "2" });
  t.window.dispatch("pagehide");
  assert.strictEqual(t.beacons.length, 1);
  assert.strictEqual(t.beacons[0].url, "/v1/events?beacon=1");
  const body = JSON.parse(t.beacons[0].body);
  assert.strictEqual(body.events.length, 2);
  assert.strictEqual(body.page_view_id, t.tracker.pageViewId);
  assert.strictEqual(t.queued().length, 0);

  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "3" });
  t.document.visibilityState = "hidden";
  t.document.dispatch("visibilitychange");
  assert.strictEqual(t.beacons.length, 2, "visibilitychange=hidden also beacons");
});

test("disabled (or a server without the flag) means zero network", async () => {
  for (const make of [
    () => setup({ enabled: false }),
    () => {
      const t = setup({ enabled: null });
      t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
      t.tracker.setEnabled(false); // held events are dropped, not sent
      return t;
    },
  ]) {
    const t = make();
    t.tracker.setRunScope("run-1");
    t.tracker.setActiveTab("results");
    const node = new Node();
    t.tracker.surfaced(node, CARD);
    t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
    await t.clock.advance(10000);
    t.window.dispatch("pagehide");
    assert.strictEqual(t.posts.length, 0);
    assert.strictEqual(t.beacons.length, 0);
    assert.strictEqual(t.io.targets.size, 0, "nothing is even observed");
  }

  // /v1/dev/status without ui_events_enabled (T1 not deployed) -> disabled.
  const t = setup({ enabled: null, fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ enabled: false }) }) });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  assert.strictEqual(await t.tracker.loadStatus(), false);
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "2" });
  await t.clock.advance(10000);
  t.window.dispatch("pagehide");
  assert.deepStrictEqual(t.posts.map((p) => p.url), ["/v1/dev/status"]);
  assert.strictEqual(t.beacons.length, 0);
});

test("events recorded before the status answer are sent once it says enabled", async () => {
  const t = setup({ enabled: null });
  t.tracker.event({ type: "open", surface: "feedback", section: "feedback_modal" });
  await t.clock.advance(10000);
  assert.strictEqual(t.posts.length, 0);
  t.tracker.setEnabled(true);
  await t.clock.advance(0);
  assert.strictEqual(t.sent().length, 1);
});

test("payloads carry only contract fields: no free text, bad values rejected", async () => {
  const t = setup();
  t.tracker.event({
    type: "action", state: "filter_used", surface: "sidebar", section: "sidebar_filter", item_id: "results",
    query: "secret query", email: "someone@example.org", message: "secret feedback",
  });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "x".repeat(33) });
  t.tracker.event({ type: "bogus", surface: "global", section: "pmid_link" });
  t.tracker.event({ type: "action", state: "typed text", surface: "sidebar", section: "sidebar_filter" });
  t.tracker.event({ type: "click", surface: "results", section: "not_a_section" });
  await t.clock.advance(5000);
  const raw = JSON.stringify(t.posts.map((p) => p.body));
  for (const secret of ["secret", "someone@example.org"]) assert.ok(!raw.includes(secret), secret);
  const events = t.sent();
  assert.strictEqual(events.length, 2, "invalid events are dropped client-side");
  const FIELDS = [
    "display_position", "event_id", "flags", "item_id", "position", "run_id", "section", "state",
    "subject_id", "subject_kind", "surface", "ts", "tumor_type_given", "type",
  ];
  events.forEach((e) => assert.deepStrictEqual(Object.keys(e).sort(), FIELDS));
  assert.strictEqual(events[1].item_id, null, "over-long ids are nulled, never truncated");
  assert.ok(events.every((e) => e.flags === null));
});

test("flags provider supplies {key: {variant, version}}", async () => {
  const t = setup();
  t.tracker.setFlagsProvider(() => ({ oe_layout: { variant: "b", version: 3 } }));
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  t.tracker.setFlagsProvider(() => ({ bad: { variant: 7 } }));
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "2" });
  await t.clock.advance(5000);
  assert.deepStrictEqual(t.sent()[0].flags, { oe_layout: { variant: "b", version: 3 } });
  assert.strictEqual(t.sent()[1].flags, null);
});

test("429 waits for Retry-After and resends the same batch; 4xx drops; network errors retry a bounded number of times", async () => {
  let statuses = [429, 202];
  let t = setup({ fetchImpl: async () => response(statuses.shift(), { "Retry-After": "10" }) });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  await t.clock.advance(5000);
  assert.strictEqual(t.posts.length, 1);
  const firstId = t.posts[0].body.events[0].event_id;
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "2" });
  await t.clock.advance(9000);
  assert.strictEqual(t.posts.length, 1, "nothing sent before Retry-After");
  t.window.dispatch("pagehide");
  assert.strictEqual(t.beacons.length, 0, "beacons respect Retry-After too");
  await t.clock.advance(2000);
  assert.strictEqual(t.posts.length, 2);
  assert.strictEqual(t.posts[1].body.events[0].event_id, firstId, "same event id resent (server dedupes)");
  assert.strictEqual(t.posts[1].body.events.length, 2);

  statuses = [422];
  t = setup({ fetchImpl: async () => response(statuses.shift() || 202) });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  await t.clock.advance(60000);
  assert.strictEqual(t.posts.length, 1, "4xx batch is dropped, not retried");

  t = setup({ fetchImpl: async () => { throw new TypeError("offline"); } });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  await t.clock.advance(600000);
  assert.strictEqual(t.posts.length, 4, "first try + 3 retries");
  assert.strictEqual(t.queued().length, 0);

  t = setup({ fetchImpl: async () => response(204) });
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  await t.clock.advance(5000);
  t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "2" });
  await t.clock.advance(60000);
  assert.strictEqual(t.posts.length, 1, "204 (server switched off) stops sending");
});

test("the queue is capped", async () => {
  const t = setup({ enabled: null });
  for (let i = 0; i < 2000; i += 1) t.tracker.event({ type: "click", surface: "global", section: "pmid_link", item_id: "1" });
  assert.strictEqual(t.queued().length, 500);
});

test("delegated PMID link clicks (click + middle auxclick) carry the card's context", async () => {
  const t = setup();
  t.tracker.setRunScope("run-1");
  const card = new Node("article");
  t.tracker.surfaced(card, CARD);
  const details = new Node("details", card);
  t.tracker.details(details, { section: "retrieved_pmids" });
  const link = new Node("a", details);
  link.href = "https://pubmed.ncbi.nlm.nih.gov/12345678/";
  const span = new Node("span", link);
  t.document.dispatch("click", { target: span });
  t.document.dispatch("auxclick", { target: link, button: 1 });
  t.document.dispatch("auxclick", { target: link, button: 2 }); // context menu: ignored
  const other = new Node("a", card);
  other.href = "https://example.org/";
  t.document.dispatch("click", { target: other });
  const guideline = new Node("a", card);
  guideline.href = "https://guidelines.example.org/very/long/url";
  guideline.dataset = { trackSection: "openevidence_guideline_link", trackItem: "0" };
  t.document.dispatch("click", { target: guideline });
  details.open = true;
  details.dispatch("toggle");
  await flushMicrotasks();
  const clicks = t.of("click");
  assert.deepStrictEqual(clicks.map((e) => [e.section, e.item_id]), [
    ["pmid_link", "12345678"], ["pmid_link", "12345678"], ["openevidence_guideline_link", "0"],
  ]);
  clicks.forEach((e) => {
    assert.strictEqual(e.surface, "results");
    assert.strictEqual(e.subject_id, "ALK");
    assert.strictEqual(e.run_id, "run-1");
  });
  const open = t.of("open")[0];
  assert.strictEqual(open.section, "retrieved_pmids");
  assert.strictEqual(open.subject_id, "ALK");
});

test("toggled() only counts trusted (user) events", async () => {
  const t = setup();
  const section = new Node("section");
  t.tracker.surfaced(section, { surface: "fusion_evidence", subject_kind: "none", section: "fusion_section", item_id: "requested_tumor", tab: "fusion" });
  t.tracker.toggled(section, true, { isTrusted: false });
  t.tracker.toggled(section, true, undefined);
  t.tracker.toggled(section, false, { isTrusted: true });
  await flushMicrotasks();
  assert.deepStrictEqual(t.all().filter((e) => e.type !== "surfaced").map((e) => [e.type, e.item_id]), [["close", "requested_tumor"]]);
});

// ---- app.js call sites -----------------------------------------------------

const annotation = (gene, extra = {}) => ({
  gene,
  fusions: [],
  cancer_association_rationale: `${gene} rationale`,
  gene_summary: `${gene} summary`,
  cancer_associated: true,
  ...extra,
});

function appWithTracker(fetchImpl) {
  const clock = fakeClock();
  const posts = [];
  const sandbox = loadApp({
    fetchImpl: fetchImpl || (async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => "" })),
  });
  const tracker = createTracker({
    fetch: async (url, init) => {
      posts.push(JSON.parse(init.body));
      return response(202);
    },
    IntersectionObserver: FakeIO,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    now: clock.now,
  });
  tracker.setEnabled(true);
  sandbox.window.ACGCTracking = tracker;
  const events = () => [...posts.flatMap((p) => p.events), ...tracker._debug().queue];
  return { sandbox, tracker, clock, posts, events };
}

test("app.js: progress re-renders and the job->run switch surface each gene card once", async () => {
  const { sandbox, events, clock } = appWithTracker();
  const genes = ["ALK", "EML4", "TP53"];
  const partial = { run_id: "job-1", annotations: genes.map((g) => annotation(g)), genes_annotated: 3 };
  for (let i = 0; i < 3; i += 1) {
    sandbox.state.currentResult = partial;
    sandbox.renderAnnotationResult(partial);
    await flushMicrotasks();
  }
  sandbox.adoptOpenEvidenceRunId("job-1", "run-1");
  const done = { ...partial, run_id: "run-1" };
  sandbox.state.currentResult = done;
  sandbox.renderAnnotationResult(done);
  await clock.advance(5000);
  const cards = events().filter((e) => e.type === "surfaced" && e.section === "gene_card");
  assert.deepStrictEqual(cards.map((e) => e.subject_id), genes);
  assert.deepStrictEqual(cards.map((e) => e.display_position), [0, 1, 2]);
  assert.ok(cards.every((e) => e.run_id === "job-1" && e.surface === "results"));
});

test("app.js: sidebar filter and feedback events never carry the typed text", async () => {
  const { sandbox, events, clock } = appWithTracker();
  sandbox.setGeneIndexFilter("eml4 private query");
  sandbox.setGeneIndexFilter("eml4 private query more");
  sandbox.setGeneIndexFilter("");
  sandbox.elements.feedbackMessage.value = "private feedback text";
  sandbox.elements.feedbackEmail.value = "person@example.org";
  sandbox.showFeedbackModal();
  sandbox.elements.feedbackMessage.value = "private feedback text";
  await sandbox.submitFeedbackForm();
  sandbox.hideFeedbackModal();
  await clock.advance(5000);
  const all = events();
  const raw = JSON.stringify(all);
  for (const secret of ["private", "person@example.org"]) assert.ok(!raw.includes(secret), secret);
  assert.deepStrictEqual(all.map((e) => [e.type, e.section, e.state]), [
    ["action", "sidebar_filter", "filter_used"],
    ["action", "sidebar_filter", "filter_cleared"],
    ["open", "feedback_modal", null],
    ["action", "feedback_modal", "submit"],
    ["close", "feedback_modal", null],
  ]);
});

test("app.js: user tab switches vs automatic fallback", async () => {
  const { sandbox, events, clock } = appWithTracker();
  const result = {
    run_id: "run-1",
    annotations: [annotation("ALK"), annotation("BRAF", { insufficient_evidence: true })],
    genes_annotated: 2,
  };
  sandbox.state.currentResult = result;
  sandbox.renderAnnotationResult(result);
  sandbox.switchResultsView("noresult");
  await flushMicrotasks();
  // A new run without insufficient-evidence genes forces the tab back.
  const next = { run_id: "run-2", annotations: [annotation("ALK")], genes_annotated: 1 };
  sandbox.state.currentResult = next;
  sandbox.renderAnnotationResult(next);
  await clock.advance(5000);
  const tabs = events().filter((e) => e.section === "tab").map((e) => [e.type, e.item_id, e.run_id]);
  assert.deepStrictEqual(tabs, [["open", "noresult", "run-1"], ["state", "results", "run-2"]]);
  const noResultCard = events().find((e) => e.surface === "no_result" && e.section === "gene_card");
  assert.strictEqual(noResultCard.subject_id, "BRAF");
  assert.strictEqual(noResultCard.position, 1);
});

test("runs.js: rows surface once across load-more re-renders; opened_run and load_more actions", async () => {
  const clock = fakeClock();
  const tracker = createTracker({ IntersectionObserver: FakeIO, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout, now: clock.now });
  tracker.setEnabled(true);
  class El extends Node {
    constructor(tag) {
      super(tag);
      this.children = [];
      const classes = new Set();
      this.classList = { toggle: (n, on) => (on ? classes.add(n) : classes.delete(n)) };
    }
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
    }
    replaceChildren() {
      this.children.forEach((child) => { child.isConnected = false; });
      this.children = [];
    }
  }
  const nodes = {};
  for (const name of ["error", "empty", "loading", "table", "tbody", "load-more", "retry", "page"]) nodes[name] = new El("div");
  const run = (id) => ({ run_id: id, created_at: "2026-10-26T12:00:00", view_url: `/?run=${id}`, input_summary: "secret input" });
  const pages = [
    { runs: [run("r1"), run("r2")], next_cursor: "c" },
    { runs: [run("r3")], next_cursor: null },
  ];
  const app = createRunsPage({
    document: { getElementById: (id) => nodes[id.slice(5)], createElement: (tag) => new El(tag) },
    window: { location: { origin: "https://acgc.example.org", href: "/runs" } },
    fetch: async () => ({ status: 200, ok: true, json: async () => pages.shift() }),
    tracker,
  });
  await app.init();
  await nodes["load-more"].map.get("click")[0]();
  const link = nodes.tbody.children[2].children[0].children[0];
  link.href = "https://acgc.example.org/?run=r3";
  nodes.tbody.dispatch("click", { target: link });
  nodes.tbody.dispatch("auxclick", { target: link, button: 2 });
  await flushMicrotasks();
  const events = tracker._debug().queue;
  const rows = events.filter((e) => e.type === "surfaced" && e.section === "runs_row");
  assert.deepStrictEqual(rows.map((e) => [e.subject_id, e.position]), [["r1", 0], ["r2", 1], ["r3", 2]]);
  assert.deepStrictEqual(
    events.filter((e) => e.type === "action").map((e) => [e.state, e.section, e.subject_id]),
    [["load_more", "runs_pagination", null], ["opened_run", "runs_row", "r3"]],
  );
  assert.strictEqual(events.filter((e) => e.type === "surfaced" && e.section === "runs_page").length, 1);
  assert.ok(!JSON.stringify(events).includes("secret"));
});

async function main() {
  let failures = 0;
  for (const { name, fn } of TESTS) {
    try {
      await fn();
      console.log(`PASS ${name}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL ${name}`);
      console.error(err);
    }
  }
  if (failures > 0) {
    console.error(`${failures}/${TESTS.length} ui tracking tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} ui tracking tests passed`);
  process.exit(0);
}

main();

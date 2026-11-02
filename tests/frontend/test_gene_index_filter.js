// Exercises the results sidebar filter box in src/static/app.js against the
// real rebuildGeneIndex / renderFusionEvidenceView / applyResultsViewMode
// code: matching by symbol, fusion (any separator), and submitted ENST/NM_
// input; the "N of M" count and No matches state; Enter jumping to the first
// match (opening a collapsed section); Escape clearing; empty section
// headings hidden; per-tab filters kept across redraws and reset on new runs;
// hidden entries never hold the active highlight; jumps move focus to the card.
// Run via tests/test_frontend_gene_index_filter.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp, FakeElement, findById } = require("./openevidence_gate_harness");

const walk = (root) => [root, ...root.children.flatMap(walk)];
const hasClass = (node, name) => node.className.split(" ").includes(name);
// Same minimal browser operations test_fusion_evidence_sections.js adds.
FakeElement.prototype.querySelectorAll = function (selector) {
  return walk(this).slice(1).filter((node) => selector === ".annotation-card[id]"
    ? hasClass(node, "annotation-card") && node.id
    : hasClass(node, selector.slice(1)));
};
const originalQuerySelector = FakeElement.prototype.querySelector;
FakeElement.prototype.querySelector = function (selector) {
  return this.querySelectorAll(selector)[0] || originalQuerySelector.call(this, selector);
};
FakeElement.prototype.closest = function () {
  for (let node = this._parent; node; node = node._parent) {
    if (node.tagName === "section" && hasClass(node, "fusion-evidence-section")) return node;
  }
  return null;
};
FakeElement.prototype.click = function () {
  (this._listeners.get("click") || []).forEach((handler) => handler());
};
FakeElement.prototype.scrollIntoView = function () { this.scrolled = true; };
let focusedElement = null;
FakeElement.prototype.focus = function (options) {
  focusedElement = this;
  this.focusOptions = options;
};

const DEBOUNCE_WAIT_MS = 120;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function app() {
  const sandbox = loadApp({ fetchImpl: async () => ({ ok: false }) });
  sandbox.document.getElementById = (id) => findById(sandbox.elements.resultsWindow, id);
  sandbox.observers = [];
  sandbox.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
      this.cards = [];
      sandbox.observers.push(this);
    }
    observe(card) { this.cards.push(card); }
    disconnect() { this.disconnected = true; }
  };
  sandbox.bindGeneIndexFilter();
  return sandbox;
}

const input = (sandbox) => sandbox.elements.geneIndexFilter;
const fire = (node, type, event = {}) => (node._listeners.get(type) || []).forEach((h) => h(event));
async function type(sandbox, value) {
  input(sandbox).value = value;
  fire(input(sandbox), "input");
  await wait(DEBOUNCE_WAIT_MS);
}
function press(sandbox, key) {
  const event = { key, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  fire(input(sandbox), "keydown", event);
  return event;
}
const items = (sandbox) => sandbox.elements.geneIndex.children.filter((n) => hasClass(n, "gene-index-item"));
const shown = (sandbox) => items(sandbox).filter((n) => !n.hidden).map((n) => n.title);
const headings = (sandbox) => sandbox.elements.geneIndex.children
  .filter((n) => hasClass(n, "fusion-index-section"))
  .map((n) => [n.textContent, !n.hidden]);
const count = (sandbox) => sandbox.elements.geneIndexFilterCount.textContent;
const noMatches = (sandbox) => !sandbox.elements.geneIndexNoMatches.hidden;

const ANNOTATIONS = [
  { gene: "ALK", fusions: ["EML4::ALK"] },
  { gene: "EML4", fusions: ["EML4::ALK"] },
  { gene: "TP53", resolved_inputs: [{ input: "ENST00000269305", resolved: "TP53", warnings: [] }] },
  { gene: "BRAF", resolved_inputs: [{ input: "NM_004333.6", resolved: "BRAF", warnings: [] }] },
  { gene: "KRAS", resolved_inputs: [{ input: "KRAS", resolved: "KRAS", warnings: [] }] },
  { gene: "SEMA3A-AS1" },
];

const FUSIONS = [
  { fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048", tumor_type: "Lung",
    literature_scope: "requested_tumor", pmids: ["1"] },
  { fusion: "KIF5B::RET", tumor_type: "Lung", literature_scope: "requested_tumor", pmids: ["2"] },
  { fusion: "NPM1::ALK", tumor_type: "Lung", literature_scope: "other_or_unspecified", pmids: ["3"] },
  { fusion: "FOO::BAR", tumor_type: "Lung", literature_scope: "no_literature", pmids: [] },
];

function renderResults(sandbox, annotations = ANNOTATIONS) {
  sandbox.state.resultsViewMode = "results";
  sandbox.applyResultsViewMode(annotations, [], []);
}

function renderFusions(sandbox) {
  sandbox.state.resultsViewMode = "fusion";
  sandbox.renderFusionEvidenceView(sandbox.state.currentResult.fusion_evidence);
}

async function testFiltersBySymbolCaseInsensitive() {
  const sandbox = app();
  renderResults(sandbox);
  assert.strictEqual(sandbox.elements.geneIndexPanel.classList.contains("hidden"), false);
  assert.strictEqual(count(sandbox), "", "no count while not filtering");
  await type(sandbox, "tp5");
  assert.deepStrictEqual(shown(sandbox), ["TP53"]);
  assert.strictEqual(count(sandbox), "1 of 6");
  await type(sandbox, "sema3a-as");
  assert.deepStrictEqual(shown(sandbox), ["SEMA3A-AS1"]);
  assert.strictEqual(sandbox.elements.geneIndexFilterClear.hidden, false);
  // Cards are never hidden or reordered by the filter.
  const cards = sandbox.elements.resultsWindow.querySelectorAll(".annotation-card[id]");
  assert.deepStrictEqual(cards.map((c) => [c.id, c.hidden]), ANNOTATIONS.map((a) => [`gene-${a.gene}`, false]));
}

async function testFiltersByFusionWithAnySeparator() {
  const sandbox = app();
  renderResults(sandbox);
  for (const query of ["eml4 alk", "EML4-ALK", "EML4::ALK", "eml4/alk", "Eml4  ::  alk"]) {
    await type(sandbox, query);
    assert.deepStrictEqual(shown(sandbox), ["ALK", "EML4"], query);
    assert.strictEqual(count(sandbox), "2 of 6", query);
  }
  sandbox.state.currentResult = { run_id: "r1", fusion_evidence: FUSIONS };
  renderFusions(sandbox);
  for (const query of ["eml4 alk", "EML4-ALK", "EML4::ALK"]) {
    await type(sandbox, query);
    assert.deepStrictEqual(shown(sandbox), ["EML4::ALK"], query);
  }
  // Each partner on its own.
  await type(sandbox, "alk");
  assert.deepStrictEqual(shown(sandbox), ["EML4::ALK", "NPM1::ALK"]);
  await type(sandbox, "kif5b");
  assert.deepStrictEqual(shown(sandbox), ["KIF5B::RET"]);
  assert.strictEqual(count(sandbox), "1 of 4");
  // A query cannot match across the end of one term and the start of the next.
  await type(sandbox, "alk enst");
  assert.deepStrictEqual(shown(sandbox), []);
}

async function testFiltersBySubmittedInput() {
  const sandbox = app();
  renderResults(sandbox);
  await type(sandbox, "enst00000269305");
  assert.deepStrictEqual(shown(sandbox), ["TP53"]);
  await type(sandbox, "NM_004333");
  assert.deepStrictEqual(shown(sandbox), ["BRAF"]);
  sandbox.state.currentResult = { run_id: "r1", fusion_evidence: FUSIONS };
  renderFusions(sandbox);
  await type(sandbox, "ENST00000389048");
  assert.deepStrictEqual(shown(sandbox), ["EML4::ALK"]);
}

async function testNoMatchesAndQueryIsNotRegexOrHtml() {
  const sandbox = app();
  renderResults(sandbox);
  for (const query of ["zzz", ".*", "(", "[a-z]+", "<img src=x onerror=bad()>"]) {
    await type(sandbox, query);
    assert.deepStrictEqual(shown(sandbox), [], query);
    assert.strictEqual(count(sandbox), "0 of 6", query);
    assert.ok(noMatches(sandbox), query);
  }
  assert.ok(walk(sandbox.elements.geneIndex).every((node) => node.innerHTML === ""));
  await type(sandbox, "");
  assert.strictEqual(shown(sandbox).length, 6);
  assert.ok(!noMatches(sandbox));
  assert.strictEqual(count(sandbox), "");
  assert.strictEqual(sandbox.elements.geneIndexFilterClear.hidden, true);
}

async function testEnterJumpsAndEscapeClears() {
  const sandbox = app();
  renderResults(sandbox);
  await type(sandbox, "alk");
  // Matches ALK (symbol) and EML4 (fusion); the exact name wins.
  assert.ok(press(sandbox, "Enter").defaultPrevented);
  assert.ok(sandbox.document.getElementById("gene-ALK").scrolled);
  assert.ok(!sandbox.document.getElementById("gene-EML4").scrolled);
  assert.deepStrictEqual(items(sandbox).filter((n) => n.classList.contains("active")).map((n) => n.title), ["ALK"]);
  // A name match beats an earlier hit via a fusion; Enter applies a pending (undebounced) query.
  input(sandbox).value = "eml";
  fire(input(sandbox), "input");
  press(sandbox, "Enter");
  assert.ok(sandbox.document.getElementById("gene-EML4").scrolled);
  assert.deepStrictEqual(items(sandbox).filter((n) => n.classList.contains("active")).map((n) => n.title), ["EML4"]);
  assert.ok(press(sandbox, "Escape").defaultPrevented);
  assert.strictEqual(input(sandbox).value, "");
  assert.strictEqual(shown(sandbox).length, 6);
  // The × button clears too.
  await type(sandbox, "kras");
  fire(sandbox.elements.geneIndexFilterClear, "click");
  assert.strictEqual(input(sandbox).value, "");
  assert.strictEqual(shown(sandbox).length, 6);
  // Enter with no match does nothing.
  await type(sandbox, "zzz");
  press(sandbox, "Enter");
}

async function testSectionsHiddenAndCollapsedSectionOpens() {
  const sandbox = app();
  sandbox.state.currentResult = { run_id: "r1", fusion_evidence: FUSIONS };
  renderFusions(sandbox);
  assert.strictEqual(input(sandbox).placeholder, "Filter fusions");
  assert.strictEqual(sandbox.elements.geneIndexFilterLabel.textContent, "Filter fusions");
  const allHeadings = [["Found in Lung", true], ["Found in other tumor types", true], ["No literature found", true]];
  assert.deepStrictEqual(headings(sandbox), allHeadings);
  await type(sandbox, "foo");
  assert.deepStrictEqual(headings(sandbox).map(([, visible]) => visible), [false, false, true]);
  const empty = sandbox.elements.resultsWindow.children[0].children.at(-1);
  assert.strictEqual(empty.children[1].hidden, true, "No literature found starts collapsed");
  press(sandbox, "Enter");
  assert.strictEqual(empty.children[1].hidden, false, "jumping opens the collapsed section");
  assert.ok(empty.children[1].children[0].scrolled);
  await type(sandbox, "");
  assert.deepStrictEqual(headings(sandbox), allHeadings);
}

async function testPerTabFiltersSurviveRedrawsAndResetOnNewRun() {
  const sandbox = app();
  sandbox.state.currentResult = { run_id: "r1", annotations: ANNOTATIONS, fusion_evidence: FUSIONS };
  renderResults(sandbox);
  assert.strictEqual(input(sandbox).placeholder, "Filter genes");
  await type(sandbox, "tp53");
  renderFusions(sandbox);
  assert.strictEqual(input(sandbox).value, "", "each tab has its own filter");
  assert.strictEqual(shown(sandbox).length, 4);
  await type(sandbox, "ret");
  // Fusion evidence polling replaces the result object and redraws.
  sandbox.state.currentResult = { ...sandbox.state.currentResult, fusion_evidence: [...FUSIONS] };
  renderFusions(sandbox);
  assert.strictEqual(input(sandbox).value, "ret");
  assert.deepStrictEqual(shown(sandbox), ["KIF5B::RET"]);
  assert.strictEqual(count(sandbox), "1 of 4");
  // A redraw that lands mid-debounce still applies the latest text.
  input(sandbox).value = "npm1";
  fire(input(sandbox), "input");
  renderFusions(sandbox);
  assert.deepStrictEqual(shown(sandbox), ["NPM1::ALK"]);
  await wait(DEBOUNCE_WAIT_MS);
  renderResults(sandbox);
  assert.strictEqual(input(sandbox).value, "tp53");
  assert.deepStrictEqual(shown(sandbox), ["TP53"]);
  // The No result tab starts unfiltered.
  sandbox.state.resultsViewMode = "noresult";
  sandbox.renderNoResultView([{ gene: "MYC" }, { gene: "MYCN" }]);
  assert.strictEqual(input(sandbox).value, "");
  assert.deepStrictEqual(shown(sandbox), ["MYC", "MYCN"]);
  await type(sandbox, "mycn");
  assert.deepStrictEqual(shown(sandbox), ["MYCN"]);
  // New run / Clear / shared run all go through resetGeneIndexFilters.
  sandbox.resetGeneIndexFilters();
  assert.strictEqual(input(sandbox).value, "");
  for (const render of [() => renderResults(sandbox), () => renderFusions(sandbox)]) {
    render();
    assert.strictEqual(input(sandbox).value, "");
    assert.ok(shown(sandbox).length > 2);
  }
  await type(sandbox, "ret");
  sandbox.clearResults();
  assert.strictEqual(input(sandbox).value, "");
  assert.strictEqual(sandbox.elements.geneIndexPanel.classList.contains("hidden"), true);
}

async function testObserverNotLeakedByFilteringOrTabSwitch() {
  const sandbox = app();
  sandbox.state.currentResult = { run_id: "r1", fusion_evidence: FUSIONS };
  renderResults(sandbox);
  const before = sandbox.observers.length;
  await type(sandbox, "alk");
  await type(sandbox, "");
  press(sandbox, "Escape");
  assert.strictEqual(sandbox.observers.length, before, "filtering creates no observers");
  const first = sandbox.observers.at(-1);
  renderFusions(sandbox);
  assert.ok(first.disconnected, "switching tabs disconnects the previous observer");
  assert.strictEqual(sandbox.observers.filter((o) => !o.disconnected).length, 1);
  // Active tracking still works on a filtered list.
  await type(sandbox, "ret");
  const card = sandbox.document.getElementById(items(sandbox)[1].dataset.targetId);
  sandbox.observers.at(-1).callback([{ isIntersecting: true, target: card }]);
  assert.ok(items(sandbox)[1].classList.contains("active"));
}

async function testHiddenEntriesNeverTakeTheActiveHighlight() {
  const sandbox = app();
  renderResults(sandbox);
  const observerCount = sandbox.observers.length;
  const observer = sandbox.observers.at(-1);
  const card = (gene) => sandbox.document.getElementById(`gene-${gene}`);
  card("ALK").getBoundingClientRect = () => ({ top: 0 });
  card("TP53").getBoundingClientRect = () => ({ top: 300 });
  const active = () => items(sandbox).filter((n) => n.classList.contains("active")).map((n) => n.title);
  observer.callback([{ isIntersecting: true, target: card("ALK") }, { isIntersecting: true, target: card("TP53") }]);
  assert.deepStrictEqual(active(), ["ALK"]);
  // The topmost on-screen card (ALK) is filtered out: the highlight moves to
  // the topmost on-screen card that still matches, never a hidden entry.
  await type(sandbox, "tp53");
  assert.deepStrictEqual(active(), ["TP53"]);
  // Scrolling the matching card away leaves only a filtered-out card on
  // screen: nothing is highlighted rather than the hidden ALK entry.
  observer.callback([{ isIntersecting: false, target: card("TP53") }]);
  assert.deepStrictEqual(active(), []);
  observer.callback([{ isIntersecting: true, target: card("ALK") }]);
  assert.deepStrictEqual(active(), []);
  await type(sandbox, "kras");
  assert.deepStrictEqual(active(), []);
  // Clearing the filter restores normal tracking.
  press(sandbox, "Escape");
  assert.deepStrictEqual(active(), ["ALK"]);
  assert.strictEqual(sandbox.observers.length, observerCount, "filtering creates no observers");
  // A hidden entry that was active before any intersection report loses it.
  const fresh = app();
  renderResults(fresh);
  items(fresh)[0].classList.add("active");
  await type(fresh, "tp53");
  assert.ok(!items(fresh)[0].classList.contains("active"));
}

async function testJumpMovesFocusToTheCard() {
  const sandbox = app();
  renderResults(sandbox);
  await type(sandbox, "braf");
  focusedElement = null;
  press(sandbox, "Enter");
  const braf = sandbox.document.getElementById("gene-BRAF");
  assert.strictEqual(focusedElement, braf, "Enter moves focus to the target card");
  assert.strictEqual(braf.getAttribute("tabindex"), "-1");
  assert.deepStrictEqual({ ...braf.focusOptions }, { preventScroll: true });
  assert.ok(braf.scrolled, "the card is still scrolled into view");
  // Mouse clicks on an entry behave the same way.
  press(sandbox, "Escape");
  items(sandbox).find((n) => n.title === "KRAS").click();
  assert.strictEqual(focusedElement, sandbox.document.getElementById("gene-KRAS"));
  // Fusion tab: the collapsed section opens and focus lands on its card.
  sandbox.state.currentResult = { run_id: "r1", fusion_evidence: FUSIONS };
  renderFusions(sandbox);
  await type(sandbox, "foo::bar");
  press(sandbox, "Enter");
  const empty = sandbox.elements.resultsWindow.children[0].children.at(-1);
  assert.strictEqual(empty.children[1].hidden, false);
  assert.strictEqual(focusedElement, empty.children[1].children[0]);
}

const tests = [testFiltersBySymbolCaseInsensitive, testFiltersByFusionWithAnySeparator,
  testFiltersBySubmittedInput, testNoMatchesAndQueryIsNotRegexOrHtml, testEnterJumpsAndEscapeClears,
  testSectionsHiddenAndCollapsedSectionOpens, testPerTabFiltersSurviveRedrawsAndResetOnNewRun,
  testObserverNotLeakedByFilteringOrTabSwitch, testHiddenEntriesNeverTakeTheActiveHighlight,
  testJumpMovesFocusToTheCard];
(async () => {
  for (const test of tests) {
    await test();
    console.log(`PASS ${test.name}`);
  }
  console.log(`${tests.length} gene index filter frontend test functions passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

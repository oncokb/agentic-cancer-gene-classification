"use strict";

const assert = require("assert");
const { loadApp, FakeElement, findById } = require("./openevidence_gate_harness");

const walk = (root) => [root, ...root.children.flatMap(walk)];
const hasClass = (node, name) => node.className.split(" ").includes(name);
const allText = (node) => node.textContent + node.children.map(allText).join(" ");
// Add only the browser operations this integration test exercises to the
// existing harness. The production rendering/sectioning/navigation run intact.
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

function app() {
  const sandbox = loadApp({ fetchImpl: async () => ({ ok: false }) });
  sandbox.document.getElementById = (id) => findById(sandbox.elements.resultsWindow, id);
  sandbox.observers = [];
  sandbox.IntersectionObserver = class {
    constructor(callback, options) {
      this.options = options;
      this.callback = callback;
      this.cards = [];
      sandbox.observers.push(this);
    }
    observe(card) { this.cards.push(card); }
    disconnect() { this.disconnected = true; }
  };
  return sandbox;
}

function fixture(tumor_type) {
  const item = (fusion, literature_scope, extra = {}) => ({
    fusion, tumor_type, literature_scope, pmids: ["1", "2"], ...extra,
  });
  return [
    item("FAILED::FIRST", "no_literature", { retrieval_status: "failed", pmids: [] }),
    item("OTHER::FIRST", tumor_type ? "other_or_unspecified" : "any_tumor"),
    item("NONE::FIRST", "no_literature", { pmids: [] }),
    item("MATCH::FIRST", tumor_type ? "requested_tumor" : "any_tumor"),
    item("OLD::PMIDS", null),
    item("OLD::COUNT", null, { pmids: [], retrieved_count: 3 }),
    item("OLD::EMPTY", null, { pmids: [] }),
    item("OLD::ABSENT", undefined, { pmids: [] }),
    item("OTHER::SECOND", tumor_type ? "other_or_unspecified" : "any_tumor"),
    item("FAILED::SCOPE", "requested_tumor", { retrieval_status: "failed" }),
  ];
}

function testSectioningAndOrder() {
  for (const tumor of ["Lung cancer", null]) {
    const sandbox = app();
    sandbox.renderFusionEvidenceView(fixture(tumor));
    const sections = sandbox.elements.resultsWindow.children[0].children;
    const expected = tumor ? [
      ["Found in Lung cancer (1)", ["MATCH::FIRST"]],
      ["Found in other tumor types (2)", ["OTHER::FIRST", "OTHER::SECOND"]],
    ] : [["Literature found (3)", ["OTHER::FIRST", "MATCH::FIRST", "OTHER::SECOND"]]];
    expected.push(
      ["Literature found — not classified by tumor type (2)", ["OLD::PMIDS", "OLD::COUNT"]],
      ["No literature found (3)", ["NONE::FIRST", "OLD::EMPTY", "OLD::ABSENT"]],
      ["Lookup failed (2)", ["FAILED::FIRST", "FAILED::SCOPE"]],
    );
    assert.deepStrictEqual(sections.map((section) => [
      section.children[0].children[0].textContent,
      section.children[1].children.map((card) => card.children[0].children[0].children[0].textContent),
    ]), expected);
    sections.forEach((section) => {
      const noLiterature = allText(section.children[0]).startsWith("No literature found");
      const toggle = section.children[0].children[0];
      assert.strictEqual(toggle.getAttribute("aria-expanded"), String(!noLiterature));
      assert.strictEqual(section.children[1].hidden, noLiterature);
      assert.strictEqual(section.tagName, "section");
      assert.strictEqual(section.children[0].tagName, "h3");
      assert.strictEqual(toggle.tagName, "button");
      assert.strictEqual(toggle.getAttribute("aria-controls"), section.children[1].id);
      section.children[1].children.forEach((card) => {
        assert.strictEqual(card.children[0].children[0].children[0].tagName, "h4");
      });
    });
  }
}

function testEmptySectionsOmitted() {
  for (const tumor of ["Lung cancer", null]) {
    const sandbox = app();
    sandbox.renderFusionEvidenceView([{ fusion: "A::B", tumor_type: tumor, literature_scope: "no_literature" }]);
    assert.strictEqual(sandbox.elements.resultsWindow.children[0].children.length, 1);
    assert.strictEqual(sandbox.elements.geneIndex.children[0].textContent, "No literature found");
    sandbox.renderFusionEvidenceView([]);
    assert.strictEqual(sandbox.elements.geneIndex.children.length, 0);
    assert.ok(sandbox.observers[0].disconnected);
  }
}

function testSidebarNavigationAndActiveTracking() {
  const sandbox = app();
  sandbox.renderFusionEvidenceView(fixture("Lung cancer"));
  const buttons = sandbox.elements.geneIndex.querySelectorAll(".gene-index-item");
  assert.strictEqual(buttons.length, 10);
  buttons.forEach((button) => {
    const card = sandbox.document.getElementById(button.dataset.targetId);
    assert.ok(card);
    assert.ok(button.title);
    assert.strictEqual(button.children[0].textContent, button.title);
    assert.strictEqual(button.children[1].textContent,
      button.title === "OLD::COUNT" ? "3" : button.title === "NONE::FIRST" ||
      button.title === "OLD::EMPTY" || button.title === "OLD::ABSENT" ||
      button.title === "FAILED::FIRST" ? "0" : "2");
    const section = card.closest();
    button.click();
    assert.strictEqual(section.querySelector(".fusion-section-toggle").getAttribute("aria-expanded"), "true");
    assert.strictEqual(section.children[1].hidden, false);
    assert.ok(card.scrolled);
  });
  const target = sandbox.document.getElementById(buttons[2].dataset.targetId);
  sandbox.observers[0].callback([{ isIntersecting: true, target, boundingClientRect: { top: 10 } }]);
  buttons.forEach((button, i) => assert.strictEqual(button.classList.contains("active"), i === 2));
  sandbox.rebuildGeneIndex([]);
  assert.ok(sandbox.observers[0].disconnected);
  // Different spellings sanitized to the same string still get unique targets.
  sandbox.renderFusionEvidenceView([{ fusion: "A::B" }, { fusion: "A--B" }]);
  const ids = sandbox.elements.geneIndex.querySelectorAll(".gene-index-item").map((b) => b.dataset.targetId);
  assert.strictEqual(new Set(ids).size, 2);
}

function testActiveTrackingAcrossObserverBatches() {
  const sandbox = app();
  sandbox.elements.resultsWindow.scrollHeight = 1000;
  sandbox.elements.resultsWindow.clientHeight = 500;
  sandbox.renderFusionEvidenceView(fixture("Lung cancer"));
  assert.strictEqual(sandbox.observers[0].options.root, null);
  const buttons = sandbox.elements.geneIndex.querySelectorAll(".gene-index-item");
  const first = sandbox.document.getElementById(buttons[0].dataset.targetId);
  const second = sandbox.document.getElementById(buttons[1].dataset.targetId);
  first.getBoundingClientRect = () => ({ top: 10 });
  second.getBoundingClientRect = () => ({ top: 100 });
  const observer = sandbox.observers[0];
  observer.callback([{ isIntersecting: true, target: first }]);
  observer.callback([{ isIntersecting: true, target: second }]);
  assert.ok(buttons[0].classList.contains("active"));
  assert.ok(!buttons[1].classList.contains("active"));
  observer.callback([{ isIntersecting: false, target: first }]);
  assert.ok(buttons[1].classList.contains("active"));
  assert.ok(!buttons[0].classList.contains("active"));
  sandbox.elements.resultsWindow.scrollHeight = 1000;
  sandbox.elements.resultsWindow.clientHeight = 1000;
  sandbox.renderFusionEvidenceView(fixture("Lung cancer"));
  assert.strictEqual(sandbox.observers.at(-1).options.root, null);
}

function testSharedPmidCountAndFutureScopes() {
  for (const tumor_type of ["Lung cancer", null]) {
    const sandbox = app();
    sandbox.renderFusionEvidenceView([
      { fusion: "OLD::COUNT", pmids: [], retrieved_count: 3, tumor_type },
      { fusion: "FUTURE::SCOPE", pmids: ["1"], literature_scope: "future_scope", tumor_type },
      { fusion: "FUTURE::EMPTY", pmids: [], literature_scope: "future_scope", tumor_type },
    ]);
    const buttons = sandbox.elements.geneIndex.querySelectorAll(".gene-index-item");
    const legacy = buttons.find((button) => button.title === "OLD::COUNT");
    const card = sandbox.document.getElementById(legacy.dataset.targetId);
    assert.strictEqual(legacy.children[1].textContent, "3");
    assert.strictEqual(card.querySelector(".status-pill").textContent, "3 PMIDs");
    const sections = sandbox.elements.resultsWindow.children[0].children;
    assert.strictEqual(sections.length, 1);
    assert.strictEqual(sections[0].children[0].children[0].textContent,
      "Literature found — not classified by tumor type (3)");
  }
}

function testExpansionPersistsAndResetsForNewResults() {
  const sandbox = app();
  const items = fixture("Lung cancer");
  sandbox.state.currentResult = { run_id: "first-run", fusion_evidence: items };
  const render = () => sandbox.renderFusionEvidenceView(sandbox.state.currentResult.fusion_evidence);
  const section = (label) => sandbox.elements.resultsWindow.children[0].children.find((node) =>
    allText(node.children[0]).startsWith(label));
  render();
  let empty = section("No literature found");
  assert.strictEqual(empty.children[1].hidden, true);
  empty.querySelector(".fusion-section-toggle").click();
  section("Found in Lung cancer").querySelector(".fusion-section-toggle").click();
  // Polling replaces the result object, retaining its run id.
  sandbox.state.currentResult = { ...sandbox.state.currentResult, fusion_evidence: [...items] };
  render();
  empty = section("No literature found");
  assert.strictEqual(empty.children[1].hidden, false);
  assert.strictEqual(section("Found in Lung cancer").children[1].hidden, true);
  // Sidebar expansion must also be remembered on the next render.
  empty.querySelector(".fusion-section-toggle").click();
  sandbox.elements.geneIndex.querySelectorAll(".gene-index-item")
    .find((button) => button.title === "NONE::FIRST").click();
  render();
  assert.strictEqual(section("No literature found").children[1].hidden, false);
  // New runs, explicit reloads, and anonymous new results restore defaults.
  sandbox.state.currentResult = { run_id: "second-run", fusion_evidence: items };
  render();
  assert.strictEqual(section("No literature found").children[1].hidden, true);
  assert.strictEqual(section("Found in Lung cancer").children[1].hidden, false);
  section("No literature found").querySelector(".fusion-section-toggle").click();
  sandbox.resetFusionEvidenceSections();
  render();
  assert.strictEqual(section("No literature found").children[1].hidden, true);
  sandbox.state.currentResult = { fusion_evidence: items };
  render();
  section("No literature found").querySelector(".fusion-section-toggle").click();
  sandbox.state.currentResult = { fusion_evidence: items };
  render();
  assert.strictEqual(section("No literature found").children[1].hidden, true);
}

function testHighlightsAndSafeCardContent() {
  const sandbox = app();
  const text = '<img src=x onerror="bad()">';
  sandbox.renderFusionEvidenceView([{
    fusion: text, tumor_type: text, literature_scope: "requested_tumor",
    requested_tumor_pmids: ["1"], pmids: ["1", "2"], well_supported: true,
    interpretation: text, evidence_cards: [{
      pmid: "1", title: text, abstract: text, journal: text, is_preprint: true,
      matched_via_alias: true, alias_matches: [{ alias: text, gene: "ALK" }],
      selected_reason: text, quote: text, requested_tumor_terms: [text, "lung"],
    }],
  }]);
  const root = sandbox.elements.resultsWindow;
  const content = allText(root);
  for (const expected of [text, `Mentions ${text} in 1 of 2 papers`,
    `Matched tumor terms: ${text}, lung`, "Well supported", "Preprint – not peer-reviewed", "Found via alias"]) {
    assert.ok(content.includes(expected), expected);
  }
  assert.ok(walk(root).every((node) => node.innerHTML === ""), "fusion API data must use DOM text");
  const paperLink = walk(root).find((node) => node.tagName === "a" && node.textContent === "PMID 1");
  assert.strictEqual(paperLink.className, "", "topline links keep the original plain-link appearance");
  assert.strictEqual(root.querySelectorAll(".citation-link").length, 2, "retrieved PMID pills retain their style");
  sandbox.renderFusionEvidenceView([{ fusion: "EML4::ALK", literature_scope: "no_literature" }]);
  assert.strictEqual(root.querySelectorAll(".fusion-partner-query-button").length, 2);
  sandbox.renderFusionEvidenceView([{ fusion: "EML4::ALK", retrieval_status: "failed" }]);
  assert.strictEqual(root.querySelectorAll(".fusion-partner-query-button").length, 0);
  sandbox.renderFusionEvidenceView([{ fusion: "A::B", tumor_type: "Lung", literature_scope: "requested_tumor" }]);
  assert.ok(allText(root).includes("Mentions Lung in 0 of 0 papers"));
}

function testResultsAndNoResultSidebarUnchanged() {
  const sandbox = app();
  const genes = [{ gene: "ALK" }, { gene: "LONG::GENE" }];
  sandbox.applyResultsViewMode(genes, [], []);
  for (const render of [() => sandbox.renderNoResultView(genes), () => sandbox.applyResultsViewMode(genes, [], [])]) {
    const previous = sandbox.observers.at(-1);
    render();
    assert.ok(previous.disconnected);
    const buttons = sandbox.elements.geneIndex.children;
    assert.deepStrictEqual(buttons.map((b) => [b.textContent, b.title, b.dataset.targetId]),
      genes.map(({ gene }) => [gene, gene, `gene-${gene}`]));
    buttons[1].click();
    const card = sandbox.document.getElementById("gene-LONG::GENE");
    assert.ok(card.scrolled);
    sandbox.observers.at(-1).callback([{ isIntersecting: true, target: card, boundingClientRect: { top: 0 } }]);
    assert.ok(buttons[1].classList.contains("active"));
    assert.ok(!buttons[0].classList.contains("active"));
  }
}

const tests = [testSectioningAndOrder, testEmptySectionsOmitted,
  testSidebarNavigationAndActiveTracking, testActiveTrackingAcrossObserverBatches,
  testHighlightsAndSafeCardContent, testSharedPmidCountAndFutureScopes,
  testExpansionPersistsAndResetsForNewResults, testResultsAndNoResultSidebarUnchanged];
for (const test of tests) {
  test();
  console.log(`PASS ${test.name}`);
}
console.log(`${tests.length} fusion evidence frontend test functions passed`);

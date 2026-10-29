"use strict";

const assert = require("assert");
const { loadApp, FakeElement, findById } = require("./openevidence_gate_harness");

const walk = (root) => [root, ...root.children.flatMap(walk)];
const hasClass = (node, name) => node.className.split(" ").includes(name);
const byClass = (root, name) => walk(root).filter((node) => hasClass(node, name));

// The production section renderer builds a real element tree; the base
// harness's fake querySelector cannot walk it, so add the two lookups the
// section markup relies on (same helpers as test_fusion_evidence_sections.js).
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

function app(fetchImpl = async () => ({ ok: false })) {
  const sandbox = loadApp({ fetchImpl });
  sandbox.document.getElementById = (id) => findById(sandbox.elements.resultsWindow, id);
  sandbox.observers = [];
  sandbox.IntersectionObserver = class {
    constructor(callback, options) {
      this.options = options;
      this.callback = callback;
      sandbox.observers.push(this);
    }
    observe() {}
    disconnect() {}
  };
  return sandbox;
}

function failedAndResolved() {
  return [
    { fusion: "KEEP::ONE", tumor_type: "LUAD", literature_scope: "requested_tumor", pmids: ["1"] },
    {
      fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048", tumor_type: "LUAD",
      retrieval_status: "failed", pmids: [],
    },
    { fusion: "EML4::ALK", tumor_type: "COAD", literature_scope: "requested_tumor", pmids: ["2"] },
  ];
}

function renderForRetry(sandbox, items = failedAndResolved()) {
  sandbox.state.currentResult = {
    run_id: "run-1", annotations: [{ gene: "ALK" }], fusion_evidence: items,
  };
  sandbox.state.resultsViewMode = "fusion";
  sandbox.state.fusionEvidenceStatus = "complete";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
}

function retryButton(sandbox) {
  return byClass(sandbox.elements.resultsWindow, "retry-failed-fusions")[0];
}

function testButtonOnlyWithFailedItemsAndPermission() {
  let sandbox = app();
  sandbox.state.canModifyRun = false;
  renderForRetry(sandbox);
  assert.strictEqual(retryButton(sandbox), undefined, "shared viewers must not see retry");
  assert.ok(byClass(sandbox.elements.resultsWindow, "fusion-evidence-section").length > 0);

  sandbox = app();
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox);
  const button = retryButton(sandbox);
  assert.ok(button, "owner sees retry when failed lookups exist");
  assert.strictEqual(button.textContent, "Retry failed lookups");
  assert.strictEqual(button.disabled, false);

  sandbox = app();
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox, failedAndResolved().filter((item) => item.retrieval_status !== "failed"));
  assert.strictEqual(retryButton(sandbox), undefined, "no failed items means no retry control");

  sandbox = app();
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox);
  sandbox.state.fusionEvidenceStatus = "running";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
  assert.strictEqual(retryButton(sandbox).disabled, true, "retry is disabled while a retry runs");
}

async function testRetrySendsOnlyFailedFusionsAndMergesInPlace() {
  const calls = [];
  const sandbox = app(async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/fusion-evidence/jobs" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-1", status_url: "/v1/fusion-evidence/jobs/job-1" }),
        text: async () => "",
      };
    }
    if (url === "/v1/fusion-evidence/jobs/job-1") {
      return {
        ok: true, status: 200,
        json: async () => ({
          status: "complete", fusions_total: 1, fusions_completed: 1,
          fusion_evidence: [{
            fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048",
            tumor_type: "LUAD", retrieval_status: "ok", literature_scope: "requested_tumor",
            pmids: ["3"],
          }],
        }),
        text: async () => "",
      };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  sandbox.state.canModifyRun = true;
  const original = failedAndResolved();
  renderForRetry(sandbox, original);

  await sandbox.retryFailedFusionEvidence();

  const posts = calls.filter((call) => call.options.method === "POST");
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(posts[0].url, "/v1/fusion-evidence/jobs");
  const body = JSON.parse(posts[0].options.body);
  assert.strictEqual(body.run_id, "run-1");
  assert.deepStrictEqual(body.fusions, [{
    fusion: "ENST00000318522::ENST00000389048", tumor_type: "LUAD",
  }], "retry must submit only failed fusions, using the submitted spelling");
  assert.ok(calls.some((call) => call.url === "/v1/fusion-evidence/jobs/job-1"));

  const merged = sandbox.state.currentResult.fusion_evidence;
  assert.strictEqual(merged.length, 3, "non-matching items are kept");
  assert.strictEqual(JSON.stringify(merged.map((item) =>
    [item.fusion, item.tumor_type, item.retrieval_status])), JSON.stringify([
    ["KEEP::ONE", "LUAD", null],
    ["EML4::ALK", "LUAD", "ok"],
    ["EML4::ALK", "COAD", null],
  ]));
  assert.strictEqual(JSON.stringify(merged[1].pmids), JSON.stringify(["3"]),
    "matching item is replaced in place");
  assert.strictEqual(sandbox.state.fusionEvidenceStatus, "complete");
}

async function testRetryErrorKeepsOldItems() {
  const sandbox = app(async (url, options = {}) => {
    if (url === "/v1/fusion-evidence/jobs" && options.method === "POST") {
      return { ok: false, status: 403, statusText: "Forbidden", text: async () => "Not allowed" };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox);

  await sandbox.retryFailedFusionEvidence();

  const items = sandbox.state.currentResult.fusion_evidence;
  assert.strictEqual(items.length, 3);
  assert.strictEqual(items[1].retrieval_status, "failed");
  assert.ok(sandbox.elements.messageBox.textContent.includes("Not allowed"));
}

async function testRetryPollingErrorKeepsOriginalItems() {
  let polls = 0;
  const sandbox = app(async (url, options = {}) => {
    if (url === "/v1/fusion-evidence/jobs" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-2", status_url: "/v1/fusion-evidence/jobs/job-2" }),
        text: async () => "",
      };
    }
    if (url === "/v1/fusion-evidence/jobs/job-2") {
      polls += 1;
      if (polls === 1) {
        return {
          ok: true, status: 200,
          json: async () => ({
            status: "running", fusions_total: 1, fusions_completed: 1,
            fusion_evidence: [{
              fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048",
              tumor_type: "LUAD", retrieval_status: "ok", pmids: ["3"],
            }],
          }),
          text: async () => "",
        };
      }
      return { ok: false, status: 503, statusText: "Service Unavailable", text: async () => "" };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  sandbox.sleep = async () => {};
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox);

  await sandbox.retryFailedFusionEvidence();

  const items = sandbox.state.currentResult.fusion_evidence;
  assert.strictEqual(items.length, 3);
  assert.strictEqual(items[1].retrieval_status, "failed", "staged poll results must not be applied");
  assert.strictEqual(JSON.stringify(items[1].pmids), JSON.stringify([]));
  assert.ok(sandbox.elements.messageBox.textContent.includes("Service Unavailable"));
}

function testDuplicateIncomingKeysMergeOnce() {
  const sandbox = app();
  const merged = sandbox.mergeFusionEvidence(
    [{ fusion: "A::B", tumor_type: "LUAD", retrieval_status: "failed" }],
    [
      { fusion: "A::B", tumor_type: "LUAD", retrieval_status: "ok", pmids: ["1"] },
      { fusion: "A::B", tumor_type: "LUAD", retrieval_status: "ok", pmids: ["2"] },
      { fusion: "NEW::ONE", tumor_type: "LUAD", retrieval_status: "ok", pmids: ["3"] },
      { fusion: "NEW::ONE", tumor_type: "LUAD", retrieval_status: "ok", pmids: ["4"] },
    ],
  );
  assert.strictEqual(JSON.stringify(merged.map((item) => [item.fusion, item.pmids])),
    JSON.stringify([["A::B", ["2"]], ["NEW::ONE", ["4"]]]));
}

function testSidebarFilterSurvivesItemMovingSections() {
  const sandbox = app();
  sandbox.bindGeneIndexFilter();
  const items = () => sandbox.elements.geneIndex.children.filter((node) =>
    hasClass(node, "gene-index-item") && !node.hidden).map((node) => node.title);
  const headings = () => sandbox.elements.geneIndex.children
    .filter((node) => hasClass(node, "fusion-index-section"))
    .map((node) => node.textContent);
  sandbox.state.canModifyRun = true;
  renderForRetry(sandbox, [{
    fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048",
    tumor_type: "LUAD", retrieval_status: "failed", pmids: [],
  }]);
  sandbox.elements.geneIndexFilter.value = "eml4";
  sandbox.setGeneIndexFilter("eml4");
  sandbox.applyGeneIndexFilter();
  assert.strictEqual(JSON.stringify(items()), JSON.stringify(["EML4::ALK"]));
  assert.strictEqual(sandbox.elements.geneIndexFilter.value, "eml4");

  const retried = sandbox.mergeFusionEvidence(sandbox.state.currentResult.fusion_evidence, [{
    fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048",
    tumor_type: "LUAD", retrieval_status: "ok", literature_scope: "requested_tumor",
    pmids: ["3"],
  }]);
  sandbox.state.currentResult = { ...sandbox.state.currentResult, fusion_evidence: retried };
  sandbox.renderAnnotationResult(sandbox.state.currentResult);

  assert.strictEqual(sandbox.elements.geneIndexFilter.value, "eml4", "filter text persists");
  assert.strictEqual(JSON.stringify(items()), JSON.stringify(["EML4::ALK"]));
  assert.deepStrictEqual(headings(), ["Found in LUAD"], "item now sits under its new section");
}

const tests = [testButtonOnlyWithFailedItemsAndPermission, testRetrySendsOnlyFailedFusionsAndMergesInPlace,
  testRetryErrorKeepsOldItems, testRetryPollingErrorKeepsOriginalItems, testDuplicateIncomingKeysMergeOnce,
  testSidebarFilterSurvivesItemMovingSections];
(async () => {
  for (const test of tests) {
    await test();
    console.log(`PASS ${test.name}`);
  }
  console.log(`${tests.length} fusion evidence retry frontend test functions passed`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

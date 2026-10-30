"use strict";

// Per-card "Re-run" on fusion evidence: one fusion, force_refresh, merged in
// place through the #152 retry path and guarded by the #153 stale-job token.

const assert = require("assert");
const { createTracker } = require("../../src/static/tracking.js");
const { loadApp, FakeElement, findById } = require("./openevidence_gate_harness");

const walk = (root) => [root, ...root.children.flatMap(walk)];
const hasClass = (node, name) => node.className.split(" ").includes(name);
const byClass = (root, name) => walk(root).filter((node) => hasClass(node, name));
const settle = async () => {
  for (let i = 0; i < 10; i++) await new Promise((resolve) => setImmediate(resolve));
};

// Same element-tree helpers as test_fusion_evidence_retry.js.
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
  sandbox.IntersectionObserver = class { observe() {} disconnect() {} };
  sandbox.sleep = async () => {};
  return sandbox;
}

// One card in every section the fusion view can show.
function everySection() {
  return [
    { fusion: "KEEP::ONE", tumor_type: "LUAD", literature_scope: "requested_tumor", pmids: ["1"] },
    { fusion: "OTHER::TT", tumor_type: "LUAD", literature_scope: "other_or_unspecified", pmids: ["2"] },
    { fusion: "ANY::TT", literature_scope: "any_tumor", pmids: ["3"] },
    {
      fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048", tumor_type: "LUAD",
      literature_scope: "no_literature", pmids: [],
    },
    { fusion: "FAIL::ED", tumor_type: "LUAD", retrieval_status: "failed", pmids: [] },
    { fusion: "PART::IAL", tumor_type: "LUAD", partial: true, literature_scope: "requested_tumor", pmids: ["4"] },
  ];
}

function render(sandbox, items = everySection(), runId = "run-1") {
  sandbox.state.currentResult = { run_id: runId, annotations: [{ gene: "ALK" }], fusion_evidence: items };
  sandbox.state.resultsViewMode = "fusion";
  sandbox.state.fusionEvidenceStatus = "complete";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
}

function cards(sandbox) {
  return byClass(sandbox.elements.resultsWindow, "fusion-evidence-card");
}

function cardFor(sandbox, fusion) {
  return cards(sandbox).find((card) => byClass(card, "fusion-section-card-name")[0].textContent === fusion);
}

function rerunButton(card) {
  return byClass(card, "rerun-fusion-evidence")[0];
}

function sectionOf(card) {
  return byClass(card.closest(), "fusion-section-toggle")[0].textContent;
}

function sectionLabels(sandbox) {
  return byClass(sandbox.elements.resultsWindow, "fusion-section-toggle").map((node) => node.textContent);
}

function sidebarHeadings(sandbox) {
  return sandbox.elements.geneIndex.children
    .filter((node) => hasClass(node, "fusion-index-section"))
    .map((node) => node.textContent);
}

// Answers POST with job-1 and every poll with `poll()` (which may return a pending promise).
function jobFetch(calls, poll) {
  return async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/fusion-evidence/jobs" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-1", status_url: "/v1/fusion-evidence/jobs/job-1" }),
        text: async () => "",
      };
    }
    if (url === "/v1/fusion-evidence/jobs/job-1") return poll();
    throw new Error(`unexpected fetch ${url}`);
  };
}

function statusResponse(status) {
  return { ok: true, status: 200, json: async () => status, text: async () => "" };
}

const RERUN_RESULT = {
  fusion: "EML4::ALK", input: "ENST00000318522::ENST00000389048", tumor_type: "LUAD",
  retrieval_status: "ok", literature_scope: "requested_tumor", pmids: ["7", "8"],
  requested_tumor_pmids: ["7"],
};

function testHiddenWithoutPermissionAndOnEveryCardWithIt() {
  let sandbox = app();
  sandbox.state.canModifyRun = false;
  render(sandbox);
  assert.strictEqual(cards(sandbox).length, 6);
  assert.strictEqual(byClass(sandbox.elements.resultsWindow, "rerun-fusion-evidence").length, 0,
    "shared viewers must not see Re-run");

  sandbox = app();
  sandbox.state.canModifyRun = true;
  render(sandbox);
  assert.strictEqual(sectionLabels(sandbox).length, 5, "fixture covers every section kind");
  cards(sandbox).forEach((card) => {
    const button = rerunButton(card);
    assert.ok(button, "every card in every section offers Re-run");
    assert.strictEqual(button.textContent, "Re-run");
    assert.strictEqual(button.disabled, false);
  });
}

async function testRerunUpdatesOnlyThatCardAndMovesSections() {
  const calls = [];
  const sandbox = app(jobFetch(calls, async () => statusResponse({
    status: "complete", fusions_total: 1, fusions_completed: 1, fusion_evidence: [RERUN_RESULT],
  })));
  const tracker = createTracker({
    IntersectionObserver: class { observe() {} unobserve() {} disconnect() {} },
    setTimeout: () => 0, clearTimeout: () => {},
  });
  tracker.setEnabled(true);
  sandbox.window.ACGCTracking = tracker;
  sandbox.bindGeneIndexFilter();
  sandbox.state.canModifyRun = true;
  const original = everySection();
  render(sandbox, original);
  assert.strictEqual(sectionOf(cardFor(sandbox, "EML4::ALK")), "No literature found (1)");
  assert.ok(sectionLabels(sandbox).includes("Found in LUAD (2)"));
  const others = JSON.stringify(original.filter((item) => item.fusion !== "EML4::ALK"));

  rerunButton(cardFor(sandbox, "EML4::ALK")).click();
  await settle();

  const posts = calls.filter((call) => call.options.method === "POST");
  assert.strictEqual(posts.length, 1);
  assert.deepStrictEqual(JSON.parse(posts[0].options.body), {
    fusions: [{ fusion: "ENST00000318522::ENST00000389048", tumor_type: "LUAD" }],
    mode: sandbox.elements.annotateMode.value || "full",
    run_id: "run-1",
    force_refresh: true,
  }, "re-run submits only this fusion, with its submitted spelling, bypassing the cache");

  const merged = sandbox.state.currentResult.fusion_evidence;
  assert.strictEqual(merged.length, 6);
  assert.strictEqual(JSON.stringify(merged[3]), JSON.stringify(RERUN_RESULT), "card replaced in place");
  assert.strictEqual(JSON.stringify(merged.filter((item) => item.fusion !== "EML4::ALK")), others,
    "every other card is untouched");

  const card = cardFor(sandbox, "EML4::ALK");
  assert.strictEqual(sectionOf(card), "Found in LUAD (3)", "card moved to its new section");
  assert.ok(!sectionLabels(sandbox).some((label) => label.startsWith("No literature found")),
    "emptied section disappears");
  assert.ok(sidebarHeadings(sandbox).includes("Found in LUAD"));
  assert.ok(!sidebarHeadings(sandbox).includes("No literature found"));
  assert.strictEqual(rerunButton(card).textContent, "Re-run");
  assert.strictEqual(rerunButton(card).disabled, false);
  assert.strictEqual(card.getAttribute("aria-busy"), null);
  assert.strictEqual(sandbox.state.fusionEvidenceStatus, "complete");
  assert.strictEqual(sandbox.elements.tabFusionEvidenceView.textContent, "Fusion evidence (6)");

  const actions = tracker._debug().queue.filter((event) => event.type === "action");
  assert.strictEqual(actions.length, 1);
  assert.strictEqual(actions[0].state, "retry");
  assert.strictEqual(actions[0].surface, "fusion_evidence");
  assert.strictEqual(actions[0].section, "fusion_card");
  assert.strictEqual(actions[0].subject_kind, "fusion");
  assert.strictEqual(actions[0].subject_id, "EML4::ALK", "canonical fusion survives the tracker's validation");
}

async function testFailureLeavesCardUnchangedWithInlineError() {
  for (const failure of [
    () => statusResponse({ status: "failed", error: "PubMed timed out" }),
    () => ({ ok: false, status: 503, statusText: "PubMed timed out", text: async () => "" }),
  ]) {
    const calls = [];
    const sandbox = app(jobFetch(calls, async () => failure()));
    sandbox.state.canModifyRun = true;
    const original = everySection();
    render(sandbox, original);
    const before = JSON.stringify(sandbox.state.currentResult);

    rerunButton(cardFor(sandbox, "FAIL::ED")).click();
    await settle();

    assert.strictEqual(JSON.stringify(sandbox.state.currentResult), before, "evidence left unchanged");
    const card = cardFor(sandbox, "FAIL::ED");
    assert.strictEqual(sectionOf(card), "Lookup failed (1)");
    const errors = byClass(sandbox.elements.resultsWindow, "fusion-rerun-error");
    assert.strictEqual(errors.length, 1, "only the re-run card shows the error");
    assert.strictEqual(byClass(card, "fusion-rerun-error")[0], errors[0]);
    assert.ok(errors[0].textContent.includes("PubMed timed out"));
    assert.strictEqual(errors[0].getAttribute("role"), "alert");
    assert.strictEqual(rerunButton(card).disabled, false, "can try again");
    assert.strictEqual(rerunButton(card).textContent, "Re-run");
    assert.strictEqual(sandbox.state.fusionEvidenceStatus, "complete");
  }
}

async function testStaleRerunAfterSwitchingRunsIsDropped() {
  for (const switchRun of [
    (sandbox) => {
      sandbox.state.currentResult = {
        run_id: "run-2", annotations: [{ gene: "BRAF" }],
        fusion_evidence: [{ fusion: "KIAA1549::BRAF", tumor_type: "LGG", pmids: ["9"] }],
      };
      sandbox.state.fusionEvidenceStatus = "complete";
      sandbox.setMessage("Shared run restored.", "info");
    },
    (sandbox) => sandbox.clearResults(),
  ]) {
    for (const lateStatus of [
      { status: "complete", fusions_total: 1, fusions_completed: 1, fusion_evidence: [RERUN_RESULT] },
      { status: "failed", error: "upstream timeout" },
    ]) {
      let release;
      const late = new Promise((resolve) => { release = resolve; });
      const sandbox = app(jobFetch([], () => late));
      sandbox.state.canModifyRun = true;
      render(sandbox);

      const rerun = sandbox.rerunFusionEvidence(sandbox.state.currentResult.fusion_evidence[3]);
      await settle();
      switchRun(sandbox);
      const before = JSON.stringify(sandbox.state.currentResult);
      const statusBefore = sandbox.state.fusionEvidenceStatus;
      const messageBefore = sandbox.elements.messageBox.textContent;
      release(statusResponse(lateStatus));
      await rerun;

      assert.strictEqual(JSON.stringify(sandbox.state.currentResult), before, "late re-run left the new run alone");
      assert.strictEqual(sandbox.state.fusionEvidenceStatus, statusBefore);
      assert.strictEqual(sandbox.elements.messageBox.textContent, messageBefore);
      if (sandbox.state.currentResult) {
        sandbox.renderAnnotationResult(sandbox.state.currentResult);
        const buttons = byClass(sandbox.elements.resultsWindow, "rerun-fusion-evidence");
        assert.ok(buttons.length > 0);
        buttons.forEach((button) => {
          assert.strictEqual(button.textContent, "Re-run", "no running state leaks into the new run");
          assert.strictEqual(button.disabled, false);
        });
        assert.strictEqual(byClass(sandbox.elements.resultsWindow, "fusion-rerun-error").length, 0);
      }
    }
  }
}

async function testDisabledWhileRunningAndNoOverlappingJobs() {
  let release;
  const late = new Promise((resolve) => { release = resolve; });
  const calls = [];
  const sandbox = app(jobFetch(calls, () => late));
  sandbox.state.canModifyRun = true;
  render(sandbox);

  rerunButton(cardFor(sandbox, "FAIL::ED")).click();
  await settle();

  const running = cardFor(sandbox, "FAIL::ED");
  assert.strictEqual(rerunButton(running).textContent, "Re-running...");
  assert.strictEqual(running.getAttribute("aria-busy"), "true");
  cards(sandbox).forEach((card) => assert.strictEqual(rerunButton(card).disabled, true,
    "every Re-run is disabled while a fusion-evidence job runs"));
  const sectionRetry = byClass(sandbox.elements.resultsWindow, "retry-failed-fusions")[0];
  assert.strictEqual(sectionRetry.disabled, true, "section retry cannot overlap the re-run");
  assert.strictEqual(sectionRetry.textContent, "Retry failed lookups");
  assert.strictEqual(rerunButton(cardFor(sandbox, "KEEP::ONE")).textContent, "Re-run");

  // Even if invoked directly, neither path starts a second job for the run.
  await sandbox.rerunFusionEvidence(sandbox.state.currentResult.fusion_evidence[4]);
  await sandbox.retryFailedFusionEvidence();
  assert.strictEqual(calls.filter((call) => call.options.method === "POST").length, 1);

  release(statusResponse({
    status: "complete", fusions_total: 1, fusions_completed: 1,
    fusion_evidence: [{ fusion: "FAIL::ED", tumor_type: "LUAD", retrieval_status: "ok",
      literature_scope: "requested_tumor", pmids: ["5"] }],
  }));
  await settle();

  cards(sandbox).forEach((card) => assert.strictEqual(rerunButton(card).disabled, false));
  assert.strictEqual(sectionOf(cardFor(sandbox, "FAIL::ED")), "Found in LUAD (3)");
}

function completedWith(entry) {
  return () => statusResponse({ status: "complete", fusions_total: 1, fusions_completed: 1, fusion_evidence: [entry] });
}

async function rerunCard(sandbox, fusion) {
  rerunButton(cardFor(sandbox, fusion)).click();
  await settle();
}

async function testCompletedJobWithBadLookupKeepsGoodCard() {
  for (const [entry, expected] of [
    [{ fusion: "KEEP::ONE", tumor_type: "LUAD", retrieval_status: "failed", pmids: [],
      interpretation: "PubMed search failed (timeout)" }, "Re-run failed: PubMed search failed (timeout)"],
    [{ fusion: "KEEP::ONE", tumor_type: "LUAD", partial: true, literature_scope: "requested_tumor", pmids: [] },
      "Re-run failed: PubMed lookup was incomplete (partial result)."],
  ]) {
    const sandbox = app(jobFetch([], completedWith(entry)));
    sandbox.state.canModifyRun = true;
    render(sandbox);
    sandbox.setMessage("before", "info");
    const before = JSON.stringify(sandbox.state.currentResult);

    await rerunCard(sandbox, "KEEP::ONE");

    assert.strictEqual(JSON.stringify(sandbox.state.currentResult), before, "prior evidence is kept");
    const card = cardFor(sandbox, "KEEP::ONE");
    assert.strictEqual(sectionOf(card), "Found in LUAD (2)");
    assert.strictEqual(JSON.stringify(sandbox.state.currentResult.fusion_evidence[0].pmids), JSON.stringify(["1"]));
    assert.ok(byClass(card, "status-pill").some((pill) => pill.textContent === "1 PMID"), "card still shows prior PMIDs");
    const errors = byClass(sandbox.elements.resultsWindow, "fusion-rerun-error");
    assert.strictEqual(errors.length, 1);
    assert.strictEqual(byClass(card, "fusion-rerun-error")[0], errors[0]);
    assert.strictEqual(errors[0].textContent, expected);
    assert.ok(!sandbox.elements.messageBox.textContent.includes("complete"), "no success message");
    assert.strictEqual(rerunButton(card).disabled, false);
  }
}

async function testFailedCardReplacedByPartialOrFailed() {
  // failed -> partial: the partial result replaces the card, no error.
  let sandbox = app(jobFetch([], completedWith({
    fusion: "FAIL::ED", tumor_type: "LUAD", partial: true, literature_scope: "requested_tumor", pmids: ["5"],
  })));
  sandbox.state.canModifyRun = true;
  render(sandbox);
  await rerunCard(sandbox, "FAIL::ED");
  let replaced = sandbox.state.currentResult.fusion_evidence[4];
  assert.strictEqual(replaced.partial, true);
  assert.strictEqual(JSON.stringify(replaced.pmids), JSON.stringify(["5"]));
  assert.strictEqual(sectionOf(cardFor(sandbox, "FAIL::ED")), "Found in LUAD (3)");
  assert.strictEqual(byClass(sandbox.elements.resultsWindow, "fusion-rerun-error").length, 0);
  assert.ok(sandbox.elements.messageBox.textContent.includes("partial"));

  // failed -> failed: the newer failure replaces the card and is shown inline.
  sandbox = app(jobFetch([], completedWith({
    fusion: "FAIL::ED", tumor_type: "LUAD", retrieval_status: "failed", pmids: [],
    interpretation: "PubMed search failed (HTTP 503)",
  })));
  sandbox.state.canModifyRun = true;
  render(sandbox);
  await rerunCard(sandbox, "FAIL::ED");
  replaced = sandbox.state.currentResult.fusion_evidence[4];
  assert.strictEqual(replaced.interpretation, "PubMed search failed (HTTP 503)", "updated failure stored");
  const card = cardFor(sandbox, "FAIL::ED");
  assert.strictEqual(sectionOf(card), "Lookup failed (1)");
  assert.strictEqual(byClass(card, "fusion-rerun-error")[0].textContent,
    "Re-run failed: PubMed search failed (HTTP 503)");
  assert.ok(!sandbox.elements.messageBox.textContent.includes("complete"));
}

function testMergeNeverReplacesSucceededWithFailedOrPartial() {
  const sandbox = app();
  const good = { fusion: "A::B", tumor_type: "LUAD", retrieval_status: "ok", pmids: ["1"] };
  const failed = { fusion: "C::D", tumor_type: "LUAD", retrieval_status: "failed", pmids: [] };
  const partial = { fusion: "E::F", tumor_type: "LUAD", partial: true, pmids: ["2"] };
  const merged = sandbox.mergeFusionEvidence([good, failed, partial], [
    { ...good, retrieval_status: "failed", pmids: [] },
    { ...failed, retrieval_status: "ok", pmids: ["3"] },
    { ...partial, retrieval_status: "failed", partial: false, pmids: [] },
  ]);
  assert.strictEqual(JSON.stringify(merged.map((item) => [item.fusion, item.retrieval_status, item.pmids])),
    JSON.stringify([["A::B", "ok", ["1"]], ["C::D", "ok", ["3"]], ["E::F", "failed", []]]));
  assert.strictEqual(sandbox.mergeFusionEvidence([good], [{ ...good, partial: true, pmids: [] }])[0], good);
}

const tests = [testHiddenWithoutPermissionAndOnEveryCardWithIt, testRerunUpdatesOnlyThatCardAndMovesSections,
  testFailureLeavesCardUnchangedWithInlineError, testStaleRerunAfterSwitchingRunsIsDropped,
  testDisabledWhileRunningAndNoOverlappingJobs, testCompletedJobWithBadLookupKeepsGoodCard,
  testFailedCardReplacedByPartialOrFailed, testMergeNeverReplacesSucceededWithFailedOrPartial];
(async () => {
  for (const test of tests) {
    await test();
    console.log(`PASS ${test.name}`);
  }
  console.log(`${tests.length} fusion evidence re-run frontend test functions passed`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

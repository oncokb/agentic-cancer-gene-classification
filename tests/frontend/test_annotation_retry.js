"use strict";

const assert = require("assert");
const { loadApp, FakeElement } = require("./openevidence_gate_harness");

const walk = (root) => [root, ...root.children.flatMap(walk)];
const hasClass = (node, name) => node.className.split(" ").includes(name);
const byClass = (root, name) => walk(root).filter((node) => hasClass(node, name));

FakeElement.prototype.querySelectorAll = function (selector) {
  return walk(this).slice(1).filter((node) => hasClass(node, selector.slice(1)));
};
const originalQuerySelector = FakeElement.prototype.querySelector;
FakeElement.prototype.querySelector = function (selector) {
  return this.querySelectorAll(selector)[0] || originalQuerySelector.call(this, selector);
};
FakeElement.prototype.click = function () {
  (this._listeners.get("click") || []).forEach((handler) => handler());
};

function app(fetchImpl = async () => ({ ok: false })) {
  return loadApp({ fetchImpl });
}

function renderNoResult(sandbox, annotations) {
  sandbox.state.canModifyRun = true;
  sandbox.state.resultsViewMode = "noresult";
  sandbox.state.currentResult = {
    run_id: "run-1",
    fusions_processed: 1,
    genes_annotated: annotations.length,
    annotations,
    fusion_evidence: [],
  };
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
}

const retryErrorsButton = (sandbox) =>
  byClass(sandbox.elements.resultsWindow, "retry-failed-annotations")[0];
const recheckButton = (sandbox) =>
  byClass(sandbox.elements.resultsWindow, "recheck-no-result-genes")[0];

const transient = { gene: "ALK", insufficient_evidence: true, error: "Synthesis error: timeout" };
const deterministic = {
  gene: "BAD", insufficient_evidence: true,
  error: "'BAD' is not a valid human Ensembl ID.",
};
const noResult = { gene: "BRAF", insufficient_evidence: true };

function testButtonsGateOnPermissionAndKind() {
  let sandbox = app();
  renderNoResult(sandbox, [transient, noResult]);
  sandbox.state.canModifyRun = false;
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
  assert.strictEqual(retryErrorsButton(sandbox), undefined, "shared viewers see no retry");
  assert.strictEqual(recheckButton(sandbox), undefined, "shared viewers see no re-check");

  sandbox = app();
  renderNoResult(sandbox, [transient, noResult]);
  assert.strictEqual(retryErrorsButton(sandbox).textContent, "Retry failed annotations");
  assert.strictEqual(recheckButton(sandbox).textContent, "Re-check no-result genes");

  sandbox = app();
  renderNoResult(sandbox, [deterministic]);
  assert.strictEqual(retryErrorsButton(sandbox), undefined,
    "deterministic rejections are not retryable");

  sandbox = app();
  renderNoResult(sandbox, [noResult]);
  assert.strictEqual(retryErrorsButton(sandbox), undefined);
  assert.ok(recheckButton(sandbox), "no-result genes can be re-checked");

  sandbox = app();
  renderNoResult(sandbox, [transient]);
  sandbox.state.annotationRetryStatus = "running";
  sandbox.state.annotationRetryKind = "errors";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
  assert.strictEqual(retryErrorsButton(sandbox).disabled, true);
  assert.strictEqual(retryErrorsButton(sandbox).textContent, "Retrying...");
}

async function testRetryErrorsPostsGenesAndMergesInPlace() {
  const calls = [];
  const sandbox = app(async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/runs/run-1/retry" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-1", status_url: "/v1/annotate/jobs/job-1" }),
        text: async () => "",
      };
    }
    if (url === "/v1/annotate/jobs/job-1") {
      return {
        ok: true, status: 200,
        json: async () => ({
          status: "complete", genes_total: 1, genes_completed: 1,
          result: {
            annotations: [{ gene: "ALK", cancer_associated: true, gene_summary: "Retried" }],
          },
        }),
        text: async () => "",
      };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  sandbox.state.canModifyRun = true;
  sandbox.state.currentRunInputs = [{ fusion: "EML4::ALK", tumor_type: "LUAD" }];
  const keep = { gene: "TP53", cancer_associated: true };
  renderNoResult(sandbox, [transient, keep]);
  sandbox.state.resultsViewMode = "noresult";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);

  await sandbox.retryFailedAnnotations("errors");

  const posts = calls.filter((call) => call.options.method === "POST");
  assert.strictEqual(posts.length, 1);
  assert.strictEqual(posts[0].url, "/v1/runs/run-1/retry");
  assert.deepStrictEqual(JSON.parse(posts[0].options.body), {
    kind: "errors",
    inputs: [{ fusion: "ALK", tumor_type: "LUAD" }],
    force_refresh: false,
  }, "the gene is sent alone, with the tumor type its fusion was submitted with");
  assert.ok(calls.some((call) => call.url === "/v1/annotate/jobs/job-1"));

  const annotations = sandbox.state.currentResult.annotations;
  assert.strictEqual(annotations.length, 2);
  assert.strictEqual(annotations[0].gene, "ALK");
  assert.strictEqual(annotations[0].gene_summary, "Retried");
  assert.strictEqual(annotations[1].gene, "TP53", "unrelated rows are kept");
  assert.strictEqual(sandbox.state.currentResult.genes_annotated, 2);
  assert.strictEqual(sandbox.state.annotationRetryStatus, null);
  assert.strictEqual(sandbox.elements.messageBox.textContent, "Failed annotations retried.");
}

async function testRecheckNoResultForcesRefreshAndFallsBackToGeneInput() {
  const calls = [];
  const sandbox = app(async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/runs/run-1/retry" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-2", status_url: "/v1/annotate/jobs/job-2" }),
        text: async () => "",
      };
    }
    if (url === "/v1/annotate/jobs/job-2") {
      return {
        ok: true, status: 200,
        json: async () => ({
          status: "complete",
          annotations: [{ gene: "BRAF", cancer_associated: true }],
        }),
        text: async () => "",
      };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  renderNoResult(sandbox, [noResult]);
  sandbox.state.currentRunInputs = [];

  await sandbox.retryFailedAnnotations("noresult");

  const post = calls.find((call) => call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body), {
    kind: "no_result",
    inputs: [{ fusion: "BRAF" }],
    force_refresh: true,
  });
  assert.strictEqual(sandbox.state.currentResult.annotations[0].cancer_associated, true);
  assert.strictEqual(sandbox.elements.messageBox.textContent, "No-result genes re-checked.");
}

// Codex review case 1: ALK failed transiently, its fusion partner BAD was
// rejected permanently. Only ALK is sent, and BAD's row is never rewritten
// even if the job returns a row for it.
async function testFusionWithPermanentlyRejectedPartnerSendsOnlyTheGene() {
  const calls = [];
  const badRow = {
    gene: "BAD", fusions: ["ALK::BAD"], insufficient_evidence: true,
    error: "'BAD' is not a valid human Ensembl ID.",
  };
  const sandbox = app(retryFetch([{
    status: "complete",
    result: {
      annotations: [
        { gene: "ALK", insufficient_evidence: false, gene_summary: "Retried" },
        { gene: "BAD", insufficient_evidence: true, error: "Synthesis error: something else" },
      ],
    },
  }], calls));
  sandbox.state.currentRunInputs = [{ fusion: "ALK::BAD", tumor_type: "LUAD" }];
  renderNoResult(sandbox, [
    { gene: "ALK", fusions: ["ALK::BAD"], insufficient_evidence: true, error: "Synthesis error: timeout" },
    badRow,
  ]);

  await sandbox.retryFailedAnnotations("errors");

  const post = calls.find((call) => call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body).inputs, [{ fusion: "ALK", tumor_type: "LUAD" }]);
  const rows = JSON.parse(JSON.stringify(sandbox.state.currentResult.annotations));
  assert.strictEqual(rows[0].gene_summary, "Retried");
  assert.deepStrictEqual(rows[1], badRow, "the permanently rejected row is untouched");
}

// Codex review case 2: ALK failed, its fusion partner TP53 already has a good
// result. Only ALK is sent, and a recomputed TP53 never replaces the good row.
async function testFusionWithGoodPartnerSendsOnlyTheGene() {
  const calls = [];
  const goodTp53 = {
    gene: "TP53", fusions: ["ALK::TP53"], insufficient_evidence: false, gene_summary: "Good",
  };
  const sandbox = app(retryFetch([{
    status: "complete",
    result: {
      annotations: [
        { gene: "ALK", insufficient_evidence: false, gene_summary: "Retried" },
        { gene: "TP53", insufficient_evidence: false, gene_summary: "Recomputed" },
      ],
    },
  }], calls));
  sandbox.state.currentRunInputs = [{ fusion: "ALK::TP53", tumor_type: "LUAD" }];
  renderNoResult(sandbox, [
    { gene: "ALK", fusions: ["ALK::TP53"], insufficient_evidence: true, error: "Synthesis error: timeout" },
    goodTp53,
  ]);

  await sandbox.retryFailedAnnotations("errors");

  const post = calls.find((call) => call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body).inputs, [{ fusion: "ALK", tumor_type: "LUAD" }]);
  const rows = JSON.parse(JSON.stringify(sandbox.state.currentResult.annotations));
  assert.strictEqual(rows[0].gene_summary, "Retried");
  assert.deepStrictEqual(rows[1], goodTp53, "the good partner row is untouched");
}

async function testRetryKeepsTheRowsSubmittedSpellings() {
  const sandbox = app(retryFetch([{
    status: "complete",
    result: {
      annotations: [{
        gene: "ALK", insufficient_evidence: false, gene_summary: "Retried",
        resolved_inputs: [{ input: "CD246", resolved: "ALK" }],
      }],
    },
  }]));
  renderNoResult(sandbox, [{
    gene: "ALK", insufficient_evidence: true, error: "Synthesis error: timeout",
    resolved_inputs: [
      { input: "CD246", resolved: "ALK" },
      { input: "ENSG00000171094", resolved: "ALK" },
    ],
  }]);

  await sandbox.retryFailedAnnotations("errors");

  const alk = sandbox.state.currentResult.annotations[0];
  assert.strictEqual(alk.gene_summary, "Retried");
  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(alk.resolved_inputs.map((item) => item.input))),
    ["CD246", "ENSG00000171094"],
  );
}

// Codex review case: the ALK row was submitted as CD246, and on retry the
// input resolves to LTK. The ALK row stays exactly as it was, no LTK row is
// added, other genes still update, and the server's outcome is shown on the row.
async function testRetryResolvingToAnotherGeneLeavesTheRowAndShowsWhy() {
  const alk = {
    gene: "ALK", insufficient_evidence: true, error: "Synthesis error: timeout",
    resolved_inputs: [{ input: "CD246", resolved: "ALK" }],
  };
  const message = "CD246 now resolves to LTK, not ALK; ALK was not updated";
  const sandbox = app(retryFetch([{
    status: "complete",
    result: {
      annotations: [
        {
          gene: "LTK", insufficient_evidence: false, gene_summary: "Wrong gene",
          resolved_inputs: [{ input: "ALK", resolved: "LTK" }],
        },
        { gene: "BRAF", insufficient_evidence: false, gene_summary: "Retried" },
      ],
    },
    retry_outcomes: [{ input: "CD246", row_gene: "ALK", resolved_gene: "LTK", message }],
  }]));
  renderNoResult(sandbox, [
    alk,
    { gene: "BRAF", insufficient_evidence: true, error: "Synthesis error: timeout" },
  ]);

  await sandbox.retryFailedAnnotations("errors");

  const rows = JSON.parse(JSON.stringify(sandbox.state.currentResult.annotations));
  assert.deepStrictEqual(rows[0], alk, "the ALK row is left exactly as it was");
  assert.strictEqual(rows[1].gene_summary, "Retried", "other genes still update");
  assert.deepStrictEqual(rows.map((row) => row.gene), ["ALK", "BRAF"], "no LTK row is added");
  const notes = byClass(sandbox.elements.resultsWindow, "annotation-retry-note");
  assert.strictEqual(notes.length, 1);
  assert.ok(notes[0].innerHTML.includes(message), "the outcome is shown on the ALK row");
  assert.strictEqual(notes[0]._parent.id, "gene-ALK");
  assert.ok(sandbox.elements.messageBox.textContent.includes("1 gene was not updated"));
}

// A result with an empty or blank gene is never written, even into a row
// whose lookup failed (which otherwise takes its first resolution).
async function testBlankResultGeneIsNeverWritten() {
  for (const blank of ["", "  "]) {
    for (const echoesSpelling of [true, false]) {
      const ensg = {
        gene: "ENSG00000157764", insufficient_evidence: true,
        error: "Unresolvable gene symbol — Ensembl lookup temporarily unavailable (503); retry later.",
      };
      const sandbox = app(retryFetch([{
        status: "complete",
        result: {
          annotations: [{
            gene: blank, insufficient_evidence: false, gene_summary: "Blank",
            resolved_inputs: echoesSpelling ? [{ input: "ENSG00000157764", resolved: blank }] : [],
          }],
        },
      }]));
      renderNoResult(sandbox, [ensg]);

      await sandbox.retryFailedAnnotations("errors");

      const label = `gene=${JSON.stringify(blank)}, echoes spelling=${echoesSpelling}`;
      assert.deepStrictEqual(
        JSON.parse(JSON.stringify(sandbox.state.currentResult.annotations)), [ensg], label,
      );
      const notes = byClass(sandbox.elements.resultsWindow, "annotation-retry-note");
      assert.strictEqual(notes.length, 1, label);
      assert.ok(
        notes[0].innerHTML.includes("ENSG00000157764 did not resolve to a gene; not updated"), label,
      );
    }
  }
}

async function testTumorTypeIsTheFirstSubmittedForTheGene() {
  const calls = [];
  const sandbox = app(retryFetch([{ status: "complete", result: { annotations: [] } }], calls));
  sandbox.state.currentRunInputs = [
    { fusion: "KRAS" },
    { fusion: "EML4::ALK" },
    { fusion: "ENSG00000157764", tumor_type: "Melanoma" },
    { fusion: "ALK", tumor_type: "NSCLC" },
    { fusion: "EML4::ALK", tumor_type: "LUAD" },
  ];
  renderNoResult(sandbox, [
    { gene: "ALK", insufficient_evidence: true, error: "Synthesis error: timeout" },
    {
      gene: "BRAF", insufficient_evidence: true, error: "Synthesis error: timeout",
      resolved_inputs: [{ input: "ENSG00000157764", resolved: "BRAF" }],
    },
    { gene: "KRAS", insufficient_evidence: true, error: "Synthesis error: timeout" },
  ]);

  await sandbox.retryFailedAnnotations("errors");

  const post = calls.find((call) => call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body).inputs, [
    { fusion: "ALK", tumor_type: "NSCLC" },
    { fusion: "BRAF", tumor_type: "Melanoma" },
    { fusion: "KRAS" },
  ]);
}

async function testRetryErrorKeepsOldRows() {
  const sandbox = app(async (url) => {
    if (url === "/v1/runs/run-1/retry") {
      return { ok: false, status: 403, statusText: "Forbidden", text: async () => "Not allowed" };
    }
    throw new Error(`unexpected fetch ${url}`);
  });
  renderNoResult(sandbox, [transient]);

  await sandbox.retryFailedAnnotations("errors");

  assert.strictEqual(sandbox.state.currentResult.annotations[0].error, "Synthesis error: timeout");
  assert.strictEqual(sandbox.state.annotationRetryStatus, null);
  assert.ok(sandbox.elements.messageBox.textContent.includes("Not allowed"));
}

// A fetch stub for one retry: POST returns a job, each status poll returns the
// next entry of `statuses` (an object, or a function run before returning).
function retryFetch(statuses, calls = []) {
  let polls = 0;
  return async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/runs/run-1/retry" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-9", status_url: "/v1/annotate/jobs/job-9" }),
        text: async () => "",
      };
    }
    if (url === "/v1/annotate/jobs/job-9") {
      let status = statuses[Math.min(polls, statuses.length - 1)];
      polls += 1;
      if (typeof status === "function") status = status();
      return { ok: true, status: 200, json: async () => status, text: async () => "" };
    }
    throw new Error(`unexpected fetch ${url}`);
  };
}

async function testFailedJobLeavesPageUnchanged() {
  const sandbox = app(retryFetch([{ status: "failed", error: "upstream exploded" }]));
  const keep = { gene: "TP53", cancer_associated: true };
  renderNoResult(sandbox, [transient, keep]);
  const before = JSON.stringify(sandbox.state.currentResult);

  await sandbox.retryFailedAnnotations("errors");

  assert.strictEqual(JSON.stringify(sandbox.state.currentResult), before);
  assert.strictEqual(sandbox.elements.tabNoResultView.textContent, "No result (1)");
  assert.strictEqual(sandbox.state.annotationRetryStatus, null);
  assert.ok(sandbox.elements.messageBox.textContent.includes("upstream exploded"));
  assert.strictEqual(retryErrorsButton(sandbox).disabled, false, "the button is usable again");
}

async function testWorseResultNeverReplacesExisting() {
  const sandbox = app(retryFetch([{
    status: "complete",
    result: {
      annotations: [
        { gene: "BRAF", insufficient_evidence: true, error: "Synthesis error: timeout" },
      ],
    },
  }]));
  renderNoResult(sandbox, [noResult]);

  await sandbox.retryFailedAnnotations("noresult");

  assert.deepStrictEqual(
    JSON.parse(JSON.stringify(sandbox.state.currentResult.annotations)),
    [noResult],
    "a no-result row is not replaced by an error",
  );
}

async function testLateResponseForAnotherRunIsDropped() {
  const otherRun = {
    run_id: "run-2", fusions_processed: 1, genes_annotated: 1,
    annotations: [{ gene: "KRAS", insufficient_evidence: true }], fusion_evidence: [],
  };
  let sandbox;
  sandbox = app(retryFetch([() => {
    // The viewer opened another run while this retry was polling.
    sandbox.state.currentResult = otherRun;
    return { status: "complete", result: { annotations: [{ gene: "ALK", cancer_associated: true }] } };
  }]));
  renderNoResult(sandbox, [transient]);

  await sandbox.retryFailedAnnotations("errors");

  assert.strictEqual(sandbox.state.currentResult, otherRun, "the other run is untouched");
  assert.deepStrictEqual(sandbox.state.currentResult.annotations.map((a) => a.gene), ["KRAS"]);
  assert.notStrictEqual(sandbox.elements.messageBox.textContent, "Failed annotations retried.");
}

async function testLateResponseAfterRunReloadIsDropped() {
  let sandbox;
  sandbox = app(retryFetch([() => {
    // Same run reopened (or a rerun shown): its retries are invalidated.
    sandbox.invalidateAnnotationRetryJobs();
    return { status: "failed", error: "late failure" };
  }]));
  renderNoResult(sandbox, [transient]);

  await sandbox.retryFailedAnnotations("errors");

  assert.strictEqual(sandbox.state.currentResult.annotations[0].error, "Synthesis error: timeout");
  assert.ok(!sandbox.elements.messageBox.textContent.includes("late failure"),
    "a stale job's failure is not reported");
}

async function testCountsAndSidebarFilterUpdate() {
  const sandbox = app(retryFetch([{
    status: "complete",
    result: { annotations: [{ gene: "BRAF", insufficient_evidence: false, cancer_associated: true }] },
  }]));
  const kras = { gene: "KRAS", insufficient_evidence: true };
  const tp53 = { gene: "TP53", insufficient_evidence: false, cancer_associated: true };
  renderNoResult(sandbox, [noResult, kras, tp53]);
  assert.strictEqual(sandbox.elements.tabResultsView.textContent, "Results (1)");
  assert.strictEqual(sandbox.elements.tabNoResultView.textContent, "No result (2)");
  const sidebar = () => sandbox.elements.geneIndex.children
    .filter((node) => hasClass(node, "gene-index-item"))
    .map((node) => node.title);
  assert.deepStrictEqual(sidebar(), ["BRAF", "KRAS"]);
  const shown = () => sandbox.elements.geneIndex.children
    .filter((node) => hasClass(node, "gene-index-item") && !node.hidden)
    .map((node) => node.title);
  sandbox.bindGeneIndexFilter();
  sandbox.elements.geneIndexFilter.value = "braf";
  (sandbox.elements.geneIndexFilter._listeners.get("input") || []).forEach((handler) => handler({}));
  await new Promise((resolve) => setTimeout(resolve, 120));
  assert.deepStrictEqual(shown(), ["BRAF"]);

  await sandbox.retryFailedAnnotations("noresult");

  assert.deepStrictEqual(shown(), [], "the No result filter is re-applied to the new list");
  assert.strictEqual(sandbox.elements.geneIndexFilter.value, "braf", "the filter text is kept");

  assert.strictEqual(sandbox.elements.tabResultsView.textContent, "Results (2)");
  assert.strictEqual(sandbox.elements.tabNoResultView.textContent, "No result (1)");
  assert.strictEqual(sandbox.state.currentResult.genes_annotated, 3);
  assert.deepStrictEqual(sidebar(), ["KRAS"], "the No result sidebar drops the re-checked gene");
  sandbox.state.resultsViewMode = "results";
  sandbox.renderAnnotationResult(sandbox.state.currentResult);
  assert.deepStrictEqual(sidebar(), ["BRAF", "TP53"]);
  assert.deepStrictEqual(shown(), ["BRAF", "TP53"], "the Results tab has its own filter");
}

const tests = [
  testButtonsGateOnPermissionAndKind,
  testRetryErrorsPostsGenesAndMergesInPlace,
  testRecheckNoResultForcesRefreshAndFallsBackToGeneInput,
  testFusionWithPermanentlyRejectedPartnerSendsOnlyTheGene,
  testFusionWithGoodPartnerSendsOnlyTheGene,
  testTumorTypeIsTheFirstSubmittedForTheGene,
  testRetryKeepsTheRowsSubmittedSpellings,
  testRetryResolvingToAnotherGeneLeavesTheRowAndShowsWhy,
  testBlankResultGeneIsNeverWritten,
  testRetryErrorKeepsOldRows,
  testFailedJobLeavesPageUnchanged,
  testWorseResultNeverReplacesExisting,
  testLateResponseForAnotherRunIsDropped,
  testLateResponseAfterRunReloadIsDropped,
  testCountsAndSidebarFilterUpdate,
];
(async () => {
  for (const test of tests) {
    await test();
    console.log(`PASS ${test.name}`);
  }
  console.log(`${tests.length} annotation retry frontend test functions passed`);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});

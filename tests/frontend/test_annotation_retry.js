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

async function testRetryErrorsPostsOriginalInputsAndMergesInPlace() {
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
    inputs: [{ fusion: "EML4::ALK", tumor_type: "LUAD" }],
    force_refresh: false,
  });
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
    inputs: [{ fusion: "BRAF" }],
    force_refresh: true,
  });
  assert.strictEqual(sandbox.state.currentResult.annotations[0].cancer_associated, true);
  assert.strictEqual(sandbox.elements.messageBox.textContent, "No-result genes re-checked.");
}

async function testRetryAvoidsDeterministicallyRejectedPartner() {
  const calls = [];
  const sandbox = app(async (url, options = {}) => {
    calls.push({ url, options });
    if (url === "/v1/runs/run-1/retry" && options.method === "POST") {
      return {
        ok: true, status: 200,
        json: async () => ({ job_id: "job-3", status_url: "/v1/annotate/jobs/job-3" }),
        text: async () => "",
      };
    }
    return {
      ok: true, status: 200,
      json: async () => ({ status: "complete", annotations: [{ gene: "ALK" }] }),
      text: async () => "",
    };
  });
  sandbox.state.canModifyRun = true;
  sandbox.state.currentRunInputs = [{ fusion: "EML4::ALK", tumor_type: "LUAD" }];
  renderNoResult(sandbox, [
    { gene: "EML4", insufficient_evidence: true, error: "'EML4': NM_1 belongs to EML4, not ALK." },
    { gene: "ALK", insufficient_evidence: true, error: "Synthesis error: timeout" },
  ]);

  await sandbox.retryFailedAnnotations("errors");

  const post = calls.find((call) => call.options.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.options.body).inputs, [
    { fusion: "ALK", tumor_type: "LUAD" },
  ], "only the retryable gene is sent, keeping the tumor context");
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
  testRetryErrorsPostsOriginalInputsAndMergesInPlace,
  testRecheckNoResultForcesRefreshAndFallsBackToGeneInput,
  testRetryAvoidsDeterministicallyRejectedPartner,
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

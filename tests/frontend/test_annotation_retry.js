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

const tests = [
  testButtonsGateOnPermissionAndKind,
  testRetryErrorsPostsOriginalInputsAndMergesInPlace,
  testRecheckNoResultForcesRefreshAndFallsBackToGeneInput,
  testRetryAvoidsDeterministicallyRejectedPartner,
  testRetryErrorKeepsOldRows,
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

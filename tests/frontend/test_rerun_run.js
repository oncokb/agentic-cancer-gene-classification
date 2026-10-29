// Proves src/static/app.js's Rerun controls on an opened saved run: they
// show only for a run opened from a link, POST /v1/runs/{id}/rerun with the
// chosen cache mode, poll the new job like a fresh submission, start the
// fusion-evidence follow-up with the original inputs, and move the URL to
// the new run. Also covers the My runs page hand-off via sessionStorage.
// Run via tests/test_frontend_rerun_run.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");

const ORIGINAL = {
  run_id: "orig/1",
  timestamp: "2026-10-01T12:00:00+00:00",
  fusions_processed: 1,
  genes_annotated: 0,
  annotations: [],
  fusion_evidence: [],
  timings_ms: {},
};
const INPUTS = [
  { fusion: "EML4::ALK", tumor_type: "LUAD", five_transcript: "ENST00000318522" },
  { fusion: "TP53", tumor_type: null },
];
const NEW_RESULT = { ...ORIGINAL, run_id: "new-run", fusion_evidence: [] };

function json(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function memoryStorage(initial = {}) {
  const items = new Map(Object.entries(initial));
  return {
    items,
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: (key) => items.delete(key),
  };
}

// routes: { "METHOD url": body | (init) => response }; every call is recorded.
function app({ routes = {}, search = "?run=orig%2F1", storage = memoryStorage() } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    const key = `${init.method || "GET"} ${url}`;
    calls.push({ key, body: init.body ? JSON.parse(init.body) : undefined });
    const route = routes[key];
    if (route === undefined) return json({}, 404);
    return typeof route === "function" ? route(init) : json(route);
  };
  const sandbox = loadApp({ fetchImpl });
  const replaced = [];
  sandbox.location = { search, pathname: "/", origin: "http://localhost" };
  sandbox.history = { replaceState: (_state, _title, url) => replaced.push(url) };
  sandbox.sessionStorage = storage;
  sandbox.bindEvents();
  return { sandbox, calls, replaced, storage };
}

const RERUN_ROUTES = {
  "GET /v1/annotate/orig%2F1": ORIGINAL,
  "POST /v1/runs/orig%2F1/rerun": {
    job_id: "job-1", status_url: "/v1/annotate/jobs/job-1", rerun_of: "orig/1", fusions: INPUTS, mode: "core",
  },
  "GET /v1/annotate/jobs/job-1": { job_id: "job-1", status: "complete", fusions_processed: 2, genes_completed: 0, result: NEW_RESULT },
  "POST /v1/fusion-evidence/jobs": { job_id: "fe-1", status_url: "/v1/fusion-evidence/jobs/fe-1" },
  "GET /v1/fusion-evidence/jobs/fe-1": { job_id: "fe-1", status: "complete", fusion_evidence: [] },
};

function click(element) {
  const handlers = element._listeners.get("click") || [];
  assert.strictEqual(handlers.length, 1, "expected one click handler on the Rerun button");
  return handlers[0]();
}

async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((resolve) => setImmediate(resolve));
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("Rerun controls show only on an opened saved run, cached by default", async () => {
  const { sandbox } = app({ routes: RERUN_ROUTES });
  sandbox.syncRerunControls();
  assert.ok(sandbox.elements.rerunControls.classList.contains("hidden"), "hidden before any run is opened");
  assert.strictEqual(sandbox.elements.rerunRun.disabled, true);
  await sandbox.loadSharedRun();
  assert.ok(!sandbox.elements.rerunControls.classList.contains("hidden"));
  assert.strictEqual(sandbox.elements.rerunRun.disabled, false);
  assert.strictEqual(sandbox.state.openedRunId, "orig/1");
  sandbox.clearResults();
  assert.ok(sandbox.elements.rerunControls.classList.contains("hidden"), "hidden once results are cleared");
});

test("Rerun posts the cached rerun, polls it, updates the URL and starts fusion evidence", async () => {
  const { sandbox, calls, replaced } = app({ routes: RERUN_ROUTES });
  await sandbox.loadSharedRun();
  sandbox.elements.rerunMode.value = "cached";
  await click(sandbox.elements.rerunRun);
  await settle();

  const keys = calls.map((call) => call.key);
  assert.deepStrictEqual(keys, [
    "GET /v1/annotate/orig%2F1",
    "POST /v1/runs/orig%2F1/rerun",
    "GET /v1/annotate/jobs/job-1",
    "POST /v1/fusion-evidence/jobs",
    "GET /v1/fusion-evidence/jobs/fe-1",
  ]);
  assert.deepStrictEqual(calls[1].body, { force_refresh: false });
  // The follow-up job gets the original fusion inputs, mode and the NEW run.
  assert.deepStrictEqual(calls[3].body, { fusions: [INPUTS[0]], mode: "core", run_id: "new-run" });
  assert.deepStrictEqual(replaced, ["/?run=new-run"]);
  assert.strictEqual(sandbox.state.currentResult.run_id, "new-run");
  assert.strictEqual(sandbox.state.openedRunId, "new-run");
  assert.strictEqual(sandbox.state.lastInputsByFusion["EML4::ALK"].five_transcript, "ENST00000318522");
  assert.strictEqual(sandbox.state.isRunning, false);
});

test("Rerun with Refresh selected sends force_refresh", async () => {
  const { sandbox, calls } = app({ routes: RERUN_ROUTES });
  await sandbox.loadSharedRun();
  sandbox.elements.rerunMode.value = "refresh";
  await click(sandbox.elements.rerunRun);
  await settle();
  assert.deepStrictEqual(calls.find((call) => call.key === "POST /v1/runs/orig%2F1/rerun").body, { force_refresh: true });
});

test("inline fusion evidence on the new run skips the follow-up job", async () => {
  const withEvidence = { ...NEW_RESULT, fusion_evidence: [{ fusion: "EML4::ALK", interpretation: "ok" }] };
  const routes = {
    ...RERUN_ROUTES,
    "GET /v1/annotate/jobs/job-1": { job_id: "job-1", status: "complete", fusions_processed: 2, genes_completed: 0, result: withEvidence },
  };
  const { sandbox, calls } = app({ routes });
  await sandbox.loadSharedRun();
  await click(sandbox.elements.rerunRun);
  await settle();
  assert.ok(!calls.some((call) => call.key === "POST /v1/fusion-evidence/jobs"));
  assert.strictEqual(sandbox.state.fusionEvidenceStatus, "complete");
});

test("a rejected rerun keeps the original run and shows the server detail as text", async () => {
  const detail = "This run can't be rerun: <img src=x onerror=alert(1)>";
  const routes = { ...RERUN_ROUTES, "POST /v1/runs/orig%2F1/rerun": () => json({ detail }, 422) };
  const { sandbox, replaced } = app({ routes });
  await sandbox.loadSharedRun();
  await click(sandbox.elements.rerunRun);
  await settle();
  assert.strictEqual(sandbox.elements.messageBox.textContent, detail);
  assert.strictEqual(sandbox.elements.messageBox.innerHTML, "");
  assert.strictEqual(sandbox.state.currentResult.run_id, "orig/1");
  assert.strictEqual(sandbox.state.openedRunId, "orig/1");
  assert.deepStrictEqual(replaced, []);
  assert.strictEqual(sandbox.elements.rerunRun.disabled, false);
});

test("a pending rerun from My runs starts after the run opens and is consumed", async () => {
  const storage = memoryStorage({ "agcg.pendingRerun": JSON.stringify({ run_id: "orig/1", force_refresh: true }) });
  const { sandbox, calls, replaced } = app({ routes: RERUN_ROUTES, storage });
  await sandbox.loadSharedRun();
  await settle();
  assert.deepStrictEqual(calls.find((call) => call.key === "POST /v1/runs/orig%2F1/rerun").body, { force_refresh: true });
  assert.deepStrictEqual(replaced, ["/?run=new-run"]);
  assert.strictEqual(storage.items.has("agcg.pendingRerun"), false);
});

test("a pending rerun for a different run is discarded, and the URL alone never reruns", async () => {
  const storage = memoryStorage({ "agcg.pendingRerun": JSON.stringify({ run_id: "other", force_refresh: false }) });
  const { sandbox, calls } = app({ routes: RERUN_ROUTES, storage, search: "?run=orig%2F1&rerun=1" });
  await sandbox.loadSharedRun();
  await settle();
  assert.deepStrictEqual(calls.map((call) => call.key), ["GET /v1/annotate/orig%2F1"]);
  assert.strictEqual(storage.items.has("agcg.pendingRerun"), false);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (error) {
      failed++;
      console.error(`not ok - ${name}\n${error.stack}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exitCode = failed ? 1 : 0;
})();

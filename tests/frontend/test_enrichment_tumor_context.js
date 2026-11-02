// Proves src/static/app.js keeps the completed genes' tumor context from a
// RUNNING annotation job poll (annotation_tumor_types) in state.currentResult,
// and sends it with lazy enrichment started mid-run, so the server applies the
// gene's tumor-scoped curator override.
// Run via tests/test_frontend_enrichment_tumor_context.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");

const ANNOTATION = { gene: "ALK", fusions: [], citations: [], evidence_cards: [], supporting_quotes: [] };

function jsonResponse(body) {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
}

async function test_running_poll_keeps_tumor_context_and_enrichment_sends_it() {
  const requests = [];
  let polls = 0;
  const sandbox = loadApp({
    fetchImpl: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === "/v1/annotate/jobs/job-1") {
        polls += 1;
        if (polls === 1) {
          return jsonResponse({
            job_id: "job-1", status: "running", fusions_processed: 2, genes_completed: 1, genes_total: 2,
            annotations: [ANNOTATION], annotation_tumor_types: { ALK: "LUAD" }, result: null, timings_ms: {},
          });
        }
        return jsonResponse({ job_id: "job-1", status: "failed", error: "stop polling" });
      }
      if (url === "/v1/annotate/enrichment/jobs") return jsonResponse({ job_id: "e-1", status_url: "/enrich-1" });
      return jsonResponse({ job_id: "e-1", status: "failed", error: "stop polling" });
    },
  });
  sandbox.setTimeout = (fn) => fn(); // no 1.5s sleep between polls

  await assert.rejects(() => sandbox.pollAnnotationJob("/v1/annotate/jobs/job-1"), /stop polling/);
  assert.deepStrictEqual({ ...sandbox.state.currentResult.annotation_tumor_types }, { ALK: "LUAD" });

  await assert.rejects(() => sandbox.submitAnnotationEnrichment(ANNOTATION, "details"), /stop polling/);
  const enrich = requests.find((r) => r.url === "/v1/annotate/enrichment/jobs");
  assert.ok(enrich, "enrichment request was not sent");
  assert.deepStrictEqual(JSON.parse(enrich.options.body).tumor_types, { ALK: "LUAD" });
}

const TESTS = [test_running_poll_keeps_tumor_context_and_enrichment_sends_it];

async function main() {
  let failures = 0;
  for (const test of TESTS) {
    try {
      await test();
      console.log(`PASS ${test.name}`);
    } catch (err) {
      failures += 1;
      console.error(`FAIL ${test.name}`);
      console.error(err);
    }
  }
  if (failures > 0) {
    console.error(`${failures}/${TESTS.length} enrichment tumor context tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} enrichment tumor context tests passed`);
}

main();

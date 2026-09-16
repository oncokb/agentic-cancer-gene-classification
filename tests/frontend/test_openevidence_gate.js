// Proves the frontend OpenEvidence sidecar gate (see review gap #1): when
// settings.openevidence_enabled is off, src/static/app.js must render no
// sidecar element and issue no GET /v1/genes/{gene}/openevidence request.
// Run via tests/test_frontend_openevidence_gate.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");

const CANCER_ASSOCIATED_ANNOTATION = {
  gene: "ALK",
  cancer_associated: true,
  insufficient_evidence: false,
  fusions: [],
};

async function withFetchLog(devStatusPayload) {
  const calls = [];
  const fetchImpl = async (url) => {
    calls.push(String(url));
    if (String(url) === "/v1/dev/status") {
      return {
        ok: true,
        json: async () => devStatusPayload,
      };
    }
    return {
      ok: true,
      json: async () => ({ available: true, distilled: { consensus_role: "test" } }),
    };
  };
  const sandbox = loadApp({ fetchImpl });
  return { sandbox, calls };
}

async function test_default_state_is_disabled_before_dev_status_resolves() {
  const { sandbox } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  assert.strictEqual(
    sandbox.state.openevidenceEnabled,
    false,
    "state.openevidenceEnabled must default to false (fail closed) before GET /v1/dev/status resolves"
  );
}

async function test_disabled_renders_no_card_and_issues_no_fetch() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: false });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, false);

  const card = sandbox.renderOpenEvidenceCard(CANCER_ASSOCIATED_ANNOTATION);
  assert.strictEqual(card, null, "renderOpenEvidenceCard must render nothing when the flag is disabled");

  // Give any (incorrectly) queued fetch a turn to fire before asserting.
  await new Promise((resolve) => setTimeout(resolve, 0));

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.deepStrictEqual(
    openEvidenceCalls,
    [],
    `expected no GET /v1/genes/{gene}/openevidence request; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
}

async function test_fetchGeneOpenEvidence_is_a_noop_when_disabled() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: false });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, false);

  // deepStrictEqual isn't usable here: the resolved object is an instance
  // of the vm sandbox's own Object constructor, a different realm from this
  // process's Object — structurally equal but never reference-equal.
  const response = await sandbox.fetchGeneOpenEvidence("ALK", null, {});
  assert.strictEqual(response.available, false);
  assert.deepStrictEqual(Object.keys(response), ["available"]);

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.deepStrictEqual(openEvidenceCalls, []);
}

async function test_enabled_still_renders_card_and_fetches() {
  const { sandbox, calls } = await withFetchLog({ enabled: false, openevidence_enabled: true });
  await sandbox.loadDevStatus();
  assert.strictEqual(sandbox.state.openevidenceEnabled, true);

  const card = sandbox.renderOpenEvidenceCard(CANCER_ASSOCIATED_ANNOTATION);
  assert.notStrictEqual(card, null, "renderOpenEvidenceCard must still render when the flag is enabled");

  await new Promise((resolve) => setTimeout(resolve, 0));

  const openEvidenceCalls = calls.filter((url) => url.includes("/openevidence"));
  assert.strictEqual(
    openEvidenceCalls.length,
    1,
    `expected exactly one GET .../openevidence request when enabled; saw: ${JSON.stringify(openEvidenceCalls)}`
  );
}

const TESTS = [
  test_default_state_is_disabled_before_dev_status_resolves,
  test_disabled_renders_no_card_and_issues_no_fetch,
  test_fetchGeneOpenEvidence_is_a_noop_when_disabled,
  test_enabled_still_renders_card_and_fetches,
];

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
    console.error(`${failures}/${TESTS.length} frontend gate tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} frontend gate tests passed`);
}

main();

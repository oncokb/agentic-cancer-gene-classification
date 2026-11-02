// Proves src/static/app.js shows a "Curator-verified" badge (not "100%
// confidence") on clinical actionability set from a curator's precedent, and
// keeps the percentage for the automated keyword score.
// Run via tests/test_frontend_curated_badge.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");

function app() {
  return loadApp({ fetchImpl: async () => ({ ok: false, json: async () => ({}) }) });
}

function badgeText(section) {
  const header = section.children.find((child) => child.className === "clinical-actionability-header");
  return header.children.find((child) => child.className === "review-badge high").textContent;
}

function test_curated_actionability_shows_curator_verified() {
  const section = app().renderClinicalActionability({
    gene: "ALK",
    clinical_actionability: {
      confidence_score: 1.0, summary: "Crizotinib response.", confidence_explanation: "Curator-verified by c",
      pmids: ["333"], evidence: [], curated: true,
    },
  });
  assert.strictEqual(badgeText(section), "Curator-verified");
}

function test_automated_actionability_keeps_confidence_percentage() {
  const section = app().renderClinicalActionability({
    gene: "ALK",
    clinical_actionability: {
      confidence_score: 0.9, summary: "Keyword precedent.", confidence_explanation: "keywords",
      pmids: ["111"], evidence: [],
    },
  });
  assert.strictEqual(badgeText(section), "90% confidence");
}

const TESTS = [test_curated_actionability_shows_curator_verified, test_automated_actionability_keeps_confidence_percentage];

let failures = 0;
for (const test of TESTS) {
  try {
    test();
    console.log(`PASS ${test.name}`);
  } catch (err) {
    failures += 1;
    console.error(`FAIL ${test.name}`);
    console.error(err);
  }
}
if (failures > 0) {
  console.error(`${failures}/${TESTS.length} curated badge tests failed`);
  process.exit(1);
}
console.log(`${TESTS.length}/${TESTS.length} curated badge tests passed`);

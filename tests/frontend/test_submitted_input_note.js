// Proves src/static/app.js shows the submitted form of an input the server
// rewrote (an Ensembl ID resolved to a symbol) beside the resolved gene or
// fusion, plus that input's warnings, as text only (never innerHTML), on the
// Results, No result and Fusion evidence cards.
// Run via tests/test_frontend_submitted_input_note.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");

function app() {
  return loadApp({ fetchImpl: async () => ({ ok: false, json: async () => ({}) }) });
}

function lines(note) {
  assert.ok(note, "expected a note element");
  assert.strictEqual(note.innerHTML, "", "note must not use innerHTML");
  return note.children.map((child) => {
    assert.strictEqual(child.innerHTML, "", "note lines must not use innerHTML");
    return child.textContent;
  });
}

function test_single_ensembl_input_shows_submitted_id() {
  const note = app().renderSubmittedInputNote([
    { input: "ENST00000269305", resolved: "TP53", warnings: [] },
  ]);
  assert.deepStrictEqual(lines(note), ["TP53 — from ENST00000269305"]);
}

function test_fusion_input_and_warnings_are_listed() {
  const note = app().renderSubmittedInputNote([
    { input: "ENST00000318522::ENST00000389048", resolved: "EML4::ALK", warnings: [] },
    {
      input: "ENST00000269305:c.215C>G",
      resolved: "TP53",
      warnings: ["variant-level interpretation of c.215C>G is not supported."],
    },
  ]);
  assert.deepStrictEqual(lines(note), [
    "EML4::ALK — from ENST00000318522::ENST00000389048",
    "TP53 — from ENST00000269305:c.215C>G",
    "variant-level interpretation of c.215C>G is not supported.",
  ]);
  assert.strictEqual(note.children[2].className, "submitted-input-warning");
}

function test_markup_in_input_stays_text() {
  const payload = "<img src=x onerror=alert(1)>";
  const note = app().renderSubmittedInputNote([{ input: payload, resolved: "TP53", warnings: [payload] }]);
  assert.deepStrictEqual(lines(note), [`TP53 — from ${payload}`, payload]);
}

function test_nothing_rendered_when_input_was_not_rewritten() {
  const sandbox = app();
  assert.strictEqual(sandbox.renderSubmittedInputNote(undefined), null);
  assert.strictEqual(sandbox.renderSubmittedInputNote([]), null);
  assert.strictEqual(sandbox.renderSubmittedInputNote([{ input: "TP53", resolved: "TP53", warnings: [] }]), null);
}

function test_fusion_evidence_card_note_uses_submitted_input() {
  const sandbox = app();
  const note = sandbox.fusionEvidenceInputNote({
    fusion: "EML4::ALK",
    input: "EML4(ENST00000318522)::ALK(ENST00000389048)",
  });
  assert.deepStrictEqual(lines(note), ["EML4::ALK — from EML4(ENST00000318522)::ALK(ENST00000389048)"]);
  assert.strictEqual(sandbox.fusionEvidenceInputNote({ fusion: "EML4::ALK", input: null }), null);
  assert.strictEqual(sandbox.fusionEvidenceInputNote({ fusion: "EML4::ALK" }), null);
}

// --- the real card render paths ---------------------------------------------

const ENST_RESOLUTION = {
  input: "ENST00000269305:c.215C>G",
  resolved: "TP53",
  five_transcript: null,
  three_transcript: null,
  warnings: ["variant-level interpretation of c.215C>G is not supported."],
};

function renderedNotes(sandbox) {
  return sandbox.createdElements
    .filter((el) => el.className === "subtle submitted-input-note")
    .map((note) => lines(note));
}

function renderView(mode, { visible = [], hidden = [], fusionEvidence = [] }) {
  const sandbox = app();
  sandbox.state.resultsViewMode = mode;
  sandbox.applyResultsViewMode(visible, hidden, fusionEvidence);
  return sandbox;
}

const EXPECTED_TP53_NOTE = [
  "TP53 — from ENST00000269305:c.215C>G",
  "variant-level interpretation of c.215C>G is not supported.",
];

function test_results_card_shows_submitted_input() {
  const annotation = {
    gene: "TP53",
    fusions: [],
    resolved_inputs: [ENST_RESOLUTION],
    cancer_associated: true,
    citations: [],
    evidence_cards: [],
    supporting_quotes: [],
    quality_flags: [],
  };
  const sandbox = renderView("results", { visible: [annotation] });
  assert.deepStrictEqual(renderedNotes(sandbox), [EXPECTED_TP53_NOTE]);
}

function test_no_result_card_shows_submitted_input() {
  const insufficient = {
    gene: "TP53",
    fusions: [],
    insufficient_evidence: true,
    resolved_inputs: [{ ...ENST_RESOLUTION }],
  };
  const sandbox = renderView("noresult", { hidden: [insufficient] });
  assert.deepStrictEqual(renderedNotes(sandbox), [EXPECTED_TP53_NOTE]);
}

function test_no_result_card_without_rewritten_input_has_no_note() {
  const sandbox = renderView("noresult", { hidden: [{ gene: "TP53", fusions: [], insufficient_evidence: true }] });
  assert.deepStrictEqual(renderedNotes(sandbox), []);
}

function test_fusion_evidence_card_shows_submitted_input() {
  const sandbox = renderView("fusion", {
    fusionEvidence: [
      {
        fusion: "EML4::ALK",
        input: "ENST00000318522::ENST00000389048",
        pmids: [],
        evidence_cards: [],
        interpretation: "ok",
      },
    ],
  });
  assert.deepStrictEqual(renderedNotes(sandbox), [["EML4::ALK — from ENST00000318522::ENST00000389048"]]);
}

const TESTS = [
  test_results_card_shows_submitted_input,
  test_no_result_card_shows_submitted_input,
  test_no_result_card_without_rewritten_input_has_no_note,
  test_fusion_evidence_card_shows_submitted_input,
  test_single_ensembl_input_shows_submitted_id,
  test_fusion_input_and_warnings_are_listed,
  test_markup_in_input_stays_text,
  test_nothing_rendered_when_input_was_not_rewritten,
  test_fusion_evidence_card_note_uses_submitted_input,
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
    console.error(`${failures}/${TESTS.length} frontend submitted-input note tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} frontend submitted-input note tests passed`);
}

main();

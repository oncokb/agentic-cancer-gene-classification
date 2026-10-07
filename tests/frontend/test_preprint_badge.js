// Proves src/static/app.js's evidence cards show a "Preprint – not
// peer-reviewed" badge for cards flagged is_preprint, and nothing for
// peer-reviewed cards or cards cached before the field existed.
// Run via tests/test_frontend_preprint_badge.py (invokes this with node).
"use strict";

const assert = require("assert");
const { loadApp, FakeElement } = require("./openevidence_gate_harness");

const BADGE_TEXT = "Preprint – not peer-reviewed";

const PREPRINT_CARD = {
  pmid: "36711734",
  title: "BRWD1 preprint",
  journal: "bioRxiv",
  publication_types: ["Preprint"],
  is_preprint: true,
  evidence_type: "preclinical",
  selected_reason: "",
  quote: null,
  abstract: null,
};
const PEER_REVIEWED_CARD = {
  pmid: "30000001",
  title: "BRWD1 in lung cancer",
  journal: "Cancer Res",
  publication_types: ["Journal Article"],
  is_preprint: false,
  evidence_type: "preclinical",
};
// Evidence card persisted before publication_types/is_preprint existed.
const LEGACY_CARD = { pmid: "20000001", title: "Old cached card", journal: "Nature", evidence_type: "other" };

function app() {
  return loadApp({ fetchImpl: async () => ({ ok: false, json: async () => ({}) }) });
}

function evidenceCardHtmlByPmid(root) {
  const byPmid = {};
  const walk = (node) => {
    if (!node) return;
    if (node.className === "evidence-card") {
      const match = /pubmed\.ncbi\.nlm\.nih\.gov\/([0-9]+)/.exec(node.innerHTML);
      byPmid[match ? match[1] : `unknown-${Object.keys(byPmid).length}`] = node.innerHTML;
    }
    (node.children || []).forEach(walk);
  };
  walk(root);
  return byPmid;
}

function test_badge_helper_only_for_flagged_cards() {
  const sandbox = app();
  assert.ok(sandbox.preprintBadgeHtml(PREPRINT_CARD).includes(BADGE_TEXT));
  assert.ok(sandbox.preprintBadgeHtml(PREPRINT_CARD).includes("review-badge preprint"));
  assert.strictEqual(sandbox.preprintBadgeHtml(PEER_REVIEWED_CARD), "");
  assert.strictEqual(sandbox.preprintBadgeHtml(LEGACY_CARD), "");
}

function test_gene_evidence_cards_render_badge_for_preprints_only() {
  const sandbox = app();
  const section = sandbox.renderSupportingEvidence({
    gene: "BRWD1",
    citations: ["36711734", "30000001", "20000001"],
    supporting_quotes: [],
    evidence_cards: [PREPRINT_CARD, PEER_REVIEWED_CARD, LEGACY_CARD],
    retrieved_pmids: [],
    retrieval_count: 3,
  });
  const cards = evidenceCardHtmlByPmid(section);
  assert.strictEqual(Object.keys(cards).length, 3, `expected 3 evidence cards, got ${Object.keys(cards)}`);
  assert.ok(cards["36711734"].includes(BADGE_TEXT), "preprint card missing badge");
  assert.ok(!cards["30000001"].includes(BADGE_TEXT), "peer-reviewed card should have no badge");
  assert.ok(!cards["20000001"].includes(BADGE_TEXT), "legacy cached card should have no badge");
}

function test_fusion_partner_evidence_cards_render_badge() {
  const sandbox = app();
  const container = new FakeElement("div");
  sandbox.renderFusionPartnerResultBody(container, {
    gene: "BRWD1",
    has_precedent: true,
    retrieved_count: 2,
    pmids: [],
    evidence_cards: [PREPRINT_CARD, PEER_REVIEWED_CARD],
  });
  const cards = evidenceCardHtmlByPmid(container);
  assert.ok(cards["36711734"].includes(BADGE_TEXT), "preprint partner card missing badge");
  assert.ok(!cards["30000001"].includes(BADGE_TEXT), "peer-reviewed partner card should have no badge");
}

const TESTS = [
  test_badge_helper_only_for_flagged_cards,
  test_gene_evidence_cards_render_badge_for_preprints_only,
  test_fusion_partner_evidence_cards_render_badge,
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
    console.error(`${failures}/${TESTS.length} frontend preprint badge tests failed`);
    process.exit(1);
  }
  console.log(`${TESTS.length}/${TESTS.length} frontend preprint badge tests passed`);
}

main();

// Proves the results gene sidebar keeps readable items with many genes:
// src/static/app.js's rebuildGeneIndex gives each item a title tooltip (long
// fusion labels are ellipsized horizontally), and styles.css stops the items
// from flex-shrinking, which squashed them to a sliver once the list was
// taller than the sidebar (e.g. ~40 genes, or fewer at higher browser zoom).
// Run via tests/test_frontend_gene_index_sidebar.py (invokes this with node).
"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");
const { loadApp } = require("./openevidence_gate_harness");

const STYLES_PATH = path.join(__dirname, "..", "..", "src", "static", "styles.css");

const GENES = ["AHI1", "COMMD10::AHI1", "SEMA3A-AS1", "ZC3H7B::BCOR"];

function testItemsCarryTitleTooltip() {
  const app = loadApp({ fetchImpl: async () => ({ ok: false, json: async () => ({}) }) });
  app.rebuildGeneIndex(GENES.map((gene) => ({ gene })));
  const items = app.elements.geneIndex.children;
  assert.deepStrictEqual(
    items.map((btn) => [btn.className, btn.textContent, btn.title]),
    GENES.map((gene) => ["gene-index-item", gene, gene]),
  );
}

function testItemsDoNotFlexShrink() {
  const css = fs.readFileSync(STYLES_PATH, "utf8");
  const rule = css.match(/^\.gene-index-item\s*\{([^}]*)\}/m);
  assert.ok(rule, ".gene-index-item rule not found in styles.css");
  assert.match(
    rule[1],
    /flex-shrink:\s*0\s*;/,
    ".gene-index-item must keep flex-shrink: 0 so a long list scrolls instead of squashing items",
  );
}

testItemsCarryTitleTooltip();
testItemsDoNotFlexShrink();
console.log("gene index sidebar tests passed");

// Expression stats page (optional expression-context module). Plain Node
// fake-DOM harness, following test_runs_page.js. Run by
// tests/test_expression_context_web.py.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const {
  createExpressionContextPage, formatNumber, formatQ, pmidUrl, expressionURL, exportURL, goldenSetExportURL,
  curatorSheetExportURL, batchExportURL, batchGoldenSetExportURL, batchCuratorSheetExportURL,
  parseBatchText, parseUploadedFileColumn,
} = require("../../src/expression_context/static/expression-context.js");

const HTML_PATH = path.join(__dirname, "../../src/expression_context/static/expression-context.html");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.textContent = "";
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.value = "";
    this.disabled = false;
    const classes = new Set();
    this.classList = {
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    };
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return this.attrs[name]; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name, event = {}) { return this.listeners[name](event); }
  get hidden() { return this.classList.contains("hidden"); }
  // All text below this node, depth first.
  get text() { return this.textContent + this.children.map((c) => c.text).join(""); }
  findAll(predicate) {
    const out = predicate(this) ? [this] : [];
    for (const child of this.children) out.push(...child.findAll(predicate));
    return out;
  }
}

const IDS = [
  "ec-form", "ec-gene", "ec-submit", "ec-error", "ec-loading", "ec-result", "ec-gene-title",
  "ec-summary", "ec-release", "ec-cbio", "ec-run", "ec-export", "ec-export-golden", "ec-export-curator",
  "ec-view-shown", "ec-view-all", "ec-view-count", "ec-table", "ec-table-empty", "ec-job-status", "ec-tbody",
  "ec-dmt-index",
  "ec-tab-single", "ec-tab-batch", "ec-single-mode", "ec-batch-mode", "ec-batch-form",
  "ec-batch-input", "ec-batch-upload-btn", "ec-batch-upload", "ec-batch-preview", "ec-batch-submit",
  "ec-batch-result", "ec-batch-title", "ec-batch-summary", "ec-batch-counts", "ec-batch-cancel",
  "ec-batch-progress", "ec-batch-export-shown", "ec-batch-export-all",
  "ec-batch-export", "ec-batch-export-golden", "ec-batch-export-curator", "ec-batch-genes",
];

const response = (body, status = 200) => ({ status, json: async () => body });

function cBioPortalLinks(node) {
  return node.findAll((n) => n.tagName === "a" && n.textContent === "mRNA vs Dx");
}

function summaryRows(nodes) {
  return nodes["ec-tbody"].children.filter((r) => r.getAttribute("class") !== "ec-detail-row");
}

function detailRows(nodes) {
  return nodes["ec-tbody"].children.filter((r) => r.getAttribute("class") === "ec-detail-row");
}

function plotParams(href) {
  const url = new URL(href);
  assert.strictEqual(`${url.origin}${url.pathname}`, "https://www.cbioportal.org/results/plots");
  return {
    url,
    studies: url.searchParams.get("cancer_study_list"),
    gene: url.searchParams.get("gene_list"),
    caseSet: url.searchParams.get("case_set_id"),
    horizontal: JSON.parse(url.searchParams.get("plots_horz_selection")),
    vertical: JSON.parse(url.searchParams.get("plots_vert_selection")),
  };
}

function env(routes, { search = "", wait = async () => {}, pollMs = 2000, maxPageRetries = 5 } = {}) {
  const nodes = Object.fromEntries(IDS.map((id) => [id, new Element("div")]));
  nodes["ec-error"].classList.toggle("hidden", true);
  nodes["ec-result"].classList.toggle("hidden", true);
  nodes["ec-run"].classList.toggle("hidden", true);
  nodes["ec-export"].classList.toggle("hidden", true);
  nodes["ec-export-golden"].classList.toggle("hidden", true);
  nodes["ec-export-curator"].classList.toggle("hidden", true);
  nodes["ec-table-empty"].classList.toggle("hidden", true);
  nodes["ec-view-shown"].classList.toggle("active", true);
  nodes["ec-view-shown"].setAttribute("aria-pressed", "true");
  nodes["ec-view-all"].setAttribute("aria-pressed", "false");
  nodes["ec-tab-single"].classList.toggle("active", true);
  nodes["ec-tab-single"].setAttribute("aria-pressed", "true");
  nodes["ec-tab-batch"].setAttribute("aria-pressed", "false");
  nodes["ec-batch-mode"].classList.toggle("hidden", true);
  nodes["ec-batch-result"].classList.toggle("hidden", true);
  nodes["ec-batch-cancel"].classList.toggle("hidden", true);
  nodes["ec-batch-export-shown"].classList.toggle("active", true);
  nodes["ec-batch-export-shown"].setAttribute("aria-pressed", "true");
  nodes["ec-batch-export-all"].setAttribute("aria-pressed", "false");
  const document = { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) };
  const calls = [];
  const replaced = [];
  const window = {
    location: { pathname: "/expression-context", search, href: "/expression-context" },
    history: { replaceState: (_s, _t, url) => replaced.push(url) },
    setTimeout: (fn) => fn(),
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET", body: options.body, signal: options.signal });
    const handler = routes[`${options.method || "GET"} ${url}`];
    if (!handler) throw new Error(`unexpected request ${options.method || "GET"} ${url}`);
    return typeof handler === "function" ? handler(url, options) : handler;
  };
  const page = createExpressionContextPage({ document, window, fetch, wait, pollMs, maxPageRetries });
  return { nodes, page, calls, window, replaced };
}

const BUNDLE = {
  bundle_tag: "expression-stats-datahub-abc1234def56-m2-b7e9f01a2c3d-pubmed-20261102",
  release_id: "datahub-abc1234def56-m2-b7e9f01a2c3d",
  schema_version: "1", method_version: "2", pubmed_snapshot_date: "2026-11-02", gene_count: 20531,
  published_at: "2026-11-02T06:00:00Z", loaded_at: "2026-11-02T06:10:00Z",
  release_url: "https://github.com/oncokb/agentic-cancer-gene-classification/releases/tag/x",
  source: { datahub_commit: "abc1234def56", generated_at: "2026-11-01T00:00:00Z" }, params: {},
};

function stats(code, extra = {}) {
  return {
    oncotree_code: code, n: 100, n_rest: 9000, median_log2: 6.2, rest_median_log2: 3.1,
    p90_log2: 8, rest_p90_log2: 5, log2fc: 2.74, log2fc_vs_median_of_type_medians: 2.5,
    cliffs_delta: 0.71, auc: 0.855, p_value: 1e-40, q_value: 1e-39, stats_pass: true,
    pubmed_count: 12, literature_precheck_pass: true, flags: [], ...extra,
  };
}

function view(overrides = {}) {
  return {
    query: "erbb2", hugo_symbol: "ERBB2", entrez_gene_id: 2064, literature_gate: "acgc_tumor_specific_v2",
    bundle: BUNDLE,
    summary: { surfaced: 1, literature_pending: 1, excluded: 2 },
    pairs: [
      {
        oncotree_code: "BRCA", cancer_type_name: "Breast Invasive Carcinoma",
        study_id: "brca_tcga_pan_can_atlas_2018",
        stats: stats("BRCA", { flags: ["tissue_of_origin_risk"] }), stats_pass: true,
        precheck: "pass", literature_eligible: true, status: "surfaced", exclusion_reason: null,
        literature: {
          supported: true, insufficient_evidence: false, pmids: ["12345678", "javascript:alert(1)"],
          rationale: "<b>amplified</b>", literature_gate: "acgc_tumor_specific_v2",
          tumor_type_query: "breast cancer", evaluated_at: "2026-11-05T00:00:00Z",
        },
      },
      {
        oncotree_code: "STAD", cancer_type_name: "Stomach Adenocarcinoma",
        study_id: "stad_tcga_pan_can_atlas_2018", stats: stats("STAD", {
          literature_precheck_pass: null, pubmed_count: null,
        }), stats_pass: true, precheck: "not_run", literature_eligible: true,
        status: "literature_pending", exclusion_reason: null, literature: null,
      },
      {
        oncotree_code: "UCEC", cancer_type_name: null,
        study_id: "ucec_tcga_pan_can_atlas_2018", stats: stats("UCEC", {
          literature_precheck_pass: false, pubmed_count: 0,
        }), stats_pass: true, precheck: "fail", literature_eligible: false,
        status: "excluded", exclusion_reason: "precheck", literature: null,
      },
      {
        oncotree_code: "GBM", cancer_type_name: "Glioblastoma",
        study_id: "gbm_tcga_pan_can_atlas_2018", stats: stats("GBM", {
          n: 0, median_log2: null, log2fc: null, cliffs_delta: null, auc: null, q_value: null,
          stats_pass: false, flags: ["small_n"],
        }), stats_pass: false, precheck: "not_run", literature_eligible: false,
        status: "excluded", exclusion_reason: "stats", literature: null,
      },
    ],
    ...overrides,
  };
}

const ALL_STUDIES = "brca_tcga_pan_can_atlas_2018,stad_tcga_pan_can_atlas_2018,gbm_tcga_pan_can_atlas_2018";

function viewWithStudyIds() {
  const v = view();
  v.pairs[2] = { ...v.pairs[2], study_id: null };
  v.pairs.push({ ...v.pairs[0] });
  return v;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("alterations render badges and safe category details on surfaced, candidate and excluded rows", async () => {
  const v = view();
  const alterations = {
    alteration_types_enriched: ["Point Mutation", "Copy Number Alteration", "<script>unknown</script>"],
    categories: {
      "Point Mutation": { status: "enriched", n_events: 24, n_profiled: 149, frequency: 0.16 },
      "Copy Number Alteration": { status: "enriched", direction: "amp", enriched_directions: ["amp", "del"],
        n_events: 74, n_profiled: 148, frequency: 0.5,
        amp: { n_events: 74, frequency: 0.5 }, del: { n_events: 3, frequency: 0.02 } },
      "Structural Variant": { status: "not_testable", reason: "<img src=x onerror=alert(1)>" },
    },
  };
  v.pairs[0].stats.alterations = alterations;
  v.pairs[1].status = "candidate";
  v.pairs[1].stats.alterations = alterations;
  v.pairs[2].stats.alterations = alterations;
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  for (let i = 0; i < 3; i++) {
    const badges = summaryRows(nodes)[i].children[3];
    assert.strictEqual(badges.children.map((n) => n.children[0].text).join(""),
      `Point MutationCopy Number Alteration (amp, del)${i === 0 ? "Expression" : ""}`);
    assert.strictEqual(badges.children.length, i === 0 ? 3 : 2);
    const detail = detailRows(nodes)[i];
    assert.ok(detail.text.includes("Alterations"));
    assert.ok(detail.text.includes("24/149 events/profiled; frequency 16.00%"));
    assert.ok(detail.text.includes("amp: 74/148"));
    assert.ok(detail.text.includes("del: 3/148"));
    assert.ok(detail.text.includes("not testable; —/— events/profiled; frequency —; <img src=x onerror=alert(1)>"));
    assert.strictEqual(detail.findAll((n) => n.tagName === "img").length, 0);
  }
  assert.strictEqual(summaryRows(nodes)[3].children[3].text, "—", "old rows need no alteration data");
});

test("surfaced rows show distinct Strong and Moderate badges with effect-size tooltips", async () => {
  const v = view();
  v.pairs[0].confidence_label = "Strong";
  v.pairs[1] = { ...v.pairs[1], status: "surfaced", confidence_label: "Moderate", confidence_tier: "candidate" };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const rows = visibleSummaryRows(nodes);
  const strong = rows[0].findAll(n => n.textContent === "Strong")[0];
  const moderate = rows[1].findAll(n => n.textContent === "Moderate")[0];
  assert.ok(strong.getAttribute("class").includes("ec-strong"));
  assert.ok(moderate.getAttribute("class").includes("ec-moderate"));
  assert.notStrictEqual(strong.getAttribute("class"), moderate.getAttribute("class"));
  assert.strictEqual(strong.getAttribute("title"), "Meets strong effect-size criteria.");
  assert.strictEqual(moderate.getAttribute("title"), "Statistically significant and literature-supported, but below the strong effect-size bar; worth a closer look.");
  assert.ok(!rows[1].text.includes("Purity concern"));
});

test("candidate-tier pending and unsupported rows use the usual literature labels", async () => {
  const v = view();
  v.pairs[1].confidence_tier = "candidate";
  v.pairs[0] = { ...v.pairs[0], status: "excluded", exclusion_reason: "literature", confidence_tier: "candidate" };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  const rows = summaryRows(nodes);
  assert.strictEqual(rows[0].children[5].text, "Excluded: no literature support");
  assert.strictEqual(rows[1].children[4].text, "Not checked yet");
  assert.strictEqual(rows[1].children[5].text, "Awaiting literature check");
});

test("formatters never show null statistics as zero", () => {
  assert.strictEqual(formatNumber(null), "—");
  assert.strictEqual(formatNumber(undefined), "—");
  assert.strictEqual(formatNumber(0), "0.00");
  assert.strictEqual(formatNumber(2.739), "2.74");
  assert.strictEqual(formatQ(null), "—");
  assert.strictEqual(formatQ(1e-39), "1.0e-39");
  assert.strictEqual(formatQ(0.0123), "0.012");
});

test("only digit PMIDs become PubMed links", () => {
  assert.strictEqual(pmidUrl("12345678"), "https://pubmed.ncbi.nlm.nih.gov/12345678/");
  assert.strictEqual(pmidUrl("javascript:alert(1)"), null);
  assert.strictEqual(pmidUrl("123/../x"), null);
});

test("renders statistics, calls, flags and the literature verdict", async () => {
  const { nodes, page, replaced } = env({
    "GET /v1/expression-context/stats/ERBB2": response(view()),
  });
  nodes["ec-gene"].value = " erbb2 ";
  await nodes["ec-form"].fire("submit", { preventDefault() {} });
  assert.ok(!nodes["ec-result"].hidden);
  assert.strictEqual(nodes["ec-gene-title"].textContent, "ERBB2 · Entrez 2064");
  assert.strictEqual(nodes["ec-summary"].textContent, "1 surfaced · 1 awaiting literature check · 2 excluded");
  assert.ok(!nodes["ec-run"].hidden, "pending pairs offer a literature check");
  const rows = summaryRows(nodes);
  const details = detailRows(nodes);
  assert.strictEqual(rows.length, 4);
  assert.strictEqual(details.length, 4);
  const cells = (row) => row.children.map((c) => c.text);
  const brca = cells(rows[0]);
  assert.strictEqual(brca.length, 8);
  assert.strictEqual(brca[0], "Breast Invasive Carcinoma (BRCA)");
  assert.strictEqual(brca[1], "Pass");
  assert.strictEqual(brca[2], "Tissue of origin");
  assert.ok(brca[4].startsWith("Supported"));
  assert.ok(brca[4].includes("<b>amplified</b>"), "rationale stays text");
  assert.strictEqual(brca[5], "Surfaced");
  assert.strictEqual(brca[6], "mRNA vs Dx");
  assert.strictEqual(brca[7], "Details");
  assert.strictEqual(rows[0].getAttribute("class"), "ec-row-surfaced");
  const links = rows[0].findAll((n) => n.tagName === "a");
  const pubmedLinks = links.filter((a) => a.getAttribute("href").startsWith("https://pubmed.ncbi.nlm.nih.gov/"));
  assert.deepStrictEqual(pubmedLinks.map((a) => a.getAttribute("href")), ["https://pubmed.ncbi.nlm.nih.gov/12345678/"]);
  assert.strictEqual(pubmedLinks[0].getAttribute("rel"), "noopener noreferrer");
  assert.strictEqual(cells(rows[1])[4], "Not checked yet");
  assert.strictEqual(cells(rows[1])[5], "Awaiting literature check");
  assert.ok(details[1].text.includes("Not run"));
  assert.strictEqual(cells(rows[2])[0], "UCEC");
  assert.strictEqual(cells(rows[2])[5], "Excluded: PubMed pre-check");
  assert.ok(details[2].text.includes("Fail (0)"));
  const gbm = cells(rows[3]);
  assert.strictEqual(gbm[1], "Fail");
  assert.strictEqual(gbm[2], "Small n");
  assert.strictEqual(gbm[5], "Excluded: statistics");
  assert.ok(details[3].text.includes("— (3.10)"));
  assert.deepStrictEqual(replaced, ["?gene=ERBB2"]);
  assert.strictEqual(nodes["ec-release"].textContent, `Data release ${BUNDLE.release_id}`);
  assert.strictEqual(page.state.view.hugo_symbol, "ERBB2");
});

test("gene-level primary link uses CANCER_STUDY across every study", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(viewWithStudyIds()) });
  await page.lookup("ERBB2");
  const links = nodes["ec-cbio"].findAll((n) => n.tagName === "a");
  const primary = links.find((a) => a.textContent === "Compare across all 32 cancer types in cBioPortal");
  assert.ok(primary, "primary comparison link is rendered");
  const p = plotParams(primary.getAttribute("href"));
  assert.strictEqual(p.studies, ALL_STUDIES, "de-duplicated and skips missing study IDs");
  assert.strictEqual(p.gene, "ERBB2");
  assert.strictEqual(p.caseSet, "all");
  assert.strictEqual(p.horizontal.dataType, "clinical_attribute");
  assert.strictEqual(p.horizontal.selectedDataSourceOption, "CANCER_STUDY");
  assert.strictEqual(p.vertical.dataType, "MRNA_EXPRESSION");
  assert.strictEqual(p.vertical.selectedDataSourceOption, "rna_seq_v2_mrna");
  assert.strictEqual(p.vertical.logScale, "true");
  assert.strictEqual(primary.getAttribute("target"), "_blank");
  assert.strictEqual(primary.getAttribute("rel"), "noopener noreferrer");
});

test("gene-level secondary link uses CANCER_TYPE_DETAILED across every study", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(viewWithStudyIds()) });
  await page.lookup("ERBB2");
  const links = nodes["ec-cbio"].findAll((n) => n.tagName === "a");
  const secondary = links.find((a) => a.textContent === "mRNA vs Dx across all 32 (detailed diagnosis)");
  assert.ok(secondary, "detailed-diagnosis comparison link is rendered");
  const p = plotParams(secondary.getAttribute("href"));
  assert.strictEqual(p.studies, ALL_STUDIES);
  assert.strictEqual(p.gene, "ERBB2");
  assert.strictEqual(p.horizontal.selectedDataSourceOption, "CANCER_TYPE_DETAILED");
  assert.strictEqual(secondary.getAttribute("target"), "_blank");
  assert.strictEqual(secondary.getAttribute("rel"), "noopener noreferrer");
});

test("each row links to its single study with the mRNA vs Dx selection", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  const links = cBioPortalLinks(nodes["ec-tbody"].children[0]);
  assert.strictEqual(links.length, 1);
  assert.strictEqual(links[0].textContent, "mRNA vs Dx");
  const p = plotParams(links[0].getAttribute("href"));
  assert.strictEqual(p.studies, "brca_tcga_pan_can_atlas_2018");
  assert.strictEqual(p.gene, "ERBB2");
  assert.strictEqual(p.horizontal.selectedDataSourceOption, "CANCER_TYPE_DETAILED");
  assert.strictEqual(links[0].getAttribute("target"), "_blank");
  assert.strictEqual(links[0].getAttribute("rel"), "noopener noreferrer");
});

test("the one-study guard never yields CANCER_STUDY", async () => {
  const direct = plotParams(expressionURL("ERBB2", ["brca_tcga_pan_can_atlas_2018"], "CANCER_STUDY"));
  assert.strictEqual(direct.horizontal.selectedDataSourceOption, "CANCER_TYPE_DETAILED");

  const v = view();
  v.pairs = [v.pairs[0]];
  v.summary = { surfaced: 1, literature_pending: 0, excluded: 0 };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const links = nodes["ec-cbio"].findAll((n) => n.tagName === "a");
  assert.strictEqual(links.length, 2);
  for (const link of links) {
    assert.notStrictEqual(plotParams(link.getAttribute("href")).horizontal.selectedDataSourceOption, "CANCER_STUDY");
  }
  const primary = links.find((a) => a.textContent === "Compare across all 32 cancer types in cBioPortal");
  assert.strictEqual(plotParams(primary.getAttribute("href")).horizontal.selectedDataSourceOption, "CANCER_TYPE_DETAILED");
});

test("a hyphenated symbol like NKX2-1 is encoded correctly", async () => {
  const v = view({ query: "nkx2-1", hugo_symbol: "NKX2-1", entrez_gene_id: 7080 });
  const { nodes, page } = env({ "GET /v1/expression-context/stats/NKX2-1": response(v) });
  await page.lookup("NKX2-1");
  const href = cBioPortalLinks(nodes["ec-tbody"].children[0])[0].getAttribute("href");
  assert.strictEqual(plotParams(href).gene, "NKX2-1");
  assert.ok(href.includes("gene_list=NKX2-1"), href);
});

test("an alias links with the resolved hugo_symbol, not the raw query", async () => {
  const v = view({ query: "her2", hugo_symbol: "ERBB2" });
  const { nodes, page } = env({ "GET /v1/expression-context/stats/HER2": response(v) });
  await page.lookup("HER2");
  const geneLink = nodes["ec-cbio"].findAll((n) => n.tagName === "a")[0];
  assert.strictEqual(plotParams(geneLink.getAttribute("href")).gene, "ERBB2");
  const rowHref = cBioPortalLinks(nodes["ec-tbody"].children[0])[0].getAttribute("href");
  assert.strictEqual(plotParams(rowHref).gene, "ERBB2");
});

test("a row without a study_id renders no cBioPortal link", async () => {
  const v = view();
  v.pairs[1] = { ...v.pairs[1], study_id: null };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  assert.deepStrictEqual(cBioPortalLinks(summaryRows(nodes)[1]), []);
  assert.strictEqual(nodes["ec-cbio"].findAll((n) => n.tagName === "a").length, 2, "other studies still compare");
});

test("excluded rows still link to cBioPortal", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  const rows = summaryRows(nodes);
  for (const index of [2, 3]) {
    const link = cBioPortalLinks(rows[index])[0];
    assert.ok(link, `excluded row ${index} should have a cBioPortal link`);
    assert.strictEqual(plotParams(link.getAttribute("href")).horizontal.selectedDataSourceOption, "CANCER_TYPE_DETAILED");
  }
});

test("an expired PubMed pre-check is labelled, not shown as a failure", async () => {
  const expired = view({
    bundle: { ...BUNDLE, source: { ...BUNDLE.source, precheck_expires_at: "2026-10-01T00:00:00Z" } },
  });
  expired.pairs[2] = {
    ...expired.pairs[2], precheck: "expired", literature_eligible: true,
    status: "literature_pending", exclusion_reason: null,
  };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(expired) });
  await page.lookup("ERBB2");
  const ucec = summaryRows(nodes)[2].children.map((c) => c.text);
  const ucecDetails = detailRows(nodes)[2].text;
  assert.ok(ucecDetails.includes("Pre-check expired (0)"), ucecDetails);
  assert.strictEqual(ucec[5], "Awaiting literature check");
});

test("method m3: enrichment labels, broad markers and subgroup statistics", async () => {
  const m3 = view({
    query: "vim", hugo_symbol: "VIM", entrez_gene_id: 7431, broadly_expressed: true,
    bundle: { ...BUNDLE, method_version: "3" },
    summary: { surfaced: 1, literature_pending: 0, excluded: 2 },
    pairs: [
      {
        oncotree_code: "BRCA", cancer_type_name: "Breast <i>Invasive</i> Carcinoma",
        stats: stats("BRCA", {
          pass_mode: "subgroup", lineage_blocked_by_broad: true, subgroup_high_count: 120,
          subgroup_high_fraction: 0.11, subgroup_high_fraction_ratio: 3.2, p95_log2_diff: 1.6,
          subgroup_q: 2e-8,
        }),
        stats_pass: true, enrichment: "Subgroup enriched", broad_blocked: false,
        precheck: "pass", literature_eligible: true, status: "surfaced", exclusion_reason: null,
        literature: {
          supported: true, insufficient_evidence: false, pmids: ["11111111"], verdict: "supported",
          cancer_type_name: "Breast Invasive Carcinoma", rationale: "VIM in breast cancer.",
          papers: [{ pmid: "11111111", tumor_specific: true }],
        },
      },
      {
        oncotree_code: "SARC", cancer_type_name: "Sarcoma",
        stats: stats("SARC", {
          stats_pass: false, pass_mode: null, lineage_blocked_by_broad: true,
          literature_precheck_pass: null, pubmed_count: null,
          subgroup_high_count: 30, subgroup_high_fraction: 0.12, subgroup_high_fraction_ratio: 1.3,
          p95_log2_diff: 0.4, subgroup_q: 0.2,
        }),
        stats_pass: false, enrichment: null, broad_blocked: true, precheck: "not_run",
        literature_eligible: false, status: "excluded", exclusion_reason: "stats", literature: null,
      },
      {
        oncotree_code: "PRAD", cancer_type_name: "Prostate Adenocarcinoma",
        stats: stats("PRAD", { pass_mode: "lineage", lineage_blocked_by_broad: false }),
        stats_pass: true, enrichment: "Lineage enriched", broad_blocked: false, precheck: "pass",
        literature_eligible: true, status: "excluded", exclusion_reason: "literature",
        literature: {
          supported: false, insufficient_evidence: true, pmids: [], verdict: "insufficient",
          rationale: "No paper reports VIM in prostate cancer.", papers: [],
        },
      },
    ],
  });
  const { nodes, page } = env({ "GET /v1/expression-context/stats/VIM": response(m3) });
  await page.lookup("vim");
  const title = nodes["ec-gene-title"];
  assert.strictEqual(title.textContent, "VIM · Entrez 7431");
  assert.strictEqual(title.text, "VIM · Entrez 7431Broadly expressed");
  const rows = summaryRows(nodes);
  const details = detailRows(nodes);
  const cells = (row) => row.children.map((c) => c.text);
  const brca = cells(rows[0]);
  assert.strictEqual(brca[0], "Breast <i>Invasive</i> Carcinoma (BRCA)", "names stay text");
  assert.strictEqual(brca[1], "PassSubgroup enriched");
  assert.ok(!brca[1].includes("Subgroup:"), "subgroup numbers moved out of the summary Statistics cell");
  assert.ok(
    details[0].text.includes("Subgroup: high n 120 (11.0%) · ratio 3.20× · p95 Δlog2 1.60 · q 2.0e-8"),
    details[0].text,
  );
  assert.ok(!brca[1].includes("Broadly expressed"), "a subgroup pass is not marked blocked");
  assert.ok(brca[4].startsWith("Supported in Breast Invasive Carcinoma"), brca[4]);
  assert.ok(brca[4].includes("Tumor-specific: PMID 11111111"), brca[4]);
  const sarc = cells(rows[1]);
  assert.strictEqual(sarc[1], "FailBroadly expressed");
  assert.strictEqual(sarc[4], "—", "never sent to the literature check");
  assert.strictEqual(sarc[5], "Excluded: statistics");
  const prad = cells(rows[2]);
  assert.strictEqual(prad[1], "PassLineage enriched");
  assert.ok(!details[2].text.includes("Subgroup:"), "no subgroup line without subgroup stats");
  assert.ok(prad[4].startsWith("Not supported insufficient evidence"), prad[4]);
  assert.deepStrictEqual(rows[2].findAll((n) => n.tagName === "a"), []);
});

test("tumor-specific verdict without tumor-specific PMIDs explains why", async () => {
  const v = view();
  v.pairs[0] = {
    ...v.pairs[0], status: "excluded", exclusion_reason: "literature",
    literature: { supported: false, insufficient_evidence: false, pmids: [], verdict: "supported", papers: [] },
  };
  v.pairs[1] = {
    ...v.pairs[1], status: "excluded", exclusion_reason: "literature",
    literature: { supported: false, insufficient_evidence: false, pmids: [], verdict: "not_supported" },
  };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const rows = summaryRows(nodes).map((row) => row.children.map((c) => c.text));
  assert.strictEqual(rows[0][4], "Not supported no tumor-specific PMIDs");
  assert.strictEqual(rows[1][4], "Not supported evidence against this tumor type");
  assert.strictEqual(nodes["ec-gene-title"].children.length, 0, "no gene marker for m2 bundles");
});

test("invalid input is rejected without a request", async () => {
  const { nodes, calls } = env({});
  nodes["ec-gene"].value = "BAD$GENE";
  await nodes["ec-form"].fire("submit", { preventDefault() {} });
  assert.strictEqual(calls.length, 0);
  assert.ok(!nodes["ec-error"].hidden);
  assert.match(nodes["ec-error"].textContent, /gene symbol/);
});

for (const [status, pattern] of [
  [503, /^Expression data hasn't loaded yet\. Try again in a few minutes\.$/],
  [404, /^NOPE1 is not in the current expression data\.$/],
  [500, /couldn't load/],
]) {
  test(`HTTP ${status} shows a clear message`, async () => {
    const { nodes, page } = env({ "GET /v1/expression-context/stats/NOPE1": response({ detail: "x" }, status) });
    await page.lookup("nope1");
    assert.match(nodes["ec-error"].textContent, pattern);
    assert.ok(nodes["ec-result"].hidden);
    assert.ok(!nodes["ec-submit"].disabled);
  });
}

test("401 sends the browser through login and back", async () => {
  const { page, window } = env({ "GET /v1/expression-context/stats/ERBB2": response({}, 401) }, { search: "?gene=ERBB2" });
  await page.lookup("ERBB2");
  assert.strictEqual(window.location.href, "/login?redirect_to=%2Fexpression-context%3Fgene%3DERBB2");
});

test("literature check: POST, poll until done, then refresh the statistics", async () => {
  const states = ["queued", "running", "succeeded"];
  let statsCalls = 0;
  const { nodes, page, calls } = env({
    "GET /v1/expression-context/stats/ERBB2": () => {
      statsCalls += 1;
      return response(statsCalls === 1 ? view() : view({ summary: { surfaced: 2, literature_pending: 0, excluded: 2 } }));
    },
    "POST /v1/expression-context/jobs": response({ job_id: "j1", status: "queued", status_url: "/v1/expression-context/jobs/j1" }, 202),
    "GET /v1/expression-context/jobs/j1": () => response({
      status: states.shift(), literature_evaluated: 1, literature_reused: 2, pair_errors: [],
    }),
  });
  await page.lookup("ERBB2");
  await nodes["ec-run"].fire("click");
  const post = calls.find((c) => c.method === "POST");
  assert.deepStrictEqual(JSON.parse(post.body), { gene: "ERBB2" });
  assert.strictEqual(calls.filter((c) => c.url.endsWith("/jobs/j1")).length, 3);
  assert.strictEqual(statsCalls, 2);
  assert.strictEqual(nodes["ec-job-status"].textContent, "Literature check finished: 1 evaluated, 2 reused.");
  assert.ok(nodes["ec-run"].hidden, "nothing left to check");
});

test("a failed job or a foreign status URL is reported, not followed", async () => {
  for (const accepted of [
    { body: { job_id: "j", status: "queued", status_url: "https://evil.example/jobs/j" }, polls: 0 },
    { body: { job_id: "j", status: "queued", status_url: "/v1/expression-context/jobs/j" }, polls: 1 },
  ]) {
    const { nodes, page, calls } = env({
      "GET /v1/expression-context/stats/ERBB2": response(view()),
      "POST /v1/expression-context/jobs": response(accepted.body, 202),
      "GET /v1/expression-context/jobs/j": response({ status: "failed" }),
    });
    await page.lookup("ERBB2");
    await page.runLiteratureCheck();
    assert.strictEqual(calls.filter((c) => c.url.includes("/jobs/j")).length, accepted.polls);
    assert.ok(!nodes["ec-error"].hidden);
    assert.ok(!nodes["ec-run"].hidden, "the check can be retried");
  }
});

test("the page no longer shows the statistics bundle or calls /bundle", async () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  assert.ok(!html.includes("ec-provenance"), "provenance card is gone");
  assert.ok(!/statistics bundle/i.test(html), "no bundle wording in the page");
  const js = fs.readFileSync(path.join(__dirname, "../../src/expression_context/static/expression-context.js"), "utf8");
  assert.ok(!js.includes("/bundle"), "page code never requests /bundle");
  for (const wording of ["statistics bundle", "bundle status", "Bundle"]) {
    assert.ok(!js.includes(wording), `no user-facing "${wording}" wording`);
  }

  // Init without ?gene= makes no requests at all; with ?gene= only the stats lookup.
  const empty = env({});
  await empty.page.init();
  assert.deepStrictEqual(empty.calls, []);
  const deep = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) }, { search: "?gene=erbb2" });
  await deep.page.init();
  assert.deepStrictEqual(deep.calls.map((c) => c.url), ["/v1/expression-context/stats/ERBB2"]);
});

test("the summary shows only the data release, not bundle internals", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  const text = nodes["ec-release"].textContent;
  assert.strictEqual(text, `Data release ${BUNDLE.release_id}`);
  assert.ok(!/method|PubMed snapshot/.test(text));
});

test("the table empty state is a polite live status region", () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  const tag = html.match(/<p\b[^>]*id="ec-table-empty"[^>]*>/);
  assert.ok(tag, "#ec-table-empty exists");
  assert.ok(tag[0].includes('role="status"'), tag[0]);
  assert.ok(tag[0].includes('aria-live="polite"'), tag[0]);
});

test("deep link ?gene= loads the gene on init", async () => {
  const { nodes, calls } = env({
    "GET /v1/expression-context/stats/HER2": response(view({ query: "her2" })),
  }, { search: "?gene=her2" });
  const page = createExpressionContextPage({
    document: { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) },
    window: { location: { search: "?gene=her2", pathname: "/expression-context" }, history: null, setTimeout: (fn) => fn() },
    fetch: async (url) => {
      assert.ok(!url.endsWith("/bundle"), "init must not request /bundle");
      return response(view({ query: "her2" }));
    },
  });
  await page.init();
  assert.strictEqual(nodes["ec-gene"].value, "her2");
  assert.strictEqual(nodes["ec-gene-title"].textContent, "ERBB2 (from her2) · Entrez 2064");
  assert.strictEqual(calls.length, 0);
});

test("header has only the visible columns and removed columns are gone", () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  const theadMatch = html.match(/<thead>([\s\S]*?)<\/thead>/);
  assert.ok(theadMatch, "table has a thead");
  const headers = [...theadMatch[1].matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/g)].map((m) => m[1].trim());
  assert.deepStrictEqual(headers, [
    "Cancer type",
    "Statistics",
    "Flags",
    "Alterations",
    "Literature",
    "Result",
    "cBioPortal",
    "",
  ]);
  for (const removed of ["n (rest)", "Median (rest)", "log2FC", "Cliff's δ", "Cliff's delta", "AUC", "q", "PubMed pre-check"]) {
    assert.ok(!headers.includes(removed), `removed column ${removed} must not be in thead`);
  }
});

test("numbers are hidden in the detail row by default and toggle updates aria-expanded", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  const summaries = summaryRows(nodes);
  const details = detailRows(nodes);
  assert.strictEqual(summaries.length, 4);
  assert.strictEqual(details.length, 4);
  for (let i = 0; i < summaries.length; i += 1) {
    assert.ok(!summaries[i].hidden, "summary row is visible");
    assert.ok(details[i].hidden, "detail row is hidden by default");
    assert.strictEqual(details[i].children.length, 1);
    assert.strictEqual(details[i].children[0].getAttribute("colspan"), "8");
  }
  const brcaSummaryText = summaries[0].text;
  for (const num of ["100 (9000)", "6.20 (3.10)", "2.74", "0.71", "0.85", "1.0e-39", "Pass (12)"]) {
    assert.ok(!brcaSummaryText.includes(num), `${num} is not in the collapsed summary row`);
    assert.ok(details[0].text.includes(num), `${num} is in the detail row`);
  }
  for (const label of [
    "n (rest)",
    "Primary tumors in this type (all other TCGA tumors)",
    "Median (rest)",
    "Median log2(RSEM + 1) in this type (all other tumors)",
    "log2FC",
    "Floor-clamped median difference, log2",
    "Cliff's delta",
    "AUC",
    "q",
    "Benjamini–Hochberg q across the cancer types for this gene",
    "Subgroup statistics",
    "PubMed pre-check",
    "PubMed count for the gene and tumor type",
  ]) {
    assert.ok(details[0].text.includes(label), `detail row includes ${label}`);
  }
  const toggleBtn = summaries[0].children[7].children[0];
  assert.strictEqual(toggleBtn.tagName, "button");
  assert.strictEqual(toggleBtn.getAttribute("type"), "button");
  assert.strictEqual(toggleBtn.textContent, "Details");
  assert.strictEqual(toggleBtn.getAttribute("aria-expanded"), "false");
  assert.strictEqual(toggleBtn.getAttribute("aria-controls"), details[0].getAttribute("id"));

  toggleBtn.fire("click");
  assert.strictEqual(toggleBtn.getAttribute("aria-expanded"), "true");
  assert.ok(!details[0].hidden, "clicking Details expands the detail row");
  assert.ok(details[1].hidden, "other detail rows remain hidden");

  toggleBtn.fire("click");
  assert.strictEqual(toggleBtn.getAttribute("aria-expanded"), "false");
  assert.ok(details[0].hidden, "clicking Details again collapses the detail row");
});

test("expanded state survives a re-render during a literature job and resets on a new gene", async () => {
  let statsCalls = 0;
  const { nodes, page } = env({
    "GET /v1/expression-context/stats/ERBB2": () => {
      statsCalls += 1;
      return response(statsCalls === 1 ? view() : view({ summary: { surfaced: 2, literature_pending: 0, excluded: 2 } }));
    },
    "POST /v1/expression-context/jobs": response(
      { job_id: "j1", status: "queued", status_url: "/v1/expression-context/jobs/j1" },
      202,
    ),
    "GET /v1/expression-context/jobs/j1": response({
      status: "succeeded", literature_evaluated: 1, literature_reused: 0, pair_errors: [],
    }),
    "GET /v1/expression-context/stats/NKX2-1": response(
      view({ query: "nkx2-1", hugo_symbol: "NKX2-1", entrez_gene_id: 7080 }),
    ),
  });
  await page.lookup("ERBB2");
  const initialBtn = summaryRows(nodes)[1].children[7].children[0];
  initialBtn.fire("click");
  assert.strictEqual(initialBtn.getAttribute("aria-expanded"), "true");
  assert.ok(!detailRows(nodes)[1].hidden);

  await nodes["ec-run"].fire("click");
  assert.strictEqual(statsCalls, 2);
  const rerenderedBtn = summaryRows(nodes)[1].children[7].children[0];
  assert.strictEqual(rerenderedBtn.getAttribute("aria-expanded"), "true", "STAD stays expanded after job re-render");
  assert.ok(!detailRows(nodes)[1].hidden, "STAD detail row stays visible after job re-render");
  assert.ok(detailRows(nodes)[0].hidden, "BRCA detail row stays hidden");

  await page.lookup("NKX2-1");
  for (const row of summaryRows(nodes)) {
    const btn = row.children[7].children[0];
    assert.strictEqual(btn.getAttribute("aria-expanded"), "false", "new gene resets aria-expanded");
  }
  for (const detail of detailRows(nodes)) {
    assert.ok(detail.hidden, "new gene resets detail rows to hidden");
  }
});

test("CSV download link appears after a gene loads, encodes the symbol, and follows the view toggle", async () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  assert.ok(html.includes('id="ec-export"'), "HTML includes Download CSV link");
  assert.ok(html.includes("Download CSV"), "HTML labels Download CSV link");
  assert.ok(html.includes('id="ec-export-golden"'), "HTML includes Download golden-set row link");
  assert.ok(html.includes("Download golden-set row"), "HTML labels Download golden-set row link");
  assert.ok(!html.includes('type="checkbox"'), "the include-excluded checkbox is gone");
  assert.ok(html.includes('id="ec-view-shown"') && html.includes("Shown only"), "HTML includes the Shown only view");
  assert.ok(html.includes('id="ec-view-all"') && html.includes("All cancer types"), "HTML includes the All view");

  assert.strictEqual(
    exportURL("A/B C", false),
    "/v1/expression-context/genes/A%2FB%20C/export.csv",
  );
  assert.strictEqual(
    exportURL("A/B C", true),
    "/v1/expression-context/genes/A%2FB%20C/export.csv?include_excluded=true",
  );
  assert.strictEqual(
    goldenSetExportURL("A/B C"),
    "/v1/expression-context/golden-set.csv?genes=A%2FB%20C",
  );

  const { nodes, page } = env({
    "GET /v1/expression-context/stats/HER2": response(view({ query: "her2", hugo_symbol: "ERBB2" })),
    "GET /v1/expression-context/stats/NKX2-1": response(
      view({ query: "nkx2-1", hugo_symbol: "NKX2-1", entrez_gene_id: 7080 }),
    ),
  });
  assert.ok(nodes["ec-export"].hidden, "Download CSV hidden before a gene loads");
  assert.ok(nodes["ec-export-golden"].hidden, "Download golden-set row hidden before a gene loads");

  await page.lookup("HER2");
  assert.ok(!nodes["ec-export"].hidden, "Download CSV visible once a gene loads");
  assert.ok(!nodes["ec-export-golden"].hidden, "Download golden-set row visible once a gene loads");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/ERBB2/export.csv",
  );
  assert.strictEqual(
    nodes["ec-export-golden"].getAttribute("href"),
    "/v1/expression-context/golden-set.csv?genes=ERBB2",
  );

  nodes["ec-view-all"].fire("click");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/ERBB2/export.csv?include_excluded=true",
  );
  assert.strictEqual(
    nodes["ec-export-golden"].getAttribute("href"),
    "/v1/expression-context/golden-set.csv?genes=ERBB2",
  );

  // A different gene starts back on "Shown only".
  await page.lookup("NKX2-1");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/NKX2-1/export.csv",
  );
  assert.strictEqual(
    nodes["ec-export-golden"].getAttribute("href"),
    "/v1/expression-context/golden-set.csv?genes=NKX2-1",
  );

  nodes["ec-view-all"].fire("click");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/NKX2-1/export.csv?include_excluded=true",
  );

  nodes["ec-view-shown"].fire("click");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/NKX2-1/export.csv",
  );
  assert.strictEqual(
    nodes["ec-export-golden"].getAttribute("href"),
    "/v1/expression-context/golden-set.csv?genes=NKX2-1",
  );
});

test("CSV download link hidden when gene has no stored results for the active release", async () => {
  const pendingView = view({
    query: "pending_gene",
    hugo_symbol: "PEND1",
    has_stored_result: false,
    pairs: [
      {
        oncotree_code: "BRCA",
        stats: stats("BRCA"),
        stats_pass: true,
        precheck: "pass",
        literature_eligible: true,
        status: "literature_pending",
        exclusion_reason: null,
        literature: null,
      },
    ],
  });
  const storedView = view({
    query: "stored_gene",
    hugo_symbol: "STORED1",
    has_stored_result: true,
  });
  const { nodes, page } = env({
    "GET /v1/expression-context/stats/PEND1": response(pendingView),
    "GET /v1/expression-context/stats/STORED1": response(storedView),
  });

  await page.lookup("PEND1");
  assert.ok(nodes["ec-export"].hidden, "Download CSV hidden while results are pending / no stored results");
  assert.ok(nodes["ec-export-golden"].hidden, "Download golden-set row hidden while results are pending");

  await page.lookup("STORED1");
  assert.ok(!nodes["ec-export"].hidden, "Download CSV visible once stored results exist");
  assert.ok(!nodes["ec-export-golden"].hidden, "Download golden-set row visible once stored results exist");
});

function visibleSummaryRows(nodes) {
  return summaryRows(nodes).filter((r) => !r.hidden);
}

test("view toggle switches the table rows and the count", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  const code = (row) => row.children[0].text;
  // Default: only the pairs that are shown (surfaced + awaiting literature).
  assert.deepStrictEqual(visibleSummaryRows(nodes).map(code), ["Breast Invasive Carcinoma (BRCA)", "Stomach Adenocarcinoma (STAD)"]);
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 2 excluded (hidden)");
  assert.ok(nodes["ec-table-empty"].hidden);

  nodes["ec-view-all"].fire("click");
  const all = visibleSummaryRows(nodes);
  assert.strictEqual(all.length, 4);
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 2 excluded");
  // Excluded rows are de-emphasised and labelled with their reason.
  const excluded = all.filter((r) => r.getAttribute("class") === "ec-row-excluded");
  assert.deepStrictEqual(excluded.map((r) => r.children[5].text), ["Excluded: PubMed pre-check", "Excluded: statistics"]);

  nodes["ec-view-shown"].fire("click");
  assert.strictEqual(visibleSummaryRows(nodes).length, 2);
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 2 excluded (hidden)");
});

test("an expanded excluded row hides with its details and comes back expanded", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  summaryRows(nodes)[3].children[7].children[0].fire("click");
  assert.ok(!detailRows(nodes)[3].hidden);
  nodes["ec-view-shown"].fire("click");
  assert.ok(summaryRows(nodes)[3].hidden && detailRows(nodes)[3].hidden, "GBM row and details hidden");
  nodes["ec-view-all"].fire("click");
  assert.ok(!summaryRows(nodes)[3].hidden && !detailRows(nodes)[3].hidden, "GBM details still expanded");
});

test("view toggle updates aria-pressed, the active state and the CSV URL", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  const pressed = () => [nodes["ec-view-shown"], nodes["ec-view-all"]].map((b) => b.getAttribute("aria-pressed"));
  const active = () => [nodes["ec-view-shown"], nodes["ec-view-all"]].map((b) => b.classList.contains("active"));
  assert.deepStrictEqual(pressed(), ["true", "false"]);
  assert.deepStrictEqual(active(), [true, false]);
  assert.strictEqual(nodes["ec-export"].getAttribute("href"), "/v1/expression-context/genes/ERBB2/export.csv");

  nodes["ec-view-all"].fire("click");
  assert.deepStrictEqual(pressed(), ["false", "true"]);
  assert.deepStrictEqual(active(), [false, true]);
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/ERBB2/export.csv?include_excluded=true",
  );
  assert.strictEqual(nodes["ec-export-golden"].getAttribute("href"), "/v1/expression-context/golden-set.csv?genes=ERBB2");

  nodes["ec-view-shown"].fire("click");
  assert.deepStrictEqual(pressed(), ["true", "false"]);
  assert.strictEqual(nodes["ec-export"].getAttribute("href"), "/v1/expression-context/genes/ERBB2/export.csv");
});

test("a gene with no shown cancer types gets an empty state pointing at the All view", async () => {
  const v = view();
  v.pairs = v.pairs.slice(2);
  v.summary = { surfaced: 0, literature_pending: 0, excluded: 2 };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  assert.ok(!nodes["ec-table-empty"].hidden);
  assert.ok(nodes["ec-table-empty"].textContent.includes("All cancer types"));
  assert.ok(nodes["ec-table-empty"].textContent.includes("2 excluded"));
  assert.ok(nodes["ec-table"].hidden);
  nodes["ec-view-all"].fire("click");
  assert.ok(nodes["ec-table-empty"].hidden);
  assert.ok(!nodes["ec-table"].hidden);
  assert.strictEqual(visibleSummaryRows(nodes).length, 2);
});

test("toolbar holds the view toggle and both downloads; release shows in the summary", async () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  const toolbar = html.match(/<div class="ec-toolbar" id="ec-toolbar">([\s\S]*?)<p class="api-keys-empty ec-table-empty/);
  assert.ok(toolbar, "page has a toolbar above the table");
  for (const id of ["ec-view-shown", "ec-view-all", "ec-view-count", "ec-export", "ec-export-golden"]) {
    assert.ok(toolbar[1].includes(`id="${id}"`), `${id} lives in the toolbar`);
  }
  assert.ok(toolbar[1].indexOf('id="ec-view-shown"') < toolbar[1].indexOf('id="ec-export"'), "toggle is left of downloads");
  assert.ok(/role="group"/.test(toolbar[1]) && /aria-pressed="true"/.test(toolbar[1]), "toggle is an aria-pressed group");

  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(view()) });
  await page.lookup("ERBB2");
  assert.ok(!nodes["ec-export"].hidden && !nodes["ec-export-golden"].hidden, "both downloads render");
  assert.ok(nodes["ec-release"].textContent.includes(BUNDLE.release_id));
});

test("the cBioPortal reading guide is collapsed by default and keeps its content", () => {
  const html = fs.readFileSync(HTML_PATH, "utf8");
  const help = html.match(/<details class="ec-help" id="ec-cbio-help"([^>]*)>([\s\S]*?)<\/details>/);
  assert.ok(help, "reading guide is a <details> disclosure");
  assert.ok(!/\bopen\b/.test(help[1]), "collapsed by default");
  assert.ok(/<summary>[\s\S]*How to read these plots[\s\S]*<\/summary>/.test(help[2]));
  const text = help[2].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
  for (const phrase of [
    "single-cancer plots auto-scale their y-axis",
    "detailed diagnosis categories",
    "only part of BRCA, not the whole cohort",
    "cluster of high dots rather than the median",
    "metastatic or recurrent samples and repeat patients",
    "not a reproduction of our test",
    "genomic alteration, not this expression result",
    "can differ from the release shown here",
  ]) {
    assert.ok(text.includes(phrase), `guide keeps: ${phrase}`);
  }
  assert.ok(!html.includes("ec-cbio-note"), "the old always-visible note is gone");
});

test("looking up a different gene resets the view toggle to Shown only", async () => {
  const { nodes, page } = env({
    "GET /v1/expression-context/stats/ERBB2": response(view()),
    "GET /v1/expression-context/stats/NKX2-1": response(
      view({ query: "nkx2-1", hugo_symbol: "NKX2-1", entrez_gene_id: 7080 }),
    ),
  });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  assert.strictEqual(page.state.showAll, true);
  assert.strictEqual(nodes["ec-view-all"].getAttribute("aria-pressed"), "true");

  await page.lookup("NKX2-1");
  assert.strictEqual(page.state.showAll, false);
  assert.strictEqual(nodes["ec-view-shown"].getAttribute("aria-pressed"), "true");
  assert.strictEqual(nodes["ec-view-all"].getAttribute("aria-pressed"), "false");
  assert.ok(nodes["ec-view-shown"].classList.contains("active"));
  assert.ok(!nodes["ec-view-all"].classList.contains("active"));
  assert.strictEqual(nodes["ec-export"].getAttribute("href"), "/v1/expression-context/genes/NKX2-1/export.csv");
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 2 excluded (hidden)");
  assert.strictEqual(summaryRows(nodes).filter((r) => !r.hidden).length, 2);
});

test("re-rendering the same gene (literature job refresh) keeps the All view", async () => {
  let statsCalls = 0;
  const { nodes, page } = env({
    "GET /v1/expression-context/stats/ERBB2": () => {
      statsCalls += 1;
      return response(statsCalls === 1 ? view() : view({ summary: { surfaced: 2, literature_pending: 0, excluded: 2 } }));
    },
    "POST /v1/expression-context/jobs": response(
      { job_id: "j1", status: "queued", status_url: "/v1/expression-context/jobs/j1" },
      202,
    ),
    "GET /v1/expression-context/jobs/j1": response({
      status: "succeeded", literature_evaluated: 1, literature_reused: 0, pair_errors: [],
    }),
  });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  await nodes["ec-run"].fire("click");
  assert.strictEqual(statsCalls, 2, "the job re-rendered the same gene");
  assert.strictEqual(page.state.showAll, true);
  assert.strictEqual(nodes["ec-view-all"].getAttribute("aria-pressed"), "true");
  assert.strictEqual(nodes["ec-view-shown"].getAttribute("aria-pressed"), "false");
  assert.strictEqual(
    nodes["ec-export"].getAttribute("href"),
    "/v1/expression-context/genes/ERBB2/export.csv?include_excluded=true",
  );
  assert.strictEqual(summaryRows(nodes).filter((r) => !r.hidden).length, 4);
});

function viewWithPurityConcern() {
  const v = view();
  v.summary = { surfaced: 1, literature_pending: 1, excluded: 3, candidate: 0 };
  v.pairs.push({
    oncotree_code: "KIRC", cancer_type_name: "Kidney <b>Renal</b> Clear Cell Carcinoma",
    study_id: "kirc_tcga_pan_can_atlas_2018", stats: stats("KIRC", { confidence_tier: "candidate", purity_check: "demoted" }),
    stats_pass: true, enrichment: "Lineage enriched", confidence_tier: "candidate", precheck: "pass",
    literature_eligible: false, status: "excluded", exclusion_reason: "purity", literature: null,
  });
  return v;
}

test("purity-demoted pairs show a distinct Purity concern badge only in All cancer types", async () => {
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(viewWithPurityConcern()) });
  await page.lookup("ERBB2");
  const code = (row) => row.children[0].text;
  assert.strictEqual(
    nodes["ec-summary"].textContent,
    "1 surfaced · 1 awaiting literature check · 3 excluded",
  );
  // "Shown only" never shows a candidate.
  assert.deepStrictEqual(visibleSummaryRows(nodes).map(code), ["Breast Invasive Carcinoma (BRCA)", "Stomach Adenocarcinoma (STAD)"]);
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 3 excluded (hidden)");

  nodes["ec-view-all"].fire("click");
  const rows = visibleSummaryRows(nodes);
  assert.strictEqual(rows.length, 5);
  const kirc = rows.find((r) => code(r).includes("(KIRC)"));
  assert.strictEqual(code(kirc), "Kidney <b>Renal</b> Clear Cell Carcinoma (KIRC)", "names stay text");
  assert.strictEqual(kirc.getAttribute("class"), "ec-row-excluded");
  const [result] = kirc.children[5].children;
  assert.strictEqual(result.textContent, "Purity concern");
  assert.ok(!result.textContent.startsWith("Excluded"), "separate from other exclusion reasons");
  assert.ok(result.getAttribute("class").includes("ec-candidate"));
  const tooltip = result.getAttribute("title");
  assert.strictEqual(tooltip, "Expression may come from non-tumor cells (falls with tumor purity)");
  // Not sent to the literature check, so no "Not checked yet" prompt and no citations.
  assert.strictEqual(kirc.children[4].text, "—");
  assert.strictEqual(nodes["ec-view-count"].textContent, "2 shown · 3 excluded");
  // Other exclusion reasons keep their own badges.
  const others = rows.filter((r) => r.getAttribute("class") === "ec-row-excluded");
  assert.deepStrictEqual(others.map((r) => r.children[5].text), ["Excluded: PubMed pre-check", "Excluded: statistics", "Purity concern"]);

  nodes["ec-view-shown"].fire("click");
  assert.ok(kirc.hidden, "candidate hidden again in Shown only");
});

test("a gene with only exclusions shows the empty state in Shown only", async () => {
  const v = viewWithPurityConcern();
  v.pairs = v.pairs.filter((p) => p.status !== "surfaced" && p.status !== "literature_pending");
  v.summary = { surfaced: 0, literature_pending: 0, excluded: 3, candidate: 0 };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  assert.strictEqual(visibleSummaryRows(nodes).length, 0);
  assert.ok(!nodes["ec-table-empty"].hidden);
  assert.ok(nodes["ec-table-empty"].textContent.includes("review the 3 excluded"), nodes["ec-table-empty"].textContent);
});

test("legacy candidate status and reason still render", async () => {
  const v = viewWithPurityConcern();
  v.summary = { surfaced: 1, literature_pending: 1, excluded: 2, candidate: 1 };
  const legacy = v.pairs.find(p => p.exclusion_reason === "purity");
  legacy.status = "candidate";
  legacy.exclusion_reason = "candidate";
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const row = summaryRows(nodes).find(r => r.text.includes("(KIRC)"));
  assert.ok(row.hidden);
  nodes["ec-view-all"].fire("click");
  assert.ok(!row.hidden);
  assert.ok(row.text.includes("Purity concern"));
  assert.ok(nodes["ec-view-count"].textContent.includes("1 purity concern"));
});

test("invalid required purity explains why the high pair is excluded", async () => {
  for (const purity of [null, "PASS"]) {
    const v = viewWithPurityConcern();
    const blocked = v.pairs.find(p => p.exclusion_reason === "purity");
    blocked.stats.purity_check = purity;
    blocked.stats.confidence_tier = "high";
    blocked.confidence_tier = null;
    const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
    await page.lookup("ERBB2");
    nodes["ec-view-all"].fire("click");
    const row = summaryRows(nodes).find(r => r.text.includes("(KIRC)"));
    const badge = row.findAll(n => n.textContent === "Purity concern")[0];
    assert.strictEqual(badge.getAttribute("title"), "Purity status is missing or unknown; high-tier pairs require a known retaining state.");
    assert.strictEqual(row.children[4].text, "—");
  }
});

test("badges and details share the canonical alteration accessor with legacy fallback", async () => {
  const detail = {
    alteration_types_enriched: ["Point Mutation"],
    categories: { "Point Mutation": { status: "enriched", n_events: 3, n_profiled: 10, frequency: 0.3 } },
  };
  for (const shape of ["canonical", "legacy", "both"]) {
    const v = view();
    if (shape !== "legacy") v.pairs[0].alterations = detail;
    if (shape !== "canonical") v.pairs[0].stats.alterations = shape === "both"
      ? { alteration_types_enriched: ["Structural Variant"], categories: {} } : detail;
    const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
    await page.lookup("ERBB2");
    assert.strictEqual(summaryRows(nodes)[0].children[3].children[0].children[0].text, "Point Mutation");
    assert.ok(detailRows(nodes)[0].text.includes("3/10 events/profiled; frequency 30.00%"));
  }
});

test("alteration buttons show scoped PubMed papers and preserve expansion only for the same gene", async () => {
  const v = view();
  v.pairs[0].stats.alterations = {
    alteration_types_enriched: ["Point Mutation", "Expression"], categories: {},
  };
  v.pairs[0].literature.alteration_pmids = { "Point Mutation": ["12345678", "99999999"] };
  v.pairs[0].literature.papers = [{ pmid: "12345678", title: "Trusted title" }];
  const next = { ...v, hugo_symbol: "EGFR", query: "EGFR" };
  const { nodes, page } = env({
    "GET /v1/expression-context/stats/ERBB2": response(v),
    "GET /v1/expression-context/stats/EGFR": response(next),
  });
  await page.lookup("ERBB2");
  let badges = summaryRows(nodes)[0].children[3].children;
  let button = badges[0].children[0];
  let panel = badges[0].children[1];
  assert.strictEqual(button.tagName, "button");
  assert.strictEqual(button.getAttribute("type"), "button");
  assert.strictEqual(button.getAttribute("aria-expanded"), "false");
  assert.ok(panel.hidden);
  button.fire("click");
  assert.strictEqual(button.getAttribute("aria-expanded"), "true");
  assert.ok(!panel.hidden);
  assert.strictEqual(panel.children.length, 1, "PMIDs outside the final tumor-specific set are omitted");
  assert.strictEqual(panel.children[0].getAttribute("href"), "https://pubmed.ncbi.nlm.nih.gov/12345678/");
  assert.ok(panel.text.includes("Trusted title"));
  assert.strictEqual(badges[1].children[1].text, "No alteration-specific paper found");
  await page.lookup("ERBB2");
  badges = summaryRows(nodes)[0].children[3].children;
  assert.strictEqual(badges[0].children[0].getAttribute("aria-expanded"), "true");
  await page.lookup("EGFR");
  badges = summaryRows(nodes)[0].children[3].children;
  assert.strictEqual(badges[0].children[0].getAttribute("aria-expanded"), "false");
});

test("old alteration verdicts prompt a re-run and DMT index highlights rows in both views", async () => {
  const v = view();
  v.pairs[0].stats.alterations = { alteration_types_enriched: ["Point Mutation"], categories: {} };
  v.dmt_adc_index = [
    { tissue_type: "Breast", tcga_codes: ["BRCA"], from: "Curator", notes: "ADC note" },
    { tissue_type: "Uterine", tcga_codes: ["UCEC"], from: "Curator", notes: "Review" },
    { tissue_type: "SCLC", tcga_codes: [], from: "Team", notes: "" },
  ];
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const button = summaryRows(nodes)[0].children[3].children[0].children[0];
  button.fire("click");
  assert.ok(summaryRows(nodes)[0].children[3].text.includes("Re-run literature check to see alteration-specific papers"));
  assert.ok(nodes["ec-dmt-index"].text.includes("SCLC (no TCGA cohort)"));
  assert.ok(summaryRows(nodes)[0].classList.contains("ec-row-dmt"));
  assert.ok(summaryRows(nodes)[0].text.includes("DMT ADC interest"));
  assert.ok(summaryRows(nodes)[0].text.includes("ADC note"));
  nodes["ec-view-all"].fire("click");
  assert.ok(summaryRows(nodes)[0].classList.contains("ec-row-dmt"));
  assert.ok(summaryRows(nodes)[2].classList.contains("ec-row-dmt"), "excluded DMT row is highlighted in All view");
  v.dmt_adc_index = [];
  await page.lookup("ERBB2");
  assert.ok(nodes["ec-dmt-index"].hidden);
  assert.ok(!summaryRows(nodes)[0].classList.contains("ec-row-dmt"));
});

test("the page never assigns innerHTML", () => {
  const source = fs.readFileSync(path.join(__dirname, "../../src/expression_context/static/expression-context.js"), "utf8");
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML/.test(source));
});

test("purity demotion has a distinct tooltip and coverage states have detail notes", async () => {
  const v = viewWithPurityConcern();
  v.pairs.find(p => p.exclusion_reason === "purity").stats.purity_check = "demoted";
  v.pairs[0].stats.purity_check = "not_applicable";
  v.pairs[1].stats.purity_check = "unassessed";
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  nodes["ec-view-all"].fire("click");
  const row = summaryRows(nodes).find(r => r.text.includes("(KIRC)"));
  const badges = row.findAll(n => n.textContent === "Purity concern");
  assert.ok(badges.some(n => n.getAttribute("title") ===
    "Expression may come from non-tumor cells (falls with tumor purity)"));
  const details = detailRows(nodes);
  assert.ok(details[0].text.includes("Not applicable to this cohort"));
  assert.ok(details[1].text.includes("Insufficient purity coverage"));
  assert.ok(details[0].text.includes("unassessed by this gate, not validated"));
  assert.ok(details[1].text.includes("unassessed by this gate, not validated"));
});

test("high-expression association stays separate from clickable frequency badges", async () => {
  const v = viewWithPurityConcern();
  v.pairs[0].stats.alteration_high_expression = {
    "Copy Number Alteration (amp)": {status: "associated_high_expression", n_altered: 11,
      n_profiled: 507, delta_median: 3.53, q: 0.0000048, or_top10: 48.6,
      p_top10: 0.0000000252, k_top10: 9, frac_top10_altered: 9/51},
    "Structural Variant": {status: "anecdotal", n_altered: 1, n_profiled: 510,
      reason: "<script>unsafe</script>"},
  };
  const { nodes, page } = env({ "GET /v1/expression-context/stats/ERBB2": response(v) });
  await page.lookup("ERBB2");
  const row = summaryRows(nodes)[0];
  const badge = row.findAll(n => n.textContent.includes("High-expression assoc."));
  assert.ok(badge.some(n => n.tagName === "span" && (n.getAttribute("class") || "").includes("ec-high-expression")));
  assert.ok(row.text.includes("anecdotal (1–2 tumors)"));
  assert.ok(detailRows(nodes)[0].text.includes("11/507 altered/profiled"));
  assert.ok(detailRows(nodes)[0].text.includes("<script>unsafe</script>"));
});

test("single view curator sheet row link and batch tab parsing, upload, submit, polling, grouped sections, download URLs, and ?batch= reload", async () => {
  assert.strictEqual(curatorSheetExportURL("A/B"), "/v1/expression-context/genes/A%2FB/curator-sheet.csv");
  assert.strictEqual(batchExportURL("b/1", false), "/v1/expression-context/batches/b%2F1/export.csv");
  assert.strictEqual(batchExportURL("b/1", true), "/v1/expression-context/batches/b%2F1/export.csv?include_excluded=true");
  assert.strictEqual(batchGoldenSetExportURL("b/1"), "/v1/expression-context/batches/b%2F1/golden-set.csv");
  assert.strictEqual(batchCuratorSheetExportURL("b/1"), "/v1/expression-context/batches/b%2F1/curator-sheet.csv");

  const parsed = parseBatchText("ERBB2, egfr\nerbb2\tKRAS");
  assert.deepStrictEqual(parsed.unique, ["ERBB2", "egfr", "KRAS"]);
  assert.strictEqual(parsed.duplicates, 1);

  const uploaded = parseUploadedFileColumn("Gene,Notes\nERBB2,amp\nEGFR,mut\n", "genes.csv");
  assert.deepStrictEqual(uploaded, ["ERBB2", "EGFR"]);

  const vErbb2 = view({ has_stored_result: true });
  vErbb2.pairs[0].confidence_label = "Strong";
  vErbb2.pairs[0].stats.alterations = {
    alteration_types_enriched: ["Copy Number Alteration"],
    categories: { "Copy Number Alteration": { status: "enriched", enriched_directions: ["amp"] } },
  };
  let pollCount = 0;
  const batchRunning = {
    batch_id: "batch-123",
    status: "running",
    release_id: BUNDLE.release_id,
    counts: { total: 2, queued: 1, running: 0, done: 0, skipped_fresh: 0, skipped_no_stats: 0, failed: 0, not_found: 1 },
    genes: [
      { position: 0, input_symbol: "HER2", hugo_symbol: "ERBB2", status: "running", error: null, notes: "duplicate of row 1", updated_at: "2026-11-17T00:00:00Z" },
      { position: 1, input_symbol: "NOPE", hugo_symbol: null, status: "not_found", error: "not in expression data", notes: null, updated_at: "2026-11-17T00:00:00Z" },
    ],
  };
  const batchDone = {
    ...batchRunning,
    status: "done",
    counts: { total: 2, queued: 0, running: 0, done: 1, skipped_fresh: 0, skipped_no_stats: 0, failed: 0, not_found: 1 },
    genes: [
      { ...batchRunning.genes[0], status: "done" },
      batchRunning.genes[1],
    ],
  };

  const { nodes, page, calls, replaced } = env({
    "GET /v1/expression-context/stats/ERBB2": response(vErbb2),
    "POST /v1/expression-context/batches": response({ batch_id: "batch-123", status: "queued", status_url: "/v1/expression-context/batches/batch-123" }, 202),
    "GET /v1/expression-context/batches/batch-123": () => {
      pollCount += 1;
      return response(pollCount === 1 ? batchRunning : batchDone);
    },
  });

  // Single view shows curator sheet row download
  await page.lookup("ERBB2");
  assert.ok(!nodes["ec-export-curator"].hidden);
  assert.strictEqual(nodes["ec-export-curator"].getAttribute("href"), "/v1/expression-context/genes/ERBB2/curator-sheet.csv");

  // Switch to Batch tab
  nodes["ec-tab-batch"].fire("click");
  assert.ok(nodes["ec-single-mode"].hidden);
  assert.ok(!nodes["ec-batch-mode"].hidden);
  assert.strictEqual(nodes["ec-tab-batch"].getAttribute("aria-pressed"), "true");

  // Input + preview count + duplicates
  nodes["ec-batch-input"].value = "HER2, HER2\nNOPE";
  nodes["ec-batch-input"].fire("input");
  assert.strictEqual(nodes["ec-batch-preview"].textContent, "2 genes (1 duplicate)");

  // File upload simulation
  await nodes["ec-batch-upload"].fire("change", {
    target: { files: [{ name: "list.csv", text: async () => "Gene,Other\nHER2,1\nHER2,2\nNOPE,3" }] },
  });
  assert.strictEqual(nodes["ec-batch-preview"].textContent, "2 genes (1 duplicate)");

  // Submit batch -> polls running then done -> renders grouped sections and download URLs
  await nodes["ec-batch-form"].fire("submit", { preventDefault() {} });
  assert.ok(replaced.includes("?batch=batch-123"));
  assert.ok(!nodes["ec-batch-result"].hidden);
  assert.strictEqual(nodes["ec-batch-summary"].textContent, "Status: done · 2/2 processed");
  assert.strictEqual(nodes["ec-batch-progress"].getAttribute("value"), "2");
  assert.strictEqual(nodes["ec-batch-progress"].getAttribute("max"), "2");
  assert.strictEqual(nodes["ec-batch-export"].getAttribute("href"), "/v1/expression-context/batches/batch-123/export.csv");
  assert.strictEqual(nodes["ec-batch-export-golden"].getAttribute("href"), "/v1/expression-context/batches/batch-123/golden-set.csv");
  assert.strictEqual(nodes["ec-batch-export-curator"].getAttribute("href"), "/v1/expression-context/batches/batch-123/curator-sheet.csv");

  // Toggle include excluded on batch per-pair export
  nodes["ec-batch-export-all"].fire("click");
  assert.strictEqual(nodes["ec-batch-export"].getAttribute("href"), "/v1/expression-context/batches/batch-123/export.csv?include_excluded=true");

  // Grouped collapsible sections
  const sections = nodes["ec-batch-genes"].children;
  assert.strictEqual(sections.length, 2);
  assert.ok(sections[0].children[0].text.includes("ERBB2 (from HER2)"));
  assert.ok(sections[1].children[0].text.includes("NOPE"));

  // Expand first gene section via native toggle -> loads stats summary with Strong/Moderate and alteration badges
  sections[0].open = true;
  sections[0].fire("toggle");
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(sections[0].text.includes("Breast Invasive Carcinoma (BRCA)"));
  assert.ok(sections[0].text.includes("Strong"));
  assert.ok(sections[0].text.includes("Copy Number Alteration (amp)"));
  assert.ok(sections[0].text.includes("Open ERBB2 in Single view"));

  // Reload with ?batch=batch-123
  const reloadEnv = env({
    "GET /v1/expression-context/batches/batch-123": response(batchDone),
  }, { search: "?batch=batch-123" });
  await reloadEnv.page.init();
  assert.strictEqual(reloadEnv.page.state.mode, "batch");
  assert.ok(!reloadEnv.nodes["ec-batch-result"].hidden);
  assert.strictEqual(reloadEnv.nodes["ec-batch-title"].textContent, "Batch batch-123");
});

test("batch with >200 genes pages all genes and listed count matches total count", async () => {
  const TOTAL = 250;
  const allGenes = Array.from({ length: TOTAL }, (_, i) => ({
    position: i,
    input_symbol: `GENE${i + 1}`,
    hugo_symbol: `GENE${i + 1}`,
    status: "done",
    error: null,
    notes: null,
    updated_at: "2026-11-17T12:00:00Z",
  }));
  const page1 = {
    batch_id: "batch-250",
    status: "done",
    release_id: "rel-1",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: allGenes.slice(0, 200),
  };
  const page2 = {
    batch_id: "batch-250",
    status: "done",
    release_id: "rel-1",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: allGenes.slice(200, 250),
  };
  const { page, nodes } = env({
    "GET /v1/expression-context/batches/batch-250": response(page1),
    "GET /v1/expression-context/batches/batch-250?offset=200&limit=200": response(page2),
  });
  await page.loadBatch("batch-250");
  assert.strictEqual(nodes["ec-batch-counts"].textContent, "250 done · 0 reused · 0 no stats · 0 queued · 0 running · 0 failed · 0 not found");
  const renderedSections = nodes["ec-batch-genes"].children;
  assert.strictEqual(renderedSections.length, 250);
  assert.ok(renderedSections[0].children[0].text.includes("GENE1"));
  assert.ok(renderedSections[249].children[0].text.includes("GENE250"));
});

test("page 2 returns 500 -> shows error banner, retries with backoff, then renders full list", async () => {
  const TOTAL = 250;
  const allGenes = Array.from({ length: TOTAL }, (_, i) => ({
    position: i,
    input_symbol: `GENE${i + 1}`,
    hugo_symbol: `GENE${i + 1}`,
    status: "done",
    error: null,
    notes: null,
    updated_at: "2026-11-17T12:00:00Z",
  }));
  const page1 = {
    batch_id: "batch-retry",
    status: "running",
    release_id: "rel-1",
    counts: { total: TOTAL, done: 200, queued: 50, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: allGenes.slice(0, 200),
  };
  const page1Done = {
    ...page1,
    status: "done",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
  };
  const page2 = {
    ...page1Done,
    genes: allGenes.slice(200, 250),
  };

  let p1Calls = 0;
  let p2Calls = 0;
  const delays = [];
  const snapshotsDuringRetry = [];

  let ref;
  ref = env(
    {
      "GET /v1/expression-context/batches/batch-retry": () => {
        p1Calls += 1;
        return response(p1Calls >= 3 ? page1Done : page1);
      },
      "GET /v1/expression-context/batches/batch-retry?offset=200&limit=200": () => {
        p2Calls += 1;
        if (p2Calls <= 2) return response({ detail: "internal error" }, 500);
        return response(page2);
      },
    },
    {
      pollMs: 1000,
      wait: async (ms) => {
        delays.push(ms);
        snapshotsDuringRetry.push({
          errorHidden: ref.nodes["ec-error"].hidden,
          errorText: ref.nodes["ec-error"].textContent,
          renderedLen: ref.nodes["ec-batch-genes"].children.length,
          incomplete: ref.page.state.batchIncomplete,
        });
      },
    }
  );

  await ref.page.loadBatch("batch-retry");

  // During attempts 1 & 2, banner was shown, incomplete was true, partial list was never rendered, and backoff grew
  assert.strictEqual(p2Calls, 3);
  assert.strictEqual(delays.length, 2);
  assert.strictEqual(delays[0], 1000);
  assert.strictEqual(delays[1], 2000);
  for (const snap of snapshotsDuringRetry) {
    assert.strictEqual(snap.errorHidden, false);
    assert.ok(snap.errorText.includes("Couldn't load all genes; retrying"));
    assert.strictEqual(snap.renderedLen, 0);
    assert.strictEqual(snap.incomplete, true);
  }

  // After retry succeeds, error clears and full 250 genes render
  assert.strictEqual(ref.nodes["ec-error"].hidden, true);
  assert.strictEqual(ref.nodes["ec-error"].textContent, "");
  assert.strictEqual(ref.page.state.batchIncomplete, false);
  assert.strictEqual(ref.nodes["ec-batch-genes"].children.length, 250);
  assert.ok(ref.nodes["ec-batch-genes"].children[249].children[0].text.includes("GENE250"));
});

test("done batch with failed page 2 still retries and renders full list", async () => {
  const TOTAL = 250;
  const allGenes = Array.from({ length: TOTAL }, (_, i) => ({
    position: i,
    input_symbol: `GENE${i + 1}`,
    hugo_symbol: `GENE${i + 1}`,
    status: "done",
    error: null,
    notes: null,
    updated_at: "2026-11-17T12:00:00Z",
  }));
  const page1Done = {
    batch_id: "batch-done-retry",
    status: "done",
    release_id: "rel-1",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: allGenes.slice(0, 200),
  };
  const page2Done = {
    ...page1Done,
    genes: allGenes.slice(200, 250),
  };

  let p2Calls = 0;
  let sawBanner = false;
  let ref;
  ref = env(
    {
      "GET /v1/expression-context/batches/batch-done-retry": response(page1Done),
      "GET /v1/expression-context/batches/batch-done-retry?offset=200&limit=200": () => {
        p2Calls += 1;
        if (p2Calls === 1) return response({ detail: "upstream 502" }, 502);
        return response(page2Done);
      },
    },
    {
      wait: async () => {
        if (!ref.nodes["ec-error"].hidden && ref.nodes["ec-error"].textContent.includes("Couldn't load all genes; retrying")) {
          sawBanner = true;
        }
      },
    }
  );

  await ref.page.loadBatch("batch-done-retry");
  assert.strictEqual(p2Calls, 2);
  assert.strictEqual(sawBanner, true);
  assert.strictEqual(ref.nodes["ec-error"].hidden, true);
  assert.strictEqual(ref.page.state.batchIncomplete, false);
  assert.strictEqual(ref.nodes["ec-batch-genes"].children.length, 250);
});

test("malformed or empty-but-short page leaves batch incomplete without partial list", async () => {
  const TOTAL = 250;
  const allGenes = Array.from({ length: TOTAL }, (_, i) => ({
    position: i,
    input_symbol: `GENE${i + 1}`,
    hugo_symbol: `GENE${i + 1}`,
    status: "done",
    error: null,
    notes: null,
    updated_at: "2026-11-17T12:00:00Z",
  }));
  const page1 = {
    batch_id: "batch-malformed",
    status: "done",
    release_id: "rel-1",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: allGenes.slice(0, 200),
  };

  // Case A: malformed page 2 (genes is not an array)
  const malEnv = env(
    {
      "GET /v1/expression-context/batches/batch-malformed": response(page1),
      "GET /v1/expression-context/batches/batch-malformed?offset=200&limit=200": response({ batch_id: "batch-malformed", genes: "not-an-array" }),
    },
    { maxPageRetries: 2 }
  );
  await malEnv.page.loadBatch("batch-malformed");
  assert.strictEqual(malEnv.page.state.batchIncomplete, true);
  assert.strictEqual(malEnv.nodes["ec-error"].hidden, false);
  assert.ok(malEnv.nodes["ec-error"].textContent.includes("Couldn't load all genes"));
  assert.strictEqual(malEnv.nodes["ec-batch-genes"].children.length, 0);
  assert.strictEqual(malEnv.nodes["ec-batch-result"].hidden, true);

  // Case B: empty-but-short page 2 (genes: [] while total=250)
  const shortEnv = env(
    {
      "GET /v1/expression-context/batches/batch-malformed": response(page1),
      "GET /v1/expression-context/batches/batch-malformed?offset=200&limit=200": response({ ...page1, genes: [] }),
    },
    { maxPageRetries: 2 }
  );
  await shortEnv.page.loadBatch("batch-malformed");
  assert.strictEqual(shortEnv.page.state.batchIncomplete, true);
  assert.strictEqual(shortEnv.nodes["ec-error"].hidden, false);
  assert.ok(shortEnv.nodes["ec-error"].textContent.includes("Couldn't load all genes"));
  assert.strictEqual(shortEnv.nodes["ec-batch-genes"].children.length, 0);
  assert.strictEqual(shortEnv.nodes["ec-batch-result"].hidden, true);
});

test("cancel on >200-gene batch while polling -> single loop and repeated clicks don't multiply loops", async () => {
  const TOTAL = 250;
  const makeGenes = (status) => Array.from({ length: TOTAL }, (_, i) => ({
    position: i,
    input_symbol: `GENE${i + 1}`,
    hugo_symbol: `GENE${i + 1}`,
    status,
    error: null,
    notes: null,
    updated_at: "2026-11-17T12:00:00Z",
  }));
  const runningGenes = makeGenes("running");
  const cancelledGenes = makeGenes("cancelled").map((g) => ({ ...g, status: "done" }));
  const runningPage1 = {
    batch_id: "batch-cancel-250",
    status: "running",
    release_id: "rel-1",
    counts: { total: TOTAL, done: 0, queued: 0, running: TOTAL, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: runningGenes.slice(0, 200),
  };
  const runningPage2 = {
    ...runningPage1,
    genes: runningGenes.slice(200, 250),
  };
  const cancelledPage1 = {
    batch_id: "batch-cancel-250",
    status: "cancelled",
    release_id: "rel-1",
    counts: { total: TOTAL, done: TOTAL, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: cancelledGenes.slice(0, 200),
  };
  const cancelledPage2 = {
    ...cancelledPage1,
    genes: cancelledGenes.slice(200, 250),
  };

  let cancelled = false;
  let poll1TimerResolve = null;
  let resolveCancelPost = null;
  let timerCount = 0;

  const ref = env(
    {
      "GET /v1/expression-context/batches/batch-cancel-250": () => response(cancelled ? cancelledPage1 : runningPage1),
      "GET /v1/expression-context/batches/batch-cancel-250?offset=200&limit=200": () => response(cancelled ? cancelledPage2 : runningPage2),
      "POST /v1/expression-context/batches/batch-cancel-250/cancel": () => new Promise((done) => {
        resolveCancelPost = () => {
          cancelled = true;
          done(response(cancelledPage1));
        };
      }),
    },
    {
      wait: () => {
        timerCount += 1;
        return new Promise((resolve) => { poll1TimerResolve = resolve; });
      },
    }
  );

  const loadPromise = ref.page.loadBatch("batch-cancel-250");
  // Wait until poll 0 finishes fetching both pages and enters sleepBatch
  while (!poll1TimerResolve) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(timerCount, 1);

  // Rapidly fire cancel 3 times while POST /cancel is in flight
  const c1 = ref.nodes["ec-batch-cancel"].fire("click");
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);
  const c2 = ref.nodes["ec-batch-cancel"].fire("click");
  const c3 = ref.nodes["ec-batch-cancel"].fire("click");

  while (!resolveCancelPost) await new Promise((r) => setTimeout(r, 0));
  const cancelPosts = ref.calls.filter((c) => c.method === "POST");
  assert.strictEqual(cancelPosts.length, 1);

  // Finish POST /cancel and resolve old timer handle too
  resolveCancelPost();
  await Promise.all([c1, c2, c3]);
  poll1TimerResolve();
  await loadPromise;

  // Flush microtasks and confirm exactly one active loop ran (2 fetches of page 1, 2 of page 2, 1 timer)
  await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(timerCount, 1);
  assert.strictEqual(ref.calls.filter((c) => c.url.endsWith("/batch-cancel-250")).length, 2);
  assert.strictEqual(ref.calls.filter((c) => c.url.includes("offset=200")).length, 2);
  assert.strictEqual(ref.calls.filter((c) => c.method === "POST").length, 1);
  assert.strictEqual(ref.nodes["ec-batch-genes"].children.length, 250);
  assert.ok(ref.nodes["ec-batch-summary"].textContent.includes("Status: cancelled"));
  assert.strictEqual(ref.page.state.batchPolling, false);
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);
});

test("switching to another batch or tab stops old loop, cancels timers/fetches, and ignores stale results", async () => {
  const oldRunning = {
    batch_id: "batch-old",
    status: "running",
    release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [{ position: 0, input_symbol: "ERBB2", hugo_symbol: "ERBB2", status: "running", error: null, notes: null, updated_at: "2026-11-17T00:00:00Z" }],
  };
  const newDone = {
    batch_id: "batch-new",
    status: "done",
    release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [{ position: 0, input_symbol: "MYCN", hugo_symbol: "MYCN", status: "done", error: null, notes: null, updated_at: "2026-11-17T00:00:00Z" }],
  };

  let oldPollCalls = 0;
  let oldSignal = null;
  let releaseOldSecondFetch = null;
  const clearedTimers = [];
  const scheduledTimers = [];

  const nodes = Object.fromEntries(IDS.map((id) => [id, new Element("div")]));
  nodes["ec-error"].classList.toggle("hidden", true);
  nodes["ec-batch-result"].classList.toggle("hidden", true);
  nodes["ec-batch-cancel"].classList.toggle("hidden", true);
  const document = { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) };
  const calls = [];
  const win = {
    location: { pathname: "/expression-context", search: "", href: "/expression-context" },
    history: { replaceState: () => {} },
    setTimeout: (fn) => {
      const id = scheduledTimers.length + 1;
      scheduledTimers.push({ id, fn });
      return id;
    },
    clearTimeout: (id) => clearedTimers.push(id),
  };
  const fetchFn = async (url, options = {}) => {
    calls.push({ url, signal: options.signal });
    if (url.endsWith("/batch-old")) {
      oldPollCalls += 1;
      if (oldPollCalls === 1) return response(oldRunning);
      oldSignal = options.signal;
      return new Promise((resolve) => {
        releaseOldSecondFetch = () => resolve(response({ detail: "boom" }, 500));
      });
    }
    if (url.endsWith("/batch-new")) {
      return response(newDone);
    }
    throw new Error(`unexpected ${url}`);
  };
  const page = createExpressionContextPage({ document, window: win, fetch: fetchFn });

  const oldPromise = page.loadBatch("batch-old");
  while (scheduledTimers.length === 0) await new Promise((r) => setTimeout(r, 0));
  // Fire timer for poll 2 of batch-old so its fetch is now in flight
  scheduledTimers[0].fn();
  while (!releaseOldSecondFetch) await new Promise((r) => setTimeout(r, 0));
  assert.ok(oldSignal);
  assert.strictEqual(oldSignal.aborted, false);

  // Switch to batch-new while batch-old fetch is in flight
  await page.loadBatch("batch-new");
  assert.strictEqual(oldSignal.aborted, true);
  assert.strictEqual(page.state.batch.batch_id, "batch-new");

  // Resolve stale batch-old fetch with 500 -> ignored cleanly, no error banner, no state overwrite
  releaseOldSecondFetch();
  await oldPromise;
  assert.strictEqual(page.state.batch.batch_id, "batch-new");
  assert.strictEqual(nodes["ec-error"].hidden, true);
  assert.strictEqual(oldPollCalls, 2);

  // Switching to single tab also cancels pending timer of a running batch
  const timerBefore = scheduledTimers.length;
  oldPollCalls = 0; // force poll 1 to return running
  const oldAgain = page.loadBatch("batch-old");
  while (scheduledTimers.length === timerBefore) await new Promise((r) => setTimeout(r, 0));
  const pendingTimerId = scheduledTimers[scheduledTimers.length - 1].id;
  page.setMode("single");
  await oldAgain;
  assert.ok(clearedTimers.includes(pendingTimerId));
});

test("malformed gene records ({hugo_symbol} only, array item, unknown status) -> incomplete, no crash, no blank rows", async () => {
  const badRecords = [
    [{ hugo_symbol: "ERBB2" }],
    [["ERBB2"]],
    [{ position: 0, input_symbol: "ERBB2", hugo_symbol: "ERBB2", status: "bogus_status" }],
  ];

  for (const badGenes of badRecords) {
    // Case A: malformed gene on page 1
    const envP1 = env(
      {
        "GET /v1/expression-context/batches/batch-bad-gene": response({
          batch_id: "batch-bad-gene",
          status: "done",
          release_id: "rel-1",
          counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
          genes: badGenes,
        }),
      },
      { maxPageRetries: 1 }
    );
    await envP1.page.loadBatch("batch-bad-gene");
    assert.strictEqual(envP1.page.state.batchIncomplete, true);
    assert.strictEqual(envP1.nodes["ec-error"].hidden, false);
    assert.ok(envP1.nodes["ec-error"].textContent.includes("Couldn't load all genes"));
    assert.strictEqual(envP1.nodes["ec-batch-genes"].children.length, 0);
    assert.strictEqual(envP1.nodes["ec-batch-result"].hidden, true);

    // Case B: malformed gene on page 2
    const goodFirstPage = Array.from({ length: 200 }, (_, i) => ({
      position: i,
      input_symbol: `GENE${i + 1}`,
      hugo_symbol: `GENE${i + 1}`,
      status: "done",
      error: null,
      notes: null,
      updated_at: "2026-11-17T00:00:00Z",
    }));
    const envP2 = env(
      {
        "GET /v1/expression-context/batches/batch-bad-p2": response({
          batch_id: "batch-bad-p2",
          status: "done",
          release_id: "rel-1",
          counts: { total: 201, done: 201, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
          genes: goodFirstPage,
        }),
        "GET /v1/expression-context/batches/batch-bad-p2?offset=200&limit=200": response({
          batch_id: "batch-bad-p2",
          status: "done",
          release_id: "rel-1",
          counts: { total: 201, done: 201, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
          genes: badGenes,
        }),
      },
      { maxPageRetries: 1 }
    );
    await envP2.page.loadBatch("batch-bad-p2");
    assert.strictEqual(envP2.page.state.batchIncomplete, true);
    assert.strictEqual(envP2.nodes["ec-error"].hidden, false);
    assert.ok(envP2.nodes["ec-error"].textContent.includes("Couldn't load all genes"));
    assert.strictEqual(envP2.nodes["ec-batch-genes"].children.length, 0);
    assert.strictEqual(envP2.nodes["ec-batch-result"].hidden, true);
  }
});

test("stale cancel response (complete or paginated) when switching to batch B is ignored and B keeps running", async () => {
  const makeGene = (sym, status = "running") => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const batchA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const batchACancelledComplete = {
    batch_id: "batch-A", status: "cancelled", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };
  const batchBRunning = {
    batch_id: "batch-B", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("MYCN", "running")],
  };
  const batchBDone = {
    batch_id: "batch-B", status: "done", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("MYCN", "done")],
  };

  // Case 1: complete cancel response for A arrives after switching to B -> B stays shown, A never rendered
  let resolveCancelA1 = null;
  let pollA1Timer = null;
  const env1 = env(
    {
      "GET /v1/expression-context/batches/batch-A": response(batchA),
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => { resolveCancelA1 = () => r(response(batchACancelledComplete)); }),
      "GET /v1/expression-context/batches/batch-B": response(batchBDone),
    },
    { wait: () => new Promise((r) => { pollA1Timer = r; }) }
  );
  const pA1 = env1.page.loadBatch("batch-A");
  while (!pollA1Timer) await new Promise((r) => setTimeout(r, 0));
  const cancelPromise1 = env1.page.cancelCurrentBatch();
  while (!resolveCancelA1) await new Promise((r) => setTimeout(r, 0));
  await env1.page.loadBatch("batch-B");
  assert.strictEqual(env1.page.state.batch.batch_id, "batch-B");

  // Resolve stale cancel A -> must be ignored
  resolveCancelA1();
  await cancelPromise1;
  pollA1Timer();
  await pA1;
  assert.strictEqual(env1.page.state.batch.batch_id, "batch-B");
  assert.strictEqual(env1.nodes["ec-batch-title"].textContent, "Batch batch-B");

  // Case 2: paginated (>200 genes) cancel response for A arrives while B is polling -> no loadBatch(A), B's loop keeps running
  let resolveCancelA2 = null;
  let bPollCalls = 0;
  let bTimerResolve = null;
  let a2Timer = null;
  const batchAPaginated = {
    batch_id: "batch-A", status: "cancelled", release_id: "rel-1",
    counts: { total: 250, done: 250, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: Array.from({ length: 200 }, (_, i) => ({ ...makeGene(`G${i}`, "done"), position: i })),
  };
  const env2 = env(
    {
      "GET /v1/expression-context/batches/batch-A": response(batchA),
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => { resolveCancelA2 = () => r(response(batchAPaginated)); }),
      "GET /v1/expression-context/batches/batch-B": () => {
        bPollCalls += 1;
        return response(bPollCalls === 1 ? batchBRunning : batchBDone);
      },
    },
    {
      wait: () => new Promise((r) => {
        if (!a2Timer) a2Timer = r;
        else bTimerResolve = r;
      }),
    }
  );
  const pA2 = env2.page.loadBatch("batch-A");
  while (!a2Timer) await new Promise((r) => setTimeout(r, 0));
  const cancelPromise2 = env2.page.cancelCurrentBatch();
  while (!resolveCancelA2) await new Promise((r) => setTimeout(r, 0));
  const pB2 = env2.page.loadBatch("batch-B");
  while (!bTimerResolve) await new Promise((r) => setTimeout(r, 0));

  const aCallsBefore = env2.calls.filter((c) => c.method === "GET" && c.url.endsWith("/batch-A")).length;
  resolveCancelA2();
  await cancelPromise2;
  // Must NOT trigger loadBatch(A)
  assert.strictEqual(env2.calls.filter((c) => c.method === "GET" && c.url.endsWith("/batch-A")).length, aCallsBefore);
  assert.strictEqual(env2.page.state.batch.batch_id, "batch-B");

  // B's polling loop still finishes cleanly to done
  bTimerResolve();
  await pB2;
  a2Timer();
  await pA2;
  assert.strictEqual(bPollCalls, 2);
  assert.strictEqual(env2.page.state.batch.batch_id, "batch-B");
  assert.ok(env2.nodes["ec-batch-summary"].textContent.includes("Status: done"));
});

test("stale cancel to Single mode or stale cancel failure leaves new state and banner intact", async () => {
  const makeGene = (sym, status = "running") => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const batchA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const batchADone = { ...batchA, status: "cancelled", genes: [makeGene("ERBB2", "done")] };

  // Case 1: cancel A -> switch to Single before POST resolves -> Single stays, no batch render, no banner
  let resolveCancelSingle = null;
  let timerSingle = null;
  const envS = env(
    {
      "GET /v1/expression-context/batches/batch-A": response(batchA),
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => { resolveCancelSingle = () => r(response(batchADone)); }),
    },
    { wait: () => new Promise((r) => { timerSingle = r; }) }
  );
  const pA = envS.page.loadBatch("batch-A");
  while (!timerSingle) await new Promise((r) => setTimeout(r, 0));
  const cancelS = envS.page.cancelCurrentBatch();
  while (!resolveCancelSingle) await new Promise((r) => setTimeout(r, 0));

  envS.page.setMode("single");
  resolveCancelSingle();
  await cancelS;
  timerSingle();
  await pA;

  assert.strictEqual(envS.page.state.mode, "single");
  assert.strictEqual(envS.nodes["ec-batch-result"].hidden, true);
  assert.strictEqual(envS.nodes["ec-error"].hidden, true);
  // Stale settlement did nothing on the page while in Single mode
  assert.strictEqual(envS.nodes["ec-batch-cancel"].disabled, true);
  // Switching back to Batch tab syncs Cancel button from pendingCancelIds
  envS.page.setMode("batch");
  assert.strictEqual(envS.nodes["ec-batch-cancel"].disabled, false);

  // Case 2: stale cancel failure (500) on A does not overwrite B's banner or state
  let resolveCancelFail = null;
  let timerB = null;
  const envF = env(
    {
      "GET /v1/expression-context/batches/batch-A": response(batchA),
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => { resolveCancelFail = () => r(response({ detail: "err" }, 500)); }),
      "GET /v1/expression-context/batches/batch-B": response({ detail: "missing" }, 404),
    },
    { wait: () => new Promise((r) => { timerB = r; }) }
  );
  const pA2 = envF.page.loadBatch("batch-A");
  while (!timerB) await new Promise((r) => setTimeout(r, 0));
  const cancelF = envF.page.cancelCurrentBatch();
  while (!resolveCancelFail) await new Promise((r) => setTimeout(r, 0));

  await envF.page.loadBatch("batch-B");
  assert.strictEqual(envF.nodes["ec-error"].textContent, "Batch not found.");
  resolveCancelFail();
  await cancelF;
  timerB();
  await pA2;
  assert.strictEqual(envF.nodes["ec-error"].textContent, "Batch not found.");
});

test("failed cancel on still-current batch shows error banner and resumes single-flight polling until done", async () => {
  const makeGene = (sym, status) => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const batchRunning = {
    batch_id: "batch-resume", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const batchDone = {
    batch_id: "batch-resume", status: "done", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };

  let getCount = 0;
  const timers = [];
  const envR = env(
    {
      "GET /v1/expression-context/batches/batch-resume": () => {
        getCount += 1;
        return response(getCount <= 2 ? batchRunning : batchDone);
      },
      "POST /v1/expression-context/batches/batch-resume/cancel": response({ detail: "fail" }, 500),
    },
    {
      wait: () => new Promise((r) => { timers.push(r); }),
    }
  );

  const loadP = envR.page.loadBatch("batch-resume");
  while (timers.length < 1) await new Promise((r) => setTimeout(r, 0));

  const cancelP = envR.page.cancelCurrentBatch();
  // Wait for resumed polling's first poll (getCount === 2) and its wait timer
  while (timers.length < 2) await new Promise((r) => setTimeout(r, 0));

  // Error banner must stay visible while polling resumed, and cancel button is enabled again
  assert.strictEqual(envR.nodes["ec-error"].hidden, false);
  assert.ok(envR.nodes["ec-error"].textContent.includes("couldn't load expression statistics"));
  assert.strictEqual(envR.nodes["ec-batch-cancel"].disabled, false);

  // Advance resumed polling to done
  timers[1]();
  await cancelP;
  timers[0]();
  await loadP;

  assert.strictEqual(getCount, 3);
  assert.ok(envR.nodes["ec-batch-summary"].textContent.includes("Status: done"));
  assert.strictEqual(envR.nodes["ec-error"].hidden, false);
});

test("reload while cancel pending keeps Cancel disabled and blocks duplicate POST until settle", async () => {
  const makeGene = (sym, status) => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const runningA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const doneA = {
    batch_id: "batch-A", status: "done", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };
  const cancelledA = {
    batch_id: "batch-A", status: "cancelled", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };

  let resolveCancelA = null;
  const timers = [];
  let getCalls = 0;
  const ref = env(
    {
      "GET /v1/expression-context/batches/batch-A": () => {
        getCalls += 1;
        // Both initial load and reload before cancel settles return running; later poll returns done
        return response(getCalls <= 2 ? runningA : doneA);
      },
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => {
        resolveCancelA = () => r(response(cancelledA));
      }),
    },
    { wait: () => new Promise((r) => { timers.push(r); }) }
  );

  const p1 = ref.page.loadBatch("batch-A");
  while (timers.length < 1) await new Promise((r) => setTimeout(r, 0));

  // Click Cancel on A (POST now pending)
  const c1 = ref.nodes["ec-batch-cancel"].fire("click");
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);
  while (!resolveCancelA) await new Promise((r) => setTimeout(r, 0));

  // Reload batch A while POST /cancel is STILL pending
  const p2 = ref.page.loadBatch("batch-A");
  while (timers.length < 2) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);

  // Clicking Cancel again during reload must send NO second POST
  await ref.nodes["ec-batch-cancel"].fire("click");
  assert.strictEqual(ref.calls.filter((c) => c.method === "POST").length, 1);

  // Settle first POST -> Cancel reflects new state (no longer pending-disabled)
  resolveCancelA();
  await c1;
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);

  // Clean up remaining poll loops
  timers[1]();
  await p2;
  timers[0]();
  await p1;
});

test("pending cancel on A allows B's Cancel to work and A settling does not touch B", async () => {
  const makeGene = (sym, status) => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const runningA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const cancelledA = { ...runningA, status: "cancelled", genes: [makeGene("ERBB2", "done")] };
  const runningB = {
    batch_id: "batch-B", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("MYCN", "running")],
  };
  const cancelledB = { ...runningB, status: "cancelled", genes: [makeGene("MYCN", "done")] };

  let resolveCancelA = null;
  let resolveCancelB = null;
  const timers = [];
  const ref = env(
    {
      "GET /v1/expression-context/batches/batch-A": response(runningA),
      "GET /v1/expression-context/batches/batch-B": response(runningB),
      "POST /v1/expression-context/batches/batch-A/cancel": () => new Promise((r) => {
        resolveCancelA = () => r(response(cancelledA));
      }),
      "POST /v1/expression-context/batches/batch-B/cancel": () => new Promise((r) => {
        resolveCancelB = () => r(response(cancelledB));
      }),
    },
    { wait: () => new Promise((r) => { timers.push(r); }) }
  );

  const pA = ref.page.loadBatch("batch-A");
  while (timers.length < 1) await new Promise((r) => setTimeout(r, 0));

  const cA = ref.nodes["ec-batch-cancel"].fire("click");
  while (!resolveCancelA) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);

  // Switch to batch B -> B's Cancel must be enabled and usable
  const pB = ref.page.loadBatch("batch-B");
  while (timers.length < 2) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.page.state.batch.batch_id, "batch-B");
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);

  // Click Cancel on B
  const cB = ref.nodes["ec-batch-cancel"].fire("click");
  while (!resolveCancelB) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);
  assert.strictEqual(ref.calls.filter((c) => c.method === "POST").length, 2);

  // Settle A's POST -> must NOT overwrite B or re-enable B's Cancel while B's POST is pending
  resolveCancelA();
  await cA;
  assert.strictEqual(ref.page.state.batch.batch_id, "batch-B");
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);

  // Settle B's POST -> B finishes cancelled
  resolveCancelB();
  await cB;
  assert.ok(ref.nodes["ec-batch-summary"].textContent.includes("Status: cancelled"));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);

  timers[1]();
  await pB;
  timers[0]();
  await pA;
});

test("submitBatch: switching to Single before 202 arrives leaves Single active and skips loadBatch", async () => {
  let resolvePost = null;
  const ref = env({
    "POST /v1/expression-context/batches": () => new Promise((r) => {
      resolvePost = () => r(response({ batch_id: "batch-late", status: "queued" }, 202));
    }),
  });

  ref.page.setMode("batch");
  const subP = ref.page.submitBatch("ERBB2, MYCN");
  while (!resolvePost) await new Promise((r) => setTimeout(r, 0));

  // Switch to Single before POST /batches resolves
  ref.page.setMode("single");
  assert.strictEqual(ref.page.state.mode, "single");

  resolvePost();
  await subP;

  // Single stays active; no GET /batches/batch-late was issued
  assert.strictEqual(ref.page.state.mode, "single");
  assert.strictEqual(ref.nodes["ec-batch-result"].hidden, true);
  assert.strictEqual(ref.calls.filter((c) => c.method === "GET").length, 0);
});

test("failed cancel -> recovery polling -> second cancel aborts recovery -> Cancel stays disabled until second POST settles", async () => {
  const makeGene = (sym, status) => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const runningA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const doneA = {
    batch_id: "batch-A", status: "done", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };

  let postCount = 0;
  let getCount = 0;
  let resolveSecondPost = null;
  const timers = [];
  const ref = env(
    {
      "GET /v1/expression-context/batches/batch-A": () => {
        getCount += 1;
        return response(getCount <= 3 ? runningA : doneA);
      },
      "POST /v1/expression-context/batches/batch-A/cancel": () => {
        postCount += 1;
        if (postCount === 1) return response({ detail: "fail 1" }, 500);
        return new Promise((r) => {
          resolveSecondPost = () => r(response({ detail: "fail 2" }, 500));
        });
      },
    },
    { wait: () => new Promise((r) => { timers.push(r); }) }
  );

  const pLoad = ref.page.loadBatch("batch-A");
  while (timers.length < 1) await new Promise((r) => setTimeout(r, 0));

  // Step 1-2: First cancel fails (500) and starts recovery polling (timers[1])
  const c1 = ref.nodes["ec-batch-cancel"].fire("click");
  while (timers.length < 2) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(postCount, 1);

  // Step 3: Second cancel click (POST 2 held pending) aborts c1's recovery polling
  const c2 = ref.nodes["ec-batch-cancel"].fire("click");
  while (!resolveSecondPost) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(postCount, 2);

  // Step 4: First invocation completely settles (timers[1] resolves c1's recovery loadBatch)
  timers[1]();
  await c1;

  // After c1 settles, Cancel MUST still be disabled because c2's POST is pending!
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);

  // Step 5: Third click sends NO duplicate POST
  await ref.nodes["ec-batch-cancel"].fire("click");
  assert.strictEqual(ref.calls.filter((c) => c.method === "POST").length, 2);

  // Step 6: Second POST settles (with 500, A still shown) -> Cancel re-enables
  resolveSecondPost();
  while (timers.length < 3) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);

  timers[2]();
  await c2;
  timers[0]();
  await pLoad;
});

test("older cancel invocation finishing after newer one has settled does not alter pending cancel map", async () => {
  const makeGene = (sym, status) => ({
    position: 0, input_symbol: sym, hugo_symbol: sym, status, error: null, notes: null, updated_at: "2026-11-17T00:00:00Z",
  });
  const runningA = {
    batch_id: "batch-A", status: "running", release_id: "rel-1",
    counts: { total: 1, done: 0, queued: 0, running: 1, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "running")],
  };
  const doneA = {
    batch_id: "batch-A", status: "done", release_id: "rel-1",
    counts: { total: 1, done: 1, queued: 0, running: 0, failed: 0, skipped_no_stats: 0, skipped_fresh: 0, not_found: 0 },
    genes: [makeGene("ERBB2", "done")],
  };

  let getCount = 0;
  let postCount = 0;
  let resolveSlowC1RecoveryGet = null;
  let resolvePost3 = null;
  const timers = [];
  const ref = env(
    {
      "GET /v1/expression-context/batches/batch-A": () => {
        getCount += 1;
        if (getCount === 2) {
          // Held open for c1's recovery loadBatch
          return new Promise((r) => {
            resolveSlowC1RecoveryGet = () => r(response(runningA));
          });
        }
        return response(getCount <= 3 ? runningA : doneA);
      },
      "POST /v1/expression-context/batches/batch-A/cancel": () => {
        postCount += 1;
        if (postCount <= 2) return response({ detail: "fail" }, 500);
        return new Promise((r) => {
          resolvePost3 = () => r(response({ ...doneA, status: "cancelled" }));
        });
      },
    },
    { wait: () => new Promise((r) => { timers.push(r); }) }
  );

  const pLoad = ref.page.loadBatch("batch-A");
  while (timers.length < 1) await new Promise((r) => setTimeout(r, 0));

  // c1 fails POST 1 and hangs in recovery GET (getCount === 2)
  const c1 = ref.nodes["ec-batch-cancel"].fire("click");
  while (!resolveSlowC1RecoveryGet) await new Promise((r) => setTimeout(r, 0));

  // c2 runs POST 2 -> fails (500) -> enters recovery polling -> pauses at timers[1]
  const c2 = ref.nodes["ec-batch-cancel"].fire("click");
  while (timers.length < 2) await new Promise((r) => setTimeout(r, 0));

  // c3 starts POST 3 (held pending)
  const c3 = ref.nodes["ec-batch-cancel"].fire("click");
  while (!resolvePost3) await new Promise((r) => setTimeout(r, 0));
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);

  // Now c1's held network GET resolves and c1 finishes completely WHILE c3 is pending
  resolveSlowC1RecoveryGet();
  await c1;

  // c1 finishing must NOT delete c3's pending cancel token or re-enable Cancel
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, true);
  await ref.nodes["ec-batch-cancel"].fire("click");
  assert.strictEqual(postCount, 3);

  // Settle c3 -> A is cancelled
  resolvePost3();
  await c3;
  assert.strictEqual(ref.nodes["ec-batch-cancel"].disabled, false);

  timers[1]();
  await c2;
  timers[0]();
  await pLoad;
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`not ok - ${name}\n${error.stack}`);
    }
  }
  if (failed) {
    console.log(`${failed} of ${tests.length} failed`);
    process.exit(1);
  }
  console.log(`${tests.length} passed`);
})();

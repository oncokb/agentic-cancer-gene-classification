// Expression stats page (optional expression-context module). Plain Node
// fake-DOM harness, following test_runs_page.js. Run by
// tests/test_expression_context_web.py.
"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const {
  createExpressionContextPage, formatNumber, formatQ, pmidUrl, expressionURL, exportURL, goldenSetExportURL,
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
  "ec-summary", "ec-release", "ec-cbio", "ec-run", "ec-export", "ec-export-golden", "ec-view-shown",
  "ec-view-all", "ec-view-count", "ec-table", "ec-table-empty", "ec-job-status", "ec-tbody",
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

function env(routes, { search = "" } = {}) {
  const nodes = Object.fromEntries(IDS.map((id) => [id, new Element("div")]));
  nodes["ec-error"].classList.toggle("hidden", true);
  nodes["ec-result"].classList.toggle("hidden", true);
  nodes["ec-run"].classList.toggle("hidden", true);
  nodes["ec-export"].classList.toggle("hidden", true);
  nodes["ec-export-golden"].classList.toggle("hidden", true);
  nodes["ec-table-empty"].classList.toggle("hidden", true);
  nodes["ec-view-shown"].classList.toggle("active", true);
  nodes["ec-view-shown"].setAttribute("aria-pressed", "true");
  nodes["ec-view-all"].setAttribute("aria-pressed", "false");
  const document = { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) };
  const calls = [];
  const replaced = [];
  const window = {
    location: { pathname: "/expression-context", search, href: "/expression-context" },
    history: { replaceState: (_s, _t, url) => replaced.push(url) },
    setTimeout: (fn) => fn(),
  };
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET", body: options.body });
    const handler = routes[`${options.method || "GET"} ${url}`];
    if (!handler) throw new Error(`unexpected request ${options.method || "GET"} ${url}`);
    return typeof handler === "function" ? handler() : handler;
  };
  const page = createExpressionContextPage({ document, window, fetch, wait: async () => {} });
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
    assert.strictEqual(badges.text, "Point MutationCopy Number Alteration (amp, del)");
    assert.strictEqual(badges.children.length, 2);
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
    assert.strictEqual(summaryRows(nodes)[0].children[3].text, "Point Mutation");
    assert.ok(detailRows(nodes)[0].text.includes("3/10 events/profiled; frequency 30.00%"));
  }
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

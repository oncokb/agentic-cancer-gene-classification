// Curator-set cancer-associated call and fusion-partner precedent editing
// (src/static/curation.js + app.js hooks) under the fake DOM in
// curation_harness.js. Run via tests/test_frontend_curation_extended_ui.py.
"use strict";

const assert = require("assert");
const { loadCurationApp, FakeElement, byData, oneByData, waitFor, runTests } = require("./curation_harness");

const OVERRIDE_META = { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z" };

function geneAnnotation(overrides = {}) {
  return {
    gene: "ALK",
    fusions: ["EML4::ALK"],
    cancer_associated: true,
    cancer_association_rationale: "Pipeline rationale.",
    citations: ["111"],
    evidence_cards: [
      { pmid: "111", title: "Paper one", journal: "J1", abstract: "Alpha sentence.", selected_reason: "r1", quote: "Alpha sentence.", evidence_type: "clinical" },
    ],
    supporting_quotes: [],
    clinical_actionability: null,
    quality_flags: [],
    ...overrides,
  };
}

function render(app, annotationOverrides = {}) {
  const result = {
    run_id: "run-1", genes_annotated: 1, fusions_processed: 1, annotation_tumor_types: { ALK: "LUAD" },
    annotations: [geneAnnotation(annotationOverrides)], fusion_evidence: [],
  };
  app.state.currentResult = result;
  app.renderAnnotationResult(result);
  return app.root("results-window");
}

const body = (app) => app.root("curation-body");
const footerButton = (app, action) => oneByData(app.root("curation-footer"), "curation", action);

// Answers the curation GET, the re-evaluation job (one poll), and the PUT.
function routes({ draft, current = { version: 0, override: null }, onPut, extra }) {
  return async (method, url, reqBody) => {
    if (method === "GET" && /^\/v1\/curation\/[a-z_]+\/[^/?]+(\?|$)/.test(url)) return [200, current];
    if (method === "POST" && url === "/v1/curation/reevaluate/jobs") {
      return [200, { job_id: "job-1", status_url: "/v1/curation/reevaluate/jobs/job-1" }];
    }
    if (method === "GET" && url === "/v1/curation/reevaluate/jobs/job-1") {
      return [200, { status: "succeeded", error: null, invalid_pmids: null, draft }];
    }
    if (method === "PUT") {
      onPut({ url, body: reqBody });
      return [200, { version: (current.version || 0) + 1, override: { ...OVERRIDE_META, payload: reqBody.payload } }];
    }
    return extra ? extra(method, url, reqBody) : undefined;
  };
}

async function toGeneReview(app) {
  oneByData(app.root("results-window"), "curation", "edit-evidence").click();
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad")?.draft, "draft");
  footerButton(app, "review").click();
}

const GENE_DRAFT = {
  gene: "ALK",
  cancer_associated: false,
  cancer_association_rationale: "Suggested rationale.",
  evidence_cards: [{ pmid: "111", title: "Paper one", abstract: "Alpha sentence.", selected_reason: "s", quote: "Alpha sentence.", evidence_type: "clinical" }],
  supporting_quotes: [],
  clinical_actionability: null,
};

// ---------------------------------------------------------------------------

async function test_suggested_call_offered_applied_flipped_and_sent_in_put() {
  let put;
  const app = loadCurationApp({ routes: routes({ draft: GENE_DRAFT, onPut: (p) => { put = p; } }) });
  render(app);
  await toGeneReview(app);

  let select = oneByData(body(app), "curation", "cancer-associated");
  assert.strictEqual(select.value, "", "starts at the current (pipeline) call");
  assert.match(oneByData(body(app), "curation", "suggested-call").textContent, /suggested: No/);
  oneByData(body(app), "curation", "use-suggested-call").click();
  select = oneByData(body(app), "curation", "cancer-associated");
  assert.strictEqual(select.value, "false", "one click applies the suggestion");
  assert.strictEqual(byData(body(app), "curation", "use-suggested-call").length, 0);
  select.value = "true";
  select.fire("change");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.body.payload.cancer_associated, true);
  assert.strictEqual(put.body.payload.cancer_association_rationale, "Suggested rationale.");

  // The run reload 404s here, so the saved override is applied locally.
  await waitFor(() => app.state.currentResult.annotations[0].curated, "rerender");
  const annotation = app.state.currentResult.annotations[0];
  assert.strictEqual(annotation.cancer_associated, true);
  assert.strictEqual(annotation.insufficient_evidence, false);
  assert.ok(annotation.curated_fields.includes("cancer_associated"));
  const tag = oneByData(app.root("results-window"), "curation", "curator-set");
  assert.strictEqual(tag.textContent, "Curator-set");
}

async function test_untouched_call_in_evidence_edit_is_not_written() {
  let put;
  const current = { version: 2, override: { ...OVERRIDE_META, payload: { cancer_associated: false } } };
  const app = loadCurationApp({ routes: routes({ draft: { ...GENE_DRAFT, cancer_associated: true }, current, onPut: (p) => { put = p; } }) });
  render(app, { cancer_associated: false, curated: true, curation_version: 2, curated_fields: ["cancer_associated"] });
  await toGeneReview(app);
  assert.strictEqual(oneByData(body(app), "curation", "cancer-associated").value, "false", "shows the current curated call");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.body.payload.cancer_associated, false, "kept verbatim, not replaced by the suggestion");

  // With no curated call, an untouched select adds none.
  let put2;
  const app2 = loadCurationApp({ routes: routes({ draft: GENE_DRAFT, onPut: (p) => { put2 = p; } }) });
  render(app2);
  await toGeneReview(app2);
  oneByData(app2.root("curation-footer"), "curation", "save").click();
  await waitFor(() => put2, "PUT");
  assert.ok(!("cancer_associated" in put2.body.payload));
}

async function test_keep_pipeline_call_drops_previous_curated_call() {
  let put;
  const current = { version: 3, override: { ...OVERRIDE_META, payload: { cancer_associated: false, citations: ["111"] } } };
  const app = loadCurationApp({
    routes: routes({ draft: { ...GENE_DRAFT, cancer_associated: null }, current, onPut: (p) => { put = p; } }),
  });
  render(app, { cancer_associated: false, curated: true, curation_version: 3, curated_fields: ["cancer_associated", "citations"] });
  await toGeneReview(app);

  const select = oneByData(body(app), "curation", "cancer-associated");
  assert.strictEqual(select.value, "false", "no model suggestion: prefilled with the current curated call");
  assert.strictEqual(byData(body(app), "curation", "suggested-call").length, 0);
  select.value = "";
  select.fire("change");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.body.expected_version, 3);
  assert.ok(!("cancer_associated" in put.body.payload), "keeping the pipeline call removes the curated one");
}

async function test_precedent_only_edit_keeps_curated_call() {
  let put;
  const current = { version: 1, override: { ...OVERRIDE_META, payload: { cancer_associated: false } } };
  const app = loadCurationApp({
    routes: routes({ draft: GENE_DRAFT, current, onPut: (p) => { put = p; } }),
  });
  render(app, { cancer_associated: false, curated: true, curation_version: 1, curated_fields: ["cancer_associated"] });
  app.openCurationDialog(app.geneCurationSubject(app.state.currentResult.annotations[0]), { focus: "precedent" });
  footerButton(app, "submit").click(); // empty precedent list: straight to review
  assert.strictEqual(byData(body(app), "curation", "cancer-associated").length, 0, "the call belongs to the evidence edit");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.body.payload.cancer_associated, false, "carried over verbatim");
}

async function test_curator_set_tag_replaces_flag_badge() {
  const app = loadCurationApp({ auth: { can_curate: false } });
  const flag = {
    code: "curator_set_cancer_association", label: "Curator-set cancer association", severity: "info",
    detail: "A curator set the cancer-associated call to No; the pipeline's call was Yes.",
  };
  const win = render(app, { cancer_associated: false, curated: true, curated_by: "cur@x", curated_fields: ["cancer_associated"], quality_flags: [flag] });
  const tag = oneByData(win, "curation", "curator-set");
  assert.strictEqual(tag.title, flag.detail);
  assert.ok(tag.closest(".status-line"), "tag sits on the status line");
  assert.ok(!win.allText.includes("Curator-set cancer association"), "no separate flag badge");
  assert.ok(win.allText.includes("Cancer associated: No"));

  const plain = render(app, { cancer_associated: true });
  assert.strictEqual(byData(plain, "curation", "curator-set").length, 0, "pipeline calls carry no tag");
}

// ---------------------------------------------------------------------------
// Call-only edits
// ---------------------------------------------------------------------------

const CURATED_SECTIONS = {
  cancer_association_rationale: "Curated rationale.",
  citations: ["111", "222"],
  evidence_cards: [{ pmid: "111", title: "Paper one", selected_reason: "Curated reason", quote: "Alpha sentence.", evidence_type: "clinical" }],
  supporting_quotes: [{ pmid: "111", quote: "Alpha sentence." }],
  therapeutic_precedent: { summary: "Curated precedent.", precedents: [{ pmid: "789", quote: "Q.", therapies: ["crizotinib"], evidence_type: "clinical", title: "P" }] },
};

function callRoutes({ current, onWrite, annotate }) {
  return async (method, url, reqBody) => {
    if (method === "GET" && url.startsWith("/v1/curation/gene/ALK")) return [200, current()];
    if (method === "PUT" || method === "DELETE") {
      const result = onWrite(method, url, reqBody);
      if (result) return result;
      return method === "PUT"
        ? [200, { version: current().version + 1, override: { ...OVERRIDE_META, payload: reqBody.payload } }]
        : [200, { reverted_version: current().version }];
    }
    if (method === "GET" && url.startsWith("/v1/annotate/") && annotate) return [200, annotate()];
    return undefined;
  };
}

async function test_call_only_edit_keeps_other_sections_byte_identical() {
  const writes = [];
  const current = { version: 4, override: { ...OVERRIDE_META, payload: JSON.parse(JSON.stringify(CURATED_SECTIONS)) } };
  const app = loadCurationApp({
    routes: callRoutes({ current: () => current, onWrite: (method, url, reqBody) => { writes.push({ method, url, body: reqBody }); } }),
  });
  const win = render(app, {
    curated: true, curation_version: 4, cancer_association_rationale: "Curated rationale.",
    curated_fields: Object.keys(CURATED_SECTIONS),
  });
  const editCall = oneByData(win, "curation", "edit-call");
  assert.ok(editCall.closest(".status-line"), "the call has its own control on the status line");
  editCall.click();
  const aux = app.root("curation-aux-body");
  assert.match(oneByData(aux, "curation", "call-current").textContent, /Yes \(pipeline\)/);
  const select = oneByData(aux, "curation", "cancer-associated");
  select.value = "false";
  select.fire("change");
  const note = oneByData(aux, "curation", "call-note");
  note.value = "Germline only";
  note.fire("input");
  oneByData(app.root("curation-aux-footer"), "curation", "save-call").click();
  await waitFor(() => writes.length, "PUT");

  assert.strictEqual(writes.length, 1);
  const { method, url, body: sent } = writes[0];
  assert.strictEqual(method, "PUT");
  assert.strictEqual(url, "/v1/curation/gene/ALK?tumor_type=LUAD");
  assert.strictEqual(sent.expected_version, 4);
  assert.strictEqual(sent.note, "Germline only");
  assert.deepStrictEqual(Object.keys(sent.payload).sort(), [...Object.keys(CURATED_SECTIONS), "cancer_associated"].sort());
  assert.strictEqual(sent.payload.cancer_associated, false);
  Object.keys(CURATED_SECTIONS).forEach((key) => {
    assert.strictEqual(JSON.stringify(sent.payload[key]), JSON.stringify(current.override.payload[key]), `${key} verbatim`);
  });
  assert.ok(!app.requests.some((r) => r.url.startsWith("/v1/curation/reevaluate")), "no re-evaluation job");
  assert.strictEqual(app.curationState.sessions.size, 0, "no edit session either");

  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "editor closed");
  const annotation = app.state.currentResult.annotations[0];
  assert.strictEqual(annotation.cancer_associated, false);
  assert.strictEqual(annotation.cancer_association_rationale, "Curated rationale.");
  assert.ok(byData(app.root("results-window"), "curation", "curator-set").length);
}

async function test_call_only_keep_pipeline_reverts_call_only_override() {
  const writes = [];
  const current = { version: 2, override: { ...OVERRIDE_META, payload: { cancer_associated: false } } };
  const app = loadCurationApp({
    routes: callRoutes({
      current: () => current,
      onWrite: (method, url) => { writes.push({ method, url }); },
      annotate: () => ({ run_id: "run-1", annotation_tumor_types: { ALK: "LUAD" }, annotations: [geneAnnotation()], fusion_evidence: [] }),
    }),
  });
  app.state.currentResult = null;
  render(app, { cancer_associated: false, curated: true, curation_version: 2, curated_fields: ["cancer_associated"] });
  oneByData(app.root("results-window"), "curation", "edit-call").click();
  const select = oneByData(app.root("curation-aux-body"), "curation", "cancer-associated");
  assert.strictEqual(select.value, "false");
  select.value = "";
  select.fire("change");
  oneByData(app.root("curation-aux-footer"), "curation", "save-call").click();
  await waitFor(() => writes.length, "DELETE");
  assert.deepStrictEqual(writes, [{ method: "DELETE", url: "/v1/curation/gene/ALK?tumor_type=LUAD&expected_version=2" }]);
  await waitFor(() => app.state.currentResult.annotations[0].cancer_associated === true, "pipeline call back");
  assert.strictEqual(byData(app.root("results-window"), "curation", "curator-set").length, 0);
}

async function test_call_only_conflict_and_no_op() {
  const writes = [];
  let current = { version: 3, override: { ...OVERRIDE_META, payload: { citations: ["111"], cancer_associated: true } } };
  const app = loadCurationApp({
    routes: callRoutes({ current: () => current, onWrite: (method, url, reqBody) => { writes.push(reqBody); } }),
  });
  render(app, { curated: true, curation_version: 2, curated_fields: ["citations"] });
  oneByData(app.root("results-window"), "curation", "edit-call").click();
  const select = oneByData(app.root("curation-aux-body"), "curation", "cancer-associated");
  select.value = "false";
  select.fire("change");
  const save = oneByData(app.root("curation-aux-footer"), "curation", "save-call");
  save.click();
  await waitFor(() => oneByData(app.root("curation-aux-body"), "curation", "call-error").textContent, "conflict");
  assert.match(oneByData(app.root("curation-aux-body"), "curation", "call-error").textContent, /now v3, call: Yes/);
  assert.strictEqual(writes.length, 0, "a stale version is never written");
  save.click();
  await waitFor(() => writes.length, "PUT after review");
  assert.deepStrictEqual(writes[0], { expected_version: 3, payload: { citations: ["111"], cancer_associated: false } });

  // Saving the call it already has writes nothing.
  current = { version: 4, override: { ...OVERRIDE_META, payload: { cancer_associated: false } } };
  render(app, { cancer_associated: false, curated: true, curation_version: 4, curated_fields: ["cancer_associated"] });
  oneByData(app.root("results-window"), "curation", "edit-call").click();
  oneByData(app.root("curation-aux-footer"), "curation", "save-call").click();
  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "closed");
  assert.strictEqual(writes.length, 1);
}

async function test_call_control_on_no_result_cards_and_hidden_from_viewers() {
  const app = loadCurationApp();
  app.state.resultsViewMode = "noresult";
  const win = render(app, { insufficient_evidence: true, cancer_associated: null });
  assert.strictEqual(byData(win, "curation", "edit-call").length, 1, "curators can decide undecided genes");
  const viewer = loadCurationApp({ auth: { can_curate: false } });
  assert.strictEqual(byData(render(viewer), "curation", "edit-call").length, 0);
}

// ---------------------------------------------------------------------------
// Fusion-partner precedent
// ---------------------------------------------------------------------------

const PARTNER = {
  gene: "ALK", tumor_type: "NSCLC", scope: "tumor_type_scoped", has_precedent: true, retrieved_count: 1,
  pmids: ["111"], interpretation: "Pipeline partner interpretation.",
  evidence_cards: [{ pmid: "111", title: "Partner paper", abstract: "Partner sentence.", selected_reason: "p", quote: "Partner sentence.", evidence_type: "other" }],
  curated: false, curated_fields: [], quality_flags: [],
};

const PARTNER_DRAFT = {
  gene: "ALK", tumor_type: "NSCLC", scope: "tumor_type_scoped", has_precedent: true, pmids: ["111", "555"],
  interpretation: "Suggested partner interpretation.",
  evidence_cards: [
    { pmid: "111", title: "Partner paper", abstract: "Partner sentence.", selected_reason: "s1", quote: "Partner sentence.", evidence_type: "review" },
    { pmid: "555", title: "New partner paper", abstract: "New verbatim sentence.", selected_reason: "s5", quote: "New verbatim sentence.", evidence_type: "case_report" },
  ],
};

function renderPartner(app, data, request) {
  const outer = new FakeElement("div");
  app.root("results-window").replaceChildren(outer);
  app.renderFusionPartnerResultBody(outer, data, request);
  const followUp = new FakeElement("div");
  followUp.className = "fusion-partner-followup";
  outer.appendChild(followUp);
  return outer;
}

async function test_partner_precedent_edit_flow() {
  let put;
  const served = {
    ...PARTNER, curated: true, curated_by: "cur@x", curated_at: "2026-10-12T00:00:00Z", curation_version: 1,
    interpretation: "Curated partner interpretation.", pmids: ["111", "555"], retrieved_count: 2,
    curated_fields: ["interpretation", "has_precedent", "pmids", "evidence_cards"],
  };
  const lookups = [];
  const app = loadCurationApp({
    routes: routes({
      draft: PARTNER_DRAFT,
      onPut: (p) => { put = p; },
      extra: async (method, url, reqBody) => {
        if (method === "POST" && url === "/v1/fusion-partner-evidence") {
          lookups.push(reqBody);
          return [200, served];
        }
        return undefined;
      },
    }),
  });
  const outer = renderPartner(app, PARTNER, { tumorType: "NSCLC", agnostic: false });
  const edit = oneByData(outer, "curation", "edit-evidence");
  assert.strictEqual(edit.textContent, "Edit partner precedent");
  edit.click();

  assert.strictEqual(byData(body(app), "list", "precedent").length, 0, "no therapeutic precedent list");
  assert.ok(body(app).allText.includes("Partner-precedent PMIDs"));
  const input = oneByData(body(app), "curation", "evidence-input");
  input.value = "555";
  oneByData(body(app), "curation", "evidence-add").click();
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("fusion_partner|ALK|nsclc")?.draft, "draft");
  assert.deepStrictEqual(app.requests.find((r) => r.method === "POST" && r.url === "/v1/curation/reevaluate/jobs").body, {
    subject_type: "fusion_partner", subject_key: "ALK", tumor_type: "NSCLC", evidence_pmids: ["111", "555"], precedent_pmids: [],
  });
  footerButton(app, "review").click();
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Suggested partner interpretation.");
  assert.strictEqual(oneByData(body(app), "curation", "well-supported").checked, true);
  assert.ok(body(app).allText.includes("Has fusion-partner precedent"));
  assert.strictEqual(byData(body(app), "curation", "cancer-associated").length, 0);
  const quote = byData(body(app), "curation", "card-quote")[1];
  quote.value = "New verbatim sentence.";
  quote.fire("input");

  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.url, "/v1/curation/fusion_partner/ALK?tumor_type=NSCLC");
  assert.deepStrictEqual(put.body, {
    expected_version: 0,
    payload: {
      interpretation: "Suggested partner interpretation.",
      has_precedent: true,
      pmids: ["111", "555"],
      evidence_cards: [
        { pmid: "111", evidence_type: "review", selected_reason: "s1", quote: "Partner sentence." },
        { pmid: "555", evidence_type: "case_report", selected_reason: "s5", quote: "New verbatim sentence." },
      ],
    },
  });

  // The lookup is re-fetched with its own request and re-rendered in place.
  await waitFor(() => outer.allText.includes("Curated by cur@x"), "partner re-render");
  assert.deepStrictEqual(lookups, [{ gene: "ALK", tumor_type: "NSCLC" }]);
  assert.ok(outer.allText.includes("2 papers found"));
  assert.strictEqual(outer.querySelectorAll(".fusion-partner-followup").length, 1, "follow-up control kept");
  assert.strictEqual(app.state.currentResult, null, "the run result is untouched");
}

async function test_partner_subject_keyed_by_searched_tumor_type() {
  const app = loadCurationApp();
  const scoped = app.partnerCurationSubject(PARTNER);
  assert.strictEqual(app.curationSubjectId(scoped), "fusion_partner|ALK|nsclc");
  const agnostic = app.partnerCurationSubject({ ...PARTNER, scope: "all_tumor_types" });
  assert.strictEqual(app.curationSubjectId(agnostic), "fusion_partner|ALK|", "all-tumor-types searches use the untyped override");
  assert.strictEqual(app.curationUrl(agnostic), "/v1/curation/fusion_partner/ALK");
}

async function test_partner_flags_and_viewer_controls() {
  const viewer = loadCurationApp({ auth: { can_curate: false } });
  const data = {
    ...PARTNER, curated: true, curated_by: "cur@x", curated_at: "2026-10-12", curation_version: 2,
    quality_flags: [
      { code: "newer_pipeline_evidence", label: "Newer pipeline evidence since curation", severity: "warning", detail: "PMID(s): 999." },
      { code: "curated_retracted_pmids_removed", label: "Retracted PMIDs removed", severity: "critical", detail: "Removed 666." },
    ],
  };
  const outer = renderPartner(viewer, data, { tumorType: "NSCLC" });
  assert.strictEqual(byData(outer, "curation", "edit-evidence").length, 0, "viewers can't edit");
  assert.ok(outer.allText.includes("Curated by cur@x · 2026-10-12 (v2)"));
  assert.strictEqual(byData(outer, "flag", "newer_pipeline_evidence").length, 1);
  assert.strictEqual(byData(outer, "flag", "curated_retracted_pmids_removed").length, 1);

  const unavailable = renderPartner(viewer, { ...PARTNER, quality_flags: [{ code: "curation_unavailable", label: "Curator edits unavailable", severity: "critical" }] }, {});
  assert.strictEqual(byData(unavailable, "flag", "curation_unavailable").length, 1);
}

runTests("curation extended UI", [
  test_suggested_call_offered_applied_flipped_and_sent_in_put,
  test_untouched_call_in_evidence_edit_is_not_written,
  test_keep_pipeline_call_drops_previous_curated_call,
  test_call_only_edit_keeps_other_sections_byte_identical,
  test_call_only_keep_pipeline_reverts_call_only_override,
  test_call_only_conflict_and_no_op,
  test_call_control_on_no_result_cards_and_hidden_from_viewers,
  test_precedent_only_edit_keeps_curated_call,
  test_curator_set_tag_replaces_flag_badge,
  test_partner_precedent_edit_flow,
  test_partner_subject_keyed_by_searched_tumor_type,
  test_partner_flags_and_viewer_controls,
]);

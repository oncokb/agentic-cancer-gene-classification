// Curator editing UI (src/static/curation.js + app.js renderers) under the
// fake DOM in curation_harness.js. Run via tests/test_frontend_curation_ui.py.
"use strict";

const assert = require("assert");
const { loadCurationApp, byData, oneByData, flush, waitFor, runTests } = require("./curation_harness");

const SUBMITTED = "Your changes have been submitted — re-evaluating rationale (this can take up to a minute)";

function geneAnnotation(overrides = {}) {
  return {
    gene: "ALK",
    fusions: ["EML4::ALK"],
    cancer_associated: true,
    cancer_association_rationale: "Pipeline rationale.",
    citations: ["111", "222"],
    evidence_cards: [
      { pmid: "111", title: "Paper one", journal: "J1", abstract: "Alpha sentence. More text.", selected_reason: "r1", quote: "Alpha sentence.", evidence_type: "clinical" },
      { pmid: "222", title: "Paper two", journal: "J2", abstract: "Beta sentence.", selected_reason: "r2", quote: "Beta sentence.", evidence_type: "review" },
    ],
    supporting_quotes: [{ pmid: "111", quote: "Alpha sentence." }],
    clinical_actionability: null,
    quality_flags: [],
    retrieved_pmids: [],
    retrieval_count: 0,
    ...overrides,
  };
}

function geneResult(annotationOverrides = {}, resultOverrides = {}) {
  return {
    run_id: "run-1",
    genes_annotated: 1,
    fusions_processed: 1,
    annotation_tumor_types: { ALK: "LUAD" },
    annotations: [geneAnnotation(annotationOverrides)],
    fusion_evidence: [],
    ...resultOverrides,
  };
}

const GENE_DRAFT = {
  gene: "ALK",
  cancer_association_rationale: "Suggested rationale.",
  evidence_cards: [
    { pmid: "111", title: "Paper one", journal: "J1", abstract: "Alpha sentence. More text.", selected_reason: "Suggested one.", quote: "Alpha sentence.", evidence_type: "clinical" },
    { pmid: "333", title: "Paper three", journal: "J3", abstract: "Gamma sentence here.", selected_reason: "Suggested three.", quote: "Gamma sentence here.", evidence_type: "bogus" },
  ],
  supporting_quotes: [{ pmid: "111", quote: "Alpha sentence." }, { pmid: "333", quote: "Gamma sentence here." }],
  clinical_actionability: {
    summary: "Suggested precedent.",
    pmids: ["789"],
    evidence: [{ pmid: "789", title: "Precedent paper", evidence_type: "clinical", therapies: ["crizotinib"], quote: "Verbatim precedent quote.", abstract: "Verbatim precedent quote." }],
  },
};

function render(app, result) {
  app.state.currentResult = result;
  app.renderAnnotationResult(result);
  return app.root("results-window");
}

function body(app) {
  return app.root("curation-body");
}

function footerButton(app, action) {
  return oneByData(app.root("curation-footer"), "curation", action);
}

function addPmid(app, listName, value) {
  const input = oneByData(body(app), "curation", `${listName}-input`);
  input.value = value;
  oneByData(body(app), "curation", `${listName}-add`).click();
}

function errorText(app, listName) {
  return oneByData(body(app), "curation", `${listName}-error`).textContent;
}

function geneSubject(app) {
  return app.geneCurationSubject(app.state.currentResult.annotations[0]);
}

// `current` is the override GET /v1/curation/{type}/{key} serves (read before
// every save): an object, or a function returning one.
function jobRoutes({ draft = GENE_DRAFT, final, extra, current = { version: 0, override: null } } = {}) {
  let polls = 0;
  return async (method, url, reqBody) => {
    if (method === "GET" && /^\/v1\/curation\/(gene|fusion)\/[^/?]+(\?|$)/.test(url)) {
      return [200, typeof current === "function" ? current() : current];
    }
    if (method === "POST" && url === "/v1/curation/reevaluate/jobs") {
      return [200, { job_id: "job-1", status_url: "/v1/curation/reevaluate/jobs/job-1" }];
    }
    if (method === "GET" && url === "/v1/curation/reevaluate/jobs/job-1") {
      polls += 1;
      if (polls < 2) return [200, { status: "running", error: null, invalid_pmids: null, draft: null }];
      return [200, final || { status: "succeeded", error: null, invalid_pmids: null, draft }];
    }
    return extra ? extra(method, url, reqBody) : undefined;
  };
}

async function toDraft(app, { focus = "evidence" } = {}) {
  app.openCurationDialog(geneSubject(app), { focus });
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad")?.draft, "draft");
  footerButton(app, "review").click();
}

// ---------------------------------------------------------------------------

async function test_viewer_sees_no_edit_controls() {
  const app = loadCurationApp({ auth: { can_curate: false } });
  const win = render(app, geneResult({ clinical_actionability: { confidence_score: 0.9, summary: "s", confidence_explanation: "e", pmids: ["5"], evidence: [] } }));
  assert.strictEqual(byData(win, "curation", "edit-evidence").length, 0);
  assert.strictEqual(byData(win, "curation", "edit-precedent").length, 0);
  const empty = geneAnnotation({ citations: [], evidence_cards: [], supporting_quotes: [] });
  assert.strictEqual(app.renderSupportingEvidence(empty), null);
  assert.strictEqual(app.renderClinicalActionability(empty), null);
  app.openCurationDialog(geneSubject(app));
  assert.ok(app.root("curation-modal").classList.contains("hidden"), "viewers can't open the editor");
}

async function test_curator_sees_controls_even_when_sections_empty() {
  const app = loadCurationApp();
  const win = render(app, geneResult({ citations: [], evidence_cards: [], supporting_quotes: [], clinical_actionability: null }));
  const edit = oneByData(win, "curation", "edit-evidence");
  assert.strictEqual(edit.textContent, "Edit evidence");
  assert.strictEqual(edit.getAttribute("aria-label"), "Edit evidence for ALK");
  const precedent = oneByData(win, "curation", "edit-precedent");
  assert.ok(precedent.closest(".clinical-actionability"), "Edit precedent sits in the precedent section");
  assert.ok(win.allText.includes("No high-confidence therapeutic precedent."));
}

async function test_pmid_add_remove_reorder_validation_and_cap() {
  const app = loadCurationApp();
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app));
  assert.ok(!app.root("curation-modal").classList.contains("hidden"));
  assert.strictEqual(app.document.activeElement.dataset.curation, "evidence-input", "focus starts on the PMID input");
  const session = app.curationState.sessions.get("gene|ALK|luad");
  assert.deepStrictEqual([...session.lists.evidence], ["111", "222"]);
  const row = oneByData(body(app), "pmid", "111");
  assert.ok(row.allText.includes("Paper one"), "row shows the title");
  assert.ok(row.allText.includes("Alpha sentence."), "PMID pill carries the abstract tooltip");

  addPmid(app, "evidence", "abc");
  assert.match(errorText(app, "evidence"), /not a valid PMID/);
  addPmid(app, "evidence", "222");
  assert.match(errorText(app, "evidence"), /already in this list/);
  addPmid(app, "evidence", "333, 444");
  assert.strictEqual(errorText(app, "evidence"), "");
  assert.deepStrictEqual([...session.lists.evidence], ["111", "222", "333", "444"]);

  oneByData(oneByData(body(app), "pmid", "111"), "curation", "remove-pmid").click();
  oneByData(oneByData(body(app), "pmid", "444"), "curation", "move-up").click();
  assert.deepStrictEqual([...session.lists.evidence], ["222", "444", "333"]);

  session.lists.evidence = Array.from({ length: 50 }, (_v, i) => String(1000 + i));
  app.renderCurationDialog();
  addPmid(app, "evidence", "9");
  assert.match(errorText(app, "evidence"), /At most 50 PMIDs per list/);
  assert.strictEqual(session.lists.evidence.length, 50);

  session.lists.evidence = Array.from({ length: 30 }, (_v, i) => String(1000 + i));
  session.lists.precedent = Array.from({ length: 20 }, (_v, i) => String(2000 + i));
  app.renderCurationDialog();
  addPmid(app, "precedent", "1000"); // shared with evidence: counts once
  assert.strictEqual(errorText(app, "precedent"), "");
  addPmid(app, "precedent", "77");
  assert.match(errorText(app, "precedent"), /unique PMIDs across evidence and precedent/);
}

async function test_submit_job_notifies_polls_and_prefills_draft() {
  const app = loadCurationApp({ routes: jobRoutes() });
  render(app, geneResult({ clinical_actionability: { confidence_score: 0.9, summary: "auto", confidence_explanation: "e", pmids: ["789"], evidence: [] } }));
  const opener = app.root("body").appendChild(app.document.createElement("button"));
  opener.focus();
  app.openCurationDialog(geneSubject(app), { focus: "precedent" });
  addPmid(app, "evidence", "333");
  oneByData(oneByData(body(app), "pmid", "222"), "curation", "remove-pmid").click();
  footerButton(app, "submit").click();
  await waitFor(() => app.requests.some((r) => r.method === "POST"), "POST");
  const post = app.requests.find((r) => r.method === "POST");
  assert.deepStrictEqual(post.body, {
    subject_type: "gene", subject_key: "ALK", tumor_type: "LUAD",
    evidence_pmids: ["111", "333"], precedent_pmids: ["789"],
  });
  await waitFor(() => app.root("toast-region").children.length, "toast");
  assert.ok(app.root("toast-region").allText.includes(SUBMITTED), "toast shows the submitted message");
  assert.ok(app.root("curation-status").allText.includes(SUBMITTED), "dialog status shows the submitted message");
  assert.ok(!app.root("curation-status").classList.contains("hidden"));
  assert.strictEqual(footerButton(app, "submit").disabled, true, "can't resubmit while running");
  assert.ok(byData(body(app), "curation", "evidence-input")[0].disabled, "PMID editing locked while running");
  assert.strictEqual(app.root("curation-notifications").children.length, 1, "persistent notification");

  app.pressKey("Escape"); // curator keeps working while the job runs
  assert.ok(app.root("curation-modal").classList.contains("hidden"));
  assert.strictEqual(app.document.activeElement, opener, "focus returns to the opener");
  render(app, app.state.currentResult); // re-renders must not drop the session

  const notifications = app.root("curation-notifications");
  await waitFor(() => byData(notifications, "curation", "notification-review").length, "Review draft offer");
  oneByData(notifications, "curation", "notification-review").click();
  assert.ok(!app.root("curation-modal").classList.contains("hidden"));
  const review = body(app);
  assert.strictEqual(oneByData(review, "curation", "rationale").value, "Suggested rationale.");
  const summaries = byData(review, "curation", "card-summary").map((t) => t.value);
  assert.deepStrictEqual(summaries, ["Suggested one.", "Suggested three."]);
  const types = byData(review, "curation", "evidence-type").map((s) => s.value);
  assert.deepStrictEqual(types, ["clinical", "other", "clinical"], "invalid evidence types fall back to other");
  assert.ok(review.querySelectorAll("label").some((l) => l.textContent === "Summary"));
  assert.strictEqual(byData(review, "curation", "quote-row").length, 2);
  assert.strictEqual(oneByData(review, "curation", "precedent-summary").value, "Suggested precedent.");
  assert.strictEqual(oneByData(review, "curation", "precedent-therapies").value, "crizotinib");
}

async function test_invalid_pmids_render_inline() {
  const final = {
    status: "failed", error: "Curator PMIDs failed validation", draft: null,
    invalid_pmids: { unknown: ["333"], retracted: ["222"], no_abstract: ["222"] },
  };
  const app = loadCurationApp({ routes: jobRoutes({ final }) });
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app));
  addPmid(app, "evidence", "333");
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad").job?.status === "failed", "failure");
  const reasons = (pmid) => byData(oneByData(body(app), "pmid", pmid), "curation", "invalid-reason").map((n) => n.textContent);
  assert.deepStrictEqual(reasons("333"), ["Not found in PubMed"]);
  assert.deepStrictEqual(reasons("222"), ["Retracted; No abstract available"]);
  assert.deepStrictEqual(reasons("111"), []);
  assert.ok(app.root("curation-status").allText.includes("Curator PMIDs failed validation"));
  assert.ok(app.root("curation-notifications").allText.includes("Re-evaluation failed for ALK"));
  // Fixing the list clears the reason and allows resubmitting.
  oneByData(oneByData(body(app), "pmid", "333"), "curation", "remove-pmid").click();
  assert.strictEqual(footerButton(app, "submit").disabled, false);
}

async function test_job_post_errors_are_explained() {
  for (const [status, detail, expected] of [
    [429, { detail: "Too many in-flight re-evaluation jobs" }, /maximum number of re-evaluations running/],
    [422, { detail: [{ msg: "List should have at most 50 items" }] }, /rejected: List should have at most 50 items/],
  ]) {
    const app = loadCurationApp({ routes: async (method) => (method === "POST" ? [status, detail] : undefined) });
    render(app, geneResult());
    app.openCurationDialog(geneSubject(app));
    footerButton(app, "submit").click();
    await waitFor(() => byData(app.root("curation-status"), "curation", "message").length, `${status} message`);
    assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, expected);
    assert.strictEqual(app.curationState.sessions.get("gene|ALK|luad").job, null);
  }
  // 403 means curator access is gone: see test_submit_and_revert_403_flip_to_read_only.
}

async function test_save_put_body_rerenders_in_place_and_toasts() {
  let put;
  const app = loadCurationApp({
    routes: jobRoutes({
      current: { version: 3, override: { curator_email: "old@mskcc.org", updated_at: "2026-09-01T00:00:00Z", payload: { citations: ["111", "222"] } } },
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        put = { url, body: reqBody };
        return [200, {
          subject_type: "gene", subject_key: "ALK", tumor_type: "luad", version: 4,
          override: {
            subject_type: "gene", subject_key: "ALK", tumor_type: "luad", curator_email: "cur@mskcc.org",
            created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-12T10:00:00Z", version: 4,
            payload: { ...reqBody.payload, evidence_cards: reqBody.payload.evidence_cards.map((c) => ({ ...c, title: `T${c.pmid}`, abstract: "A" })) },
          },
        }];
      },
    }),
  });
  const result = geneResult({ curated: true, curated_by: "old@mskcc.org", curated_at: "2026-09-01T00:00:00Z", curation_version: 3, curated_fields: ["citations"] });
  result.annotations.push({ ...geneAnnotation(), gene: "BRAF", cancer_association_rationale: "BRAF untouched." });
  render(app, result);
  app.openCurationDialog(geneSubject(app));
  addPmid(app, "evidence", "333");
  oneByData(oneByData(body(app), "pmid", "222"), "curation", "remove-pmid").click();
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad").draft, "draft");
  footerButton(app, "review").click();

  const review = body(app);
  const rationale = oneByData(review, "curation", "rationale");
  rationale.value = "Curator rationale.";
  rationale.fire("input");
  const quote = byData(review, "curation", "card-quote")[1];
  const warning = quote.parentNode.querySelector("[data-curation='quote-warning']");
  assert.ok(warning.classList.contains("hidden"), "verbatim quote: no warning");
  quote.value = "Paraphrased, not in the abstract.";
  quote.fire("input");
  assert.ok(!warning.classList.contains("hidden"), "non-verbatim quote warns");
  assert.match(warning.textContent, /isn't a verbatim excerpt/);
  const type = byData(review, "curation", "evidence-type")[1];
  type.value = "preclinical";
  type.fire("change");
  oneByData(review, "curation", "add-quote").click();
  byData(body(app), "curation", "remove-quote")[0].click();
  const note = oneByData(body(app), "curation", "note");
  note.value = "Swapped 222 for 333";
  note.fire("input");

  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.url, "/v1/curation/gene/ALK?tumor_type=LUAD");
  assert.deepStrictEqual(put.body, {
    expected_version: 3,
    note: "Swapped 222 for 333",
    payload: {
      cancer_association_rationale: "Curator rationale.",
      citations: ["111", "333"],
      evidence_cards: [
        { pmid: "111", evidence_type: "clinical", selected_reason: "Suggested one.", quote: "Alpha sentence." },
        { pmid: "333", evidence_type: "preclinical", selected_reason: "Suggested three.", quote: "Paraphrased, not in the abstract." },
      ],
      supporting_quotes: [{ pmid: "333", quote: "Gamma sentence here." }],
    },
  });
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "dialog closes");
  const [alk, braf] = app.state.currentResult.annotations;
  assert.strictEqual(alk.cancer_association_rationale, "Curator rationale.");
  assert.deepStrictEqual(alk.citations, ["111", "333"]);
  assert.strictEqual(alk.curation_version, 4);
  assert.strictEqual(alk.curated_by, "cur@mskcc.org");
  assert.strictEqual(braf.cancer_association_rationale, "BRAF untouched.", "other results kept");
  assert.ok(app.root("toast-region").allText.includes("Curator edits saved"));
  assert.ok(app.root("results-window").allText.includes("Curated by cur@mskcc.org · 2026-10-12 (v4)"));
  assert.strictEqual(app.curationState.sessions.size, 0, "session cleared after save");

  // A stale enrichment poll (computed before the save) can't undo it.
  const merged = app.mergeAnnotations(app.state.currentResult.annotations, [
    geneAnnotation({ curated: true, curation_version: 3, cancer_association_rationale: "Old curated.", gene_summary: "Enriched summary." }),
  ]);
  const enriched = merged.find((a) => a.gene === "ALK");
  assert.strictEqual(enriched.cancer_association_rationale, "Curator rationale.");
  assert.strictEqual(enriched.curation_version, 4);
  assert.strictEqual(enriched.gene_summary, "Enriched summary.", "non-curated enrichment fields still merge");
  const newer = app.mergeAnnotations(app.state.currentResult.annotations, [
    geneAnnotation({ curated: true, curation_version: 5, cancer_association_rationale: "Newer curator." }),
  ]).find((a) => a.gene === "ALK");
  assert.strictEqual(newer.cancer_association_rationale, "Newer curator.", "a newer server version wins");
}

async function test_save_409_offers_reload_and_resaves_with_current_version() {
  const puts = [];
  const app = loadCurationApp({
    routes: jobRoutes({
      // Another curator saves v4 between our pre-save read and our PUT.
      current: () => (puts.length ? {
        version: 4,
        override: { curator_email: "other@x", updated_at: "2026-10-11T00:00:00Z", payload: { cancer_association_rationale: "Other curator." } },
      } : { version: 0, override: null }),
      extra: async (method, url, reqBody) => {
        if (method === "PUT") {
          puts.push(reqBody);
          if (puts.length === 1) {
            return [409, { detail: { message: "This override was changed by someone else; reload and reapply your edits.", current_version: 4 } }];
          }
          return [200, { version: 5, override: { curator_email: "c@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
        }
        if (method === "GET" && url === "/v1/annotate/run-1") {
          return [200, geneResult({ curated: true, curation_version: 4, curated_by: "other@x", curated_at: "2026-10-11T00:00:00Z", cancer_association_rationale: "Other curator." })];
        }
        return undefined;
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app);
  footerButton(app, "save").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "reload-conflict").length, "conflict");
  assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, /Another curator saved changes \(now v4\)/);
  assert.strictEqual(puts[0].expected_version, 0);
  assert.ok(!app.root("curation-modal").classList.contains("hidden"), "draft stays open");

  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad").baseVersion === 4, "reload");
  assert.strictEqual(app.state.currentResult.annotations[0].cancer_association_rationale, "Other curator.");
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Suggested rationale.", "draft kept");
  footerButton(app, "save").click();
  await waitFor(() => puts.length === 2, "resave");
  assert.strictEqual(puts[1].expected_version, 4);
}

async function test_save_422_shows_invalid_pmids() {
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method) => (method === "PUT"
        ? [422, { detail: { message: "Some PMIDs cannot be used as curated evidence.", invalid_pmids: [{ pmid: "333", reason: "retracted" }] } }]
        : undefined),
    }),
  });
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app));
  addPmid(app, "evidence", "333");
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad").draft, "draft");
  footerButton(app, "review").click();
  footerButton(app, "save").click();
  await waitFor(() => app.root("curation-status").allText.includes("Retracted"), "422");
  assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, /PMID 333: Retracted/);
  assert.strictEqual(oneByData(oneByData(body(app), "pmid", "333"), "curation", "invalid-reason").textContent, "PMID 333: Retracted");
}

async function test_precedent_only_edit_saves_only_precedent() {
  let put;
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        put = reqBody;
        return [200, { version: 1, override: { curator_email: "c@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
      },
    }),
  });
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app), { focus: "precedent" });
  oneByData(body(app), "curation", "evidence-include").checked = false;
  oneByData(body(app), "curation", "evidence-include").fire("change");
  addPmid(app, "precedent", "789");
  footerButton(app, "submit").click();
  await waitFor(() => app.requests.some((r) => r.method === "POST"), "POST");
  assert.deepStrictEqual(app.requests.find((r) => r.method === "POST").body.evidence_pmids, []);
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad").draft, "draft");
  footerButton(app, "review").click();
  assert.strictEqual(byData(body(app), "curation", "rationale").length, 0, "evidence not under edit");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.deepStrictEqual(put.payload, {
    therapeutic_precedent: {
      summary: "Suggested precedent.",
      precedents: [{ pmid: "789", quote: "Verbatim precedent quote.", therapies: ["crizotinib"], evidence_type: "clinical" }],
    },
  });
  await waitFor(() => app.state.currentResult.annotations[0].clinical_actionability?.curated, "rerender");
  assert.ok(app.root("results-window").allText.includes("Curated by c@x"));
}

async function test_curated_badge_flags_history_and_revert() {
  let deleted;
  const app = loadCurationApp({
    routes: async (method, url) => {
      if (method === "GET" && url === "/v1/curation/gene/ALK/history?tumor_type=LUAD") {
        return [200, { entries: [
          { version: 2, action: "save", curator_email: "cur@x", created_at: "2026-10-02T00:00:00Z", payload: { citations: ["111"] }, note: "fixed" },
          { version: 1, action: "save", curator_email: "cur@x", created_at: "2026-10-01T00:00:00Z", payload: { citations: ["9"] } },
        ] }];
      }
      if (method === "DELETE") {
        deleted = url;
        return [200, { reverted_version: 2 }];
      }
      if (method === "GET" && url === "/v1/annotate/run-1") return [200, geneResult({ cancer_association_rationale: "Pipeline again." })];
      return undefined;
    },
  });
  const win = render(app, geneResult({
    curated: true, curated_by: "cur@x", curated_at: "2026-10-02T09:00:00Z", curation_version: 2, curated_fields: ["citations"],
    quality_flags: [
      { code: "newer_pipeline_evidence", label: "Newer pipeline evidence since curation", severity: "warning",
        detail: "The pipeline now cites PMID(s) that were not in its evidence when this was curated: 555, 666. Consider reviewing the curated version." },
      { code: "curated_retracted_pmids_removed", label: "Retracted PMIDs removed from curated content", severity: "critical", detail: "Removed retracted PMID(s) from the curator's version: 9." },
      { code: "low_evidence_score", label: "Low evidence", severity: "warning", detail: "" },
    ],
  }, { curation_unavailable: true }));
  assert.strictEqual(oneByData(win, "curation", "curated-badge").textContent, "Curated by cur@x · 2026-10-02 (v2)");
  const newer = oneByData(win, "flag", "newer_pipeline_evidence");
  assert.ok(newer.allText.includes("555") && newer.allText.includes("666"), "lists the new PMIDs");
  assert.ok(byData(newer, "curation", "edit-evidence").length, "Edit evidence shortcut");
  assert.ok(oneByData(win, "flag", "curated_retracted_pmids_removed").allText.includes("9."));
  assert.ok(oneByData(win, "flag", "curation_unavailable_banner").allText.includes("Curator edits unavailable"));
  const statusLine = win.querySelectorAll(".status-line")[0];
  assert.ok(statusLine.allText.includes("Low evidence"));
  assert.ok(!statusLine.allText.includes("Newer pipeline evidence"), "curation flags aren't duplicated");

  oneByData(win, "curation", "view-history").click();
  await waitFor(() => app.root("curation-aux-body").querySelectorAll("li").length === 2, "history");
  const history = app.root("curation-aux-body").allText;
  assert.ok(history.includes("v2 · Saved") && history.includes("Note: fixed") && history.includes("Evidence PMIDs: 111"));
  app.pressKey("Escape");
  assert.ok(app.root("curation-aux-modal").classList.contains("hidden"));

  const revertButton = oneByData(app.root("results-window"), "curation", "revert");
  revertButton.focus(); // a real click focuses the button
  revertButton.click();
  assert.ok(app.root("curation-aux-body").allText.includes("Revert ALK to the pipeline output?"));
  assert.strictEqual(deleted, undefined, "nothing deleted before confirming");
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "revert");
  assert.strictEqual(deleted, "/v1/curation/gene/ALK?tumor_type=LUAD&expected_version=2");
  assert.ok(app.document.activeElement.classList.contains("curation-edit-button"), "focus lands on the re-rendered card");
  assert.strictEqual(app.document.activeElement.dataset.curationSubject, "gene|ALK|luad");
  assert.strictEqual(app.state.currentResult.annotations[0].cancer_association_rationale, "Pipeline again.");
  assert.strictEqual(byData(app.root("results-window"), "curation", "curated-badge").length, 0);
  assert.ok(app.root("toast-region").allText.includes("ALK reverted to pipeline output"));

  const viewer = loadCurationApp({ auth: { can_curate: false } });
  const viewerWin = render(viewer, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 2 }));
  assert.strictEqual(byData(viewerWin, "curation", "view-history").length, 1, "viewers can read history");
  assert.strictEqual(byData(viewerWin, "curation", "revert").length, 0, "viewers can't revert");
}

async function test_fusion_flow() {
  let put;
  const fusionDraft = {
    fusion: "EML4::ALK", interpretation: "Suggested fusion interpretation.", well_supported: true, pmids: ["111", "444"],
    evidence_cards: [
      { pmid: "111", title: "F1", abstract: "Fusion one.", selected_reason: "s1", quote: "Fusion one.", evidence_type: "clinical", fusion: "EML4::ALK" },
      { pmid: "444", title: "F4", abstract: "Fusion four.", selected_reason: "s4", quote: "Fusion four.", evidence_type: "case_report", fusion: "EML4::ALK" },
    ],
    clinical_actionability: null,
  };
  const app = loadCurationApp({
    routes: jobRoutes({
      draft: fusionDraft,
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        put = { url, body: reqBody };
        return [200, { version: 1, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
      },
    }),
  });
  app.state.resultsViewMode = "fusion";
  app.state.fusionEvidenceStatus = "complete";
  const item = { fusion: "EML4::ALK", tumor_type: "LUAD", well_supported: false, retrieved_count: 1, pmids: ["111"], interpretation: "Pipeline fusion.", evidence_cards: [], quality_flags: [] };
  const win = render(app, geneResult({}, { fusion_evidence: [item] }));
  oneByData(win, "curation", "edit-evidence").click();
  assert.strictEqual(byData(body(app), "list", "precedent").length, 0, "no precedent list for fusions");
  assert.strictEqual(byData(body(app), "curation", "evidence-include").length, 0);
  addPmid(app, "evidence", "444");
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("fusion|EML4::ALK|luad")?.draft, "draft");
  assert.deepStrictEqual(app.requests.find((r) => r.method === "POST").body, {
    subject_type: "fusion", subject_key: "EML4::ALK", tumor_type: "LUAD", evidence_pmids: ["111", "444"], precedent_pmids: [],
  });
  footerButton(app, "review").click();
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Suggested fusion interpretation.");
  const wellSupported = oneByData(body(app), "curation", "well-supported");
  assert.strictEqual(wellSupported.checked, true);
  assert.strictEqual(byData(body(app), "curation", "quotes").length, 0, "fusions have no supporting quotes");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.url, "/v1/curation/fusion/EML4%3A%3AALK?tumor_type=LUAD");
  assert.deepStrictEqual(put.body, {
    expected_version: 0,
    payload: {
      interpretation: "Suggested fusion interpretation.",
      well_supported: true,
      pmids: ["111", "444"],
      evidence_cards: [
        { pmid: "111", evidence_type: "clinical", selected_reason: "s1", quote: "Fusion one." },
        { pmid: "444", evidence_type: "case_report", selected_reason: "s4", quote: "Fusion four." },
      ],
    },
  });
  await waitFor(() => app.state.currentResult.fusion_evidence[0].curated, "rerender");
  const fused = app.state.currentResult.fusion_evidence[0];
  assert.strictEqual(fused.interpretation, "Suggested fusion interpretation.");
  assert.strictEqual(fused.retrieved_count, 2);
  assert.ok(app.root("results-window").allText.includes("Curated by cur@x · 2026-10-12 (v1)"));

  // A stale fusion-evidence poll keeps the curated version.
  const reconciled = app.reconcileFusionEvidence(app.state.currentResult.fusion_evidence, [item]);
  assert.strictEqual(reconciled[0].interpretation, "Suggested fusion interpretation.");
}

async function test_escape_closes_and_draft_survives_reopen() {
  const app = loadCurationApp({ routes: jobRoutes() });
  render(app, geneResult());
  await toDraft(app);
  const rationale = oneByData(body(app), "curation", "rationale");
  rationale.value = "Edited but not saved.";
  rationale.fire("input");
  app.pressKey("Escape");
  assert.ok(app.root("curation-modal").classList.contains("hidden"));
  render(app, app.state.currentResult);
  const edit = oneByData(app.root("results-window"), "curation", "edit-evidence");
  assert.match(edit.textContent, /draft in progress/);
  edit.click();
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Edited but not saved.");
  assert.strictEqual(app.document.activeElement, oneByData(body(app), "curation", "rationale"), "focus moves into the dialog");
  footerButton(app, "discard").click();
  assert.strictEqual(app.curationState.sessions.size, 0);
  await flush();
}

async function test_save_and_revert_503_contention_show_retry_message() {
  const puts = [];
  let deletes = 0;
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method === "PUT") {
          puts.push(reqBody);
          if (puts.length === 1) return [503, { detail: "Curation storage contention; please retry." }, { "Retry-After": "1" }];
          return [200, { version: 1, override: { curator_email: "c@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
        }
        if (method === "DELETE") {
          deletes += 1;
          return [503, { detail: "Curation storage contention; please retry." }, { "Retry-After": "2" }];
        }
        return undefined;
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app);
  footerButton(app, "save").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "retry-save").length, "retry offer");
  assert.strictEqual(
    oneByData(app.root("curation-status"), "curation", "message").textContent,
    "Couldn't save right now (Curation storage contention). Nothing was changed — please retry in 1 second."
  );
  assert.ok(!app.root("curation-modal").classList.contains("hidden"), "draft kept open");
  assert.strictEqual(app.curationState.sessions.get("gene|ALK|luad").baseVersion, 0, "version unchanged");
  oneByData(app.root("curation-status"), "curation", "retry-save").click();
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "retried save");
  assert.strictEqual(puts.length, 2);
  assert.deepStrictEqual(puts[1], puts[0], "retry resends the same body");

  oneByData(app.root("results-window"), "curation", "revert").click();
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => app.root("curation-aux-body").allText.includes("please retry"), "revert retry message");
  assert.ok(app.root("curation-aux-body").allText.includes(
    "Couldn't revert right now (Curation storage contention). Nothing was changed — please retry in 2 seconds."
  ));
  assert.ok(!app.root("curation-aux-modal").classList.contains("hidden"), "confirm stays open");
  await waitFor(() => !oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").disabled, "re-enabled");
  assert.strictEqual(deletes, 1);
  assert.ok(app.state.currentResult.annotations[0].curated, "still curated");
}

async function test_unresolved_tumor_context_disables_edit() {
  const app = loadCurationApp();
  const flag = { code: "curation_unavailable", label: "Curator edits unavailable", severity: "critical", detail: "Curator edits could not be loaded; showing pipeline output." };
  // Legacy shared link: ALK's tumor context couldn't be reconstructed (absent from the map).
  const win = render(app, geneResult({ quality_flags: [flag] }, { annotation_tumor_types: {}, curation_unavailable: true }));
  for (const action of ["edit-evidence", "edit-precedent"]) {
    const button = oneByData(win, "curation", action);
    assert.strictEqual(button.disabled, true, `${action} disabled`);
    assert.match(button.title, /tumor context for the gene couldn't be resolved/);
  }
  assert.ok(oneByData(win, "flag", "curation_unavailable_banner"));
  app.openCurationDialog(geneSubject(app));
  assert.ok(app.root("curation-modal").classList.contains("hidden"), "editor refuses to open");

  // A gene confirmed to have no tumor type (null in the map) stays editable.
  const ok = loadCurationApp();
  const okWin = render(ok, geneResult({ quality_flags: [flag] }, { annotation_tumor_types: { ALK: null } }));
  assert.strictEqual(oneByData(okWin, "curation", "edit-evidence").disabled, false);
}

// --- Review regressions -----------------------------------------------------

const CURATED_PRECEDENT = {
  summary: "Curated precedent.",
  precedents: [{ pmid: "789", quote: "Verbatim precedent quote.", therapies: ["crizotinib"], evidence_type: "clinical", title: "Precedent paper", abstract: "Verbatim precedent quote." }],
};

function bothSectionsCurated() {
  return geneResult({
    curated: true, curated_by: "old@x", curated_at: "2026-10-01T00:00:00Z", curation_version: 2,
    curated_fields: ["cancer_association_rationale", "citations", "evidence_cards", "supporting_quotes", "therapeutic_precedent"],
    clinical_actionability: {
      confidence_score: 1, summary: "Curated precedent.", confidence_explanation: "Curator-verified", pmids: ["789"],
      evidence: [{ pmid: "789", title: "Precedent paper", abstract: "Verbatim precedent quote.", quote: "Verbatim precedent quote.", therapies: ["crizotinib"], evidence_type: "clinical" }],
      curated: true,
    },
  });
}

async function test_evidence_only_edit_preserves_curated_precedent_verbatim() {
  let put;
  const currentPayload = {
    cancer_association_rationale: "Old curated rationale.", citations: ["111", "222"],
    evidence_cards: [{ pmid: "111", evidence_type: "clinical", selected_reason: "r1", quote: "Alpha sentence.", title: "Paper one" }],
    supporting_quotes: [{ pmid: "111", quote: "Alpha sentence." }],
    therapeutic_precedent: CURATED_PRECEDENT,
  };
  const app = loadCurationApp({
    routes: jobRoutes({
      current: { version: 2, override: { curator_email: "old@x", updated_at: "2026-10-01T00:00:00Z", payload: currentPayload } },
      extra: async (method, url, reqBody) => {
        if (method === "PUT") {
          put = reqBody;
          return [200, { version: 3, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
        }
        if (method === "GET" && url === "/v1/annotate/run-1") {
          return [200, geneResult({ ...bothSectionsCurated().annotations[0], curation_version: 3, curated_by: "cur@x", curated_at: "2026-10-12T00:00:00Z", cancer_association_rationale: "Server-rendered rationale." })];
        }
        return undefined;
      },
    }),
  });
  render(app, bothSectionsCurated());
  app.openCurationDialog(geneSubject(app), { focus: "evidence" });
  assert.strictEqual(oneByData(body(app), "curation", "precedent-include").checked, false, "precedent not under edit");
  const evidenceInclude = oneByData(body(app), "curation", "evidence-include");
  evidenceInclude.checked = false;
  evidenceInclude.fire("change");
  footerButton(app, "submit").click();
  assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, /Choose evidence, precedent, or both/);
  assert.ok(!app.requests.some((r) => r.method === "POST"), "nothing submitted");
  oneByData(body(app), "curation", "evidence-include").checked = true;
  oneByData(body(app), "curation", "evidence-include").fire("change");
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad")?.draft, "draft");
  assert.deepStrictEqual(app.requests.find((r) => r.method === "POST").body.precedent_pmids, [], "precedent not re-evaluated");
  footerButton(app, "review").click();
  assert.strictEqual(byData(body(app), "curation", "precedent").length, 0, "no precedent editor");
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.expected_version, 2);
  assert.deepStrictEqual(put.payload.therapeutic_precedent, CURATED_PRECEDENT, "precedent sent verbatim");
  assert.strictEqual(put.payload.cancer_association_rationale, "Suggested rationale.", "edited section replaced");
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "closed");
  const shown = app.state.currentResult.annotations[0];
  assert.strictEqual(shown.cancer_association_rationale, "Server-rendered rationale.", "renders the server's version");
  assert.ok(shown.clinical_actionability.curated, "curated precedent still shown");
}

async function saveOnce(app, version) {
  await toDraft(app);
  footerButton(app, "save").click();
  await waitFor(() => app.curationState.localVersions.get("gene|ALK|luad")?.version === version, "saved");
}

async function test_delayed_annotation_poll_cannot_undo_save() {
  let polls = 0;
  const stale = geneAnnotation({ cancer_association_rationale: "Pipeline rationale (stale poll)." });
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method === "PUT") return [200, { version: 1, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
        if (url === "/v1/annotate/jobs/a1") {
          polls += 1;
          const base = { job_id: "a1", genes_completed: 1, genes_total: 1, fusions_processed: 1, annotations: [stale], annotation_tumor_types: { ALK: "LUAD" } };
          if (polls === 1) return [200, { ...base, status: "running", result: null }];
          return [200, { ...base, status: "complete", result: geneResult({ cancer_association_rationale: "Pipeline rationale (stale poll)." }) }];
        }
        return undefined;
      },
    }),
  });
  render(app, geneResult());
  await saveOnce(app, 1);
  assert.strictEqual(app.state.currentResult.annotations[0].cancer_association_rationale, "Suggested rationale.");
  const final = await app.pollAnnotationJob("/v1/annotate/jobs/a1");
  assert.strictEqual(polls, 2);
  const shown = app.state.currentResult.annotations[0];
  assert.strictEqual(shown.cancer_association_rationale, "Suggested rationale.", "completion poll kept the save");
  assert.strictEqual(shown.curation_version, 1);
  assert.strictEqual(final.annotations[0].cancer_association_rationale, "Suggested rationale.");
}

async function test_cross_tumor_context_draft_never_touches_displayed_annotation() {
  let put;
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        put = { url, body: reqBody };
        return [200, { version: 1, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app); // ALK · LUAD draft
  app.pressKey("Escape");
  // Another run now shows ALK under a different tumor context.
  render(app, geneResult({ cancer_association_rationale: "BRCA pipeline." }, { run_id: "run-2", annotation_tumor_types: { ALK: "BRCA" } }));
  const luad = { type: "gene", key: "ALK", tumorType: "LUAD", label: "ALK" };
  assert.strictEqual(app.findCurationRecord(luad), null, "LUAD subject isn't the displayed BRCA record");
  assert.ok(!app.replaceCurationRecord(luad, { gene: "ALK" }), "replace refuses another context");
  assert.ok(!oneByData(app.root("results-window"), "curation", "edit-evidence").textContent.includes("draft in progress"));
  app.openCurationDialog(luad, { step: "review" });
  footerButton(app, "save").click();
  await waitFor(() => put, "PUT");
  assert.strictEqual(put.url, "/v1/curation/gene/ALK?tumor_type=LUAD", "saves its own subject");
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "closed");
  const shown = app.state.currentResult.annotations[0];
  assert.strictEqual(shown.cancer_association_rationale, "BRCA pipeline.", "BRCA annotation untouched");
  assert.ok(!shown.curated);
  // Fusion keys are canonicalized: EML4--ALK is the same subject as EML4::ALK.
  assert.strictEqual(
    app.curationSubjectId({ type: "fusion", key: "eml4--alk", tumorType: " LUAD " }),
    app.curationSubjectId({ type: "fusion", key: "EML4::ALK", tumorType: "luad" })
  );
}

async function test_lists_locked_while_submission_pending() {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const routes = jobRoutes();
  const app = loadCurationApp({
    routes: async (method, url, reqBody) => {
      if (method === "POST") await gate;
      return routes(method, url, reqBody);
    },
  });
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app));
  const session = app.curationState.sessions.get("gene|ALK|luad");
  footerButton(app, "submit").click(); // POST now pending
  assert.ok(session.submitting);
  assert.ok(oneByData(body(app), "curation", "evidence-input").disabled, "input disabled while POST pending");
  assert.ok(oneByData(body(app), "curation", "evidence-add").disabled);
  assert.ok(oneByData(body(app), "curation", "precedent-include").disabled);
  assert.ok(oneByData(oneByData(body(app), "pmid", "111"), "curation", "remove-pmid").disabled);
  assert.match(app.addCurationPmids(session, "evidence", "999").error, /Wait for the current re-evaluation/);
  app.removeCurationPmid(session, "evidence", "111");
  app.moveCurationPmid(session, "evidence", "222", -1);
  app.setCurationInclude(session, "precedent", true);
  assert.deepStrictEqual([...session.lists.evidence], ["111", "222"], "lists unchanged");
  assert.strictEqual(session.include.precedent, false);
  release();
  await waitFor(() => session.draft, "draft");
  assert.deepStrictEqual(app.requests.find((r) => r.method === "POST").body.evidence_pmids, ["111", "222"]);
  assert.ok(Object.isFrozen(session.submitted.lists.evidence), "draft built from a frozen snapshot");
  assert.deepStrictEqual([...session.draft.cards.map((c) => c.pmid)], ["111", "222"]);
}

async function test_failed_conflict_reload_keeps_expected_version() {
  const puts = [];
  let getFails = false;
  const app = loadCurationApp({
    routes: jobRoutes({
      current: () => (puts.length ? {
        version: 4,
        override: { curator_email: "other@x", updated_at: "2026-10-11T00:00:00Z", payload: { cancer_association_rationale: "Other.", citations: ["9"] } },
      } : { version: 0, override: null }),
      extra: async (method, url, reqBody) => {
        if (method === "PUT") {
          puts.push(reqBody);
          return [409, { detail: { message: "changed", current_version: 4 } }];
        }
        if (method === "GET" && url === "/v1/annotate/run-1") {
          return [200, geneResult({ curated: true, curation_version: 4, curated_by: "other@x", curated_at: "2026-10-11T00:00:00Z", cancer_association_rationale: "Other." })];
        }
        return undefined;
      },
    }),
  });
  const routes = app.fetch;
  app.fetch = async (url, options) => (getFails && url.startsWith("/v1/curation/gene/") ? { ok: false, status: 500, headers: { get: () => null }, text: async () => "boom", json: async () => ({}) } : routes(url, options));
  app.globalThis.fetch = app.fetch;
  render(app, geneResult());
  await toDraft(app);
  footerButton(app, "save").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "reload-conflict").length, "conflict");
  const session = app.curationState.sessions.get("gene|ALK|luad");
  getFails = true;
  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => app.root("curation-status").allText.includes("Couldn't load the latest version"), "reload error");
  assert.strictEqual(session.baseVersion, 0, "version not advanced");
  assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, /still targets v0\. Retry the reload\./);
  getFails = false;
  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => session.baseVersion === 4, "advanced after a successful fetch and refresh");
  const message = oneByData(app.root("curation-status"), "curation", "message").textContent;
  assert.match(message, /v4 by other@x on 2026-10-11\. The results now show it\./);
  assert.strictEqual(app.state.currentResult.annotations[0].cancer_association_rationale, "Other.");
}

async function test_conflict_reload_with_failed_run_refresh_requires_reviewing_preview() {
  const puts = [];
  let current = { version: 0, override: null };
  const app = loadCurationApp({
    routes: jobRoutes({
      current: () => current,
      extra: async (method, url, reqBody) => {
        if (method === "PUT") {
          puts.push(reqBody);
          return [200, { version: 5, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
        }
        if (method === "GET" && url === "/v1/annotate/run-1") return [500, { detail: "run store down" }];
        return undefined;
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app);
  current = {
    version: 4,
    override: {
      curator_email: "other@x", updated_at: "2026-10-11T00:00:00Z",
      payload: {
        cancer_association_rationale: "Other curator's <b>rationale</b>.", citations: ["9"],
        evidence_cards: [{ pmid: "9", evidence_type: "clinical", selected_reason: "Other reason.", quote: "Other quote." }],
        therapeutic_precedent: { summary: "Other precedent.", precedents: [{ pmid: "789", therapies: ["alectinib"], quote: "Q." }] },
      },
    },
  };
  footerButton(app, "save").click(); // the pre-save read finds v4: conflict, no PUT
  await waitFor(() => byData(app.root("curation-status"), "curation", "reload-conflict").length, "conflict");
  assert.strictEqual(puts.length, 0);
  const session = app.curationState.sessions.get("gene|ALK|luad");
  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "conflict-preview").length, "preview");
  assert.strictEqual(session.baseVersion, 0, "not advanced while the results can't show v4");
  assert.match(oneByData(app.root("curation-status"), "curation", "message").textContent, /couldn't be refreshed.*still targets v0/);
  const preview = oneByData(app.root("curation-status"), "curation", "conflict-preview");
  for (const text of ["Other curator's <b>rationale</b>.", "PMID 9 — clinical — Other reason. — “Other quote.”", "Other precedent.", "PMID 789 — alectinib — “Q.”"]) {
    assert.ok(preview.querySelectorAll("dd").some((dd) => dd.textContent === text), `preview shows ${text}`);
  }
  assert.ok(!preview.allText.includes("<dd>"), "rendered as text, not markup");

  // Saving without confirming still targets v0, so the server-side check stops it.
  footerButton(app, "save").click();
  await waitFor(() => !app.curationState.busy && byData(app.root("curation-status"), "curation", "reload-conflict").length, "still conflicted");
  assert.strictEqual(puts.length, 0, "no PUT against an unseen version");

  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "accept-conflict").length, "preview again");
  oneByData(app.root("curation-status"), "curation", "accept-conflict").click();
  assert.strictEqual(session.baseVersion, 4, "advances only after the curator reviewed v4");
  footerButton(app, "save").click();
  await waitFor(() => puts.length === 1, "re-save");
  assert.strictEqual(puts[0].expected_version, 4);
}

async function test_conflict_with_revert_and_failed_refresh_keeps_version() {
  let current = { version: 0, override: null };
  const app = loadCurationApp({
    routes: jobRoutes({
      current: () => current,
      extra: async (method, url) => {
        if (method === "PUT") return [409, { detail: { message: "changed", current_version: 3 } }];
        if (method === "GET" && url === "/v1/annotate/run-1") return [500, { detail: "down" }];
        return undefined;
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app);
  current = { version: 3, override: null }; // another curator saved then reverted
  footerButton(app, "save").click();
  await waitFor(() => byData(app.root("curation-status"), "curation", "reload-conflict").length, "conflict");
  oneByData(app.root("curation-status"), "curation", "reload-conflict").click();
  await waitFor(() => app.root("curation-status").allText.includes("Retry the reload"), "retry");
  assert.strictEqual(app.curationState.sessions.get("gene|ALK|luad").baseVersion, 0);
  assert.strictEqual(byData(app.root("curation-status"), "curation", "accept-conflict").length, 0, "no preview of pipeline content");
}

async function test_save_restores_focus_to_rerendered_opener() {
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => (method === "PUT"
        ? [200, { version: 1, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }]
        : undefined),
    }),
  });
  render(app, geneResult());
  const opener = oneByData(app.root("results-window"), "curation", "edit-evidence");
  opener.focus();
  opener.click();
  addPmid(app, "evidence", "333");
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|ALK|luad")?.draft, "draft");
  footerButton(app, "review").click();
  footerButton(app, "save").click();
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "saved");
  const active = app.document.activeElement;
  assert.ok(active.isConnected, "focus is on a connected element");
  assert.strictEqual(active.dataset.curation, "edit-evidence");
  assert.notStrictEqual(active, opener, "the re-rendered opener, not the detached one");
  assert.ok(app.root("results-window").contains(active));
}

async function test_focus_recovery_uses_control_id_for_quote_rows() {
  const app = loadCurationApp({ routes: jobRoutes() });
  render(app, geneResult());
  await toDraft(app);
  oneByData(body(app), "curation", "add-quote").click(); // 3 quote rows
  const removes = byData(body(app), "curation", "remove-quote");
  assert.strictEqual(removes.length, 3);
  // Focus the second row's PMID select, then rebuild (as a select change does).
  const selects = byData(body(app), "curation", "quote-pmid");
  selects[1].focus();
  const id = selects[1].id;
  app.renderCurationDialog();
  assert.strictEqual(app.document.activeElement.id, id, "same row's control by id, not the first quote row");
  assert.notStrictEqual(app.document.activeElement, byData(body(app), "curation", "quote-pmid")[0]);
  removes[2].focus();
  const removeId = removes[2].id;
  app.renderCurationDialog();
  assert.strictEqual(app.document.activeElement.id, removeId);
}

async function test_retry_refresh_wording_matches_fresh_record() {
  let calls = 0;
  const app = loadCurationApp({
    routes: async (method, url) => {
      if (method === "DELETE") return [200, { reverted_version: 2 }];
      if (method === "GET" && url === "/v1/annotate/run-1") {
        calls += 1;
        if (calls === 1) return [500, { detail: "down" }];
        // Meanwhile another curator saved v3.
        return [200, geneResult({ curated: true, curated_by: "other@x", curated_at: "2026-10-12", curation_version: 3 })];
      }
      return undefined;
    },
  });
  render(app, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 2 }));
  oneByData(app.root("results-window"), "curation", "revert").click();
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => byData(app.root("results-window"), "curation", "retry-refresh").length, "pending");
  oneByData(app.root("results-window"), "curation", "retry-refresh").click();
  await waitFor(() => !app.curationState.pendingRefresh.size, "refreshed");
  const toasts = app.root("toast-region").allText;
  assert.ok(toasts.includes("ALK now shows the current curated version (v3) by other@x"));
  assert.ok(!toasts.includes("now shows the pipeline version"));
}

async function test_focus_trap_and_inert_background() {
  const app = loadCurationApp();
  render(app, geneResult());
  app.openCurationDialog(geneSubject(app));
  const modal = app.root("curation-modal");
  const shell = app.root(".app-shell");
  assert.strictEqual(shell.inert, true);
  assert.strictEqual(shell.getAttribute("aria-hidden"), "true");
  assert.strictEqual(app.root("curation-notifications").inert, true);
  assert.notStrictEqual(modal.inert, true);
  const focusable = modal.querySelectorAll("button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])")
    .filter((n) => !n.closest(".hidden"));
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  assert.strictEqual(first.id, "close-curation");
  last.focus();
  let event = app.pressKey("Tab");
  assert.ok(event.defaultPrevented);
  assert.strictEqual(app.document.activeElement, first, "Tab from last wraps to first");
  event = app.pressKey("Tab", { shiftKey: true });
  assert.ok(event.defaultPrevented);
  assert.strictEqual(app.document.activeElement, last, "Shift+Tab from first wraps to last");
  app.root("body").focus(); // focus escaped somehow
  app.pressKey("Tab");
  assert.ok(modal.contains(app.document.activeElement), "Tab pulls focus back into the dialog");

  // Removing a focused row moves focus deliberately, not to the page.
  const removeButton = oneByData(oneByData(body(app), "pmid", "222"), "curation", "remove-pmid");
  removeButton.focus();
  removeButton.click();
  assert.ok(modal.contains(app.document.activeElement), "focus stays in the dialog after a rebuild");

  app.pressKey("Escape");
  assert.strictEqual(shell.inert, false);
  assert.strictEqual(shell.getAttribute("aria-hidden"), null);
}

async function test_revert_with_failed_reload_shows_pending_refresh() {
  let runOk = false;
  const app = loadCurationApp({
    routes: async (method, url) => {
      if (method === "DELETE") return [200, { reverted_version: 2 }];
      if (method === "GET" && url === "/v1/annotate/run-1") return runOk ? [200, geneResult({ cancer_association_rationale: "Pipeline again." })] : [500, { detail: "down" }];
      return undefined;
    },
  });
  render(app, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 2, cancer_association_rationale: "Curated text." }));
  oneByData(app.root("results-window"), "curation", "revert").click();
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "revert done");
  const win = app.root("results-window");
  assert.ok(byData(win, "flag", "pending_refresh").length, "pending-refresh state shown");
  assert.strictEqual(byData(win, "curation", "curated-badge").length, 0, "not presented as curated");
  assert.ok(!app.root("toast-region").allText.includes("reverted to pipeline output"), "no success toast");
  assert.ok(app.root("toast-region").allText.includes("couldn't be loaded yet"));
  runOk = true;
  oneByData(win, "curation", "retry-refresh").click();
  await waitFor(() => !app.curationState.pendingRefresh.size, "refreshed");
  assert.strictEqual(app.state.currentResult.annotations[0].cancer_association_rationale, "Pipeline again.");
  assert.strictEqual(byData(app.root("results-window"), "flag", "pending_refresh").length, 0);
}

// ---------------------------------------------------------------------------
// Visibility: every curator edit control renders only when /auth/me says
// can_curate. Viewers get curated content read-only on every surface.
// ---------------------------------------------------------------------------

// data-curation actions that change or start changing curated content.
const EDIT_ACTIONS = [
  "edit-evidence", "edit-precedent", "revert", "confirm-revert", "submit", "review", "save", "retry-refresh",
];

function curatedEverywhereResult() {
  const newer = {
    code: "newer_pipeline_evidence", label: "Newer pipeline evidence since curation", severity: "warning",
    detail: "The pipeline now cites PMID(s) that were not in its evidence when this was curated: 555.",
  };
  return geneResult({
    curated: true, curated_by: "cur@x", curated_at: "2026-10-02T09:00:00Z", curation_version: 2,
    quality_flags: [newer],
    clinical_actionability: {
      curated: true, confidence_score: 0.9, summary: "Curated precedent.", confidence_explanation: "e",
      pmids: ["789"],
      evidence: [{ pmid: "789", title: "Precedent paper", abstract: "Precedent abstract.", quote: "q", therapies: ["crizotinib"] }],
    },
  }, {
    fusion_evidence: [{
      fusion: "EML4::ALK", tumor_type: "LUAD", well_supported: true, retrieved_count: 1, pmids: ["111"],
      literature_scope: "requested_tumor", requested_tumor_pmids: ["111"],
      interpretation: "Curated fusion.", curated: true, curated_by: "cur@x", curated_at: "2026-10-02T09:00:00Z",
      curation_version: 1, quality_flags: [newer],
      evidence_cards: [{ pmid: "111", title: "F1", abstract: "Fusion one.", selected_reason: "s1", quote: "Fusion one.", evidence_type: "clinical" }],
    }],
  });
}

function editControls(win) {
  return [...new Set([
    ...EDIT_ACTIONS.flatMap((action) => byData(win, "curation", action)),
    ...win.querySelectorAll(".curation-edit-button"),
    ...win.querySelectorAll(".curation-panel, .supporting-evidence, .clinical-actionability, .fusion-evidence-card")
      .flatMap((section) => section.querySelectorAll("input, textarea, select")),
  ])];
}

function renderBothViews(app) {
  const result = curatedEverywhereResult();
  app.state.resultsViewMode = "gene";
  const geneWin = render(app, result);
  const gene = {
    controls: editControls(geneWin),
    precedent: geneWin.querySelectorAll(".clinical-actionability"),
    text: geneWin.allText,
  };
  app.state.resultsViewMode = "fusion";
  app.state.fusionEvidenceStatus = "complete";
  const fusionWin = render(app, result);
  const fusion = {
    controls: editControls(fusionWin),
    cards: fusionWin.querySelectorAll(".fusion-evidence-card"),
    text: fusionWin.allText,
  };
  return { gene, fusion, result };
}

async function test_viewer_from_auth_me_sees_no_edit_controls_anywhere() {
  const app = loadCurationApp({
    auth: { can_curate: undefined },
    routes: async (method, url) => (url === "/auth/me"
      ? [200, { auth_enabled: true, authenticated: true, email: "view@x", role: "viewer", can_curate: false }]
      : undefined),
  });
  await app.checkAuth();
  assert.strictEqual(app.state.auth.can_curate, false, "role comes from /auth/me");

  const { gene, fusion, result } = renderBothViews(app);
  // Curated content is still shown, read-only.
  assert.ok(gene.text.includes("Curated by cur@x"), "viewers see the curated badge on genes");
  assert.ok(gene.text.includes("Curated precedent."), "viewers see the curated precedent");
  assert.ok(gene.text.includes("789"), "viewers see precedent PMIDs");
  assert.strictEqual(gene.precedent.length, 1);
  assert.ok(fusion.cards.length === 1 && fusion.text.includes("Curated fusion."), "viewers see the curated fusion");
  assert.ok(fusion.text.includes("Curated by cur@x"), "viewers see the curated badge on fusions");
  assert.deepStrictEqual(gene.controls.map((c) => c.dataset.curation || c.tagName), [], "no gene-card edit controls");
  assert.deepStrictEqual(fusion.controls.map((c) => c.dataset.curation || c.tagName), [], "no fusion-card edit controls");

  // Nothing opens the editor or starts a re-evaluation for a viewer.
  const geneSubject = app.geneCurationSubject(result.annotations[0]);
  const fusionSubject = app.fusionCurationSubject(result.fusion_evidence[0]);
  for (const subject of [geneSubject, fusionSubject]) {
    for (const focus of ["evidence", "precedent"]) {
      app.openCurationDialog(subject, { focus });
      app.openCurationDialog(subject, { focus, step: "review" });
    }
  }
  assert.ok(app.root("curation-modal").classList.contains("hidden"), "the editor never opens");
  assert.strictEqual(app.root("curation-body").children.length, 0, "no PMID inputs were built");
  assert.strictEqual(app.curationState.sessions.size, 0, "no edit/re-evaluate session");
  assert.strictEqual(app.root("curation-notifications").children.length, 0, "no re-evaluate notifications");
  assert.ok(
    !app.requests.some((r) => r.method !== "GET" || r.url.startsWith("/v1/curation/reevaluate")),
    "no curation write or re-evaluate request",
  );
}

async function test_curator_from_auth_me_sees_edit_controls_everywhere() {
  const app = loadCurationApp({
    auth: { can_curate: undefined },
    routes: async (method, url) => (url === "/auth/me"
      ? [200, { auth_enabled: false, authenticated: true, email: "cur@x", role: "curator", can_curate: true }]
      : undefined),
  });
  await app.checkAuth();
  assert.strictEqual(app.state.auth.can_curate, true);
  const { gene, fusion, result } = renderBothViews(app);
  const actions = (controls) => controls.map((c) => c.dataset.curation).filter(Boolean).sort();
  // Edit evidence twice (heading + newer-evidence shortcut), Edit precedent, Revert.
  assert.deepStrictEqual(actions(gene.controls), ["edit-evidence", "edit-evidence", "edit-precedent", "revert"]);
  assert.ok(gene.precedent[0].querySelectorAll(".curation-edit-button").length === 1, "Edit precedent sits in the precedent section");
  assert.deepStrictEqual(actions(fusion.controls), ["edit-evidence", "edit-evidence", "revert"]);

  // The precedent PMID editor and the re-evaluate submit are reachable.
  app.openCurationDialog(app.geneCurationSubject(result.annotations[0]), { focus: "precedent" });
  assert.ok(!app.root("curation-modal").classList.contains("hidden"));
  assert.ok(byData(body(app), "curation", "precedent-input").length === 1, "precedent PMID input");
  assert.ok(byData(body(app), "curation", "evidence-input").length === 1, "evidence PMID input");
  assert.ok(byData(app.root("curation-footer"), "curation", "submit").length === 1, "re-evaluate submit");
}

// Curated text, LLM draft text and abstracts are never parsed as markup.
async function test_curated_and_draft_text_never_render_as_markup() {
  const evil = '<img src=x onerror="alert(1)">';
  const htmlOf = (el) => [el._html, ...el.children.flatMap(htmlOf)];
  const assertNoMarkup = (app, label) => {
    ["results-window", "curation-body", "curation-footer", "curation-aux-body", "curation-notifications", "toast-region"]
      .forEach((id) => {
        const raw = htmlOf(app.root(id)).filter((html) => html.includes("<img"));
        assert.deepStrictEqual(raw, [], `${label}: unescaped markup in #${id}`);
      });
  };
  const draft = {
    gene: "ALK",
    cancer_association_rationale: `Rationale ${evil}`,
    evidence_cards: [{
      pmid: "111", title: `Title ${evil}`, journal: `Journal ${evil}`, abstract: `Abstract ${evil}`,
      selected_reason: `Reason ${evil}`, quote: `Quote ${evil}`, evidence_type: "clinical",
    }],
    supporting_quotes: [{ pmid: "111", quote: `Quote ${evil}` }],
    clinical_actionability: {
      summary: `Precedent ${evil}`, pmids: ["789"],
      evidence: [{ pmid: "789", title: `PTitle ${evil}`, abstract: `PAbstract ${evil}`, quote: `PQuote ${evil}`, therapies: [`drug ${evil}`] }],
    },
  };
  const app = loadCurationApp({ routes: jobRoutes({ draft }) });
  render(app, geneResult({
    curated: true, curated_by: `cur${evil}@x`, curated_at: "2026-10-02T09:00:00Z", curation_version: 1,
    cancer_association_rationale: `Curated ${evil}`,
    evidence_cards: [{ pmid: "111", title: `Curated title ${evil}`, journal: "J", abstract: `Curated abstract ${evil}`, selected_reason: `Curated reason ${evil}`, quote: `Curated quote ${evil}`, evidence_type: "clinical" }],
    quality_flags: [{ code: "curated_retracted_pmids_removed", label: `Flag ${evil}`, severity: "critical", detail: `Detail ${evil}` }],
  }));
  assertNoMarkup(app, "curated card");
  assert.ok(app.root("results-window").textContent.includes(`Detail ${evil}`), "flag detail shown as text");

  await toDraft(app);
  assertNoMarkup(app, "draft review");
  const reviewText = body(app).textContent;
  assert.ok(reviewText.includes(`Abstract ${evil}`), "draft abstract shown as text");
  assert.ok(reviewText.includes(`Title ${evil}`), "draft title shown as text");
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, `Rationale ${evil}`, "draft rationale is a field value");
}

// ---------------------------------------------------------------------------
// Losing curator access mid-session, and pending writes
// ---------------------------------------------------------------------------

const ACCESS_LOST = "You no longer have curator access";

function meRoute(getMe, routes) {
  return async (method, url, reqBody) => (url === "/auth/me" ? [200, getMe()] : routes(method, url, reqBody));
}

function typeInto(node, value, event = "input") {
  node.value = value;
  node.fire(event);
}

function noticeText(app) {
  return `${app.root("toast-region").allText} ${app.root("curation-notifications").allText}`;
}

async function test_demotion_on_auth_refresh_removes_controls_and_blocks_writes() {
  const curator = { auth_enabled: true, authenticated: true, email: "cur@x", role: "curator", can_curate: true };
  let me = curator;
  const app = loadCurationApp({ auth: { can_curate: undefined }, routes: meRoute(() => me, jobRoutes()) });
  await app.checkAuth();
  const result = curatedEverywhereResult();
  render(app, result);
  assert.ok(editControls(app.root("results-window")).length > 0, "curator starts with edit controls");
  await toDraft(app);
  typeInto(oneByData(body(app), "curation", "rationale"), "Typed by the curator.");
  typeInto(oneByData(body(app), "curation", "note"), "Typed note.");
  const id = "gene|ALK|luad";
  const session = app.curationState.sessions.get(id);

  me = { ...curator, role: "viewer", can_curate: false };
  await app.checkAuth();

  assert.ok(app.root("curation-modal").classList.contains("hidden"), "the open editor closes");
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), [],
    "no gene-card edit controls remain");
  app.state.resultsViewMode = "fusion";
  app.state.fusionEvidenceStatus = "complete";
  app.renderAnnotationResult(app.state.currentResult);
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), [],
    "no fusion-card edit controls remain");
  for (const action of ["notification-review", "notification-open"]) {
    assert.strictEqual(byData(app.root("curation-notifications"), "curation", action).length, 0, `no ${action}`);
  }
  assert.ok(noticeText(app).includes(ACCESS_LOST), "a short notice explains why");
  assert.strictEqual(app.curationState.sessions.get(id), session, "the draft stays in memory");
  assert.strictEqual(session.draft.rationale, "Typed by the curator.");
  assert.strictEqual(session.note, "Typed note.");

  // No handler sends anything without permission.
  const before = app.requests.length;
  const subject = app.geneCurationSubject(result.annotations[0]);
  await app.saveCurationDraft(session);
  await app.submitCurationReevaluation(session);
  await app.reloadAfterCurationConflict(session);
  await app.revertCuration(subject, 2, app.document.createElement("p"), app.document.createElement("button"));
  app.confirmCurationRevert(subject);
  app.openCurationDialog(subject, { step: "review" });
  assert.deepStrictEqual(app.requests.slice(before).map((r) => `${r.method} ${r.url}`), [], "no request was sent");
  assert.ok(app.root("curation-aux-modal").classList.contains("hidden"), "revert confirmation never opens");
  assert.ok(app.root("curation-modal").classList.contains("hidden"), "editor never opens");

  // Re-promotion brings the controls and the kept draft back.
  me = curator;
  await app.checkAuth();
  assert.ok(editControls(app.root("results-window")).length > 0, "controls return on re-promotion");
  app.openCurationDialog(subject, { step: "review" });
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Typed by the curator.");
  assert.strictEqual(oneByData(body(app), "curation", "note").value, "Typed note.");
}

async function test_save_403_flips_to_read_only_and_keeps_the_draft() {
  const puts = [];
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        puts.push(reqBody);
        return [403, { detail: "Curator privileges required" }];
      },
    }),
  });
  render(app, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 0 }));
  await toDraft(app);
  typeInto(oneByData(body(app), "curation", "rationale"), "Typed before the 403.");
  footerButton(app, "save").click();
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "editor closes on 403");
  assert.strictEqual(puts.length, 1);
  assert.strictEqual(app.state.auth.can_curate, false, "the UI is read-only now");
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), []);
  assert.ok(noticeText(app).includes(ACCESS_LOST));
  const session = app.curationState.sessions.get("gene|ALK|luad");
  assert.ok(session && session.draft, "draft kept");
  assert.strictEqual(session.draft.rationale, "Typed before the 403.");
  await app.saveCurationDraft(session);
  assert.strictEqual(puts.length, 1, "no retry is sent without permission");
}

async function test_submit_and_revert_403_flip_to_read_only() {
  const app = loadCurationApp({
    routes: async (method) => (method === "POST" || method === "DELETE" ? [403, { detail: "Curator privileges required" }] : undefined),
  });
  render(app, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 2 }));
  oneByData(app.root("results-window"), "curation", "revert").click();
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "revert dialog closes on 403");
  assert.strictEqual(app.state.auth.can_curate, false);
  assert.ok(noticeText(app).includes(ACCESS_LOST));
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), []);

  const app2 = loadCurationApp({
    routes: async (method) => (method === "POST" ? [403, { detail: "Curator privileges required" }] : undefined),
  });
  render(app2, geneResult());
  app2.openCurationDialog(geneSubject(app2));
  addPmid(app2, "evidence", "333");
  footerButton(app2, "submit").click();
  await waitFor(() => app2.root("curation-modal").classList.contains("hidden"), "editor closes on 403");
  assert.strictEqual(app2.state.auth.can_curate, false);
  assert.deepStrictEqual([...app2.curationState.sessions.get("gene|ALK|luad").lists.evidence], ["111", "222", "333"],
    "PMID edits kept");
}

async function test_editor_is_read_only_while_a_save_is_pending() {
  const gates = [];
  const puts = [];
  const app = loadCurationApp({
    routes: jobRoutes({
      extra: async (method, url, reqBody) => {
        if (method !== "PUT") return undefined;
        puts.push(reqBody);
        await new Promise((resolve) => gates.push(resolve));
        if (puts.length === 1) return [500, { detail: "boom" }];
        return [200, { version: 1, override: { curator_email: "cur@x", updated_at: "2026-10-12T00:00:00Z", payload: reqBody.payload } }];
      },
    }),
  });
  render(app, geneResult());
  await toDraft(app);
  typeInto(oneByData(body(app), "curation", "rationale"), "Final text.");
  typeInto(oneByData(body(app), "curation", "note"), "My note.");
  const fields = () => body(app).querySelectorAll("input, textarea, select, button");

  footerButton(app, "save").click();
  await waitFor(() => gates.length === 1, "PUT pending");
  assert.ok(fields().length > 0);
  assert.deepStrictEqual(fields().filter((node) => !node.disabled).map((n) => n.dataset.curation || n.tagName), [],
    "every review field is read-only while the PUT is in flight");
  assert.ok(footerButton(app, "save").disabled, "save disabled");
  assert.ok(footerButton(app, "discard").disabled && footerButton(app, "back").disabled);
  gates[0]();
  await waitFor(() => !app.curationState.busy, "first save failed");
  assert.ok(!app.root("curation-modal").classList.contains("hidden"), "editor stays open on failure");
  assert.strictEqual(oneByData(body(app), "curation", "rationale").disabled, false, "editable again");
  assert.strictEqual(oneByData(body(app), "curation", "rationale").value, "Final text.", "input intact");
  assert.strictEqual(oneByData(body(app), "curation", "note").value, "My note.");
  assert.ok(!footerButton(app, "save").disabled);

  footerButton(app, "save").click();
  await waitFor(() => gates.length === 2, "second PUT pending");
  assert.deepStrictEqual(fields().filter((node) => !node.disabled).map((n) => n.dataset.curation || n.tagName), []);
  gates[1]();
  await waitFor(() => app.root("curation-modal").classList.contains("hidden"), "saved");
  assert.strictEqual(puts[1].payload.cancer_association_rationale, "Final text.", "what was saved is what was typed");
  assert.strictEqual(puts[1].note, "My note.");
}

async function test_revert_dialog_is_locked_while_delete_is_pending() {
  let release;
  const app = loadCurationApp({
    routes: async (method, url) => {
      if (method === "DELETE") {
        await new Promise((resolve) => { release = resolve; });
        return [500, { detail: "boom" }];
      }
      return undefined;
    },
  });
  render(app, geneResult({ curated: true, curated_by: "cur@x", curated_at: "2026-10-02", curation_version: 2 }));
  oneByData(app.root("results-window"), "curation", "revert").click();
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => release, "DELETE pending");
  assert.ok(oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").disabled);
  assert.ok(oneByData(app.root("curation-aux-footer"), "curation", "cancel-revert").disabled, "cancel locked while pending");
  app.pressKey("Escape");
  assert.ok(!app.root("curation-aux-modal").classList.contains("hidden"), "Escape can't close mid-revert");
  release();
  await waitFor(() => !app.curationState.busy, "revert failed");
  assert.ok(!oneByData(app.root("curation-aux-footer"), "curation", "cancel-revert").disabled, "cancel re-enabled");
  assert.ok(!oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").disabled);
  assert.ok(app.root("curation-aux-body").allText.includes("boom"), "error shown");
}

async function test_access_lost_during_save_preflight_sends_no_put() {
  let releasePreflight;
  let preflightSeen = false;
  let brafForbidden = false;
  const puts = [];
  const app = loadCurationApp({
    routes: async (method, url, reqBody) => {
      if (method === "POST" && url === "/v1/curation/reevaluate/jobs") {
        const id = reqBody.subject_key === "ALK" ? "job-a" : "job-b";
        return [200, { job_id: id, status_url: `/v1/curation/reevaluate/jobs/${id}` }];
      }
      if (url === "/v1/curation/reevaluate/jobs/job-a") {
        return [200, { status: "succeeded", error: null, invalid_pmids: null, draft: GENE_DRAFT }];
      }
      if (url === "/v1/curation/reevaluate/jobs/job-b") {
        return brafForbidden
          ? [403, { detail: "Curator privileges required" }]
          : [200, { status: "running", error: null, invalid_pmids: null, draft: null }];
      }
      if (method === "GET" && url.startsWith("/v1/curation/gene/ALK?")) {
        preflightSeen = true;
        await new Promise((resolve) => { releasePreflight = resolve; });
        return [200, { version: 0, override: null }];
      }
      if (method === "PUT") puts.push(reqBody);
      return method === "PUT" ? [200, { version: 1, override: { curator_email: "c", updated_at: "x", payload: reqBody.payload } }] : undefined;
    },
  });
  const result = geneResult({}, { annotation_tumor_types: { ALK: "LUAD", BRAF: "LUAD" } });
  result.annotations.push({ ...geneAnnotation(), gene: "BRAF" });
  render(app, result);
  // Another subject's re-evaluation job is polling in the background.
  app.openCurationDialog(app.geneCurationSubject(result.annotations[1]));
  footerButton(app, "submit").click();
  await waitFor(() => app.curationState.sessions.get("gene|BRAF|luad")?.job?.id === "job-b", "BRAF job");
  app.closeCurationDialog();

  await toDraft(app);
  typeInto(oneByData(body(app), "curation", "rationale"), "Typed before the preflight.");
  footerButton(app, "save").click();
  await waitFor(() => preflightSeen && releasePreflight, "preflight GET held");
  brafForbidden = true; // demoted server-side: the other job's poll sees 403
  await waitFor(() => app.state.auth.can_curate === false, "access lost via the poll");
  releasePreflight();
  await waitFor(() => !app.curationState.busy, "save finished");
  assert.strictEqual(puts.length, 0, "no PUT after access was lost mid-save");
  assert.ok(app.root("curation-modal").classList.contains("hidden"));
  assert.strictEqual(app.curationState.sessions.get("gene|ALK|luad").draft.rationale, "Typed before the preflight.");
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), []);
}

async function test_save_preflight_403_flips_to_read_only_and_keeps_the_draft() {
  const puts = [];
  let saving = false;
  const routes = jobRoutes();
  const app = loadCurationApp({
    routes: async (method, url, reqBody) => {
      if (method === "GET" && /^\/v1\/curation\/gene\/ALK\?/.test(url) && saving) {
        return [403, { detail: "Curator privileges required" }];
      }
      if (method === "PUT") puts.push(reqBody);
      return routes(method, url, reqBody);
    },
  });
  render(app, geneResult());
  await toDraft(app);
  typeInto(oneByData(body(app), "curation", "rationale"), "Typed before the 403 preflight.");
  saving = true;
  const before = app.requests.length;
  footerButton(app, "save").click();
  await waitFor(() => !app.curationState.busy && app.requests.slice(before).some((r) => r.method === "GET"), "preflight answered");
  assert.strictEqual(app.state.auth.can_curate, false, "a 403 on the preflight GET means access is gone");
  assert.ok(app.root("curation-modal").classList.contains("hidden"), "editor closes");
  assert.ok(noticeText(app).includes(ACCESS_LOST));
  assert.strictEqual(puts.length, 0);
  assert.deepStrictEqual(editControls(app.root("results-window")).map((c) => c.dataset.curation || c.tagName), []);
  assert.strictEqual(app.curationState.sessions.get("gene|ALK|luad").draft.rationale, "Typed before the 403 preflight.");
}

runTests("curation UI", [
  test_access_lost_during_save_preflight_sends_no_put,
  test_save_preflight_403_flips_to_read_only_and_keeps_the_draft,
  test_demotion_on_auth_refresh_removes_controls_and_blocks_writes,
  test_save_403_flips_to_read_only_and_keeps_the_draft,
  test_submit_and_revert_403_flip_to_read_only,
  test_editor_is_read_only_while_a_save_is_pending,
  test_revert_dialog_is_locked_while_delete_is_pending,
  test_viewer_from_auth_me_sees_no_edit_controls_anywhere,
  test_curator_from_auth_me_sees_edit_controls_everywhere,
  test_curated_and_draft_text_never_render_as_markup,
  test_viewer_sees_no_edit_controls,
  test_curator_sees_controls_even_when_sections_empty,
  test_pmid_add_remove_reorder_validation_and_cap,
  test_submit_job_notifies_polls_and_prefills_draft,
  test_invalid_pmids_render_inline,
  test_job_post_errors_are_explained,
  test_save_put_body_rerenders_in_place_and_toasts,
  test_save_409_offers_reload_and_resaves_with_current_version,
  test_save_422_shows_invalid_pmids,
  test_precedent_only_edit_saves_only_precedent,
  test_curated_badge_flags_history_and_revert,
  test_fusion_flow,
  test_escape_closes_and_draft_survives_reopen,
  test_save_and_revert_503_contention_show_retry_message,
  test_unresolved_tumor_context_disables_edit,
  test_evidence_only_edit_preserves_curated_precedent_verbatim,
  test_delayed_annotation_poll_cannot_undo_save,
  test_cross_tumor_context_draft_never_touches_displayed_annotation,
  test_lists_locked_while_submission_pending,
  test_failed_conflict_reload_keeps_expected_version,
  test_focus_trap_and_inert_background,
  test_revert_with_failed_reload_shows_pending_refresh,
  test_conflict_reload_with_failed_run_refresh_requires_reviewing_preview,
  test_conflict_with_revert_and_failed_refresh_keeps_version,
  test_save_restores_focus_to_rerendered_opener,
  test_focus_recovery_uses_control_id_for_quote_rows,
  test_retry_refresh_wording_matches_fresh_record,
]);

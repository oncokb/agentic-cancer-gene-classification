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

function jobRoutes({ draft = GENE_DRAFT, final, extra } = {}) {
  let polls = 0;
  return async (method, url, reqBody) => {
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
    [403, { detail: "Curator privileges required for this action." }, /curator privileges/],
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
}

async function test_save_put_body_rerenders_in_place_and_toasts() {
  let put;
  const app = loadCurationApp({
    routes: jobRoutes({
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

  oneByData(app.root("results-window"), "curation", "revert").click();
  assert.ok(app.root("curation-aux-body").allText.includes("Revert ALK to the pipeline output?"));
  assert.strictEqual(deleted, undefined, "nothing deleted before confirming");
  oneByData(app.root("curation-aux-footer"), "curation", "confirm-revert").click();
  await waitFor(() => app.root("curation-aux-modal").classList.contains("hidden"), "revert");
  assert.strictEqual(deleted, "/v1/curation/gene/ALK?tumor_type=LUAD&expected_version=2");
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

runTests("curation UI", [
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
]);

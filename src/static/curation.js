// Curator editing for gene annotations and fusion evidence: edit evidence /
// precedent PMIDs, re-evaluate them into a draft (POST
// /v1/curation/reevaluate/jobs), review and edit the draft, then save it as a
// curator override (PUT /v1/curation/{type}/{key}). Also renders the curated
// badge, curation quality flags, history and revert controls.
//
// Loaded before app.js; app.js's globals (`state`, `elements`, `sleep`,
// `setMessage`, `makePubMedLink`, `renderAnnotationResult`) are only used at
// call time. Edit sessions — including drafts and in-flight jobs — live in
// `curationState`, never in the rendered results, so re-renders and
// enrichment merges cannot wipe them. The server enforces the curator role;
// the client only hides controls from viewers.

const CURATION_MAX_PMIDS = 50;
const CURATION_EVIDENCE_TYPES = ["clinical", "preclinical", "case_report", "review", "other"];
const CURATION_SUBMITTED_MESSAGE =
  "Your changes have been submitted — re-evaluating rationale (this can take up to a minute)";
const CURATION_POLL = {
  initialDelayMs: 2000,
  maxDelayMs: 10000,
  backoff: 1.5,
  totalCapMs: 10 * 60 * 1000,
  maxConsecutiveErrors: 3,
};
const CURATION_TOAST_MS = 6000;
const CURATION_FLAG_CODES = new Set([
  "newer_pipeline_evidence",
  "curated_retracted_pmids_removed",
  "curation_unavailable",
]);
const CURATION_INVALID_REASONS = {
  unknown: "Not found in PubMed",
  retracted: "Retracted",
  no_abstract: "No abstract available",
  not_found_or_no_abstract: "Not found in PubMed, or has no abstract",
};
// Fields a saved override can change; kept from the local copy when a stale
// poll/enrichment response (an older curation_version) is merged over it.
const CURATION_GENE_KEYS = [
  "cancer_association_rationale", "citations", "evidence_cards", "supporting_quotes",
  "clinical_actionability", "insufficient_evidence", "quality_flags",
  "curated", "curated_by", "curated_at", "curation_version", "curated_fields",
];
const CURATION_FUSION_KEYS = [
  "interpretation", "well_supported", "pmids", "retrieved_count", "evidence_cards", "quality_flags",
  "curated", "curated_by", "curated_at", "curation_version", "curated_fields",
];

const curationState = {
  // subjectId -> edit session { subject, baseVersion, lists, include, invalid,
  // job, draft, step, note, message }.
  sessions: new Map(),
  // subjectId -> { version, revertedFrom } for overrides saved/reverted here.
  localVersions: new Map(),
  openSubjectId: null,
  returnFocus: null,
  busy: false,
  auxReturnFocus: null,
};

// ---------------------------------------------------------------------------
// Subjects
// ---------------------------------------------------------------------------

function canCurate() {
  return Boolean(state.auth && state.auth.can_curate);
}

function curationNormalizeTumorType(value) {
  return String(value || "").trim().toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

function geneCurationSubject(annotation) {
  const tumorTypes = state.currentResult?.annotation_tumor_types || {};
  return {
    type: "gene",
    key: annotation.gene,
    tumorType: tumorTypes[annotation.gene] || null,
    label: annotation.gene,
  };
}

function fusionCurationSubject(item) {
  return { type: "fusion", key: item.fusion, tumorType: item.tumor_type || null, label: item.fusion };
}

function curationSubjectId(subject) {
  return [subject.type, String(subject.key).toUpperCase(), curationNormalizeTumorType(subject.tumorType)].join("|");
}

function curationUrl(subject, suffix = "", params = {}) {
  const query = new URLSearchParams();
  if (subject.tumorType) query.set("tumor_type", subject.tumorType);
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null) query.set(key, String(value));
  });
  const qs = query.toString();
  return `/v1/curation/${subject.type}/${encodeURIComponent(subject.key)}${suffix}${qs ? `?${qs}` : ""}`;
}

function findCurationRecord(subject) {
  const result = state.currentResult;
  if (!result) return null;
  if (subject.type === "gene") {
    return (result.annotations || []).find((item) => item.gene === subject.key) || null;
  }
  const id = curationSubjectId(subject);
  return (result.fusion_evidence || []).find((item) => curationSubjectId(fusionCurationSubject(item)) === id) || null;
}

function replaceCurationRecord(subject, next) {
  const result = state.currentResult;
  if (!result) return;
  if (subject.type === "gene") {
    state.currentResult = {
      ...result,
      annotations: (result.annotations || []).map((item) => (item.gene === subject.key ? next : item)),
    };
    return;
  }
  const id = curationSubjectId(subject);
  state.currentResult = {
    ...result,
    fusion_evidence: (result.fusion_evidence || []).map((item) =>
      curationSubjectId(fusionCurationSubject(item)) === id ? next : item
    ),
  };
}

// Keeps locally saved/reverted curated content when a poll or enrichment
// response computed before that save (older curation_version) arrives late.
function reconcileCuratedRecord(subject, existing, incoming) {
  const local = curationState.localVersions.get(curationSubjectId(subject));
  if (!local || !existing || !incoming) return incoming;
  const version = incoming.curation_version || 0;
  const stale = version < local.version || (local.revertedFrom && version > 0 && version <= local.revertedFrom);
  if (!stale) return incoming;
  const merged = { ...incoming };
  (subject.type === "gene" ? CURATION_GENE_KEYS : CURATION_FUSION_KEYS).forEach((key) => {
    if (key in existing) merged[key] = existing[key];
    else delete merged[key];
  });
  return merged;
}

function reconcileFusionEvidence(existingList, incomingList) {
  const existingById = new Map(
    (existingList || []).map((item) => [curationSubjectId(fusionCurationSubject(item)), item])
  );
  return (incomingList || []).map((item) => {
    const subject = fusionCurationSubject(item);
    return reconcileCuratedRecord(subject, existingById.get(curationSubjectId(subject)), item);
  });
}

// ---------------------------------------------------------------------------
// Small DOM helpers
// ---------------------------------------------------------------------------

function curationEl(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.type) node.type = options.type;
  if (options.id) node.id = options.id;
  Object.entries(options.attrs || {}).forEach(([name, value]) => node.setAttribute(name, value));
  Object.entries(options.dataset || {}).forEach(([name, value]) => {
    node.dataset[name] = value;
  });
  if (options.onClick) node.addEventListener("click", options.onClick);
  children.filter(Boolean).forEach((child) => node.appendChild(child));
  return node;
}

function curationButton(text, onClick, { className = "secondary-action", action, label, disabled } = {}) {
  const button = curationEl("button", {
    className,
    text,
    type: "button",
    dataset: action ? { curation: action } : {},
    onClick,
  });
  if (label) button.setAttribute("aria-label", label);
  if (disabled) button.disabled = true;
  return button;
}

function curationById(id) {
  return document.getElementById(id);
}

function curationShow(node, visible) {
  if (node) node.classList.toggle("hidden", !visible);
}

function curationNormalizeText(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function isVerbatimQuote(quote, abstract) {
  const text = curationNormalizeText(quote);
  if (!text || !abstract) return true;
  return curationNormalizeText(abstract).includes(text);
}

async function curationResponseDetail(response) {
  const text = await response.text().catch(() => "");
  try {
    const payload = JSON.parse(text);
    return payload.detail !== undefined ? payload.detail : payload;
  } catch {
    return text || response.statusText || "";
  }
}

function curationDetailMessage(detail, fallback) {
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((item) => item.msg || JSON.stringify(item)).join("; ") || fallback;
  }
  return detail.message || fallback;
}

function formatCurationDate(value) {
  return value ? String(value).slice(0, 10) : "";
}

// ---------------------------------------------------------------------------
// Toasts and persistent job notifications
// ---------------------------------------------------------------------------

function showCurationToast(message, type = "info") {
  const region = curationById("toast-region");
  if (!region) return null;
  const toast = curationEl("div", { className: `toast toast-${type}`, text: message });
  region.appendChild(toast);
  setTimeout(() => {
    if (toast.remove) toast.remove();
  }, CURATION_TOAST_MS);
  return toast;
}

function curationJobStatusText(session) {
  const job = session.job;
  if (!job) return "";
  if (job.status === "succeeded") return `Draft ready for ${session.subject.label}.`;
  if (job.status === "failed") return `Re-evaluation failed for ${session.subject.label}: ${job.error || "unknown error"}`;
  return `${session.subject.label}: ${CURATION_SUBMITTED_MESSAGE}`;
}

function renderCurationNotification(session) {
  const container = curationById("curation-notifications");
  if (!container) return;
  const id = curationSubjectId(session.subject);
  const existing = Array.from(container.children || []).find((child) => child.dataset && child.dataset.subject === id);
  if (!session.job || session.job.dismissed) {
    if (existing) container.removeChild(existing);
    return;
  }
  const status = session.job.status;
  const item = curationEl("div", {
    className: `curation-notification curation-notification-${status}`,
    dataset: { subject: id },
  });
  item.appendChild(curationEl("p", { text: curationJobStatusText(session) }));
  const actions = curationEl("div", { className: "curation-notification-actions" });
  if (status === "succeeded") {
    actions.appendChild(
      curationButton("Review draft", () => openCurationDialog(session.subject, { step: "review" }), {
        className: "primary-action",
        action: "notification-review",
      })
    );
  } else {
    actions.appendChild(
      curationButton("Open editor", () => openCurationDialog(session.subject), { action: "notification-open" })
    );
  }
  if (status === "succeeded" || status === "failed") {
    actions.appendChild(
      curationButton(
        "Dismiss",
        () => {
          session.job.dismissed = true;
          renderCurationNotification(session);
        },
        { action: "notification-dismiss", label: `Dismiss ${session.subject.label} notification` }
      )
    );
  }
  item.appendChild(actions);
  if (existing) container.replaceChild(item, existing);
  else container.appendChild(item);
}

// ---------------------------------------------------------------------------
// Edit sessions
// ---------------------------------------------------------------------------

function curationPmidInfo(record) {
  const info = new Map();
  const add = (pmid, title, abstract) => {
    if (!pmid) return;
    const prev = info.get(pmid) || {};
    info.set(pmid, { title: prev.title || title || "", abstract: prev.abstract || abstract || "" });
  };
  (record?.evidence_cards || []).forEach((card) => add(card.pmid, card.title, card.abstract));
  (record?.clinical_actionability?.evidence || []).forEach((item) => add(item.pmid, item.title, item.abstract));
  return info;
}

function createCurationSession(subject, record, focus) {
  const isGene = subject.type === "gene";
  const evidence = isGene ? record?.citations || [] : record?.pmids || [];
  const precedent = isGene ? record?.clinical_actionability?.pmids || [] : [];
  const curatedFields = record?.curated_fields || [];
  return {
    subject,
    baseVersion: record?.curation_version || 0,
    lists: { evidence: [...evidence], precedent: [...precedent] },
    include: {
      evidence: focus !== "precedent" || curatedFields.includes("citations"),
      precedent: isGene && (focus === "precedent" || curatedFields.includes("therapeutic_precedent")),
    },
    info: curationPmidInfo(record),
    invalid: new Map(),
    inputErrors: { evidence: "", precedent: "" },
    job: null,
    draft: null,
    step: "pmids",
    note: "",
    message: null,
  };
}

function getCurationSession(subject, focus) {
  const id = curationSubjectId(subject);
  let session = curationState.sessions.get(id);
  if (!session) {
    session = createCurationSession(subject, findCurationRecord(subject), focus);
    curationState.sessions.set(id, session);
  } else if (!session.job && !session.draft) {
    // Reopening an untouched session from the other control widens it; once a
    // job or draft exists its sections stay fixed (the draft was built from them).
    if (focus === "precedent" && subject.type === "gene") session.include.precedent = true;
    if (focus === "evidence") session.include.evidence = true;
  }
  return session;
}

function discardCurationSession(session) {
  curationState.sessions.delete(curationSubjectId(session.subject));
  if (session.job) session.job.dismissed = true;
  renderCurationNotification(session);
}

function curationJobRunning(session) {
  return Boolean(session.job && (session.job.status === "queued" || session.job.status === "running"));
}

// Parses "123, 456 789" into PMIDs, validating format, duplicates and the cap.
// Returns { added, error }; mutates the session list only on success.
function addCurationPmids(session, listName, rawValue) {
  const values = String(rawValue || "").split(/[\s,;]+/).map((value) => value.trim()).filter(Boolean);
  if (!values.length) return { added: [], error: "Enter a PMID." };
  const list = session.lists[listName];
  const added = [];
  for (const value of values) {
    if (!/^[0-9]{1,9}$/.test(value) || /^0+$/.test(value)) {
      return { added: [], error: `"${value}" is not a valid PMID (use the numeric PubMed ID).` };
    }
    if (list.includes(value) || added.includes(value)) {
      return { added: [], error: `PMID ${value} is already in this list.` };
    }
    added.push(value);
  }
  if (list.length + added.length > CURATION_MAX_PMIDS) {
    return { added: [], error: `At most ${CURATION_MAX_PMIDS} PMIDs per list.` };
  }
  const other = session.lists[listName === "evidence" ? "precedent" : "evidence"];
  const combined = new Set([...list, ...added, ...other]);
  if (combined.size > CURATION_MAX_PMIDS) {
    return { added: [], error: `At most ${CURATION_MAX_PMIDS} unique PMIDs across evidence and precedent.` };
  }
  list.push(...added);
  session.include[listName] = true;
  curationListsChanged(session);
  return { added, error: "" };
}

function removeCurationPmid(session, listName, pmid) {
  session.lists[listName] = session.lists[listName].filter((value) => value !== pmid);
  session.include[listName] = true;
  session.invalid.delete(pmid);
  curationListsChanged(session);
}

function moveCurationPmid(session, listName, pmid, delta) {
  const list = session.lists[listName];
  const from = list.indexOf(pmid);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= list.length) return;
  list.splice(from, 1);
  list.splice(to, 0, pmid);
  session.include[listName] = true;
  curationListsChanged(session);
}

// Any PMID change invalidates a finished draft (it was built from the old
// lists); editing is disabled while a job is running.
function curationListsChanged(session) {
  if (session.job && !curationJobRunning(session)) {
    session.job.dismissed = true;
    renderCurationNotification(session);
    session.job = null;
  }
  session.draft = null;
}

// ---------------------------------------------------------------------------
// Re-evaluation jobs
// ---------------------------------------------------------------------------

function curationJobBody(session) {
  const { subject } = session;
  return {
    subject_type: subject.type,
    subject_key: subject.key,
    tumor_type: subject.tumorType || null,
    evidence_pmids: session.include.evidence ? [...session.lists.evidence] : [],
    precedent_pmids: session.include.precedent ? [...session.lists.precedent] : [],
  };
}

function curationJobErrorMessage(status, detail) {
  if (status === 429) {
    return "You already have the maximum number of re-evaluations running. Wait for one to finish, then resubmit.";
  }
  if (status === 403) return "Your account doesn't have curator privileges, so it can't edit curated evidence.";
  if (status === 401) return "Your session has expired. Sign in again to continue.";
  if (status === 422) {
    return `The PMID lists were rejected: ${curationDetailMessage(detail, "check the PMIDs and the 50-PMID limit")}.`;
  }
  return curationDetailMessage(detail, "Couldn't start re-evaluation; please retry.");
}

async function submitCurationReevaluation(session) {
  if (curationState.busy || curationJobRunning(session)) return;
  const body = curationJobBody(session);
  session.invalid = new Map();
  if (!body.evidence_pmids.length && !body.precedent_pmids.length) {
    // Nothing to re-evaluate (e.g. clearing the precedent): edit directly.
    session.draft = curationDraftFromRecord(session);
    session.step = "review";
    session.message = null;
    renderCurationDialog();
    return;
  }
  curationState.busy = true;
  session.message = null;
  renderCurationDialog();
  try {
    const response = await fetch("/v1/curation/reevaluate/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await curationResponseDetail(response);
      session.message = { type: "error", text: curationJobErrorMessage(response.status, detail) };
      return;
    }
    const created = await response.json();
    session.job = { id: created.job_id, statusUrl: created.status_url, status: "queued", error: null };
    session.draft = null;
    showCurationToast(`${session.subject.label}: ${CURATION_SUBMITTED_MESSAGE}`);
    renderCurationNotification(session);
    pollCurationJob(session, session.job);
  } catch (error) {
    session.message = { type: "error", text: `Couldn't start re-evaluation: ${error.message}` };
  } finally {
    curationState.busy = false;
    renderCurationDialog();
  }
}

function curationJobIsCurrent(session, job) {
  return curationState.sessions.get(curationSubjectId(session.subject)) === session && session.job === job;
}

async function pollCurationJob(session, job) {
  let delay = CURATION_POLL.initialDelayMs;
  let waited = 0;
  let errors = 0;
  while (curationJobIsCurrent(session, job)) {
    await sleep(delay);
    waited += delay;
    delay = Math.min(CURATION_POLL.maxDelayMs, Math.max(1, delay * CURATION_POLL.backoff));
    if (!curationJobIsCurrent(session, job)) return;
    let status;
    try {
      const response = await fetch(job.statusUrl);
      if (response.status === 404) {
        finishCurationJob(session, job, {
          status: "failed",
          error: "The re-evaluation job was not found (it may have expired or the server restarted). Resubmit.",
        });
        return;
      }
      if (!response.ok) {
        const detail = await curationResponseDetail(response);
        if (response.status === 401 || response.status === 403) {
          finishCurationJob(session, job, { status: "failed", error: curationJobErrorMessage(response.status, detail) });
          return;
        }
        throw new Error(curationDetailMessage(detail, response.statusText || "status check failed"));
      }
      status = await response.json();
      errors = 0;
    } catch (error) {
      errors += 1;
      if (errors >= CURATION_POLL.maxConsecutiveErrors) {
        finishCurationJob(session, job, { status: "failed", error: `Lost contact with the re-evaluation job: ${error.message}` });
        return;
      }
      continue;
    }
    if (status.status === "succeeded" || status.status === "failed") {
      finishCurationJob(session, job, status);
      return;
    }
    if (status.status !== job.status) {
      job.status = status.status;
      renderCurationNotification(session);
      if (curationState.openSubjectId === curationSubjectId(session.subject)) renderCurationDialog();
    }
    if (waited >= CURATION_POLL.totalCapMs) {
      finishCurationJob(session, job, {
        status: "failed",
        error: "Re-evaluation is taking much longer than expected. Resubmit to try again.",
      });
      return;
    }
  }
}

function finishCurationJob(session, job, status) {
  if (!curationJobIsCurrent(session, job)) return;
  job.status = status.status;
  job.error = status.error || null;
  if (status.status === "succeeded") {
    session.draft = curationDraftFromJob(session, status.draft || {});
    showCurationToast(`Draft ready for ${session.subject.label} — review and save it.`, "success");
  } else {
    session.invalid = curationInvalidFromJob(status.invalid_pmids);
    session.step = "pmids";
    showCurationToast(`Re-evaluation failed for ${session.subject.label}.`, "error");
  }
  renderCurationNotification(session);
  if (curationState.openSubjectId === curationSubjectId(session.subject)) renderCurationDialog();
}

// Job failures report {unknown: [...], retracted: [...], no_abstract: [...]};
// a PMID can appear under more than one reason.
function curationInvalidFromJob(invalid) {
  const map = new Map();
  Object.entries(invalid || {}).forEach(([reason, pmids]) => {
    (pmids || []).forEach((pmid) => {
      const label = CURATION_INVALID_REASONS[reason] || reason;
      map.set(pmid, map.has(pmid) ? `${map.get(pmid)}; ${label}` : label);
    });
  });
  return map;
}

// PUT 422 reports [{pmid, reason}].
function curationInvalidFromSave(invalid) {
  const map = new Map();
  (invalid || []).forEach((item) => map.set(item.pmid, CURATION_INVALID_REASONS[item.reason] || item.reason));
  return map;
}

// ---------------------------------------------------------------------------
// Drafts (editable copies; never stored in the rendered DOM)
// ---------------------------------------------------------------------------

function curationEvidenceType(value) {
  return CURATION_EVIDENCE_TYPES.includes(value) ? value : "other";
}

function curationDraftCard(card) {
  return {
    pmid: card.pmid,
    title: card.title || "",
    journal: card.journal || "",
    abstract: card.abstract || "",
    is_preprint: Boolean(card.is_preprint),
    selected_reason: card.selected_reason || "",
    quote: card.quote || "",
    evidence_type: curationEvidenceType(card.evidence_type),
  };
}

function curationDraftPrecedent(actionability) {
  return {
    summary: actionability?.summary || "",
    items: (actionability?.evidence || []).map((item) => ({
      pmid: item.pmid,
      title: item.title || "",
      abstract: item.abstract || "",
      quote: item.quote || "",
      therapies: (item.therapies || []).join(", "),
      evidence_type: curationEvidenceType(item.evidence_type),
    })),
  };
}

function curationOrderCards(cards, pmids) {
  const byPmid = new Map(cards.map((card) => [card.pmid, card]));
  return pmids.map((pmid) => byPmid.get(pmid) || curationDraftCard({ pmid }));
}

function curationDraftFromJob(session, draft) {
  const isGene = session.subject.type === "gene";
  const cards = curationOrderCards((draft.evidence_cards || []).map(curationDraftCard), session.lists.evidence);
  cards.forEach((card) => session.info.set(card.pmid, { title: card.title, abstract: card.abstract }));
  const precedent = curationDraftPrecedent(draft.clinical_actionability);
  precedent.items = session.lists.precedent.map(
    (pmid) => precedent.items.find((item) => item.pmid === pmid) ||
      { pmid, title: "", abstract: "", quote: "", therapies: "", evidence_type: "other" }
  );
  precedent.items.forEach((item) => session.info.set(item.pmid, { title: item.title, abstract: item.abstract }));
  return {
    rationale: isGene ? draft.cancer_association_rationale || "" : draft.interpretation || "",
    wellSupported: Boolean(draft.well_supported),
    cards,
    quotes: (draft.supporting_quotes || []).map((quote) => ({ pmid: quote.pmid, quote: quote.quote || "" })),
    precedent,
  };
}

function curationDraftFromRecord(session) {
  const record = findCurationRecord(session.subject) || {};
  const isGene = session.subject.type === "gene";
  return {
    rationale: isGene ? record.cancer_association_rationale || "" : record.interpretation || "",
    wellSupported: Boolean(record.well_supported),
    cards: [],
    quotes: [],
    precedent: { summary: "", items: [] },
  };
}

function buildCurationPayload(session) {
  const { draft, include, subject } = session;
  const cards = draft.cards.map((card) => ({
    pmid: card.pmid,
    evidence_type: curationEvidenceType(card.evidence_type),
    selected_reason: card.selected_reason.trim(),
    quote: card.quote.trim() || null,
  }));
  const pmids = session.lists.evidence.slice();
  if (subject.type === "fusion") {
    return {
      interpretation: draft.rationale.trim(),
      well_supported: Boolean(draft.wellSupported),
      pmids,
      evidence_cards: cards,
    };
  }
  const payload = {};
  if (include.evidence) {
    payload.cancer_association_rationale = draft.rationale.trim();
    payload.citations = pmids;
    payload.evidence_cards = cards;
    payload.supporting_quotes = draft.quotes
      .filter((quote) => quote.pmid && quote.quote.trim())
      .map((quote) => ({ pmid: quote.pmid, quote: quote.quote.trim() }));
  }
  if (include.precedent) {
    payload.therapeutic_precedent = {
      summary: draft.precedent.summary.trim(),
      precedents: draft.precedent.items.map((item) => ({
        pmid: item.pmid,
        quote: item.quote.trim() || null,
        therapies: item.therapies.split(",").map((therapy) => therapy.trim()).filter(Boolean),
        evidence_type: curationEvidenceType(item.evidence_type),
      })),
    };
  }
  return payload;
}

// ---------------------------------------------------------------------------
// Save / revert / reload
// ---------------------------------------------------------------------------

function curatedRecordFromOverride(subject, record, response) {
  const override = response.override || {};
  const payload = override.payload || {};
  const next = { ...record };
  const dropFlags = new Set(["newer_pipeline_evidence", "curated_retracted_pmids_removed"]);
  if (subject.type === "gene") {
    if (payload.cancer_association_rationale !== undefined) {
      next.cancer_association_rationale = payload.cancer_association_rationale;
    }
    if (payload.citations) {
      next.citations = payload.citations;
      next.insufficient_evidence = !payload.citations.length;
      if (payload.citations.length) dropFlags.add("no_verified_citations");
    }
    if (payload.evidence_cards) next.evidence_cards = payload.evidence_cards;
    if (payload.supporting_quotes) next.supporting_quotes = payload.supporting_quotes;
    if (payload.therapeutic_precedent) {
      const precedent = payload.therapeutic_precedent;
      const items = precedent.precedents || [];
      next.clinical_actionability = items.length
        ? {
            confidence_score: 1,
            confidence_level: "high",
            summary: precedent.summary || "Curator-verified therapeutic precedent.",
            confidence_explanation:
              `Curator-verified by ${override.curator_email} on ${formatCurationDate(override.updated_at)}; ` +
              "replaces the automated literature-keyword score.",
            pmids: items.map((item) => item.pmid),
            score_components: [],
            evidence: items.map((item) => ({ ...item })),
            curated: true,
            curated_by: override.curator_email,
            curated_at: override.updated_at,
          }
        : null;
    }
  } else {
    if (payload.interpretation !== undefined) next.interpretation = payload.interpretation;
    if (payload.well_supported !== undefined) next.well_supported = payload.well_supported;
    if (payload.pmids) {
      next.pmids = payload.pmids;
      next.retrieved_count = payload.pmids.length;
    }
    if (payload.evidence_cards) next.evidence_cards = payload.evidence_cards.map((card) => ({ ...card, fusion: record.fusion }));
  }
  next.curated = true;
  next.curated_by = override.curator_email;
  next.curated_at = override.updated_at;
  next.curation_version = response.version;
  next.curated_fields = Object.keys(payload);
  next.quality_flags = (record.quality_flags || []).filter((flag) => !dropFlags.has(flag.code));
  return next;
}

async function saveCurationDraft(session) {
  if (curationState.busy || !session.draft) return;
  const body = { expected_version: session.baseVersion || 0, payload: buildCurationPayload(session) };
  const note = session.note.trim();
  if (note) body.note = note;
  curationState.busy = true;
  session.message = { type: "info", text: "Saving curator edits…" };
  renderCurationDialog();
  try {
    const response = await fetch(curationUrl(session.subject), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await curationResponseDetail(response);
      handleCurationSaveError(session, response.status, detail);
      return;
    }
    const saved = await response.json();
    const record = findCurationRecord(session.subject);
    if (record) {
      replaceCurationRecord(session.subject, curatedRecordFromOverride(session.subject, record, saved));
    }
    curationState.localVersions.set(curationSubjectId(session.subject), { version: saved.version, revertedFrom: 0 });
    curationState.busy = false;
    discardCurationSession(session);
    closeCurationDialog();
    if (state.currentResult) renderAnnotationResult(state.currentResult);
    showCurationToast("Curator edits saved", "success");
  } catch (error) {
    session.message = { type: "error", text: `Couldn't save: ${error.message}` };
  } finally {
    curationState.busy = false;
    if (curationState.openSubjectId) renderCurationDialog();
  }
}

function handleCurationSaveError(session, status, detail) {
  if (status === 409) {
    const current = detail && detail.current_version;
    session.conflictVersion = current;
    session.message = {
      type: "error",
      text: `Another curator saved changes${current ? ` (now v${current})` : ""} since you started editing.`,
      conflict: true,
    };
    return;
  }
  if (status === 422 && detail && Array.isArray(detail.invalid_pmids)) {
    session.invalid = curationInvalidFromSave(detail.invalid_pmids);
    session.message = {
      type: "error",
      text: `${detail.message || "Some PMIDs cannot be used as curated evidence."} ` +
        Array.from(session.invalid, ([pmid, reason]) => `PMID ${pmid}: ${reason}`).join("; "),
    };
    return;
  }
  if (status === 403) {
    session.message = { type: "error", text: curationJobErrorMessage(403) };
    return;
  }
  session.message = { type: "error", text: curationDetailMessage(detail, "Couldn't save curator edits; please retry.") };
}

// Re-reads the subject from the saved run (the server overlays the live
// override on every read). Returns the fresh record, or null.
async function reloadCurationSubject(subject) {
  const runId = state.currentResult?.run_id;
  if (!runId) return null;
  const response = await fetch(`/v1/annotate/${encodeURIComponent(runId)}`);
  if (!response.ok) return null;
  const result = await response.json();
  const id = curationSubjectId(subject);
  const fresh = subject.type === "gene"
    ? (result.annotations || []).find((item) => item.gene === subject.key)
    : (result.fusion_evidence || []).find((item) => curationSubjectId(fusionCurationSubject(item)) === id);
  if (!fresh) return null;
  replaceCurationRecord(subject, fresh);
  return fresh;
}

async function reloadAfterCurationConflict(session) {
  if (curationState.busy) return;
  curationState.busy = true;
  try {
    const fresh = await reloadCurationSubject(session.subject);
    if (fresh) {
      session.baseVersion = fresh.curation_version || 0;
      if (state.currentResult) renderAnnotationResult(state.currentResult);
    } else if (session.conflictVersion) {
      session.baseVersion = session.conflictVersion;
    }
    session.message = {
      type: "info",
      text: `Reloaded the latest version (v${session.baseVersion}). Your draft is kept — review it and save again to replace that version.`,
    };
  } catch (error) {
    session.message = { type: "error", text: `Couldn't reload: ${error.message}` };
  } finally {
    curationState.busy = false;
    renderCurationDialog();
  }
}

function confirmCurationRevert(subject) {
  const record = findCurationRecord(subject);
  const version = record?.curation_version || 0;
  const body = curationEl("div", {}, [
    curationEl("p", {
      text: `Revert ${subject.label} to the pipeline output? This removes the curator edits (v${version}); the revert is recorded in history.`,
    }),
  ]);
  const error = curationEl("p", { className: "curation-error", attrs: { role: "alert" } });
  body.appendChild(error);
  const confirmButton = curationButton("Revert to pipeline", null, { className: "danger-action", action: "confirm-revert" });
  confirmButton.addEventListener("click", () => revertCuration(subject, version, error, confirmButton));
  openCurationAuxModal(`Revert ${subject.label}`, body, [
    curationButton("Cancel", closeCurationAuxModal, { action: "cancel-revert" }),
    confirmButton,
  ]);
}

async function revertCuration(subject, version, errorNode, button) {
  if (curationState.busy) return;
  curationState.busy = true;
  button.disabled = true;
  try {
    const response = await fetch(curationUrl(subject, "", { expected_version: version || undefined }), { method: "DELETE" });
    if (!response.ok && response.status !== 404) {
      const detail = await curationResponseDetail(response);
      if (response.status === 409) {
        errorNode.textContent =
          `Another curator saved changes${detail?.current_version ? ` (now v${detail.current_version})` : ""}. ` +
          "Close this and reload the results before reverting.";
      } else if (response.status === 403) {
        errorNode.textContent = curationJobErrorMessage(403);
      } else {
        errorNode.textContent = curationDetailMessage(detail, "Couldn't revert; please retry.");
      }
      return;
    }
    curationState.localVersions.set(curationSubjectId(subject), { version: 0, revertedFrom: version });
    const fresh = await reloadCurationSubject(subject).catch(() => null);
    if (!fresh) {
      const record = findCurationRecord(subject);
      if (record) {
        replaceCurationRecord(subject, {
          ...record, curated: false, curated_by: null, curated_at: null, curation_version: null, curated_fields: [],
        });
      }
      setMessage(`${subject.label} reverted. Re-run the annotation to see the full pipeline output.`, "info");
    }
    curationState.busy = false;
    closeCurationAuxModal();
    if (state.currentResult) renderAnnotationResult(state.currentResult);
    showCurationToast(`${subject.label} reverted to pipeline output`, "success");
  } catch (error) {
    errorNode.textContent = `Couldn't revert: ${error.message}`;
  } finally {
    curationState.busy = false;
    button.disabled = false;
  }
}

async function showCurationHistory(subject) {
  const body = curationEl("div", { className: "curation-history", attrs: { role: "status" } }, [
    curationEl("p", { className: "subtle", text: "Loading history…" }),
  ]);
  openCurationAuxModal(`${subject.label} curation history`, body, [
    curationButton("Close", closeCurationAuxModal, { className: "primary-action", action: "close-history" }),
  ]);
  try {
    const response = await fetch(curationUrl(subject, "/history"));
    if (!response.ok) {
      const detail = await curationResponseDetail(response);
      body.replaceChildren(curationEl("p", { className: "curation-error", text: curationDetailMessage(detail, "Couldn't load history.") }));
      return;
    }
    const history = await response.json();
    const entries = history.entries || [];
    if (!entries.length) {
      body.replaceChildren(curationEl("p", { className: "subtle", text: "No curator edits yet." }));
      return;
    }
    const list = curationEl("ol", { className: "curation-history-list" });
    entries.forEach((entry) => {
      const payload = entry.payload || {};
      const pmids = payload.citations || payload.pmids || [];
      const precedent = (payload.therapeutic_precedent?.precedents || []).map((item) => item.pmid);
      const lines = [
        curationEl("strong", {
          text: `v${entry.version} · ${entry.action === "revert" ? "Reverted to pipeline" : "Saved"}`,
        }),
        curationEl("span", { className: "subtle", text: ` by ${entry.curator_email} · ${formatCurationDate(entry.created_at)}` }),
      ];
      if (entry.action === "save") {
        lines.push(curationEl("div", { className: "subtle", text: `Sections: ${Object.keys(payload).join(", ") || "—"}` }));
        if (pmids.length) lines.push(curationEl("div", { className: "subtle", text: `Evidence PMIDs: ${pmids.join(", ")}` }));
        if (precedent.length) lines.push(curationEl("div", { className: "subtle", text: `Precedent PMIDs: ${precedent.join(", ")}` }));
      }
      if (entry.note) lines.push(curationEl("div", { text: `Note: ${entry.note}` }));
      list.appendChild(curationEl("li", {}, lines));
    });
    body.replaceChildren(list);
  } catch (error) {
    body.replaceChildren(curationEl("p", { className: "curation-error", text: `Couldn't load history: ${error.message}` }));
  }
}

// ---------------------------------------------------------------------------
// Result-card controls (called from app.js renderers)
// ---------------------------------------------------------------------------

function curationEditButton(subject, focus, text) {
  if (!canCurate()) return null;
  const resuming = curationState.sessions.has(curationSubjectId(subject));
  const button = curationButton(
    resuming ? `${text} (draft in progress)` : text,
    () => openCurationDialog(subject, { focus }),
    {
      className: "secondary-action curation-edit-button",
      action: focus === "precedent" ? "edit-precedent" : "edit-evidence",
      label: `${text} for ${subject.label}`,
    }
  );
  button.dataset.curationSubject = curationSubjectId(subject);
  return button;
}

function curationNewerPmids(flag) {
  return (String(flag.detail || "").match(/\b\d{1,9}\b/g) || []);
}

// Curated badge, curation flags, history and revert controls for one subject.
// Returns null when there's nothing to show (uncurated, no curation flags).
function renderCurationPanel(subject, record) {
  const flags = (record?.quality_flags || []).filter((flag) => CURATION_FLAG_CODES.has(flag.code));
  if (!record?.curated && !flags.length) return null;
  const panel = curationEl("div", { className: "curation-panel", dataset: { curationPanel: subject.type } });

  if (record.curated) {
    const row = curationEl("div", { className: "curation-panel-row" });
    row.appendChild(curationEl("span", {
      className: "review-badge curated-badge",
      text: `Curated by ${record.curated_by || "a curator"} · ${formatCurationDate(record.curated_at)} (v${record.curation_version || 1})`,
      dataset: { curation: "curated-badge" },
    }));
    row.appendChild(curationButton("View history", () => showCurationHistory(subject), {
      className: "text-button", action: "view-history", label: `View curation history for ${subject.label}`,
    }));
    if (canCurate()) {
      row.appendChild(curationButton("Revert to pipeline", () => confirmCurationRevert(subject), {
        className: "text-button danger-link", action: "revert", label: `Revert ${subject.label} to pipeline output`,
      }));
    }
    panel.appendChild(row);
  }

  flags.forEach((flag) => {
    const severity = flag.severity === "critical" ? "critical" : "warning";
    const notice = curationEl("div", {
      className: `curation-flag curation-flag-${severity}`,
      attrs: { role: flag.code === "curation_unavailable" ? "alert" : "note" },
      dataset: { flag: flag.code },
    });
    notice.appendChild(curationEl("strong", { text: flag.label || flag.code }));
    if (flag.code === "newer_pipeline_evidence") {
      const pmids = curationNewerPmids(flag);
      notice.appendChild(curationEl("span", { text: " The pipeline now also cites: " }));
      const links = curationEl("span", { className: "citation-link-list" });
      pmids.forEach((pmid) => links.appendChild(makePubMedLink(pmid)));
      notice.appendChild(links);
      const edit = curationEditButton(subject, "evidence", "Edit evidence");
      if (edit) notice.appendChild(edit);
    } else if (flag.detail) {
      notice.appendChild(curationEl("span", { text: ` ${flag.detail}` }));
    }
    panel.appendChild(notice);
  });
  return panel;
}

function renderCurationUnavailableBanner(result) {
  if (!result?.curation_unavailable) return null;
  return curationEl("div", {
    className: "curation-flag curation-flag-critical curation-unavailable-banner",
    attrs: { role: "alert" },
    dataset: { flag: "curation_unavailable_banner" },
  }, [
    curationEl("strong", { text: "Curator edits unavailable." }),
    curationEl("span", { text: " Curator edits couldn't be loaded, so some results show pipeline output only." }),
  ]);
}

// ---------------------------------------------------------------------------
// Edit dialog
// ---------------------------------------------------------------------------

function openCurationDialog(subject, { focus = "evidence", step } = {}) {
  if (!canCurate()) return;
  const session = getCurationSession(subject, focus);
  if (step === "review" && session.draft) session.step = "review";
  if (!curationState.openSubjectId) curationState.returnFocus = document.activeElement || null;
  curationState.openSubjectId = curationSubjectId(subject);
  session.focus = focus;
  curationShow(curationById("curation-modal"), true);
  renderCurationDialog({ focusFirst: true });
}

function closeCurationDialog() {
  if (curationState.busy) return;
  const subjectId = curationState.openSubjectId;
  curationState.openSubjectId = null;
  curationShow(curationById("curation-modal"), false);
  let target = curationState.returnFocus;
  curationState.returnFocus = null;
  if (!target || target.isConnected === false) {
    // The opener was replaced by a re-render: focus its replacement instead.
    target = Array.from(document.querySelectorAll(".curation-edit-button")).find(
      (button) => button.dataset.curationSubject === subjectId
    );
  }
  if (target && target.focus) target.focus();
}

function openCuratedSession() {
  return curationState.openSubjectId ? curationState.sessions.get(curationState.openSubjectId) || null : null;
}

function renderCurationDialog({ focusFirst = false } = {}) {
  const session = openCuratedSession();
  if (!session) return;
  const isReview = session.step === "review" && session.draft;
  const title = curationById("curation-title");
  const subtitle = curationById("curation-subtitle");
  const statusNode = curationById("curation-status");
  const bodyNode = curationById("curation-body");
  const footer = curationById("curation-footer");
  const subjectText = `${session.subject.label}${session.subject.tumorType ? ` · ${session.subject.tumorType}` : ""}`;
  title.textContent = isReview ? `Review draft — ${session.subject.label}` : `Edit evidence — ${session.subject.label}`;
  subtitle.textContent = isReview
    ? `Edit the suggested text, then save it as the curated version of ${subjectText}.`
    : `Choose the PMIDs for ${subjectText}, then submit them for re-evaluation.`;

  renderCurationStatus(session, statusNode);
  bodyNode.replaceChildren(isReview ? renderCurationReview(session) : renderCurationPmidStep(session));
  footer.replaceChildren(...renderCurationFooter(session, isReview));

  if (focusFirst) {
    const preferred = bodyNode.querySelector(
      isReview ? "input, textarea, select" : `[data-curation='${session.focus === "precedent" ? "precedent" : "evidence"}-input']`
    );
    if (preferred && preferred.focus) preferred.focus();
  }
}

function renderCurationStatus(session, node) {
  const parts = [];
  const job = session.job;
  if (job && curationJobRunning(session)) {
    parts.push(curationEl("p", { className: "curation-progress", text: CURATION_SUBMITTED_MESSAGE }));
  } else if (job && job.status === "failed") {
    parts.push(curationEl("p", { className: "curation-error", text: `Re-evaluation failed: ${job.error || "unknown error"}` }));
  } else if (job && job.status === "succeeded" && session.step !== "review") {
    parts.push(curationEl("p", { text: "The draft is ready. Review it before saving." }));
  }
  if (session.message) {
    parts.push(curationEl("p", {
      className: session.message.type === "error" ? "curation-error" : "",
      text: session.message.text,
      dataset: { curation: "message" },
    }));
    if (session.message.conflict) {
      parts.push(curationButton("Reload latest version", () => reloadAfterCurationConflict(session), {
        action: "reload-conflict",
      }));
    }
  }
  node.replaceChildren(...parts);
  curationShow(node, parts.length > 0);
}

function renderCurationFooter(session, isReview) {
  const running = curationJobRunning(session);
  const buttons = [
    curationButton("Discard edits", () => {
      if (curationState.busy) return;
      discardCurationSession(session);
      closeCurationDialog();
      if (state.currentResult) renderAnnotationResult(state.currentResult);
    }, { action: "discard", disabled: curationState.busy }),
  ];
  if (isReview) {
    buttons.push(curationButton("Back to PMIDs", () => {
      session.step = "pmids";
      renderCurationDialog();
    }, { action: "back", disabled: curationState.busy }));
    buttons.push(curationButton("Save curator edits", () => saveCurationDraft(session), {
      className: "primary-action", action: "save", disabled: curationState.busy,
    }));
    return buttons;
  }
  if (session.draft && !running) {
    buttons.push(curationButton("Review draft", () => {
      session.step = "review";
      renderCurationDialog({ focusFirst: true });
    }, { className: "primary-action", action: "review" }));
    return buttons;
  }
  buttons.push(curationButton(running ? "Re-evaluating…" : "Submit for re-evaluation", () => submitCurationReevaluation(session), {
    className: "primary-action", action: "submit", disabled: running || curationState.busy,
  }));
  return buttons;
}

function renderCurationPmidStep(session) {
  const container = curationEl("div", { className: "curation-pmid-step" });
  container.appendChild(renderCurationPmidList(session, "evidence"));
  if (session.subject.type === "gene") container.appendChild(renderCurationPmidList(session, "precedent"));
  container.appendChild(curationEl("p", {
    className: "subtle",
    text: `Up to ${CURATION_MAX_PMIDS} PMIDs in total. Submitting runs the selected papers through re-evaluation; nothing is saved until you review the draft.`,
  }));
  return container;
}

function renderCurationPmidList(session, listName) {
  const isEvidence = listName === "evidence";
  const running = curationJobRunning(session);
  const legendText = isEvidence
    ? (session.subject.type === "gene" ? "Evidence PMIDs (Cited on PubMed)" : "Fusion evidence PMIDs")
    : "Therapeutic precedent PMIDs";
  const fieldset = curationEl("fieldset", { className: "curation-pmid-list", dataset: { list: listName } });
  fieldset.appendChild(curationEl("legend", { text: legendText }));

  if (session.subject.type === "gene") {
    const includeId = `curation-include-${listName}`;
    const include = curationEl("input", { type: "checkbox", id: includeId, dataset: { curation: `${listName}-include` } });
    include.checked = Boolean(session.include[listName]);
    include.disabled = running;
    include.addEventListener("change", () => {
      session.include[listName] = include.checked;
      curationListsChanged(session);
      renderCurationDialog();
    });
    fieldset.appendChild(curationEl("label", { className: "curation-include", attrs: { for: includeId } }, [
      include,
      curationEl("span", {
        text: isEvidence
          ? " Include evidence (rationale, cited PMIDs, evidence cards) in this edit"
          : " Include therapeutic precedent in this edit",
      }),
    ]));
  }

  const list = session.lists[listName];
  fieldset.appendChild(curationEl("p", { className: "subtle", text: `${list.length} / ${CURATION_MAX_PMIDS} PMIDs` }));
  const rows = curationEl("ol", { className: "curation-pmid-rows" });
  if (!list.length) {
    rows.appendChild(curationEl("li", {
      className: "subtle",
      text: isEvidence ? "No evidence PMIDs yet." : "No precedent PMIDs (saving an empty list clears the precedent).",
    }));
  }
  list.forEach((pmid, index) => {
    const info = session.info.get(pmid) || {};
    const row = curationEl("li", { className: "curation-pmid-row", dataset: { pmid } });
    const pill = makePubMedLink(pmid, info.abstract);
    if (info.title && pill.setAttribute) pill.setAttribute("title", info.title);
    row.appendChild(pill);
    if (info.title) row.appendChild(curationEl("span", { className: "curation-pmid-title", text: info.title }));
    const reason = session.invalid.get(pmid);
    if (reason) {
      row.classList.add("invalid");
      row.appendChild(curationEl("span", {
        className: "curation-error curation-pmid-reason",
        text: reason,
        dataset: { curation: "invalid-reason" },
      }));
    }
    const controls = curationEl("span", { className: "curation-pmid-controls" });
    controls.appendChild(curationButton("↑", () => { moveCurationPmid(session, listName, pmid, -1); renderCurationDialog(); }, {
      className: "icon-button small", action: "move-up", label: `Move PMID ${pmid} up`, disabled: running || index === 0,
    }));
    controls.appendChild(curationButton("↓", () => { moveCurationPmid(session, listName, pmid, 1); renderCurationDialog(); }, {
      className: "icon-button small", action: "move-down", label: `Move PMID ${pmid} down`,
      disabled: running || index === list.length - 1,
    }));
    controls.appendChild(curationButton("Remove", () => { removeCurationPmid(session, listName, pmid); renderCurationDialog(); }, {
      className: "text-button danger-link", action: "remove-pmid", label: `Remove PMID ${pmid}`, disabled: running,
    }));
    row.appendChild(controls);
    rows.appendChild(row);
  });
  fieldset.appendChild(rows);

  const inputId = `curation-add-${listName}`;
  const errorId = `curation-add-${listName}-error`;
  const input = curationEl("input", {
    id: inputId,
    attrs: { inputmode: "numeric", autocomplete: "off", placeholder: "e.g. 12345678", "aria-describedby": errorId },
    dataset: { curation: `${listName}-input` },
  });
  input.disabled = running;
  const error = curationEl("p", {
    id: errorId,
    className: "curation-error",
    text: session.inputErrors[listName] || "",
    attrs: { role: "alert" },
    dataset: { curation: `${listName}-error` },
  });
  const add = () => {
    const result = addCurationPmids(session, listName, input.value);
    session.inputErrors[listName] = result.error;
    renderCurationDialog();
    const again = curationById("curation-body").querySelector(`[data-curation='${listName}-input']`);
    if (again) {
      if (result.error) again.value = input.value;
      if (again.focus) again.focus();
    }
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      add();
    }
  });
  const addRow = curationEl("div", { className: "curation-add-row" }, [
    curationEl("label", { attrs: { for: inputId }, text: isEvidence ? "Add evidence PMID" : "Add precedent PMID" }),
    input,
    curationButton("Add", add, { action: `${listName}-add`, disabled: running }),
  ]);
  fieldset.appendChild(addRow);
  fieldset.appendChild(error);
  if (!session.include[listName] && session.subject.type === "gene") fieldset.classList.add("excluded");
  return fieldset;
}

function curationField(labelText, control, { id, hint } = {}) {
  control.id = id;
  const wrapper = curationEl("div", { className: "feedback-field curation-field" }, [
    curationEl("label", { attrs: { for: id }, text: labelText }),
    control,
  ]);
  if (hint) wrapper.appendChild(hint);
  return wrapper;
}

function curationTextarea(value, onInput, { rows = 3, dataset } = {}) {
  const textarea = curationEl("textarea", { attrs: { rows: String(rows) }, dataset });
  textarea.value = value || "";
  textarea.addEventListener("input", () => onInput(textarea.value));
  return textarea;
}

function curationQuoteWarning(quote, abstract) {
  const warning = curationEl("p", { className: "curation-warning", dataset: { curation: "quote-warning" } });
  const update = (value) => {
    const ok = isVerbatimQuote(value, abstract);
    warning.textContent = ok ? "" : "This quote isn't a verbatim excerpt of the abstract. You can still save it.";
    curationShow(warning, !ok);
  };
  update(quote);
  return { warning, update };
}

function curationEvidenceTypeSelect(value, onChange, id) {
  const select = curationEl("select", { dataset: { curation: "evidence-type" } });
  CURATION_EVIDENCE_TYPES.forEach((type) => {
    const option = curationEl("option", { text: type.replace(/_/g, " ") });
    option.value = type;
    if (type === value) option.selected = true;
    select.appendChild(option);
  });
  select.value = value;
  select.addEventListener("change", () => onChange(select.value));
  return curationField("Evidence type", select, { id });
}

function renderCurationReview(session) {
  const { draft, subject } = session;
  const isGene = subject.type === "gene";
  const container = curationEl("div", { className: "curation-review" });
  const idPrefix = `curation-${cssSafeIdForCuration(subject.key)}`;

  if (!isGene || session.include.evidence) {
    container.appendChild(curationField(
      isGene ? "Cancer association rationale" : "Fusion interpretation",
      curationTextarea(draft.rationale, (value) => { draft.rationale = value; }, { rows: 5, dataset: { curation: "rationale" } }),
      { id: `${idPrefix}-rationale` }
    ));
    if (!isGene) {
      const checkbox = curationEl("input", { type: "checkbox", id: `${idPrefix}-well-supported`, dataset: { curation: "well-supported" } });
      checkbox.checked = Boolean(draft.wellSupported);
      checkbox.addEventListener("change", () => { draft.wellSupported = checkbox.checked; });
      container.appendChild(curationEl("label", { className: "curation-include", attrs: { for: checkbox.id } }, [
        checkbox, curationEl("span", { text: " Well supported" }),
      ]));
    }

    const cardsSection = curationEl("section", { className: "curation-review-section" }, [
      curationEl("h3", { text: `Evidence cards (${draft.cards.length})` }),
    ]);
    if (!draft.cards.length) cardsSection.appendChild(curationEl("p", { className: "subtle", text: "No evidence PMIDs." }));
    draft.cards.forEach((card, index) => {
      cardsSection.appendChild(renderCurationDraftCard(session, card, `${idPrefix}-card-${index}`));
    });
    container.appendChild(cardsSection);
    if (isGene) container.appendChild(renderCurationQuotes(session, idPrefix));
  }

  if (isGene && session.include.precedent) container.appendChild(renderCurationPrecedent(session, idPrefix));

  container.appendChild(curationField(
    "Note for history (optional)",
    curationTextarea(session.note, (value) => { session.note = value; }, { rows: 2, dataset: { curation: "note" } }),
    { id: `${idPrefix}-note` }
  ));
  return container;
}

function cssSafeIdForCuration(value) {
  return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "-");
}

function renderCurationDraftCard(session, card, idPrefix) {
  const item = curationEl("article", { className: "evidence-card curation-draft-card", dataset: { pmid: card.pmid } });
  const topline = curationEl("div", { className: "evidence-card-topline" }, [makePubMedLink(card.pmid, card.abstract)]);
  item.appendChild(topline);
  item.appendChild(curationEl("h5", { text: card.title || "Untitled PubMed record" }));
  item.appendChild(curationEl("p", { className: "evidence-card-meta", text: card.journal || "Journal unavailable" }));
  const reason = session.invalid.get(card.pmid);
  if (reason) item.appendChild(curationEl("p", { className: "curation-error", text: `PMID ${card.pmid}: ${reason}`, dataset: { curation: "invalid-reason" } }));
  item.appendChild(curationField(
    "Summary",
    curationTextarea(card.selected_reason, (value) => { card.selected_reason = value; }, { rows: 3, dataset: { curation: "card-summary" } }),
    { id: `${idPrefix}-summary` }
  ));
  const quoteCheck = curationQuoteWarning(card.quote, card.abstract);
  item.appendChild(curationField(
    "Quote",
    curationTextarea(card.quote, (value) => { card.quote = value; quoteCheck.update(value); }, { rows: 3, dataset: { curation: "card-quote" } }),
    { id: `${idPrefix}-quote`, hint: quoteCheck.warning }
  ));
  item.appendChild(curationEvidenceTypeSelect(card.evidence_type, (value) => { card.evidence_type = value; }, `${idPrefix}-type`));
  if (card.abstract) {
    const details = curationEl("details", { className: "curation-abstract" }, [
      curationEl("summary", { text: "Abstract" }),
      curationEl("p", { text: card.abstract }),
    ]);
    item.appendChild(details);
  }
  return item;
}

function renderCurationQuotes(session, idPrefix) {
  const { draft } = session;
  const section = curationEl("section", { className: "curation-review-section", dataset: { curation: "quotes" } }, [
    curationEl("h3", { text: `Supporting quotes (${draft.quotes.length})` }),
  ]);
  const abstractByPmid = new Map(draft.cards.map((card) => [card.pmid, card.abstract]));
  draft.quotes.forEach((quote, index) => {
    const row = curationEl("div", { className: "curation-quote-row", dataset: { curation: "quote-row" } });
    const select = curationEl("select", { dataset: { curation: "quote-pmid" } });
    const pmids = session.lists.evidence.includes(quote.pmid) || !quote.pmid
      ? session.lists.evidence
      : [quote.pmid, ...session.lists.evidence];
    pmids.forEach((pmid) => {
      const option = curationEl("option", { text: `PMID ${pmid}` });
      option.value = pmid;
      if (pmid === quote.pmid) option.selected = true;
      select.appendChild(option);
    });
    select.value = quote.pmid;
    const quoteCheck = curationQuoteWarning(quote.quote, abstractByPmid.get(quote.pmid));
    select.addEventListener("change", () => {
      quote.pmid = select.value;
      renderCurationDialog();
    });
    row.appendChild(curationField("Source PMID", select, { id: `${idPrefix}-quote-${index}-pmid` }));
    row.appendChild(curationField(
      "Quote",
      curationTextarea(quote.quote, (value) => { quote.quote = value; quoteCheck.update(value); }, { rows: 2, dataset: { curation: "quote-text" } }),
      { id: `${idPrefix}-quote-${index}-text`, hint: quoteCheck.warning }
    ));
    row.appendChild(curationButton("Remove quote", () => {
      draft.quotes.splice(index, 1);
      renderCurationDialog();
    }, { className: "text-button danger-link", action: "remove-quote", label: `Remove supporting quote ${index + 1}` }));
    section.appendChild(row);
  });
  section.appendChild(curationButton("Add quote", () => {
    draft.quotes.push({ pmid: session.lists.evidence[0] || "", quote: "" });
    renderCurationDialog();
  }, { action: "add-quote", disabled: !session.lists.evidence.length }));
  return section;
}

function renderCurationPrecedent(session, idPrefix) {
  const precedent = session.draft.precedent;
  const section = curationEl("section", { className: "curation-review-section", dataset: { curation: "precedent" } }, [
    curationEl("h3", { text: "High-confidence therapeutic precedent" }),
  ]);
  if (!precedent.items.length) {
    section.appendChild(curationEl("p", {
      className: "subtle",
      text: "No precedent PMIDs: saving clears the therapeutic precedent for this gene.",
    }));
    return section;
  }
  section.appendChild(curationField(
    "Precedent summary",
    curationTextarea(precedent.summary, (value) => { precedent.summary = value; }, { rows: 3, dataset: { curation: "precedent-summary" } }),
    { id: `${idPrefix}-precedent-summary` }
  ));
  precedent.items.forEach((item, index) => {
    const card = curationEl("article", { className: "evidence-card curation-draft-card", dataset: { pmid: item.pmid } }, [
      curationEl("div", { className: "evidence-card-topline" }, [makePubMedLink(item.pmid, item.abstract)]),
      curationEl("h5", { text: item.title || "Untitled PubMed record" }),
    ]);
    const quoteCheck = curationQuoteWarning(item.quote, item.abstract);
    card.appendChild(curationField(
      "Quote",
      curationTextarea(item.quote, (value) => { item.quote = value; quoteCheck.update(value); }, { rows: 2, dataset: { curation: "precedent-quote" } }),
      { id: `${idPrefix}-precedent-${index}-quote`, hint: quoteCheck.warning }
    ));
    const therapies = curationEl("input", { dataset: { curation: "precedent-therapies" }, attrs: { placeholder: "e.g. crizotinib, alectinib" } });
    therapies.value = item.therapies;
    therapies.addEventListener("input", () => { item.therapies = therapies.value; });
    card.appendChild(curationField("Therapies (comma-separated)", therapies, { id: `${idPrefix}-precedent-${index}-therapies` }));
    card.appendChild(curationEvidenceTypeSelect(item.evidence_type, (value) => { item.evidence_type = value; }, `${idPrefix}-precedent-${index}-type`));
    section.appendChild(card);
  });
  return section;
}

// ---------------------------------------------------------------------------
// Auxiliary modal (history, revert confirmation)
// ---------------------------------------------------------------------------

function openCurationAuxModal(titleText, body, buttons) {
  curationById("curation-aux-title").textContent = titleText;
  curationById("curation-aux-body").replaceChildren(body);
  curationById("curation-aux-footer").replaceChildren(...buttons);
  if (curationById("curation-aux-modal").classList.contains("hidden")) {
    curationState.auxReturnFocus = document.activeElement || null;
  }
  curationShow(curationById("curation-aux-modal"), true);
  const first = buttons[0];
  if (first && first.focus) first.focus();
}

function closeCurationAuxModal() {
  if (curationState.busy) return;
  curationShow(curationById("curation-aux-modal"), false);
  const target = curationState.auxReturnFocus;
  curationState.auxReturnFocus = null;
  if (target && target.focus && target.isConnected !== false) target.focus();
}

function bindCurationEvents() {
  const close = curationById("close-curation");
  if (close) close.addEventListener("click", closeCurationDialog);
  const closeAux = curationById("close-curation-aux");
  if (closeAux) closeAux.addEventListener("click", closeCurationAuxModal);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const aux = curationById("curation-aux-modal");
    if (aux && !aux.classList.contains("hidden")) {
      closeCurationAuxModal();
      return;
    }
    const modal = curationById("curation-modal");
    if (modal && !modal.classList.contains("hidden")) closeCurationDialog();
  });
}

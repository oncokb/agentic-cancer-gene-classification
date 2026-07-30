const API_BASE_URL_KEY = "agcg.apiBaseUrl";
const ONCOKB_TOKEN_KEY = "agcg.oncokbToken";
const NCBI_KEY_KEY = "agcg.ncbiApiKey";

// TSV column order
const TSV_COLUMNS = [
  "fusion", "tumor_type", "five_exon", "three_exon",
  "five_genomic", "three_genomic", "five_transcript", "three_transcript",
];

const state = {
  isRunning: false,
  currentResult: null,
};

const elements = {
  apiBaseUrlInput: document.querySelector("#api-base-url-input"),
  apiBaseUrlModal: document.querySelector("#api-base-url-modal"),
  saveApiUrl: document.querySelector("#save-api-url"),
  saveApiUrlModal: document.querySelector("#save-api-url-modal"),
  closeSetup: document.querySelector("#close-setup"),
  dismissSetup: document.querySelector("#dismiss-setup"),
  exportCsv: document.querySelector("#export-csv"),
  exportJson: document.querySelector("#export-json"),
  fusionInput: document.querySelector("#fusion-input"),
  geneIndex: document.querySelector("#gene-index"),
  installOutput: document.querySelector("#install-output"),
  messageBox: document.querySelector("#message-box"),
  ncbiApiKeyInput: document.querySelector("#ncbi-api-key-input"),
  ncbiStatus: document.querySelector("#ncbi-status"),
  oncokbStatus: document.querySelector("#oncokb-status"),
  oncokbTokenInput: document.querySelector("#oncokb-token-input"),
  openSetup: document.querySelector("#open-setup"),
  resultsWindow: document.querySelector("#results-window"),
  runButton: document.querySelector("#run-button"),
  runSummary: document.querySelector("#run-summary"),
  saveNcbiApiKey: document.querySelector("#save-ncbi-api-key"),
  saveOncokbToken: document.querySelector("#save-oncokb-token"),
  setupModal: document.querySelector("#setup-modal"),
  setupSummary: document.querySelector("#setup-summary"),
  tsvHint: document.querySelector("#tsv-hint"),
};

const editableFields = [
  ["in_oncokb", "In OncoKB", "boolean"],
  ["cancer_associated", "Cancer associated", "boolean"],
  ["cancer_association_rationale", "Rationale", "long"],
  ["cancer_type_prevalence", "Cancer type prevalence", "text"],
  ["gene_class", "Gene class", "text"],
  ["signaling_pathways", "Signaling pathways", "text"],
  ["gene_summary", "Gene summary", "long"],
  ["citations", "Supporting citation PMIDs", "list"],
  ["retrieval_count", "Retrieval count", "number"],
  ["insufficient_evidence", "Insufficient evidence", "booleanRequired"],
  ["evidence_support_score", "Evidence support score", "number"],
  ["evidence_support_explanation", "Evidence support explanation", "long"],
  ["cache_status", "Cache status", "text"],
  ["cache_reason", "Cache reason", "text"],
  ["cached_at", "Cached at", "text"],
  ["last_pubmed_checked_at", "Last PubMed checked", "text"],
  ["error", "Error", "text"],
];

// ---------------------------------------------------------------------------
// API URL helpers
// ---------------------------------------------------------------------------

function getApiBaseUrl() {
  return (localStorage.getItem(API_BASE_URL_KEY) || "").replace(/\/$/, "");
}

function apiUrl(path) {
  const base = getApiBaseUrl();
  return base ? base + path : path;
}

// ---------------------------------------------------------------------------
// TSV parsing
// ---------------------------------------------------------------------------

function detectTsv(rawText) {
  return rawText.split("\n").some((line) => line.includes("\t"));
}

function parseFusions() {
  const raw = elements.fusionInput.value;
  const lines = raw.split("\n").map((l) => l.trim()).filter(Boolean);
  if (!lines.length) return [];

  if (detectTsv(raw)) {
    const parsed = lines
      .map((line) => {
        const cols = line.split("\t");
        const obj = {};
        TSV_COLUMNS.forEach((col, i) => {
          const val = (cols[i] || "").trim();
          if (val) {
            if (col === "five_exon" || col === "three_exon") {
              const n = parseInt(val, 10);
              if (!isNaN(n)) obj[col] = n;
            } else {
              obj[col] = val;
            }
          }
        });
        return obj;
      })
      .filter((obj) => Boolean(obj.fusion));
    updateTsvHint(`TSV mode: ${parsed.length} fusion${parsed.length === 1 ? "" : "s"} parsed.`);
    return parsed;
  }

  // Plain newline/comma-separated fusion strings
  const fusions = raw
    .split(/\n|,/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((fusion) => ({ fusion }));
  updateTsvHint("");
  return fusions;
}

function updateTsvHint(text) {
  elements.tsvHint.textContent = text;
}

// ---------------------------------------------------------------------------
// Settings persistence
// ---------------------------------------------------------------------------

function loadSettings() {
  const savedUrl = getApiBaseUrl();
  elements.apiBaseUrlInput.value = savedUrl;
  elements.apiBaseUrlModal.value = savedUrl;

  const hasOncokb = Boolean(localStorage.getItem(ONCOKB_TOKEN_KEY));
  const hasNcbi = Boolean(localStorage.getItem(NCBI_KEY_KEY));
  elements.oncokbStatus.textContent = hasOncokb
    ? "OncoKB token is saved locally."
    : "Not configured. Paste a token to enable OncoKB membership lookup.";
  elements.ncbiStatus.textContent = hasNcbi
    ? "NCBI API key is saved locally."
    : "Recommended. Add an NCBI API key to reduce PubMed rate-limit delays.";
  elements.setupSummary.textContent = savedUrl
    ? `API: ${savedUrl}`
    : "Using same-host API.";
}

function saveApiUrl(url) {
  const trimmed = url.trim().replace(/\/$/, "");
  if (trimmed) {
    localStorage.setItem(API_BASE_URL_KEY, trimmed);
  } else {
    localStorage.removeItem(API_BASE_URL_KEY);
  }
  elements.apiBaseUrlInput.value = trimmed;
  elements.apiBaseUrlModal.value = trimmed;
  elements.setupSummary.textContent = trimmed ? `API: ${trimmed}` : "Using same-host API.";
  setInstallOutput("Backend URL saved", trimmed || "Using same-host API.");
}

function saveOncoKBToken() {
  const token = elements.oncokbTokenInput.value.trim();
  if (!token) {
    setInstallOutput("OncoKB token not entered", "Paste a token before saving.", "error");
    return;
  }
  localStorage.setItem(ONCOKB_TOKEN_KEY, token);
  elements.oncokbTokenInput.value = "";
  elements.oncokbStatus.textContent = "OncoKB token is saved locally.";
  setInstallOutput("OncoKB token saved", "The token will be sent with each annotation request.");
}

function saveNCBIApiKey() {
  const key = elements.ncbiApiKeyInput.value.trim();
  if (!key) {
    setInstallOutput("NCBI API key not entered", "Paste a key before saving.", "error");
    return;
  }
  localStorage.setItem(NCBI_KEY_KEY, key);
  elements.ncbiApiKeyInput.value = "";
  elements.ncbiStatus.textContent = "NCBI API key is saved locally.";
  setInstallOutput("NCBI API key saved", "The key will be sent with each annotation request.");
}

// ---------------------------------------------------------------------------
// UI state helpers
// ---------------------------------------------------------------------------

function setMessage(message, type = "info") {
  elements.messageBox.textContent = message;
  elements.messageBox.className = `status-box ${type}`;
}

function clearMessage() {
  elements.messageBox.textContent = "";
  elements.messageBox.className = "status-box hidden";
}

function setRunning(isRunning) {
  state.isRunning = isRunning;
  elements.runButton.textContent = isRunning ? "Running..." : "Run";
  elements.runButton.disabled = isRunning;
}

function showSetupModal() {
  elements.setupModal.classList.remove("hidden");
}

function hideSetupModal() {
  elements.setupModal.classList.add("hidden");
}

function setInstallOutput(title, body, type = "info") {
  elements.installOutput.className = "install-output";
  elements.installOutput.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.installOutput.classList.toggle("error", type === "error");
  elements.installOutput.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// Annotation run
// ---------------------------------------------------------------------------

async function runAnnotation() {
  const fusionInputs = parseFusions();
  if (!fusionInputs.length) {
    setMessage("Add at least one fusion before running.", "error");
    return;
  }

  setRunning(true);
  clearMessage();
  setMessage(`Submitting ${fusionInputs.length} fusion${fusionInputs.length === 1 ? "" : "s"} for annotation…`, "info");

  try {
    const response = await fetch(apiUrl("/v1/annotate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fusions: fusionInputs }),
    });

    if (!response.ok) {
      let detail = response.statusText;
      const text = await response.text();
      try {
        const payload = JSON.parse(text);
        detail = payload.detail || detail;
      } catch {
        detail = text || detail;
      }
      throw new Error(detail || "Request failed");
    }

    const result = await response.json();
    state.currentResult = result;
    renderAnnotationResult(result);
    setMessage("Annotation complete. Review fields before exporting.", "info");
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  } finally {
    setRunning(false);
  }
}

function formatRunError(message) {
  if (String(message).includes("ONCOKB_API_TOKEN")) {
    return "OncoKB API token is required. Open Settings and configure your OncoKB token.";
  }
  return message;
}

// ---------------------------------------------------------------------------
// Gene index sidebar
// ---------------------------------------------------------------------------

let _geneObserver = null;

function rebuildGeneIndex(annotations) {
  elements.geneIndex.replaceChildren();
  if (!annotations || !annotations.length) return;

  annotations.forEach((annotation) => {
    const btn = document.createElement("button");
    btn.className = "gene-index-item";
    btn.textContent = annotation.gene;
    btn.type = "button";
    btn.addEventListener("click", () => {
      const card = document.getElementById(`gene-${annotation.gene}`);
      if (card) card.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    elements.geneIndex.appendChild(btn);
  });

  observeGeneCards();
}

function observeGeneCards() {
  if (_geneObserver) _geneObserver.disconnect();
  const cards = elements.resultsWindow.querySelectorAll(".annotation-card[id]");
  if (!cards.length) return;

  _geneObserver = new IntersectionObserver(
    (entries) => {
      const visible = entries.filter((e) => e.isIntersecting);
      if (!visible.length) return;
      const topEntry = visible.reduce((a, b) =>
        a.boundingClientRect.top < b.boundingClientRect.top ? a : b,
      );
      const gene = topEntry.target.id.replace("gene-", "");
      elements.geneIndex.querySelectorAll(".gene-index-item").forEach((btn) => {
        btn.classList.toggle("active", btn.textContent === gene);
      });
    },
    { root: elements.resultsWindow, threshold: 0.15 },
  );

  cards.forEach((card) => _geneObserver.observe(card));
}

// ---------------------------------------------------------------------------
// Results rendering
// ---------------------------------------------------------------------------

function renderAnnotationResult(result) {
  const annotations = result.annotations || [];
  const hasAnnotations = annotations.length > 0;
  elements.exportCsv.disabled = !hasAnnotations;
  elements.exportJson.disabled = !hasAnnotations;

  const total = result.genes_annotated;
  elements.runSummary.textContent =
    `${total} gene${total === 1 ? "" : "s"} annotated from ` +
    `${result.fusions_processed} fusion${result.fusions_processed === 1 ? "" : "s"}.`;

  if (!hasAnnotations) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `
      <h3>${escapeHtml(result.run_error ? "Run stopped" : "No results")}</h3>
      <p>${escapeHtml(result.run_error || "No gene annotations were returned.")}</p>
    `;
    elements.resultsWindow.replaceChildren(empty);
    rebuildGeneIndex([]);
    return;
  }

  const list = document.createElement("div");
  list.className = "annotation-list";

  annotations.forEach((annotation, index) => {
    const card = document.createElement("article");
    card.className = "annotation-card";
    card.id = `gene-${annotation.gene}`;
    card.innerHTML = `
      <header>
        <div>
          <h3>${escapeHtml(annotation.gene)}</h3>
          <div class="subtle">${escapeHtml(formatList(annotation.fusions))}</div>
          <div class="review-badges">${renderCompactBadges(annotation)}</div>
        </div>
        <span class="status-pill">${escapeHtml(annotation.date_annotated || "")}</span>
      </header>
      <div class="annotation-fields"></div>
    `;

    const fields = card.querySelector(".annotation-fields");
    for (const [field, label, type] of editableFields) {
      fields.appendChild(renderEditableField(annotation, index, field, label, type));
    }
    list.appendChild(card);
  });

  elements.resultsWindow.replaceChildren(list);
  rebuildGeneIndex(annotations);
}

function reviewPriority(annotation) {
  if (annotation.insufficient_evidence) {
    return {
      label: "Low priority",
      tone: "low",
      title: "Retrieved evidence was insufficient for a confident cancer annotation.",
    };
  }
  if (annotation.cancer_associated === false) {
    return {
      label: "Low priority",
      tone: "low",
      title: "Current evidence does not support a cancer association.",
    };
  }
  if (annotation.in_oncokb) {
    return {
      label: "High priority",
      tone: "high",
      title: "This gene is already represented in OncoKB and may need curator attention.",
    };
  }
  if (annotation.cancer_associated === true) {
    return { label: "Review", tone: "neutral", title: "Review this result before export." };
  }
  return { label: "Review", tone: "neutral", title: "Review this result before export." };
}

function evidenceSignal(annotation) {
  if (annotation.insufficient_evidence) {
    return {
      label: "Insufficient evidence",
      tone: "low",
      title: "The model did not find enough grounded evidence to classify this gene.",
    };
  }
  if (annotation.cancer_associated === false) {
    return {
      label: "No cancer evidence",
      tone: "low",
      title: "Current retrieved evidence does not support a cancer association.",
    };
  }
  if (annotation.cancer_associated === true) {
    return {
      label: "Cancer associated",
      tone: "neutral",
      title: "Literature supports a cancer association.",
    };
  }
  return null;
}

function compactBadges(annotation) {
  return [
    reviewPriority(annotation),
    evidenceSignal(annotation),
    annotation.in_oncokb
      ? { label: "OncoKB", tone: "high", title: "OncoKB membership lookup returned true." }
      : null,
  ].filter(Boolean);
}

function renderCompactBadges(annotation) {
  return compactBadges(annotation)
    .map(
      (badge) => `
        <span class="review-badge ${escapeHtml(badge.tone)}" title="${escapeHtml(badge.title)}">
          ${escapeHtml(badge.label)}
        </span>
      `,
    )
    .join("");
}

function renderEditableField(annotation, index, field, label, type) {
  const wrapper = document.createElement("label");
  wrapper.className = type === "long" || type === "list" ? "field wide" : "field";
  const labelElement = document.createElement("span");
  labelElement.textContent = label;
  wrapper.appendChild(labelElement);

  let control;
  if (type === "boolean" || type === "booleanRequired") {
    control = document.createElement("select");
    if (type === "boolean") {
      control.appendChild(new Option("", ""));
    }
    control.appendChild(new Option("TRUE", "true"));
    control.appendChild(new Option("FALSE", "false"));
    control.value =
      annotation[field] === null || annotation[field] === undefined
        ? ""
        : String(Boolean(annotation[field]));
  } else if (type === "long" || type === "list") {
    control = document.createElement("textarea");
    control.rows = type === "long" ? 4 : 2;
    control.value =
      type === "list" ? formatList(annotation[field]) : annotation[field] || "";
  } else {
    control = document.createElement("input");
    control.type = type === "number" ? "number" : "text";
    if (field === "evidence_support_score") {
      control.step = "0.01";
      control.min = "0";
      control.max = "1";
    }
    control.value =
      annotation[field] === null || annotation[field] === undefined ? "" : annotation[field];
  }

  control.dataset.index = index;
  control.dataset.field = field;
  control.dataset.type = type;
  control.addEventListener("input", handleAnnotationEdit);
  wrapper.appendChild(control);
  return wrapper;
}

function handleAnnotationEdit(event) {
  if (!state.currentResult) return;
  const { index, field, type } = event.target.dataset;
  const annotation = state.currentResult.annotations[Number(index)];
  let value = event.target.value;

  if (type === "boolean" || type === "booleanRequired") {
    value = value === "" ? null : value === "true";
  } else if (type === "list") {
    value = value
      .split(";")
      .map((item) => item.trim())
      .filter(Boolean);
  } else if (type === "number") {
    value = value === "" ? 0 : Number(value);
  }

  annotation[field] = value;
}

// ---------------------------------------------------------------------------
// Export
// ---------------------------------------------------------------------------

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function exportCsv() {
  if (!state.currentResult) return;
  const response = await fetch(apiUrl("/v1/export/annotation-results.csv"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.currentResult),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || "CSV export failed");
  }
  const blob = await response.blob();
  downloadBlob(blob, "annotation_results.csv");
}

function exportJson() {
  if (!state.currentResult) return;
  const blob = new Blob([JSON.stringify(state.currentResult, null, 2)], {
    type: "application/json",
  });
  downloadBlob(blob, "results.json");
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function formatList(value) {
  if (!Array.isArray(value)) return value || "";
  return value.join("; ");
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// ---------------------------------------------------------------------------
// Event binding
// ---------------------------------------------------------------------------

function bindEvents() {
  elements.runButton.addEventListener("click", runAnnotation);
  elements.openSetup.addEventListener("click", () => showSetupModal());
  elements.closeSetup.addEventListener("click", () => hideSetupModal());
  elements.dismissSetup.addEventListener("click", () => hideSetupModal());

  elements.saveApiUrl.addEventListener("click", () =>
    saveApiUrl(elements.apiBaseUrlInput.value),
  );
  elements.saveApiUrlModal.addEventListener("click", () =>
    saveApiUrl(elements.apiBaseUrlModal.value),
  );
  elements.saveOncokbToken.addEventListener("click", saveOncoKBToken);
  elements.saveNcbiApiKey.addEventListener("click", saveNCBIApiKey);

  elements.exportJson.addEventListener("click", exportJson);
  elements.exportCsv.addEventListener("click", async () => {
    try {
      await exportCsv();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });

  elements.fusionInput.addEventListener("input", () => {
    parseFusions();
  });
}

bindEvents();
loadSettings();

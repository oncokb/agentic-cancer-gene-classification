const SETUP_DISMISSED_KEY = "agcg.localSetupDismissed.v1";
const TUTORIAL_DISMISSED_KEY = "agcg.tutorialDismissed.v1";
const LOCAL_BACKEND_PREFERENCE = ["codex", "claude-code", "copilot", "antigravity"];

const state = {
  mode: "annotate",
  backendStatus: [],
  installers: [],
  backendSelectionTouched: false,
  anthropicSdkConfigured: false,
  minimumSetupComplete: false,
  isRunning: false,
  googleSheetsConfigured: false,
  currentResult: null,
  currentRunGenesTotal: 0,
  benchmarkResult: null,
};

const elements = {
  backendSelect: document.querySelector("#backend-select"),
  backendStatus: document.querySelector("#backend-status"),
  benchmarkOptions: document.querySelector("#benchmark-options"),
  closeSetup: document.querySelector("#close-setup"),
  antigravityInstallCommand: document.querySelector("#antigravity-install-command"),
  antigravityPill: document.querySelector("#antigravity-pill"),
  claudePill: document.querySelector("#claude-pill"),
  codexPill: document.querySelector("#codex-pill"),
  copilotPill: document.querySelector("#copilot-pill"),
  dismissSetup: document.querySelector("#dismiss-setup"),
  exportCsv: document.querySelector("#export-csv"),
  exportGoogleSheet: document.querySelector("#export-google-sheet"),
  exportJson: document.querySelector("#export-json"),
  fusionInput: document.querySelector("#fusion-input"),
  fusionInputField: document.querySelector("#fusion-input-field"),
  googleServiceAccountFile: document.querySelector("#google-service-account-file"),
  googleSheetsPill: document.querySelector("#google-sheets-pill"),
  googleSheetsStatus: document.querySelector("#google-sheets-status"),
  installButtons: document.querySelectorAll(".install-button"),
  installOutput: document.querySelector("#install-output"),
  claudeInstallCommand: document.querySelector("#claude-install-command"),
  codexInstallCommand: document.querySelector("#codex-install-command"),
  copilotInstallCommand: document.querySelector("#copilot-install-command"),
  loginButtons: document.querySelectorAll(".login-button"),
  messageBox: document.querySelector("#message-box"),
  metricsGrid: document.querySelector("#metrics-grid"),
  minimumSetupList: document.querySelector("#minimum-setup-list"),
  minimumSetupPanel: document.querySelector("#minimum-setup-panel"),
  modeDescription: document.querySelector("#mode-description"),
  ncbiApiKeyInput: document.querySelector("#ncbi-api-key-input"),
  ncbiPill: document.querySelector("#ncbi-pill"),
  ncbiStatus: document.querySelector("#ncbi-status"),
  noJudge: document.querySelector("#no-judge"),
  oncokbPill: document.querySelector("#oncokb-pill"),
  oncokbStatus: document.querySelector("#oncokb-status"),
  oncokbTokenInput: document.querySelector("#oncokb-token-input"),
  openSetup: document.querySelector("#open-setup"),
  prepareLocalPaths: document.querySelector("#prepare-local-paths"),
  refreshBackends: document.querySelector("#refresh-backends"),
  resultsWindow: document.querySelector("#results-window"),
  runButton: document.querySelector("#run-button"),
  runSummary: document.querySelector("#run-summary"),
  saveNcbiApiKey: document.querySelector("#save-ncbi-api-key"),
  saveGoogleServiceAccount: document.querySelector("#save-google-service-account"),
  saveOncokbToken: document.querySelector("#save-oncokb-token"),
  setupModal: document.querySelector("#setup-modal"),
  setupSummary: document.querySelector("#setup-summary"),
};

const editableFields = [
  ["in_oncokb", "In OncoKB", "boolean"],
  ["cancer_associated", "Cancer associated", "boolean"],
  ["cancer_association_rationale", "Rationale", "long"],
  ["cancer_associated_gene_tier", "Cancer tier", "text"],
  ["og_or_tsg", "OG/TSG", "text"],
  ["cancer_type_prevalence", "Cancer type prevalence", "text"],
  ["gene_class", "Gene class", "text"],
  ["signaling_pathways", "Signaling pathways", "text"],
  ["gene_summary", "Gene summary", "long"],
  ["citations", "Supporting citation PMIDs", "list"],
  ["retrieval_count", "Retrieval count", "number"],
  ["retrieved_pmids", "Retrieved PMIDs", "list"],
  ["insufficient_evidence", "Insufficient evidence", "booleanRequired"],
  ["confidence", "Confidence", "number"],
  ["error", "Error", "text"],
];

const modeDescriptions = {
  annotate: {
    title: "Annotation",
    body: "Run new gene fusion annotations, review each gene result, then export JSON or CSV.",
  },
  benchmark: {
    title: "Benchmark",
    body: "Run the saved holdout set to compare pipeline output against expected annotations and citation metrics.",
  },
};

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
  elements.runButton.textContent = isRunning ? "Running..." : runButtonLabel();
  updateRunButtonState();
}

function parseFusions() {
  return elements.fusionInput.value
    .split(/\n|,/)
    .map((fusion) => fusion.trim())
    .filter(Boolean);
}

function selectedLocalBackend() {
  return elements.backendSelect.value || null;
}

function backendLabel(backend) {
  if (backend === "claude-code") return "Claude Code";
  if (backend === "codex") return "Codex";
  if (backend === "copilot") return "GitHub Copilot";
  if (backend === "antigravity") return "Antigravity";
  return "Anthropic SDK";
}

function backendInstalled(backend) {
  const status = state.backendStatus.find((item) => item.backend === backend);
  return Boolean(status && status.installed);
}

function applyPreferredBackendDefault() {
  if (state.backendSelectionTouched) return;
  const preferredBackend = LOCAL_BACKEND_PREFERENCE.find((backend) =>
    backendInstalled(backend),
  );
  elements.backendSelect.value = preferredBackend || "";
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll(".segment").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  renderModeDescription(mode);
  elements.fusionInputField.classList.toggle("hidden", mode !== "annotate");
  elements.benchmarkOptions.classList.toggle("hidden", mode !== "benchmark");
  elements.runButton.textContent = runButtonLabel();
  clearMessage();
}

function renderModeDescription(mode) {
  const description = modeDescriptions[mode] || modeDescriptions.annotate;
  elements.modeDescription.innerHTML = `
    <strong>${escapeHtml(description.title)}</strong>
    <p>${escapeHtml(description.body)}</p>
  `;
}

function runButtonLabel() {
  return state.mode === "benchmark" ? "Run Benchmark" : "Run";
}

async function fetchBackendStatus({ forceModal = false } = {}) {
  try {
    const response = await fetch("/v1/local-backends/status");
    if (!response.ok) throw new Error(`Status request failed: ${response.status}`);
    const payload = await response.json();
    state.backendStatus = payload.backends || [];
    state.anthropicSdkConfigured = Boolean(payload.anthropic_sdk_configured);
    state.minimumSetupComplete = Boolean(payload.minimum_setup_complete);
    applyPreferredBackendDefault();
    renderBackendStatus(payload);

    const tutorialDismissed = localStorage.getItem(TUTORIAL_DISMISSED_KEY) === "true";
    if (forceModal || payload.setup_required || !tutorialDismissed) {
      showSetupModal();
    }
    updateRunButtonState();
  } catch (error) {
    state.minimumSetupComplete = false;
    elements.setupSummary.textContent = "Could not check local backend installation.";
    updateRunButtonState();
    setMessage(error.message, "error");
  }
}

async function fetchInstallerInfo() {
  try {
    const response = await fetch("/v1/local-backends/installers");
    if (!response.ok) throw new Error(`Installer request failed: ${response.status}`);
    state.installers = await response.json();
    renderInstallers();
  } catch (error) {
    elements.codexInstallCommand.textContent = "Open the setup guide to install manually.";
    elements.claudeInstallCommand.textContent = "Open the setup guide to install manually.";
    elements.copilotInstallCommand.textContent = "Open the setup guide to install manually.";
    elements.antigravityInstallCommand.textContent = "Open the setup guide to install manually.";
    setInstallOutput("Installer metadata could not be loaded.", error.message, "error");
  }
}

async function fetchGoogleSheetsConfig() {
  try {
    const response = await fetch("/v1/google-sheets/config");
    if (!response.ok) throw new Error(`Google Sheets config request failed: ${response.status}`);
    const payload = await response.json();
    renderGoogleSheetsConfig(payload);
  } catch (error) {
    state.googleSheetsConfigured = false;
    elements.googleSheetsStatus.textContent = "Could not check Google Sheets configuration.";
    updateSetupPill(elements.googleSheetsPill, null);
    updateGoogleSheetExportButton();
  }
}

async function fetchOncoKBConfig() {
  try {
    const response = await fetch("/v1/oncokb/config");
    if (!response.ok) throw new Error(`OncoKB config request failed: ${response.status}`);
    const payload = await response.json();
    renderOncoKBConfig(payload);
  } catch (error) {
    elements.oncokbStatus.textContent = "Could not check OncoKB configuration.";
    updateSetupPill(elements.oncokbPill, null);
  }
}

async function fetchNCBIConfig() {
  try {
    const response = await fetch("/v1/ncbi/config");
    if (!response.ok) throw new Error(`NCBI config request failed: ${response.status}`);
    const payload = await response.json();
    renderNCBIConfig(payload);
  } catch (error) {
    elements.ncbiStatus.textContent = "Could not check NCBI configuration.";
    updateSetupPill(elements.ncbiPill, null);
  }
}

function renderOncoKBConfig(config) {
  const configured = Boolean(config && config.configured);
  elements.oncokbPill.textContent = configured ? "Configured" : "Missing";
  elements.oncokbPill.className = `status-pill ${configured ? "ok" : "missing"}`;
  if (configured) {
    const source = config.source === "environment" ? ".env" : "local setup";
    elements.oncokbStatus.textContent = `Configured from ${source}.`;
  } else {
    elements.oncokbStatus.textContent =
      "Not configured. Paste an OncoKB API token before running annotations.";
  }
}

function renderNCBIConfig(config) {
  const configured = Boolean(config && config.configured);
  elements.ncbiPill.textContent = configured ? "Configured" : "Recommended";
  elements.ncbiPill.className = `status-pill ${configured ? "ok" : ""}`;
  if (configured) {
    const source = config.source === "environment" ? ".env" : "local setup";
    elements.ncbiStatus.textContent = `Configured from ${source}.`;
  } else {
    elements.ncbiStatus.textContent =
      "Recommended. Add an NCBI API key to reduce PubMed rate-limit delays.";
  }
}

function renderGoogleSheetsConfig(config) {
  const configured = Boolean(config && config.configured);
  state.googleSheetsConfigured = configured;
  elements.googleSheetsPill.textContent = configured ? "Configured" : "Missing";
  elements.googleSheetsPill.className = `status-pill ${configured ? "ok" : "missing"}`;
  if (configured) {
    const source = config.source === "environment" ? ".env" : "local upload";
    elements.googleSheetsStatus.textContent =
      `Configured from ${source}: ${config.service_account_email}`;
  } else {
    elements.googleSheetsStatus.textContent =
      "Not configured. Upload a service account JSON key to enable direct export.";
  }
  updateGoogleSheetExportButton();
}

function renderBackendStatus(statusPayload) {
  renderSelectedBackendStatus();
  renderMinimumSetup(statusPayload);

  const codex = state.backendStatus.find((item) => item.backend === "codex");
  const claude = state.backendStatus.find((item) => item.backend === "claude-code");
  const copilot = state.backendStatus.find((item) => item.backend === "copilot");
  const antigravity = state.backendStatus.find((item) => item.backend === "antigravity");
  updateSetupPill(elements.codexPill, codex);
  updateSetupPill(elements.claudePill, claude);
  updateSetupPill(elements.copilotPill, copilot);
  updateSetupPill(elements.antigravityPill, antigravity);

  if (statusPayload.setup_required) {
    elements.setupSummary.textContent = statusPayload.setup_messages.join(" ");
  } else {
    elements.setupSummary.textContent = "Minimum setup is complete.";
  }
  renderInstallers();
}

function renderMinimumSetup(statusPayload) {
  const executionReady = statusPayload.anthropic_sdk_configured ||
    statusPayload.local_backend_configured;
  const requirements = [
    [
      executionReady,
      executionReady
        ? "Execution path configured."
        : "Configure Anthropic SDK or install a local agent.",
    ],
    [
      statusPayload.oncokb_configured,
      statusPayload.oncokb_configured
        ? "OncoKB API token configured."
        : "Paste an OncoKB API token from OncoKB account settings.",
    ],
  ];
  elements.minimumSetupList.innerHTML = requirements
    .map(([complete, label]) => (
      `<li class="${complete ? "complete" : "missing"}">${escapeHtml(label)}</li>`
    ))
    .join("");
  elements.minimumSetupPanel.classList.toggle(
    "complete",
    Boolean(statusPayload.minimum_setup_complete),
  );
}

function renderSelectedBackendStatus() {
  const backend = selectedLocalBackend();
  elements.backendStatus.innerHTML = "";
  if (!backend) {
    if (state.anthropicSdkConfigured) {
      elements.backendStatus.innerHTML =
        '<p class="backend-help">Using Anthropic SDK.</p>';
      elements.backendStatus.title = "";
      return;
    }
    elements.backendStatus.innerHTML =
      '<p class="backend-help error">Anthropic SDK is not configured. Add an API key or select a configured local agent in Settings.</p>';
    elements.backendStatus.title = "";
    return;
  }

  const status = state.backendStatus.find((item) => item.backend === backend);
  if (status && status.installed) {
    const details = status.version || status.path || "Ready";
    elements.backendStatus.innerHTML = `
      <p class="backend-help">${escapeHtml(backendLabel(backend))} is ready.</p>
    `;
    elements.backendStatus.title = details;
    return;
  }

  elements.backendStatus.innerHTML = `
    <p class="backend-help error">
      ${escapeHtml(backendLabel(backend))} is not installed on this computer.
      Open Settings to install or configure it.
    </p>
  `;
  elements.backendStatus.title = "";
  updateRunButtonState();
}

function selectedExecutionReady() {
  const backend = selectedLocalBackend();
  if (!backend) return state.anthropicSdkConfigured;
  return backendInstalled(backend);
}

function updateRunButtonState() {
  elements.runButton.disabled =
    state.isRunning || !state.minimumSetupComplete || !selectedExecutionReady();
}

function renderInstallers() {
  const codex = state.installers.find((item) => item.backend === "codex");
  const claude = state.installers.find((item) => item.backend === "claude-code");
  const copilot = state.installers.find((item) => item.backend === "copilot");
  const antigravity = state.installers.find((item) => item.backend === "antigravity");
  renderInstallerCommand(elements.codexInstallCommand, codex);
  renderInstallerCommand(elements.claudeInstallCommand, claude);
  renderInstallerCommand(elements.copilotInstallCommand, copilot);
  renderInstallerCommand(elements.antigravityInstallCommand, antigravity);

  elements.installButtons.forEach((button) => {
    const backend = button.dataset.backend;
    const installer = state.installers.find((item) => item.backend === backend);
    const installed = backendInstalled(backend);
    const hasSetupUrl = Boolean(installer && installer.setup_url);
    button.disabled = installed || !installer || (!installer.supported && !hasSetupUrl);
    if (installed) {
      button.textContent = `${backendLabel(backend)} installed`;
    } else if (hasSetupUrl && !installer.supported) {
      button.textContent = `Open ${backendLabel(backend)} setup`;
    } else if (!installer || !installer.supported) {
      button.textContent = "Manual setup required";
    } else {
      button.textContent = `Install ${backendLabel(backend)}`;
    }
  });

  elements.loginButtons.forEach((button) => {
    const backend = button.dataset.backend;
    const installed = backendInstalled(backend);
    button.disabled = !installed;
    button.textContent = installed
      ? `Log in to ${backendLabel(backend)}`
      : `Install ${backendLabel(backend)} first`;
  });
}

function renderInstallerCommand(element, installer) {
  if (!installer) {
    element.textContent = "Checking installer...";
  } else if (!installer.supported) {
    element.textContent = installer.setup_url
      ? "Open the official setup page from this app."
      : "Automatic install is not supported on this operating system.";
  } else {
    element.textContent = installer.display_command;
  }
}

function updateSetupPill(element, status) {
  if (!status) {
    element.textContent = "Unknown";
    element.className = "status-pill";
    return;
  }
  element.textContent = status.installed ? "Installed" : "Missing";
  element.className = `status-pill ${status.installed ? "ok" : "missing"}`;
}

function showSetupModal() {
  elements.setupModal.classList.remove("hidden");
}

function hideSetupModal({ remember = false } = {}) {
  if (remember) {
    localStorage.setItem(SETUP_DISMISSED_KEY, "true");
    localStorage.setItem(TUTORIAL_DISMISSED_KEY, "true");
  }
  elements.setupModal.classList.add("hidden");
}

function setInstallOutput(title, body, type = "info") {
  elements.installOutput.className = "install-output";
  elements.installOutput.innerHTML = `
    <h3>${escapeHtml(title)}</h3>
    <p>${escapeHtml(body)}</p>
  `;
  elements.installOutput.classList.toggle("error", type === "error");
}

async function installBackend(backend) {
  const label = backendLabel(backend);
  const installer = state.installers.find((item) => item.backend === backend);
  if (installer && !installer.supported && installer.setup_url) {
    window.open(installer.setup_url, "_blank", "noreferrer");
    setInstallOutput(
      `${label} setup opened`,
      "Complete the provider setup, then refresh status before running locally.",
    );
    return;
  }

  const confirmed = window.confirm(
    `Install ${label} using the official installer? This may take a few minutes.`,
  );
  if (!confirmed) return;

  elements.installButtons.forEach((button) => {
    button.disabled = true;
  });
  setInstallOutput(`Installing ${label}`, "The installer is running. Keep this window open.");

  try {
    const response = await fetch("/v1/local-backends/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend }),
    });
    const payload = await readJsonResponse(response);
    const output = [payload.stdout, payload.stderr].filter(Boolean).join("\n\n");
    const nextSteps = (payload.next_steps || []).join(" ");
    elements.installOutput.className = "install-output";
    elements.installOutput.innerHTML = `
      <h3>${payload.installed ? `${label} installed` : `${label} install needs attention`}</h3>
      <p>${escapeHtml(nextSteps || "Refresh status before running locally.")}</p>
      ${output ? `<pre>${escapeHtml(output)}</pre>` : ""}
    `;
    await fetchBackendStatus({ forceModal: true });
  } catch (error) {
    setInstallOutput(`${label} install failed`, error.message, "error");
    renderInstallers();
  }
}

async function loginBackend(backend) {
  const label = backendLabel(backend);
  const confirmed = window.confirm(
    `Start ${label} login? A GitHub browser or SSO prompt may open.`,
  );
  if (!confirmed) return;

  elements.loginButtons.forEach((button) => {
    button.disabled = true;
  });
  setInstallOutput(`Logging in to ${label}`, "Complete the browser/SSO prompt if one opens.");

  try {
    const response = await fetch("/v1/local-backends/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend }),
    });
    const payload = await readJsonResponse(response);
    const output = [payload.stdout, payload.stderr].filter(Boolean).join("\n\n");
    const nextSteps = (payload.next_steps || []).join(" ");
    elements.installOutput.className = "install-output";
    elements.installOutput.innerHTML = `
      <h3>${payload.return_code === 0 ? `${label} login finished` : `${label} login needs attention`}</h3>
      <p>${escapeHtml(nextSteps || "Refresh status before running locally.")}</p>
      ${output ? `<pre>${escapeHtml(output)}</pre>` : ""}
    `;
    await fetchBackendStatus({ forceModal: true });
  } catch (error) {
    setInstallOutput(`${label} login failed`, error.message, "error");
    renderInstallers();
  }
}

async function prepareLocalPaths() {
  elements.prepareLocalPaths.disabled = true;
  setInstallOutput(
    "Preparing local agent paths",
    "The app is checking standard install locations and saving detected paths.",
  );

  try {
    const response = await fetch("/v1/local-backends/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await readJsonResponse(response);
    const labels = Object.keys(payload.configured_paths || {})
      .map((backend) => backendLabel(backend))
      .join(", ");
    const detail = labels
      ? `${payload.message} Detected: ${labels}.`
      : "No local agent executables were detected yet. Install or log in to a local agent, then run this again.";
    setInstallOutput("Local app setup complete", detail);
    await fetchBackendStatus({ forceModal: true });
  } catch (error) {
    setInstallOutput("Local app setup failed", error.message, "error");
  } finally {
    elements.prepareLocalPaths.disabled = false;
  }
}

async function saveGoogleServiceAccount() {
  const file = elements.googleServiceAccountFile.files[0];
  if (!file) {
    setInstallOutput(
      "Google Sheets credentials not selected",
      "Choose a service account JSON file before saving.",
      "error",
    );
    return;
  }

  const confirmed = window.confirm(
    "Save this Google service account JSON on this computer for Google Sheets export?",
  );
  if (!confirmed) return;

  elements.saveGoogleServiceAccount.disabled = true;
  setInstallOutput(
    "Saving Google Sheets credentials",
    "The credential is being stored in this computer's app config directory.",
  );

  try {
    const serviceAccountJson = await file.text();
    const response = await fetch("/v1/google-sheets/service-account", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ service_account_json: serviceAccountJson }),
    });
    const payload = await readJsonResponse(response);
    setInstallOutput("Google Sheets configured", payload.message);
    elements.googleServiceAccountFile.value = "";
    await fetchGoogleSheetsConfig();
  } catch (error) {
    setInstallOutput("Google Sheets setup failed", error.message, "error");
  } finally {
    elements.saveGoogleServiceAccount.disabled = false;
  }
}

async function saveOncoKBToken() {
  const token = elements.oncokbTokenInput.value.trim();
  if (!token) {
    setInstallOutput(
      "OncoKB token not entered",
      "Paste an OncoKB API token before saving.",
      "error",
    );
    return;
  }

  elements.saveOncokbToken.disabled = true;
  setInstallOutput(
    "Saving OncoKB token",
    "The token is being stored in this computer's app config directory.",
  );

  try {
    const response = await fetch("/v1/oncokb/token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_token: token }),
    });
    const payload = await readJsonResponse(response);
    setInstallOutput("OncoKB configured", payload.message);
    elements.oncokbTokenInput.value = "";
    await fetchOncoKBConfig();
    await fetchBackendStatus({ forceModal: true });
  } catch (error) {
    setInstallOutput("OncoKB setup failed", error.message, "error");
  } finally {
    elements.saveOncokbToken.disabled = false;
  }
}

async function saveNCBIApiKey() {
  const apiKey = elements.ncbiApiKeyInput.value.trim();
  if (!apiKey) {
    setInstallOutput(
      "NCBI API key not entered",
      "Paste an NCBI API key before saving.",
      "error",
    );
    return;
  }

  elements.saveNcbiApiKey.disabled = true;
  setInstallOutput(
    "Saving NCBI API key",
    "The key is being stored in this computer's app config directory.",
  );

  try {
    const response = await fetch("/v1/ncbi/api-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: apiKey }),
    });
    const payload = await readJsonResponse(response);
    setInstallOutput("NCBI configured", payload.message);
    elements.ncbiApiKeyInput.value = "";
    await fetchNCBIConfig();
  } catch (error) {
    setInstallOutput("NCBI setup failed", error.message, "error");
  } finally {
    elements.saveNcbiApiKey.disabled = false;
  }
}

async function runSelectedMode() {
  if (state.mode === "benchmark") {
    await runBenchmark();
    return;
  }
  await runAnnotation();
}

async function runAnnotation() {
  const fusions = parseFusions();
  if (!fusions.length) {
    setMessage("Add at least one fusion before running.", "error");
    return;
  }

  const localBackend = selectedLocalBackend();
  if (localBackend && !backendInstalled(localBackend)) {
    setMessage(`${backendLabel(localBackend)} was not detected on this server.`, "error");
    return;
  }

  setRunning(true);
  clearMessage();
  state.currentRunGenesTotal = 0;
  try {
    state.benchmarkResult = null;
    await streamAnnotationRun(fusions, localBackend);
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  } finally {
    setRunning(false);
  }
}

async function streamAnnotationRun(fusions, localBackend) {
  const response = await fetch("/v1/annotate/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fusions, local_backend: localBackend }),
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

  if (!response.body) {
    throw new Error("This browser does not support streaming annotation results.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      processAnnotationStreamLine(line);
    }
    if (done) break;
  }

  if (buffer.trim()) {
    processAnnotationStreamLine(buffer);
  }
}

function processAnnotationStreamLine(line) {
  const trimmed = line.trim();
  if (!trimmed) return;
  const event = JSON.parse(trimmed);
  handleAnnotationStreamEvent(event);
}

function handleAnnotationStreamEvent(event) {
  if (event.type === "start") {
    state.currentRunGenesTotal = event.genes_total || 0;
    state.currentResult = {
      run_id: event.run_id,
      timestamp: event.timestamp,
      fusions_processed: event.fusions_processed,
      genes_annotated: 0,
      annotations: [],
      run_error: null,
    };
    renderAnnotationResult(state.currentResult);
    setMessage(`Annotation run started for ${state.currentRunGenesTotal} genes.`, "info");
    return;
  }

  if (event.type === "annotation") {
    appendAnnotationResult(event.annotation);
    const total = event.genes_total || state.currentRunGenesTotal;
    setMessage(`Annotated ${event.completed_count} of ${total} genes.`, "info");
    return;
  }

  if (event.type === "error") {
    if (event.result) {
      state.currentResult = event.result;
      state.currentRunGenesTotal = Math.max(
        state.currentRunGenesTotal,
        event.result.genes_annotated || 0,
      );
      renderAnnotationResult(event.result);
    } else if (state.currentResult) {
      state.currentResult.run_error = event.message;
      renderAnnotationResult(state.currentResult);
    }
    setMessage(formatRunError(event.message), "error");
    return;
  }

  if (event.type === "complete") {
    state.currentResult = event.result;
    state.currentRunGenesTotal = event.result.genes_annotated;
    renderAnnotationResult(event.result);
    if (event.result.run_error) {
      setMessage(formatRunError(event.result.run_error), "error");
    } else {
      setMessage("Annotation run finished. Review fields before exporting.", "info");
    }
  }
}

function appendAnnotationResult(annotation) {
  if (!state.currentResult) return;
  const annotations = state.currentResult.annotations || [];
  const existingIndex = annotations.findIndex((item) => item.gene === annotation.gene);
  if (existingIndex >= 0) {
    annotations[existingIndex] = annotation;
  } else {
    annotations.push(annotation);
  }
  state.currentResult.annotations = annotations;
  state.currentResult.genes_annotated = annotations.length;
  renderAnnotationResult(state.currentResult);
}

async function runBenchmark() {
  const localBackend = selectedLocalBackend();
  if (localBackend && !backendInstalled(localBackend)) {
    setMessage(`${backendLabel(localBackend)} was not detected on this server.`, "error");
    return;
  }

  setRunning(true);
  clearMessage();
  try {
    const response = await fetch("/v1/benchmark", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        local_backend: localBackend,
        no_judge: elements.noJudge.checked,
      }),
    });
    const payload = await readJsonResponse(response);
    state.benchmarkResult = payload;
    state.currentResult = payload.pipeline_result;
    state.currentRunGenesTotal = payload.pipeline_result.genes_annotated;
    renderBenchmarkMetrics(payload);
    renderAnnotationResult(payload.pipeline_result);
    setMessage("Benchmark finished. CSV export uses the benchmark pipeline result.", "info");
  } catch (error) {
    setMessage(formatRunError(error.message), "error");
  } finally {
    setRunning(false);
  }
}

function formatRunError(message) {
  if (String(message).includes("ONCOKB_API_TOKEN")) {
    return "OncoKB API token is required. Open Setup and paste your token from OncoKB account settings.";
  }
  if (String(message).toLowerCase().includes("insufficient")) {
    return String(message);
  }
  return message;
}

async function readJsonResponse(response) {
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const detail = payload && payload.detail ? payload.detail : response.statusText;
    throw new Error(detail || "Request failed");
  }
  return payload;
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
  if (annotation.cancer_associated_gene_tier === "Class I - Driver") {
    return {
      label: "High priority",
      tone: "high",
      title: "Classification indicates strong driver-level evidence.",
    };
  }
  if (annotation.cancer_associated_gene_tier === "Class II - Likely Driver") {
    return {
      label: "Moderate priority",
      tone: "medium",
      title: "Classification indicates functional cancer evidence, but not established driver-level evidence.",
    };
  }
  if (annotation.cancer_associated_gene_tier === "Class III - Cancer Relevant") {
    return {
      label: "Context only",
      tone: "context",
      title: "Classification indicates contextual or indirect cancer relevance.",
    };
  }
  return {
    label: "Review",
    tone: "neutral",
    title: "Review this result before export.",
  };
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
  if (annotation.cancer_associated_gene_tier === "Class I - Driver") {
    return {
      label: "Driver-level evidence",
      tone: "high",
      title: "Equivalent raw tier: Class I - Driver.",
    };
  }
  if (annotation.cancer_associated_gene_tier === "Class II - Likely Driver") {
    return {
      label: "Functional cancer evidence",
      tone: "medium",
      title: "Equivalent raw tier: Class II - Likely Driver.",
    };
  }
  if (annotation.cancer_associated_gene_tier === "Class III - Cancer Relevant") {
    return {
      label: "Contextual cancer evidence",
      tone: "context",
      title: "Equivalent raw tier: Class III - Cancer Relevant.",
    };
  }
  if (annotation.cancer_associated === true) {
    return {
      label: "Cancer associated",
      tone: "neutral",
      title: "Cancer-associated result without a tier assignment.",
    };
  }
  return null;
}

function compactBadges(annotation) {
  return [
    reviewPriority(annotation),
    evidenceSignal(annotation),
    annotation.in_oncokb
      ? {
          label: "OncoKB",
          tone: "high",
          title: "OncoKB membership lookup returned true.",
        }
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

function renderAnnotationResult(result) {
  const annotations = result.annotations || [];
  const hasAnnotations = annotations.length > 0;
  elements.exportCsv.disabled = !hasAnnotations;
  elements.exportJson.disabled = !hasAnnotations;
  updateGoogleSheetExportButton();
  elements.metricsGrid.classList.toggle("hidden", !state.benchmarkResult);
  const total = state.currentRunGenesTotal || result.genes_annotated;
  const progressText = total && result.genes_annotated < total
    ? `${result.genes_annotated} of ${total} genes annotated`
    : `${result.genes_annotated} genes annotated`;
  elements.runSummary.textContent =
    `${progressText} from ` +
    `${result.fusions_processed} fusion${result.fusions_processed === 1 ? "" : "s"}.`;

  if (!hasAnnotations) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `
      <h3>${escapeHtml(result.run_error ? "Run stopped" : "Annotations running")}</h3>
      <p>${escapeHtml(result.run_error || "Finished gene annotations will appear here as they complete.")}</p>
    `;
    elements.resultsWindow.replaceChildren(empty);
    return;
  }

  const list = document.createElement("div");
  list.className = "annotation-list";

  annotations.forEach((annotation, index) => {
    const card = document.createElement("article");
    card.className = "annotation-card";
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
}

function updateGoogleSheetExportButton() {
  const hasAnnotations = Boolean(
    state.currentResult &&
    state.currentResult.annotations &&
    state.currentResult.annotations.length,
  );
  elements.exportGoogleSheet.disabled = !hasAnnotations || !state.googleSheetsConfigured;
  elements.exportGoogleSheet.title = state.googleSheetsConfigured
    ? ""
    : "Configure Google Sheets in setup before exporting.";
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
    control.value = annotation[field] === null || annotation[field] === undefined
      ? ""
      : String(Boolean(annotation[field]));
  } else if (type === "long" || type === "list") {
    control = document.createElement("textarea");
    control.rows = type === "long" ? 4 : 2;
    control.value = type === "list"
      ? formatList(annotation[field])
      : annotation[field] || "";
  } else {
    control = document.createElement("input");
    control.type = type === "number" ? "number" : "text";
    if (field === "confidence") {
      control.step = "0.01";
      control.min = "0";
      control.max = "1";
    }
    control.value = annotation[field] === null || annotation[field] === undefined
      ? ""
      : annotation[field];
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

function renderBenchmarkMetrics(result) {
  const metrics = [
    ["Genes", result.n_genes],
    ["Cancer accuracy", result.categorical_metrics.cancer_associated?.accuracy],
    ["Tier macro F1", result.categorical_metrics.cancer_tier?.macro_f1],
    ["Citation F1", result.categorical_metrics.citations?.f1],
  ];
  elements.metricsGrid.replaceChildren(
    ...metrics.map(([label, value]) => {
      const card = document.createElement("div");
      card.className = "metric-card";
      card.innerHTML = `<span>${escapeHtml(label)}</span><strong>${formatMetric(value)}</strong>`;
      return card;
    }),
  );
  elements.metricsGrid.classList.remove("hidden");
}

function formatMetric(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "number") return value.toFixed(3);
  return String(value);
}

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
  const response = await fetch("/v1/export/annotation-results.csv", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(state.currentResult),
  });
  if (!response.ok) {
    const payload = await readJsonResponse(response);
    throw new Error(payload.detail || "CSV export failed");
  }
  const blob = await response.blob();
  downloadBlob(blob, "annotation_results.csv");
}

async function exportGoogleSheet() {
  if (!state.currentResult) return;
  const savedSpreadsheetId = localStorage.getItem("agcg.googleSheetId") || "";
  const spreadsheetId = window.prompt(
    "Google spreadsheet ID or URL",
    savedSpreadsheetId,
  );
  if (!spreadsheetId) return;

  const sheetName = window.prompt("Sheet tab name", "Annotation Results");
  if (!sheetName) return;

  const normalizedSpreadsheetId = parseSpreadsheetId(spreadsheetId);
  const confirmed = window.confirm(
    `Replace the contents of the "${sheetName}" tab in this spreadsheet?`,
  );
  if (!confirmed) return;

  const response = await fetch("/v1/export/google-sheet", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      spreadsheet_id: normalizedSpreadsheetId,
      sheet_name: sheetName,
      result: state.currentResult,
    }),
  });
  const payload = await readJsonResponse(response);
  localStorage.setItem("agcg.googleSheetId", normalizedSpreadsheetId);
  setMessage(
    `Exported ${payload.updated_rows} rows to ${payload.sheet_name}.`,
    "info",
  );
  window.open(payload.spreadsheet_url, "_blank", "noreferrer");
}

function parseSpreadsheetId(value) {
  const match = String(value).match(/\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/);
  return match ? match[1] : String(value).trim();
}

function exportJson() {
  if (!state.currentResult) return;
  const payload = state.benchmarkResult || state.currentResult;
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  });
  downloadBlob(blob, state.benchmarkResult ? "benchmark_report.json" : "results.json");
}

function bindEvents() {
  document.querySelectorAll(".segment").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
  elements.runButton.addEventListener("click", runSelectedMode);
  elements.openSetup.addEventListener("click", () => showSetupModal());
  elements.closeSetup.addEventListener("click", () => hideSetupModal());
  elements.dismissSetup.addEventListener("click", () => hideSetupModal({ remember: true }));
  elements.refreshBackends.addEventListener("click", () => fetchBackendStatus({ forceModal: true }));
  elements.prepareLocalPaths.addEventListener("click", prepareLocalPaths);
  elements.saveGoogleServiceAccount.addEventListener("click", saveGoogleServiceAccount);
  elements.saveNcbiApiKey.addEventListener("click", saveNCBIApiKey);
  elements.saveOncokbToken.addEventListener("click", saveOncoKBToken);
  elements.backendSelect.addEventListener("change", () => {
    state.backendSelectionTouched = true;
    renderSelectedBackendStatus();
    updateRunButtonState();
  });
  elements.installButtons.forEach((button) => {
    button.addEventListener("click", () => installBackend(button.dataset.backend));
  });
  elements.loginButtons.forEach((button) => {
    button.addEventListener("click", () => loginBackend(button.dataset.backend));
  });
  elements.exportJson.addEventListener("click", exportJson);
  elements.exportGoogleSheet.addEventListener("click", async () => {
    try {
      await exportGoogleSheet();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
  elements.exportCsv.addEventListener("click", async () => {
    try {
      await exportCsv();
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
}

bindEvents();
fetchBackendStatus();
fetchInstallerInfo();
fetchGoogleSheetsConfig();
fetchNCBIConfig();
fetchOncoKBConfig();

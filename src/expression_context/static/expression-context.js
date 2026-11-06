// Expression stats page (optional expression-context module; see
// src/expression_context/REMOVAL.md). API values are only ever text nodes.
(function (root) {
  "use strict";

  const API = "/v1/expression-context";
  const SYMBOL = /^[A-Z0-9][A-Z0-9._-]{0,63}$/;
  const PMID = /^\d{1,9}$/;
  const FLAG_LABELS = {
    tissue_of_origin_risk: "Tissue of origin",
    small_n: "Small n",
  };
  const REASON_LABELS = {
    stats: "statistics",
    precheck: "PubMed pre-check",
    literature: "literature",
  };

  function formatNumber(value, digits = 2) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toFixed(digits);
  }

  function formatQ(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    const q = Number(value);
    if (q === 0) return "0";
    return q < 0.001 ? q.toExponential(1) : q.toFixed(3);
  }

  function pmidUrl(pmid) {
    const text = String(pmid).trim();
    return PMID.test(text) ? `https://pubmed.ncbi.nlm.nih.gov/${text}/` : null;
  }

  function friendlyError(status, gene) {
    if (status === 401) return "Your session has expired. Redirecting you to sign in…";
    if (status === 403) return "Your account isn't active yet. Please contact your AGCG administrator.";
    if (status === 404) return `${gene} is not in the expression statistics bundle.`;
    if (status === 422) return "Enter a gene symbol (letters, digits, '.', '_' or '-').";
    if (status === 503) return "Expression statistics are not loaded yet. Please try again in a few minutes.";
    if (status >= 500) return "The server couldn't load expression statistics right now. Please try again later.";
    return "Couldn't load expression statistics. Check your connection and try again.";
  }

  function createExpressionContextPage({ document: doc, window: win, fetch: fetchFn, wait, pollMs = 2000, maxPolls = 600 }) {
    const el = {};
    for (const id of [
      "ec-form", "ec-gene", "ec-submit", "ec-error", "ec-loading", "ec-result", "ec-gene-title",
      "ec-summary", "ec-run", "ec-job-status", "ec-tbody", "ec-provenance", "ec-provenance-status",
    ]) {
      el[id] = doc.getElementById(id);
    }
    const sleep = wait || ((ms) => new Promise((done) => win.setTimeout(done, ms)));
    const state = { view: null, loading: false, jobRunning: false };

    function show(node, visible) { if (node) node.classList.toggle("hidden", !visible); }

    function setError(message) {
      el["ec-error"].textContent = message || "";
      show(el["ec-error"], Boolean(message));
    }

    function node(tag, text, className) {
      const element = doc.createElement(tag);
      if (text !== undefined && text !== null) element.textContent = String(text);
      if (className) element.setAttribute("class", className);
      return element;
    }

    function badge(text, kind) { return node("span", text, `ec-badge ec-${kind}`); }

    function cell(row, content, className) {
      const td = doc.createElement("td");
      if (className) td.setAttribute("class", className);
      if (content === null || content === undefined || typeof content === "string" || typeof content === "number") {
        td.textContent = content === null || content === undefined ? "" : String(content);
      } else {
        for (const child of [].concat(content)) td.appendChild(child);
      }
      row.appendChild(td);
      return td;
    }

    function pmidLinks(pmids) {
      const wrap = node("span", null, "ec-pmids");
      for (const pmid of pmids || []) {
        const url = pmidUrl(pmid);
        if (url) {
          const link = node("a", `PMID ${String(pmid).trim()}`);
          link.setAttribute("href", url);
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noopener noreferrer");
          wrap.appendChild(link);
        } else {
          wrap.appendChild(node("span", String(pmid)));
        }
      }
      return wrap;
    }

    function literatureCell(pair) {
      const verdict = pair.literature;
      if (!verdict) {
        return [badge(pair.literature_eligible ? "Not checked yet" : "—", "neutral")];
      }
      const parts = [verdict.supported ? badge("Supported", "pass") : badge("Not supported", "fail")];
      if (!verdict.supported) {
        parts.push(node("span", verdict.insufficient_evidence ? " insufficient evidence" : " no cited PMIDs", "ec-neutral"));
      }
      if ((verdict.pmids || []).length) {
        const block = node("div");
        block.appendChild(pmidLinks(verdict.pmids));
        parts.push(block);
      }
      if (verdict.rationale) parts.push(node("div", verdict.rationale, "ec-rationale"));
      return parts;
    }

    function resultCell(pair) {
      if (pair.status === "surfaced") return [badge("Surfaced", "pass")];
      if (pair.status === "literature_pending") return [badge("Awaiting literature check", "warn")];
      const reason = REASON_LABELS[pair.exclusion_reason] || pair.exclusion_reason || "unknown";
      return [badge(`Excluded: ${reason}`, "neutral")];
    }

    function precheckCell(pair) {
      const count = pair.stats.pubmed_count;
      const suffix = count === null || count === undefined ? "" : ` (${count})`;
      if (pair.precheck === "pass") return [badge(`Pass${suffix}`, "pass")];
      if (pair.precheck === "fail") return [badge(`Fail${suffix}`, "fail")];
      // Counts past source.precheck_expires_at: they don't exclude the pair.
      if (pair.precheck === "expired") return [badge(`Pre-check expired${suffix}`, "warn")];
      return [badge("Not run", "neutral")];
    }

    function flagsCell(flags) {
      return (flags || []).map((flag) => badge(FLAG_LABELS[flag] || flag, flag === "tissue_of_origin_risk" ? "warn" : "neutral"));
    }

    function renderRow(pair) {
      const row = doc.createElement("tr");
      if (pair.status === "surfaced") row.setAttribute("class", "ec-row-surfaced");
      const s = pair.stats;
      const name = pair.cancer_type_name ? `${pair.cancer_type_name} (${pair.oncotree_code})` : pair.oncotree_code;
      cell(row, name);
      cell(row, `${s.n} (${s.n_rest})`, "ec-num");
      cell(row, `${formatNumber(s.median_log2)} (${formatNumber(s.rest_median_log2)})`, "ec-num");
      cell(row, formatNumber(s.log2fc), "ec-num");
      cell(row, formatNumber(s.cliffs_delta), "ec-num");
      cell(row, formatNumber(s.auc), "ec-num");
      cell(row, formatQ(s.q_value), "ec-num");
      cell(row, [pair.stats_pass ? badge("Pass", "pass") : badge("Fail", "fail")]);
      cell(row, precheckCell(pair));
      cell(row, flagsCell(s.flags));
      cell(row, literatureCell(pair));
      cell(row, resultCell(pair));
      return row;
    }

    function renderProvenance(bundle, refresh) {
      const dl = el["ec-provenance"];
      dl.replaceChildren();
      if (!bundle) {
        el["ec-provenance-status"].textContent = "No statistics bundle is loaded yet.";
      } else {
        el["ec-provenance-status"].textContent = "";
      }
      const add = (label, value, href) => {
        if (value === null || value === undefined || value === "") return;
        dl.appendChild(node("dt", label));
        const dd = node("dd");
        if (href) {
          const link = node("a", value);
          link.setAttribute("href", href);
          link.setAttribute("target", "_blank");
          link.setAttribute("rel", "noopener noreferrer");
          dd.appendChild(link);
        } else {
          dd.textContent = String(value);
        }
        dl.appendChild(dd);
      };
      if (bundle) {
        const source = bundle.source || {};
        const releaseUrl = typeof bundle.release_url === "string" && bundle.release_url.startsWith("https://github.com/")
          ? bundle.release_url : null;
        add("Bundle", bundle.bundle_tag, releaseUrl);
        add("Release ID", bundle.release_id);
        add("Method version", bundle.method_version);
        add("PubMed snapshot", bundle.pubmed_snapshot_date);
        add("Genes", bundle.gene_count);
        add("Datahub commit", source.datahub_commit);
        add("Generated", source.generated_at);
        add("PubMed pre-check expires", source.precheck_expires_at);
        add("Published", bundle.published_at);
        add("Loaded", bundle.loaded_at);
      }
      if (refresh) {
        add("Last checked", refresh.last_checked_at);
        if (refresh.pinned_bundle_tag) add("Pinned to", refresh.pinned_bundle_tag);
        const errorIsCurrent = refresh.last_error && (!refresh.last_checked_at || refresh.last_error_at > refresh.last_checked_at);
        if (errorIsCurrent) add("Last refresh error", refresh.last_error);
      }
    }

    function render(view) {
      state.view = view;
      const title = view.query && view.query.trim().toUpperCase() !== view.hugo_symbol
        ? `${view.hugo_symbol} (from ${view.query.trim()})`
        : view.hugo_symbol;
      el["ec-gene-title"].textContent = view.entrez_gene_id ? `${title} · Entrez ${view.entrez_gene_id}` : title;
      const summary = view.summary || {};
      el["ec-summary"].textContent =
        `${summary.surfaced || 0} surfaced · ${summary.literature_pending || 0} awaiting literature check · ` +
        `${summary.excluded || 0} excluded`;
      show(el["ec-run"], (summary.literature_pending || 0) > 0 && !state.jobRunning);
      el["ec-tbody"].replaceChildren();
      for (const pair of view.pairs || []) el["ec-tbody"].appendChild(renderRow(pair));
      show(el["ec-result"], true);
      renderProvenance(view.bundle, null);
    }

    async function getJson(url, options) {
      let response;
      try {
        response = await fetchFn(url, { credentials: "same-origin", headers: { Accept: "application/json" }, ...options });
      } catch (_) {
        return { status: 0, body: null };
      }
      let body = null;
      try { body = await response.json(); } catch (_) { body = null; }
      return { status: response.status, body };
    }

    function redirectToLogin() {
      const target = `${win.location.pathname || "/expression-context"}${win.location.search || ""}`;
      win.location.href = `/login?redirect_to=${encodeURIComponent(target)}`;
    }

    async function loadBundleStatus() {
      const { status, body } = await getJson(`${API}/bundle`);
      if (status === 200 && body) {
        if (!state.view) renderProvenance(body.active, body.refresh);
        else renderProvenance(state.view.bundle, body.refresh);
      } else if (status === 401) {
        redirectToLogin();
      } else {
        el["ec-provenance-status"].textContent = "Couldn't load the bundle status.";
      }
    }

    async function lookup(rawGene) {
      const gene = String(rawGene || "").trim().toUpperCase();
      setError("");
      if (!SYMBOL.test(gene)) {
        setError(friendlyError(422, gene));
        return;
      }
      state.loading = true;
      show(el["ec-loading"], true);
      el["ec-submit"].disabled = true;
      try {
        const { status, body } = await getJson(`${API}/stats/${encodeURIComponent(gene)}`);
        if (status === 200 && body) {
          render(body);
          if (win.history && typeof win.history.replaceState === "function") {
            win.history.replaceState(null, "", `?gene=${encodeURIComponent(gene)}`);
          }
          return;
        }
        show(el["ec-result"], false);
        setError(friendlyError(status, gene));
        if (status === 401) redirectToLogin();
      } finally {
        state.loading = false;
        show(el["ec-loading"], false);
        el["ec-submit"].disabled = false;
      }
    }

    async function runLiteratureCheck() {
      const view = state.view;
      if (!view || state.jobRunning) return;
      state.jobRunning = true;
      show(el["ec-run"], false);
      el["ec-job-status"].textContent = "Literature check queued…";
      try {
        const { status, body } = await getJson(`${API}/jobs`, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ gene: view.hugo_symbol }),
        });
        if (status !== 202 || !body || typeof body.status_url !== "string" || !body.status_url.startsWith(`${API}/jobs/`)) {
          el["ec-job-status"].textContent = "";
          setError(friendlyError(status, view.hugo_symbol));
          if (status === 401) redirectToLogin();
          return;
        }
        for (let poll = 0; poll < maxPolls; poll += 1) {
          const job = await getJson(body.status_url);
          if (job.status === 200 && job.body) {
            if (job.body.status === "succeeded") {
              const errors = (job.body.pair_errors || []).length;
              el["ec-job-status"].textContent =
                `Literature check finished: ${job.body.literature_evaluated} evaluated, ` +
                `${job.body.literature_reused} reused${errors ? `, ${errors} failed (retry later)` : ""}.`;
              state.jobRunning = false;
              await lookup(view.hugo_symbol);
              return;
            }
            if (job.body.status === "failed") {
              el["ec-job-status"].textContent = "";
              setError("The literature check failed. Please try again later.");
              return;
            }
            el["ec-job-status"].textContent = "Literature check running… this can take several minutes.";
          } else if (job.status === 401) {
            redirectToLogin();
            return;
          }
          await sleep(pollMs);
        }
        el["ec-job-status"].textContent = "Still running — reload the page later to see the results.";
      } finally {
        state.jobRunning = false;
        if (state.view) show(el["ec-run"], (state.view.summary.literature_pending || 0) > 0);
      }
    }

    el["ec-form"].addEventListener("submit", (event) => {
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      return lookup(el["ec-gene"].value);
    });
    el["ec-run"].addEventListener("click", () => runLiteratureCheck());

    async function init() {
      const params = new URLSearchParams(win.location.search || "");
      const gene = params.get("gene");
      const tasks = [loadBundleStatus()];
      if (gene) {
        el["ec-gene"].value = gene;
        tasks.push(lookup(gene));
      }
      await Promise.all(tasks);
    }

    return { init, lookup, runLiteratureCheck, state };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createExpressionContextPage, formatNumber, formatQ, pmidUrl };
  } else {
    createExpressionContextPage({ document: root.document, window: root, fetch: root.fetch.bind(root) }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);

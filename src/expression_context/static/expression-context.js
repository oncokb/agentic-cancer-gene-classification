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
  // ACGC's shared .review-badge tones (src/static/styles.css).
  const BADGE_TONES = {
    pass: "high",
    info: "context",
    fail: "quality-critical",
    warn: "medium",
    neutral: "low",
    candidate: "medium",
    strong: "high",
    moderate: "medium",
    alteration: "context",
  };
  const REASON_LABELS = {
    stats: "statistics",
    precheck: "PubMed pre-check",
    literature: "no literature support",
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

  // Public cBioPortal expression plot. `horizontal` is the clinical attribute
  // shown on the x-axis ("CANCER_STUDY" = study of origin, "CANCER_TYPE_DETAILED"
  // = mRNA vs Dx). CANCER_STUDY with exactly one study silently opens the wrong
  // plot (Mutation Count), so fall back to the detailed-diagnosis view.
  function expressionURL(symbol, studyIds, horizontal) {
    const selected = horizontal === "CANCER_STUDY" && studyIds.length === 1
      ? "CANCER_TYPE_DETAILED"
      : horizontal;
    const query = new URLSearchParams({
      cancer_study_list: studyIds.join(","),
      gene_list: symbol,
      case_set_id: "all",
      plots_horz_selection: JSON.stringify({ dataType: "clinical_attribute", selectedDataSourceOption: selected }),
      plots_vert_selection: JSON.stringify({ dataType: "MRNA_EXPRESSION", selectedDataSourceOption: "rna_seq_v2_mrna", logScale: "true" }),
    });
    return `https://www.cbioportal.org/results/plots?${query}`;
  }

  function exportURL(gene, includeExcluded = false) {
    const encoded = encodeURIComponent(String(gene || "").trim());
    return includeExcluded
      ? `${API}/genes/${encoded}/export.csv?include_excluded=true`
      : `${API}/genes/${encoded}/export.csv`;
  }

  function goldenSetExportURL(gene) {
    const encoded = encodeURIComponent(String(gene || "").trim());
    return `${API}/golden-set.csv?genes=${encoded}`;
  }

  function curatorSheetExportURL(gene) {
    const encoded = encodeURIComponent(String(gene || "").trim());
    return `${API}/genes/${encoded}/curator-sheet.csv`;
  }

  function batchExportURL(batchId, includeExcluded = false) {
    const encoded = encodeURIComponent(String(batchId || "").trim());
    return includeExcluded
      ? `${API}/batches/${encoded}/export.csv?include_excluded=true`
      : `${API}/batches/${encoded}/export.csv`;
  }

  function batchGoldenSetExportURL(batchId) {
    const encoded = encodeURIComponent(String(batchId || "").trim());
    return `${API}/batches/${encoded}/golden-set.csv`;
  }

  function batchCuratorSheetExportURL(batchId) {
    const encoded = encodeURIComponent(String(batchId || "").trim());
    return `${API}/batches/${encoded}/curator-sheet.csv`;
  }

  function parseBatchText(text) {
    const raw = String(text || "");
    const tokens = raw.split(/[\r\n,\t]+/).map((s) => s.trim()).filter(Boolean);
    const seen = new Set();
    const unique = [];
    let duplicates = 0;
    for (const token of tokens) {
      const upper = token.toUpperCase();
      if (seen.has(upper)) {
        duplicates += 1;
      } else {
        seen.add(upper);
        unique.push(token);
      }
    }
    return { tokens, unique, duplicates };
  }

  function parseUploadedFileColumn(text, filename = "") {
    const lines = String(text || "").split(/\r?\n/);
    const isDelimited = /\.(csv|tsv)$/i.test(String(filename || ""));
    const genes = [];
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i].trim();
      if (!line) continue;
      let first = line;
      if (isDelimited || line.includes("\t") || line.includes(",")) {
        const delim = line.includes("\t") ? "\t" : ",";
        first = line.split(delim)[0].trim().replace(/^["']|["']$/g, "").trim();
      }
      if (!first) continue;
      if (i === 0 && /^(gene|genes|hugo_symbol|symbol|gene_symbol)$/i.test(first)) {
        continue;
      }
      genes.push(first);
    }
    return genes;
  }

  function friendlyError(status, gene) {
    if (status === 401) return "Your session has expired. Redirecting you to sign in…";
    if (status === 403) return "Your account isn't active yet. Please contact your AGCG administrator.";
    if (status === 404) return `${gene} is not in the current expression data.`;
    if (status === 422) return "Enter a gene symbol (letters, digits, '.', '_' or '-').";
    if (status === 503) return "Expression data hasn't loaded yet. Try again in a few minutes.";
    if (status >= 500) return "The server couldn't load expression statistics right now. Please try again later.";
    return "Couldn't load expression statistics. Check your connection and try again.";
  }

  function createExpressionContextPage({ document: doc, window: win, fetch: fetchFn, wait, pollMs = 2000, maxPolls = 600 }) {
    const el = {};
    for (const id of [
      "ec-form", "ec-gene", "ec-submit", "ec-error", "ec-loading", "ec-result", "ec-gene-title",
      "ec-summary", "ec-release", "ec-cbio", "ec-run", "ec-export", "ec-export-golden", "ec-export-curator",
      "ec-view-shown", "ec-view-all", "ec-view-count", "ec-table", "ec-table-empty", "ec-job-status", "ec-tbody",
      "ec-dmt-index",
      "ec-tab-single", "ec-tab-batch", "ec-single-mode", "ec-batch-mode", "ec-batch-form",
      "ec-batch-input", "ec-batch-upload-btn", "ec-batch-upload", "ec-batch-preview", "ec-batch-submit",
      "ec-batch-result", "ec-batch-title", "ec-batch-summary", "ec-batch-counts", "ec-batch-cancel",
      "ec-batch-progress", "ec-batch-export-shown", "ec-batch-export-all",
      "ec-batch-export", "ec-batch-export-golden", "ec-batch-export-curator", "ec-batch-genes",
    ]) {
      el[id] = doc.getElementById(id);
    }
    const sleep = wait || ((ms) => new Promise((done) => win.setTimeout(done, ms)));
    // showAll: the "All cancer types" view (excluded pairs listed, CSV include_excluded=true).
    const state = {
      view: null,
      loading: false,
      jobRunning: false,
      expanded: new Set(),
      alterationExpanded: new Set(),
      showAll: false,
      rows: [],
      mode: "single",
      batch: null,
      batchShowAll: false,
      batchExpanded: new Set(),
      batchGeneViews: new Map(),
      batchPolling: false,
    };

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

    function badge(text, kind) {
      return node("span", text, `review-badge ec-badge ec-${kind} ${BADGE_TONES[kind] || "low"}`);
    }

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
      // Tumor-specific verdicts (v2) carry `verdict`; older ones don't.
      const tumorSpecific = typeof verdict.verdict === "string";
      const tumor = verdict.cancer_type_name || pair.cancer_type_name || pair.oncotree_code;
      const parts = [verdict.supported
        ? badge(tumorSpecific ? `Supported in ${tumor}` : "Supported", "pass")
        : badge("Not supported", "fail")];
      if (!verdict.supported) {
        let why = verdict.insufficient_evidence ? " insufficient evidence" : " no cited PMIDs";
        if (tumorSpecific && verdict.verdict === "not_supported") why = " evidence against this tumor type";
        else if (tumorSpecific && !verdict.insufficient_evidence) why = " no tumor-specific PMIDs";
        parts.push(node("span", why, "ec-neutral"));
      }
      if ((verdict.pmids || []).length) {
        const block = node("div");
        if (tumorSpecific) block.appendChild(node("span", "Tumor-specific: ", "ec-neutral"));
        block.appendChild(pmidLinks(verdict.pmids));
        parts.push(block);
      }
      if (verdict.rationale) parts.push(node("div", verdict.rationale, "ec-rationale"));
      return parts;
    }

    function hasSubgroupStats(s) {
      return ["subgroup_high_count", "subgroup_high_fraction", "subgroup_high_fraction_ratio",
        "p95_log2_diff", "subgroup_q"].some((key) => s[key] !== null && s[key] !== undefined);
    }

    function subgroupText(s) {
      const count = s.subgroup_high_count === null || s.subgroup_high_count === undefined
        ? "—" : String(s.subgroup_high_count);
      const fraction = s.subgroup_high_fraction === null || s.subgroup_high_fraction === undefined
        ? "—" : `${formatNumber(Number(s.subgroup_high_fraction) * 100, 1)}%`;
      return `Subgroup: high n ${count} (${fraction}) · ratio ${formatNumber(s.subgroup_high_fraction_ratio)}× · ` +
        `p95 Δlog2 ${formatNumber(s.p95_log2_diff)} · q ${formatQ(s.subgroup_q)}`;
    }

    function statsCell(pair) {
      const parts = [pair.stats_pass ? badge("Pass", "pass") : badge("Fail", "fail")];
      if (pair.stats_pass && pair.enrichment) parts.push(badge(pair.enrichment, "info"));
      // Lineage enrichment blocked by broad expression (method m3): shown, never passing.
      if (pair.broad_blocked) parts.push(badge("Broadly expressed", "warn"));
      return parts;
    }

    function resultCell(pair) {
      if (pair.status === "surfaced") {
        const parts = [badge("Surfaced", "pass")];
        if (["Strong", "Moderate"].includes(pair.confidence_label)) {
          const label = badge(pair.confidence_label, pair.confidence_label.toLowerCase());
          label.setAttribute("title", pair.confidence_label === "Moderate"
            ? "Statistically significant and literature-supported, but below the strong effect-size bar; worth a closer look."
            : "Meets strong effect-size criteria.");
          parts.push(label);
        }
        return parts;
      }
      if (pair.exclusion_reason === "purity" || pair.status === "candidate") {
        const concern = badge("Purity concern", "candidate");
        concern.setAttribute("title", pair.stats.purity_check === "demoted"
          ? "Expression may come from non-tumor cells (falls with tumor purity)"
          : "Purity status is missing or unknown; high-tier pairs require a known retaining state.");
        return [concern];
      }
      if (pair.status === "literature_pending") return [badge("Awaiting literature check", "warn")];
      const reason = REASON_LABELS[pair.exclusion_reason] || pair.exclusion_reason || "unknown";
      return [badge(`Excluded: ${reason}`, "neutral")];
    }

    function isExcluded(pair) { return pair.status === "excluded"; }
    function isCandidate(pair) { return pair.status === "candidate"; }
    // Hidden in the "Shown only" view: excluded pairs and candidates.
    function isHidden(pair) { return isExcluded(pair) || isCandidate(pair); }

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

    function cbioLink(label, symbol, studyIds, horizontal) {
      const link = node("a", label);
      link.setAttribute("href", expressionURL(symbol, studyIds, horizontal));
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      return link;
    }

    function cbioportalCell(pair, symbol) {
      if (!pair.study_id || !symbol) return [];
      return [cbioLink("mRNA vs Dx", symbol, [pair.study_id], "CANCER_TYPE_DETAILED")];
    }

    function renderCbioCompare(view) {
      const container = el["ec-cbio"];
      container.replaceChildren();
      const studyIds = [];
      const seen = new Set();
      for (const pair of view.pairs || []) {
        if (!pair.study_id || seen.has(pair.study_id)) continue;
        seen.add(pair.study_id);
        studyIds.push(pair.study_id);
      }
      if (!studyIds.length || !view.hugo_symbol) return;
      container.appendChild(cbioLink(
        "Compare across all 32 cancer types in cBioPortal",
        view.hugo_symbol, studyIds, "CANCER_STUDY",
      ));
      container.appendChild(cbioLink(
        "mRNA vs Dx across all 32 (detailed diagnosis)",
        view.hugo_symbol, studyIds, "CANCER_TYPE_DETAILED",
      ));
    }

    const alterationNames = ["Point Mutation", "Structural Variant", "Copy Number Alteration", "Expression"];

    // New APIs derive the canonical field from stats; accept older stats-only responses.
    function alterationsFor(pair) { return pair.alterations || pair.stats.alterations; }

    function alterationLabel(name, category) {
      if (name !== "Copy Number Alteration" || !category) return name;
      const directions = (category.enriched_directions || []).filter((d) => ["amp", "del"].includes(d));
      if (!directions.length && ["amp", "del"].includes(category.direction)) directions.push(category.direction);
      return directions.length ? `${name} (${directions.join(", ")})` : name;
    }

    function alterationBadges(pair) {
      const alterations = alterationsFor(pair);
      const categories = alterations && alterations.categories || {};
      const names = alterationNames.filter((name) =>
        (name === "Expression" && pair.status === "surfaced") ||
        (alterations && (alterations.alteration_types_enriched || []).includes(name) &&
         (!categories[name] || categories[name].status === "enriched")));
      const badges = names.map((name) => {
        const key = `${pair.oncotree_code}|${name}`;
        const panelId = `ec-alteration-${pair.oncotree_code}-${name.replace(/\W+/g, "-")}`;
        const wrap = node("span", null, "ec-alteration-item");
        const button = node("button", alterationLabel(name, categories[name]),
          `review-badge ec-badge ec-alteration context ec-alteration-button`);
        button.setAttribute("type", "button");
        button.setAttribute("aria-expanded", state.alterationExpanded.has(key) ? "true" : "false");
        button.setAttribute("aria-controls", panelId);
        const panel = node("span", null, "ec-alteration-panel");
        panel.setAttribute("id", panelId);
        const verdict = pair.literature;
        if (verdict && verdict.alteration_pmids == null) {
          panel.textContent = "Re-run literature check to see alteration-specific papers";
        } else {
          const pmids = verdict && verdict.alteration_pmids && verdict.alteration_pmids[name] || [];
          const allowed = new Set(verdict && verdict.pmids || []);
          const papers = new Map((verdict && verdict.papers || []).map((paper) => [paper.pmid, paper]));
          let count = 0;
          for (const pmid of pmids) {
            const url = allowed.has(pmid) && pmidUrl(pmid);
            if (!url) continue;
            const paper = papers.get(pmid);
            const link = node("a", paper && paper.title ? `${paper.title} (PMID ${pmid})` : `PMID ${pmid}`);
            link.setAttribute("href", url);
            link.setAttribute("target", "_blank");
            link.setAttribute("rel", "noopener noreferrer");
            panel.appendChild(link);
            count += 1;
          }
          if (!count) panel.textContent = "No alteration-specific paper found";
        }
        show(panel, state.alterationExpanded.has(key));
        button.addEventListener("click", () => {
          const next = !state.alterationExpanded.has(key);
          if (next) state.alterationExpanded.add(key); else state.alterationExpanded.delete(key);
          button.setAttribute("aria-expanded", next ? "true" : "false");
          show(panel, next);
        });
        wrap.appendChild(button);
        wrap.appendChild(panel);
        return wrap;
      });
      for (const [category, entry] of Object.entries(pair.stats.alteration_high_expression || {})) {
        if (entry.status === "associated_high_expression") {
          badges.push(node("span", `${category}: High-expression assoc.`, "ec-badge ec-high-expression"));
        } else if (entry.status === "anecdotal") {
          badges.push(node("span", `${category}: anecdotal (1–2 tumors)`, "ec-muted"));
        }
      }
      return badges.length ? badges : "—";
    }

    function alterationCounts(counts, fallbackProfiled) {
      const count = (n) => n == null ? "—" : String(n);
      const frequency = counts.frequency == null ? "—" : `${formatNumber(counts.frequency * 100)}%`;
      return `${count(counts.n_events)}/${count(counts.n_profiled ?? fallbackProfiled)} events/profiled; frequency ${frequency}`;
    }

    function renderDetailRow(pair, detailId, expanded) {
      const s = pair.stats;
      const detailRow = doc.createElement("tr");
      detailRow.setAttribute("id", detailId);
      detailRow.setAttribute("class", "ec-detail-row");
      show(detailRow, expanded);
      const td = doc.createElement("td");
      td.setAttribute("colspan", "8");
      td.setAttribute("class", "ec-detail-cell");
      let dl = node("dl", null, "ec-details-list");
      const addDetail = (label, value, tooltip, ddClass) => {
        const dt = node("dt", label);
        if (tooltip) {
          dt.setAttribute("title", tooltip);
          dt.appendChild(node("span", ` (${tooltip})`, "ec-detail-hint"));
        }
        const dd = doc.createElement("dd");
        if (ddClass) dd.setAttribute("class", ddClass);
        if (typeof value === "string" || typeof value === "number") {
          dd.textContent = String(value);
        } else {
          for (const child of [].concat(value)) dd.appendChild(child);
        }
        dl.appendChild(dt);
        dl.appendChild(dd);
      };
      addDetail(
        "n (rest)",
        `${s.n} (${s.n_rest})`,
        "Primary tumors in this type (all other TCGA tumors)",
        "ec-num",
      );
      addDetail(
        "Median (rest)",
        `${formatNumber(s.median_log2)} (${formatNumber(s.rest_median_log2)})`,
        "Median log2(RSEM + 1) in this type (all other tumors)",
        "ec-num",
      );
      addDetail(
        "log2FC",
        formatNumber(s.log2fc),
        "Floor-clamped median difference, log2",
        "ec-num",
      );
      addDetail("Cliff's delta", formatNumber(s.cliffs_delta), "Cliff's δ", "ec-num");
      addDetail("AUC", formatNumber(s.auc), null, "ec-num");
      addDetail(
        "q",
        formatQ(s.q_value),
        "Benjamini–Hochberg q across the cancer types for this gene",
        "ec-num",
      );
      addDetail(
        "Subgroup statistics",
        hasSubgroupStats(s) ? subgroupText(s) : "—",
        null,
        "ec-subgroup",
      );
      if (["not_applicable", "unassessed"].includes(s.purity_check)) {
        addDetail("Tumor purity", s.purity_check === "not_applicable"
          ? "Not applicable to this cohort; unassessed by this gate, not validated"
          : "Insufficient purity coverage; unassessed by this gate, not validated");
      }
      addDetail(
        "PubMed pre-check",
        precheckCell(pair),
        "PubMed count for the gene and tumor type",
      );
      const alterations = alterationsFor(pair);
      if (alterations) {
        td.appendChild(dl);
        dl = node("dl", null, "ec-details-list");
        td.appendChild(node("h4", "Alterations", "ec-alterations-heading"));
        const categories = alterations.categories || {};
        for (const name of alterationNames) {
          const category = categories[name];
          if (!category) continue;
          const status = (category.status || "unknown").replace(/_/g, " ");
          let text = `${status}; ${alterationCounts(category)}`;
          if (category.reason) text += `; ${category.reason}`;
          if (name === "Copy Number Alteration") {
            for (const direction of ["amp", "del"]) {
              if (category[direction]) text += `; ${direction}: ${alterationCounts(category[direction], category.n_profiled)}`;
            }
          }
          addDetail(name, text);
        }
      }
      for (const [name, entry] of Object.entries(pair.stats.alteration_high_expression || {})) {
        const status = entry.status.replace(/_/g, " ");
        let text = `${status}; ${entry.n_altered}/${entry.n_profiled} altered/profiled; ` +
          `Δ median ${formatNumber(entry.delta_median)} log2; MW q ${formatQ(entry.q)}; ` +
          `top-decile k ${entry.k_top10}; OR ${entry.or_top10 ?? "—"}; ` +
          `Fisher p ${formatQ(entry.p_top10)}; altered fraction ${formatNumber(entry.frac_top10_altered)}`;
        if (entry.reason) text += `; ${entry.reason}`;
        addDetail(`${name}: high-expression association`, text);
      }
      td.appendChild(dl);
      detailRow.appendChild(td);
      return detailRow;
    }

    function renderRows(pair, symbol) {
      const row = doc.createElement("tr");
      if (pair.status === "surfaced") row.setAttribute("class", "ec-row-surfaced");
      else if (isCandidate(pair)) row.setAttribute("class", "ec-row-excluded ec-row-candidate");
      else if (isExcluded(pair)) row.setAttribute("class", "ec-row-excluded");
      const dmtRows = (state.view.dmt_adc_index || []).filter((item) =>
        (item.tcga_codes || []).includes(pair.oncotree_code));
      if (dmtRows.length) row.classList.toggle("ec-row-dmt", true);
      const s = pair.stats;
      const code = pair.oncotree_code;
      const name = pair.cancer_type_name ? `${pair.cancer_type_name} (${code})` : code;
      const detailId = `ec-details-${code}`;
      const expanded = state.expanded.has(code);
      const toggleBtn = node("button", "Details", "ec-details-toggle");
      toggleBtn.setAttribute("type", "button");
      toggleBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggleBtn.setAttribute("aria-controls", detailId);
      const detailRow = renderDetailRow(pair, detailId, expanded);
      toggleBtn.addEventListener("click", () => {
        const next = !state.expanded.has(code);
        if (next) state.expanded.add(code);
        else state.expanded.delete(code);
        toggleBtn.setAttribute("aria-expanded", next ? "true" : "false");
        show(detailRow, next);
      });
      const cancerCell = cell(row, name);
      if (dmtRows.length) {
        const info = node("details", null, "ec-dmt-detail");
        info.appendChild(node("summary", "DMT ADC interest", "review-badge ec-badge context"));
        for (const item of dmtRows) {
          info.appendChild(node("div", `${item.tissue_type}${item.from ? ` · From: ${item.from}` : ""}${item.notes ? ` · ${item.notes}` : ""}`));
        }
        cancerCell.appendChild(info);
      }
      cell(row, statsCell(pair));
      cell(row, flagsCell(s.flags));
      cell(row, alterationBadges(pair), "ec-alterations");
      cell(row, literatureCell(pair));
      cell(row, resultCell(pair));
      cell(row, cbioportalCell(pair, symbol));
      cell(row, toggleBtn, "ec-toggle-cell");
      state.rows.push({ pair, code, row, detailRow });
      return [row, detailRow];
    }

    function hasStoredResult(view) {
      if (!view) return false;
      if (typeof view.has_stored_result === "boolean") return view.has_stored_result;
      const pairs = view.pairs || [];
      return pairs.some((p) => p.literature != null || (p.status !== "literature_pending" && p.exclusion_reason !== "pending"));
    }

    function updateExportLink() {
      const view = state.view;
      const link = el["ec-export"];
      const goldenLink = el["ec-export-golden"];
      const curatorLink = el["ec-export-curator"];
      const symbol = view && (view.hugo_symbol || view.query) ? String(view.hugo_symbol || view.query).trim() : "";
      if (curatorLink) {
        if (symbol) {
          curatorLink.setAttribute("href", curatorSheetExportURL(symbol));
          show(curatorLink, true);
        } else {
          show(curatorLink, false);
        }
      }
      if (!symbol || !hasStoredResult(view)) {
        show(link, false);
        show(goldenLink, false);
        return;
      }
      const includeExcluded = state.showAll;
      if (link) {
        link.setAttribute("href", exportURL(symbol, includeExcluded));
        link.setAttribute(
          "title",
          includeExcluded ? "CSV of every cancer type, including excluded ones" : "CSV of the cancer types shown",
        );
        show(link, true);
      }
      if (goldenLink) {
        goldenLink.setAttribute("href", goldenSetExportURL(symbol));
        show(goldenLink, true);
      }
    }

    // Applies the "Shown only" / "All cancer types" view to the toggle, the
    // table rows, the count and the CSV link.
    function applyView() {
      const showAll = state.showAll;
      for (const [id, pressed] of [["ec-view-shown", !showAll], ["ec-view-all", showAll]]) {
        const button = el[id];
        if (!button) continue;
        button.setAttribute("aria-pressed", pressed ? "true" : "false");
        button.classList.toggle("active", pressed);
      }
      for (const { pair, code, row, detailRow } of state.rows) {
        const visible = showAll || !isHidden(pair);
        show(row, visible);
        show(detailRow, visible && state.expanded.has(code));
      }
      const pairs = (state.view && state.view.pairs) || [];
      const excluded = pairs.filter(isExcluded).length;
      const candidates = pairs.filter(isCandidate).length;
      const hidden = excluded + candidates;
      const shown = pairs.length - hidden;
      const needs = candidates ? ` · ${candidates} purity ${candidates === 1 ? "concern" : "concerns"}` : "";
      if (el["ec-view-count"]) {
        el["ec-view-count"].textContent = showAll
          ? `${shown} shown · ${excluded} excluded${needs}`
          : `${shown} shown · ${excluded} excluded${needs} (hidden)`;
      }
      const empty = !showAll && shown === 0 && pairs.length > 0;
      if (el["ec-table-empty"]) {
        el["ec-table-empty"].textContent = empty
          ? `No cancer types pass for ${state.view.hugo_symbol} in this release. ` +
            `Choose "All cancer types" to review the ` +
            (candidates ? `${hidden} not shown.` : `${excluded} excluded.`)
          : "";
        show(el["ec-table-empty"], empty);
      }
      show(el["ec-table"], !empty);
      updateExportLink();
    }

    function setView(showAll) {
      state.showAll = Boolean(showAll);
      applyView();
    }

    function releaseText(bundle) {
      return bundle && bundle.release_id ? `Data release ${bundle.release_id}` : "";
    }

    function render(view) {
      if (!state.view || state.view.hugo_symbol !== view.hugo_symbol) {
        state.expanded.clear();
        state.alterationExpanded.clear();
        // A new gene starts on "Shown only"; same-gene re-renders keep the view.
        state.showAll = false;
      }
      state.view = view;
      const title = view.query && view.query.trim().toUpperCase() !== view.hugo_symbol
        ? `${view.hugo_symbol} (from ${view.query.trim()})`
        : view.hugo_symbol;
      el["ec-gene-title"].textContent = view.entrez_gene_id ? `${title} · Entrez ${view.entrez_gene_id}` : title;
      if (view.broadly_expressed === true) {
        // Method m3: the gene is broadly expressed across cohorts.
        el["ec-gene-title"].appendChild(badge("Broadly expressed", "warn"));
      }
      const summary = view.summary || {};
      el["ec-summary"].textContent =
        `${summary.surfaced || 0} surfaced · ${summary.literature_pending || 0} awaiting literature check · ` +
        `${summary.excluded || 0} excluded` +
        (summary.candidate ? ` · ${summary.candidate} purity ${summary.candidate === 1 ? "concern" : "concerns"}` : "");
      if (el["ec-release"]) el["ec-release"].textContent = releaseText(view.bundle);
      if (el["ec-dmt-index"]) {
        const tissues = [...new Set((view.dmt_adc_index || []).map((item) =>
          `${item.tissue_type}${item.tcga_codes.length ? "" : " (no TCGA cohort)"}`))];
        el["ec-dmt-index"].textContent = tissues.length ? `DMT ADC index cancers: ${tissues.join(", ")}` : "";
        show(el["ec-dmt-index"], tissues.length > 0);
      }
      show(el["ec-run"], (summary.literature_pending || 0) > 0 && !state.jobRunning);
      renderCbioCompare(view);
      el["ec-tbody"].replaceChildren();
      state.rows = [];
      for (const pair of view.pairs || []) {
        for (const row of renderRows(pair, view.hugo_symbol)) {
          el["ec-tbody"].appendChild(row);
        }
      }
      applyView();
      show(el["ec-result"], true);
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

    async function lookup(rawGene) {
      const gene = String(rawGene || "").trim().toUpperCase();
      setError("");
      if (!SYMBOL.test(gene)) {
        setError(friendlyError(422, gene));
        return;
      }
      if (
        state.view &&
        gene !== state.view.hugo_symbol &&
        (!state.view.query || gene !== state.view.query.trim().toUpperCase())
      ) {
        state.expanded.clear();
        state.alterationExpanded.clear();
      }
      state.loading = true;
      el["ec-loading"].textContent = `Loading ${gene}…`;
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
    if (el["ec-view-shown"]) el["ec-view-shown"].addEventListener("click", () => setView(false));
    if (el["ec-view-all"]) el["ec-view-all"].addEventListener("click", () => setView(true));

    function setMode(mode) {
      state.mode = mode === "batch" ? "batch" : "single";
      const isBatch = state.mode === "batch";
      if (el["ec-tab-single"]) {
        el["ec-tab-single"].setAttribute("aria-pressed", !isBatch ? "true" : "false");
        el["ec-tab-single"].classList.toggle("active", !isBatch);
      }
      if (el["ec-tab-batch"]) {
        el["ec-tab-batch"].setAttribute("aria-pressed", isBatch ? "true" : "false");
        el["ec-tab-batch"].classList.toggle("active", isBatch);
      }
      show(el["ec-single-mode"], !isBatch);
      show(el["ec-batch-mode"], isBatch);
      if (isBatch) {
        show(el["ec-result"], false);
        if (state.batch) show(el["ec-batch-result"], true);
      } else {
        show(el["ec-batch-result"], false);
        if (state.view) show(el["ec-result"], true);
      }
    }

    function updateBatchPreview() {
      if (!el["ec-batch-input"] || !el["ec-batch-preview"]) return;
      const { unique, duplicates } = parseBatchText(el["ec-batch-input"].value);
      const countText = `${unique.length} ${unique.length === 1 ? "gene" : "genes"}`;
      const dupText = duplicates > 0 ? ` (${duplicates} ${duplicates === 1 ? "duplicate" : "duplicates"})` : "";
      el["ec-batch-preview"].textContent = `${countText}${dupText}`;
    }

    function updateBatchExportLinks() {
      const batch = state.batch;
      if (!batch || !batch.batch_id) return;
      for (const [id, pressed] of [
        ["ec-batch-export-shown", !state.batchShowAll],
        ["ec-batch-export-all", state.batchShowAll],
      ]) {
        const btn = el[id];
        if (!btn) continue;
        btn.setAttribute("aria-pressed", pressed ? "true" : "false");
        btn.classList.toggle("active", pressed);
      }
      if (el["ec-batch-export"]) {
        el["ec-batch-export"].setAttribute("href", batchExportURL(batch.batch_id, state.batchShowAll));
      }
      if (el["ec-batch-export-golden"]) {
        el["ec-batch-export-golden"].setAttribute("href", batchGoldenSetExportURL(batch.batch_id));
      }
      if (el["ec-batch-export-curator"]) {
        el["ec-batch-export-curator"].setAttribute("href", batchCuratorSheetExportURL(batch.batch_id));
      }
    }

    function setBatchExportView(includeExcluded) {
      state.batchShowAll = Boolean(includeExcluded);
      updateBatchExportLinks();
    }

    function renderBatchGeneSummary(bodyNode, view, item) {
      bodyNode.replaceChildren();
      if (item.notes) {
        bodyNode.appendChild(node("div", `Note: ${item.notes}`, "ec-neutral"));
      }
      if (item.error) {
        bodyNode.appendChild(node("div", `Error: ${item.error}`, "ec-neutral"));
      }
      if (view && Array.isArray(view.pairs)) {
        const shown = view.pairs.filter((p) => !isHidden(p));
        if (shown.length) {
          for (const pair of shown) {
            const line = node("div", null, "ec-batch-pair-line");
            const label = pair.cancer_type_name ? `${pair.cancer_type_name} (${pair.oncotree_code})` : pair.oncotree_code;
            line.appendChild(node("span", `${label}: `, "ec-batch-pair-name"));
            if (pair.status === "surfaced") {
              line.appendChild(badge("Surfaced", "pass"));
              if (["Strong", "Moderate"].includes(pair.confidence_label)) {
                line.appendChild(badge(pair.confidence_label, pair.confidence_label.toLowerCase()));
              }
            } else if (pair.status === "literature_pending") {
              line.appendChild(badge("Awaiting literature check", "warn"));
            }
            const alt = alterationsFor(pair);
            const enriched = (alt && alt.alteration_types_enriched) || [];
            for (const cat of enriched) {
              const catObj = alt && alt.categories && alt.categories[cat];
              line.appendChild(badge(alterationLabel(cat, catObj), "alteration"));
            }
            for (const [cat, entry] of Object.entries((pair.stats && pair.stats.alteration_high_expression) || {})) {
              if (entry && entry.status === "associated_high_expression") {
                line.appendChild(node("span", `${cat}: High-expression assoc.`, "ec-badge ec-high-expression"));
              }
            }
            bodyNode.appendChild(line);
          }
        } else {
          bodyNode.appendChild(node("div", "No shown cancer types.", "ec-neutral"));
        }
      } else if (item.status === "not_found") {
        bodyNode.appendChild(node("div", "Not in expression data.", "ec-neutral"));
      }
      if (item.hugo_symbol) {
        const links = node("div", null, "ec-batch-gene-links");
        const singleLink = node("a", `Open ${item.hugo_symbol} in Single view`);
        singleLink.setAttribute("href", `?gene=${encodeURIComponent(item.hugo_symbol)}`);
        singleLink.addEventListener("click", (ev) => {
          if (ev && typeof ev.preventDefault === "function") ev.preventDefault();
          setMode("single");
          if (el["ec-gene"]) el["ec-gene"].value = item.hugo_symbol;
          lookup(item.hugo_symbol);
        });
        links.appendChild(singleLink);
        bodyNode.appendChild(links);
      }
    }

    async function loadBatchGeneSection(item, bodyNode) {
      if (!item.hugo_symbol) {
        renderBatchGeneSummary(bodyNode, null, item);
        return;
      }
      if (state.batchGeneViews.has(item.hugo_symbol)) {
        renderBatchGeneSummary(bodyNode, state.batchGeneViews.get(item.hugo_symbol), item);
        return;
      }
      bodyNode.textContent = `Loading ${item.hugo_symbol}…`;
      const { status, body } = await getJson(`${API}/stats/${encodeURIComponent(item.hugo_symbol)}`);
      if (status === 200 && body) {
        state.batchGeneViews.set(item.hugo_symbol, body);
        renderBatchGeneSummary(bodyNode, body, item);
      } else {
        renderBatchGeneSummary(bodyNode, null, item);
      }
    }

    function batchGeneBadge(status) {
      if (status === "done") return badge("Done", "pass");
      if (status === "skipped_fresh") return badge("Reused fresh", "info");
      if (status === "skipped_no_stats") return badge("No stats pass", "neutral");
      if (status === "running") return badge("Running", "warn");
      if (status === "queued") return badge("Queued", "neutral");
      if (status === "not_found") return badge("Not found", "fail");
      return badge("Failed", "fail");
    }

    function renderBatch(batch) {
      state.batch = batch;
      const counts = batch.counts || {};
      const total = counts.total || (batch.genes || []).length || 0;
      const finished = (counts.done || 0) + (counts.skipped_no_stats || 0) +
        (counts.skipped_fresh || 0) + (counts.failed || 0) + (counts.not_found || 0);
      if (el["ec-batch-title"]) {
        el["ec-batch-title"].textContent = `Batch ${batch.batch_id}`;
      }
      if (el["ec-batch-summary"]) {
        el["ec-batch-summary"].textContent =
          `Status: ${batch.status} · ${finished}/${total} processed`;
      }
      if (el["ec-batch-counts"]) {
        el["ec-batch-counts"].textContent =
          `${counts.done || 0} done · ${counts.skipped_fresh || 0} reused · ` +
          `${counts.skipped_no_stats || 0} no stats · ${counts.queued || 0} queued · ` +
          `${counts.running || 0} running · ${counts.failed || 0} failed · ` +
          `${counts.not_found || 0} not found`;
      }
      if (el["ec-batch-progress"]) {
        el["ec-batch-progress"].setAttribute("max", String(Math.max(1, total)));
        el["ec-batch-progress"].setAttribute("value", String(finished));
      }
      const running = batch.status === "queued" || batch.status === "running";
      show(el["ec-batch-cancel"], running);
      updateBatchExportLinks();

      if (el["ec-batch-genes"]) {
        el["ec-batch-genes"].replaceChildren();
        for (const item of batch.genes || []) {
          const key = `${item.position}:${item.hugo_symbol || item.input_symbol}`;
          const section = node("details", null, "ec-batch-gene-section");
          if (state.batchExpanded.has(key)) section.setAttribute("open", "open");
          const summary = node("summary");
          const label = item.hugo_symbol && item.input_symbol.trim().toUpperCase() !== item.hugo_symbol
            ? `${item.hugo_symbol} (from ${item.input_symbol.trim()})`
            : (item.hugo_symbol || item.input_symbol);
          summary.appendChild(node("span", label, "ec-batch-gene-title"));
          summary.appendChild(batchGeneBadge(item.status));
          const bodyNode = node("div", null, "ec-batch-gene-body");
          section.appendChild(summary);
          section.appendChild(bodyNode);
          if (state.batchExpanded.has(key)) {
            loadBatchGeneSection(item, bodyNode);
          } else {
            renderBatchGeneSummary(bodyNode, state.batchGeneViews.get(item.hugo_symbol) || null, item);
          }
          summary.addEventListener("click", () => {
            const willOpen = !state.batchExpanded.has(key);
            if (willOpen) {
              state.batchExpanded.add(key);
              section.setAttribute("open", "open");
              loadBatchGeneSection(item, bodyNode);
            } else {
              state.batchExpanded.delete(key);
            }
          });
          el["ec-batch-genes"].appendChild(section);
        }
      }
      show(el["ec-batch-result"], true);
    }

    async function loadBatch(batchId) {
      const id = String(batchId || "").trim();
      if (!id) return;
      setError("");
      setMode("batch");
      state.batchGeneViews.clear();
      if (win.history && typeof win.history.replaceState === "function") {
        win.history.replaceState(null, "", `?batch=${encodeURIComponent(id)}`);
      }
      for (let poll = 0; poll < maxPolls; poll += 1) {
        const { status, body } = await getJson(`${API}/batches/${encodeURIComponent(id)}`);
        if (status === 401) {
          redirectToLogin();
          return;
        }
        if (status !== 200 || !body) {
          setError(status === 404 ? "Batch not found." : friendlyError(status, id));
          return;
        }
        renderBatch(body);
        if (!["queued", "running"].includes(body.status)) {
          return;
        }
        await sleep(pollMs);
      }
    }

    async function submitBatch(rawText) {
      const text = rawText !== undefined ? rawText : (el["ec-batch-input"] ? el["ec-batch-input"].value : "");
      const { tokens } = parseBatchText(text);
      setError("");
      if (!tokens.length) {
        setError("Enter at least one gene symbol.");
        return;
      }
      if (tokens.length > 2000) {
        setError("A batch can include at most 2000 gene entries.");
        return;
      }
      if (el["ec-batch-submit"]) el["ec-batch-submit"].disabled = true;
      try {
        const { status, body } = await getJson(`${API}/batches`, {
          method: "POST",
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ genes: tokens }),
        });
        if (status === 401) {
          redirectToLogin();
          return;
        }
        if (status !== 202 || !body || !body.batch_id) {
          setError(friendlyError(status, ""));
          return;
        }
        await loadBatch(body.batch_id);
      } finally {
        if (el["ec-batch-submit"]) el["ec-batch-submit"].disabled = false;
      }
    }

    async function cancelCurrentBatch() {
      if (!state.batch || !state.batch.batch_id) return;
      const id = state.batch.batch_id;
      const { status, body } = await getJson(`${API}/batches/${encodeURIComponent(id)}/cancel`, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (status === 200 && body) {
        renderBatch(body);
      } else if (status === 401) {
        redirectToLogin();
      }
    }

    if (el["ec-tab-single"]) el["ec-tab-single"].addEventListener("click", () => setMode("single"));
    if (el["ec-tab-batch"]) el["ec-tab-batch"].addEventListener("click", () => setMode("batch"));
    if (el["ec-batch-input"]) el["ec-batch-input"].addEventListener("input", () => updateBatchPreview());
    if (el["ec-batch-upload-btn"] && el["ec-batch-upload"]) {
      el["ec-batch-upload-btn"].addEventListener("click", () => {
        if (typeof el["ec-batch-upload"].click === "function") el["ec-batch-upload"].click();
      });
      el["ec-batch-upload"].addEventListener("change", async (event) => {
        const file = (event && event.target && event.target.files && event.target.files[0]) ||
          (el["ec-batch-upload"].files && el["ec-batch-upload"].files[0]);
        if (!file) return;
        const text = typeof file.text === "function" ? await file.text() : String(file.content || "");
        const genes = parseUploadedFileColumn(text, file.name || "");
        if (el["ec-batch-input"]) {
          el["ec-batch-input"].value = genes.join("\n");
          updateBatchPreview();
        }
      });
    }
    if (el["ec-batch-form"]) {
      el["ec-batch-form"].addEventListener("submit", (event) => {
        if (event && typeof event.preventDefault === "function") event.preventDefault();
        return submitBatch();
      });
    }
    if (el["ec-batch-cancel"]) el["ec-batch-cancel"].addEventListener("click", () => cancelCurrentBatch());
    if (el["ec-batch-export-shown"]) el["ec-batch-export-shown"].addEventListener("click", () => setBatchExportView(false));
    if (el["ec-batch-export-all"]) el["ec-batch-export-all"].addEventListener("click", () => setBatchExportView(true));

    async function init() {
      const params = new URLSearchParams(win.location.search || "");
      const batchId = params.get("batch");
      if (batchId) {
        await loadBatch(batchId);
        return;
      }
      const gene = params.get("gene");
      if (gene) {
        el["ec-gene"].value = gene;
        await lookup(gene);
      }
    }

    return {
      init,
      lookup,
      runLiteratureCheck,
      setView,
      setMode,
      submitBatch,
      loadBatch,
      cancelCurrentBatch,
      setBatchExportView,
      updateBatchPreview,
      state,
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      createExpressionContextPage,
      formatNumber,
      formatQ,
      pmidUrl,
      expressionURL,
      exportURL,
      goldenSetExportURL,
      curatorSheetExportURL,
      batchExportURL,
      batchGoldenSetExportURL,
      batchCuratorSheetExportURL,
      parseBatchText,
      parseUploadedFileColumn,
    };
  } else {
    createExpressionContextPage({ document: root.document, window: root, fetch: root.fetch.bind(root) }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);

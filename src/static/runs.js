// My runs page: metadata-only cursor pagination. API values are text nodes.
(function (root) {
  "use strict";

  function formatDateTime(value) {
    if (!value) return "";
    // Run timestamps are UTC. Older Safari requires millisecond precision and
    // an explicit timezone; older stored timestamps can omit both.
    let normalized = String(value).replace(" ", "T");
    normalized = normalized.replace(/(\.\d{3})\d+/, "$1");
    normalized = normalized.replace(/\.(\d{1,2})(?=Z|[+-]\d{2}:?\d{2}|$)/, (_, digits) => `.${digits.padEnd(3, "0")}`);
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?$/.test(normalized)) normalized += "Z";
    const date = new Date(normalized);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function viewHref(run, origin) {
    try {
      if (typeof run.view_url === "string") {
        const url = new URL(run.view_url, origin);
        if (url.origin === origin && ["http:", "https:"].includes(url.protocol)) {
          return url.href;
        }
      }
    } catch (_) { /* Fall back to a locally constructed link. */ }
    return run.run_id ? `/?run=${encodeURIComponent(run.run_id)}` : null;
  }

  // Read by app.js on the run page it opens (see takePendingRerun there).
  const PENDING_RERUN_KEY = "agcg.pendingRerun";

  function friendlyError(error) {
    const { status, network } = error;
    if (status === 401) return "Your session has expired. Redirecting you to sign in…";
    if (status === 403) return "Your account isn't active yet — it may be pending administrator approval. Please contact your AGCG administrator to view your runs.";
    if (status === 503) return "The run store is unavailable right now. Please try again later.";
    if (status === 422) return "This page cursor is invalid. Start over to reload your runs.";
    if (status >= 500) return "The server couldn't load your runs right now. Please try again later.";
    if (network) return "Couldn't load your runs. Check your connection and try again.";
    return "Couldn't load your runs. Try again.";
  }

  function createRunsPage({ document: doc, window: win, fetch: fetchFn }) {
    const el = {};
    for (const name of ["error", "empty", "loading", "table", "tbody", "load-more", "retry"]) {
      el[name] = doc.getElementById(`runs-${name}`);
    }
    const state = { runs: [], nextCursor: null, loaded: false, loading: false, error: null };
    const show = (node, visible) => node.classList.toggle("hidden", !visible);

    function cell(value) {
      const td = doc.createElement("td");
      td.textContent = value == null ? "" : String(value);
      return td;
    }

    function rerunCell(run, href) {
      const td = doc.createElement("td");
      if (!href || !run.run_id) return td;
      const mode = doc.createElement("select");
      mode.setAttribute("aria-label", "Rerun with");
      for (const [value, label] of [["cached", "Use cached results"], ["refresh", "Refresh (force_refresh)"]]) {
        const option = doc.createElement("option");
        option.value = value;
        option.textContent = label;
        mode.appendChild(option);
      }
      mode.value = "cached";
      const button = doc.createElement("button");
      button.type = "button";
      button.className = "runs-rerun";
      button.textContent = "Rerun";
      button.title = "Start a new run with this run's inputs; this run is left unchanged";
      button.addEventListener("click", () => {
        try {
          win.sessionStorage.setItem(PENDING_RERUN_KEY, JSON.stringify({
            run_id: run.run_id, force_refresh: mode.value === "refresh",
          }));
        } catch (_) { /* Without storage the run still opens, with its own Rerun button. */ }
        win.location.href = href;
      });
      td.appendChild(mode);
      td.appendChild(button);
      return td;
    }

    function render() {
      show(el.loading, state.loading && state.runs.length === 0);
      show(el.empty, state.loaded && !state.loading && !state.error && state.runs.length === 0);
      show(el.error, Boolean(state.error));
      el.error.textContent = state.error ? friendlyError(state.error) : "";
      show(el.retry, Boolean(state.error) && ![401, 403].includes(state.error.status));
      el.retry.textContent = state.error && state.error.status === 422 ? "Start over" : "Retry";
      el.retry.disabled = state.loading;
      show(el.table, state.runs.length > 0);
      show(el["load-more"], Boolean(state.nextCursor) && !state.error);
      el["load-more"].disabled = state.loading;
      el.tbody.replaceChildren();
      for (const run of state.runs) {
        const row = doc.createElement("tr");
        const dateCell = cell("");
        const href = viewHref(run, win.location.origin);
        if (href) {
          const link = doc.createElement("a");
          link.href = href;
          link.textContent = formatDateTime(run.created_at) || "Open run";
          dateCell.appendChild(link);
        } else {
          dateCell.textContent = formatDateTime(run.created_at);
        }
        row.appendChild(dateCell);
        const summary = run.input_summary || "";
        row.appendChild(cell(run.input_count == null ? summary : `${summary ? summary + " " : ""}(${run.input_count})`));
        row.appendChild(cell(run.genes_annotated));
        row.appendChild(cell(run.source));
        row.appendChild(rerunCell(run, href));
        el.tbody.appendChild(row);
      }
    }

    async function load() {
      if (state.loading) return;
      state.loading = true;
      state.error = null;
      render();
      try {
        let url = "/v1/runs?limit=20";
        if (state.nextCursor) url += `&cursor=${encodeURIComponent(state.nextCursor)}`;
        let response;
        try {
          response = await fetchFn(url, {
            method: "GET", credentials: "same-origin", headers: { Accept: "application/json" },
          });
        } catch (_) {
          throw { network: true };
        }
        if (response.status === 401) win.location.href = "/login?redirect_to=%2Fruns";
        if (!response.ok) throw { status: response.status };
        const data = await response.json();
        // A malformed response must not masquerade as an empty history.
        if (!Array.isArray(data.runs)) throw new Error("Invalid runs response");
        state.runs = state.runs.concat(data.runs);
        state.nextCursor = data.next_cursor || null;
        state.loaded = true;
      } catch (error) {
        state.error = error;
      } finally {
        state.loading = false;
        render();
      }
    }

    el["load-more"].addEventListener("click", () => {
      if (state.nextCursor) return load();
    });
    el.retry.addEventListener("click", () => {
      if (state.loading || !state.error || [401, 403].includes(state.error.status)) return;
      if (state.error.status === 422) {
        state.nextCursor = null;
        state.runs = [];
        state.loaded = false;
      }
      return load();
    });
    return { init: load };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createRunsPage, formatDateTime };
  } else {
    createRunsPage({ document: root.document, window: root, fetch: root.fetch.bind(root) }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);

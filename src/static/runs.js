// My runs page (/runs): list saved runs for the signed-in user.
// Uses GET /v1/runs with cursor pagination. Escapes all text via textContent.
(function (root) {
  "use strict";

  const PAGE_PATH = "/runs";
  const DEFAULT_LIMIT = 20;

  class ApiError extends Error {
    constructor(status) {
      super(`HTTP ${status}`);
      this.status = status;
    }
  }

  function redirectToLogin() {
    const win = root.window || root;
    if (win && win.location) {
      win.location.href = `/login?redirect_to=${encodeURIComponent(PAGE_PATH)}`;
    }
  }

  async function request(url) {
    const fetchFn = (root.fetch || fetch);
    const init = { method: "GET", credentials: "same-origin", headers: { Accept: "application/json" } };
    let response;
    try {
      response = await fetchFn(url, init);
    } catch (_networkError) {
      throw new ApiError(0);
    }
    if (response.status === 401) {
      redirectToLogin();
      throw new ApiError(401);
    }
    if (!response.ok) {
      throw new ApiError(response.status);
    }
    return response.json();
  }

  function formatDateTime(isoString) {
    if (!isoString) return "";
    try {
      const d = new Date(isoString);
      if (Number.isNaN(d.getTime())) return isoString;
      return d.toLocaleString();
    } catch (e) {
      return isoString;
    }
  }

  function createRunsPage(deps) {
    const doc = deps.document || (root.document);
    const win = deps.window || (root.window || root);
    const fetchFn = deps.fetch || (root.fetch || fetch);
    const $ = (id) => doc.getElementById(id);

    const el = {
      error: $("runs-error"),
      empty: $("runs-empty"),
      loading: $("runs-loading"),
      table: $("runs-table"),
      tbody: $("runs-tbody"),
      loadMore: $("runs-load-more"),
    };

    const state = {
      cursor: null,
      nextCursor: null,
      runs: [],
      loading: false,
    };

    function show(node, visible) {
      if (node) node.classList.toggle("hidden", !visible);
    }

    function setMessage(node, message) {
      if (!node) return;
      node.textContent = message || "";
      show(node, Boolean(message));
    }

    function friendlyError(status) {
      if (status === 401) return "Your session has expired. Redirecting you to sign in…";
      if (status === 503) return "The run store is unavailable right now. Please try again later.";
      if (status === 422) return "This page link is invalid. Please refresh.";
      if (status >= 500) return "The server couldn't load your runs right now. Please try again later.";
      return "Couldn't load your runs. Check your connection and try again.";
    }

    function cell(text) {
      const td = doc.createElement("td");
      td.textContent = text == null ? "" : String(text);
      return td;
    }

    function render() {
      show(el.loading, state.loading && state.runs.length === 0);
      show(el.empty, !state.loading && state.runs.length === 0);
      show(el.table, state.runs.length > 0);
      el.tbody.replaceChildren();

      for (const run of state.runs) {
        const tr = doc.createElement("tr");

        const href = run.view_url || (run.run_id ? `/?run=${encodeURIComponent(run.run_id)}` : "#");

        // Date/time (local time)
        tr.appendChild(cell(formatDateTime(run.created_at)));

        // Inputs (input_summary + count)
        let inputsText = "";
        if (run.input_summary && run.input_count != null) {
          inputsText = `${run.input_summary} (${run.input_count})`;
        } else if (run.input_summary) {
          inputsText = run.input_summary;
        } else if (run.input_count != null) {
          inputsText = `${run.input_count}`;
        }
        tr.appendChild(cell(inputsText));

        // Genes annotated
        tr.appendChild(cell(run.genes_annotated != null ? run.genes_annotated : ""));

        // Source
        tr.appendChild(cell(run.source || ""));

        // Each row links to view_url
        tr.setAttribute("role", "link");
        tr.tabIndex = 0;
        tr.style.cursor = "pointer";
        tr.addEventListener("click", function (e) {
          if (e.target && e.target.closest && e.target.closest("a")) return;
          if (win && win.location) {
            win.location.href = href;
          }
        });
        tr.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            if (win && win.location) {
              win.location.href = href;
            }
          }
        });

        el.tbody.appendChild(tr);
      }

      if (state.nextCursor) {
        show(el.loadMore, true);
        el.loadMore.disabled = state.loading;
      } else {
        show(el.loadMore, false);
      }
    }

    async function load() {
      if (state.loading) return;
      state.loading = true;
      setMessage(el.error, "");
      render();
      try {
        let url = `/v1/runs?limit=${DEFAULT_LIMIT}`;
        if (state.cursor) {
          url += `&cursor=${encodeURIComponent(state.cursor)}`;
        }
        const data = await request(url);
        const runs = Array.isArray(data.runs) ? data.runs : [];
        state.runs = state.runs.concat(runs);
        state.nextCursor = data.next_cursor || null;
      } catch (err) {
        if (err && err.status !== 401) {
          setMessage(el.error, friendlyError(err.status));
        }
      } finally {
        state.loading = false;
        render();
      }
    }

    if (el.loadMore) {
      el.loadMore.addEventListener("click", function () {
        if (!state.loading && state.nextCursor) {
          state.cursor = state.nextCursor;
          load();
        }
      });
    }

    load();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createRunsPage };
  } else if (root) {
    createRunsPage({ document: root.document, window: root, fetch: root.fetch });
  }
})(typeof window !== "undefined" ? window : global);

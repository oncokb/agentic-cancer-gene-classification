/* Curator application form and administrator queue. User content is always text. */
(function (root) {
  "use strict";
  function createCuratorRequests({ document: doc, fetch: fetchFn, admin = false, onDecision = () => {} }) {
    const el = (id) => doc.getElementById(`curator-${id}`);
    const show = (node, visible) => node.classList.toggle("hidden", !visible);
    let cursor = null, busy = false, pending = null;
    async function api(url, payload) {
      const response = await fetchFn(url, {
        credentials: "same-origin",
        ...(payload === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
      });
      const fallback = response.ok
        ? "Unexpected server response. Please retry shortly."
        : `Request failed (HTTP ${response.status}). Please retry shortly.`;
      const contentType = (response.headers.get("content-type") || "").split(";", 1)[0].trim().toLowerCase();
      if (contentType !== "application/json") throw new Error(fallback);
      let body;
      try { body = await response.json(); }
      catch (_) { throw new Error(fallback); }
      if (!response.ok) throw new Error(body && typeof body.detail === "string" ? body.detail : fallback);
      return body;
    }
    function error(err) { el("message").textContent = err.message || "Unable to load requests. Please retry."; }
    function text(tag, value, parent) {
      const node = doc.createElement(tag);
      node.textContent = value == null ? "" : String(value);
      parent.appendChild(node);
      return node;
    }
    async function loadStatus() {
      show(el("form"), false);
      try {
        const [data, auth] = await Promise.all([
          api("/v1/curator-requests/me"), api("/auth/me"),
        ]);
        el("message").textContent = `Request status: ${data.status}${data.status === "rejected" && data.request.review_note ? `. Reviewer note: ${data.request.review_note}` : ""}`;
        const eligible = auth.authenticated && auth.user && auth.user.role === "viewer" && auth.user.status === "active";
        show(el("form"), Boolean(eligible) && data.status !== "pending");
      } catch (err) { error(err); }
    }
    async function submit(event) {
      event.preventDefault();
      if (busy) return;
      busy = true;
      el("submit").disabled = true;
      const payload = {};
      for (const field of ["reason", "affiliation", "expertise", "orcid"]) payload[field] = el(field).value.trim();
      if (!payload.orcid) payload.orcid = null;
      try {
        await api("/v1/curator-requests", payload);
        await loadStatus();
      } catch (err) { error(err); }
      finally { busy = false; el("submit").disabled = false; }
    }
    async function loadQueue(reset = true) {
      if (busy) return;
      busy = true;
      el("more").disabled = true;
      if (reset) { cursor = null; el("rows").replaceChildren(); }
      el("message").textContent = "Loading…";
      const params = new URLSearchParams({ status: el("filter").value, limit: "50" });
      if (cursor) params.set("cursor", cursor);
      try {
        const data = await api(`/v1/admin/curator-requests?${params}`);
        for (const request of data.requests) {
          const card = doc.createElement("article");
          card.className = "api-keys-card";
          text("h3", request.email, card);
          for (const field of ["status", "created_at", "reason", "affiliation", "expertise", "orcid", "review_note"]) {
            if (request[field]) text("p", `${field.replaceAll("_", " ")}: ${request[field]}`, card);
          }
          if (request.status === "pending") {
            for (const action of ["approve", "reject"]) {
              const button = text("button", action === "approve" ? "Approve" : "Reject", card);
              button.type = "button";
              button.addEventListener("click", () => {
                pending = { id: request.id, action };
                el("confirm-message").textContent = `${action === "approve" ? "Approve curator access for" : "Reject the request from"} ${request.email}?`;
                el("note").value = "";
                el("review-error").textContent = "";
                show(el("confirm"), true);
              });
            }
          }
          el("rows").appendChild(card);
        }
        cursor = data.next_cursor;
        show(el("more"), Boolean(cursor));
        el("message").textContent = el("rows").children.length ? "" : "No requests match.";
      } catch (err) { error(err); }
      finally { busy = false; el("more").disabled = false; }
    }
    async function review(event) {
      event.preventDefault();
      if (busy || !pending) return;
      busy = true;
      el("review-submit").disabled = true;
      try {
        await api(`/v1/admin/curator-requests/${pending.id}/${pending.action}`, { note: el("note").value.trim() || null });
        pending = null;
        show(el("confirm"), false);
        busy = false;
        await loadQueue();
        await onDecision();
      } catch (err) { el("review-error").textContent = err.message; }
      finally { busy = false; el("review-submit").disabled = false; }
    }
    function init() {
      if (admin) {
        el("filters").addEventListener("submit", (event) => { event.preventDefault(); return loadQueue(); });
        el("more").addEventListener("click", () => loadQueue(false));
        el("review").addEventListener("submit", review);
        el("cancel").addEventListener("click", () => { if (!busy) { pending = null; show(el("confirm"), false); } });
        return loadQueue();
      }
      el("form").addEventListener("submit", submit);
      el("close").addEventListener("click", () => show(el("dialog"), false));
      el("open").addEventListener("click", () => { show(el("dialog"), true); return loadStatus(); });
    }
    return { init, loadQueue, loadStatus, submit, review };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createCuratorRequests };
  else {
    root.createCuratorRequests = createCuratorRequests;
    const admin = Boolean(root.document.getElementById("curator-rows"));
    createCuratorRequests({ document: root.document, fetch: root.fetch.bind(root), admin,
      onDecision: () => root.location.reload(),
    }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);

// Admin user management page (/admin/users): users table with role/status
// controls, a confirmation dialog for every change, and the change log
// (all changes or one user's). API values are only ever text nodes.
(function (root) {
  "use strict";

  const ROLES = ["viewer", "curator", "admin"];
  const ROLE_RANK = { viewer: 0, curator: 1, admin: 2 };
  const LOGIN_URL = "/login?redirect_to=%2Fadmin%2Fusers";
  const PAGE_SIZE = 50;

  function formatDateTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function isDemotion(oldRole, newRole) {
    return ROLE_RANK[newRole] < ROLE_RANK[oldRole];
  }

  // What a change means for the confirmation dialog; `danger` for demote/disable.
  function describeChange(user, change) {
    const who = user.email;
    if (change.role) {
      const danger = isDemotion(user.role, change.role);
      return {
        message: `Change ${who}'s role from ${user.role} to ${change.role}?`,
        warning: danger ? `${who} loses ${user.role} permissions on their next request.` : "",
        action: danger ? "Demote" : "Change role",
        danger,
      };
    }
    if (change.status === "disabled") {
      return {
        message: `Disable ${who}?`,
        warning: `${who} loses access to the app and their API keys until re-enabled.`,
        action: "Disable",
        danger: true,
      };
    }
    const approving = user.status === "pending";
    return {
      message: approving ? `Approve ${who}'s account?` : `Re-enable ${who}?`,
      warning: "",
      action: approving ? "Approve" : "Re-enable",
      danger: false,
    };
  }

  function errorMessage(error) {
    if (error && typeof error.detail === "string" && error.detail) return error.detail;
    const status = error && error.status;
    if (status === 403) return "Administrator privileges required.";
    if (status === 503) return "The user store is unavailable right now. Please retry shortly.";
    if (status === 422) return "That request was invalid. Reload the page and try again.";
    if (error && error.network) return "Couldn't reach the server. Check your connection and try again.";
    return "Something went wrong. Please try again.";
  }

  function createAdminUsersPage({ document: doc, window: win, fetch: fetchFn }) {
    const $ = (id) => doc.getElementById(id);
    const el = {
      readonly: $("admin-readonly"),
      error: $("admin-error"),
      filters: $("admin-filters"),
      search: $("admin-search"),
      roleFilter: $("admin-role-filter"),
      statusFilter: $("admin-status-filter"),
      empty: $("admin-empty"),
      loading: $("admin-loading"),
      table: $("admin-table"),
      tbody: $("admin-tbody"),
      loadMore: $("admin-load-more"),
      log: $("admin-log"),
      logHeading: $("admin-log-heading"),
      logAll: $("admin-log-all"),
      logEmpty: $("admin-log-empty"),
      logTable: $("admin-log-table"),
      logTbody: $("admin-log-tbody"),
      logMore: $("admin-log-more"),
      confirm: $("admin-confirm"),
      confirmMessage: $("admin-confirm-message"),
      confirmWarning: $("admin-confirm-warning"),
      confirmReason: $("admin-confirm-reason"),
      confirmError: $("admin-confirm-error"),
      confirmCancel: $("admin-confirm-cancel"),
      confirmOk: $("admin-confirm-ok"),
    };
    const state = {
      me: null,
      writable: false,
      users: [],
      nextCursor: null,
      filters: { search: "", role: "", status: "" },
      loading: false,
      loaded: false,
      error: null,
      log: { email: null, changes: [], nextCursor: null, loading: false, loaded: false },
      pending: null,
      saving: false,
    };
    const show = (node, visible) => node.classList.toggle("hidden", !visible);

    async function api(url, init) {
      let response;
      try {
        response = await fetchFn(url, {
          credentials: "same-origin",
          ...init,
          headers: { Accept: "application/json", ...((init && init.headers) || {}) },
        });
      } catch (_) {
        throw { network: true };
      }
      if (response.status === 401) {
        win.location.href = LOGIN_URL;
        throw { status: 401 };
      }
      let body = null;
      try {
        body = await response.json();
      } catch (_) { /* Non-JSON bodies fall back to a generic message. */ }
      if (!response.ok) {
        throw { status: response.status, detail: body && typeof body.detail === "string" ? body.detail : null };
      }
      return body;
    }

    function cell(value) {
      const td = doc.createElement("td");
      td.textContent = value == null ? "" : String(value);
      return td;
    }

    function button(label, onClick, className) {
      const btn = doc.createElement("button");
      btn.type = "button";
      btn.textContent = label;
      if (className) btn.className = className;
      btn.addEventListener("click", onClick);
      return btn;
    }

    function roleCell(user) {
      const td = doc.createElement("td");
      const select = doc.createElement("select");
      select.setAttribute("aria-label", `Role for ${user.email}`);
      for (const role of ROLES) {
        const option = doc.createElement("option");
        option.value = role;
        option.textContent = role;
        select.appendChild(option);
      }
      select.value = user.role;
      select.disabled = !state.writable;
      select.addEventListener("change", () => {
        const role = select.value;
        // The select shows the stored role until the change is confirmed.
        select.value = user.role;
        if (role !== user.role) openConfirm(user, { role });
      });
      td.appendChild(select);
      return td;
    }

    function statusCell(user) {
      const td = doc.createElement("td");
      const badge = doc.createElement("span");
      badge.className = `api-key-status status-${user.status === "disabled" ? "revoked" : user.status === "pending" ? "inactive" : "active"}`;
      badge.textContent = user.status;
      td.appendChild(badge);
      return td;
    }

    function actionsCell(user) {
      const td = doc.createElement("td");
      if (!state.writable) return td;
      if (user.status === "pending") {
        td.appendChild(button("Approve", () => openConfirm(user, { status: "active" })));
      } else if (user.status === "disabled") {
        td.appendChild(button("Re-enable", () => openConfirm(user, { status: "active" })));
      } else if (user.email !== state.me) {
        td.appendChild(button("Disable", () => openConfirm(user, { status: "disabled" }), "danger-link"));
      }
      td.appendChild(button("History", () => showHistory(user.email)));
      return td;
    }

    function renderUsers() {
      show(el.readonly, state.loaded && !state.writable);
      show(el.loading, state.loading && state.users.length === 0);
      show(el.empty, state.loaded && !state.loading && !state.error && state.users.length === 0);
      show(el.error, Boolean(state.error));
      el.error.textContent = state.error ? errorMessage(state.error) : "";
      show(el.table, state.users.length > 0);
      show(el.loadMore, Boolean(state.nextCursor) && !state.error);
      el.loadMore.disabled = state.loading;
      el.tbody.replaceChildren();
      for (const user of state.users) {
        const row = doc.createElement("tr");
        row.appendChild(cell(user.name));
        row.appendChild(cell(user.email));
        row.appendChild(roleCell(user));
        row.appendChild(statusCell(user));
        row.appendChild(cell(formatDateTime(user.last_login_at)));
        row.appendChild(cell(user.annotation_count));
        row.appendChild(actionsCell(user));
        el.tbody.appendChild(row);
      }
    }

    function renderLog() {
      const log = state.log;
      show(el.log, state.writable);
      el.logHeading.textContent = log.email ? `Change history for ${log.email}` : "Change log";
      show(el.logAll, Boolean(log.email));
      show(el.logEmpty, log.loaded && !log.loading && log.changes.length === 0);
      show(el.logTable, log.changes.length > 0);
      show(el.logMore, Boolean(log.nextCursor));
      el.logMore.disabled = log.loading;
      el.logTbody.replaceChildren();
      for (const change of log.changes) {
        const row = doc.createElement("tr");
        row.appendChild(cell(formatDateTime(change.created_at)));
        row.appendChild(cell(change.email));
        row.appendChild(cell(change.field));
        row.appendChild(cell(`${change.old_value == null ? "—" : change.old_value} → ${change.new_value}`));
        row.appendChild(cell(change.actor));
        row.appendChild(cell(change.reason || ""));
        el.logTbody.appendChild(row);
      }
    }

    function usersUrl() {
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      const { search, role, status } = state.filters;
      if (search) params.set("search", search);
      if (role) params.set("role", role);
      if (status) params.set("status", status);
      if (state.nextCursor) params.set("cursor", state.nextCursor);
      return `/v1/admin/users?${params.toString()}`;
    }

    async function loadUsers({ reset = false } = {}) {
      if (state.loading) return;
      if (reset) {
        state.users = [];
        state.nextCursor = null;
      }
      state.loading = true;
      state.error = null;
      renderUsers();
      try {
        const data = await api(usersUrl(), { method: "GET" });
        if (!data || !Array.isArray(data.users)) throw new Error("Invalid users response");
        state.users = state.users.concat(data.users);
        state.nextCursor = data.next_cursor || null;
        state.writable = data.writable === true;
        state.loaded = true;
      } catch (error) {
        state.error = error;
      } finally {
        state.loading = false;
        renderUsers();
        renderLog();
      }
    }

    async function loadLog({ reset = false } = {}) {
      const log = state.log;
      if (log.loading || !state.writable) return;
      if (reset) {
        log.changes = [];
        log.nextCursor = null;
        log.loaded = false;
      }
      log.loading = true;
      renderLog();
      const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
      if (log.nextCursor) params.set("cursor", log.nextCursor);
      const base = log.email
        ? `/v1/admin/users/${encodeURIComponent(log.email)}/changes`
        : "/v1/admin/role-changes";
      try {
        const data = await api(`${base}?${params.toString()}`, { method: "GET" });
        if (!data || !Array.isArray(data.changes)) throw new Error("Invalid change log response");
        log.changes = log.changes.concat(data.changes);
        log.nextCursor = data.next_cursor || null;
        log.loaded = true;
      } catch (error) {
        state.error = error;
        renderUsers();
      } finally {
        log.loading = false;
        renderLog();
      }
    }

    function showHistory(email) {
      state.log.email = email;
      return loadLog({ reset: true });
    }

    function openConfirm(user, change) {
      const info = describeChange(user, change);
      state.pending = { user, change };
      el.confirmMessage.textContent = info.message;
      el.confirmWarning.textContent = info.warning;
      show(el.confirmWarning, Boolean(info.warning));
      el.confirmReason.value = "";
      el.confirmError.textContent = "";
      show(el.confirmError, false);
      el.confirmOk.textContent = info.action;
      el.confirmOk.className = info.danger ? "danger-action" : "primary-action";
      el.confirmOk.disabled = false;
      show(el.confirm, true);
    }

    function closeConfirm() {
      if (state.saving) return;
      state.pending = null;
      show(el.confirm, false);
    }

    async function submitConfirm() {
      if (!state.pending || state.saving) return;
      const { user, change } = state.pending;
      state.saving = true;
      el.confirmOk.disabled = true;
      try {
        const reason = String(el.confirmReason.value || "").trim();
        const data = await api(`/v1/admin/users/${encodeURIComponent(user.email)}/changes`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...change, ...(reason ? { reason } : {}) }),
        });
        state.users = state.users.map((u) => (u.email === data.user.email ? data.user : u));
        state.saving = false;
        closeConfirm();
        renderUsers();
        if (state.log.loaded) await loadLog({ reset: true });
      } catch (error) {
        el.confirmError.textContent = errorMessage(error);
        show(el.confirmError, true);
      } finally {
        state.saving = false;
        el.confirmOk.disabled = false;
      }
    }

    el.filters.addEventListener("submit", (event) => {
      if (event && event.preventDefault) event.preventDefault();
      state.filters = {
        search: String(el.search.value || "").trim(),
        role: el.roleFilter.value || "",
        status: el.statusFilter.value || "",
      };
      return loadUsers({ reset: true });
    });
    el.loadMore.addEventListener("click", () => (state.nextCursor ? loadUsers() : undefined));
    el.logMore.addEventListener("click", () => (state.log.nextCursor ? loadLog() : undefined));
    el.logAll.addEventListener("click", () => showHistory(null));
    el.confirmCancel.addEventListener("click", closeConfirm);
    el.confirmOk.addEventListener("click", submitConfirm);

    async function init() {
      try {
        const me = await api("/auth/me", { method: "GET" });
        state.me = me && me.user && me.user.email ? String(me.user.email).toLowerCase() : null;
      } catch (_) { /* The users request below reports auth problems. */ }
      await loadUsers({ reset: true });
      if (state.writable) await loadLog({ reset: true });
    }

    return { init, state, openConfirm, submitConfirm, showHistory };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createAdminUsersPage, describeChange, isDemotion, formatDateTime };
  } else {
    createAdminUsersPage({ document: root.document, window: root, fetch: root.fetch.bind(root) }).init();
  }
})(typeof window !== "undefined" ? window : globalThis);

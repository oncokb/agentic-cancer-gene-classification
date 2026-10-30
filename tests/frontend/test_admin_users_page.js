// Plain Node fake-DOM harness for src/static/admin-users.js (following
// test_runs_page.js), plus the admin-only "Users" link app.js shows.
// Run via tests/test_frontend_admin_users_page.py (invokes this with node).
"use strict";
const assert = require("assert");
const { createAdminUsersPage, describeChange, isDemotion } = require("../../src/static/admin-users.js");
const { loadApp } = require("./openevidence_gate_harness");

class Element {
  constructor(tag) {
    this.tagName = tag;
    this.textContent = "";
    this.className = "";
    this.value = "";
    this.disabled = false;
    this.children = [];
    this.listeners = {};
    this.attrs = {};
    const classes = new Set();
    this.classList = {
      toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)),
      contains: (name) => classes.has(name),
    };
    // Any innerHTML use would be a bug: user data must stay text.
    Object.defineProperty(this, "innerHTML", {
      set() { throw new Error("innerHTML must not be used"); },
      get() { return ""; },
    });
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  replaceChildren() { this.children = []; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name, event) { return this.listeners[name](event || { preventDefault() {} }); }
  get hidden() { return this.classList.contains("hidden"); }
}

const IDS = [
  "admin-readonly", "admin-error", "admin-filters", "admin-search", "admin-role-filter",
  "admin-status-filter", "admin-empty", "admin-loading", "admin-table", "admin-tbody",
  "admin-load-more", "admin-log", "admin-log-heading", "admin-log-all", "admin-log-empty",
  "admin-log-table", "admin-log-tbody", "admin-log-more", "admin-confirm", "admin-confirm-message",
  "admin-confirm-warning", "admin-confirm-reason", "admin-confirm-error", "admin-confirm-cancel",
  "admin-confirm-ok",
];

const ME = "admin@mskcc.org";
const user = (email, role, status, extra = {}) => ({
  email, name: email.split("@")[0], role, status, last_login_at: "2026-10-01T12:00:00Z",
  annotation_count: 3, ...extra,
});
const ADMIN = user(ME, "admin", "active");
const CURATOR = user("curator@mskcc.org", "curator", "active");
const PENDING = user("pending@mskcc.org", "viewer", "pending");
const DISABLED = user("off@mskcc.org", "viewer", "disabled");
const CHANGE = {
  id: 7, email: CURATOR.email, field: "role", old_value: "curator", new_value: "viewer",
  actor: ME, reason: "<b>left team</b>", request_id: null, created_at: "2026-10-02T12:00:00Z",
};

const response = (body, status = 200) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
const ME_RESPONSE = response({ auth_enabled: true, authenticated: true, user: { email: ME, role: "admin" } });
const usersPage = (users, extra = {}) => response({ users, next_cursor: null, backend: "mysql", writable: true, ...extra });
const logPage = (changes, extra = {}) => response({ changes, next_cursor: null, ...extra });

function env(queue) {
  const nodes = {};
  for (const id of IDS) nodes[id] = new Element(id);
  const calls = [];
  const window = { location: { href: "/admin/users" } };
  const page = createAdminUsersPage({
    document: { getElementById: (id) => nodes[id], createElement: (tag) => new Element(tag) },
    window,
    fetch: async (url, init) => {
      calls.push({ url, init });
      assert.ok(queue.length, `Unexpected request: ${url}`);
      const next = queue.shift();
      if (next instanceof Error) throw next;
      return next;
    },
  });
  return { page, nodes, calls, window };
}

const rows = (e) => e.nodes["admin-tbody"].children;
const rowFor = (e, email) => rows(e).find((row) => row.children[1].textContent === email);
const buttons = (row) => row.children[6].children;
const buttonLabels = (row) => buttons(row).map((b) => b.textContent);
const roleSelect = (row) => row.children[2].children[0];
const allText = (node) => [node.textContent].concat(node.children.flatMap(allText));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("loads users and the change log; rows offer the right status action", async () => {
  const e = env([ME_RESPONSE, usersPage([ADMIN, CURATOR, PENDING, DISABLED]), logPage([CHANGE])]);
  await e.page.init();
  assert.deepStrictEqual(e.calls.map((c) => c.url), [
    "/auth/me", "/v1/admin/users?limit=50", "/v1/admin/role-changes?limit=50",
  ]);
  assert.ok(e.calls.every((c) => c.init.credentials === "same-origin"));
  assert.ok(e.nodes["admin-readonly"].hidden);
  assert.ok(!e.nodes["admin-table"].hidden);
  assert.deepStrictEqual(buttonLabels(rowFor(e, ME)), ["History"]); // no self-disable
  assert.deepStrictEqual(buttonLabels(rowFor(e, CURATOR.email)), ["Disable", "History"]);
  assert.deepStrictEqual(buttonLabels(rowFor(e, PENDING.email)), ["Approve", "History"]);
  assert.deepStrictEqual(buttonLabels(rowFor(e, DISABLED.email)), ["Re-enable", "History"]);
  assert.strictEqual(roleSelect(rowFor(e, CURATOR.email)).value, "curator");
  assert.ok(!e.nodes["admin-log"].hidden);
  const logRow = e.nodes["admin-log-tbody"].children[0];
  assert.strictEqual(logRow.children[3].textContent, "curator → viewer");
  assert.strictEqual(logRow.children[5].textContent, "<b>left team</b>"); // text, not markup
});

test("user data is inserted as text only", async () => {
  const evil = user("x@mskcc.org", "viewer", "active", { name: "<img src=x onerror=alert(1)>" });
  const e = env([ME_RESPONSE, usersPage([evil]), logPage([])]);
  await e.page.init();
  assert.strictEqual(rowFor(e, evil.email).children[0].textContent, evil.name);
});

test("redis backend shows the read-only notice, disables controls and skips the log", async () => {
  const e = env([ME_RESPONSE, usersPage([ADMIN, CURATOR], { backend: "redis", writable: false })]);
  await e.page.init();
  assert.strictEqual(e.calls.length, 2);
  assert.ok(!e.nodes["admin-readonly"].hidden);
  assert.ok(roleSelect(rowFor(e, CURATOR.email)).disabled);
  assert.deepStrictEqual(buttonLabels(rowFor(e, CURATOR.email)), []);
  assert.ok(e.nodes["admin-log"].hidden);
});

test("search and filters reset the list and are encoded; load more passes the cursor", async () => {
  const e = env([
    ME_RESPONSE, usersPage([ADMIN], { next_cursor: "c+/=" }), logPage([]),
    usersPage([CURATOR], { next_cursor: "n" }), usersPage([PENDING]),
  ]);
  await e.page.init();
  assert.ok(!e.nodes["admin-load-more"].hidden);
  e.nodes["admin-search"].value = "  josé & co ";
  e.nodes["admin-role-filter"].value = "curator";
  e.nodes["admin-status-filter"].value = "active";
  await e.nodes["admin-filters"].fire("submit");
  assert.strictEqual(e.calls[3].url, "/v1/admin/users?limit=50&search=jos%C3%A9+%26+co&role=curator&status=active");
  assert.deepStrictEqual(rows(e).map((r) => r.children[1].textContent), [CURATOR.email]);
  await e.nodes["admin-load-more"].fire("click");
  assert.strictEqual(e.calls[4].url, "/v1/admin/users?limit=50&search=jos%C3%A9+%26+co&role=curator&status=active&cursor=n");
  assert.deepStrictEqual(rows(e).map((r) => r.children[1].textContent), [CURATOR.email, PENDING.email]);
  assert.ok(e.nodes["admin-load-more"].hidden);
});

test("demotion asks for confirmation; cancel sends nothing and keeps the stored role", async () => {
  const e = env([ME_RESPONSE, usersPage([ADMIN, CURATOR]), logPage([])]);
  await e.page.init();
  const select = roleSelect(rowFor(e, CURATOR.email));
  select.value = "viewer";
  select.fire("change");
  assert.strictEqual(select.value, "curator");
  assert.ok(!e.nodes["admin-confirm"].hidden);
  assert.strictEqual(e.nodes["admin-confirm-ok"].textContent, "Demote");
  assert.strictEqual(e.nodes["admin-confirm-ok"].className, "danger-action");
  assert.ok(!e.nodes["admin-confirm-warning"].hidden);
  e.nodes["admin-confirm-cancel"].fire("click");
  assert.ok(e.nodes["admin-confirm"].hidden);
  assert.strictEqual(e.calls.length, 3);
});

test("confirming a disable posts the change with the reason, updates the row and reloads the log", async () => {
  const disabled = { ...CURATOR, status: "disabled" };
  const change = { ...CHANGE, field: "status", old_value: "active", new_value: "disabled" };
  const e = env([
    ME_RESPONSE, usersPage([ADMIN, CURATOR]), logPage([]),
    response({ user: disabled, changes: [change] }), logPage([change]),
  ]);
  await e.page.init();
  buttons(rowFor(e, CURATOR.email))[0].fire("click");
  assert.strictEqual(e.nodes["admin-confirm-ok"].textContent, "Disable");
  assert.strictEqual(e.nodes["admin-confirm-ok"].className, "danger-action");
  e.nodes["admin-confirm-reason"].value = "  left team ";
  await e.nodes["admin-confirm-ok"].fire("click");
  const post = e.calls[3];
  assert.strictEqual(post.url, "/v1/admin/users/curator%40mskcc.org/changes");
  assert.strictEqual(post.init.method, "POST");
  assert.deepStrictEqual(JSON.parse(post.init.body), { status: "disabled", reason: "left team" });
  assert.ok(e.nodes["admin-confirm"].hidden);
  assert.deepStrictEqual(buttonLabels(rowFor(e, CURATOR.email)), ["Re-enable", "History"]);
  assert.strictEqual(e.calls[4].url, "/v1/admin/role-changes?limit=50");
  assert.strictEqual(e.nodes["admin-log-tbody"].children.length, 1);
});

test("approve and promotion confirm without the danger styling", async () => {
  const e = env([ME_RESPONSE, usersPage([ADMIN, PENDING]), logPage([])]);
  await e.page.init();
  buttons(rowFor(e, PENDING.email))[0].fire("click");
  assert.strictEqual(e.nodes["admin-confirm-ok"].textContent, "Approve");
  assert.strictEqual(e.nodes["admin-confirm-ok"].className, "primary-action");
  assert.ok(e.nodes["admin-confirm-warning"].hidden);
  assert.deepStrictEqual(describeChange(PENDING, { role: "admin" }).danger, false);
  assert.deepStrictEqual(describeChange(DISABLED, { status: "active" }).action, "Re-enable");
  assert.ok(isDemotion("admin", "curator") && isDemotion("curator", "viewer"));
  assert.ok(!isDemotion("viewer", "admin"));
});

test("a refused change keeps the dialog open with the server's message as text", async () => {
  const detail = "This change would leave no active admin. <script>";
  const e = env([ME_RESPONSE, usersPage([ADMIN, CURATOR]), logPage([]), response({ detail }, 409)]);
  await e.page.init();
  e.page.openConfirm(ADMIN, { role: "viewer" });
  await e.nodes["admin-confirm-ok"].fire("click");
  assert.ok(!e.nodes["admin-confirm"].hidden);
  assert.ok(!e.nodes["admin-confirm-error"].hidden);
  assert.strictEqual(e.nodes["admin-confirm-error"].textContent, detail);
  assert.ok(!e.nodes["admin-confirm-ok"].disabled);
  assert.strictEqual(roleSelect(rowFor(e, ME)).value, "admin");
});

test("double-clicking confirm sends one request", async () => {
  let resolve;
  const pending = new Promise((done) => { resolve = done; });
  const e = env([ME_RESPONSE, usersPage([ADMIN, CURATOR]), logPage([]), pending, logPage([])]);
  await e.page.init();
  e.page.openConfirm(CURATOR, { role: "admin" });
  const first = e.nodes["admin-confirm-ok"].fire("click");
  await e.nodes["admin-confirm-ok"].fire("click");
  resolve(response({ user: { ...CURATOR, role: "admin" }, changes: [] }));
  await first;
  assert.strictEqual(e.calls.filter((c) => c.init.method === "POST").length, 1);
});

test("per-user history and back to all changes", async () => {
  const e = env([
    ME_RESPONSE, usersPage([ADMIN, CURATOR]), logPage([]),
    logPage([CHANGE], { next_cursor: "k" }), logPage([{ ...CHANGE, id: 6 }]), logPage([]),
  ]);
  await e.page.init();
  await buttons(rowFor(e, CURATOR.email))[1].fire("click");
  assert.strictEqual(e.calls[3].url, "/v1/admin/users/curator%40mskcc.org/changes?limit=50");
  assert.strictEqual(e.nodes["admin-log-heading"].textContent, `Change history for ${CURATOR.email}`);
  assert.ok(!e.nodes["admin-log-all"].hidden);
  await e.nodes["admin-log-more"].fire("click");
  assert.strictEqual(e.calls[4].url, "/v1/admin/users/curator%40mskcc.org/changes?limit=50&cursor=k");
  assert.strictEqual(e.nodes["admin-log-tbody"].children.length, 2);
  await e.nodes["admin-log-all"].fire("click");
  assert.strictEqual(e.calls[5].url, "/v1/admin/role-changes?limit=50");
  assert.strictEqual(e.nodes["admin-log-heading"].textContent, "Change log");
  assert.ok(e.nodes["admin-log-all"].hidden);
});

test("errors: 403 and 503 show messages, 401 redirects to login", async () => {
  for (const [status, text] of [[403, "Administrator privileges required."], [503, "unavailable"]]) {
    const e = env([ME_RESPONSE, response({}, status)]);
    await e.page.init();
    assert.ok(!e.nodes["admin-error"].hidden);
    assert.ok(e.nodes["admin-error"].textContent.includes(text));
    assert.ok(e.nodes["admin-empty"].hidden);
  }
  const e = env([response({}, 401), response({}, 401)]);
  await e.page.init();
  assert.strictEqual(e.window.location.href, "/login?redirect_to=%2Fadmin%2Fusers");
});

test("app.js shows the Users menu link to admins only", async () => {
  for (const [role, visible] of [["admin", true], ["curator", false], ["viewer", false]]) {
    const app = loadApp({
      fetchImpl: async () => response({
        auth_enabled: true, authenticated: true, user: { email: "u@mskcc.org", name: "U", role },
      }),
    });
    app.elements.adminUsersLink.classList.add("hidden");
    await app.checkAuth();
    assert.strictEqual(!app.elements.adminUsersLink.classList.contains("hidden"), visible, role);
  }
});

test("page scripts never use innerHTML or tracking", () => {
  const fs = require("fs");
  const path = require("path");
  const source = fs.readFileSync(path.join(__dirname, "..", "..", "src", "static", "admin-users.js"), "utf8");
  assert.ok(!source.includes("innerHTML"));
  assert.ok(!source.includes("ACGCTracking"));
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (error) {
      failed += 1;
      console.error(`not ok - ${name}\n${error.stack}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exitCode = failed ? 1 : 0;
})();

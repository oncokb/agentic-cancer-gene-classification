"use strict";
const assert = require("assert");
const { loadApp } = require("./openevidence_gate_harness");
const { createCuratorRequests } = require("../../src/static/curator-requests.js");
class Element {
  constructor() {
    this.children = []; this.listeners = {}; this.value = ""; this.textContent = "";
    this.disabled = false;
    const classes = new Set(["hidden"]);
    this.classList = { toggle: (name, on) => on ? classes.add(name) : classes.delete(name), contains: (name) => classes.has(name) };
    Object.defineProperty(this, "innerHTML", { set() { throw Error("Unsafe HTML"); } });
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  addEventListener(event, fn) { this.listeners[event] = fn; }
  fire(event) { return this.listeners[event]({ preventDefault() {} }); }
}
const response = (body, status = 200) => ({ ok: status < 400, status, headers: { get: () => "application/json" }, json: async () => body });
const record = { id: 7, email: "<img src=x>", status: "pending", reason: "<script>bad</script>", affiliation: "MSK", expertise: "Genes" };
function env(queue, admin = false, currentUser = { role: "viewer", status: "active" }) {
  const nodes = {}, calls = [];
  const doc = { getElementById: id => nodes[id] || (nodes[id] = new Element()), createElement: () => new Element() };
  const page = createCuratorRequests({ document: doc, admin, fetch: async (url, init) => {
    calls.push({ url, init });
    if (url === "/auth/me") return response({ authenticated: Boolean(currentUser), user: currentUser });
    assert.ok(queue.length, `Unexpected request ${url}`);
    const next = queue.shift();
    if (next instanceof Error) throw next;
    return next;
  } });
  const el = id => doc.getElementById(`curator-${id}`);
  el("filter").value = "pending";
  return { page, calls, el };
}
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
test("user menu entry is visible only for viewers", async () => {
  for (const role of ["viewer", "curator", "admin"]) {
    const app = loadApp({ fetchImpl: async () => response({ auth_enabled: true, authenticated: true, user: { email: "u@mskcc.org", name: "U", role } }) });
    await app.checkAuth();
    assert.equal(app.elements.curatorRequestLink.classList.contains("hidden"), role !== "viewer");
  }
});
test("opening loads latest status; none shows form", async () => {
  const e = env([response({ status: "none", request: null })]);
  e.page.init();
  await e.el("open").fire("click");
  assert.equal(e.calls[0].url, "/v1/curator-requests/me");
  assert.equal(e.el("form").classList.contains("hidden"), false);
  e.el("close").fire("click");
  assert.equal(e.el("dialog").classList.contains("hidden"), true);
});
test("active viewers can reapply after approval but pending requests hide the form", async () => {
  for (const status of ["none", "rejected", "approved", "pending"]) {
    const e = env([response({ status, request: record })]);
    await e.page.loadStatus();
    assert.equal(e.el("form").classList.contains("hidden"), status === "pending");
    assert.ok(e.el("message").textContent.includes(status));
  }
});
test("only currently active viewers see the form after approval", async () => {
  for (const currentUser of [{ role: "curator", status: "active" }, { role: "admin", status: "active" }, { role: "viewer", status: "disabled" }, { role: "viewer", status: "pending" }, null]) {
    const e = env([response({ status: "approved", request: record })], false, currentUser);
    await e.page.loadStatus();
    assert.equal(e.el("form").classList.contains("hidden"), true);
  }
});
test("rejected shows note as text and permits another request", async () => {
  const e = env([response({ status: "rejected", request: { review_note: "<b>retry</b>" } })]);
  await e.page.loadStatus();
  assert.ok(e.el("message").textContent.includes("<b>retry</b>"));
  assert.equal(e.el("form").classList.contains("hidden"), false);
});
test("submission trims inputs, omits empty ORCID and refreshes status", async () => {
  const e = env([response(record), response({ status: "pending", request: record })]);
  e.page.init();
  for (const field of ["reason", "affiliation", "expertise"]) e.el(field).value = ` ${field} `;
  await e.el("form").fire("submit");
  assert.deepEqual(JSON.parse(e.calls[0].init.body), { reason: "reason", affiliation: "affiliation", expertise: "expertise", orcid: null });
  assert.equal(e.calls[0].init.headers["Content-Type"], "application/json");
  assert.ok(e.calls.every(call => call.init.credentials === "same-origin"));
  assert.equal(e.el("submit").disabled, false);
});
test("API conflicts and unavailable backends show useful text", async () => {
  const e = env([response({ detail: "Requires MySQL" }, 409)]);
  await e.page.loadStatus();
  assert.equal(e.el("message").textContent, "Requires MySQL");
});
test("non-JSON proxy errors show a readable HTTP error without parsing HTML", async () => {
  const e = env([{ ok: false, status: 502, headers: { get: () => "text/html" }, json: async () => { throw Error("HTML must not be parsed as JSON"); } }]);
  await e.page.loadStatus();
  assert.equal(e.el("message").textContent, "Request failed (HTTP 502). Please retry shortly.");
});
test("unexpected successful content and malformed JSON show readable errors", async () => {
  for (const contentType of [null, "text/html", "application/json"]) {
    const e = env([{ ok: true, status: 200, headers: { get: () => contentType }, json: async () => { throw SyntaxError("Unexpected token <"); } }]);
    await e.page.loadStatus();
    assert.equal(e.el("message").textContent, "Unexpected server response. Please retry shortly.");
  }
});
test("queue renders user content as text and paginates", async () => {
  const e = env([response({ requests: [record], next_cursor: "abc" }), response({ requests: [{ ...record, id: 6 }], next_cursor: null })], true);
  await e.page.init();
  assert.equal(e.el("rows").children[0].children[0].textContent, record.email);
  assert.equal(e.el("rows").children[0].children[2].textContent, `reason: ${record.reason}`);
  await e.el("more").fire("click");
  assert.ok(e.calls[1].url.includes("cursor=abc"));
  assert.equal(e.el("rows").children.length, 2);
  assert.equal(e.el("more").classList.contains("hidden"), true);
});
test("review requires confirmation; cancel sends nothing", async () => {
  const e = env([response({ requests: [record], next_cursor: null })], true);
  await e.page.init();
  e.el("rows").children[0].children.at(-2).fire("click");
  assert.equal(e.calls.length, 1);
  assert.ok(e.el("confirm-message").textContent.includes(record.email));
  e.el("cancel").fire("click");
  await e.el("review").fire("submit");
  assert.equal(e.calls.length, 1);
});
test("approve and reject submit optional notes and refresh queue", async () => {
  for (const action of ["approve", "reject"]) {
    const e = env([response({ requests: [record], next_cursor: null }), response({}), response({ requests: [], next_cursor: null })], true);
    await e.page.init();
    e.el("rows").children[0].children.at(action === "approve" ? -2 : -1).fire("click");
    e.el("note").value = " <b>note</b> ";
    await e.el("review").fire("submit");
    assert.equal(e.calls[1].url, `/v1/admin/curator-requests/7/${action}`);
    assert.deepEqual(JSON.parse(e.calls[1].init.body), { note: "<b>note</b>" });
    assert.equal(e.el("rows").children.length, 0);
    assert.equal(e.el("confirm").classList.contains("hidden"), true);
  }
});
test("failed review stays open and displays conflict", async () => {
  const e = env([response({ requests: [record], next_cursor: null }), response({ detail: "Already reviewed" }, 409)], true);
  await e.page.init();
  e.el("rows").children[0].children.at(-2).fire("click");
  await e.el("review").fire("submit");
  assert.equal(e.el("review-error").textContent, "Already reviewed");
  assert.equal(e.el("confirm").classList.contains("hidden"), false);
  assert.equal(e.el("review-submit").disabled, false);
});
test("filter refresh replaces previous rows and cursor", async () => {
  const e = env([response({ requests: [record], next_cursor: "abc" }), response({ requests: [{ ...record, status: "rejected" }], next_cursor: null })], true);
  await e.page.init();
  e.el("filter").value = "rejected";
  await e.el("filters").fire("submit");
  assert.ok(e.calls[1].url.includes("status=rejected"));
  assert.ok(!e.calls[1].url.includes("cursor"));
  assert.equal(e.el("rows").children.length, 1);
  assert.equal(e.el("rows").children[0].children.filter(node => node.listeners.click).length, 0);
});
(async () => {
  for (const [name, fn] of tests) { await fn(); console.log(`PASS ${name}`); }
  console.log(`${tests.length} test cases passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });

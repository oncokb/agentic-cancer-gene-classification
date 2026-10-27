// Browser UI engagement tracking (contract: docs/ui-events.md). Plain script,
// loaded before app.js / runs.js, which reach it as window.ACGCTracking.
// Sends nothing until GET /v1/dev/status reports ui_events_enabled: true;
// events recorded before that answer arrives are held, then sent or dropped.
(function (root) {
  "use strict";

  const SCHEMA_VERSION = 1;
  const ENDPOINT = "/v1/events";
  const BATCH_SIZE = 50;
  const FLUSH_INTERVAL_MS = 5000;
  const MAX_QUEUE = 500;
  const MAX_NETWORK_RETRIES = 3;
  const DEFAULT_RETRY_AFTER_MS = 30000;
  const DWELL_MS = 1000;
  const VISIBLE_RATIO = 0.5;
  // 0.5 is the impression threshold; the lower steps let an element taller
  // than the viewport qualify once it fills half of it.
  const IO_THRESHOLDS = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1];

  const TYPES = new Set(["surfaced", "impression", "open", "close", "click", "state", "action"]);
  const SURFACES = new Set(["results", "fusion_evidence", "no_result", "sidebar", "runs", "feedback", "global"]);
  const SECTIONS = new Set([
    "gene_card", "rationale", "gene_summary", "lazy_field", "retrieved_pmids", "evidence_cards",
    "evidence_card", "supporting_quotes", "supporting_quote", "pmid_link", "clinical_actionability",
    "score_breakdown", "openevidence", "openevidence_content", "openevidence_guideline_link",
    "domains_treatments", "fusion_section", "fusion_card", "fusion_paper", "fusion_partner_check",
    "sidebar_entry", "sidebar_filter", "tab", "runs_page", "runs_row", "runs_pagination", "feedback_modal",
  ]);
  const STATES = {
    state: new Set(["loading", "pending", "ready", "cached", "unavailable", "failed", "timeout"]),
    action: new Set([
      "filter_used", "filter_cleared", "jump", "load_more", "retry", "submit", "submit_failed", "opened_run",
    ]),
  };
  const SUBJECT_KINDS = new Set(["gene", "fusion", "run", "none"]);
  // Context a child element takes from the nearest registered ancestor
  // (e.g. a quote inside a gene card). section/item_id are never inherited.
  const INHERITED = ["surface", "subject_kind", "subject_id", "position", "display_position", "tumor_type_given", "tab"];
  const PUBMED_HREF = /^https?:\/\/pubmed\.ncbi\.nlm\.nih\.gov\/(\d{1,32})\/?(?:[?#].*)?$/;

  function uuid4(cryptoObj) {
    if (cryptoObj && typeof cryptoObj.randomUUID === "function") return cryptoObj.randomUUID();
    const bytes = new Uint8Array(16);
    if (cryptoObj && typeof cryptoObj.getRandomValues === "function") cryptoObj.getRandomValues(bytes);
    else for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  const idOrNull = (value, max) => {
    if (value === null || value === undefined || value === "") return null;
    const text = String(value);
    return text.length <= max ? text : null;
  };
  const intOrNull = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
  const boolOrNull = (value) => (typeof value === "boolean" ? value : null);

  function createTracker(env = {}) {
    const win = env.window || null;
    const doc = env.document || null;
    const nav = env.navigator || null;
    const fetchFn = env.fetch || null;
    const IO = env.IntersectionObserver || null;
    const setTimer = env.setTimeout || setTimeout;
    const clearTimer = env.clearTimeout || clearTimeout;
    const now = env.now || (() => Date.now());
    const defer = env.queueMicrotask || ((fn) => Promise.resolve().then(fn));
    const newId = env.uuid || (() => uuid4(env.crypto));

    const pageViewId = newId();
    // null until the dev-status answer: hold events; false: drop and never send.
    let enabled = null;
    let flagsProvider = null;
    let activeTab = null;

    let scope = { token: null, ids: new Set(), runId: null };
    let surfacedKeys = new Set();
    let stateKeys = new Set();
    const entries = new Map(); // logical key -> impression entry
    const nodeKey = new WeakMap(); // DOM node -> logical key
    const nodeContext = new WeakMap(); // DOM node -> descriptor it was registered with
    let pending = [];
    const timing = new Set(); // entries with a running dwell timer

    let queue = [];
    let flushTimer = null;
    let inflight = false;
    let blockedUntil = 0;
    let networkFailures = 0;
    const stats = { sent: 0, dropped: 0, batches: 0, beacons: 0 };

    const observer = IO ? new IO(onIntersect, { root: null, threshold: IO_THRESHOLDS }) : null;

    // ---- descriptors -------------------------------------------------------

    function parentOf(node) {
      return node ? node.parentNode || node.parentElement || node._parent || null : null;
    }

    // The descriptor for `el`: inheritable context from its nearest
    // registered ancestors (nearest wins), then `partial`.
    function resolve(el, partial) {
      const merged = {};
      for (let node = parentOf(el); node; node = parentOf(node)) {
        const ctx = nodeContext.get(node);
        if (!ctx) continue;
        INHERITED.forEach((field) => {
          if (!(field in merged) && ctx[field] !== undefined) merged[field] = ctx[field];
        });
      }
      return { ...merged, ...(partial || {}) };
    }

    // Nearest registered node at or above `el`, resolved with its own section.
    function contextOf(el) {
      for (let node = el; node; node = parentOf(node)) {
        const ctx = nodeContext.get(node);
        if (ctx) return resolve(node, ctx);
      }
      return {};
    }

    function logicalKey(desc, scopeToken) {
      return [
        scopeToken ?? "", desc.surface, desc.subject_kind || "none", desc.subject_id ?? "",
        desc.section, desc.item_id ?? "",
      ].join("|");
    }

    function flags() {
      if (!flagsProvider) return null;
      try {
        const value = flagsProvider();
        if (!value || typeof value !== "object") return null;
        const keys = Object.keys(value);
        if (!keys.length || keys.length > 10) return null;
        const out = {};
        for (const key of keys) {
          const flag = value[key];
          const variant = idOrNull(flag && flag.variant, 32);
          if (variant === null || !Number.isInteger(flag.version)) return null;
          out[key] = { variant, version: flag.version };
        }
        return out;
      } catch (_) {
        return null;
      }
    }

    // Builds the wire event from an explicit whitelist, so nothing a call
    // site passes beyond the contract's fields (free text included) is sent.
    function build(desc, runId) {
      const type = desc.type;
      const stateValue = STATES[type] ? desc.state : null;
      if (!TYPES.has(type) || !SURFACES.has(desc.surface) || !SECTIONS.has(desc.section)) return null;
      if (STATES[type] && !STATES[type].has(stateValue) && !(type === "state" && stateValue == null)) return null;
      const subjectKind = SUBJECT_KINDS.has(desc.subject_kind) ? desc.subject_kind : "none";
      return {
        event_id: newId(),
        ts: now(),
        type,
        surface: desc.surface,
        section: desc.section,
        state: stateValue ?? null,
        subject_kind: subjectKind,
        subject_id: subjectKind === "none" ? null : idOrNull(desc.subject_id, 64),
        run_id: idOrNull(desc.run_id !== undefined ? desc.run_id : runId, 64),
        item_id: idOrNull(desc.item_id, 32),
        position: intOrNull(desc.position),
        display_position: intOrNull(desc.display_position),
        tumor_type_given: boolOrNull(desc.tumor_type_given),
        flags: flags(),
      };
    }

    function emit(desc, runId = scope.runId) {
      if (enabled === false) return;
      const event = build(desc, runId);
      if (!event) return;
      if (queue.length >= MAX_QUEUE) {
        stats.dropped += 1;
        return;
      }
      queue.push(event);
      if (queue.length >= BATCH_SIZE) flush();
      else scheduleFlush(FLUSH_INTERVAL_MS);
    }

    // ---- registration ------------------------------------------------------

    function register(el, partial, { surfaced }) {
      if (!el || !partial || enabled === false) return;
      nodeContext.set(el, partial);
      // Resolved after the current synchronous render, once the element has
      // been attached under the card it inherits its subject from.
      pending.push({ el, partial, surfaced, scopeToken: scope.token, runId: scope.runId });
      if (pending.length === 1) defer(processPending);
    }

    function processPending() {
      const batch = pending;
      pending = [];
      batch.forEach(({ el, partial, surfaced, scopeToken, runId }) => {
        if (scopeToken !== scope.token) return; // registered under a run since replaced
        const desc = resolve(el, partial);
        if (!SURFACES.has(desc.surface) || !SECTIONS.has(desc.section)) return;
        const key = logicalKey(desc, scopeToken);
        if (surfaced && !surfacedKeys.has(key)) {
          surfacedKeys.add(key);
          emit({ ...desc, type: "surfaced" }, runId);
        }
        let entry = entries.get(key);
        if (!entry) {
          entry = { key, desc, node: null, impressed: false, visible: false, timer: null };
          entries.set(key, entry);
        }
        entry.desc = desc;
        if (entry.node === el) return;
        // A re-render replaced the node: keep the entry (and any running
        // dwell) and watch the replacement instead; its first intersection
        // report decides whether the dwell continues.
        if (entry.node && observer) observer.unobserve(entry.node);
        entry.node = el;
        nodeKey.set(el, key);
        if (!entry.impressed && observer) observer.observe(el);
      });
    }

    // ---- impressions -------------------------------------------------------

    function documentVisible() {
      return !doc || doc.visibilityState !== "hidden";
    }

    function tabActive(entry) {
      return entry.desc.tab == null || entry.desc.tab === activeTab;
    }

    function qualifies(record) {
      if (!record.isIntersecting) return false;
      if (record.intersectionRatio >= VISIBLE_RATIO) return true;
      const rootHeight = record.rootBounds ? record.rootBounds.height : win && win.innerHeight;
      return Boolean(rootHeight) && record.intersectionRect.height >= VISIBLE_RATIO * rootHeight;
    }

    function onIntersect(records) {
      records.forEach((record) => {
        const entry = entries.get(nodeKey.get(record.target));
        if (!entry || entry.node !== record.target || entry.impressed) return;
        entry.visible = qualifies(record);
        if (entry.visible) startDwell(entry);
        else cancelDwell(entry);
      });
    }

    function startDwell(entry) {
      if (entry.impressed || entry.timer || !entry.visible || !documentVisible() || !tabActive(entry)) return;
      entry.timer = setTimer(() => expire(entry), DWELL_MS);
      timing.add(entry);
    }

    function cancelDwell(entry) {
      if (!entry.timer) return;
      clearTimer(entry.timer);
      entry.timer = null;
      timing.delete(entry);
    }

    // Visible fraction of `node` after clipping by the viewport and by any
    // scrolling/clipping ancestor (the results pane scrolls on its own).
    function measuredVisible(node) {
      if (!node.getBoundingClientRect || !win) return true;
      const rect = node.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (!area) return false;
      let top = Math.max(rect.top, 0);
      let left = Math.max(rect.left, 0);
      let bottom = Math.min(rect.bottom, win.innerHeight);
      let right = Math.min(rect.right, win.innerWidth);
      let clipHeight = win.innerHeight;
      if (typeof win.getComputedStyle === "function") {
        for (let p = node.parentElement; p && p !== doc.documentElement; p = p.parentElement) {
          const style = win.getComputedStyle(p);
          if (style.overflowY === "visible" && style.overflowX === "visible") continue;
          const clip = p.getBoundingClientRect();
          top = Math.max(top, clip.top);
          left = Math.max(left, clip.left);
          bottom = Math.min(bottom, clip.bottom);
          right = Math.min(right, clip.right);
          clipHeight = Math.min(clipHeight, clip.height);
        }
      }
      const height = Math.max(0, bottom - top);
      const width = Math.max(0, right - left);
      if (!height || !width) return false;
      return (height * width) / area >= VISIBLE_RATIO || height >= VISIBLE_RATIO * clipHeight;
    }

    function expire(entry) {
      entry.timer = null;
      timing.delete(entry);
      const node = entry.node;
      if (entry.impressed || !node) return;
      if (!node.isConnected || !entry.visible || !documentVisible() || !tabActive(entry)) return;
      if (!measuredVisible(node)) {
        entry.visible = false;
        return;
      }
      entry.impressed = true;
      if (observer) observer.unobserve(node);
      emit({ ...entry.desc, type: "impression" });
    }

    function pauseAll() {
      [...timing].forEach(cancelDwell);
    }

    function resumeAll() {
      entries.forEach((entry) => startDwell(entry));
    }

    // ---- transport ---------------------------------------------------------

    function scheduleFlush(delayMs) {
      if (flushTimer) return;
      flushTimer = setTimer(() => {
        flushTimer = null;
        flush();
      }, delayMs);
    }

    function envelope(events) {
      return JSON.stringify({ schema_version: SCHEMA_VERSION, page_view_id: pageViewId, events });
    }

    function requeue(batch) {
      queue = batch.concat(queue).slice(0, MAX_QUEUE);
    }

    async function flush() {
      if (enabled !== true || inflight || !queue.length || !fetchFn) return;
      const wait = blockedUntil - now();
      if (wait > 0) {
        scheduleFlush(wait);
        return;
      }
      const batch = queue.splice(0, BATCH_SIZE);
      inflight = true;
      let response;
      try {
        response = await fetchFn(ENDPOINT, {
          method: "POST",
          credentials: "same-origin",
          keepalive: true,
          headers: { "Content-Type": "application/json", "X-ACGC-Events": "1" },
          body: envelope(batch),
        });
      } catch (_) {
        inflight = false;
        networkFailures += 1;
        if (networkFailures > MAX_NETWORK_RETRIES) {
          networkFailures = 0;
          stats.dropped += batch.length;
        } else {
          requeue(batch);
        }
        scheduleFlush(1000 * 2 ** Math.min(networkFailures, 5));
        return;
      }
      inflight = false;
      stats.batches += 1;
      const status = response.status;
      if (status === 429) {
        const header = response.headers && response.headers.get ? response.headers.get("Retry-After") : null;
        const seconds = Number(header);
        blockedUntil = now() + (Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 : DEFAULT_RETRY_AFTER_MS);
        requeue(batch);
        scheduleFlush(blockedUntil - now());
        return;
      }
      if (status >= 500) {
        networkFailures += 1;
        if (networkFailures > MAX_NETWORK_RETRIES) {
          networkFailures = 0;
          stats.dropped += batch.length;
        } else {
          requeue(batch);
        }
        scheduleFlush(1000 * 2 ** Math.min(networkFailures, 5));
        return;
      }
      networkFailures = 0;
      if (status === 204) {
        // The server has events switched off: stop sending for this page.
        setEnabled(false);
        return;
      }
      if (status >= 400) stats.dropped += batch.length; // 4xx: the batch itself is bad
      else stats.sent += batch.length;
      if (queue.length >= BATCH_SIZE) flush();
      else if (queue.length) scheduleFlush(FLUSH_INTERVAL_MS);
    }

    // Page is going away or hidden: hand what's queued to sendBeacon, which
    // can't set headers, hence ?beacon=1 (see the contract's Transport).
    function flushBeacon() {
      if (enabled !== true || !queue.length || blockedUntil > now()) return;
      if (!nav || typeof nav.sendBeacon !== "function") {
        flush();
        return;
      }
      while (queue.length) {
        const batch = queue.slice(0, BATCH_SIZE);
        let accepted = false;
        try {
          accepted = nav.sendBeacon(`${ENDPOINT}?beacon=1`, envelope(batch));
        } catch (_) {
          accepted = false;
        }
        if (!accepted) break; // keep the rest for a later fetch flush
        queue.splice(0, batch.length);
        stats.beacons += 1;
        stats.sent += batch.length;
      }
    }

    // ---- page / link wiring ------------------------------------------------

    function onLinkActivate(event) {
      if (event.type === "auxclick" && event.button !== 1) return;
      const target = event.target;
      const link = target && typeof target.closest === "function" ? target.closest("a[href]") : null;
      if (!link) return;
      let section = link.dataset && link.dataset.trackSection;
      let itemId = link.dataset && link.dataset.trackItem;
      if (!section) {
        const match = PUBMED_HREF.exec(link.href || "");
        if (!match) return;
        section = "pmid_link";
        itemId = match[1];
      }
      const ctx = contextOf(link);
      const desc = { type: "click", section, item_id: itemId, surface: ctx.surface || "global" };
      INHERITED.forEach((field) => {
        if (field !== "surface" && ctx[field] !== undefined) desc[field] = ctx[field];
      });
      emit(desc);
    }

    if (doc && typeof doc.addEventListener === "function") {
      doc.addEventListener("click", onLinkActivate, true);
      doc.addEventListener("auxclick", onLinkActivate, true);
      doc.addEventListener("visibilitychange", () => {
        if (doc.visibilityState === "hidden") {
          pauseAll();
          flushBeacon();
        } else {
          resumeAll();
        }
      });
    }
    if (win && typeof win.addEventListener === "function") {
      win.addEventListener("pagehide", () => {
        pauseAll();
        flushBeacon();
      });
    }

    // ---- public API --------------------------------------------------------

    function setEnabled(value) {
      enabled = value === true;
      if (!enabled) {
        queue = [];
        clearTimer(flushTimer);
        flushTimer = null;
        return;
      }
      if (queue.length) scheduleFlush(0);
    }

    function setRunScope(runId) {
      const id = runId == null ? null : String(runId);
      if (id !== null && scope.ids.has(id)) return;
      if (id === null && scope.token === null) return;
      // A genuinely new run: dedupe starts over and old nodes stop counting.
      pauseAll();
      entries.forEach((entry) => {
        if (entry.node && observer) observer.unobserve(entry.node);
      });
      entries.clear();
      surfacedKeys = new Set();
      stateKeys = new Set();
      scope = { token: id, ids: new Set(id === null ? [] : [id]), runId: id };
    }

    // A job's progress id becomes its final run_id: same run, same dedupe.
    function aliasRunScope(jobId, runId) {
      if (jobId == null || runId == null || !scope.ids.has(String(jobId))) return;
      scope.ids.add(String(runId));
      scope.runId = String(runId);
    }

    function setActiveTab(tab) {
      if (tab === activeTab) return;
      activeTab = tab;
      entries.forEach((entry) => {
        if (tabActive(entry)) startDwell(entry);
        else cancelDwell(entry);
      });
    }

    // One state event per (logical element, state) per run; "ready" and
    // "cached" are the same transition reached two ways.
    function trackState(el, stateValue) {
      const ctx = contextOf(el);
      if (!ctx.section) return;
      const slot = stateValue === "cached" ? "ready" : stateValue;
      const key = `${logicalKey(ctx, scope.token)}|${slot}`;
      if (stateKeys.has(key)) return;
      stateKeys.add(key);
      emit({ ...ctx, type: "state", state: stateValue });
    }

    // Explicit interaction. `target` (optional) supplies inherited context.
    function trackEvent(target, partial) {
      if (partial === undefined) {
        partial = target;
        target = null;
      }
      emit(target ? { ...contextOf(target), ...partial } : partial);
    }

    // open/close for a toggle the user caused; script-driven toggles (the
    // sidebar opening a collapsed section via .click()) are untrusted.
    function trackToggle(el, open, event) {
      if (!event || event.isTrusted !== true) return;
      trackEvent(el, { type: open ? "open" : "close" });
    }

    // <details>: impression on the summary row plus user open/close. Nothing
    // here restores `open` from state, so every toggle event is the user's.
    function trackDetails(el, partial) {
      register(el, partial, { surfaced: false });
      el.addEventListener("toggle", () => trackEvent(el, { type: el.open ? "open" : "close" }));
    }

    function loadStatus() {
      if (!fetchFn) {
        setEnabled(false);
        return Promise.resolve(false);
      }
      return Promise.resolve()
        .then(() => fetchFn("/v1/dev/status", { credentials: "same-origin" }))
        .then((response) => (response.ok ? response.json() : {}))
        .then((payload) => {
          setEnabled(payload && payload.ui_events_enabled === true);
          return enabled;
        })
        .catch(() => {
          setEnabled(false);
          return false;
        });
    }

    return {
      pageViewId,
      setEnabled,
      loadStatus,
      setFlagsProvider: (fn) => {
        flagsProvider = typeof fn === "function" ? fn : null;
      },
      setRunScope,
      aliasRunScope,
      setActiveTab,
      surfaced: (el, partial) => register(el, partial, { surfaced: true }),
      observe: (el, partial) => register(el, partial, { surfaced: false }),
      details: trackDetails,
      event: trackEvent,
      state: trackState,
      toggled: trackToggle,
      flush,
      flushBeacon,
      _debug: () => ({ queue: queue.slice(), entries, stats: { ...stats }, enabled, scope }),
    };
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createTracker };
  } else {
    root.ACGCTracking = createTracker({
      window: root,
      document: root.document,
      navigator: root.navigator,
      fetch: root.fetch ? root.fetch.bind(root) : null,
      IntersectionObserver: root.IntersectionObserver,
      crypto: root.crypto,
      queueMicrotask: root.queueMicrotask ? root.queueMicrotask.bind(root) : undefined,
    });
  }
})(typeof window !== "undefined" ? window : globalThis);

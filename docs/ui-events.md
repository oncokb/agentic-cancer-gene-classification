# ACGC UI events — contract v1 (shared by T1 server and T2 client)

## Transport
- `POST /v1/events`, `Content-Type: application/json`, header `X-ACGC-Events: 1` (CSRF guard).
- Same-origin only: if `Origin` is present it must match the app's own origin; also accept `Sec-Fetch-Site: same-origin`.
- `navigator.sendBeacon` can't set headers: server also accepts `?beacon=1` instead of the header, still requiring same-origin Origin / Sec-Fetch-Site. Beacon bodies may arrive as `text/plain`; parse as JSON.
- Auth: browser session (via `require_auth`). API-key requests → 403 (events are a UI concept).
- Response `202 {"accepted": n, "rejected": m, "duplicates": d}`. `413` if body > 32 KB; `422` if envelope invalid; `429` + `Retry-After` when rate-limited (per user, shared across pods via Redis; default 120 batches/min; fail-open = drop and 202 if Redis down, never 5xx storms); `204` no-op when `UI_EVENTS_ENABLED=false`.
- Client learns the switch from `GET /v1/dev/status` → `"ui_events_enabled": bool`; when false it sends nothing.
- Config: `UI_EVENTS_ENABLED` (default false), `UI_EVENTS_RATE_LIMIT_PER_MINUTE` (120), `UI_EVENTS_METRIC_FLAG_KEYS` (default empty).

## Envelope
{"schema_version": 1, "page_view_id": "<uuid4>", "events": [Event, ...]}   // 1..50 events

## Event fields
| field | type | rules |
|---|---|---|
| event_id | uuid4 | required; server dedupes 10 min (Redis; in-memory fallback) |
| ts | int | client epoch ms; drop if > 24 h old or > 5 min in future |
| type | enum | surfaced, impression, open, close, click, state, action |
| surface | enum | results, fusion_evidence, no_result, sidebar, runs, feedback, global |
| section | enum | gene_card, rationale, gene_summary, lazy_field, retrieved_pmids, evidence_cards, evidence_card, supporting_quotes, supporting_quote, pmid_link, clinical_actionability, score_breakdown, openevidence, openevidence_content, openevidence_guideline_link, domains_treatments, fusion_section, fusion_card, fusion_paper, fusion_partner_check, sidebar_entry, sidebar_filter, tab, runs_page, runs_row, runs_pagination, feedback_modal |
| state | enum/null | openevidence & type=state: loading, pending, ready, cached, unavailable, failed, timeout. type=action: filter_used, filter_cleared, jump, load_more, retry, submit, submit_failed, opened_run. Otherwise null |
| subject_kind | enum | gene, fusion, run, none |
| subject_id | str ≤64 / null | gene symbol, canonical A::B fusion, or run id. Never user-typed free text |
| run_id | str ≤64 / null | |
| item_id | str ≤32 / null | e.g. PMID, lazy field name, tab name, fusion section key |
| position | int ≥0 / null | original index in the run's result list |
| display_position | int ≥0 / null | index as currently displayed |
| tumor_type_given | bool / null | whether the run had a tumor type (never the value) |
| flags | obj / null | {flag_key: {"variant": str ≤32, "version": int}}, ≤10 keys. Client sends null until feature flags ship |

Unknown fields or bad values → that event is rejected (counted), not the whole batch.

## Semantics
- surfaced: element mounted/offered, once per logical element per page view.
- impression: ≥50% in viewport, document visible, its tab active, continuously ≥1 s; once per logical element per page view.
- Logical key = page_view_id + run scope + surface + subject + section + item_id (NOT the DOM node). Re-renders (job polling ~1.5 s, enrichment, tab switches) must not produce new surfaced/impression events. Temporary job_id aliases to final run_id without resetting dedupe.
- openevidence = the card frame; openevidence_content = the ready body. A loading impression must not consume the ready one. OpenEvidence state transitions emit type=state once per transition.
- open/close: user-initiated toggles only (not restored state on re-render).
- click: link activation (click + middle-click auxclick).
- Privacy: no emails, no filter/query text, no feedback contents, no free text.

## Server output (T1)
- Metric `acgc.ui.events` (count) via src/observability.py helpers; tags ONLY type, surface, section, state, plus exp:<key>/variant:<v> for keys in UI_EVENTS_METRIC_FLAG_KEYS. Never gene/run/pmid/user tags.
- Also `acgc.ui.events.rejected{reason}`, `acgc.ui.batches{outcome}`.
- Structured log `ui_event` per accepted event: all fields + existing hashed user key + server ts + auth provider. No email.

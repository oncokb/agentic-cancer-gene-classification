# Datadog: OpenEvidence sidecar metrics

The OpenEvidence sidecar (`GET /v1/genes/{gene}/openevidence`, its background
lookups, and the offline cache warmup) emits DogStatsD metrics through
`src/observability.py`, like every other product metric. They exist to verify
the sidecar's optimizations (the `osler` model switch, cache warming, the async
"pending + poll" flow) and to track paid OpenEvidence usage.

- Emitted only when `DATADOG_METRICS_ENABLED=true` (nothing is sent otherwise).
- Every name is prefixed by the DogStatsD namespace, `DATADOG_METRICS_NAMESPACE`
  (default `acgc`), so `openevidence.upstream.calls` is sent as
  `acgc.openevidence.upstream.calls`.
- Global tags are not added by the app (`_merge_tags` adds none). The DogStatsD
  client adds `env`, `service`, and `version` from `DD_ENV` / `DD_SERVICE` /
  `DD_VERSION` (set at deploy time, see the main README), so every metric
  carries `service:agentic-cancer-gene-classification`.
- Metrics are best-effort: a failing metrics client never changes a response or
  a lookup. They also never block the event loop. The shared DogStatsd client
  (used by every `acgc.*` metric) runs the library's background sender, and
  emitting only enqueues a packet into a bounded queue (4096 packets) that
  drops instead of blocking when full. The client is built once at app
  startup, off the event loop, and stopped (with a 2s cap) at shutdown. If
  it cannot be built (for example a bad `DD_*` setting), startup continues
  and metrics stay off for the life of the process. Requires
  `datadog>=0.55.0`. With `OPENEVIDENCE_ENABLED=false` the endpoint's response is
  byte-identical to before; only `endpoint.requests{outcome:disabled}` and its
  duration are counted.

## Metrics

| Metric (`acgc.` prefix) | Type | Tags | Meaning |
| --- | --- | --- | --- |
| `openevidence.endpoint.requests` | count | `outcome`, `model`, `available`, `has_fusion`, `has_tumor_type` | One per request to the sidecar endpoint. `outcome` is one of the values listed below the table. `available:true` means a card was returned. |
| `openevidence.endpoint.duration_ms` | distribution | `outcome`, `model` | Endpoint response time. |
| `openevidence.upstream.calls` | count | `model`, `status`, `source`, `http_status_class` (when a response arrived) | One per **paid** HTTP request to `/streaming/analysis`. Each retry attempt is its own call. `status`: `success`, `timeout`, `http_error`, `error` (network/other), or `cancelled` (cut off by the sidecar's overall lookup budget or by shutdown). `source`: `sidecar`, `warmup`, or `other` (ad-hoc or benchmark use of `OpenEvidenceClient`). `http_status_class`: `2xx`, `4xx`, or `5xx`. |
| `openevidence.upstream.duration_ms` | distribution | `model`, `status`, `source` | Duration of one upstream call, from the request start until the full answer has streamed (or the call fails). |
| `openevidence.upstream.time_to_first_chunk_ms` | distribution | `model`, `source` | Time from the request start to the first streamed chunk. |
| `openevidence.retries` | count | `model`, `reason`, `source` | Retries scheduled by the client (up to 2 per lookup, so 3 attempts in all). `reason`: `http_408`, `http_429`, `http_5xx`, or `network`. A timeout is never retried. |
| `openevidence.cache.lookups` | count | `model`, `result` | The endpoint's cache-only peek at the shared Redis cache. `result`: `hit`, `miss`, or `error`. This peek does not go through `cached_call`, so it is **not** counted in `redis_cache.lookups`. `redis_cache.lookups{cache_name:openevidence}` counts only the read-through lookups made by background lookups and warmup. Nothing is double counted. |
| `openevidence.lookups.started` | count | `model` | Background lookups started: the cache missed, this request claimed the lease, and a paid call is on its way. |
| `openevidence.lookups.deduped` | count | `model`, `scope` | Requests that joined a lookup already in flight instead of paying for a new one. `scope:in_process` means the lookup is in this worker's task registry; this includes every poll while a lookup runs. `scope:cross_pod` means another worker or pod holds the Redis lease. |
| `openevidence.lookups.completed` | count | `model`, `result` | Background lookup outcomes, exactly one per `lookups.started` (recorded when the lookup's task finishes, however it finishes). `result`: `success`, `failed`, `abandoned` (the lease was lost before the paid call, so no call was made), `discarded` (the lease was lost mid-call and the failure was not published), `cancelled` (cancelled at shutdown or while queued, including before it ever ran; no failure marker is published), or `error` (crashed unexpectedly). |
| `openevidence.lookups.time_to_ready_ms` | distribution | `model` | Server-side time from the start of a background lookup (the first request that missed) until the answer is available to polls. It includes the semaphore wait. Measured without new state, so it starts at the lookup's start, not at the first "pending" response; the two differ by at most `OPENEVIDENCE_SIDECAR_PENDING_WAIT_SECONDS`. |
| `openevidence.semaphore_wait_ms` | distribution | `model` | Time a background lookup waited for a slot under `OPENEVIDENCE_SIDECAR_CONCURRENCY` (3 by default). |
| `openevidence.lease.unavailable` | count | `model`, `op` | Redis-lease fallbacks: the claim (`op:claim`) or a renewal (`op:renew`) hit a Redis error. The lookup then proceeds without a cross-pod lease, so a duplicate paid call is possible. |

`outcome` values for `openevidence.endpoint.requests`:

- `cache_hit`: the answer came from the shared Redis cache, including entries filled by warmup.
- `ready`: the answer came from this worker's own just-finished lookup. This is usually a poll after "pending", or a lookup that finished within the pending-wait window.
- `pending`: no answer yet.
- `failed`: the lookup failed, or a recent failure is on record.
- `not_cancer_associated`: skipped because the gene was annotated as not cancer associated.
- `disabled`: `OPENEVIDENCE_ENABLED` is false.
- `error`: the endpoint raised.

`unauthenticated` is not reachable: `require_auth` rejects the request before
the handler runs, so the count would need middleware. It is left out on
purpose.

**Cardinality.** Every tag value comes from a small fixed set (model name, the
enums above, booleans, and the HTTP status class). Raw genes, fusions, tumor
types, PMIDs, users, URLs, and cache keys are never tags. No per-gene
dimension is emitted. If one is ever needed, use `gene_latency_tag`
(watchlist bucket or `other`).

**Cache hit rate.** Pending polls inflate a naive peek hit rate: a lookup that
takes 2 minutes produces about 12 misses at a 10s poll interval. The dashboard's
primary hit rate is therefore *per demand*:
`cache_hit / (cache_hit + lookups.started)`. That is the share of distinct
asks answered from cache rather than paid for. The raw peek hit rate is plotted
next to it for reference.

## Dashboard widget group

[`openevidence_dashboard_widgets.json`](openevidence_dashboard_widgets.json)
is a Datadog **group** widget titled "OpenEvidence sidecar". It contains:

- Cache hit rate % (query value and timeseries).
- Total paid calls.
- Endpoint requests by outcome (stacked bars).
- Paid upstream calls by model, status, and source.
- Upstream latency p50, p90, and p99 by model.
- Endpoint response time p50 and p95.
- Retries, upstream failures and timeouts, and failed answers.
- Semaphore wait p90, time to ready, and time to first chunk.
- Lookups started, deduped, and completed, plus lease fallbacks.

Every query is scoped to `service:agentic-cancer-gene-classification`. If the
target dashboard has an `$env` or `$service` template variable, find and
replace that filter (for example with `$service,$env`) before importing.

**Percentiles.** p50, p90, p95, and p99 on a distribution metric need
percentile aggregations enabled for that metric. In Datadog, open
**Metrics → Summary**, select the metric (for example
`acgc.openevidence.upstream.duration_ms`), then **Manage Tags → Enable
percentiles**. Do this for `acgc.openevidence.upstream.duration_ms`,
`acgc.openevidence.endpoint.duration_ms`, `acgc.openevidence.semaphore_wait_ms`,
`acgc.openevidence.lookups.time_to_ready_ms`, and
`acgc.openevidence.upstream.time_to_first_chunk_ms`.

### Import through the API (recommended)

This is the primary path for dashboard `s7n-fsn-pqm`. Use your own keys from the environment, and never commit or hardcode them.
This script GETs the dashboard, appends the group, and PUTs it back. Run it
from the repo root.

```bash
export DD_SITE="${DD_SITE:-datadoghq.com}"
: "${DD_API_KEY:?set DD_API_KEY}" "${DD_APP_KEY:?set DD_APP_KEY}"
DASH=s7n-fsn-pqm

curl -sf "https://api.${DD_SITE}/api/v1/dashboard/${DASH}" \
  -H "DD-API-KEY: ${DD_API_KEY}" -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  > /tmp/dash-${DASH}.json

# Back up first; re-running appends a second copy of the group.
cp /tmp/dash-${DASH}.json /tmp/dash-${DASH}.backup.json

jq --slurpfile group docs/datadog/openevidence_dashboard_widgets.json '
  (.reflow_type == "fixed") as $fixed
  | {title, description, layout_type, reflow_type, widgets, template_variables,
     template_variable_presets, notify_list, tags}
  | with_entries(select(.value != null))
  | .widgets += [
      ($group[0] | if $fixed
                   then . + {layout: {x: 0, y: 1000, width: 12, height: 21}}
                   else del(.. | .layout?) end)
    ]' /tmp/dash-${DASH}.json > /tmp/dash-${DASH}.new.json

curl -sf -X PUT "https://api.${DD_SITE}/api/v1/dashboard/${DASH}" \
  -H "Content-Type: application/json" \
  -H "DD-API-KEY: ${DD_API_KEY}" -H "DD-APPLICATION-KEY: ${DD_APP_KEY}" \
  --data @/tmp/dash-${DASH}.new.json | jq '{id, title, url}'
```

On a dashboard with `reflow_type: fixed`, the group is placed at the bottom
with a full-width layout. Otherwise the script strips the per-widget layouts,
which only apply to fixed-reflow dashboards. Restore
`/tmp/dash-${DASH}.backup.json` with the same PUT if anything looks wrong.

### Import through the UI (alternative)

1. Open `https://app.datadoghq.com/dashboard/s7n-fsn-pqm` (use your org's
   site domain if it differs).
2. Either export the dashboard JSON (**Configure → Export dashboard JSON**),
   append the contents of `openevidence_dashboard_widgets.json` to its
   top-level `widgets` array, and load it back with **Configure → Import
   dashboard JSON** (this replaces the whole dashboard, so keep the export as
   a backup); or edit the dashboard's JSON in place, if your Datadog UI
   offers that, and add the group to `widgets` the same way.
3. Check the group renders, then save.

Neither the API script nor the UI route has been run against a live
Datadog dashboard yet (no keys were available when this was written).
Verify the result on the dashboard after importing.

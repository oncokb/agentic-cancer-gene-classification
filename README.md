# Agentic Cancer Gene Classification

M0 annotation engine for candidate cancer genes and gene fusions. The pipeline accepts either singleton gene symbols or fusions, resolves HGNC symbols, retrieves PubMed literature, and asks an LLM to produce structured cancer-gene annotations with verified PMID citations.

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Optional services:

- `ONCOKB_API_TOKEN`: enables OncoKB membership lookups.
- `NCBI_API_KEY`: raises NCBI E-utilities rate limits.

Citation precision knobs:

- `MAX_PAPERS_FOR_SYNTHESIS`: selected PubMed records passed to synthesis.
- `MAX_CITATIONS_PER_ANNOTATION`: verified PMIDs retained in final output.

Latency knobs:

- `ANNOTATION_GENE_CONCURRENCY`: number of genes annotated concurrently.
- `LLM_CONCURRENCY`: maximum concurrent LLM calls across retrieval, selection,
  and synthesis.
- `SYNTHESIS_MODEL_ESCALATION`: when true, use `SYNTHESIS_FAST_MODEL` first and
  call `SYNTHESIS_MODEL` only when the fast pass is missing core fields,
  under-cited, low-support, or sourced from Tier 2 retrieval.
- `PUBMED_STAGED_RETRIEVAL`: when true, Tier 1 PubMed retrieval stops after
  enough abstracts are found for synthesis instead of running every query family.
- `SELECTION_LLM_THRESHOLD`: only use the selection LLM when the retrieved
  corpus is at or below this size; larger corpora use deterministic ranking.

## API Keys

Copy `.env.example` to `.env` and add only the keys you need for the mode you are
running.

- `ANTHROPIC_SDK_PROVIDER`: optional SDK transport. Use `anthropic` for the
  direct Anthropic API or `bedrock` for AWS Bedrock. Defaults to `anthropic`.
- `ANTHROPIC_API_KEY`: required when `ANTHROPIC_SDK_PROVIDER=anthropic`,
  including benchmark judge scoring. Create a Claude Console account, then
  generate a key from API Keys:
  https://platform.claude.com/settings/keys.
- `BEDROCK_AWS_DEFAULT_REGION`, `BEDROCK_AWS_ACCESS_KEY_ID`,
  `BEDROCK_AWS_SECRET_ACCESS_KEY`, `BEDROCK_AWS_SESSION_TOKEN`: used when
  `ANTHROPIC_SDK_PROVIDER=bedrock`. These mirror the LibreChat Bedrock env
  names. If explicit keys are omitted, the AWS credential chain/profile is used.
- `AWS_PROFILE` or `BEDROCK_AWS_PROFILE`: optional AWS profile for Bedrock.
- `BEDROCK_SYNTHESIS_MODEL` and `BEDROCK_SELECTION_MODEL`: optional Bedrock
  model IDs for AGCG's synthesis and selection calls. Use these when the normal
  `SYNTHESIS_MODEL` / `SELECTION_MODEL` values are direct Anthropic model names.
- `BEDROCK_SYNTHESIS_FAST_MODEL`: optional Bedrock model ID for the lightweight
  synthesis pass when `SYNTHESIS_MODEL_ESCALATION=true`.
- `ONCOKB_API_TOKEN`: optional, but recommended for OncoKB membership lookups.
  OncoKB requires an account and data-access approval/license. After approval,
  the token is available in account settings:
  https://www.oncokb.org/account/settings. OncoKB API documentation:
  https://api.oncokb.org/oncokb-website/api.
- `NCBI_API_KEY`: optional. PubMed E-utilities work without it, but NCBI raises
  the limit from 3 requests/second to 10 requests/second when a key is supplied.
  Sign in to NCBI, open account settings, and create a key under API Key
  Management:
  https://support.nlm.nih.gov/kbArticle/?pn=KA-05317.

Do not commit `.env` or paste real keys into tracked files.

## Run With Anthropic SDK

For the direct Anthropic API, set `ANTHROPIC_API_KEY` in `.env`:

```bash
ANTHROPIC_SDK_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

For AWS Bedrock, use the same `BEDROCK_AWS_*` env family as the LibreChat
deployment:

```bash
ANTHROPIC_SDK_PROVIDER=bedrock
BEDROCK_AWS_DEFAULT_REGION=us-east-1
AWS_PROFILE=490004633549
BEDROCK_SYNTHESIS_MODEL=anthropic.claude-sonnet-4-5-20250929-v1:0
BEDROCK_SYNTHESIS_FAST_MODEL=anthropic.claude-haiku-4-5-20251001-v1:0
BEDROCK_SELECTION_MODEL=anthropic.claude-haiku-4-5-20251001-v1:0
```

Then run the CLI:

```bash
python -m src.cli \
  --fusions "ALK" \
  --output results.json \
  --output-csv results.csv
```

To prioritize the latency-sensitive fields (`cancer_associated`, rationale,
gene summary, citations, and evidence support), run in core mode:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" \
  --mode core \
  --output results.json
```

To test multiple genes or fusions in one run, pass each input after `--fusions`:

```bash
python -m src.cli \
  --fusions "ALK" "TP53::BRAF" "ETV6::NTRK3" "BCR::ABL1" \
  --output results.json \
  --output-csv results.csv
```

For larger batches, put one gene or fusion per line in a text file:

```text
ALK
TP53::BRAF
ETV6::NTRK3
BCR::ABL1
```

Then run:

```bash
python -m src.cli \
  --input inputs.txt \
  --output results.json \
  --output-csv results.csv
```

Or run the API. To annotate one gene and return the same JSON payload used by a
result card:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate/gene \
  -H "Content-Type: application/json" \
  -d '{"gene":"ALK","tumor_type":"LUAD"}'
```

The response is one `GeneAnnotation` object with fields such as `gene`,
`fusions`, `in_oncokb`, `cancer_associated`,
`cancer_association_rationale`, `cancer_type_prevalence`, `gene_class`,
`signaling_pathways`, `gene_summary`, `citations`, `supporting_quotes`,
`retrieval_count`, `retrieved_pmids`, `evidence_support_score`, and cache
metadata.

For batch runs or mixed genes and fusions, use `/v1/annotate`:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["ALK",{"fusion":"EML4::ALK","tumor_type":"LUAD"}],"mode":"core"}'
```

For progressive delivery, create an annotation job and poll its status. The
status response includes completed gene annotations as they finish:

```bash
curl -X POST http://127.0.0.1:8000/v1/annotate/jobs \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF","ETV6::NTRK3"],"mode":"core"}'

curl http://127.0.0.1:8000/v1/annotate/jobs/{job_id}
```

This path uses the Anthropic SDK for selection, synthesis, benchmark judging, and
Tier 2 agentic retrieval. In Bedrock mode, direct Anthropic model names must be
replaced with Bedrock model IDs via `BEDROCK_SYNTHESIS_MODEL`,
`BEDROCK_SYNTHESIS_FAST_MODEL`, and `BEDROCK_SELECTION_MODEL`, or by setting
`SYNTHESIS_MODEL`, `SYNTHESIS_FAST_MODEL`, and `SELECTION_MODEL` to Bedrock IDs
directly.

## Latency Comparison

The comparison runner executes the same input set twice:

- baseline-like settings: sequential genes, broad Tier 1 retrieval, selection
  LLM enabled
- optimized settings: configured concurrency, staged Tier 1 retrieval, and
  selection skipping for large corpora

```bash
python -m benchmarks.compare_latency \
  --fusions "TP53::BRAF" "ETV6::NTRK3" \
  --mode core \
  --output latency_report.json
```

The report includes run-level timings, per-gene retrieval/selection/synthesis
timings, and total milliseconds plus percent savings.

## Output Files

- `--output results.json`: full structured pipeline result, including run
  metadata and one `GeneAnnotation` object per annotated gene.
- `--output-csv results.csv`: curator-facing spreadsheet export with one row per
  gene. This file is intended for direct Google Sheets import.

The CSV keeps the core annotation fields from the JSON and adds
`publication_links`, a semicolon-separated list of PubMed URLs derived from the
verified PMID citations. Multi-value fields such as `fusions`, `citations`, and
`publication_links` are semicolon-separated so they stay in one spreadsheet cell.
Unknown optional values are left blank.

`evidence_support_score` is an explainable 0.0-1.0 grounding score calculated by
AGCG after PMID verification. It estimates how strongly the generated annotation
is supported by retrieved literature and verified citations; it is not a
calibrated probability of biological truth or clinical actionability.

To import into Google Sheets, create or open a sheet, use **File > Import**,
upload `results.csv`, and choose whether to insert it as a new sheet or append it
to an existing sheet. Treat this CSV as a review artifact; it does not write back
to the project source-of-truth sheet.

## Run Locally Without Anthropic API Credits

Use `--local [BACKEND]` to route LLM calls through a locally authenticated agent CLI.

Supported backends:

- `codex`
- `claude-code`
- `antigravity`

Bare `--local` defaults to `claude-code`.

Examples:

```bash
python -m src.cli \
  --fusions "ALK" \
  --local codex \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "ALK" \
  --local claude-code \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "ALK" \
  --local antigravity \
  --output results.json \
  --output-csv results.csv
```

For local mode, make sure the selected tool is installed and logged in on the host:

```bash
codex --version
claude --version
```

Antigravity defaults to:

```bash
antigravity -p {prompt}
```

Override that command shape if needed:

```bash
ANTIGRAVITY_LOCAL_COMMAND='your-command {prompt}' \
  python -m src.cli --fusions "ALK" --local antigravity
```

## Docker

Default API image:

```bash
docker compose up --build annotation-service
curl http://127.0.0.1:8000/health
```

Build a registry-ready image for IT:

```bash
docker build -t cbioportal/agentic-cancer-gene-classification:latest .
```

Use a release-specific tag when handing an image to IT, for example
`cbioportal/agentic-cancer-gene-classification:2026-07-30`.

The default Docker service uses the Anthropic SDK path. Provide
`ANTHROPIC_API_KEY` in a local `.env` for direct Anthropic Compose-based
annotation calls, or set `ANTHROPIC_SDK_PROVIDER=bedrock` plus AWS Bedrock env
vars for Bedrock calls. For Kubernetes or Argo CD deployments, keep `.env` out
of git and provide runtime configuration through Kubernetes Secrets, External
Secrets, or the cluster's approved secret-management path.

The Docker image includes Datadog Python tracing, starts the API with
`ddtrace-run`, and includes Datadog correlation fields in stdout logs. In Argo
CD/Kubernetes, follow the existing cBioPortal/OncoKB Datadog convention by
enabling the Datadog admission controller on the pod and setting service
tags/env vars at deploy time:

```yaml
metadata:
  labels:
    tags.datadoghq.com/env: <cluster-env>
    tags.datadoghq.com/service: agentic-cancer-gene-classification
    tags.datadoghq.com/version: <image-tag-or-git-sha>
    admission.datadoghq.com/enabled: "true"
```

```yaml
env:
  - name: DD_ENV
    value: <cluster-env>
  - name: DD_SERVICE
    value: agentic-cancer-gene-classification
  - name: DD_VERSION
    value: <image-tag-or-git-sha>
  - name: DD_LOGS_INJECTION
    value: "true"
  - name: DD_TRACE_ENABLED
    value: "true"
  - name: DD_APM_ENABLED
    value: "true"
```

Local Docker Compose sets `DD_TRACE_ENABLED=false` so development containers do
not attempt to send traces to a missing local Datadog Agent. Override that env
var if you are testing with a local Agent.

Dockerized local Codex:

```bash
docker compose --profile local up --build annotation-service-local
curl -X POST http://127.0.0.1:8001/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["ALK","TP53::BRAF"],"local_backend":"codex"}'
```

The local Docker profile builds the image with:

```bash
docker build --build-arg INSTALL_LOCAL_AGENTS=true .
```

It installs Linux-native `@openai/codex` and `@anthropic-ai/claude-code`, then mounts host local-agent config:

```text
${HOME}/.codex      -> /home/appuser/.codex
${HOME}/.claude     -> /home/appuser/.claude
${HOME}/.claude.json -> /home/appuser/.claude.json
```

Dockerized Codex has been validated end-to-end with host `~/.codex` mounted. Dockerized Claude Code may require logging in inside the container; mounting host Claude config did not reliably carry the login session into Linux during testing.

## MySQL Run Store And Gene Cache

When MySQL is configured, each successful annotation request is saved in the
`runs` table by `run_id`. Curators can share a run ID and peers can retrieve the
same result without recomputing it:

```bash
curl http://127.0.0.1:8000/v1/annotate/<run_id>
```

The same store also keeps the latest reusable annotation per canonical gene in
`gene_annotations`. This is separate from run sharing: `runs` preserves exact
request/response history, while `gene_annotations` reduces future LLM/PubMed
work for genes that are still fresh enough.

Default freshness policy:

- OncoKB-positive cached genes are reused for 90 days before a lightweight PubMed
  freshness check.
- High evidence support (`evidence_support_score >= 0.8`) is reused for 60 days.
- Medium evidence support (`>= 0.5`) is reused for 30 days.
- Low support or insufficient-evidence annotations are reused for 14 days.
- Stale cache entries trigger a cheap PubMed PMID-only search for papers newer
  than `last_pubmed_checked_at`. If no new PMIDs are found, the cache is reused
  and `last_pubmed_checked_at` is updated. If new PMIDs are found, the gene is
  rerun through retrieval and synthesis.
- Requests can set `force_refresh: true` to bypass gene cache reuse.

## Benchmark

Run the benchmark with API credits:

```bash
python -m benchmarks.run_benchmark \
  --route local \
  --mode core \
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

Run the benchmark locally and skip the SDK judge call:

```bash
python -m benchmarks.run_benchmark \
  --route local \
  --local codex \
  --no-judge \
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

`--no-judge` is required for a fully credit-free benchmark run because `benchmarks/judge.py` still uses the Anthropic SDK for LLM-as-judge summary scoring.

Benchmark outputs:

- `--output benchmark_report.json`: metrics, per-gene deltas, optional judge
  results, and the raw `pipeline_result`.
- `--results-csv benchmark_results.csv`: one-row-per-gene CSV generated from the
  same `pipeline_result` used for metric scoring.

## Tests

```bash
pytest
ruff check src benchmarks tests
docker compose --profile local config --quiet
```

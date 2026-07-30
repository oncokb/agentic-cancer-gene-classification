# Agentic Cancer Gene Classification

M0 annotation engine for candidate cancer gene fusions. The pipeline splits fusions into genes, resolves HGNC symbols, retrieves PubMed literature, and asks an LLM to produce structured cancer-gene annotations with verified PMID citations.

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

## API Keys

Copy `.env.example` to `.env` and add only the keys you need for the mode you are
running.

- `ANTHROPIC_API_KEY`: required for Anthropic SDK mode and for benchmark judge
  scoring. Create a Claude Console account, then generate a key from API Keys:
  https://platform.claude.com/settings/keys.
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

## Run With Anthropic API Key

Set `ANTHROPIC_API_KEY` in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Then run the CLI:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" \
  --output results.json \
  --output-csv results.csv
```

To test multiple fusions in one run, pass each fusion after `--fusions`:

```bash
python -m src.cli \
  --fusions "TP53::BRAF" "ETV6::NTRK3" "BCR::ABL1" \
  --output results.json \
  --output-csv results.csv
```

For larger batches, put one fusion per line in a text file:

```text
TP53::BRAF
ETV6::NTRK3
BCR::ABL1
```

Then run:

```bash
python -m src.cli \
  --input fusions.txt \
  --output results.json \
  --output-csv results.csv
```

Or run the API:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF"]}'
```

This path uses the Anthropic SDK for selection, synthesis, and Tier 2 agentic retrieval.

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
  --fusions "TP53::BRAF" \
  --local codex \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "TP53::BRAF" \
  --local claude-code \
  --output results.json \
  --output-csv results.csv

python -m src.cli \
  --fusions "TP53::BRAF" \
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
  python -m src.cli --fusions "TP53::BRAF" --local antigravity
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
`ANTHROPIC_API_KEY` in a local `.env` for Compose-based annotation calls. For
Kubernetes or Argo CD deployments, keep `.env` out of git and provide runtime
configuration through Kubernetes Secrets, External Secrets, or the cluster's
approved secret-management path.

Dockerized local Codex:

```bash
docker compose --profile local up --build annotation-service-local
curl -X POST http://127.0.0.1:8001/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF"],"local_backend":"codex"}'
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
  --output benchmark_report.json \
  --results-csv benchmark_results.csv
```

Run the benchmark locally and skip the SDK judge call:

```bash
python -m benchmarks.run_benchmark \
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

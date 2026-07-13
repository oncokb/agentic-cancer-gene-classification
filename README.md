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

The default Docker service uses the Anthropic SDK path. Provide `ANTHROPIC_API_KEY` in `.env` for annotation calls.

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

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

## Run With Anthropic API Key

Set `ANTHROPIC_API_KEY` in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
```

Then run the CLI:

```bash
python -m src.cli --fusions "TP53::BRAF" --output results.json
```

Or run the API:

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000
curl -X POST http://127.0.0.1:8000/v1/annotate \
  -H "Content-Type: application/json" \
  -d '{"fusions":["TP53::BRAF"]}'
```

This path uses the Anthropic SDK for selection, synthesis, and Tier 2 agentic retrieval.

## Run Locally Without Anthropic API Credits

Use `--local [BACKEND]` to route LLM calls through a locally authenticated agent CLI.

Supported backends:

- `codex`
- `claude-code`
- `antigravity`

Bare `--local` defaults to `claude-code`.

Examples:

```bash
python -m src.cli --fusions "TP53::BRAF" --local codex --output results.json
python -m src.cli --fusions "TP53::BRAF" --local claude-code --output results.json
python -m src.cli --fusions "TP53::BRAF" --local antigravity --output results.json
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
python -m benchmarks.run_benchmark --output benchmark_report.json
```

Run the benchmark locally and skip the SDK judge call:

```bash
python -m benchmarks.run_benchmark --local codex --no-judge --output benchmark_report.json
```

`--no-judge` is required for a fully credit-free benchmark run because `benchmarks/judge.py` still uses the Anthropic SDK for LLM-as-judge summary scoring.

## Tests

```bash
pytest
ruff check src benchmarks tests
docker compose --profile local config --quiet
```

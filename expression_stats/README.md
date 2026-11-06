# TCGA expression statistics

Isolated port of `gene-expression-relevance` at `a7c7cc6`, method v2. The
statistical implementation, exact dependency constraints, HGNC checksum pin,
and date-free release identity semantics are preserved. Relocation, removal
of the search page, and transport code change the code fingerprint normally.

The single authoritative producer/consumer contract is [docs/contract.md](docs/contract.md).
Weekly/manual builds publish verified GitHub release bundles for the reader
being developed in #167. The application never imports this package.

From this directory, with Python 3.12:

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -c constraints.txt setuptools
pip install --no-build-isolation -c constraints.txt -e '.[dev]'
ruff check .
pytest
python -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --out artifacts \
  --genes ERBB2,ALB,KLK3,ESR1,TP53
python scripts/bundle.py artifacts bundle
```

Omit `--genes` for a full build; blank, whitespace and comma-only lists also
mean full build. Local builds support unkeyed PubMed access below 3 requests/s;
set `NCBI_API_KEY` for keyed access. Workflow prechecks require this secret.
`--skip-precheck` and subsets are review-only. No page or web server is needed.
Downloads and PubMed counts persist in `.cache/`; failed builds stage output
without replacing completed artifacts. Generated files are ignored.

Pipeline CI runs its own stricter ruff rules and tests. Root ruff explicitly
excludes this directory; ACGC pytest's existing `testpaths = ["tests"]` excludes
these tests. The isolation test statically checks imports in both directions.
The app dependencies are unchanged, and `.dockerignore` excludes this directory.

Only the release workflow calls `scripts/publish.py`; running the build and
bundle commands above never creates a release. Publish requires `GH_TOKEN`
with contents write permission and uses GitHub CLI on the runner. A failed
publication stays draft; investigate/delete that draft before retrying.

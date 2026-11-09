# GitHub ref isolation in checker tests

The failing hosted run `42348598480` at `27889f8` reported 2 failures and
165 passes. Both failures were inherited-environment bugs in the tests:
`test_workflow_deployment_policy` and
`test_live_mapping_changes_invalidate_early_exit` exercise main-branch policy
without specifying `GITHUB_REF`.

Commit `948d994` correctly restricted deployment and unchanged-build early exits
to main. GitHub pull-request jobs set `GITHUB_REF=refs/pull/168/merge`; branch
push jobs set the feature-branch ref. Both must produce `changed=true` and
`deployable=false`, regardless of matching published inputs. Local shells had
no `GITHUB_REF`, so the checker's default was main and masked the fixture bug.
The prior local green results therefore did not establish hosted CI success.

The fix sets main explicitly in those two fixtures, preserving every expected
value. A separate parametrized test covers both review-branch and PR refs and
fails if the unchanged-build check is entered. Production checker/workflow
logic is unchanged. Real scheduled runs use `refs/heads/main`, retain their
matching-input/mapping/freshness checks, and are unaffected by this fix.

Only tests and this document change: no file included in `code_fingerprint()`
changes. Consequently this fix does not change `code_sha256` or `release_id` for
the same inputs and resolved dependency versions. Statistics, thresholds and
artifact contracts are unchanged.

## Reproduction and validation

Reproduction uses tracked files from `27889f8` in an isolated Linux Python 3.12
container and a new venv, with no mounted home, tokens, existing build artifacts,
or local caches. Installation matches the workflow:

```sh
python -m venv /tmp/ci-venv
/tmp/ci-venv/bin/pip install -c constraints.txt setuptools
/tmp/ci-venv/bin/pip install --no-build-isolation -c constraints.txt -e '.[dev]'
/tmp/ci-venv/bin/ruff check .
GITHUB_ACTIONS=true GITHUB_REF=refs/pull/168/merge /tmp/ci-venv/bin/pytest
```

The local container is Linux arm64 Python 3.12.14; the original hosted run is
Linux x86_64 Python 3.12.16. Pinned Python dependencies match. Final hosted
verification uses the actual GitHub workflow, rather than claiming the local
container has the hosted OS/architecture/patch version.

The unmodified full suite reproduces **2 failed, 165 passed** with the PR ref.
Changing only the ambient ref to absent in that same installed baseline makes
both affected test groups pass. The corrected suite is then run with the PR ref
still present; it must pass without changing the hosted CI environment.

Root gates use the existing app dev environment (`UV_NO_SYNC=1` prevents lock
regeneration), with numerical dependencies verified absent and disposable
MySQL/Redis on dedicated ports:

```sh
UV_NO_SYNC=1 uv run ruff check
UV_NO_SYNC=1 uv run python -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('numpy','scipy','pandas'))"
env PATH="$PWD/.venv/bin:/usr/bin:/bin" UV_NO_SYNC=1 \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=62126 MYSQL_USER=acgc MYSQL_PASSWORD=acgc \
  MYSQL_DATABASE=acgc_test REDIS_URL=redis://127.0.0.1:62127/0 \
  ACGC_ALLOW_DESTRUCTIVE_TESTS=1 /opt/homebrew/bin/uv run pytest -q
```

Results:

- Original `tests/test_provenance.py`, ref removed: **17 cases passed** (2.10s).
- Corrected `ruff check .`: pass; full `pytest` with inherited PR ref:
  **168 collected cases passed** (77.51s).
- Root ruff: pass; root pytest: **2,895 passed, 74 skipped**
  (2,969 collected cases; 183.20s), with numpy/scipy/pandas absent.
- Root source, dependencies and tests are untouched. Pre-existing untracked
  root `uv.lock` is left untouched. No GitHub releases are created.

The final GitHub run IDs/conclusions are recorded in the PR description after
push, so hosted success is verified independently of local gates.

# Runtime and control-integrity follow-up

The m3 statistical implementation and per-gene JSON schema are byte-for-byte
unchanged from `63dae68`. Prior 20,488-gene scientific/size measurements remain
in [method3-validation.md](method3-validation.md); a full real-data rerun was not
needed for these operational changes. No GitHub release was created.

## Behavior and tests

- The rate is absent from hashed `PARAMS`. Operational defaults are in
  `runtime-settings.json`, outside the package-source fingerprint; the effective
  rate is recorded in unhashed `source.precheck_requests_per_second`. Tests run
  the actual fixture builder with different environment and settings-file rates
  and verify equal code fingerprints/release IDs, and different rate metadata.
  This source revision itself changes the code fingerprint normally; future
  rate-only configuration changes do not invalidate literature verdicts.
- Control IDs and (gene, tcga) pairs must be unique, even for subsets.
  Publication uses panel-derived positive and negative counts, requires every
  positive to pass, zero negative passes and missing=0. Tests use a differently
  sized panel and duplicate IDs/pairs rather than depending only on 35/27.
- Per-pair SHA-256 deterministically shortens the 30-day maximum TTL to 15–30
  days. Existing cached fetch timestamps are reused. Tests verify deterministic
  expiries, spread across multiple weeks, maximum age, expiry refresh and
  manifest freshness using the actual jittered deadlines.
- Prechecks default to a 300-minute phase budget and are capped at 315 minutes
  from builder start, leaving at least 15 minutes before the 330-minute step
  timeout. Socket timeouts, rate-limit waits and Retry-After/retry sleeps use the
  remaining budget. Expiry cancels pending work, stops workers, flushes committed
  SQLite counts and raises an incomplete-build error. Previous complete output
  remains; `<out>-incomplete/` retains manifest/control/precheck reports. The
  workflow's always-run cache save still executes, enabling a subsequent run to
  resume without repeating successful queries. No incomplete bundle can publish.
  Tests bound a one-day Retry-After to five seconds using a fake clock, exercise
  post-request budget exhaustion, verify persisted counts and successfully
  resume the builder without repeating the request.
- Skipped prechecks use a unique per-run concurrency group and never restore or
  save PubMed caches, so they cannot evict pending cron jobs or shadow more recent
  resumable counts. Event/ref workflow queues and the publisher lock remain.
- Pipeline import-name checks exempt `sys.stdlib_module_names`; regression
  coverage permits `import json` alongside a future `src/json.py`, while still
  rejecting application imports.

## Exact gates

From `expression_stats/`, in its independent constrained Python 3.12 environment:

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Both pass: **167 collected test cases passed**, 6.10 seconds.

From the repository root, preserving the existing app dev environment with
`UV_NO_SYNC=1` and verifying no numerical dependencies:

```sh
UV_NO_SYNC=1 uv run ruff check
UV_NO_SYNC=1 uv run python -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('numpy','scipy','pandas'))"
env PATH="$PWD/.venv/bin:/usr/bin:/bin" UV_NO_SYNC=1 \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=62126 MYSQL_USER=acgc MYSQL_PASSWORD=acgc \
  MYSQL_DATABASE=acgc_test REDIS_URL=redis://127.0.0.1:62127/0 \
  ACGC_ALLOW_DESTRUCTIVE_TESTS=1 /opt/homebrew/bin/uv run pytest -q
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 \
  .github/workflows/expression-stats-ci.yml \
  .github/workflows/expression-stats-release.yml
```

Root ruff and actionlint pass. Root pytest: **2,895 passed, 74 skipped**
(2,969 collected app cases), 907 warnings, 48.44 seconds, using disposable
MySQL/Redis and a CI-like PATH without local LLM CLIs. Containers were stopped
after validation. The pre-existing untracked root `uv.lock` is not part of this
change. App source/dependencies and existing app tests are untouched.

Known risk remains the first live GitHub publication; the PR body retains its
verification checklist. This follow-up used mocked HTTP/GitHub and fake clocks
for deadline tests, not a multi-hour cold-cache PubMed run.

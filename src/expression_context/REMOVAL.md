# Removing the expression-context module

The feature is self-contained by design (see `docs/expression-context.md`,
"Removability requirements"). This removes the ACGC *consumer* only; the
producer (`expression_stats/`, its workflows and its release bundles) has its
own removal notes in that directory.

1. **Delete the package**, which includes the "Expression stats" page and its
   static files (`src/expression_context/static/`):
   `git rm -r src/expression_context/`
2. **Delete the registration** in `src/main.py` — exactly two lines:
   - `from src.expression_context import register_expression_context`
   - `register_expression_context(app)  # optional module; ...`
3. **Delete the tests and their fixtures**:
   ```sh
   git rm tests/test_expression_context_*.py tests/expression_context_helpers.py
   git rm tests/frontend/test_expression_context_page.js
   git rm -r tests/fixtures/expression_context/
   ```
   (`test_expression_context_api.py`, `..._boundary.py`, `..._bundle.py`,
   `..._lease.py`, `..._nullable.py`, `..._releases.py`, `..._store_gate.py`,
   `..._web.py`.)
4. **Delete the design doc** (optional): `git rm docs/expression-context.md`
5. **Drop the tables** (they only exist where the flag was ever enabled):
   ```sql
   DROP TABLE IF EXISTS expression_context_results;
   DROP TABLE IF EXISTS expression_context_stats;
   DROP TABLE IF EXISTS expression_context_aliases;
   DROP TABLE IF EXISTS expression_context_bundles;
   DROP TABLE IF EXISTS expression_context_state;
   ```
6. **Remove the env** from the ArgoCD-managed k8s manifests / Helm values /
   `.env`: every `EXPRESSION_CONTEXT_*` variable — at least
   `EXPRESSION_CONTEXT_ENABLED`, and any of `EXPRESSION_CONTEXT_RELEASES_REPO`,
   `EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG`, `EXPRESSION_CONTEXT_GITHUB_TOKEN`
   (and its secret), `EXPRESSION_CONTEXT_REFRESH_INTERVAL`,
   `EXPRESSION_CONTEXT_REFRESH_ERROR_RETRY`, `EXPRESSION_CONTEXT_REDIS_URL`
   that were set.
7. Optional Redis cleanup (entries expire on their own TTLs):
   `expression_context:lease:*` keys and `job:expression_context:*` job
   snapshots.

**Verify** after steps 1–4: `git grep -n -i "expression.context"` prints
nothing outside `expression_stats/` (the producer), and
`git diff --stat <base>` shows only deletions of the files listed above plus
the two `src/main.py` lines.

Nothing else references the module: `src/config.py` has no
`expression_context` fields, ACGC's own UI (`src/static/`) has no link to the
page (the "Expression stats" tab is injected by the module at runtime), no
existing table was altered, and no dependency was added to `pyproject.toml`.
The guard tests in `tests/test_expression_context_boundary.py` enforce this
while the module exists.

To merely **disable** it, set `EXPRESSION_CONTEXT_ENABLED=false` (the default)
and restart: the API routes, the page and its nav tab disappear, and no
outbound calls (GitHub included) are made; the tables, if created, are left
untouched. To roll back to an earlier bundle without disabling, set
`EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG` to that release tag.

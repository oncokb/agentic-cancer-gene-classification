# Removing the expression-context module

The feature is self-contained by design (see `docs/expression-context.md`,
"Removability requirements"). To remove it completely:

1. **Delete the package**: `git rm -r src/expression_context/`
2. **Delete the registration** in `src/main.py` — exactly two lines:
   - `from src.expression_context import register_expression_context`
   - `register_expression_context(app)  # optional module; ...`
3. **Delete the tests and their fixtures**:
   ```sh
   git rm tests/test_expression_context_*.py tests/expression_context_helpers.py
   git rm -r tests/fixtures/expression_context/
   ```
   (`test_expression_context_api.py`, `..._artifact.py`, `..._boundary.py`,
   `..._store_gate.py`, `..._lease.py`, `..._nullable.py`.)
4. **Delete the design doc** (optional): `git rm docs/expression-context.md`
5. **Drop the table** (only exists where the flag was ever enabled):
   ```sql
   DROP TABLE IF EXISTS expression_context_results;
   ```
6. **Remove the env** from the k8s manifest / Helm values / `.env`: every
   `EXPRESSION_CONTEXT_*` variable (at least `EXPRESSION_CONTEXT_ENABLED` and
   `EXPRESSION_CONTEXT_ARTIFACT_BASE_URL`).
7. Optional Redis cleanup (entries expire on their own TTLs):
   `expression_context:*` cache, negative-cache and lease keys, and
   `job:expression_context:*` job snapshots.

**Verify** after steps 1–4: `git grep -n -i "expression.context"` prints
nothing, and `git diff --stat <base>` shows only deletions of the files
listed above plus the two `src/main.py` lines.

Nothing else references the module: `src/config.py` has no
`expression_context` fields, no existing table was altered, and no
dependency was added to `pyproject.toml`. The guard test
`tests/test_expression_context_boundary.py` enforces this while the module
exists.

To merely **disable** it, set `EXPRESSION_CONTEXT_ENABLED=false` (the
default) and restart: routes disappear and no outbound calls are made; the
table, if created, is left untouched.

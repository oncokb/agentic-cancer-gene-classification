# Remove the isolated statistics producer

1. Delete `expression_stats/` (including its tests, docs and local generated data).
2. Delete `.github/workflows/expression-stats-ci.yml` and
   `.github/workflows/expression-stats-release.yml`.
3. Remove the `&& !startsWith(github.event.release.tag_name, 'expression-stats-')`
   guard from `.github/workflows/docker-hub-release.yml` once no future bundle
   releases can be published. Retain it if publishing bundles by other means.
4. Remove the `NCBI_API_KEY` repository secret if no other workflow uses it.
5. Remove the `expression_stats` exclusion from root ruff configuration and
   `expression_stats/` from `.dockerignore` (both are harmless if retained).

Existing release bundles may be retained as historical data. Consumer removal
is a separate task and this producer has no application registration or imports.

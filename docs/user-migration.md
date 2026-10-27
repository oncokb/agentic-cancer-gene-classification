# Runbook: copy users from Redis to MySQL

`python -m src.scripts.migrate_users_to_mysql` copies the user profiles that
sign-in keeps in Redis into the MySQL `users` table. Run it in production
**before** deploying the change that switches sign-in to MySQL.

What it does:

- Reads Redis only (`SMEMBERS`, `SCAN`, `GET`, `HGETALL`). It deletes nothing;
  Redis stays the fallback for one release.
- Reads the same Redis as sign-in (`REDIS_URL`, or `--redis-url`) and the same
  MySQL as the app (`MYSQL_*` / `DB_URL`, `DB_USERNAME`, `DB_PASSWORD`). It runs
  no DDL: the app must already have created the user tables (#147). Before
  writing anything it checks that `users` and `role_changes` are readable.
- Writes only through the user store (`import_user`, `grant_bootstrap_admin`). It creates
  users missing from MySQL with their role, status, provider, groups, claims,
  first/last login, login count and usage (profile count plus the separate
  `agcg:user_activity:*` record). It never changes a user that is already in
  MySQL; it reports which fields differ.
- `--bootstrap-admin EMAIL` (repeatable) grants the admin role once, logged in
  `role_changes` with the fixed actor `system:migration`. The store checks for
  an earlier grant and changes the role in one transaction. If someone later
  changes that role, no re-run (concurrent ones included) grants it again.
- Prints counts. Emails are printed only with `--show-emails`; otherwise users
  are shown as `user #N`. It never prints tokens, claims or connection URLs
  (not even a malformed one). Provider values the app doesn't set are counted
  as `other`.
- Dry run is the default. `--apply` writes, then checks the result.

Exit codes: `0` OK. `1` the post-apply check failed: a Redis profile was
skipped (unparseable or a duplicate after email normalization), a Redis user
is missing from MySQL, a row created by this run differs from Redis
(`created_at` included), or a bootstrap admin lacks the admin role. `2` MySQL
or Redis can't be reached, the Redis URL is malformed, or a required table is
missing (nothing written). The check is per email. MySQL users that aren't in
Redis are counted but don't fail it.

## Steps

1. **Open a shell where the app's environment is set.** The simplest way is
   the running app pod, which already has `REDIS_URL` and the MySQL settings:

   ```sh
   kubectl -n <namespace> get pods            # pick a pod of the app deployment
   kubectl -n <namespace> exec -it <app-pod> -- sh
   ```

   From a laptop instead, port-forward both services and set the variables in
   that shell. Read the values from the deployment's secret; don't paste them
   into tickets or chat:

   ```sh
   kubectl -n <namespace> port-forward svc/<redis-service> 6379:6379 &
   kubectl -n <namespace> port-forward svc/<mysql-service> 3306:3306 &   # or the RDS host
   export REDIS_URL=redis://127.0.0.1:6379/0 MYSQL_HOST=127.0.0.1 MYSQL_PORT=3306 \
          MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DATABASE=...
   ```

2. **Dry run.** It writes nothing:

   ```sh
   python -m src.scripts.migrate_users_to_mysql --bootstrap-admin kuangf@mskcc.org
   ```

   As of the last check, production should show: `Redis profiles found: 16`,
   `unparseable (skipped): 0`, `by role: curator=16`, `by status: active=16`,
   `by provider: google=8, keycloak=8`, `admins in Redis: 0`,
   `Redis usage records: 1`, `Users that would be created: 16`, and
   `would grant admin` for the bootstrap admin. If the counts differ, find out
   why before applying. To see which users are affected, add `--show-emails`.

3. **Apply:**

   ```sh
   python -m src.scripts.migrate_users_to_mysql --apply --bootstrap-admin kuangf@mskcc.org
   ```

   Expect `Created: 16`, `Bootstrap admin grants logged: 1`,
   `Redis profiles: 16; MySQL users: 16`, `Redis profiles skipped (not copied): 0`,
   `Redis users missing from MySQL: 0`, `MySQL admins: 1`, `Result: OK`, and
   exit code 0 (`echo $?`).

4. **Verify again.** Run the dry run from step 2. It should report
   `Users that would be created: 0`, `Already in MySQL and unchanged: 16` and
   `already admin`. Users who annotated after the apply show as differing in
   `annotation_count`/`last_annotation_at` while sign-in still uses Redis. That
   is expected and is not overwritten.

The script can be re-run any number of times. Run it once more just before
the switch-over deploy to copy users who signed up in the meantime. It does
not copy logins or annotations made after the first apply by users who are
already in MySQL. A re-run lists those users as differing.

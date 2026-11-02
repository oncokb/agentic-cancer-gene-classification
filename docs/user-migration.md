# Runbook: move users from Redis to MySQL

Two parts: copy the users (`python -m src.scripts.migrate_users_to_mysql`),
then switch sign-in and permission checks over with `USER_STORE_BACKEND=mysql`
([Cutover](#cutover-switch-sign-in-to-mysql) below). The switch ships off
(`USER_STORE_BACKEND=redis` is the default), so deploying it changes nothing
until the setting is flipped.

## Copy users

`python -m src.scripts.migrate_users_to_mysql` copies the user profiles that
sign-in keeps in Redis into the MySQL `users` table. Run it in production
**before** setting `USER_STORE_BACKEND=mysql`.

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

### Steps

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

## Cutover: switch sign-in to MySQL

`USER_STORE_BACKEND` decides where sign-in (Google, Keycloak, SAML, dev
login), the per-request permission check, the API-key owner check, the admin
user list (`GET /auth/users`) and usage counts read and write user records.

| | `redis` (default) | `mysql` |
|---|---|---|
| User records | `agcg:user:<email>` in Redis | `users` table |
| New user | `JIT_DEFAULT_ROLE`, active | **viewer**, active |
| `JIT_REQUIRE_ADMIN_APPROVAL=true` | new user pending | new user pending |
| Returning user's role/status | kept, except IdP admin groups (Keycloak `KEYCLOAK_ADMIN_ROLES`, `SAML_ADMIN_GROUPS`) make them admin | **never changed by sign-in**; IdP groups are stored but don't set a role |
| `BOOTSTRAP_ADMIN_EMAILS` | ignored | made admin once (see below) |

Unchanged with either backend: every request re-reads the user's current
role and status with exactly one lookup and no cache; a user missing from
the store must sign in again (401); an unreachable store is a 503 (sign-in
sets no cookie, an existing session is kept); an API key whose owner is
missing is rejected (401). With `mysql` there is no fallback to Redis or to
the session's claims, and Redis user data is neither read nor modified.

Usage counters (`annotation_count`, `last_annotation_at`) are not an access
check and are recorded after the work. If that write fails, the annotation
response (or job) still succeeds, the count is dropped (never buffered) and
an error is logged under `user_store.activity_write_failed` (no email in
the message). Only the per-request user lookup fails closed with 503.

`BOOTSTRAP_ADMIN_EMAILS` (comma-separated) is applied at startup to listed
users already in MySQL, and at sign-in to the rest. With `mysql`, the app
**refuses to start** when the user store can't be read or granting a listed
existing user fails, rather than serving without the configured admin. Each listed user is made
admin once, logged in `role_changes` with the actor `system:migration`, the
same actor as the script's `--bootstrap-admin`. Any earlier grant by either
one counts: if an admin later removes the role, neither a restart nor a
sign-in grants it again.

### Steps

1. **Dry run** the copy (step 2 above) and check the counts.
2. **Apply** it (step 3 above): expect `Result: OK` and exit code 0.
3. **Verify the counts**: re-run the dry run (step 4 above). Expect
   `Users that would be created: 0` and `Redis users missing from MySQL: 0`
   (from the apply).
4. **Set** in the deployment's environment (ConfigMap / secret):

   ```sh
   USER_STORE_BACKEND=mysql
   BOOTSTRAP_ADMIN_EMAILS=kuangf@mskcc.org
   ```

   Re-run the copy (`--apply`) right before this step if anyone signed up
   since the last apply; while sign-in still uses Redis, new sign-ups land
   only there.
5. **Restart** the app (`kubectl -n <namespace> rollout restart
   deployment/<app>`). Startup logs `MySQL user store ready: N active
   admin(s)` and `Bootstrap admin for kuangf@mskcc.org: already admin` (or
   `granted`). If pods crash-loop with `UserStoreStartupError`, MySQL isn't
   readable or the grant failed: fix it, or roll back (below).
6. **Check**: sign in as the bootstrap admin, open `GET /auth/users` (all
   migrated users listed with roles), and sign in as a non-admin to confirm
   their role is unchanged.

After the cutover, admins approve, disable and re-enable users and change
roles on the `/admin/users` page; see [admin.md](admin.md).

### Rollback

Set `USER_STORE_BACKEND=redis` (`BOOTSTRAP_ADMIN_EMAILS` is then ignored) and
restart. Sign-in goes back to the Redis records, which the MySQL backend
never touched. **Users created and role/status changes made while on `mysql`
are not in Redis**: those users get a fresh Redis record (`JIT_DEFAULT_ROLE`)
at their next sign-in, and role changes have to be redone. Their MySQL rows
stay, so switching back to `mysql` later picks them up again.

# Managing users (admins)

Admins manage who may use ACGC on the **Users** page, `/admin/users`, linked
from the user menu (shown to admins only). Signed-out visitors are sent
through sign-in and back; signed-in non-admins get a 403 page. The page
uses the [admin API](api.md#admin-user-management-v1admin), which scripts
can call with an admin's API key.

## What you can do

- **Find users**: search by email or name, filter by role and status. The
  list is newest sign-in first; **Load more** fetches the next page.
- **Approve** a pending account (pending → active), **Disable** an active
  one, **Re-enable** a disabled one.
- **Change a role** with the role menu: viewer, curator or admin.
- **History** shows one user's changes; **Show all changes** returns to the
  full change log.

Every change opens a confirmation with an optional reason, which is stored
in the change log. Demotions and disabling are marked as such. A change
applies on the user's next request: a disabled user loses access (sessions are refused) and their
API keys stop working; a demoted admin loses admin pages and endpoints.

## Safety rules

- There is always at least one **active** admin: the last one can't be
  demoted or disabled (a disabled admin doesn't count). The check runs in the
  same database transaction as the change, so two admins removing each other
  at the same moment can't both succeed.
- You can't disable your own account. You can demote yourself while another
  active admin remains.
- Requests that change nothing write nothing.
- Every change is recorded in `role_changes` with your email as the actor;
  the log is insert-only.

## Requirements

Changes need the MySQL user store (`USER_STORE_BACKEND=mysql`, see
[user-migration.md](user-migration.md)). With `redis` the page shows a
read-only notice, the role and status controls are disabled, and the change
log is unavailable. If the user store can't be reached, the page shows an
error and nothing is changed.

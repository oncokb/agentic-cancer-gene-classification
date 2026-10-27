"""One-off copy of user records from Redis into the MySQL user store.

Usage:
  # Report what would happen (the default; writes nothing)
  python -m src.scripts.migrate_users_to_mysql --bootstrap-admin someone@mskcc.org

  # Create the missing users, grant bootstrap admins, then verify
  python -m src.scripts.migrate_users_to_mysql --apply --bootstrap-admin someone@mskcc.org

Redis layout read (see src/auth.py): the `agcg:users` set of emails, one
`agcg:user:<email>` JSON profile per user, and `agcg:user_activity:<email>`
hashes with usage counted since usage moved out of the profile. Redis is only
read (SMEMBERS, SCAN, GET, HGETALL); it stays the fallback for one release.

MySQL is only written through the UserStore API (import_user,
grant_bootstrap_admin), so emails, search columns and JSON are normalized
exactly as on login. Before writing, every table the run uses (users,
role_changes) must be readable. An email already in MySQL is never
overwritten: differences are reported. A bootstrap admin is granted the admin
role once, logged in role_changes under the fixed actor `system:migration`;
the store checks for an earlier grant and changes the role in one
transaction, so a later demotion is never undone, even by a concurrent run.

Output never includes tokens, claims contents, connection URLs or unknown
provider values, and emails only with --show-emails (otherwise users are
numbered in email order).

Exit status: 0 on success; 1 when the --apply verification fails (a Redis
profile was skipped, a Redis user is missing from MySQL, a created row
differs from Redis, or a bootstrap admin is not admin); 2 when MySQL or Redis
can't be used or a required table is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from pydantic import ValidationError

from src.auth import UserProfile, UserStoreUnavailable, _activity_key, _with_activity
from src.pipeline.user_store import (
    BOOTSTRAP_ACTOR,
    BOOTSTRAP_ALREADY_ADMIN,
    BOOTSTRAP_GRANTED,
    BOOTSTRAP_PREVIOUSLY_GRANTED,
    UserFilters,
    UserNotFound,
    UserRecord,
    UserStore,
    normalize_email,
    prepare_import,
    utc_now,
)

USERS_SET_KEY = "agcg:users"
PROFILE_PREFIX = "agcg:user:"
ACTIVITY_PREFIX = _activity_key("")
# Providers src/auth.py and src/main.py set; any other value prints as "other".
KNOWN_PROVIDERS = ("api_key", "dev", "google", "header", "keycloak", "local", "saml")
BOOTSTRAP_REASON = "bootstrap admin grant (migrate_users_to_mysql)"

# Fields compared between Redis and MySQL. updated_at is set by the import and
# role_updated_* only by role changes. created_at is compared only for rows
# this run created (a pre-existing row was created by its first login).
COMPARED_FIELDS = (
    "name", "picture", "domain", "provider", "role", "status", "groups", "claims",
    "first_login_at", "last_login_at", "login_count", "annotation_count", "last_annotation_at",
)


class ReadOnlyRedis:
    """Exposes only the read commands this script needs, so nothing here can
    write to or delete from Redis."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def smembers(self, key: str):
        return await self._client.smembers(key)

    async def get(self, key: str):
        return await self._client.get(key)

    async def hgetall(self, key: str):
        return await self._client.hgetall(key)

    async def scan_keys(self, match: str) -> List[str]:
        return [key async for key in self._client.scan_iter(match=match, count=500)]


@dataclass
class Unparseable:
    key_email: str
    reason: str


@dataclass
class RedisSnapshot:
    users: List[UserRecord]
    unparseable: List[Unparseable]
    usage_records: int
    orphan_usage_records: int

    @property
    def profiles_found(self) -> int:
        return len(self.users) + len(self.unparseable)


@dataclass
class Plan:
    to_create: List[UserRecord] = field(default_factory=list)
    unchanged: List[str] = field(default_factory=list)
    differs: Dict[str, List[str]] = field(default_factory=dict)
    # email -> "grant", "already admin", "missing", or "previously granted; ..."
    bootstrap: Dict[str, str] = field(default_factory=dict)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _key_identity(key_email: str) -> Optional[str]:
    try:
        return normalize_email(key_email)
    except ValueError:
        return None


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # Python 3.9's fromisoformat doesn't take a trailing "Z".
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(text)


def record_from_profile(profile: UserProfile, now: datetime) -> UserRecord:
    """The users row for a Redis profile (usage already overlaid). The first
    login is the creation time; Redis has no separate one."""
    first_login_at = _parse_time(profile.first_login_at)
    last_login_at = _parse_time(profile.last_login_at)
    if first_login_at is None or last_login_at is None:
        raise ValueError("login timestamps are required")
    return prepare_import(
        UserRecord(
            email=profile.email,
            name=profile.name,
            picture=profile.picture,
            domain=profile.domain,
            provider=profile.provider,
            status=profile.status,
            role=profile.role,
            role_updated_by=None,
            role_updated_at=None,
            groups=list(profile.groups),
            claims=dict(profile.claims),
            first_login_at=first_login_at,
            last_login_at=last_login_at,
            login_count=profile.login_count,
            annotation_count=profile.annotation_count,
            last_annotation_at=_parse_time(profile.last_annotation_at),
            created_at=first_login_at,
            updated_at=now,
        )
    )


async def read_redis(redis: ReadOnlyRedis, now: Optional[datetime] = None) -> RedisSnapshot:
    """Every profile listed in agcg:users or stored under agcg:user:*, parsed
    into users rows. Records that can't be used are returned with a reason
    that never quotes their contents."""
    now = now or utc_now()
    listed = {_text(e) for e in await redis.smembers(USERS_SET_KEY)}
    stored = {_text(k)[len(PROFILE_PREFIX):] for k in await redis.scan_keys(f"{PROFILE_PREFIX}*")}
    activity_emails = {
        _text(k)[len(ACTIVITY_PREFIX):] for k in await redis.scan_keys(f"{ACTIVITY_PREFIX}*")
    }

    parsed: List[UserRecord] = []
    unparseable: List[Unparseable] = []
    profile_emails = set()
    for key_email in sorted(listed | stored):
        payload = await redis.get(f"{PROFILE_PREFIX}{key_email}")
        if not payload:
            unparseable.append(Unparseable(key_email, "listed in agcg:users but has no profile"))
            continue
        try:
            profile = UserProfile.model_validate_json(payload)
        except ValidationError:
            unparseable.append(Unparseable(key_email, "profile is not valid profile JSON"))
            continue
        app_email = profile.email.strip().lower()
        profile_emails.add(app_email)
        try:
            profile = _with_activity(profile, await redis.hgetall(_activity_key(app_email)))
        except (TypeError, ValueError):
            unparseable.append(Unparseable(key_email, "usage record has a non-integer count"))
            continue
        try:
            record = record_from_profile(profile, now)
        except (TypeError, ValueError):
            unparseable.append(
                Unparseable(key_email, "invalid email, role, status, timestamp, count, groups or claims")
            )
            continue
        if record.email != _key_identity(key_email):
            unparseable.append(Unparseable(key_email, "profile email does not match its key"))
            continue
        parsed.append(record)

    # Two Redis records that normalize to one email: copy neither, report both.
    counts = Counter(u.email for u in parsed)
    users = []
    for user in parsed:
        if counts[user.email] > 1:
            unparseable.append(Unparseable(user.email, "duplicate email after normalization"))
        else:
            users.append(user)
    return RedisSnapshot(
        users=users,
        unparseable=unparseable,
        usage_records=len(activity_emails),
        orphan_usage_records=len(activity_emails - profile_emails),
    )


def diff_fields(
    redis_user: UserRecord, mysql_user: UserRecord, *, bootstrap: bool,
    fields: Sequence[str] = COMPARED_FIELDS,
) -> List[str]:
    """Names (never values) of compared fields that differ. A bootstrap
    admin's admin role in MySQL is expected, not a difference."""
    differing = []
    for name in fields:
        if name == "role" and bootstrap and mysql_user.role == "admin":
            continue
        if getattr(redis_user, name) != getattr(mysql_user, name):
            differing.append(name)
    return differing


async def _previous_grant(store: UserStore, email: str) -> bool:
    cursor = None
    while True:
        page = await store.list_role_changes(email=email, limit=500, cursor=cursor)
        if any(
            c.field == "role" and c.new_value == "admin" and c.actor == BOOTSTRAP_ACTOR
            for c in page.changes
        ):
            return True
        if page.next_cursor is None:
            return False
        cursor = page.next_cursor


async def _bootstrap_state(
    store: UserStore, email: str, existing: Optional[UserRecord], in_redis: bool
) -> str:
    """What a grant would do now; for the report only. --apply decides again,
    atomically, in grant_bootstrap_admin."""
    if existing is None:
        return "grant" if in_redis else "missing"
    if existing.role == "admin":
        return BOOTSTRAP_ALREADY_ADMIN
    if await _previous_grant(store, email):
        return f"{BOOTSTRAP_PREVIOUSLY_GRANTED}; role since changed to {existing.role}, left as is"
    return "grant"


async def build_plan(snapshot: RedisSnapshot, store: UserStore, bootstrap_admins: Sequence[str]) -> Plan:
    plan = Plan()
    existing: Dict[str, Optional[UserRecord]] = {}
    for user in snapshot.users:
        current = await store.get_user(user.email)
        existing[user.email] = current
        if current is None:
            plan.to_create.append(user)
            continue
        fields = diff_fields(user, current, bootstrap=user.email in bootstrap_admins)
        if fields:
            plan.differs[user.email] = fields
        else:
            plan.unchanged.append(user.email)
    redis_emails = {u.email for u in snapshot.users}
    for email in bootstrap_admins:
        current = existing[email] if email in existing else await store.get_user(email)
        plan.bootstrap[email] = await _bootstrap_state(store, email, current, email in redis_emails)
    return plan


async def _mysql_emails(store: UserStore) -> List[str]:
    emails, cursor = [], None
    while True:
        page = await store.list_users(UserFilters(), limit=500, cursor=cursor)
        emails.extend(u.email for u in page.users)
        if page.next_cursor is None:
            return emails
        cursor = page.next_cursor


class Report:
    """Prints counts; emails only when show_emails, else stable numbers."""

    def __init__(self, emails: Sequence[str], show_emails: bool, out: Callable[[str], None]) -> None:
        self._numbers = {email: i + 1 for i, email in enumerate(sorted(set(emails)))}
        self._show = show_emails
        self.out = out

    def who(self, email: str) -> str:
        if self._show:
            return email
        number = self._numbers.get(email)
        return f"user #{number}" if number else "a user not in Redis"

    def line(self, text: str = "") -> None:
        self.out(text)


def _shown_provider(user: UserRecord) -> str:
    # Provider is free text in Redis; print only values the app itself sets.
    return user.provider if user.provider in KNOWN_PROVIDERS else "other"


def _breakdown(values: Sequence[str]) -> str:
    counts = Counter(values)
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"


def _print_summary(report: Report, snapshot: RedisSnapshot, plan: Plan, apply: bool) -> None:
    users = snapshot.users
    report.line(f"Mode: {'APPLY' if apply else 'DRY RUN (nothing written; pass --apply to write)'}")
    report.line(f"Redis profiles found: {snapshot.profiles_found}")
    report.line(f"  parseable: {len(users)}")
    report.line(f"  unparseable (skipped): {len(snapshot.unparseable)}")
    for item in snapshot.unparseable:
        report.line(f"    - {report.who(item.key_email)}: {item.reason}")
    # Role and status were validated against the store's fixed values.
    report.line(f"  by role: {_breakdown([u.role for u in users])}")
    report.line(f"  by status: {_breakdown([u.status for u in users])}")
    report.line(f"  by provider: {_breakdown([_shown_provider(u) for u in users])}")
    report.line(f"  admins in Redis: {sum(1 for u in users if u.role == 'admin')}")
    report.line(
        f"Redis usage records: {snapshot.usage_records} "
        f"({snapshot.orphan_usage_records} without a profile, not copied)"
    )
    report.line(f"{'Users to create' if apply else 'Users that would be created'}: {len(plan.to_create)}")
    report.line(f"Already in MySQL and unchanged: {len(plan.unchanged)}")
    report.line(f"Already in MySQL but differing (not overwritten): {len(plan.differs)}")
    for email, fields in sorted(plan.differs.items()):
        report.line(f"    - {report.who(email)}: {', '.join(fields)}")
    report.line(f"Bootstrap admins: {len(plan.bootstrap)}")
    for email, state in plan.bootstrap.items():
        shown = {
            "grant": "grant admin" if apply else "would grant admin",
            "missing": "not in Redis or MySQL; cannot grant",
        }.get(state, state)
        report.line(f"    - {report.who(email)}: {shown}")


async def verify(
    report: Report, snapshot: RedisSnapshot, store: UserStore, created: Sequence[str],
    bootstrap_admins: Sequence[str],
) -> bool:
    """Per email: every parseable Redis profile is in MySQL, rows created by
    this run match Redis (created_at included), bootstrap admins are admin.
    Fails whenever any Redis profile was skipped, since it was not copied."""
    missing, created_mismatch, differs = [], [], []
    for redis_user in snapshot.users:
        email = redis_user.email
        mysql_user = await store.get_user(email)
        if mysql_user is None:
            missing.append(email)
            continue
        is_created = email in created
        fields = diff_fields(
            redis_user, mysql_user, bootstrap=email in bootstrap_admins,
            fields=COMPARED_FIELDS + ("created_at",) if is_created else COMPARED_FIELDS,
        )
        if fields:
            (created_mismatch if is_created else differs).append((email, fields))
    not_admin = []
    for email in bootstrap_admins:
        user = await store.get_user(email)
        if user is None or user.role != "admin":
            not_admin.append(email)
    mysql_emails = await _mysql_emails(store)
    not_in_redis = set(mysql_emails) - {u.email for u in snapshot.users}
    admins = await store.count_admins(active_only=False)

    report.line("Verification:")
    report.line(f"  Redis profiles: {snapshot.profiles_found}; MySQL users: {len(mysql_emails)}")
    report.line(f"  Redis profiles skipped (not copied): {len(snapshot.unparseable)}")
    report.line(f"  Redis users missing from MySQL: {len(missing)}")
    for email in missing:
        report.line(f"    - {report.who(email)}")
    report.line(f"  MySQL users not in Redis (left as is): {len(not_in_redis)}")
    report.line(f"  MySQL admins: {admins}")
    report.line(f"  Bootstrap admins without the admin role: {len(not_admin)}")
    for email in not_admin:
        report.line(f"    - {report.who(email)}")
    report.line(f"  Created this run but differing from Redis: {len(created_mismatch)}")
    for email, fields in created_mismatch:
        report.line(f"    - {report.who(email)}: {', '.join(fields)}")
    report.line(f"  Pre-existing users differing from Redis (left as is): {len(differs)}")
    for email, fields in differs:
        report.line(f"    - {report.who(email)}: {', '.join(fields)}")
    ok = not (snapshot.unparseable or missing or not_admin or created_mismatch)
    report.line(f"  Result: {'OK' if ok else 'MISMATCH'}")
    return ok


async def _preflight(store: UserStore, report: Report) -> bool:
    """Every table the run reads or writes must be usable before anything is
    written, so a missing table can't leave a partial copy."""
    checks = (
        ("users", lambda: store.list_users(limit=1)),
        ("role_changes", lambda: store.list_role_changes(limit=1)),
    )
    for table, check in checks:
        try:
            await check()
        except UserStoreUnavailable:
            report.line(
                f"MySQL table `{table}` is not readable; deploy the user store (#147) first. "
                "Nothing was written."
            )
            return False
    return True


async def migrate(
    redis: ReadOnlyRedis,
    store: UserStore,
    *,
    apply: bool,
    bootstrap_admins: Sequence[str] = (),
    show_emails: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    admins = list(dict.fromkeys(normalize_email(e) for e in bootstrap_admins))
    if not await _preflight(store, Report((), show_emails, out)):
        return 2
    snapshot = await read_redis(redis)
    plan = await build_plan(snapshot, store, admins)
    report = Report(
        [u.email for u in snapshot.users] + [u.key_email for u in snapshot.unparseable] + admins,
        show_emails,
        out,
    )
    _print_summary(report, snapshot, plan, apply)
    if not apply:
        return 0

    created: List[str] = []
    for user in plan.to_create:
        if await store.import_user(replace(user, updated_at=utc_now())):
            created.append(user.email)
        else:
            report.line(f"  {report.who(user.email)} appeared in MySQL meanwhile; left as is")
    report.line(f"Created: {len(created)}")
    granted = 0
    for email in admins:
        # The store re-checks role and earlier grants atomically, so a plan
        # made before another run's grant or a demotion can't re-grant.
        try:
            outcome = await store.grant_bootstrap_admin(email, reason=BOOTSTRAP_REASON)
        except UserNotFound:
            outcome = "not in MySQL; cannot grant"
        granted += outcome == BOOTSTRAP_GRANTED
        report.line(f"  bootstrap {report.who(email)}: {outcome}")
    report.line(f"Bootstrap admin grants logged: {granted}")
    ok = await verify(report, snapshot, store, created, admins)
    return 0 if ok else 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.scripts.migrate_users_to_mysql",
        description="Copy user records from Redis into the MySQL user store (Redis is only read).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only; write nothing (default).")
    mode.add_argument("--apply", action="store_true", help="Create missing users, grant bootstrap admins, verify.")
    parser.add_argument(
        "--bootstrap-admin",
        action="append",
        default=[],
        metavar="EMAIL",
        help=f"Grant this user the admin role (once, logged in role_changes as {BOOTSTRAP_ACTOR}). Repeatable.",
    )
    parser.add_argument("--show-emails", action="store_true", help="Print emails instead of user numbers.")
    parser.add_argument(
        "--redis-url",
        default=None,
        help="Redis to read (default: REDIS_URL from the environment). Never printed.",
    )
    args = parser.parse_args(argv)
    for email in args.bootstrap_admin:
        try:
            normalize_email(email)
        except ValueError:
            parser.error("--bootstrap-admin needs an email address")
    return args


async def _run(args: argparse.Namespace) -> int:
    import aiomysql
    import redis.asyncio as redis_asyncio

    from src.config import settings
    from src.pipeline.run_store import RunStore, _mysql_connection_kwargs

    redis_url = (args.redis_url or settings.redis_url).strip()
    if not redis_url:
        print("No Redis configured (REDIS_URL or --redis-url).", file=sys.stderr)
        return 2
    try:
        client = redis_asyncio.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        # The parser's message can quote the URL, password included.
        print(f"Invalid Redis URL ({type(exc).__name__}); the URL is not shown.", file=sys.stderr)
        return 2
    try:
        # A pool on the existing schema, not RunStore.create(): the app has
        # already created the user tables, and this script runs no DDL.
        pool = await aiomysql.create_pool(**_mysql_connection_kwargs(), autocommit=True, minsize=1, maxsize=2)
    except Exception as exc:
        await client.aclose()
        print(f"Cannot connect to MySQL: {type(exc).__name__}", file=sys.stderr)
        return 2
    store = RunStore(pool)
    try:
        return await migrate(
            ReadOnlyRedis(client),
            store,
            apply=args.apply,
            bootstrap_admins=args.bootstrap_admin,
            show_emails=args.show_emails,
        )
    except UserStoreUnavailable as exc:
        print(f"MySQL error: {exc}", file=sys.stderr)
        return 2
    except redis_asyncio.RedisError as exc:
        print(f"Redis error: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        await store.close()
        await client.aclose()


def main(argv: Optional[Sequence[str]] = None) -> int:
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())

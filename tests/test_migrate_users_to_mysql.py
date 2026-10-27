"""The Redis -> MySQL user migration script, against an in-memory Redis and
FakeUserStore. Nothing here connects to a real Redis or MySQL."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.pipeline.user_store import LoginIdentity
from src.pipeline.user_store_fake import FakeUserStore
from src.scripts import migrate_users_to_mysql as migration
from src.scripts.migrate_users_to_mysql import ReadOnlyRedis, migrate, parse_args

T0 = datetime(2026, 3, 1, 9, 30, 0, 123456, tzinfo=timezone.utc)
ADMIN = "kuangf@mskcc.org"
SECRET_CLAIM = "claim-value-that-must-never-print"

_READ_COMMANDS = {"smembers", "get", "hgetall", "scan_iter"}


class UserRedis:
    """Just the Redis commands src.auth uses for users (strings, one set,
    hashes, SCAN), recording every command name called."""

    def __init__(self) -> None:
        self.strings = {}
        self.sets = {}
        self.hashes = {}
        self.commands = []

    def __getattr__(self, name):
        # Any command not defined here (set, delete, hset, ...) is a write.
        raise AssertionError(f"migration called Redis.{name}")

    async def smembers(self, key):
        self.commands.append("smembers")
        return set(self.sets.get(key, set()))

    async def get(self, key):
        self.commands.append("get")
        return self.strings.get(key)

    async def hgetall(self, key):
        self.commands.append("hgetall")
        return dict(self.hashes.get(key, {}))

    async def scan_iter(self, match, count=None):
        self.commands.append("scan_iter")
        prefix = match.rstrip("*")
        for key in sorted({*self.strings, *self.sets, *self.hashes}):
            if key.startswith(prefix):
                yield key

    def snapshot(self):
        return json.dumps([self.strings, {k: sorted(v) for k, v in self.sets.items()}, self.hashes], sort_keys=True)


def _profile(email, *, provider="google", role="curator", status="active", i=0, **overrides):
    profile = {
        "email": email,
        "name": f"User {i}",
        "picture": None,
        "domain": email.split("@")[1],
        "role": role,
        "status": status,
        "provider": provider,
        "groups": [f"group-{i}"],
        "claims": {"sub": f"sub-{i}", "access_token": SECRET_CLAIM},
        "first_login_at": (T0 + timedelta(days=i)).isoformat(),
        "last_login_at": (T0 + timedelta(days=i + 30)).isoformat(),
        "login_count": i + 1,
        "annotation_count": 0,
        "last_annotation_at": None,
    }
    profile.update(overrides)
    return profile


def _put(redis, profile, *, listed=True, raw=None):
    email = profile["email"] if profile else raw[0]
    redis.strings[f"agcg:user:{email}"] = raw[1] if raw else json.dumps(profile)
    if listed:
        redis.sets.setdefault("agcg:users", set()).add(email)


def _production_like():
    """16 users: all curator/active, 8 Keycloak + 8 Google, one usage record."""
    redis = UserRedis()
    emails = [ADMIN] + [f"user{i:02d}@mskcc.org" for i in range(1, 16)]
    for i, email in enumerate(emails):
        _put(redis, _profile(email, provider="keycloak" if i % 2 else "google", i=i))
    redis.hashes["agcg:user_activity:user03@mskcc.org"] = {
        "annotation_count": "4",
        "last_annotation_at": (T0 + timedelta(days=40)).isoformat(),
    }
    return redis, emails


async def _run(redis, store, *, apply, admins=(ADMIN,), show_emails=False):
    lines = []
    code = await migrate(
        ReadOnlyRedis(redis), store, apply=apply, bootstrap_admins=list(admins),
        show_emails=show_emails, out=lines.append,
    )
    return code, "\n".join(lines)


async def _all_users(store):
    return {u.email: u for u in (await store.list_users(limit=500)).users}


async def test_dry_run_reports_counts_and_writes_nothing():
    redis, _ = _production_like()
    before = redis.snapshot()
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=False)

    assert code == 0
    assert await _all_users(store) == {}
    assert (await store.list_role_changes()).changes == []
    assert redis.snapshot() == before
    assert "DRY RUN" in out
    assert "Redis profiles found: 16" in out
    assert "unparseable (skipped): 0" in out
    assert "by role: curator=16" in out
    assert "by status: active=16" in out
    assert "by provider: google=8, keycloak=8" in out
    assert "admins in Redis: 0" in out
    assert "Redis usage records: 1 (0 without a profile" in out
    assert "Users that would be created: 16" in out
    assert "user #1: would grant admin" in out


async def test_apply_creates_all_and_grants_the_bootstrap_admin_once():
    redis, emails = _production_like()
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=True)

    assert code == 0, out
    users = await _all_users(store)
    assert sorted(users) == sorted(emails)
    assert {u.role for e, u in users.items() if e != ADMIN} == {"curator"}
    assert {u.status for u in users.values()} == {"active"}
    assert users[ADMIN].role == "admin"
    changes = (await store.list_role_changes()).changes
    assert len(changes) == 1
    change = changes[0]
    assert (change.email, change.field, change.old_value, change.new_value) == (ADMIN, "role", "curator", "admin")
    assert change.actor == "system:migration" and "bootstrap" in change.reason
    assert await store.count_admins() == 1
    assert "Created: 16" in out and "Bootstrap admin grants logged: 1" in out
    assert "Redis profiles: 16; MySQL users: 16" in out and "Result: OK" in out


async def test_preserves_provider_groups_timestamps_and_usage():
    redis, _ = _production_like()
    profile = _profile(
        "used@mskcc.org", provider="keycloak", i=5, annotation_count=3,
        last_annotation_at=(T0 + timedelta(days=10)).isoformat(),
    )
    _put(redis, profile)
    redis.hashes["agcg:user_activity:used@mskcc.org"] = {
        "annotation_count": "4", "last_annotation_at": "2026-05-01T00:00:00Z",
    }
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=True)

    assert code == 0, out
    user = await store.get_user("used@mskcc.org")
    assert (user.provider, user.groups, user.name) == ("keycloak", ["group-5"], "User 5")
    assert user.first_login_at == user.created_at == T0 + timedelta(days=5)
    assert user.last_login_at == T0 + timedelta(days=35)
    assert user.login_count == 6
    assert user.annotation_count == 7  # 3 in the profile + 4 in the usage record
    assert user.last_annotation_at == datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert user.claims["sub"] == "sub-5"
    usage_only = await store.get_user("user03@mskcc.org")
    assert usage_only.annotation_count == 4
    assert "Redis usage records: 2 (0 without a profile" in out


async def test_rerun_is_a_no_op():
    redis, _ = _production_like()
    store = FakeUserStore()
    await _run(redis, store, apply=True)
    before = await _all_users(store)

    code, out = await _run(redis, store, apply=True)

    assert code == 0, out
    assert await _all_users(store) == before
    assert len((await store.list_role_changes()).changes) == 1
    assert "Users to create: 0" in out
    assert "Already in MySQL and unchanged: 16" in out
    assert "user #1: already admin" in out
    assert "Created: 0" in out and "Bootstrap admin grants logged: 0" in out


async def test_existing_differing_user_is_reported_not_overwritten():
    redis, _ = _production_like()
    store = FakeUserStore()
    await store.upsert_login(
        LoginIdentity(email="USER05@mskcc.org", name="Live Name", provider="google"),
        default_role="viewer", default_status="pending", at=T0,
    )
    before = await store.get_user("user05@mskcc.org")

    code, out = await _run(redis, store, apply=True)

    assert code == 0, out
    assert await store.get_user("user05@mskcc.org") == before
    assert "Already in MySQL but differing (not overwritten): 1" in out
    assert "role, status" in out and "name" in out
    assert "Pre-existing users differing from Redis (left as is): 1" in out
    assert "Users to create: 15" in out


async def test_bootstrap_grant_is_not_repeated_after_a_human_changes_the_role():
    redis, _ = _production_like()
    store = FakeUserStore()
    await _run(redis, store, apply=True)
    await store.set_role(ADMIN, "curator", actor="someone@mskcc.org", reason="handover")

    code, out = await _run(redis, store, apply=True)

    assert (await store.get_user(ADMIN)).role == "curator"
    assert len((await store.list_role_changes(email=ADMIN)).changes) == 2
    assert "previously granted; role since changed to curator, left as is" in out
    assert code == 1  # verification flags the bootstrap admin without the role


async def test_bootstrap_admin_missing_everywhere_fails_verification():
    redis, _ = _production_like()
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=True, admins=(ADMIN, "nobody@mskcc.org"))

    assert code == 1
    assert "not in Redis or MySQL; cannot grant" in out
    assert "Bootstrap admins without the admin role: 1" in out
    assert await store.get_user("nobody@mskcc.org") is None


async def test_unparseable_records_are_reported_and_skipped():
    redis, _ = _production_like()
    _put(redis, None, raw=("broken@mskcc.org", "{not json"))
    _put(redis, _profile("badrole@mskcc.org", role="owner"))
    _put(redis, _profile("badtime@mskcc.org", first_login_at="yesterday"))
    _put(redis, _profile("other@mskcc.org"), listed=True)
    redis.strings["agcg:user:mismatch@mskcc.org"] = json.dumps(_profile("someoneelse@mskcc.org"))
    redis.sets["agcg:users"].add("ghost@mskcc.org")  # listed, no profile
    redis.hashes["agcg:user_activity:badcount@mskcc.org"] = {"annotation_count": "lots"}
    _put(redis, _profile("badcount@mskcc.org"))
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=True, show_emails=True)

    assert code == 1  # 23 Redis profiles, 17 MySQL users
    users = await _all_users(store)
    assert len(users) == 17 and "other@mskcc.org" in users
    for skipped in ("broken", "badrole", "badtime", "mismatch", "ghost", "badcount"):
        assert f"{skipped}@mskcc.org" not in users
    assert "unparseable (skipped): 6" in out
    assert "broken@mskcc.org: profile is not valid profile JSON" in out
    assert "ghost@mskcc.org: listed in agcg:users but has no profile" in out
    assert "mismatch@mskcc.org: profile email does not match its key" in out
    assert "badcount@mskcc.org: usage record has a non-integer count" in out
    assert "Redis profiles: 23; MySQL users: 17" in out and "Result: MISMATCH" in out


async def test_emails_differing_only_by_normalization_are_both_skipped():
    redis = UserRedis()
    _put(redis, _profile("josé@mskcc.org"))
    _put(redis, _profile("josé@mskcc.org"))
    _put(redis, _profile("plain@mskcc.org"))
    store = FakeUserStore()

    code, out = await _run(redis, store, apply=True, admins=())

    assert list(await _all_users(store)) == ["plain@mskcc.org"]
    assert out.count("duplicate email after normalization") == 2
    assert code == 1


async def test_output_has_no_emails_without_the_flag_and_never_claims():
    redis, emails = _production_like()
    _put(redis, None, raw=("broken@mskcc.org", "{not json"))
    store = FakeUserStore()
    await store.upsert_login(
        LoginIdentity(email="user05@mskcc.org", name="Live"), default_role="viewer", default_status="active",
    )

    _, dry = await _run(redis, store, apply=False)
    _, applied = await _run(redis, store, apply=True)
    _, shown = await _run(redis, store, apply=False, show_emails=True)

    for out in (dry, applied):
        assert "@" not in out
        assert "mskcc" not in out
    for out in (dry, applied, shown):
        assert SECRET_CLAIM not in out and "sub-" not in out
    assert ADMIN in shown and "broken@mskcc.org" in shown


async def test_redis_is_only_read():
    redis, _ = _production_like()
    before = redis.snapshot()

    await _run(redis, FakeUserStore(), apply=True)

    assert set(redis.commands) <= _READ_COMMANDS
    assert redis.snapshot() == before


def test_cli_defaults_to_dry_run_and_validates_options():
    args = parse_args([])
    assert (args.apply, args.bootstrap_admin, args.show_emails) == (False, [], False)
    args = parse_args(["--apply", "--bootstrap-admin", "A@x.org", "--bootstrap-admin", "b@x.org"])
    assert args.apply and args.bootstrap_admin == ["A@x.org", "b@x.org"]
    with pytest.raises(SystemExit):
        parse_args(["--apply", "--dry-run"])
    with pytest.raises(SystemExit):
        parse_args(["--bootstrap-admin", "not-an-email"])


def test_read_only_wrapper_exposes_no_write_commands():
    wrapper = ReadOnlyRedis(UserRedis())
    for command in ("set", "delete", "sadd", "srem", "hset", "hincrby", "expire", "flushdb"):
        assert not hasattr(wrapper, command)
    assert migration.ACTIVITY_PREFIX == "agcg:user_activity:"

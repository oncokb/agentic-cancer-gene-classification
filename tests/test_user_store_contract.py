"""Shared contract suite for the UserStore implementations.

Every test runs against FakeUserStore and against the MySQL UserStoreMixin
on RunStore. The MySQL variant needs a reachable MySQL (e.g. `docker compose
up -d mysql`, or MYSQL_* env vars) and is skipped without one, like
tests/test_run_store.py; any other startup error (bad schema SQL, ...)
fails. Each test uses its own email domain and deletes
only those rows, so it never touches other data in the database.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import aiomysql
import pymysql
import pytest

from src.pipeline import user_store
from src.pipeline.run_store import RunStore, _mysql_connection_kwargs
from src.pipeline.user_store import (
    CuratorRequestAlreadyDecided,
    CuratorRequestNotFound,
    DuplicatePendingCuratorRequest,
    LoginIdentity,
    UserFilters,
    UserNotFound,
)
from src.pipeline.user_store_fake import FakeUserStore

T0 = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)

# MySQL errors meaning "no usable database here": can't connect / unknown
# host / connection lost, and no credentials or database configured.
_MYSQL_UNAVAILABLE_ERRNOS = {2002, 2003, 2005, 2006, 2013, 1045, 1049}


def _mysql_unavailable(exc: BaseException) -> bool:
    if isinstance(exc, pymysql.err.OperationalError):
        return bool(exc.args) and exc.args[0] in _MYSQL_UNAVAILABLE_ERRNOS
    return isinstance(exc, OSError)


@pytest.fixture(params=["fake", "mysql"])
async def backend(request):
    domain = f"ut{uuid.uuid4().hex[:12]}.example"
    if request.param == "fake":
        yield "fake", FakeUserStore(), domain
        return
    try:
        store = await RunStore.create()
    except Exception as exc:
        if not _mysql_unavailable(exc):
            raise
        pytest.skip(f"MySQL not reachable: {exc}")
    try:
        yield "mysql", store, domain
    finally:
        async with store._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                pattern = f"%@{domain}"
                for table in ("role_changes", "curator_requests", "users"):
                    await cursor.execute(f"DELETE FROM {table} WHERE email LIKE %s", (pattern,))
        await store.close()


@pytest.fixture
def store(backend):
    return backend[1]


@pytest.fixture
def email(backend):
    domain = backend[2]
    return lambda local: f"{local}@{domain}"


@pytest.fixture
def break_audit_insert(backend, monkeypatch):
    """Make the role_changes insert fail mid-transaction."""
    kind = backend[0]

    def _break():
        if kind == "mysql":
            monkeypatch.setattr(
                user_store,
                "_INSERT_ROLE_CHANGE_SQL",
                user_store._INSERT_ROLE_CHANGE_SQL.replace("role_changes", "role_changes_missing"),
            )
        else:

            def _fail(self, change):
                raise RuntimeError("audit write failed")

            monkeypatch.setattr(FakeUserStore, "_append_role_change", _fail)

    return _break


@pytest.fixture
def hold_user_lock(backend):
    """Block writers to a user: a row lock from another MySQL connection, or
    the fake's store lock."""
    kind, store, _ = backend

    @asynccontextmanager
    async def _hold(address):
        if kind == "fake":
            async with store._lock:
                yield
            return
        conn = await aiomysql.connect(**_mysql_connection_kwargs(), autocommit=False)
        try:
            await conn.begin()
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT email FROM users WHERE email = %s FOR UPDATE", (address,))
            yield
        finally:
            await conn.rollback()
            conn.close()

    return _hold


async def _login(store, address, *, role="viewer", status="active", at=None, **identity):
    return await store.upsert_login(
        LoginIdentity(email=address, name=identity.pop("name", "Test User"), **identity),
        default_role=role,
        default_status=status,
        at=at,
    )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def test_first_login_creates_user_with_defaults(store, email):
    user = await _login(
        store,
        email("New.User").upper(),
        name="New User",
        picture="https://pics.example/a.png",
        provider="keycloak",
        groups=["g1"],
        claims={"sub": "abc"},
        role="viewer",
        status="pending",
        at=T0,
    )

    assert user.email == email("new.user")
    assert user.domain == email("x").split("@")[1]
    assert (user.name, user.picture, user.provider) == ("New User", "https://pics.example/a.png", "keycloak")
    assert (user.role, user.status) == ("viewer", "pending")
    assert (user.groups, user.claims) == (["g1"], {"sub": "abc"})
    assert user.first_login_at == user.last_login_at == user.created_at == T0
    assert user.login_count == 1
    assert user.annotation_count == 0 and user.last_annotation_at is None
    assert user.role_updated_by is None and user.role_updated_at is None
    assert await store.get_user(email("NEW.user")) == user


async def test_login_never_changes_existing_role_or_status(store, email):
    address = email("returning")
    await _login(store, address, role="viewer", status="pending", at=T0)
    await store.set_role(address, "admin", actor=email("boss"), reason="lead")
    await store.set_status(address, "disabled", actor=email("boss"), reason="left")
    before = await store.get_user(address)

    later = T0 + timedelta(hours=1)
    user = await _login(
        store, address, name="Renamed", picture="https://pics.example/b.png", provider="saml",
        groups=["new-group"], claims={"k": "v"}, role="curator", status="active", at=later,
    )

    assert (user.role, user.status) == ("admin", "disabled")
    assert (user.role_updated_by, user.role_updated_at) == (before.role_updated_by, before.role_updated_at)
    assert (user.name, user.picture, user.provider) == ("Renamed", "https://pics.example/b.png", "saml")
    assert (user.groups, user.claims) == (["new-group"], {"k": "v"})
    assert user.first_login_at == T0 and user.last_login_at == later
    assert user.login_count == 2
    page = await store.list_role_changes(email=address)
    assert len(page.changes) == 2  # only the explicit set_role/set_status


async def test_login_without_name_or_picture_keeps_known_values(store, email):
    address = email("keep")
    await _login(store, address, name="Known Name", picture="https://pics.example/k.png", at=T0)
    user = await _login(store, address, name="", picture=None, at=T0 + timedelta(minutes=1))
    assert (user.name, user.picture) == ("Known Name", "https://pics.example/k.png")


async def test_concurrent_first_logins_create_one_user(store, email):
    address = email("racer")
    await asyncio.gather(*(_login(store, address, role="viewer") for _ in range(10)))
    user = await store.get_user(address)
    assert user.login_count == 10 and user.role == "viewer"


async def test_emails_differing_by_accent_or_collation_are_distinct_users(store, email):
    jose, jose_accent = email("jose"), email("jos\u00e9")
    await _login(store, jose, name="Jose")
    await store.set_role(jose, "admin", actor=email("boss"))

    accented = await _login(store, jose_accent, name="José")
    assert (accented.email, accented.role, accented.login_count) == (jose_accent, "viewer", 1)
    assert (await store.get_user(jose)).role == "admin"

    # NFC + lowercase: decomposed and upper-case spellings are the same user.
    again = await _login(store, email("JOSE\u0301"), name="José")
    assert (again.email, again.login_count) == (jose_accent, 2)
    assert (await store.get_user(email("jose\u0301"))).email == jose_accent

    # ß and ss compare equal under utf8mb4_0900_ai_ci; here they must not.
    await _login(store, email("strasse"))
    assert await store.get_user(email("stra\u00dfe")) is None
    await _login(store, email("stra\u00dfe"))
    assert (await store.get_user(email("strasse"))).login_count == 1

    await _request(store, jose)
    assert await store.get_pending_request(jose_accent) is None
    await _request(store, jose_accent)
    assert (await store.list_role_changes(email=jose_accent)).changes == []
    assert len((await store.list_role_changes(email=jose)).changes) == 1


async def test_actor_is_normalized(store, email):
    address = email("acted-on")
    await _login(store, address)
    change = await store.set_role(address, "curator", actor="  " + email("ADMIN") + " ")
    assert change.actor == email("admin")
    assert (await store.get_user(address)).role_updated_by == email("admin")
    with pytest.raises(ValueError):
        await store.set_role(address, "admin", actor="  ")


async def test_groups_and_claims_come_back_as_json(store, email):
    user = await _login(
        store, email("json"), groups=("g1", "g2"), claims={"roles": ("a", "b"), "nested": {"t": (1, 2)}}
    )
    expected_claims = {"roles": ["a", "b"], "nested": {"t": [1, 2]}}
    assert (user.groups, user.claims) == (["g1", "g2"], expected_claims)
    assert (await store.get_user(email("json"))).claims == expected_claims
    with pytest.raises(ValueError):
        await _login(store, email("bad-json"), claims={"s": {1, 2}})
    assert await store.get_user(email("bad-json")) is None


_NON_JSON_NUMBERS = [float("nan"), float("inf"), float("-inf")]


@pytest.mark.parametrize("bad", _NON_JSON_NUMBERS, ids=["nan", "inf", "-inf"])
@pytest.mark.parametrize(
    "where",
    [
        lambda v: {"claims": {"score": v}},
        lambda v: {"claims": {"scores": [1, v]}},
        lambda v: {"claims": {"nested": {"deep": [{"x": v}]}}},
        lambda v: {"groups": [v]},
        lambda v: {"groups": ["ok", [v]]},
    ],
    ids=["claim", "claim-list", "claim-nested", "group", "group-nested"],
)
async def test_non_json_numbers_are_rejected_before_storage(store, email, bad, where):
    with pytest.raises(ValueError, match="JSON"):
        await _login(store, email("nan-new"), **where(bad))
    assert await store.get_user(email("nan-new")) is None

    before = await _login(store, email("nan-existing"))
    with pytest.raises(ValueError, match="JSON"):
        await _login(store, email("nan-existing"), **where(bad))
    assert await store.get_user(email("nan-existing")) == before


async def test_upsert_login_rejects_unknown_defaults(store, email):
    with pytest.raises(ValueError):
        await _login(store, email("x"), role="superuser")
    with pytest.raises(ValueError):
        await _login(store, email("x"), status="banned")
    assert await store.get_user(email("x")) is None


# ---------------------------------------------------------------------------
# set_role / set_status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, field, old, new",
    [("set_role", "role", "viewer", "curator"), ("set_status", "status", "active", "disabled")],
)
async def test_change_writes_exactly_one_audit_row(store, email, method, field, old, new):
    address = email("target")
    await _login(store, address)

    change = await getattr(store, method)(address, new, actor=email("admin"), reason="why", request_id=None)

    assert (change.field, change.old_value, change.new_value) == (field, old, new)
    assert (change.actor, change.reason, change.request_id) == (email("admin"), "why", None)
    user = await store.get_user(address)
    assert getattr(user, field) == new
    changes = (await store.list_role_changes(email=address)).changes
    assert changes == [change]
    if field == "role":
        assert (user.role_updated_by, user.role_updated_at) == (email("admin"), change.created_at)
    else:
        assert user.role_updated_by is None


@pytest.mark.parametrize("method, value", [("set_role", "viewer"), ("set_status", "active")])
async def test_unchanged_value_is_a_no_op_without_audit(store, email, method, value):
    address = email("same")
    before = await _login(store, address)
    assert await getattr(store, method)(address, value, actor=email("admin")) is None
    assert await store.get_user(address) == before
    assert (await store.list_role_changes(email=address)).changes == []


async def test_set_role_unknown_user_or_value(store, email):
    with pytest.raises(UserNotFound):
        await store.set_role(email("ghost"), "curator", actor=email("admin"))
    await _login(store, email("real"))
    with pytest.raises(ValueError):
        await store.set_role(email("real"), "owner", actor=email("admin"))
    with pytest.raises(ValueError):
        await store.set_status(email("real"), "frozen", actor=email("admin"))
    assert (await store.list_role_changes(email=email("real"))).changes == []


@pytest.mark.parametrize("method, value", [("set_role", "admin"), ("set_status", "disabled")])
async def test_failed_audit_insert_rolls_back_the_change(store, email, break_audit_insert, method, value):
    address = email("atomic")
    before = await _login(store, address)
    break_audit_insert()
    with pytest.raises(Exception):
        await getattr(store, method)(address, value, actor=email("admin"))
    assert await store.get_user(address) == before
    assert (await store.list_role_changes(email=address)).changes == []


async def test_cancelled_set_role_propagates_and_writes_nothing(store, email, backend, hold_user_lock):
    address = email("cancelled")
    before = await _login(store, address)
    async with hold_user_lock(address):
        task = asyncio.create_task(store.set_role(address, "admin", actor=email("admin")))
        await asyncio.sleep(0.5)  # now waiting on the lock
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await store.get_user(address) == before
    assert (await store.list_role_changes(email=address)).changes == []
    if backend[0] == "mysql":
        assert all(not conn.closed for conn in store._pool._free)
    assert await store.set_role(address, "admin", actor=email("admin")) is not None


async def test_concurrent_set_role_audits_each_real_change_once(store, email):
    address = email("contended")
    await _login(store, address)
    results = await asyncio.gather(
        *(store.set_role(address, "curator", actor=email(f"a{i}")) for i in range(8))
    )
    assert sum(r is not None for r in results) == 1
    assert len((await store.list_role_changes(email=address)).changes) == 1


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


async def test_incr_activity_is_atomic_under_concurrency(store, email):
    address = email("busy")
    await _login(store, address)
    times = [T0 + timedelta(seconds=i) for i in range(40)]
    results = await asyncio.gather(*(store.incr_activity(address, 1, at) for at in times))
    assert all(results)
    await store.incr_activity(address, 5, T0 - timedelta(days=1))
    user = await store.get_user(address)
    assert user.annotation_count == 45
    assert user.last_annotation_at == max(times)


async def test_incr_activity_unknown_user_and_bad_count(store, email):
    assert await store.incr_activity(email("nobody"), 1) is False
    await _login(store, email("someone"))
    with pytest.raises(ValueError):
        await store.incr_activity(email("someone"), 0)


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


async def test_list_users_orders_filters_and_paginates(store, email, backend):
    domain = backend[2]
    for i in range(5):
        await _login(store, email(f"u{i}"), at=T0 + timedelta(minutes=i))
    # Two users share a login time; email (descending) breaks the tie.
    await _login(store, email("tie"), at=T0 + timedelta(minutes=2))
    await store.set_role(email("u1"), "curator", actor=email("admin"))
    await store.set_status(email("u3"), "disabled", actor=email("admin"))

    seen = []
    cursor = None
    while True:
        page = await store.list_users(UserFilters(search=domain), limit=2, cursor=cursor)
        seen.extend(u.email for u in page.users)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [email(x) for x in ("u4", "u3", "u2", "tie", "u1", "u0")]

    curators = await store.list_users(UserFilters(role="curator", search=domain))
    assert [u.email for u in curators.users] == [email("u1")]
    disabled = await store.list_users(UserFilters(status="disabled", search=domain))
    assert [u.email for u in disabled.users] == [email("u3")]
    by_name = await store.list_users(UserFilters(search="U2@" + domain.upper()))
    assert [u.email for u in by_name.users] == [email("u2")]
    with pytest.raises(ValueError):
        await store.list_users(cursor="not a cursor!")


async def test_list_users_search_escapes_like_wildcards(store, email, backend):
    await _login(store, email("a_b"))
    await _login(store, email("axb"))
    page = await store.list_users(UserFilters(search="a_b@" + backend[2]))
    assert [u.email for u in page.users] == [email("a_b")]


async def _search(store, domain, needle):
    """Emails in this test's domain matching `needle` (the MySQL database may
    hold other rows)."""
    page = await store.list_users(UserFilters(search=needle), limit=500)
    return sorted(u.email for u in page.users if u.email.endswith("@" + domain))


async def test_search_uses_full_case_folding(store, email, backend):
    domain = backend[2]
    await _login(store, email("ipek"), name="\u0130pek Y\u0131lmaz")  # İpek Yılmaz
    await _login(store, email("strasse"), name="Hans Stra\u00dfe")  # Straße
    await _login(store, email("ross"), name="Hans Strasse")

    for needle in ("\u0130pek", "\u0130PEK", "\u0130pek y\u0131lmaz"):
        assert await _search(store, domain, needle) == [email("ipek")], needle
    # Rule: ß folds to ss, so ß and ss find each other, in names and emails.
    both = sorted([email("strasse"), email("ross")])
    for needle in ("stra\u00dfe", "STRASSE", "Strasse"):
        assert await _search(store, domain, needle) == both, needle
    await _login(store, email("gro\u00df"), name="Anna")  # groß@
    assert await _search(store, domain, "gross@") == [email("gro\u00df")]


async def test_search_treats_composed_and_decomposed_accents_alike(store, email, backend):
    domain = backend[2]
    await _login(store, email("composed"), name="Jos\u00e9 Garc\u00eda")
    await _login(store, email("decomposed"), name="Rene\u0301e Dupont")
    for needle in ("jose\u0301", "JOS\u00c9", "garci\u0301a"):
        assert await _search(store, domain, needle) == [email("composed")], needle
    for needle in ("ren\u00e9e", "RENE\u0301E"):
        assert await _search(store, domain, needle) == [email("decomposed")], needle
    assert await _search(store, domain, "jose garcia") == []


async def test_search_escapes_like_wildcards_in_names(store, email, backend):
    domain = backend[2]
    await _login(store, email("pct"), name="Sure 100% Today")
    await _login(store, email("pct-no"), name="Sure 1000 Today")
    await _login(store, email("under"), name="Mary_Ann")
    await _login(store, email("under-no"), name="MaryxAnn")
    await _login(store, email("slash"), name="back\\slash")
    assert await _search(store, domain, "100%") == [email("pct")]
    assert await _search(store, domain, "y_a") == [email("under")]
    assert await _search(store, domain, "k\\s") == [email("slash")]


async def test_count_admins(store, email):
    baseline_active = await store.count_admins()
    baseline_all = await store.count_admins(active_only=False)
    for name in ("adm1", "adm2"):
        await _login(store, email(name))
        await store.set_role(email(name), "admin", actor="bootstrap@ops.example")
    await store.set_status(email("adm2"), "disabled", actor="bootstrap@ops.example")
    assert await store.count_admins() == baseline_active + 1
    assert await store.count_admins(active_only=False) == baseline_all + 2


# ---------------------------------------------------------------------------
# Curator requests
# ---------------------------------------------------------------------------


async def _request(store, address, **kwargs):
    return await store.create_curator_request(
        address,
        reason=kwargs.get("reason", "I curate fusions"),
        affiliation=kwargs.get("affiliation", "MSK"),
        expertise=kwargs.get("expertise", "Sarcoma"),
        orcid=kwargs.get("orcid"),
    )


async def test_create_and_get_pending_request(store, email):
    address = email("Applicant")
    request = await _request(store, address, orcid="0000-0002-1825-0097")
    assert request.email == address.lower() and request.status == "pending"
    assert request.orcid == "0000-0002-1825-0097"
    assert request.reviewer_email is None and request.decided_at is None
    assert await store.get_pending_request(address) == request
    assert await store.get_curator_request(request.id) == request


async def test_duplicate_pending_request_is_rejected(store, email):
    address = email("dup")
    first = await _request(store, address)
    with pytest.raises(DuplicatePendingCuratorRequest):
        await _request(store, address.upper())
    assert (await store.list_requests(email=address)).requests == [first]


async def test_concurrent_requests_leave_one_pending(store, email):
    address = email("dupe-race")
    results = await asyncio.gather(*(_request(store, address) for _ in range(8)), return_exceptions=True)
    created = [r for r in results if not isinstance(r, BaseException)]
    assert len(created) == 1
    assert all(isinstance(r, DuplicatePendingCuratorRequest) for r in results if r not in created)
    assert len((await store.list_requests(email=address)).requests) == 1


async def test_new_request_allowed_after_decision(store, email):
    address = email("again")
    await _login(store, address)
    first = await _request(store, address)
    await store.decide_request(first.id, False, email("admin"), "not yet")
    second = await _request(store, address)
    assert second.id != first.id
    assert await store.get_pending_request(address) == second


async def test_approve_makes_viewer_curator_with_linked_audit(store, email):
    address = email("viewer")
    await _login(store, address, role="viewer")
    request = await _request(store, address)

    decided = await store.decide_request(request.id, True, email("admin"), "welcome")

    assert (decided.status, decided.reviewer_email, decided.review_note) == ("approved", email("admin"), "welcome")
    assert decided.decided_at is not None
    assert await store.get_curator_request(request.id) == decided
    assert await store.get_pending_request(address) is None
    user = await store.get_user(address)
    assert (user.role, user.role_updated_by) == ("curator", email("admin"))
    changes = (await store.list_role_changes(email=address)).changes
    assert len(changes) == 1
    assert (changes[0].field, changes[0].old_value, changes[0].new_value) == ("role", "viewer", "curator")
    assert (changes[0].request_id, changes[0].actor) == (request.id, email("admin"))


async def test_reject_leaves_role_alone(store, email):
    address = email("rejected")
    await _login(store, address, role="viewer")
    request = await _request(store, address)
    decided = await store.decide_request(request.id, False, email("admin"), "no")
    assert decided.status == "rejected"
    assert (await store.get_user(address)).role == "viewer"
    assert (await store.list_role_changes(email=address)).changes == []


@pytest.mark.parametrize("role", ["curator", "admin"])
async def test_approve_never_demotes(store, email, role):
    address = email("senior")
    await _login(store, address)
    await store.set_role(address, role, actor=email("boss"))
    request = await _request(store, address)
    await store.decide_request(request.id, True, email("admin"))
    assert (await store.get_user(address)).role == role
    assert len((await store.list_role_changes(email=address)).changes) == 1


async def test_approve_for_missing_user_writes_nothing(store, email):
    request = await _request(store, email("no-account"))
    with pytest.raises(UserNotFound):
        await store.decide_request(request.id, True, email("admin"))
    assert (await store.get_curator_request(request.id)).status == "pending"


async def test_approve_rolls_back_request_when_audit_fails(store, email, break_audit_insert):
    address = email("rollback")
    await _login(store, address, role="viewer")
    request = await _request(store, address)
    break_audit_insert()
    with pytest.raises(Exception):
        await store.decide_request(request.id, True, email("admin"))
    assert await store.get_curator_request(request.id) == request
    assert (await store.get_user(address)).role == "viewer"
    assert (await store.list_role_changes(email=address)).changes == []


async def test_decide_unknown_or_already_decided(store, email):
    with pytest.raises(CuratorRequestNotFound):
        await store.decide_request(987654321, True, email("admin"))
    await _login(store, email("once"))
    request = await _request(store, email("once"))
    await store.decide_request(request.id, False, email("admin"))
    with pytest.raises(CuratorRequestAlreadyDecided):
        await store.decide_request(request.id, True, email("admin"))
    assert (await store.get_user(email("once"))).role == "viewer"


async def test_concurrent_decisions_have_one_winner(store, email):
    address = email("contest")
    await _login(store, address, role="viewer")
    request = await _request(store, address)
    results = await asyncio.gather(
        store.decide_request(request.id, True, email("a1")),
        store.decide_request(request.id, False, email("a2")),
        store.decide_request(request.id, True, email("a3")),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1
    assert sum(isinstance(r, CuratorRequestAlreadyDecided) for r in results) == 2
    stored = await store.get_curator_request(request.id)
    assert stored == winners[0]
    expected_role = "curator" if stored.status == "approved" else "viewer"
    assert (await store.get_user(address)).role == expected_role
    assert len((await store.list_role_changes(email=address)).changes) == (1 if stored.status == "approved" else 0)


async def test_list_requests_filters_and_paginates(store, email):
    address = email("history")
    await _login(store, address)
    ids = []
    for i in range(3):
        request = await _request(store, address, reason=f"r{i}")
        ids.append(request.id)
        if i < 2:
            await store.decide_request(request.id, False, email("admin"))
    first = await store.list_requests(email=address, limit=2)
    assert [r.id for r in first.requests] == ids[::-1][:2]
    second = await store.list_requests(email=address, limit=2, cursor=first.next_cursor)
    assert [r.id for r in second.requests] == [ids[0]] and second.next_cursor is None
    pending = await store.list_requests(email=address, status="pending")
    assert [r.id for r in pending.requests] == [ids[2]]
    with pytest.raises(ValueError):
        await store.list_requests(status="maybe")


async def test_list_role_changes_paginates_newest_first(store, email):
    address = email("flip")
    await _login(store, address)
    for role in ("curator", "admin", "viewer"):
        await store.set_role(address, role, actor=email("admin"))
    first = await store.list_role_changes(email=address, limit=2)
    second = await store.list_role_changes(email=address, limit=2, cursor=first.next_cursor)
    values = [c.new_value for c in first.changes + second.changes]
    assert values == ["viewer", "admin", "curator"] and second.next_cursor is None

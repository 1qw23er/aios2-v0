"""Owner inbound-authentication contract tests (#74).

Locks the full HTTP Basic auth matrix and owner-surface protection mandated by
the #74 architecture review's locked decisions:

  * 503 (``owner_auth_not_configured``) vs 401 (missing / wrong credentials);
  * ``WWW-Authenticate: Basic realm="aios-owner"`` on every 401;
  * a wrong id and a wrong key are indistinguishable (both 401, same body);
  * the direct ``review-status`` write backdoor is 410 Gone -- there is no
    owner-authenticated version of it, so the dual-reviewer + owner gate cannot
    be bypassed;
  * ``/orchestrator/process`` and every ``/owner/*`` route require owner auth;
  * ``GET /agents`` and ``GET /agents/{id}`` stay public;
  * an owner action records the AUTHENTICATED ``owner_id`` as the audit actor,
    while the reviewer identity stays the assigned agent (the owner is never the
    reviewer);
  * the owner secret never appears in a response body;
  * an inventory test enumerates exactly which routes are owner-protected.

The ``real_auth_client`` / ``owner_auth_app`` fixtures build an app whose
``authenticate_owner`` dependency is the REAL one (no override), so these tests
exercise production auth behavior -- and the owner-surface inventory test fails
loudly if any route forgets ``authenticate_owner``. The ``client`` fixture
installs a test-only trusted-owner override on *its own* app object (via
``app.dependency_overrides``) for the tests that need an already-authenticated
owner (review wiring / audit identity); that override is local to this app and
cannot leak to the real-auth tests or other apps.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

# Reusable review wiring (plain functions, NOT fixtures) from the review suite.
from test_review_protocol import _launch, _session, _wire_review_flow

from aios.actor import ActorContext
from aios.api.app import create_app
from aios.api.security import (
    MIN_OWNER_API_KEY_LENGTH,
    OWNER_API_KEY_ENV,
    OWNER_ID_ENV,
    OWNER_REALM,
    authenticate_owner,
)
from aios.audit import AuditLog
from aios.models import ReviewResult
from aios.review import owner_approve_review

_OWNER_ID = "owner-real"
_OWNER_KEY = "k" * (MIN_OWNER_API_KEY_LENGTH + 8)  # comfortably >= 32 chars
_CHALLENGE = f'Basic realm="{OWNER_REALM}"'


def _configure_owner(monkeypatch) -> None:
    monkeypatch.setenv(OWNER_ID_ENV, _OWNER_ID)
    monkeypatch.setenv(OWNER_API_KEY_ENV, _OWNER_KEY)


def _unconfigure_owner(monkeypatch) -> None:
    monkeypatch.delenv(OWNER_ID_ENV, raising=False)
    monkeypatch.delenv(OWNER_API_KEY_ENV, raising=False)


@pytest.fixture
def real_auth_client(tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    """A TestClient exercising the REAL ``authenticate_owner`` dependency.

    The autouse test override is popped, so requests must carry genuine Basic
    credentials. Owner auth is left UNCONFIGURED by default; tests call
    ``_configure_owner`` when they need the configured (401) branch.
    """
    monkeypatch.setenv(
        "AIOS_DATABASE_URL", f"sqlite:///{(tmp_path / 'owner_auth.db').as_posix()}"
    )
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    _unconfigure_owner(monkeypatch)
    application = create_app()
    application.dependency_overrides.pop(authenticate_owner, None)
    with TestClient(application, follow_redirects=False) as client:
        yield client


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> Iterator[TestClient]:
    """An already-authenticated owner client.

    The test-only trusted-owner override is installed on THIS app object only,
    via ``app.dependency_overrides`` -- it never touches the production
    ``authenticate_owner`` function and cannot leak to the real-auth fixtures.
    """
    monkeypatch.setenv(
        "AIOS_DATABASE_URL", f"sqlite:///{(tmp_path / 'owner_auth_ov.db').as_posix()}"
    )
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)
    app = create_app()
    app.dependency_overrides[authenticate_owner] = lambda: ActorContext(
        kind="owner", owner_id="owner"
    )
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


# --- 503 (misconfiguration) branch ------------------------------------------


def test_unconfigured_returns_503(real_auth_client: TestClient) -> None:
    """No owner env configured -> a *server* problem, so 503 (never 401)."""
    resp = real_auth_client.get("/audit", auth=(_OWNER_ID, _OWNER_KEY))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "owner_auth_not_configured"


def test_short_key_returns_503(real_auth_client: TestClient, monkeypatch) -> None:
    short = "x" * (MIN_OWNER_API_KEY_LENGTH - 1)
    monkeypatch.setenv(OWNER_ID_ENV, _OWNER_ID)
    monkeypatch.setenv(OWNER_API_KEY_ENV, short)
    # Even with matching credentials, a too-short key is a misconfiguration.
    resp = real_auth_client.get("/audit", auth=(_OWNER_ID, short))
    assert resp.status_code == 503
    assert resp.json()["detail"] == "owner_auth_not_configured"


def test_owner_id_with_colon_returns_503(real_auth_client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv(OWNER_ID_ENV, "ow:ner")  # ':' is the Basic field separator
    monkeypatch.setenv(OWNER_API_KEY_ENV, _OWNER_KEY)
    resp = real_auth_client.get("/audit", auth=("ow:ner", _OWNER_KEY))
    assert resp.status_code == 503


def test_owner_id_with_control_char_returns_503(
    real_auth_client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv(OWNER_ID_ENV, "own\x01er")
    monkeypatch.setenv(OWNER_API_KEY_ENV, _OWNER_KEY)
    resp = real_auth_client.get("/audit")
    assert resp.status_code == 503


# --- 401 (authentication) branch --------------------------------------------


def test_missing_credentials_returns_401_with_challenge(
    real_auth_client: TestClient, monkeypatch
) -> None:
    _configure_owner(monkeypatch)
    resp = real_auth_client.get("/audit")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == _CHALLENGE


def test_wrong_id_returns_401(real_auth_client: TestClient, monkeypatch) -> None:
    _configure_owner(monkeypatch)
    resp = real_auth_client.get("/audit", auth=("not-the-owner", _OWNER_KEY))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == _CHALLENGE


def test_wrong_key_returns_401(real_auth_client: TestClient, monkeypatch) -> None:
    _configure_owner(monkeypatch)
    resp = real_auth_client.get("/audit", auth=(_OWNER_ID, "z" * (MIN_OWNER_API_KEY_LENGTH + 8)))
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == _CHALLENGE


def test_wrong_id_and_wrong_key_indistinguishable(
    real_auth_client: TestClient, monkeypatch
) -> None:
    """A wrong id, a wrong key, and both-wrong are indistinguishable to the caller
    (no short-circuit, no field-level leak): identical status, body and headers."""
    _configure_owner(monkeypatch)
    bad_key = "z" * (MIN_OWNER_API_KEY_LENGTH + 8)
    r_id = real_auth_client.get("/audit", auth=("nope", _OWNER_KEY))
    r_key = real_auth_client.get("/audit", auth=(_OWNER_ID, bad_key))
    r_both = real_auth_client.get("/audit", auth=("nope", bad_key))
    assert r_id.status_code == r_key.status_code == r_both.status_code == 401
    assert r_id.json() == r_key.json() == r_both.json()
    assert {r.headers["WWW-Authenticate"] for r in (r_id, r_key, r_both)} == {_CHALLENGE}


def test_correct_credentials_authorize(real_auth_client: TestClient, monkeypatch) -> None:
    _configure_owner(monkeypatch)
    resp = real_auth_client.get("/audit", auth=(_OWNER_ID, _OWNER_KEY))
    assert resp.status_code == 200


def test_owner_secret_never_in_response_body(
    real_auth_client: TestClient, monkeypatch
) -> None:
    """The owner API key must never be echoed in any response body."""
    _configure_owner(monkeypatch)
    unauth = real_auth_client.get("/audit")
    ok = real_auth_client.get("/audit", auth=(_OWNER_ID, _OWNER_KEY))
    assert _OWNER_KEY not in unauth.text
    assert _OWNER_KEY not in ok.text


# --- backdoor removal (decision 2): no dual-review / owner-gate bypass -------


@pytest.mark.parametrize("method", ["post", "put", "patch"])
def test_review_status_backdoor_is_gone(real_auth_client: TestClient, method: str) -> None:
    """The direct ``review-status`` write is 410 Gone for every method.

    Because it is 410 (not merely owner-authenticated) there is no way -- even
    for the owner -- to force ``Artifact.review_status`` and thereby skip the
    dual-reviewer aggregation and the owner final gate.
    """
    resp = getattr(real_auth_client, method)("/artifacts/some-artifact/review-status")
    assert resp.status_code == 410


# --- orchestration + owner surface protection -------------------------------


def test_orchestrator_process_requires_owner(
    real_auth_client: TestClient, monkeypatch
) -> None:
    _configure_owner(monkeypatch)
    resp = real_auth_client.post("/orchestrator/process")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == _CHALLENGE


def _owner_routes(app) -> list[tuple[str, str]]:
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or ()
        if route.path.startswith("/owner"):
            for method in methods:
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    routes.append((route.path, method))
    return routes


def test_all_owner_routes_require_auth(real_auth_client: TestClient, monkeypatch) -> None:
    """Every ``/owner/*`` route (GET and POST) rejects an unauthenticated call."""
    _configure_owner(monkeypatch)
    owner_routes = _owner_routes(real_auth_client.app)
    assert owner_routes, "expected /owner/* routes to exist"
    for path, method in owner_routes:
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        resp = real_auth_client.request(method, concrete)
        assert resp.status_code == 401, f"{method} {path} unprotected (got {resp.status_code})"
        assert resp.headers.get("WWW-Authenticate") == _CHALLENGE


def test_public_agent_reads_stay_public(real_auth_client: TestClient, monkeypatch) -> None:
    """``GET /agents`` and ``GET /agents/{id}`` remain readable without auth."""
    _configure_owner(monkeypatch)
    r_list = real_auth_client.get("/agents")
    assert r_list.status_code == 200
    r_one = real_auth_client.get("/agents/does-not-exist")
    assert r_one.status_code not in (401, 403, 503)


# --- owner-route inventory (decision 6/8) -----------------------------------


def _requires_owner(dependant) -> bool:
    stack = [dependant]
    seen: set[int] = set()
    while stack:
        dep = stack.pop()
        if id(dep) in seen:
            continue
        seen.add(id(dep))
        if getattr(dep, "call", None) is authenticate_owner:
            return True
        stack.extend(dep.dependencies)
    return False


def _protected_routes(app) -> set[tuple[str, str]]:
    protected: set[tuple[str, str]] = set()
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        methods = getattr(route, "methods", None)
        if dependant is None or not methods:
            continue
        if _requires_owner(dependant):
            for method in methods:
                if method not in {"HEAD", "OPTIONS"}:
                    protected.add((route.path, method))
    return protected


def test_owner_route_inventory(real_auth_client: TestClient) -> None:
    """Pin exactly which routes are owner-protected vs public (regression guard)."""
    protected = _protected_routes(real_auth_client.app)

    must_protect = {
        ("/audit", "GET"),
        ("/orchestrator/process", "POST"),
        ("/tasks/{review_task_id}/review/submit", "POST"),
        ("/tasks/{task_id}/review/revision", "POST"),
        ("/tasks/{task_id}/revision", "POST"),
        ("/tasks/{task_id}/publish-gate", "POST"),
        ("/artifacts/{artifact_id}/reviews/approve", "POST"),
        ("/approvals/{approval_id}/decide", "POST"),
        ("/agents", "POST"),
        ("/agents/{agent_id}/enabled", "PUT"),
        ("/knowledge/candidates", "POST"),
        ("/knowledge/candidates/{candidate_id}/review", "POST"),
        ("/knowledge/facts/{fact_id}/deactivate", "POST"),
        ("/knowledge/unclassified", "GET"),
    }
    missing = must_protect - protected
    assert not missing, f"expected these routes to be owner-protected: {sorted(missing)}"

    must_stay_public = {
        ("/health", "GET"),
        ("/agents", "GET"),
        ("/agents/{agent_id}", "GET"),
        ("/projects", "GET"),
        ("/projects", "POST"),
    }
    leaked = must_stay_public & protected
    assert not leaked, f"these routes must NOT be owner-protected: {sorted(leaked)}"

    # Every declared /owner/* route must be owner-protected.
    owner_declared = set(_owner_routes(real_auth_client.app))
    assert owner_declared <= protected, (
        f"unprotected /owner routes: {sorted(owner_declared - protected)}"
    )


# --- review submit identity semantics (decision 1) --------------------------


def test_owner_action_records_authenticated_owner_id_not_reviewer(
    client: TestClient,
) -> None:
    """An owner action's audit actor is the AUTHENTICATED owner_id (here 'alice'),
    while every review is attributed to the assigned AGENT -- the owner is never
    recorded as a reviewer (decision 1)."""
    _launch(client)
    with _session() as session:
        draft, tasks = _wire_review_flow(session)
        # Each review is bound to a real agent reviewer, never to an owner identity.
        for review_task in tasks:
            result = session.exec(
                select(ReviewResult).where(ReviewResult.review_task_id == review_task.id)
            ).first()
            assert result is not None
            assert result.reviewer_agent_id == review_task.assigned_agent_id
            assert result.reviewer_agent_id not in (None, "owner", "alice")

        # The owner final approval carries the *authenticated* owner_id into audit.
        owner_approve_review(
            session,
            artifact_id=draft.id,
            actor=ActorContext(kind="owner", owner_id="alice"),
        )
        approved = session.exec(
            select(AuditLog).where(AuditLog.action == "review.owner_approved")
        ).first()
        assert approved is not None
        assert approved.actor == "alice"


# --- override isolation (decision 8: override lives only in tests, no leak) ---


def test_override_does_not_leak_across_apps(
    authenticated_app: FastAPI,
    owner_auth_app: FastAPI,
    monkeypatch,
) -> None:
    """A trusted-owner override installed on one app must NOT leak to other apps.

    Build an app WITH the override and confirm a protected route is authorized
    without credentials; then reuse a FRESH app (``owner_auth_app``, which pops
    any override) and confirm the same route is rejected -- 503 while owner auth
    is unconfigured, and 401 (with the ``WWW-Authenticate`` challenge) once the
    owner is configured but no credentials are supplied. If the override had
    leaked into shared/global state, the second app would wrongly return 200.
    """
    # App A: override installed on this app object only.
    with TestClient(authenticated_app, follow_redirects=False) as a:
        assert a.get("/audit").status_code == 200
        # The override key is present only on app A.
        assert authenticate_owner in authenticated_app.dependency_overrides

    # App B: a brand-new app with no override -> real auth rejects it.
    with TestClient(owner_auth_app, follow_redirects=False) as b:
        assert authenticate_owner not in owner_auth_app.dependency_overrides
        # Unconfigured owner -> 503 (server misconfiguration, not auth).
        assert b.get("/audit").status_code == 503
        # Configure the owner, still no credentials -> 401 with challenge.
        monkeypatch.setenv(OWNER_ID_ENV, _OWNER_ID)
        monkeypatch.setenv(OWNER_API_KEY_ENV, _OWNER_KEY)
        resp = b.get("/audit")
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == _CHALLENGE

    # Both fixtures pop the override on teardown (see conftest), so no override
    # can leak into a subsequent test's app. The rejection above already proves
    # the override never reached shared/global state.

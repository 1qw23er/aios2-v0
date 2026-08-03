"""Owner Operating Layer V0 -- owner-surface contract suite (Issue #121 / PR #122).

Two layers are pinned here:

1. **Sealed-token navigation chain (§2.1 / §4.1 / §12).** picker ->
   ``project_select`` -> ``project_context`` -> ``detail_view`` ->
   ``inbox_action``. No ``project_id`` request parameter, no raw resource id in
   any URL, and every cross-schema / cross-endpoint / cross-inbox confusion
   collapses to the one uniform failure class.

2. **Owner-facing error normalization (§8).** *Every* rejected owner-surface
   request -- including one whose parameters never reach the handler because
   FastAPI's own request validation rejected them first -- must come back as the
   same bounded business page. A machine-readable ``RequestValidationError``
   body would both hand the owner engineering output and make "parameter
   missing" observably different from "token invalid", which is exactly the
   enumeration signal §4.1 rule 6 exists to remove.

   The scope of that normalization is asserted too: a NON-OOL route keeps
   FastAPI's ordinary 422 JSON, so this is presentation normalization on one
   surface -- not a global API redesign.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.content_draft import ContentDraftService
from aios.customer_service import CustomerService
from aios.db import get_database_url, get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    CsChannel,
    CsSuggestion,
    KnowledgeReviewDecisionValue,
    Message,
    Project,
    ProjectStatus,
)
from aios.owner_inbox import MSG_TOKEN_INVALID

OWNER_ID = "owner-1"
OWNER_KEY = "o" * 40
AUTH = (OWNER_ID, OWNER_KEY)

PROJECT_LABEL = "闲鱼代运营 · 家电类目"
DRAFT_TOPIC = "夏季空调清洗话术"

# Machine-readable validation artefacts that must never reach the owner surface.
# NOTE: these are checked as *JSON-shaped* fragments on purpose -- a bare "loc"
# also occurs inside legitimate CSS ("inline-block"), so a naive substring test
# would be meaningless.
_MACHINE_MARKERS = (
    '"detail"',
    '"loc"',
    '"type"',
    '"msg"',
    "Field required",
    "value_error",
    "type_error",
    "validation error",
)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@dataclass
class Seed:
    project_id: str
    draft_id: str


def _key_b64(raw: bytes) -> str:
    """Encode key material exactly the way operators must configure it (§2.1.1).

    Unpadded base64url -- the single spelling ``_decode_key`` accepts. Tests use
    this helper rather than ``base64.b64encode`` so a fixture can never drift
    into a spelling the production loader rejects.
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture
def ool_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIOS_DATABASE_URL", f"sqlite:///{(tmp_path / 'ool.db').as_posix()}")
    monkeypatch.setenv("AIOS_OWNER_ID", OWNER_ID)
    monkeypatch.setenv("AIOS_OWNER_API_KEY", OWNER_KEY)
    monkeypatch.setenv("AIOS_OOL_TOKEN_CURRENT_KID", "k1")
    monkeypatch.setenv("AIOS_OOL_TOKEN_CURRENT_KEY_B64", _key_b64(b"A" * 32))
    monkeypatch.delenv("AIOS_OOL_TOKEN_PREVIOUS_KID", raising=False)
    monkeypatch.delenv("AIOS_OOL_TOKEN_PREVIOUS_KEY_B64", raising=False)
    monkeypatch.delenv("AIOS_AGENT_API_KEY", raising=False)


@pytest.fixture
def seed(ool_env) -> Seed:
    """One live Project holding one content draft that is waiting on the owner."""
    run_migrations()
    owner = ActorContext(kind="owner", owner_id=OWNER_ID)
    with Session(get_engine(get_database_url())) as session:
        project = Project(name=PROJECT_LABEL, objective="测试", status=ProjectStatus.ACTIVE)
        session.add(project)
        session.commit()
        session.refresh(project)
        draft = ContentDraftService(session).create_content_draft(
            project_id=project.id,
            actor=owner,
            topic=DRAFT_TOPIC,
            body="先问使用年限，再给清洗套餐报价。",
            idempotency_key="idem-ool-1",
        )
        return Seed(project_id=project.id, draft_id=draft.id)


@pytest.fixture
def client(seed: Seed) -> TestClient:
    from aios.api.app import create_app

    with TestClient(create_app(), follow_redirects=False) as test_client:
        yield test_client


def _select_token(client: TestClient) -> str:
    page = client.get("/owner/project-picker", auth=AUTH)
    assert page.status_code == 200, page.text[:400]
    match = re.search(r'name="select_token" value="([^"]+)"', page.text)
    assert match is not None, "picker did not mint a project_select token"
    return match.group(1)


def _context_token(client: TestClient) -> str:
    hub = client.post(
        "/owner/project-pick", auth=AUTH, data={"select_token": _select_token(client)}
    )
    assert hub.status_code == 200, hub.text[:400]
    match = re.search(r"/owner/inboxes/content\?ctx=([^\"&]+)", hub.text)
    assert match is not None, "hub did not link an inbox with a project_context"
    return unquote(match.group(1))


def _detail_token(client: TestClient, ctx: str) -> str:
    page = client.get("/owner/inboxes/content", auth=AUTH, params={"ctx": ctx})
    assert page.status_code == 200, page.text[:400]
    match = re.search(r'name="detail_token" value="([^"]+)"', page.text)
    assert match is not None, "inbox did not mint a detail_view token"
    return match.group(1)


def _action_token(client: TestClient, ctx: str) -> str:
    detail = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert detail.status_code == 200, detail.text[:400]
    match = re.search(r'name="action_token" value="([^"]+)"', detail.text)
    assert match is not None, "detail did not mint an inbox_action token"
    return match.group(1)


def assert_normalized_failure(response, *, seed: Seed | None = None) -> None:
    """The one bounded owner-facing failure class (§8).

    409 + HTML + business sentence, with no validation diagnostics, no raw
    sealed token and no internal identifier.
    """
    assert response.status_code == 409, f"{response.status_code}: {response.text[:300]}"
    assert response.headers["content-type"].startswith("text/html")
    for marker in _MACHINE_MARKERS:
        assert marker not in response.text, f"leaked validation artefact {marker!r}"
    assert "422" not in response.text
    assert "Traceback" not in response.text
    assert "ServiceError" not in response.text
    if seed is not None:
        assert seed.project_id not in response.text
        assert seed.draft_id not in response.text


# --------------------------------------------------------------------------
# 1. sealed-token navigation chain
# --------------------------------------------------------------------------


def test_picker_shows_business_labels_and_hides_ids(client: TestClient, seed: Seed) -> None:
    page = client.get("/owner/project-picker", auth=AUTH)
    assert page.status_code == 200
    assert PROJECT_LABEL in page.text
    assert seed.project_id not in page.text
    assert 'name="select_token"' in page.text


def test_picker_requires_owner_auth(client: TestClient) -> None:
    assert client.get("/owner/project-picker").status_code == 401


def test_project_pick_mints_a_context_without_exposing_ids(
    client: TestClient, seed: Seed
) -> None:
    hub = client.post(
        "/owner/project-pick", auth=AUTH, data={"select_token": _select_token(client)}
    )
    assert hub.status_code == 200
    assert PROJECT_LABEL in hub.text
    assert seed.project_id not in hub.text
    assert "/owner/inboxes/content?ctx=" in hub.text


def test_select_token_replay_is_stateless(client: TestClient) -> None:
    """T-PS4: replaying a ``project_select`` only mints a fresh equivalent context."""
    token = _select_token(client)
    first = client.post("/owner/project-pick", auth=AUTH, data={"select_token": token})
    second = client.post("/owner/project-pick", auth=AUTH, data={"select_token": token})
    assert (first.status_code, second.status_code) == (200, 200)


def test_select_token_is_rejected_by_the_inbox_list(client: TestClient, seed: Seed) -> None:
    """T-PS1: a ``project_select`` may only be spent at ``/owner/project-pick``."""
    response = client.get(
        "/owner/inboxes/content", auth=AUTH, params={"ctx": _select_token(client)}
    )
    assert_normalized_failure(response, seed=seed)


def test_context_token_is_rejected_by_project_pick(client: TestClient, seed: Seed) -> None:
    """T-PS2: the reverse confusion is refused just as uniformly."""
    response = client.post(
        "/owner/project-pick", auth=AUTH, data={"select_token": _context_token(client)}
    )
    assert_normalized_failure(response, seed=seed)


def test_inbox_lists_business_rows_only(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    page = client.get("/owner/inboxes/content", auth=AUTH, params={"ctx": ctx})
    assert page.status_code == 200
    assert DRAFT_TOPIC in page.text
    assert seed.draft_id not in page.text
    assert seed.project_id not in page.text
    assert 'name="detail_token"' in page.text


def test_unknown_inbox_kind_is_normalized(client: TestClient, seed: Seed) -> None:
    response = client.get(
        "/owner/inboxes/nope", auth=AUTH, params={"ctx": _context_token(client)}
    )
    assert_normalized_failure(response, seed=seed)


def test_detail_view_keeps_the_resource_reference_sealed(
    client: TestClient, seed: Seed
) -> None:
    """T-D1: there is no ``/{rid}`` route; the reference travels encrypted in a POST."""
    ctx = _context_token(client)
    detail = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert detail.status_code == 200
    assert DRAFT_TOPIC in detail.text
    assert seed.draft_id not in detail.text
    assert seed.project_id not in detail.text
    assert 'name="action_token"' in detail.text


def test_detail_token_is_rejected_by_another_inbox(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    response = client.post(
        "/owner/inboxes/cs/detail",
        auth=AUTH,
        data={"detail_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert_normalized_failure(response, seed=seed)


def test_detail_token_is_rejected_by_decide(client: TestClient, seed: Seed) -> None:
    """T-D6: a ``detail_view`` token can never authorize a mutation."""
    ctx = _context_token(client)
    response = client.post(
        "/owner/inboxes/content/decide",
        auth=AUTH,
        data={"action_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert_normalized_failure(response, seed=seed)


def test_action_token_is_rejected_by_detail(client: TestClient, seed: Seed) -> None:
    """T-D7: and an ``inbox_action`` token can never be replayed as navigation."""
    ctx = _context_token(client)
    response = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": _action_token(client, ctx), "ctx": ctx},
    )
    assert_normalized_failure(response, seed=seed)


def test_decide_applies_and_reports_a_business_outcome(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    response = client.post(
        "/owner/inboxes/content/decide",
        auth=AUTH,
        data={
            "action_token": _action_token(client, ctx),
            "ctx": ctx,
            "reason": "先按最新报价改一版",
        },
    )
    assert response.status_code == 200
    assert DRAFT_TOPIC in response.text
    assert seed.draft_id not in response.text


def test_action_token_replay_never_mutates_twice(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    token = _action_token(client, ctx)
    data = {"action_token": token, "ctx": ctx, "reason": "先按最新报价改一版"}
    assert client.post("/owner/inboxes/content/decide", auth=AUTH, data=data).status_code == 200
    replay = client.post(
        "/owner/inboxes/content/decide",
        auth=AUTH,
        data={**data, "reason": "重复提交"},
    )
    assert replay.status_code in (200, 409)
    assert seed.draft_id not in replay.text
    assert seed.project_id not in replay.text


def test_missing_token_key_fails_closed(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("AIOS_OOL_TOKEN_CURRENT_KEY_B64", raising=False)
    response = client.get("/owner/project-picker", auth=AUTH)
    assert response.status_code == 503
    assert "AIOS_OOL" not in response.text


def test_garbage_token_leaks_no_engineering_string(client: TestClient, seed: Seed) -> None:
    response = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": "junk", "ctx": _context_token(client)},
    )
    assert_normalized_failure(response, seed=seed)
    for engineering in ("Artifact not found", "not authorized", "token_type", "project_select"):
        assert engineering not in response.text


# --------------------------------------------------------------------------
# 2. §8 error normalization -- missing / malformed request parameters
# --------------------------------------------------------------------------


def test_missing_project_select_token_is_normalized(client: TestClient, seed: Seed) -> None:
    """A ``project_select`` that never arrives must not surface FastAPI's 422."""
    assert_normalized_failure(
        client.post("/owner/project-pick", auth=AUTH, data={}), seed=seed
    )


def test_missing_project_context_token_is_normalized(client: TestClient, seed: Seed) -> None:
    assert_normalized_failure(client.get("/owner/inboxes/content", auth=AUTH), seed=seed)


def test_missing_detail_view_token_is_normalized(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    assert_normalized_failure(
        client.post("/owner/inboxes/content/detail", auth=AUTH, data={"ctx": ctx}), seed=seed
    )


def test_missing_inbox_action_token_is_normalized(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    assert_normalized_failure(
        client.post(
            "/owner/inboxes/content/decide", auth=AUTH, data={"ctx": ctx, "reason": "x"}
        ),
        seed=seed,
    )


def test_missing_context_on_a_decide_is_normalized(client: TestClient, seed: Seed) -> None:
    ctx = _context_token(client)
    assert_normalized_failure(
        client.post(
            "/owner/inboxes/content/decide",
            auth=AUTH,
            data={"action_token": _action_token(client, ctx)},
        ),
        seed=seed,
    )


def test_malformed_owner_form_body_is_normalized(client: TestClient, seed: Seed) -> None:
    """An unparseable multipart body is a client error, not an engineering report."""
    response = client.post(
        "/owner/inboxes/content/decide",
        auth=AUTH,
        content=b"not-a-valid-multipart-body",
        headers={"content-type": "multipart/form-data"},
    )
    assert_normalized_failure(response, seed=seed)


def test_wrong_content_type_on_an_owner_form_is_normalized(
    client: TestClient, seed: Seed
) -> None:
    """A JSON body posted at a form endpoint yields no fields -> same failure page."""
    assert_normalized_failure(
        client.post("/owner/project-pick", auth=AUTH, json={"select_token": "x"}), seed=seed
    )


# --------------------------------------------------------------------------
# 3. indistinguishability + scope
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    ["", "junk", "AAAA" * 40],
    ids=["missing", "malformed", "well-formed-but-undecryptable"],
)
def test_absent_and_invalid_tokens_are_indistinguishable(
    client: TestClient, seed: Seed, bad_value: str
) -> None:
    """Missing / malformed / undecryptable must be one response, byte for byte.

    Anything less lets a caller enumerate *why* a token was refused, which is
    the very signal the uniform failure class removes (§4.1 rule 6).
    """
    data = {"select_token": bad_value} if bad_value else {}
    response = client.post("/owner/project-pick", auth=AUTH, data=data)
    assert_normalized_failure(response, seed=seed)
    assert MSG_TOKEN_INVALID in response.text


def test_missing_and_invalid_token_bodies_are_identical(client: TestClient) -> None:
    absent = client.post("/owner/project-pick", auth=AUTH, data={})
    invalid = client.post("/owner/project-pick", auth=AUTH, data={"select_token": "junk"})
    assert absent.status_code == invalid.status_code
    assert absent.headers["content-type"] == invalid.headers["content-type"]
    assert absent.text == invalid.text


def _ring():
    from aios.owner_inbox import load_token_key_ring

    return load_token_key_ring()


def _mint(**claims) -> str:
    """Seal an arbitrary *valid-shaped* token so a single rejection cause can be isolated."""
    from aios.owner_inbox import seal_token

    return seal_token(claims, ring=_ring())


def _raw_seal(claims: dict) -> str:
    """Seal claims **without** mint-time validation.

    ``seal_token`` refuses to mint a token carrying a forbidden or extra claim,
    so the only way to prove the *resolver* rejects one -- and rejects it with
    the same bytes as every other untrusted input -- is to forge the envelope
    the way an attacker holding a leaked key would.
    """
    import json
    import secrets as _secrets

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from aios.owner_inbox import (
        ENVELOPE_ALG,
        ENVELOPE_VERSION,
        NONCE_BYTES,
        _b64u_encode,
        canonical_json,
    )

    ring = _ring()
    header = canonical_json(
        {"v": ENVELOPE_VERSION, "kid": ring.current.kid, "alg": ENVELOPE_ALG}
    ).encode("utf-8")
    plaintext = json.dumps(
        claims, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    nonce = _secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(ring.current.key).encrypt(nonce, plaintext, header)
    return ".".join([_b64u_encode(header), _b64u_encode(nonce), _b64u_encode(ciphertext)])


def _action_claims(seed: Seed, **overrides) -> dict:
    from aios.owner_inbox import PURPOSE_CONTENT_APPROVE, TOKEN_TYPE_INBOX_ACTION

    claims = {
        "token_type": TOKEN_TYPE_INBOX_ACTION,
        "owner": OWNER_ID,
        "inbox": "content",
        "kind": "artifact",
        "rid": seed.draft_id,
        "display_binding": "0" * 64,
        "operating_project": seed.project_id,
        "resource_scope": seed.project_id,
        "purpose": PURPOSE_CONTENT_APPROVE,
    }
    claims.update(overrides)
    return claims


def test_every_untrusted_rejection_is_byte_identical(client: TestClient, seed: Seed) -> None:
    """T-N1: the whole untrusted-input class is **one** response, byte for byte.

    Before this test the surface answered "wrong endpoint", "stale card" and
    "row not found" with three *different* Chinese sentences, so an attacker
    holding one leaked token could probe which resource ids exist, which ones
    live in another project, and which ones merely changed -- precisely the
    enumeration oracle §4.1 rule 6 forbids. Comparing full response bodies (not
    just status codes) is the only assertion that actually pins that down: a
    status-only check passes happily while the body leaks the cause.
    """
    import time

    from aios.owner_inbox import (
        PURPOSE_CS_ADOPT_AND_SEND,
        TOKEN_TYPE_INBOX_ACTION,
    )

    ctx = _context_token(client)
    detail_token = _detail_token(client, ctx)
    valid_action = _action_token(client, ctx)
    assert valid_action  # the happy path still mints, so the matrix is meaningful

    # A second live project, used to prove "visible from another operating
    # context" is refused with the same bytes as "does not exist at all".
    with Session(get_engine(get_database_url())) as session:
        other = Project(name="另一个项目", objective="测试", status=ProjectStatus.ACTIVE)
        session.add(other)
        session.commit()
        session.refresh(other)
        other_id = other.id

    now = int(time.time())
    cases: dict[str, dict] = {
        "absent": {},
        "malformed": {"action_token": "junk"},
        "undecryptable": {"action_token": "AAAA" * 40},
        "expired": {
            "action_token": _mint(**_action_claims(seed), iat=now - 7200, exp=now - 3600)
        },
        "wrong-token-type-detail": {"action_token": detail_token},
        "wrong-token-type-context": {"action_token": ctx},
        "wrong-purpose-other-inbox": {
            "action_token": _mint(**_action_claims(seed, purpose=PURPOSE_CS_ADOPT_AND_SEND))
        },
        "forbidden-extra-claim": {
            "action_token": _raw_seal(
                {
                    **_action_claims(seed),
                    "v": 1,
                    "iat": now,
                    "exp": now + 600,
                    "project_ref": seed.project_id,
                }
            )
        },
        "stale-display-binding": {"action_token": _mint(**_action_claims(seed))},
        "drifted-resource-scope": {
            "action_token": _mint(**_action_claims(seed, resource_scope=other_id))
        },
        "row-outside-operating-project": {
            "action_token": _mint(**_action_claims(seed, operating_project=other_id))
        },
        "unresolvable-rid": {
            "action_token": _mint(**_action_claims(seed, rid="art_does_not_exist"))
        },
    }

    bodies: dict[str, bytes] = {}
    for name, extra in cases.items():
        response = client.post(
            "/owner/inboxes/content/decide", auth=AUTH, data={"ctx": ctx, **extra}
        )
        assert_normalized_failure(response, seed=seed)
        assert other_id not in response.text, f"{name} leaked a project id"
        bodies[name] = response.content

    # An unknown inbox kind in the URL belongs to the same class.
    unknown_kind = client.post(
        "/owner/inboxes/nope/decide", auth=AUTH, data={"ctx": ctx, "action_token": valid_action}
    )
    assert_normalized_failure(unknown_kind, seed=seed)
    bodies["unknown-inbox-kind"] = unknown_kind.content

    baseline = bodies["absent"]
    differing = sorted(name for name, body in bodies.items() if body != baseline)
    assert not differing, f"distinguishable rejection bodies: {differing}"

    # Guard against the degenerate pass where *nothing* was actually exercised.
    assert len(bodies) == len(cases) + 1
    assert TOKEN_TYPE_INBOX_ACTION  # imported for the claim builder above


def test_untrusted_rejection_is_identical_across_endpoints(
    client: TestClient, seed: Seed
) -> None:
    """T-N2: the uniform page does not depend on *which* owner endpoint refused.

    If each endpoint rendered its own variant, the response body would still
    reveal how far a forged token travelled through the chain.
    """
    ctx = _context_token(client)
    responses = [
        client.post("/owner/project-pick", auth=AUTH, data={"select_token": "junk"}),
        client.get("/owner/inboxes/content", auth=AUTH, params={"ctx": "junk"}),
        client.post(
            "/owner/inboxes/content/detail",
            auth=AUTH,
            data={"detail_token": "junk", "ctx": ctx},
        ),
        client.post(
            "/owner/inboxes/content/decide",
            auth=AUTH,
            data={"action_token": "junk", "ctx": ctx},
        ),
    ]
    for response in responses:
        assert_normalized_failure(response, seed=seed)
    assert len({response.content for response in responses}) == 1


def test_empty_picker_still_fails_closed_on_broken_key(ool_env, monkeypatch) -> None:
    """T-N3: no live project must not disguise unusable key material.

    The picker used to load the key ring lazily, inside the per-project mint
    loop. With zero live projects the loop never ran, so a deployment whose
    token key was missing or corrupt answered ``200`` with an empty list -- the
    one state where the surface looks healthy precisely because it can seal
    nothing. Fail-closed has to be a property of the *configuration*, not of
    how much data happens to exist.
    """
    from aios.api.app import create_app

    run_migrations()  # schema only: deliberately zero Project rows
    monkeypatch.setenv("AIOS_OOL_TOKEN_CURRENT_KEY_B64", "not-base64!!!")

    with TestClient(create_app(), follow_redirects=False) as broken:
        response = broken.get("/owner/project-picker", auth=AUTH)
        assert response.status_code == 503, response.text[:300]
        assert 'name="select_token"' not in response.text

    # Same empty database, healthy key -> an honest empty picker, not a 503.
    monkeypatch.setenv("AIOS_OOL_TOKEN_CURRENT_KEY_B64", _key_b64(b"A" * 32))
    with TestClient(create_app(), follow_redirects=False) as healthy:
        ok = healthy.get("/owner/project-picker", auth=AUTH)
        assert ok.status_code == 200, ok.text[:300]
        assert 'name="select_token"' not in ok.text


def test_owner_auth_still_precedes_normalization(client: TestClient) -> None:
    """Normalization must not swallow 401: an unauthenticated call stays 401.

    Otherwise a missing credential would be laundered into a business page and
    the owner-surface auth guard would silently stop being observable.
    """
    for method, url in (
        ("post", "/owner/project-pick"),
        ("get", "/owner/inboxes/content"),
        ("post", "/owner/inboxes/content/detail"),
        ("post", "/owner/inboxes/content/decide"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 401, f"{method.upper()} {url} -> {response.status_code}"


@pytest.mark.parametrize(
    "key_env",
    [
        {},
        {"AIOS_OOL_TOKEN_CURRENT_KID": "k1"},
        {"AIOS_OOL_TOKEN_CURRENT_KEY_B64": _key_b64(b"A" * 32)},
        {
            "AIOS_OOL_TOKEN_CURRENT_KID": "k1",
            "AIOS_OOL_TOKEN_CURRENT_KEY_B64": _key_b64(b"A" * 16),
        },
        {"AIOS_OOL_TOKEN_CURRENT_KID": "k1", "AIOS_OOL_TOKEN_CURRENT_KEY_B64": "not-base64!!!"},
        {
            # Padded base64 is a *different spelling* of correct key material.
            # The contract is unpadded base64url, so this must fail closed too:
            # silently repairing it would give the same key two valid forms.
            "AIOS_OOL_TOKEN_CURRENT_KID": "k1",
            "AIOS_OOL_TOKEN_CURRENT_KEY_B64": base64.urlsafe_b64encode(b"A" * 32).decode(),
        },
    ],
    ids=[
        "absent",
        "kid-without-key",
        "key-without-kid",
        "key-too-short",
        "key-not-base64",
        "key-padded",
    ],
)
def test_broken_key_material_fails_closed(seed: Seed, monkeypatch, key_env: dict) -> None:
    """No usable key material must yield 503 -- never a surface that still works.

    Every guarantee on this surface (no raw ids, no client-supplied identity,
    decisions bound to what the owner was shown) is carried *by the sealed
    token*. If a deployment loses or mis-sets the key, the only safe outcome is
    for the surface to refuse to serve. Degrading to an unsealed page, or
    minting a token under a broken key, would keep the UI working while every
    invariant behind it is gone -- the worst possible failure mode.
    """
    from aios.api.app import create_app

    for name in (
        "AIOS_OOL_TOKEN_CURRENT_KID",
        "AIOS_OOL_TOKEN_CURRENT_KEY_B64",
        "AIOS_OOL_TOKEN_PREVIOUS_KID",
        "AIOS_OOL_TOKEN_PREVIOUS_KEY_B64",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in key_env.items():
        monkeypatch.setenv(name, value)

    with TestClient(create_app(), follow_redirects=False) as broken:
        response = broken.get("/owner/project-picker", auth=AUTH)
        assert response.status_code == 503, response.text[:300]
        assert 'name="select_token"' not in response.text
        assert seed.project_id not in response.text


def test_non_ool_validation_error_keeps_fastapi_422(client: TestClient) -> None:
    """Scope guard: the normalization is bound to the OOL surface only.

    A non-OOL route must keep FastAPI's ordinary machine-readable 422 so this
    change stays presentation normalization on one surface rather than a global
    redesign of the API's validation semantics.
    """
    response = client.post("/projects", json={})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


# --------------------------------------------------------------------------
# 4. the one decision that leaves the product boundary
# --------------------------------------------------------------------------
#
# Every other inbox decision only mutates rows we own. ``cs.adopt_and_send``
# actually emits an outbound customer message, so a defect on this path is not
# a 500 the owner retries -- it is a message that was, or was not, really sent.
# The service-layer suite (tests/test_customer_service.py) pins the
# transaction contract; what is pinned *here* is that the sealed-token chain
# reaches it at all. A wired-up route calling a method that does not exist
# looks perfectly healthy in unit tests and dies on the owner's first click.


@dataclass
class CsSeed:
    conversation_id: str
    suggestion_id: str


@pytest.fixture
def cs_seed(seed: Seed) -> CsSeed:
    """One live conversation whose AI draft reply is waiting on the owner."""
    owner = ActorContext(kind="owner", owner_id=OWNER_ID)
    with Session(get_engine(get_database_url())) as session:
        artifact = Artifact(
            project_id=seed.project_id,
            type=ArtifactType.JSON,
            uri="cs-source.json",
            checksum="sha256:cs-source",
            review_status=ArtifactReviewStatus.APPROVED,
            metadata_json={},
        )
        session.add(artifact)
        session.commit()
        session.refresh(artifact)

        knowledge = KnowledgeService(session)
        candidate = knowledge.submit_candidate(
            artifact.id,
            "apple banana cherry date",
            project_id=seed.project_id,
            tags=["wechat_writing"],
            actor=owner,
        )
        knowledge.review_candidate(
            candidate.id,
            KnowledgeReviewDecisionValue.APPROVE,
            "seed rationale",
            actor=owner,
            series_id="ool-cs-series",
            version=1,
        )

        service = CustomerService(session)
        conversation = service.create_conversation(
            owner, project_id=seed.project_id, channel=CsChannel.MOCK
        )
        # 3 of 4 tokens matched -> 0.75 -> below the 0.80 auto-send threshold,
        # so the reply is parked as HUMAN_CONFIRM and shows up in the inbox.
        suggestion = service.generate_suggestion(
            owner,
            conversation_id=conversation.id,
            inbound_message_id=None,
            text="apple banana cherry",
        )
        assert suggestion.decision == "human_confirm", suggestion.decision
        return CsSeed(conversation_id=conversation.id, suggestion_id=suggestion.id)


def _cs_adopt_action_token(client: TestClient, ctx: str) -> str:
    """Walk inbox -> detail and return the ``采用并发送`` inbox_action token."""
    listing = client.get("/owner/inboxes/cs", auth=AUTH, params={"ctx": ctx})
    assert listing.status_code == 200, listing.text[:400]
    detail_match = re.search(r'name="detail_token" value="([^"]+)"', listing.text)
    assert detail_match is not None, "cs inbox listed no actionable conversation"

    detail = client.post(
        "/owner/inboxes/cs/detail",
        auth=AUTH,
        data={"detail_token": detail_match.group(1), "ctx": ctx},
    )
    assert detail.status_code == 200, detail.text[:400]
    action_match = re.search(
        r'采用并发送</div>.*?name="action_token" value="([^"]+)"', detail.text, re.S
    )
    assert action_match is not None, "detail offered no adopt-and-send decision"
    return action_match.group(1)


def _outbound_messages(conversation_id: str) -> list[Message]:
    with Session(get_engine(get_database_url())) as session:
        return list(
            session.exec(
                select(Message).where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "outbound",
                )
            )
        )


def test_cs_adopt_and_send_completes_through_the_sealed_token_chain(
    client: TestClient, cs_seed: CsSeed
) -> None:
    """picker -> pick -> cs inbox -> detail -> decide actually sends the reply."""
    ctx = _context_token(client)
    action_token = _cs_adopt_action_token(client, ctx)

    decide = client.post(
        "/owner/inboxes/cs/decide",
        auth=AUTH,
        data={"action_token": action_token, "ctx": ctx},
    )
    assert decide.status_code == 200, decide.text[:600]
    assert decide.headers["content-type"].startswith("text/html")
    assert "已发送回复。" in decide.text
    assert "Traceback" not in decide.text

    outbound = _outbound_messages(cs_seed.conversation_id)
    assert len(outbound) == 1, f"expected exactly one outbound message, got {len(outbound)}"
    assert outbound[0].sender_type == "owner"
    assert outbound[0].is_auto_sent is False

    with Session(get_engine(get_database_url())) as session:
        assert session.get(CsSuggestion, cs_seed.suggestion_id).consumed is True


def test_cs_adopt_and_send_replay_never_sends_twice(
    client: TestClient, seed: Seed, cs_seed: CsSeed
) -> None:
    """Re-posting the same inbox_action must not emit a second customer message.

    A duplicated outbound message is visible to the customer and cannot be
    withdrawn, so the replay has to collapse into the same bounded failure page
    as any other refused token -- not into a second send.
    """
    ctx = _context_token(client)
    action_token = _cs_adopt_action_token(client, ctx)
    first = client.post(
        "/owner/inboxes/cs/decide",
        auth=AUTH,
        data={"action_token": action_token, "ctx": ctx},
    )
    assert first.status_code == 200, first.text[:400]

    replay = client.post(
        "/owner/inboxes/cs/decide",
        auth=AUTH,
        data={"action_token": action_token, "ctx": ctx},
    )
    assert_normalized_failure(replay, seed=seed)
    assert len(_outbound_messages(cs_seed.conversation_id)) == 1

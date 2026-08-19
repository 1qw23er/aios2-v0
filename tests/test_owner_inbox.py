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
from datetime import datetime
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import append_audit
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
from aios.owner_inbox import (
    INBOX_CONTENT,
    INBOX_CS,
    MSG_TOKEN_INVALID,
    OwnerInboxService,
)

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
    # ``TestResponse`` (from the client) exposes ``.text``; a bare
    # ``HTMLResponse`` (the direct ``_failure`` unit tests) does not in this
    # Starlette version, so derive the body uniformly.
    body = getattr(response, "text", None)
    if body is None:
        body = response.body.decode()
    assert response.status_code == 409, f"{response.status_code}: {body[:300]}"
    assert response.headers["content-type"].startswith("text/html")
    for marker in _MACHINE_MARKERS:
        assert marker not in body, f"leaked validation artefact {marker!r}"
    assert "422" not in body
    assert "Traceback" not in body
    assert "ServiceError" not in body
    if seed is not None:
        assert seed.project_id not in body
        assert seed.draft_id not in body


# --------------------------------------------------------------------------
# 1b. §8 _failure() normalization (issue #126)
# --------------------------------------------------------------------------
#
# §8 says every OOL-level resolution failure reaches the owner surface as the
# *uniform* 409 page. The business sentence is still tailored per status, but
# the HTTP status must collapse to 409 so the owner never observes a
# 422/403/404 distinction (that split is the enumeration signal §4.1 rule 6
# exists to erase). 5xx, by contrast, is a real infrastructure failure the
# owner must not mistake for a retry-able input problem, so it is preserved.


def test_failure_normalizes_422_to_409() -> None:
    """A ``ServiceError(422, …)`` must render as 409 on the owner surface (#126)."""
    from aios.api.owner_inbox_routes import _failure
    from aios.services import ServiceError

    response = _failure(ServiceError(422, "请选择客户阶段。"))
    assert_normalized_failure(response)


@pytest.mark.parametrize("status_code", [403, 404, 409, 422])
def test_failure_normalizes_every_4xx_to_409(status_code: int) -> None:
    """§8: all client (4xx) errors collapse to the single 409 class (#126)."""
    from aios.api.owner_inbox_routes import _failure
    from aios.services import ServiceError

    response = _failure(ServiceError(status_code, "提交的信息不完整，请补充后重试。"))
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")


def test_failure_preserves_5xx_status() -> None:
    """5xx is a real infrastructure failure -- must NOT be rewritten to 409 (#126)."""
    from aios.api.owner_inbox_routes import _failure
    from aios.services import ServiceError

    response = _failure(ServiceError(503, "downstream unavailable"))
    assert response.status_code == 503
    _body = getattr(response, "text", None) or response.body.decode()
    assert "AIOS_OOL" not in _body


def test_decide_422_from_service_normalizes_to_409(
    client: TestClient, seed: Seed, monkeypatch
) -> None:
    """End-to-end: a 422 raised by the service layer on a real OOL decide must
    reach the owner as the uniform 409 page, not a raw 422 (#126).

    This pins the contract on the *handler* path (``owner_inbox_decide`` ->
    ``_failure``), not just on the helper, so a future refactor of the handler
    cannot silently leak a 422 again.
    """
    from aios.owner_inbox import OwnerInboxService
    from aios.services import ServiceError

    def _boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise ServiceError(422, "请选择客户阶段。")

    monkeypatch.setattr(OwnerInboxService, "decide", _boom)

    ctx = _context_token(client)
    token = _action_token(client, ctx)
    data = {"action_token": token, "ctx": ctx, "reason": "x"}
    response = client.post("/owner/inboxes/content/decide", auth=AUTH, data=data)
    assert_normalized_failure(response, seed=seed)


# --------------------------------------------------------------------------
# 1c. §8 audit-translation regression (issue #127 / D2 / #130)
# --------------------------------------------------------------------------
#
# The owner-facing audit history must never show raw gateway / scheduler /
# orchestrator / bootstrap identifiers (defect D2). The closed allow-lists in
# ``OwnerInboxService._translate_audit_actor`` / ``_translate_audit_action``
# translate every stored value to bounded business Chinese; anything outside
# the list collapses to a safe fallback rather than leaking. These tests pin
# that guarantee so a future change cannot re-open the enumeration oracle.


_INTERNAL_ACTORS = (
    "gateway",
    "scheduler",
    "orchestrator",
    "bootstrap",
    "system",
    "kubernetes",
    "unknown",
    "agent-registry",
)


def test_translate_audit_actor_collapses_internal_identifiers() -> None:
    """No internal component identifier reaches the owner as raw text (#127)."""
    from aios.owner_inbox import OwnerInboxService

    for raw in _INTERNAL_ACTORS:
        translated = OwnerInboxService._translate_audit_actor(raw)
        assert translated == "系统", f"{raw!r} leaked as {translated!r}"
        assert raw not in translated


def test_translate_audit_actor_owner_and_agent_stay_distinct() -> None:
    """Owner reads as '你', agents as 'AI 助手' -- never the raw id (#127)."""
    from aios.owner_inbox import OwnerInboxService

    assert OwnerInboxService._translate_audit_actor("owner") == "你"
    assert OwnerInboxService._translate_audit_actor("owner:abc123") == "你"
    assert OwnerInboxService._translate_audit_actor("agent") == "AI 助手"
    assert OwnerInboxService._translate_audit_actor("agent:hermes-9") == "AI 助手"


def test_translate_audit_action_falls_back_for_unknown() -> None:
    """Unknown actions collapse to the bounded fallback, not the raw dotted id (#127)."""
    from aios.owner_inbox import OwnerInboxService

    raw = "gateway.internal_dispatch_raw"
    translated = OwnerInboxService._translate_audit_action(raw)
    assert translated == "状态已更新"
    assert raw not in translated


def test_translate_audit_action_known_labels_preserved() -> None:
    """Known actions keep their business label (#127)."""
    from aios.owner_inbox import OwnerInboxService

    assert OwnerInboxService._translate_audit_action("content_draft.approve") == "内容已批准"
    assert OwnerInboxService._translate_audit_action("cs.outbound_send") == "外呼已发送"


def test_owner_surface_audit_history_translates_internal_identifiers(
    client: TestClient, seed: Seed
) -> None:
    """End-to-end: a raw internal audit row on the owner detail surface must
    render as the safe translation, never the raw identifier (#127)."""
    from aios.audit import AuditLog
    from aios.db import Session, get_database_url, get_engine

    with Session(get_engine(get_database_url())) as session:
        session.add(
            AuditLog(
                actor="gateway",
                action="gateway.internal_dispatch_raw",
                resource_type="artifact",
                resource_id=seed.draft_id,
                project_id=seed.project_id,
                idempotency_key="reg-audit-127-1",
            )
        )
        session.commit()

    ctx = _context_token(client)
    detail = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert detail.status_code == 200
    assert "gateway" not in detail.text
    assert "gateway.internal_dispatch_raw" not in detail.text
    assert "系统" in detail.text
    assert "状态已更新" in detail.text


# --------------------------------------------------------------------------
# 1d. §8 content decisions (issue #128)
# --------------------------------------------------------------------------
#
# ``_content_decisions`` maps an ``ArtifactReviewStatus`` to the owner-action
# purposes offered on the detail page. The ``UNVERIFIED`` branch (resubmit) is
# reachable when a row sits in UNVERIFIED: the service API may pre-set it at
# create time, or a partial failure (edit committed, submit failed) leaves it
# behind -- in both cases the owner sees a valid retry entry. The owner's daily
# happy path (edit-and-resubmit) never produces UNVERIFIED; it lands
# REVIEW_PASSED or NEEDS_REVISION. The branch is therefore intentional and
# required, and the regression below locks the mapping so a future edit cannot
# silently repurpose it (#128).


def test_content_decisions_owner_needs_revision_offers_edit_and_resubmit() -> None:
    """Owner daily flow: NEEDS_REVISION -> edit-and-resubmit, never resubmit (#128)."""
    from aios.models import ArtifactReviewStatus
    from aios.owner_inbox import (
        PURPOSE_CONTENT_EDIT_AND_RESUBMIT,
        OwnerInboxService,
    )

    assert OwnerInboxService._content_decisions(ArtifactReviewStatus.NEEDS_REVISION) == [
        PURPOSE_CONTENT_EDIT_AND_RESUBMIT
    ]


def test_content_decisions_review_passed_offers_approve_reject() -> None:
    """REVIEW_PASSED surfaces the approve / reject pair (#128)."""
    from aios.models import ArtifactReviewStatus
    from aios.owner_inbox import (
        PURPOSE_CONTENT_APPROVE,
        PURPOSE_CONTENT_REJECT,
        OwnerInboxService,
    )

    assert OwnerInboxService._content_decisions(ArtifactReviewStatus.REVIEW_PASSED) == [
        PURPOSE_CONTENT_APPROVE,
        PURPOSE_CONTENT_REJECT,
    ]


def test_content_decisions_unverified_is_service_only_resubmit() -> None:
    """UNVERIFIED -> resubmit: reachable via service pre-set or a partial
    failure (edit committed, submit failed) -- a valid retry entry, not dead
    UI; the owner daily happy path never produces it (#128)."""
    from aios.models import ArtifactReviewStatus
    from aios.owner_inbox import PURPOSE_CONTENT_RESUBMIT, OwnerInboxService

    assert OwnerInboxService._content_decisions(ArtifactReviewStatus.UNVERIFIED) == [
        PURPOSE_CONTENT_RESUBMIT
    ]


# --------------------------------------------------------------------------
# 1e. §6 pagination contract (issue #125)
# --------------------------------------------------------------------------
#
# Deterministic keyset pagination (never offset-over-a-mutating-set), stable
# total order with an explicit id tiebreaker, a server-side page-size cap,
# cursor semantics that survive concurrent inserts / state transitions without
# delivering a row twice or skipping it, and an opaque cursor that never
# exposes a raw internal id to the owner surface.

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50


def _seed_many_drafts(
    session: Session, project_id: str, count: int, *, prefix: str
) -> list[str]:
    """Create ``count`` pending content drafts; return their ids (created order)."""
    owner = ActorContext(kind="owner", owner_id=OWNER_ID)
    created: list[str] = []
    for i in range(count):
        draft = ContentDraftService(session).create_content_draft(
            project_id=project_id,
            actor=owner,
            topic=f"{prefix} {i}",
            body=f"正文 {i}",
            idempotency_key=f"idem-page-{prefix}-{i}",
        )
        created.append(draft.id)
    return created


def _ool_service(session: Session) -> OwnerInboxService:
    return OwnerInboxService(session)


def test_page_size_defaults_to_20(client: TestClient, seed: Seed) -> None:
    """No page_size -> deterministic default of 20, never unbounded (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 25, prefix="默认")
        page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT
        )
    assert len(page.items) == DEFAULT_PAGE_SIZE
    assert page.has_more is True
    assert page.next_token is not None
    assert page.page_size == DEFAULT_PAGE_SIZE


def test_page_size_capped_at_server_max(client: TestClient, seed: Seed) -> None:
    """Client-supplied sizes above the hard cap are clamped, never honoured (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 55, prefix="封顶")
        page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID),
            ctx,
            INBOX_CONTENT,
            page_size=10_000,
        )
    assert len(page.items) == MAX_PAGE_SIZE
    assert page.page_size == MAX_PAGE_SIZE
    assert page.has_more is True


def test_page_size_non_positive_falls_back_to_default(client: TestClient, seed: Seed) -> None:
    """0 / negative page sizes resolve deterministically to the default (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 5, prefix="回退")
        for bad in (0, -1, -42):
            page = _ool_service(session).list_inbox(
                ActorContext(kind="owner", owner_id=OWNER_ID),
                ctx,
                INBOX_CONTENT,
                page_size=bad,
            )
            # 1 seeded draft + 5 created = 6 pending rows, all served on one page.
            assert len(page.items) == 6
            assert page.page_size == len(page.items)


def test_pagination_walks_all_pages_no_dup_no_skip(client: TestClient, seed: Seed) -> None:
    """Keyset paging over a stable snapshot: every row exactly once, no repeats (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 25, prefix="遍历")
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = _ool_service(session).list_inbox(
                ActorContext(kind="owner", owner_id=OWNER_ID),
                ctx,
                INBOX_CONTENT,
                page_size=10,
                cursor_token=cursor,
            )
            seen.extend(item.detail_ref for item in page.items)
            pages += 1
            if not page.has_more or page.next_token is None:
                assert page.next_token is None
                break
            cursor = page.next_token
        assert pages == 3  # 10 + 10 + 6 (26 rows: 25 created + 1 seeded)
        assert len(seen) == 26
        assert len(set(seen)) == 26  # no duplicates


def test_tiebreaker_orders_same_timestamp_rows_stably(
    client: TestClient, seed: Seed
) -> None:
    """Equal timestamps never reorder: the id tiebreaker is a total order (#125).

    All rows share one ``created_at`` so the keyset cursor has nothing but the
    id to split page boundaries on. Walking pages of 2 must deliver every row
    exactly once in the same id order as a single unbounded read -- without the
    id tiebreaker the equal-ts slice could reorder or drop rows across pages.
    """
    ctx = _context_token(client)
    from aios.models import Artifact, ArtifactType

    fixed_ts = datetime(2026, 1, 2, 3, 4, 5)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 5, prefix="同刻")
        rows = session.exec(
            select(Artifact).where(
                Artifact.project_id == seed.project_id,
                Artifact.type == ArtifactType.CONTENT_DRAFT,
            )
        ).all()
        assert len(rows) == 6  # 1 seed draft + 5 created
        for row in rows:
            row.created_at = fixed_ts
            session.add(row)
        session.commit()

        # Reference order: single unbounded read, id tiebreaker ascending.
        whole = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=50
        )
        expected_refs = [item.detail_ref for item in whole.items]
        assert len(expected_refs) == 6

        # Walk pages of 2 across the equal-ts block.
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            page = _ool_service(session).list_inbox(
                ActorContext(kind="owner", owner_id=OWNER_ID),
                ctx,
                INBOX_CONTENT,
                page_size=2,
                cursor_token=cursor,
            )
            seen.extend(item.detail_ref for item in page.items)
            pages += 1
            if page.next_token is None:
                assert not page.has_more
                break
            cursor = page.next_token
    assert pages == 3  # 2 + 2 + 2
    assert len(seen) == 6
    assert len(set(seen)) == 6  # no dup, no skip
    # Stable across both walks: same order (id tiebreaker is the total order).
    assert seen == expected_refs


def test_cursor_token_never_exposes_internal_ids(client: TestClient, seed: Seed) -> None:
    """The next-page cursor is an opaque sealed token: no raw draft id in it (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        created = _seed_many_drafts(session, seed.project_id, 25, prefix="不泄")
        page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=10
        )
        cursor = page.next_token
    assert cursor is not None
    for draft_id in created:
        assert draft_id not in cursor
    assert seed.draft_id not in cursor


def test_cursor_from_one_inbox_rejected_on_another(client: TestClient, seed: Seed) -> None:
    """A content cursor replayed against the cs inbox is refused (untrusted) (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 25, prefix="跨箱")
        content_page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=10
        )
        service = _ool_service(session)
        with pytest.raises(Exception) as excinfo:
            service.list_inbox(
                ActorContext(kind="owner", owner_id=OWNER_ID),
                ctx,
                INBOX_CS,
                cursor_token=content_page.next_token,
            )
    assert "untrusted" in str(excinfo.value).lower() or "409" in str(excinfo.value)


def test_cursor_minted_for_another_project_is_rejected(
    client: TestClient, seed: Seed
) -> None:
    """A cursor sealed under project A cannot page project B: replaying it
    against B's context would silently skip B rows, so it must be refused
    with the same untrusted class (#125 audit P2-1)."""
    from aios.owner_inbox import PURPOSE_INBOX_NEXT, TOKEN_TYPE_INBOX_CURSOR

    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        other = Project(name="另一项目", objective="测试", status=ProjectStatus.ACTIVE)
        session.add(other)
        session.commit()
        session.refresh(other)
        other_id = other.id
        # A *validly shaped* cursor whose operating_project / resource_scope
        # point at the other project.
        foreign_cursor = _mint(
            token_type=TOKEN_TYPE_INBOX_CURSOR,
            owner=OWNER_ID,
            operating_project=other_id,
            resource_scope=other_id,
            inbox=INBOX_CONTENT,
            purpose=PURPOSE_INBOX_NEXT,
            sort_ts="2026-01-01T00:00:00",
            sort_id="art_other",
        )
        service = _ool_service(session)
        with pytest.raises(Exception) as excinfo:
            service.list_inbox(
                ActorContext(kind="owner", owner_id=OWNER_ID),
                ctx,
                INBOX_CONTENT,
                cursor_token=foreign_cursor,
            )
    assert "untrusted" in str(excinfo.value).lower() or "409" in str(excinfo.value)


def test_state_change_between_pages_never_delivers_twice(
    client: TestClient, seed: Seed, monkeypatch
) -> None:
    """A row processed between pages leaves the pending snapshot; it never
    reappears on a later page and is never delivered twice (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 15, prefix="状态")
        page1 = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=5
        )
        # Process the *last* pending artifact in total order (created_at desc,
        # id desc) -- it sorts after page 1, so it was not yet delivered.
        # Marking it APPROVED makes it leave the pending snapshot before the
        # remaining pages are read: it must never appear, and the walk must
        # deliver exactly the other 15 rows once. (Fixing a flake: previously
        # the row was picked by random UUID order, so ~1/3 of runs it already
        # sat on page 1 and the count drifted to 16.)
        from aios.models import Artifact, ArtifactReviewStatus, ArtifactType

        row = session.exec(
            select(Artifact)
            .where(
                Artifact.project_id == seed.project_id,
                Artifact.type == ArtifactType.CONTENT_DRAFT,
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        assert row is not None
        row.review_status = ArtifactReviewStatus.APPROVED
        session.add(row)
        session.commit()
        page2 = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID),
            ctx,
            INBOX_CONTENT,
            page_size=5,
            cursor_token=page1.next_token,
        )
        page3 = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID),
            ctx,
            INBOX_CONTENT,
            page_size=10,
            cursor_token=page2.next_token,
        )
    all_items = page1.items + page2.items + page3.items
    refs = [item.detail_ref for item in all_items]
    assert len(refs) == len(set(refs))  # no duplicate delivery
    # 1 seeded + 15 created = 16 pending; one processed away -> 15 delivered.
    assert len(all_items) == 15


def test_insert_between_pages_appears_on_later_pages_only(
    client: TestClient, seed: Seed
) -> None:
    """A row inserted after page 1 shows up on a later page -- never re-delivers
    rows already seen, never skips the new row (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 10, prefix="插入前")
        # 1 seeded + 10 created = 11 pending rows: page of 10 leaves one behind.
        page1 = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=10
        )
        assert len(page1.items) == 10
        assert page1.has_more is True
        assert page1.next_token is not None
        _seed_many_drafts(session, seed.project_id, 3, prefix="插入后")
        page2 = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=20
        )
    assert len(page2.items) == 14  # 11 old + 3 new, none duplicated
    refs = [item.detail_ref for item in page2.items]
    assert len(refs) == len(set(refs))


def test_last_page_has_no_next_token(client: TestClient, seed: Seed) -> None:
    """The final page carries has_more=False and next_token=None (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        # 1 seeded + 9 created = 10 rows: exactly one page of 10.
        _seed_many_drafts(session, seed.project_id, 9, prefix="末页")
        page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=10
        )
    assert len(page.items) == 10
    assert page.has_more is False
    assert page.next_token is None


def test_empty_inbox_has_no_next_token(client: TestClient, seed: Seed) -> None:
    """An empty pending list: zero items, no cursor, no more (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        # Remove the seeded draft from the pending set so the inbox is truly empty.
        from aios.models import Artifact, ArtifactReviewStatus, ArtifactType

        row = session.exec(
            select(Artifact).where(
                Artifact.project_id == seed.project_id,
                Artifact.type == ArtifactType.CONTENT_DRAFT,
            )
        ).first()
        assert row is not None
        row.review_status = ArtifactReviewStatus.APPROVED
        session.add(row)
        session.commit()
        page = _ool_service(session).list_inbox(
            ActorContext(kind="owner", owner_id=OWNER_ID), ctx, INBOX_CONTENT, page_size=10
        )
    assert page.items == []
    assert page.has_more is False
    assert page.next_token is None
    assert page.page_size == 0


def test_list_route_accepts_page_size_and_renders_next_link(
    client: TestClient, seed: Seed
) -> None:
    """The list route honours page_size and renders a next-page link (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 12, prefix="路由")
    listing = client.get(
        "/owner/inboxes/content",
        auth=AUTH,
        params={"ctx": ctx, "page_size": 5},
    )
    assert listing.status_code == 200, listing.text[:400]
    assert "下一页" in listing.text
    assert "本页 5 条" in listing.text


def test_list_route_cursor_walks_all_pages(client: TestClient, seed: Seed) -> None:
    """End-to-end: GET with cursor pages through every row without loss (#125)."""
    ctx = _context_token(client)
    with Session(get_engine(get_database_url())) as session:
        _seed_many_drafts(session, seed.project_id, 25, prefix="全路")
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"ctx": ctx, "page_size": 10}
        if cursor is not None:
            params["cursor"] = cursor
        listing = client.get("/owner/inboxes/content", auth=AUTH, params=params)
        assert listing.status_code == 200, listing.text[:400]
        refs = re.findall(r'<div class="ref">(内容#[0-9]+)</div>', listing.text)
        seen.extend(refs)
        match = re.search(r'href="/owner/inboxes/content\?ctx=[^"]*&cursor=([^"]+)"', listing.text)
        if match is None:
            break
        cursor = unquote(match.group(1))
    # 25 created + 1 seeded = 26 rows over 3 pages (10/10/6).
    assert len(seen) == 26
    assert len(set(seen)) == 26


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


# --------------------------------------------------------------------------
# 6. owner audit-history presentation (defect D2, Issue #118 / #130)
#
#    The owner audit history rendered on every inbox detail view must never
#    surface raw internal identifiers -- ``owner:owner-1``, ``agent:...``,
#    ``knowledge.fact.approved``, ``cs.outbound_send``, ``feedback.*`` and
#    similar gateway/audit strings. They are translated to bounded business
#    Chinese at the presentation layer only; the AuditLog *storage* is never
#    rewritten (no migration, no identity change, no weakened auditability).
#
#    The render path (OwnerInbox._audit_entries -> console escape) is identical
#    for every inbox kind, so the single closed allow-list is exercised here
#    across all four chains' action vocabularies plus the four actor classes.
# --------------------------------------------------------------------------

# Raw internal identifiers that must NEVER reach the owner surface. If any of
# these appears in a rendered detail page the audit history is leaking.
_AUDIT_LEAK_MARKERS = (
    "owner:owner-1",
    "owner:",
    "agent:",
    "agent:agent-7",
    "agent:agent-9",
    "agent:agent-3",
    "gateway",
    "distribution",
    "unknown-actor-xyz",
    "content_draft.",
    "content_draft.approve",
    "content_draft.reject",
    "knowledge.fact.",
    "knowledge.fact.approved",
    "knowledge.candidate.",
    "knowledge.candidate.rejected",
    "cs.outbound",
    "cs.outbound_send",
    "cs.escalation",
    "cs.lead_stage",
    "feedback.",
    "feedback.owner_approve",
    "feedback.stage_transition",
    "some.future.action",
)

# Bounded business Chinese that MUST be present after the fix (and is absent
# before it, which is what makes the test RED first).
_AUDIT_BUSINESS_LABELS = (
    "你",              # owner:* actor
    "AI 助手",         # agent:* actor
    "内容已批准",       # content_draft.approve
    "内容已驳回",       # content_draft.reject
    "知识事实已批准",     # knowledge.fact.approved
    "知识候选已驳回",     # knowledge.candidate.rejected
    "外呼已发送",       # cs.outbound_send
    "已升级处理",       # cs.escalation
    "反馈已审定",       # feedback.owner_approve
    "反馈阶段已流转",     # feedback.stage_transition
    "状态已更新",       # unknown action (fallback)
)


def _insert_audit(
    *,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str,
    project_id: str,
    seq: int,
) -> None:
    with Session(get_engine(get_database_url())) as session:
        append_audit(
            session,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            project_id=project_id,
            task_id=None,
            before={},
            after={},
            idempotency_key=f"idem-d2-audit-{seq}",
        )
        session.commit()


def _content_detail_page(client: TestClient, ctx: str):
    detail = client.post(
        "/owner/inboxes/content/detail",
        auth=AUTH,
        data={"detail_token": _detail_token(client, ctx), "ctx": ctx},
    )
    assert detail.status_code == 200, detail.text[:400]
    return detail


def _cs_detail_page(client: TestClient, ctx: str):
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
    return detail


def _assert_audit_presentation_clean(html: str, *, expect_labels: tuple[str, ...]) -> None:
    for marker in _AUDIT_LEAK_MARKERS:
        assert marker not in html, f"owner surface leaked raw audit id {marker!r}"
    for label in expect_labels:
        assert label in html, f"owner surface missing translated label {label!r}"


def test_owner_audit_history_never_leaks_raw_identifiers_across_chains(
    client: TestClient, seed: Seed
) -> None:
    """D2 RED-before-GREEN (Issue #130).

    The single content artifact detail view carries a representative audit trail
    spanning all four chains' action vocabularies (content / knowledge / cs /
    feedback) plus every actor class. Before the presentation fix the raw
    ``owner:`` / ``agent:`` / ``knowledge.fact.`` / ``cs.outbound_send`` /
    ``feedback.*`` strings render verbatim -> the leak assertions fail (RED).
    After the closed allow-list translation they are replaced by bounded
    business Chinese -> the test turns GREEN.
    """
    _insert_audit(
        actor="owner:owner-1", action="content_draft.approve",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=1,
    )
    _insert_audit(
        actor="agent:agent-7", action="knowledge.fact.approved",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=2,
    )
    _insert_audit(
        actor="system", action="cs.outbound_send",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=3,
    )
    _insert_audit(
        actor="gateway", action="feedback.owner_approve",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=4,
    )
    _insert_audit(
        actor="owner:owner-1", action="knowledge.candidate.rejected",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=5,
    )
    _insert_audit(
        actor="agent:agent-9", action="content_draft.reject",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=6,
    )
    _insert_audit(
        actor="distribution", action="cs.escalation",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=7,
    )
    _insert_audit(
        actor="owner:owner-1", action="feedback.stage_transition",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=8,
    )
    _insert_audit(
        actor="unknown-actor-xyz", action="some.future.action",
        resource_type="artifact", resource_id=seed.draft_id, project_id=seed.project_id, seq=9,
    )

    ctx = _context_token(client)
    detail = _content_detail_page(client, ctx)
    _assert_audit_presentation_clean(detail.text, expect_labels=_AUDIT_BUSINESS_LABELS)


def test_owner_audit_history_translates_cs_conversation_chain(
    client: TestClient, seed: Seed, cs_seed: CsSeed
) -> None:
    """D2 RED-before-GREEN for the ``conversation`` resource_type (CS chain).

    The CS detail view renders audit rows filtered by resource_type
    ``conversation``; this confirms the same closed allow-list is applied on the
    natural CS resource type, not only on the artifact page above.
    """
    _insert_audit(
        actor="owner:owner-1", action="cs.outbound_send",
        resource_type="conversation", resource_id=cs_seed.conversation_id,
        project_id=seed.project_id, seq=10,
    )
    _insert_audit(
        actor="agent:agent-3", action="cs.escalation",
        resource_type="conversation", resource_id=cs_seed.conversation_id,
        project_id=seed.project_id, seq=11,
    )
    _insert_audit(
        actor="system", action="cs.lead_stage",
        resource_type="conversation", resource_id=cs_seed.conversation_id,
        project_id=seed.project_id, seq=12,
    )

    ctx = _context_token(client)
    detail = _cs_detail_page(client, ctx)
    _assert_audit_presentation_clean(
        detail.text,
        expect_labels=("你", "AI 助手", "外呼已发送", "已升级处理", "线索阶段已更新"),
    )

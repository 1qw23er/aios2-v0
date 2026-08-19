"""AIOS Owner Operating Layer V0 (OOL V0) -- thin token/binding layer.

Implements the merged plan ``docs/superpowers/plans/2026-08-02-owner-operating-layer-v0.md``
(Issue #121 / PR #122). This module is deliberately **thin**: it never
re-implements CAS, transactions or audit. It only

1. seals / resolves the stateless ``OwnerSealedToken`` (AES-256-GCM, cleartext
   ``kid`` header as AAD -- plan §2.1),
2. builds the four owner inbox **read projections** (§6),
3. computes the canonical ``display_binding`` / ``facts_binding`` consent
   digests (§2.4 / §2.4.1),
4. adapts a business-label click into the correct existing service call with
   **server-bound** identities (§2.2 / §7).

Hard invariants enforced here (plan §1.3 / §2.1 / §4.1):

* **Zero new models, zero Alembic migration.** Only existing rows are read and
  existing services are called. This module adds no revision of its own and
  leaves whatever the current single Alembic head happens to be untouched.
* **The owner never relays an internal identity.** ``rid`` / ``project_id`` /
  ``series_id`` / ``version`` / ``checksum`` / ``revision`` / enum values travel
  only *inside* the AEAD ciphertext, never in a URL, HTML, JSON, log or referrer.
* **Four one-type-one-purpose token schemas** (``project_select`` /
  ``project_context`` / ``detail_view`` / ``inbox_action``) share one envelope
  and are mutually exclusive at the endpoint level -- there is no schema
  promotion (§2.1.2).
* **Stateless project context.** There is no mutable ``selected_project``
  session; the operating context is frozen inside each sealed token (§4.1).
* **``operating_project == "company"`` is forbidden in V0** and is rejected at
  mint and at every resolution phase (§2.1.2 V0 envelope invariant).
* **Fail-closed key handling.** A missing/invalid key configuration raises
  ``503 owner_token_key_unavailable``; there is never a default/empty key
  fallback (§2.1.1).

**Deliberate error normalization (§4.1 rule 6 + §8).** Every OOL-level
resolution failure -- malformed token, unknown ``kid``, AEAD auth failure,
expiry, schema confusion, wrong project, hidden row, missing row, stale
display-binding -- collapses to a *fixed* human-readable message with the
**same** HTTP status (409), so an attacker cannot use status/body/timing as an
enumeration oracle. Only the underlying services' own ``ServiceError`` codes
(403/409/422/502 ...) are translated into the richer §8 messages.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlmodel import Session, select

from aios.actor import ActorContext
from aios.audit import AuditLog
from aios.content_draft import ContentDraftService
from aios.customer_service import CustomerService, redact_pii
from aios.feedback import FeedbackService, FeedbackTransition
from aios.knowledge_service import KnowledgeService
from aios.knowledge_tags import CANONICAL_KNOWLEDGE_TAGS, is_legacy_unclassified
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Conversation,
    CsSuggestion,
    CsSuggestionDecision,
    KnowledgeCandidate,
    KnowledgeCandidateStatus,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
    LeadStage,
    Message,
    Project,
    ProjectStatus,
    now_utc,
)
from aios.services import ServiceError

# --------------------------------------------------------------------------
# 1. Constants, business labels, uniform messages
# --------------------------------------------------------------------------

ENVELOPE_VERSION = 1
ENVELOPE_ALG = "A256GCM"
NONCE_BYTES = 12
KEY_BYTES = 32
CLOCK_SKEW_SECONDS = 30
DEFAULT_TTL_SECONDS = 900

COMPANY_SCOPE = "company"

TOKEN_TYPE_PROJECT_SELECT = "project_select"
TOKEN_TYPE_PROJECT_CONTEXT = "project_context"
TOKEN_TYPE_DETAIL_VIEW = "detail_view"
TOKEN_TYPE_INBOX_ACTION = "inbox_action"
TOKEN_TYPE_INBOX_CURSOR = "inbox_cursor"

TOKEN_TYPES: frozenset[str] = frozenset(
    {
        TOKEN_TYPE_PROJECT_SELECT,
        TOKEN_TYPE_PROJECT_CONTEXT,
        TOKEN_TYPE_DETAIL_VIEW,
        TOKEN_TYPE_INBOX_ACTION,
        TOKEN_TYPE_INBOX_CURSOR,
    }
)

PURPOSE_PROJECT_SELECT = "project_select"
PURPOSE_PROJECT_INBOX = "project_inbox"
PURPOSE_VIEW_DETAIL = "view_detail"
PURPOSE_INBOX_NEXT = "inbox_next"

# §6 pagination contract (#125): deterministic keyset paging. A page request
# without an explicit size is served at the default; anything above the hard
# cap is clamped server-side -- an unbounded page is never honoured.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 50

# Endpoint identities are PATHS ONLY; the HTTP method is bound separately in
# ``SchemaSpec.method`` so that PHASE 3 checks both independently.
ENDPOINT_PROJECT_PICK = "/owner/project-pick"
ENDPOINT_INBOX_LIST = "/owner/inboxes/{kind}"
ENDPOINT_INBOX_DETAIL = "/owner/inboxes/{kind}/detail"
ENDPOINT_INBOX_DECIDE = "/owner/inboxes/{kind}/decide"

INBOX_CONTENT = "content"
INBOX_CS = "cs"
INBOX_FEEDBACK = "feedback"
INBOX_KNOWLEDGE = "knowledge"

# Allowlisted URL ``{kind}`` path segment (§2.1.2b step 11).
INBOX_KINDS: frozenset[str] = frozenset({INBOX_CONTENT, INBOX_CS, INBOX_FEEDBACK, INBOX_KNOWLEDGE})

INBOX_TITLES: dict[str, str] = {
    INBOX_CONTENT: "内容决策",
    INBOX_CS: "客服决策",
    INBOX_FEEDBACK: "反馈决策",
    INBOX_KNOWLEDGE: "知识决策",
}

# Resource kinds valid inside each inbox's tokens (token claim ``kind``).
INBOX_RESOURCE_KINDS: dict[str, frozenset[str]] = {
    INBOX_CONTENT: frozenset({"artifact"}),
    INBOX_CS: frozenset({"conversation", "suggestion"}),
    INBOX_FEEDBACK: frozenset({"feedback"}),
    INBOX_KNOWLEDGE: frozenset({"candidate", "fact"}),
}

# Exact action purposes (§2.1 normative table / §2.4 binding headings).
PURPOSE_CONTENT_APPROVE = "content.approve"
PURPOSE_CONTENT_REJECT = "content.reject"
PURPOSE_CONTENT_RESUBMIT = "content.resubmit"
PURPOSE_CONTENT_EDIT_AND_RESUBMIT = "content.edit_and_resubmit"
PURPOSE_CS_ADOPT_AND_SEND = "cs.adopt_and_send"
PURPOSE_CS_ESCALATE = "cs.escalate"
PURPOSE_CS_SET_LEAD_STAGE = "cs.set_lead_stage"
PURPOSE_CS_ASSIGN_HUMAN = "cs.assign_human"
PURPOSE_CS_CREATE_FOLLOWUP = "cs.create_followup"
PURPOSE_FEEDBACK_APPROVE_SOLUTION = "feedback.approve_solution"
PURPOSE_FEEDBACK_REJECT_SOLUTION = "feedback.reject_solution"
PURPOSE_FEEDBACK_DEFER = "feedback.defer"
PURPOSE_FEEDBACK_MARK_DUPLICATE = "feedback.mark_duplicate"
PURPOSE_FEEDBACK_REJECT_FEEDBACK = "feedback.reject_feedback"
PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE = "knowledge.approve_candidate"
PURPOSE_KNOWLEDGE_REJECT_CANDIDATE = "knowledge.reject_candidate"
PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE = "knowledge.classify_candidate"
PURPOSE_KNOWLEDGE_DEACTIVATE_FACT = "knowledge.deactivate_fact"

ACTION_PURPOSES: frozenset[str] = frozenset(
    {
        PURPOSE_CONTENT_APPROVE,
        PURPOSE_CONTENT_REJECT,
        PURPOSE_CONTENT_RESUBMIT,
        PURPOSE_CONTENT_EDIT_AND_RESUBMIT,
        PURPOSE_CS_ADOPT_AND_SEND,
        PURPOSE_CS_ESCALATE,
        PURPOSE_CS_SET_LEAD_STAGE,
        PURPOSE_CS_ASSIGN_HUMAN,
        PURPOSE_CS_CREATE_FOLLOWUP,
        PURPOSE_FEEDBACK_APPROVE_SOLUTION,
        PURPOSE_FEEDBACK_REJECT_SOLUTION,
        PURPOSE_FEEDBACK_DEFER,
        PURPOSE_FEEDBACK_MARK_DUPLICATE,
        PURPOSE_FEEDBACK_REJECT_FEEDBACK,
        PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE,
        PURPOSE_KNOWLEDGE_REJECT_CANDIDATE,
        PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE,
        PURPOSE_KNOWLEDGE_DEACTIVATE_FACT,
    }
)

ACTION_LABELS: dict[str, str] = {
    PURPOSE_CONTENT_APPROVE: "批准",
    PURPOSE_CONTENT_REJECT: "驳回（填理由）",
    PURPOSE_CONTENT_RESUBMIT: "重新送审",
    PURPOSE_CONTENT_EDIT_AND_RESUBMIT: "编辑并重新送审",
    PURPOSE_CS_ADOPT_AND_SEND: "采用并发送",
    PURPOSE_CS_ESCALATE: "转人工",
    PURPOSE_CS_SET_LEAD_STAGE: "标记阶段",
    PURPOSE_CS_ASSIGN_HUMAN: "分配负责人",
    PURPOSE_CS_CREATE_FOLLOWUP: "建跟进任务",
    PURPOSE_FEEDBACK_APPROVE_SOLUTION: "批准方案",
    PURPOSE_FEEDBACK_REJECT_SOLUTION: "驳回方案",
    PURPOSE_FEEDBACK_DEFER: "暂缓",
    PURPOSE_FEEDBACK_MARK_DUPLICATE: "标记重复",
    PURPOSE_FEEDBACK_REJECT_FEEDBACK: "拒绝该反馈",
    PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE: "审定通过（选系列）",
    PURPOSE_KNOWLEDGE_REJECT_CANDIDATE: "驳回",
    PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE: "先分类",
    PURPOSE_KNOWLEDGE_DEACTIVATE_FACT: "停用事实（填理由）",
}

# Owner input required by each action (server renders the matching control).
INPUT_NONE = "none"
INPUT_REASON = "reason"
INPUT_BODY = "body"
INPUT_TEXT = "text"
INPUT_TAGS = "tags"
INPUT_STAGE = "stage"
INPUT_CANONICAL = "canonical"
INPUT_TITLE = "title"
INPUT_SERIES = "series"

ACTION_INPUTS: dict[str, str] = {
    PURPOSE_CONTENT_APPROVE: INPUT_NONE,
    PURPOSE_CONTENT_REJECT: INPUT_REASON,
    PURPOSE_CONTENT_RESUBMIT: INPUT_NONE,
    PURPOSE_CONTENT_EDIT_AND_RESUBMIT: INPUT_BODY,
    PURPOSE_CS_ADOPT_AND_SEND: INPUT_TEXT,
    PURPOSE_CS_ESCALATE: INPUT_NONE,
    PURPOSE_CS_SET_LEAD_STAGE: INPUT_STAGE,
    PURPOSE_CS_ASSIGN_HUMAN: INPUT_NONE,
    PURPOSE_CS_CREATE_FOLLOWUP: INPUT_TITLE,
    PURPOSE_FEEDBACK_APPROVE_SOLUTION: INPUT_NONE,
    PURPOSE_FEEDBACK_REJECT_SOLUTION: INPUT_REASON,
    PURPOSE_FEEDBACK_DEFER: INPUT_REASON,
    PURPOSE_FEEDBACK_MARK_DUPLICATE: INPUT_CANONICAL,
    PURPOSE_FEEDBACK_REJECT_FEEDBACK: INPUT_REASON,
    PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE: INPUT_SERIES,
    PURPOSE_KNOWLEDGE_REJECT_CANDIDATE: INPUT_REASON,
    PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE: INPUT_TAGS,
    PURPOSE_KNOWLEDGE_DEACTIVATE_FACT: INPUT_REASON,
}

# §6.5 business-language view model (owner never sees a raw enum / tag string).
LEAD_STAGE_LABELS: dict[str, str] = {
    LeadStage.VISITOR.value: "访客",
    LeadStage.LEAD.value: "线索",
    LeadStage.QUALIFIED.value: "合格",
    LeadStage.PROPOSAL.value: "方案",
    LeadStage.WON.value: "成交",
}
LEAD_STAGE_BY_LABEL: dict[str, LeadStage] = {
    label: LeadStage(value) for value, label in LEAD_STAGE_LABELS.items()
}

CANONICAL_TAG_LABELS: dict[str, str] = {
    "user_research": "用户调研",
    "positioning": "定位策略",
    "wechat_writing": "公众号写作",
    "xhs_adaptation": "小红书改写",
    "video_script": "视频脚本",
    "packaging": "内容包装",
    "knowledge_capture": "知识沉淀",
}
CANONICAL_TAG_BY_LABEL: dict[str, str] = {
    label: tag for tag, label in CANONICAL_TAG_LABELS.items()
}

RISK_TAG_LABELS: dict[str, str] = {
    "privacy": "涉及隐私",
    "needs_human_review": "需人工复核",
    "security": "涉及安全",
    "compliance": "涉及合规",
    "data_loss": "可能丢数据",
    "billing": "涉及计费",
}

CONTENT_STATUS_LABELS: dict[str, str] = {
    ArtifactReviewStatus.REVIEW_PASSED.value: "独立复审已通过，待你批准",
    ArtifactReviewStatus.UNVERIFIED.value: "编辑已保存，待重新送审",
    ArtifactReviewStatus.NEEDS_REVISION.value: "复审未通过，待你重新编辑",
}

CONTENT_PENDING_STATUSES: tuple[ArtifactReviewStatus, ...] = (
    ArtifactReviewStatus.REVIEW_PASSED,
    ArtifactReviewStatus.UNVERIFIED,
    ArtifactReviewStatus.NEEDS_REVISION,
)

# A Project the owner may currently operate. COMPLETED / CANCELLED are terminal
# and therefore NOT live (§2.1.2a step 5 "missing / deleted / non-live").
LIVE_PROJECT_STATUSES: frozenset[ProjectStatus] = frozenset(
    {ProjectStatus.PROPOSED, ProjectStatus.ACTIVE, ProjectStatus.BLOCKED}
)

FEEDBACK_AWAIT_OWNER_APPROVE = "AWAIT_OWNER_APPROVE"

PREVIEW_CHARS = 120

# Uniform owner-facing messages (§8). All OOL-level failures use HTTP 409 so
# 403 / 404 / 409 are indistinguishable from the outside (§4.1 rule 6).
#
# ``MSG_TOKEN_INVALID`` is the single sentence for the whole "this navigation /
# action input can no longer be trusted" class: malformed, expired, wrong
# ``token_type``, wrong purpose-endpoint pairing, undecryptable, unknown inbox
# kind, unresolvable ``rid``, row outside the operating project, drifted
# ``resource_scope``, stale ``display_binding`` -- and, at the HTTP boundary,
# *absent*. It deliberately reads as "失效或不完整" so that a missing request
# parameter and a tampered token produce the same wording; any per-cause
# phrasing would re-introduce the enumeration signal §4.1 rule 6 removes.
# See :func:`_untrusted` for the exhaustive list and the timing analysis.
MSG_TOKEN_INVALID = "当前页面信息已失效或不完整，请返回后重新打开最新页面。"
MSG_ACTION_UNAVAILABLE = "该操作已不可用，请刷新收件箱。"
MSG_STALE = "该条目已变更，请刷新收件箱。"
MSG_MISSING = "该条目已不存在或已处理，请刷新收件箱。"
MSG_NEED_REASON = "请填写处理理由。"
MSG_CONTENT_PARTIAL = "编辑已保存，但重新送审未成功，请重试送审。"
MSG_KEY_UNAVAILABLE = "owner_token_key_unavailable"

UNIFORM_STATUS = 409


def _untrusted() -> ServiceError:
    """The **only** failure raised while a request's own input is still being judged.

    Every step whose outcome is decided by comparing client-supplied material
    (the sealed token, the URL inbox kind) against server state raises *this*
    error and nothing else: malformed / undecryptable / expired / wrong
    ``token_type`` / wrong purpose / wrong endpoint-method / forbidden or extra
    claim / unknown inbox kind / unresolvable ``rid`` / row outside the
    operating project / drifted ``resource_scope`` / stale ``display_binding``.

    Because ``untrusted=True``, the HTTP boundary renders all of them with one
    byte-identical 409 page (``owner_inbox_routes._untrusted_input``). An
    attacker therefore cannot tell "this token is expired" from "this row does
    not exist" from "this row belongs to another project" -- which is the whole
    point of §4.1 rule 6 / §8 (no enumeration oracle).

    Timing note: every branch that consults *server state* (row lookup, scope
    and binding comparison) performs the same DB read before deciding, and all
    binding comparisons use :func:`secrets.compare_digest`, so no server-held
    value is recoverable by timing. The one remaining latency difference -- a
    structurally malformed token short-circuits before AES-GCM -- discloses
    only a property of the request the caller themselves supplied, so it
    carries no information about the server.
    """
    return ServiceError(UNIFORM_STATUS, MSG_TOKEN_INVALID, untrusted=True)


def _action_unavailable() -> ServiceError:
    """A *resolved* request whose purpose has no valid handler (defensive only)."""
    return ServiceError(UNIFORM_STATUS, MSG_ACTION_UNAVAILABLE)


def _stale() -> ServiceError:
    """A *resolved* row whose domain state no longer supports the decision."""
    return ServiceError(UNIFORM_STATUS, MSG_STALE)


def _key_unavailable() -> ServiceError:
    return ServiceError(503, MSG_KEY_UNAVAILABLE)


# --------------------------------------------------------------------------
# 2. Canonical serialization + binding digests (§2.4 / §2.4.1)
# --------------------------------------------------------------------------


def canonical_json(payload: Any) -> str:
    """The one canonical JSON form used across AIOS (feedback.py / customer_service.py)."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(binding: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(binding)).encode("utf-8")).hexdigest()


def _sha256_prefixed(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_facts_binding(
    knowledge_fact_refs: Sequence[str], fact_revisions: Mapping[str, int]
) -> str:
    """Canonical CS fact-provenance digest -- implemented verbatim per plan §2.4.1.

    Order-independent (``sorted`` refs), compact separators, ``ensure_ascii=False``,
    UTF-8 bytes, ``sha256:`` prefix. Empty ref set yields the fixed constant
    ``sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a``.
    """
    canonical_facts = json.dumps(
        {ref: fact_revisions[ref] for ref in sorted(knowledge_fact_refs)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical_facts).hexdigest()


def project_display_binding(project: Project) -> str:
    """§2.4 project-label binding: ``{project_ref, business_label, updated_at}`` -> sha256."""
    return _digest(
        {
            "project_ref": project.id,
            "business_label": project.name,
            "updated_at": project.updated_at.isoformat(),
        }
    )


def content_display_binding(artifact: Artifact) -> str:
    """§2.4 CONTENT binding: review_status + reviewed_revision + reviewed_checksum."""
    metadata = dict(artifact.metadata_json or {})
    review = dict(metadata.get("independent_review") or {})
    return _digest(
        {
            "kind": "artifact",
            "rid": artifact.id,
            "review_status": str(artifact.review_status),
            "reviewed_revision": str(review.get("reviewed_revision", "")),
            "reviewed_checksum": str(review.get("reviewed_checksum", "")),
        }
    )


def cs_suggestion_display_binding(suggestion: CsSuggestion, conv_suggestion_count: int) -> str:
    """§2.4 CS ``cs.adopt_and_send`` binding (uses only persisted authoritative data)."""
    return _digest(
        {
            "kind": "suggestion",
            "rid": suggestion.id,
            "decision": str(suggestion.decision),
            "text_sha256": _sha256_prefixed(suggestion.text),
            "facts_binding": canonical_facts_binding(
                list(suggestion.knowledge_fact_refs or []),
                dict(suggestion.fact_revisions or {}),
            ),
            "consumed": bool(suggestion.consumed),
            "conv_suggestion_count": str(conv_suggestion_count),
        }
    )


def cs_conversation_display_binding(conversation: Conversation, suggestion_count: int) -> str:
    """§2.4 CS conversation-level binding (escalate / lead stage / assign / followup)."""
    return _digest(
        {
            "kind": "conversation",
            "rid": conversation.id,
            "lead_stage": str(conversation.lead_stage),
            "suggestion_count": str(suggestion_count),
        }
    )


def feedback_display_binding(artifact: Artifact) -> str:
    """§2.4 FEEDBACK binding: stage + revision_count + checksum."""
    metadata = dict(artifact.metadata_json or {})
    return _digest(
        {
            "kind": "feedback",
            "rid": artifact.id,
            "stage": str(metadata.get("stage", "")),
            "revision": str(artifact.revision_count),
            "checksum": artifact.checksum,
        }
    )


def knowledge_candidate_display_binding(candidate: KnowledgeCandidate, head_version: int) -> str:
    """§2.4 KNOWLEDGE candidate binding (freezes the head version an approval supersedes).

    ``KnowledgeCandidate`` carries no ``series_id`` column, so the binding's
    ``series_id`` is the empty string per plan (``<candidate.series_id or ''>``)
    and ``head_version`` is the head of that (empty) series -- deterministic at
    both render and decision time.
    """
    return _digest(
        {
            "kind": "candidate",
            "rid": candidate.id,
            "status": str(candidate.status),
            "statement_sha256": _sha256_prefixed(candidate.statement),
            "series_id": str(getattr(candidate, "series_id", "") or ""),
            "head_version": str(head_version),
        }
    )


def knowledge_fact_display_binding(fact: KnowledgeFact) -> str:
    """§2.4 KNOWLEDGE fact binding (deactivation consent)."""
    return _digest(
        {
            "kind": "fact",
            "rid": fact.id,
            "status": str(fact.status),
            "version": str(fact.version),
            "statement_sha256": _sha256_prefixed(fact.statement),
            "series_id": fact.series_id,
        }
    )


# --------------------------------------------------------------------------
# 3. Key ring -- fail-closed loading / rotation (§2.1.1)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenKey:
    kid: str
    key: bytes


@dataclass(frozen=True)
class TokenKeyRing:
    current: TokenKey
    previous: TokenKey | None = None
    previous_accept_until: datetime | None = None
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    def select(self, kid: str, *, now: datetime) -> bytes:
        """Return the AES key for ``kid``; unknown / expired-window kid fails closed."""
        if secrets.compare_digest(kid, self.current.kid):
            return self.current.key
        previous = self.previous
        if previous is not None and secrets.compare_digest(kid, previous.kid):
            deadline = self.previous_accept_until
            if deadline is not None and now <= deadline:
                return previous.key
        raise _untrusted()


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(value: str) -> bytes:
    if not value or any(ch not in _B64U_ALPHABET for ch in value):
        raise _untrusted()
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, base64.binascii.Error) as exc:  # pragma: no cover - defensive
        raise _untrusted() from exc


_B64U_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _decode_key(raw: str | None) -> bytes:
    """Decode one configured token key: **unpadded** base64url of exactly 32 bytes.

    The encoding is part of the operator-facing contract (§2.1.1), so it is
    enforced rather than repaired: a value carrying ``=`` padding is rejected
    outright instead of being silently stripped. Accepting both spellings would
    mean the same key material has two valid representations, which makes key
    rotation and cross-environment comparison ambiguous. 32 raw bytes encode to
    exactly 43 unpadded base64url characters, so the contract has one spelling.
    """
    if not raw:
        raise _key_unavailable()
    candidate = raw.strip()
    if any(ch not in _B64U_ALPHABET for ch in candidate):
        raise _key_unavailable()
    if len(candidate) % 4 == 1:
        raise _key_unavailable()
    try:
        key = base64.urlsafe_b64decode((candidate + "=" * (-len(candidate) % 4)).encode("ascii"))
    except (ValueError, base64.binascii.Error) as exc:  # pragma: no cover - defensive
        raise _key_unavailable() from exc
    if len(key) != KEY_BYTES:
        raise _key_unavailable()
    return key


def _parse_utc_deadline(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _key_unavailable() from exc
    if parsed.tzinfo is None:
        raise _key_unavailable()
    return parsed.astimezone(UTC)


def load_token_key_ring(env: Mapping[str, str] | None = None) -> TokenKeyRing:
    """Load the OOL token key ring, fail-closed (§2.1.1).

    Any missing / malformed / non-32-byte / colliding-kid / unparseable-deadline
    configuration raises ``503 owner_token_key_unavailable``. There is no
    hardcoded, empty or default key fallback.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    current_kid = (source.get("AIOS_OOL_TOKEN_CURRENT_KID") or "").strip()
    if not current_kid:
        raise _key_unavailable()
    current_key = _decode_key(source.get("AIOS_OOL_TOKEN_CURRENT_KEY_B64"))

    previous: TokenKey | None = None
    accept_until: datetime | None = None
    previous_kid = (source.get("AIOS_OOL_TOKEN_PREVIOUS_KID") or "").strip()
    if previous_kid:
        if previous_kid == current_kid:
            raise _key_unavailable()
        previous = TokenKey(kid=previous_kid, key=_decode_key(
            source.get("AIOS_OOL_TOKEN_PREVIOUS_KEY_B64")
        ))
        raw_deadline = (source.get("AIOS_OOL_TOKEN_PREVIOUS_ACCEPT_UNTIL") or "").strip()
        if not raw_deadline:
            raise _key_unavailable()
        accept_until = _parse_utc_deadline(raw_deadline)

    ttl_raw = (source.get("AIOS_OOL_TOKEN_TTL_SECONDS") or "").strip()
    ttl_seconds = DEFAULT_TTL_SECONDS
    if ttl_raw:
        try:
            ttl_seconds = int(ttl_raw)
        except ValueError as exc:
            raise _key_unavailable() from exc
        if ttl_seconds <= 0:
            raise _key_unavailable()

    return TokenKeyRing(
        current=TokenKey(kid=current_kid, key=current_key),
        previous=previous,
        previous_accept_until=accept_until,
        ttl_seconds=ttl_seconds,
    )


# --------------------------------------------------------------------------
# 4. OwnerSealedToken -- seal + three-phase resolve (§2.1 / §2.1.2)
# --------------------------------------------------------------------------

COMMON_CLAIMS: frozenset[str] = frozenset({"v", "token_type", "owner", "iat", "exp"})


@dataclass(frozen=True)
class SchemaSpec:
    required: frozenset[str]
    forbidden: frozenset[str]
    purposes: frozenset[str]
    endpoint: str
    method: str


_NAV_REQUIRED = frozenset(
    {"project_ref", "operating_project", "project_display_binding", "purpose"}
)
_NAV_FORBIDDEN = frozenset({"inbox", "kind", "rid", "resource_scope", "display_binding"})
_RESOURCE_REQUIRED = frozenset(
    {"operating_project", "resource_scope", "inbox", "kind", "rid", "purpose", "display_binding"}
)
_RESOURCE_FORBIDDEN = frozenset({"project_ref", "project_display_binding"})
# Cursor claims pin the inbox and the last row's sort key. ``kind`` is
# explicitly forbidden: a cursor's meaning is defined by (inbox, sort key)
# alone, and a kind claim would invite cross-entity confusion (#125).
_CURSOR_REQUIRED = frozenset(
    {"operating_project", "resource_scope", "inbox", "purpose", "sort_ts", "sort_id"}
)
_CURSOR_FORBIDDEN = frozenset(
    {"project_ref", "project_display_binding", "rid", "display_binding", "kind"}
)

SCHEMAS: dict[str, SchemaSpec] = {
    TOKEN_TYPE_PROJECT_SELECT: SchemaSpec(
        required=_NAV_REQUIRED,
        forbidden=_NAV_FORBIDDEN,
        purposes=frozenset({PURPOSE_PROJECT_SELECT}),
        endpoint=ENDPOINT_PROJECT_PICK,
        method="POST",
    ),
    TOKEN_TYPE_PROJECT_CONTEXT: SchemaSpec(
        required=_NAV_REQUIRED,
        forbidden=_NAV_FORBIDDEN,
        purposes=frozenset({PURPOSE_PROJECT_INBOX}),
        endpoint=ENDPOINT_INBOX_LIST,
        method="GET",
    ),
    TOKEN_TYPE_DETAIL_VIEW: SchemaSpec(
        required=_RESOURCE_REQUIRED,
        forbidden=_RESOURCE_FORBIDDEN,
        purposes=frozenset({PURPOSE_VIEW_DETAIL}),
        endpoint=ENDPOINT_INBOX_DETAIL,
        method="POST",
    ),
    TOKEN_TYPE_INBOX_ACTION: SchemaSpec(
        required=_RESOURCE_REQUIRED,
        forbidden=_RESOURCE_FORBIDDEN,
        purposes=ACTION_PURPOSES,
        endpoint=ENDPOINT_INBOX_DECIDE,
        method="POST",
    ),
    TOKEN_TYPE_INBOX_CURSOR: SchemaSpec(
        required=_CURSOR_REQUIRED,
        forbidden=_CURSOR_FORBIDDEN,
        purposes=frozenset({PURPOSE_INBOX_NEXT}),
        endpoint=ENDPOINT_INBOX_LIST,
        method="GET",
    ),
}


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def seal_token(
    claims: Mapping[str, Any],
    *,
    ring: TokenKeyRing,
    now: datetime | None = None,
) -> str:
    """Seal an ``OwnerSealedToken`` (AES-256-GCM, cleartext ``kid`` header as AAD).

    Mint-time invariants (§2.1.2): the schema must be one of the four whitelisted
    types, carry exactly its allowed claim set, use an allowed purpose, and MUST
    NOT declare ``operating_project == "company"`` (forbidden in V0).
    """
    moment = now or now_utc()
    payload: dict[str, Any] = dict(claims)
    token_type = payload.get("token_type")
    if token_type not in TOKEN_TYPES:
        raise _untrusted()
    spec = SCHEMAS[str(token_type)]

    payload.setdefault("v", ENVELOPE_VERSION)
    payload.setdefault("iat", int(moment.timestamp()))
    payload.setdefault("exp", int(moment.timestamp()) + ring.ttl_seconds)

    if payload.get("operating_project") == COMPANY_SCOPE:
        raise _untrusted()
    if payload.get("purpose") not in spec.purposes:
        raise _untrusted()
    allowed = COMMON_CLAIMS | spec.required
    if set(payload) != allowed:
        raise _untrusted()

    header = canonical_json({"v": ENVELOPE_VERSION, "kid": ring.current.kid, "alg": ENVELOPE_ALG})
    header_bytes = header.encode("utf-8")
    plaintext = json.dumps(
        payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = AESGCM(ring.current.key).encrypt(nonce, plaintext, header_bytes)
    return ".".join([_b64u_encode(header_bytes), _b64u_encode(nonce), _b64u_encode(ciphertext)])


def _phase1_open_envelope(
    token: str, *, actor: ActorContext, ring: TokenKeyRing, now: datetime
) -> dict[str, Any]:
    """PHASE 1 -- envelope + common-field validation only (no schema-specific field)."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise _untrusted()
    header_b64, nonce_b64, ct_b64 = token.split(".")
    header_bytes = _b64u_decode(header_b64)
    nonce = _b64u_decode(nonce_b64)
    ciphertext = _b64u_decode(ct_b64)
    if len(nonce) != NONCE_BYTES:
        raise _untrusted()

    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _untrusted() from exc
    if not isinstance(header, dict) or set(header) != {"v", "kid", "alg"}:
        raise _untrusted()
    if header.get("v") != ENVELOPE_VERSION or header.get("alg") != ENVELOPE_ALG:
        raise _untrusted()
    kid = header.get("kid")
    if not _is_str(kid):
        raise _untrusted()

    key = ring.select(str(kid), now=now)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, header_bytes)
    except InvalidTag as exc:
        raise _untrusted() from exc

    try:
        claims = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _untrusted() from exc
    if not isinstance(claims, dict):
        raise _untrusted()

    if claims.get("v") != ENVELOPE_VERSION:
        raise _untrusted()
    if not _is_str(claims.get("token_type")) or not _is_str(claims.get("owner")):
        raise _untrusted()
    if not _is_int(claims.get("iat")) or not _is_int(claims.get("exp")):
        raise _untrusted()

    epoch = int(now.timestamp())
    if epoch + CLOCK_SKEW_SECONDS < int(claims["iat"]):
        raise _untrusted()
    if epoch - CLOCK_SKEW_SECONDS > int(claims["exp"]):
        raise _untrusted()

    owner_id = actor.owner_id or ""
    if actor.kind != "owner" or not secrets.compare_digest(owner_id, str(claims["owner"])):
        raise _untrusted()

    # V0 envelope invariant: no company operating context, at any phase.
    if claims.get("operating_project") == COMPANY_SCOPE:
        raise _untrusted()
    return claims


def resolve_sealed_token(
    token: str,
    *,
    actor: ActorContext,
    endpoint: str,
    method: str,
    ring: TokenKeyRing,
    inbox: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Resolve an ``OwnerSealedToken`` through PHASE 1 -> PHASE 2 -> PHASE 3 (§2.1.2).

    Returns the validated claims. Performs **no** database access -- the live-row
    load, scope resolution and display-binding comparison belong to the
    schema resolvers in :class:`OwnerInboxService`.
    """
    moment = now or now_utc()
    claims = _phase1_open_envelope(token, actor=actor, ring=ring, now=moment)

    # PHASE 2 -- whitelist dispatch; an unknown token_type never reaches a schema.
    token_type = str(claims["token_type"])
    if token_type not in TOKEN_TYPES:
        raise _untrusted()
    spec = SCHEMAS[token_type]

    # PHASE 3 -- schema-specific REQUIRED / FORBIDDEN / purpose / endpoint / method.
    if endpoint != spec.endpoint or method != spec.method:
        raise _untrusted()
    present = set(claims)
    if not spec.required.issubset(present):
        raise _untrusted()
    if present & spec.forbidden:
        raise _untrusted()
    # Extra claims are never silently ignored.
    if present - (COMMON_CLAIMS | spec.required):
        raise _untrusted()

    purpose = claims.get("purpose")
    if purpose not in spec.purposes:
        raise _untrusted()

    if token_type in {TOKEN_TYPE_PROJECT_SELECT, TOKEN_TYPE_PROJECT_CONTEXT}:
        if not _is_str(claims.get("project_ref")):
            raise _untrusted()
        if not _is_str(claims.get("operating_project")):
            raise _untrusted()
        if not _is_str(claims.get("project_display_binding")):
            raise _untrusted()
        return claims

    claim_inbox = claims.get("inbox")
    claim_kind = claims.get("kind")
    if claim_inbox not in INBOX_KINDS:
        raise _untrusted()

    # Cursor claims carry no resource row (no rid / kind / binding); their
    # identity is (inbox + sort key). ``sort_ts`` must be an ISO-8601
    # timestamp; ``sort_id`` the opaque row id of the last item on the page.
    if token_type == TOKEN_TYPE_INBOX_CURSOR:
        if inbox is not None and claim_inbox != inbox:
            raise _untrusted()
        if not _is_str(claims.get("sort_ts")) or not _is_str(claims.get("sort_id")):
            raise _untrusted()
        if not _is_str(claims.get("operating_project")) or not _is_str(
            claims.get("resource_scope")
        ):
            raise _untrusted()
        return claims

    if claim_kind not in INBOX_RESOURCE_KINDS[str(claim_inbox)]:
        raise _untrusted()
    if inbox is not None and claim_inbox != inbox:
        raise _untrusted()
    if not _is_str(claims.get("rid")) or not _is_str(claims.get("display_binding")):
        raise _untrusted()
    if not _is_str(claims.get("operating_project")) or not _is_str(claims.get("resource_scope")):
        raise _untrusted()
    # The action purpose must belong to the token's own inbox.
    if token_type == TOKEN_TYPE_INBOX_ACTION and str(purpose).split(".", 1)[0] != str(claim_inbox):
        raise _untrusted()
    return claims


# --------------------------------------------------------------------------
# 5. Read-projection value objects (§6)
# --------------------------------------------------------------------------


@dataclass
class ProjectOption:
    """One owner-facing project choice on the picker (no raw project id)."""

    business_label: str
    status_label: str
    select_token: str


@dataclass
class DecisionOption:
    purpose: str
    label: str
    token: str
    input_kind: str = INPUT_NONE
    choices: list[str] = field(default_factory=list)


@dataclass
class InboxItem:
    token: str
    business_label: str
    status_label: str
    detail_ref: str
    preview: str
    decisions: list[str]
    updated_at: str


@dataclass
class InboxPage:
    inbox: str
    title: str
    project_label: str
    context_token: str
    items: list[InboxItem]
    page_size: int = 0
    has_more: bool = False
    next_token: str | None = None


@dataclass
class InboxDetail:
    inbox: str
    title: str
    project_label: str
    context_token: str
    detail_ref: str
    business_label: str
    status_label: str
    sections: list[tuple[str, str]]
    options: list[DecisionOption]
    audit_entries: list[str]


@dataclass
class DecisionInput:
    """Owner-typed *business* input. Never an internal identity (§1.3)."""

    reason: str | None = None
    body: str | None = None
    text: str | None = None
    title: str | None = None
    stage_label: str | None = None
    tag_labels: list[str] = field(default_factory=list)
    canonical_choice: str | None = None
    series_choice: str | None = None


@dataclass
class DecisionResult:
    message: str
    context_token: str


def _preview(text: str | None) -> str:
    body = redact_pii(text or "").strip().replace("\n", " ")
    if len(body) <= PREVIEW_CHARS:
        return body
    return body[:PREVIEW_CHARS] + "…"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _parse_cursor_timestamp(raw: str) -> datetime:
    """Parse the ISO-8601 sort timestamp carried by an inbox cursor (#125).

    Unparseable timestamps are refused fail-closed -- a cursor must never be
    repaired into a guess about where the last page ended.
    """
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise _untrusted() from exc


# --------------------------------------------------------------------------
# 6. OwnerInboxService -- resolvers, projections, action adapters
# --------------------------------------------------------------------------


class OwnerInboxService:
    """Thin owner-inbox binding layer. Never re-implements CAS / audit / transactions."""

    def __init__(self, session: Session, *, ring: TokenKeyRing | None = None) -> None:
        self.session = session
        self._ring = ring

    # -- key ring -----------------------------------------------------------

    @property
    def ring(self) -> TokenKeyRing:
        if self._ring is None:
            self._ring = load_token_key_ring()
        return self._ring

    # -- project picker / bootstrap (§4.1) ----------------------------------

    def _live_projects(self) -> list[Project]:
        rows = list(self.session.exec(select(Project)))
        live = [row for row in rows if row.status in LIVE_PROJECT_STATUSES]
        return sorted(live, key=lambda row: (row.created_at, row.id))

    def _load_live_project(self, project_ref: str) -> Project:
        project = self.session.get(Project, project_ref)
        if project is None or project.status not in LIVE_PROJECT_STATUSES:
            raise _untrusted()
        return project

    def list_project_options(self, actor: ActorContext) -> list[ProjectOption]:
        """``GET /owner/project-picker`` -- business labels + sealed select tokens."""
        _assert_owner(actor)
        # Fail closed on key configuration *before* looking at the data. Loading
        # the ring lazily inside the loop would let an owner with zero live
        # projects receive a successful empty picker while the token key is
        # missing / malformed, i.e. the surface would silently look healthy in
        # exactly the state where it can mint nothing (§2.1.1 fail-closed).
        ring = self.ring
        options: list[ProjectOption] = []
        for project in self._live_projects():
            token = seal_token(
                {
                    "token_type": TOKEN_TYPE_PROJECT_SELECT,
                    "owner": actor.owner_id or "",
                    "project_ref": project.id,
                    "operating_project": project.id,
                    "project_display_binding": project_display_binding(project),
                    "purpose": PURPOSE_PROJECT_SELECT,
                },
                ring=ring,
            )
            options.append(
                ProjectOption(
                    business_label=project.name,
                    status_label=str(project.status),
                    select_token=token,
                )
            )
        return options

    def _verify_project_claims(self, claims: Mapping[str, Any]) -> Project:
        """Shared steps 4-11 of §2.1.2a / steps 4-10 of §2.1.2b."""
        project = self._load_live_project(str(claims["project_ref"]))
        # `project_ref` and `operating_project` are distinct proofs -- never collapsed.
        if not secrets.compare_digest(project.id, str(claims["operating_project"])):
            raise _untrusted()
        live_binding = project_display_binding(project)
        if not secrets.compare_digest(live_binding, str(claims["project_display_binding"])):
            raise _untrusted()
        if project.id == COMPANY_SCOPE:  # pragma: no cover - defensive
            raise _untrusted()
        return project

    def _mint_context_token(self, actor: ActorContext, project: Project) -> str:
        return seal_token(
            {
                "token_type": TOKEN_TYPE_PROJECT_CONTEXT,
                "owner": actor.owner_id or "",
                "project_ref": project.id,
                "operating_project": project.id,
                "project_display_binding": project_display_binding(project),
                "purpose": PURPOSE_PROJECT_INBOX,
            },
            ring=self.ring,
        )

    def resolve_project_select(self, actor: ActorContext, select_token: str) -> tuple[Project, str]:
        """§2.1.2a Project-select resolver (13 steps). Mints a fresh context token."""
        _assert_owner(actor)
        claims = resolve_sealed_token(
            select_token,
            actor=actor,
            endpoint=ENDPOINT_PROJECT_PICK,
            method="POST",
            ring=self.ring,
        )
        project = self._verify_project_claims(claims)
        return project, self._mint_context_token(actor, project)

    def resolve_project_context(
        self, actor: ActorContext, context_token: str, inbox: str
    ) -> tuple[Project, str]:
        """§2.1.2b Project-context resolver. ``inbox`` is the allowlisted URL kind."""
        _assert_owner(actor)
        if inbox not in INBOX_KINDS:
            raise _untrusted()
        claims = resolve_sealed_token(
            context_token,
            actor=actor,
            endpoint=ENDPOINT_INBOX_LIST,
            method="GET",
            ring=self.ring,
        )
        project = self._verify_project_claims(claims)
        return project, context_token

    # -- universal scope resolver (§2.1.3) ----------------------------------

    def _load_row(self, kind: str, rid: str) -> Any:
        if kind == "artifact" or kind == "feedback":
            row = self.session.get(Artifact, rid)
            expected = ArtifactType.CONTENT_DRAFT if kind == "artifact" else ArtifactType.FEEDBACK
            if row is None or row.type != expected:
                raise _untrusted()
            return row
        if kind == "conversation":
            row = self.session.get(Conversation, rid)
        elif kind == "suggestion":
            row = self.session.get(CsSuggestion, rid)
        elif kind == "candidate":
            row = self.session.get(KnowledgeCandidate, rid)
        elif kind == "fact":
            row = self.session.get(KnowledgeFact, rid)
        else:  # pragma: no cover - PHASE 3 already allowlists the kind
            raise _untrusted()
        if row is None:
            raise _untrusted()
        return row

    @staticmethod
    def _resource_scope(kind: str, row: Any) -> str:
        """Authoritative scope of the live row -- ``project_id`` only, NEVER provenance."""
        if kind in {"candidate", "fact"}:
            return row.project_id or COMPANY_SCOPE
        return str(row.project_id)

    def _resolve_bound_row(
        self, claims: Mapping[str, Any], *, mutating: bool
    ) -> tuple[Any, str]:
        """Load the live row and apply the §2.1.3 visibility / mutability rules."""
        kind = str(claims["kind"])
        row = self._load_row(kind, str(claims["rid"]))
        operating_project = str(claims["operating_project"])
        scope = self._resource_scope(kind, row)

        # The token's sealed scope must still describe the live row.
        if not secrets.compare_digest(scope, str(claims["resource_scope"])):
            raise _untrusted()
        visible = scope in (operating_project, COMPANY_SCOPE)
        if not visible:
            raise _untrusted()
        if mutating and scope != operating_project:
            # Company-wide rows are READ-ONLY from a project operating context.
            raise _untrusted()
        return row, scope

    def _live_display_binding(self, kind: str, row: Any) -> str:
        if kind == "artifact":
            return content_display_binding(row)
        if kind == "feedback":
            return feedback_display_binding(row)
        if kind == "conversation":
            return cs_conversation_display_binding(row, self._suggestion_count(row.id))
        if kind == "suggestion":
            return cs_suggestion_display_binding(row, self._suggestion_count(row.conversation_id))
        if kind == "candidate":
            return knowledge_candidate_display_binding(row, self._head_version(row))
        if kind == "fact":
            return knowledge_fact_display_binding(row)
        raise _untrusted()  # pragma: no cover - allowlisted upstream

    def _assert_fresh(self, claims: Mapping[str, Any], row: Any) -> None:
        live = self._live_display_binding(str(claims["kind"]), row)
        if not secrets.compare_digest(live, str(claims["display_binding"])):
            raise _untrusted()

    # -- helpers used by bindings / projections -----------------------------

    def _suggestion_count(self, conversation_id: str) -> int:
        rows = list(
            self.session.exec(
                select(CsSuggestion).where(CsSuggestion.conversation_id == conversation_id)
            )
        )
        return len(rows)

    def _head_version(self, candidate: KnowledgeCandidate) -> int:
        series_id = str(getattr(candidate, "series_id", "") or "")
        rows = list(
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.series_id == series_id,
                    KnowledgeFact.project_id == candidate.project_id,
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED,
                )
            )
        )
        return max((row.version for row in rows), default=0)

    def _seq(self, rows: Sequence[Any], target_id: str) -> int:
        ordered = sorted(rows, key=lambda row: (row.created_at, row.id))
        for index, row in enumerate(ordered, start=1):
            if row.id == target_id:
                return index
        return 0

    def _content_rows(self, project_id: str) -> list[Artifact]:
        return list(
            self.session.exec(
                select(Artifact).where(
                    Artifact.project_id == project_id,
                    Artifact.type == ArtifactType.CONTENT_DRAFT,
                )
            )
        )

    def _feedback_rows(self, project_id: str) -> list[Artifact]:
        return list(
            self.session.exec(
                select(Artifact).where(
                    Artifact.project_id == project_id,
                    Artifact.type == ArtifactType.FEEDBACK,
                )
            )
        )

    def _candidate_rows(self, project_id: str) -> list[KnowledgeCandidate]:
        rows = list(self.session.exec(select(KnowledgeCandidate)))
        return [row for row in rows if row.project_id in (project_id, None)]

    def _fact_rows(self, project_id: str) -> list[KnowledgeFact]:
        rows = list(self.session.exec(select(KnowledgeFact)))
        return [row for row in rows if row.project_id in (project_id, None)]

    def _series_labels(self, project_id: str) -> dict[str, str]:
        """§3.4 step 5: rank series by IMMUTABLE series-root ``created_at`` then id."""
        roots: dict[str, datetime] = {}
        for fact in self._fact_rows(project_id):
            current = roots.get(fact.series_id)
            if current is None or fact.created_at < current:
                roots[fact.series_id] = fact.created_at
        ordered = sorted(roots.items(), key=lambda item: (item[1], item[0]))
        return {
            series_id: f"系列 #{index}"
            for index, (series_id, _) in enumerate(ordered, start=1)
        }

    def _mint_detail_token(
        self,
        actor: ActorContext,
        *,
        operating_project: str,
        scope: str,
        inbox: str,
        kind: str,
        rid: str,
        binding: str,
    ) -> str:
        return seal_token(
            {
                "token_type": TOKEN_TYPE_DETAIL_VIEW,
                "owner": actor.owner_id or "",
                "operating_project": operating_project,
                "resource_scope": scope,
                "inbox": inbox,
                "kind": kind,
                "rid": rid,
                "purpose": PURPOSE_VIEW_DETAIL,
                "display_binding": binding,
            },
            ring=self.ring,
        )

    def _mint_action_token(
        self,
        actor: ActorContext,
        *,
        operating_project: str,
        scope: str,
        inbox: str,
        kind: str,
        rid: str,
        purpose: str,
        binding: str,
    ) -> str:
        return seal_token(
            {
                "token_type": TOKEN_TYPE_INBOX_ACTION,
                "owner": actor.owner_id or "",
                "operating_project": operating_project,
                "resource_scope": scope,
                "inbox": inbox,
                "kind": kind,
                "rid": rid,
                "purpose": purpose,
                "display_binding": binding,
            },
            ring=self.ring,
        )

    # -- inbox listing (§6.1 - §6.4, pagination contract #125) --------------

    @staticmethod
    def _sort_key(inbox: str) -> str:
        """Stable total-order sort column per inbox (#125).

        ``Artifact`` (content / feedback) carries no ``updated_at`` column
        (models.py), so those two inboxes sort on ``created_at``; customer
        service and knowledge sort on ``updated_at``. In every case the row id
        is the explicit tiebreaker, so equal timestamps never reorder between
        requests.
        """
        if inbox in (INBOX_CONTENT, INBOX_FEEDBACK):
            return "created_at"
        return "updated_at"

    @staticmethod
    def _normalize_page_size(page_size: int | None) -> int:
        """Deterministic page-size policy (#125): missing / non-positive sizes
        resolve to the default; anything above the hard cap is clamped."""
        if page_size is None or page_size <= 0:
            return DEFAULT_PAGE_SIZE
        return min(page_size, MAX_PAGE_SIZE)

    @staticmethod
    def _cursor_after(
        row: Any, sort_field: str, cursor_ts: datetime, cursor_id: str
    ) -> bool:
        """Keyset comparison: strictly after ``(cursor_ts, cursor_id)`` (#125)."""
        ts = getattr(row, sort_field)
        if ts > cursor_ts:
            return True
        if ts < cursor_ts:
            return False
        return str(row.id) > cursor_id

    def _snapshot(
        self,
        rows: list[Any],
        inbox: str,
        cursor: tuple[datetime, str] | None,
        limit: int,
    ) -> tuple[list[Any], bool]:
        """Stable sort + keyset slice for one inbox (#125).

        Returns ``(page_rows, has_more)`` where ``page_rows`` holds at most
        ``limit`` rows and ``has_more`` reports whether rows remain. Rows
        inserted after the cursor land on later pages; rows whose state leaves
        the pending set simply stop being returned -- nothing is delivered
        twice.
        """
        sort_field = self._sort_key(inbox)
        ordered = sorted(rows, key=lambda row: (getattr(row, sort_field), str(row.id)))
        if cursor is not None:
            cursor_ts, cursor_id = cursor
            ordered = [
                row
                for row in ordered
                if self._cursor_after(row, sort_field, cursor_ts, cursor_id)
            ]
        page_rows = ordered[:limit]
        return page_rows, len(ordered) > limit

    def _mint_cursor_token(
        self,
        actor: ActorContext,
        *,
        operating_project: str,
        scope: str,
        inbox: str,
        sort_ts: str,
        sort_id: str,
    ) -> str:
        return seal_token(
            {
                "token_type": TOKEN_TYPE_INBOX_CURSOR,
                "owner": actor.owner_id or "",
                "operating_project": operating_project,
                "resource_scope": scope,
                "inbox": inbox,
                "purpose": PURPOSE_INBOX_NEXT,
                "sort_ts": sort_ts,
                "sort_id": sort_id,
            },
            ring=self.ring,
        )

    def _resolve_cursor(
        self, actor: ActorContext, cursor_token: str | None, inbox: str, project_id: str
    ) -> tuple[datetime, str] | None:
        """Resolve an opaque next-page cursor to ``(sort_ts, sort_id)`` (#125).

        The sealed token is validated against the *requested* inbox (PHASE 3),
        so a cursor minted for one inbox can never page another one. Its
        ``operating_project`` / ``resource_scope`` claims must also equal the
        project being listed: a cursor minted under project A must not be
        replayed against project B's context token (which would silently skip
        B rows and break the no-skip guarantee).
        """
        if cursor_token is None or cursor_token == "":
            return None
        claims = resolve_sealed_token(
            cursor_token,
            actor=actor,
            endpoint=ENDPOINT_INBOX_LIST,
            method="GET",
            ring=self.ring,
            inbox=inbox,
        )
        if not secrets.compare_digest(str(claims.get("operating_project")), project_id) or (
            not secrets.compare_digest(str(claims.get("resource_scope")), project_id)
        ):
            raise _untrusted()
        return _parse_cursor_timestamp(str(claims["sort_ts"])), str(claims["sort_id"])

    def list_inbox(
        self,
        actor: ActorContext,
        context_token: str,
        inbox: str,
        *,
        page_size: int | None = None,
        cursor_token: str | None = None,
    ) -> InboxPage:
        """One page of one inbox (pagination contract #125).

        Deterministic keyset paging: a page is the slice of the stable total
        order strictly after the sealed cursor (or the head of the list for the
        first page), capped at the server-side maximum. ``has_more`` and the
        opaque ``next_token`` define the owner-facing "下一页" semantics; an
        empty page means no pending items; re-visiting a page re-evaluates the
        cursor against live state so already-handled rows never reappear.
        """
        project, ctx = self.resolve_project_context(actor, context_token, inbox)
        limit = self._normalize_page_size(page_size)
        cursor = self._resolve_cursor(actor, cursor_token, inbox, project.id)
        if inbox == INBOX_CONTENT:
            items, has_more, last_row = self._content_items(
                actor, project, limit=limit, cursor=cursor
            )
        elif inbox == INBOX_CS:
            items, has_more, last_row = self._cs_items(
                actor, project, limit=limit, cursor=cursor
            )
        elif inbox == INBOX_FEEDBACK:
            items, has_more, last_row = self._feedback_items(
                actor, project, limit=limit, cursor=cursor
            )
        else:
            items, has_more, last_row = self._knowledge_items(
                actor, project, limit=limit, cursor=cursor
            )
        next_token = None
        if has_more and last_row is not None:
            next_token = self._mint_cursor_token(
                actor,
                operating_project=project.id,
                scope=project.id,
                inbox=inbox,
                sort_ts=_iso(getattr(last_row, self._sort_key(inbox))),
                sort_id=str(last_row.id),
            )
        return InboxPage(
            inbox=inbox,
            title=INBOX_TITLES[inbox],
            project_label=project.name,
            context_token=ctx,
            items=items,
            page_size=len(items),
            has_more=has_more,
            next_token=next_token,
        )

    @staticmethod
    def _content_decisions(status: ArtifactReviewStatus) -> list[str]:
        # Owner daily flow: an edit-and-resubmit runs update+submit in one
        # request; the happy path lands REVIEW_PASSED (approve/reject offered),
        # and a degraded review lands NEEDS_REVISION (edit-and-resubmit offered).
        # The UNVERIFIED branch (resubmit) is reachable when the row was left in
        # UNVERIFIED: either pre-set by the content service API at create time,
        # or left behind by a partial failure (edit committed, submit failed) --
        # in both cases the owner sees it as a valid retry entry, so the branch
        # is intentional and required, not dead UI (#128).
        if status == ArtifactReviewStatus.REVIEW_PASSED:
            return [PURPOSE_CONTENT_APPROVE, PURPOSE_CONTENT_REJECT]
        if status == ArtifactReviewStatus.UNVERIFIED:
            return [PURPOSE_CONTENT_RESUBMIT]
        if status == ArtifactReviewStatus.NEEDS_REVISION:
            return [PURPOSE_CONTENT_EDIT_AND_RESUBMIT]
        return []

    def _content_items(
        self,
        actor: ActorContext,
        project: Project,
        *,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> tuple[list[InboxItem], bool, Any]:
        rows = self._content_rows(project.id)
        pending = [row for row in rows if row.review_status in CONTENT_PENDING_STATUSES]
        page_rows, has_more = self._snapshot(pending, INBOX_CONTENT, cursor, limit)
        items: list[InboxItem] = []
        # ``Artifact`` has no ``updated_at`` column (models.py) -- an edit bumps
        # ``revision_count`` / ``checksum`` instead, which the display binding
        # already covers. Order (and show) the immutable ``created_at``.
        for artifact in page_rows:
            metadata = dict(artifact.metadata_json or {})
            topic = str(metadata.get("topic") or "未命名内容")
            status_label = CONTENT_STATUS_LABELS.get(str(artifact.review_status), "待处理")
            items.append(
                InboxItem(
                    token=self._mint_detail_token(
                        actor,
                        operating_project=project.id,
                        scope=project.id,
                        inbox=INBOX_CONTENT,
                        kind="artifact",
                        rid=artifact.id,
                        binding=content_display_binding(artifact),
                    ),
                    business_label=f"《{topic}》— 第 {artifact.revision_count} 轮",
                    status_label=status_label,
                    detail_ref=f"内容#{self._seq(rows, artifact.id)}",
                    preview=_preview(artifact.uri),
                    decisions=self._content_decisions(artifact.review_status),
                    updated_at=_iso(artifact.created_at),
                )
            )
        return items, has_more, (page_rows[-1] if page_rows else None)

    def _pending_suggestion(self, conversation_id: str) -> CsSuggestion | None:
        rows = list(
            self.session.exec(
                select(CsSuggestion).where(CsSuggestion.conversation_id == conversation_id)
            )
        )
        pending = [
            row
            for row in rows
            if row.decision == CsSuggestionDecision.HUMAN_CONFIRM and not row.consumed
        ]
        if not pending:
            return None
        return sorted(pending, key=lambda row: (row.created_at, row.id))[-1]

    def _has_escalation(self, conversation_id: str) -> bool:
        rows = list(
            self.session.exec(select(Message).where(Message.conversation_id == conversation_id))
        )
        return any(row.escalation_flag for row in rows)

    def _cs_decisions(self, conversation: Conversation, pending: CsSuggestion | None) -> list[str]:
        decisions: list[str] = []
        if pending is not None:
            decisions.append(PURPOSE_CS_ADOPT_AND_SEND)
        decisions.append(PURPOSE_CS_ESCALATE)
        decisions.append(PURPOSE_CS_SET_LEAD_STAGE)
        if conversation.assigned_human is None:
            decisions.append(PURPOSE_CS_ASSIGN_HUMAN)
        decisions.append(PURPOSE_CS_CREATE_FOLLOWUP)
        return decisions

    def _cs_items(
        self,
        actor: ActorContext,
        project: Project,
        *,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> tuple[list[InboxItem], bool, Any]:
        rows = list(
            self.session.exec(select(Conversation).where(Conversation.project_id == project.id))
        )
        visible: list[Conversation] = []
        for conversation in rows:
            pending = self._pending_suggestion(conversation.id)
            escalated = self._has_escalation(conversation.id)
            if pending is None and not escalated:
                continue
            visible.append(conversation)
        page_rows, has_more = self._snapshot(visible, INBOX_CS, cursor, limit)
        items: list[InboxItem] = []
        for conversation in page_rows:
            pending = self._pending_suggestion(conversation.id)
            escalated = self._has_escalation(conversation.id)
            ref = conversation.customer_ref or f"{self._seq(rows, conversation.id)}"
            if pending is not None:
                confidence = int(round((pending.confidence or 0.0) * 100))
                label = (
                    f"客户对话 #{ref} — AI 建议：{_preview(pending.text)}"
                    f"（置信度 {confidence}%）"
                )
                status_label = "待你确认/转人工"
            else:
                label = f"客户对话 #{ref} — 已标记需人工处理"
                status_label = "待你处理"
            items.append(
                InboxItem(
                    token=self._mint_detail_token(
                        actor,
                        operating_project=project.id,
                        scope=project.id,
                        inbox=INBOX_CS,
                        kind="conversation",
                        rid=conversation.id,
                        binding=cs_conversation_display_binding(
                            conversation, self._suggestion_count(conversation.id)
                        ),
                    ),
                    business_label=label,
                    status_label=status_label,
                    detail_ref=f"对话#{ref}",
                    preview=_preview(pending.text if pending is not None else ""),
                    decisions=self._cs_decisions(conversation, pending),
                    updated_at=_iso(conversation.updated_at),
                )
            )
        return items, has_more, (page_rows[-1] if page_rows else None)

    def _feedback_items(
        self,
        actor: ActorContext,
        project: Project,
        *,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> tuple[list[InboxItem], bool, Any]:
        rows = self._feedback_rows(project.id)
        awaiting = [
            row
            for row in rows
            if dict(row.metadata_json or {}).get("stage") == FEEDBACK_AWAIT_OWNER_APPROVE
        ]
        page_rows, has_more = self._snapshot(awaiting, INBOX_FEEDBACK, cursor, limit)
        items: list[InboxItem] = []
        # Same as the content inbox: ``Artifact`` carries no ``updated_at``.
        for artifact in page_rows:
            metadata = dict(artifact.metadata_json or {})
            items.append(
                InboxItem(
                    token=self._mint_detail_token(
                        actor,
                        operating_project=project.id,
                        scope=project.id,
                        inbox=INBOX_FEEDBACK,
                        kind="feedback",
                        rid=artifact.id,
                        binding=feedback_display_binding(artifact),
                    ),
                    business_label=f"用户反馈：{_preview(artifact.uri)}",
                    status_label="方案已就绪，待你批准",
                    detail_ref=f"反馈#{self._seq(rows, artifact.id)}",
                    preview=_preview(str(metadata.get("solution_text") or "")),
                    decisions=[
                        PURPOSE_FEEDBACK_APPROVE_SOLUTION,
                        PURPOSE_FEEDBACK_REJECT_SOLUTION,
                        PURPOSE_FEEDBACK_DEFER,
                        PURPOSE_FEEDBACK_MARK_DUPLICATE,
                        PURPOSE_FEEDBACK_REJECT_FEEDBACK,
                    ],
                    updated_at=_iso(artifact.created_at),
                )
            )
        return items, has_more, (page_rows[-1] if page_rows else None)

    @staticmethod
    def _candidate_decisions(
        candidate: KnowledgeCandidate, scope: str, operating: str
    ) -> list[str]:
        if scope != operating:
            return []  # company-wide rows are read-only from a project context
        if candidate.status != KnowledgeCandidateStatus.DRAFT:
            return []
        if is_legacy_unclassified(candidate.tags):
            return [PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE, PURPOSE_KNOWLEDGE_REJECT_CANDIDATE]
        return [PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE, PURPOSE_KNOWLEDGE_REJECT_CANDIDATE]

    def _knowledge_items(
        self,
        actor: ActorContext,
        project: Project,
        *,
        limit: int,
        cursor: tuple[datetime, str] | None,
    ) -> tuple[list[InboxItem], bool, Any]:
        candidates = self._candidate_rows(project.id)
        facts = self._fact_rows(project.id)
        # Unified pending snapshot: DRAFT candidates + APPROVED facts, sorted
        # together on the knowledge inbox's stable key (updated_at, id).
        unified: list[tuple[str, Any]] = [
            ("candidate", candidate)
            for candidate in candidates
            if candidate.status == KnowledgeCandidateStatus.DRAFT
        ]
        unified.extend(
            ("fact", fact) for fact in facts if fact.status == KnowledgeFactStatus.APPROVED
        )
        sort_field = self._sort_key(INBOX_KNOWLEDGE)
        ordered = sorted(
            unified, key=lambda item: (getattr(item[1], sort_field), str(item[1].id))
        )
        if cursor is not None:
            cursor_ts, cursor_id = cursor
            ordered = [
                item
                for item in ordered
                if self._cursor_after(item[1], sort_field, cursor_ts, cursor_id)
            ]
        page_rows = ordered[:limit]
        has_more = len(ordered) > limit
        items: list[InboxItem] = []
        series_labels = self._series_labels(project.id)
        for kind, row in page_rows:
            if kind == "candidate":
                candidate = row
                scope = candidate.project_id or COMPANY_SCOPE
                decisions = self._candidate_decisions(candidate, scope, project.id)
                status_label = (
                    "需先分类"
                    if is_legacy_unclassified(candidate.tags)
                    else "待你审定"
                )
                if scope == COMPANY_SCOPE:
                    status_label = "公司级知识（只读）"
                items.append(
                    InboxItem(
                        token=self._mint_detail_token(
                            actor,
                            operating_project=project.id,
                            scope=scope,
                            inbox=INBOX_KNOWLEDGE,
                            kind="candidate",
                            rid=candidate.id,
                            binding=knowledge_candidate_display_binding(
                                candidate, self._head_version(candidate)
                            ),
                        ),
                        business_label=f"知识候选：{_preview(candidate.statement)}",
                        status_label=status_label,
                        detail_ref=f"知识候选#{self._seq(candidates, candidate.id)}",
                        preview=_preview(candidate.statement),
                        decisions=decisions,
                        updated_at=_iso(candidate.updated_at),
                    )
                )
            else:
                fact = row
                scope = fact.project_id or COMPANY_SCOPE
                decisions = [PURPOSE_KNOWLEDGE_DEACTIVATE_FACT] if scope == project.id else []
                items.append(
                    InboxItem(
                        token=self._mint_detail_token(
                            actor,
                            operating_project=project.id,
                            scope=scope,
                            inbox=INBOX_KNOWLEDGE,
                            kind="fact",
                            rid=fact.id,
                            binding=knowledge_fact_display_binding(fact),
                        ),
                        business_label=(
                            f"知识事实（{series_labels.get(fact.series_id, '系列')}）："
                            f"{_preview(fact.statement)}"
                        ),
                        status_label=(
                            "公司级知识（只读）" if scope == COMPANY_SCOPE else "待你停用"
                        ),
                        detail_ref=f"知识事实#{self._seq(facts, fact.id)}",
                        preview=_preview(fact.statement),
                        decisions=decisions,
                        updated_at=_iso(fact.updated_at),
                    )
                )
        return items, has_more, (page_rows[-1][1] if page_rows else None)

    # -- detail view (§2.1.2c) ----------------------------------------------

    def resolve_detail_view(
        self, actor: ActorContext, detail_token: str, inbox: str, context_token: str
    ) -> InboxDetail:
        _assert_owner(actor)
        if inbox not in INBOX_KINDS:
            raise _untrusted()
        claims = resolve_sealed_token(
            detail_token,
            actor=actor,
            endpoint=ENDPOINT_INBOX_DETAIL,
            method="POST",
            ring=self.ring,
            inbox=inbox,
        )
        project = self._load_live_project(str(claims["operating_project"]))
        row, scope = self._resolve_bound_row(claims, mutating=False)
        self._assert_fresh(claims, row)

        kind = str(claims["kind"])
        sections, business_label, status_label, detail_ref, purposes = self._detail_view_model(
            inbox, kind, row, project, scope
        )
        options = [
            DecisionOption(
                purpose=purpose,
                label=ACTION_LABELS[purpose],
                token=self._mint_action_token(
                    actor,
                    operating_project=project.id,
                    scope=self._resource_scope(action_kind, action_row),
                    inbox=inbox,
                    kind=action_kind,
                    rid=action_row.id,
                    purpose=purpose,
                    binding=self._live_display_binding(action_kind, action_row),
                ),
                input_kind=ACTION_INPUTS[purpose],
                choices=self._choices_for(purpose, project),
            )
            for purpose, action_kind, action_row in purposes
        ]
        return InboxDetail(
            inbox=inbox,
            title=INBOX_TITLES[inbox],
            project_label=project.name,
            context_token=context_token,
            detail_ref=detail_ref,
            business_label=business_label,
            status_label=status_label,
            sections=sections,
            options=options,
            audit_entries=self._audit_entries(kind, row, project.id),
        )

    def _choices_for(self, purpose: str, project: Project) -> list[str]:
        if purpose == PURPOSE_CS_SET_LEAD_STAGE:
            return list(LEAD_STAGE_LABELS.values())
        if purpose == PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE:
            return [CANONICAL_TAG_LABELS[tag] for tag in sorted(CANONICAL_KNOWLEDGE_TAGS)]
        if purpose == PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE:
            return ["新建系列", *self._series_labels(project.id).values()]
        if purpose == PURPOSE_FEEDBACK_MARK_DUPLICATE:
            rows = self._feedback_rows(project.id)
            ordered = sorted(rows, key=lambda row: (row.created_at, row.id))
            return [f"反馈#{index}" for index, _ in enumerate(ordered, start=1)]
        return []

    def _detail_view_model(
        self, inbox: str, kind: str, row: Any, project: Project, scope: str
    ) -> tuple[list[tuple[str, str]], str, str, str, list[tuple[str, str, Any]]]:
        """Return (sections, business_label, status_label, detail_ref, [(purpose, kind, row)])."""
        if inbox == INBOX_CONTENT:
            metadata = dict(row.metadata_json or {})
            review = dict(metadata.get("independent_review") or {})
            topic = str(metadata.get("topic") or "未命名内容")
            sections = [
                ("内容正文", redact_pii(row.uri or "")),
                ("独立复审结论", str(review.get("result") or "尚无复审结论")),
                ("复审说明", str(review.get("bounded_reason") or "")),
            ]
            purposes = [
                (purpose, "artifact", row) for purpose in self._content_decisions(row.review_status)
            ]
            return (
                sections,
                f"《{topic}》— 第 {row.revision_count} 轮",
                CONTENT_STATUS_LABELS.get(str(row.review_status), "待处理"),
                f"内容#{self._seq(self._content_rows(project.id), row.id)}",
                purposes,
            )

        if inbox == INBOX_CS:
            conversation = row
            pending = self._pending_suggestion(conversation.id)
            messages = list(
                self.session.exec(
                    select(Message).where(Message.conversation_id == conversation.id)
                )
            )
            recent = sorted(messages, key=lambda item: (item.created_at, item.id))[-5:]
            transcript = "\n".join(f"- {redact_pii(item.body)}" for item in recent)
            sections = [
                ("最近消息", transcript),
                ("AI 建议回复", _preview(pending.text) if pending is not None else "（暂无建议）"),
                ("当前阶段", LEAD_STAGE_LABELS.get(str(conversation.lead_stage), "未知")),
            ]
            purposes: list[tuple[str, str, Any]] = []
            if pending is not None:
                purposes.append((PURPOSE_CS_ADOPT_AND_SEND, "suggestion", pending))
            for purpose in self._cs_decisions(conversation, pending):
                if purpose != PURPOSE_CS_ADOPT_AND_SEND:
                    purposes.append((purpose, "conversation", conversation))
            ref = conversation.customer_ref or conversation.id[-4:]
            return (
                sections,
                f"客户对话 #{ref}",
                "待你确认/转人工",
                f"对话#{ref}",
                purposes,
            )

        if inbox == INBOX_FEEDBACK:
            metadata = dict(row.metadata_json or {})
            raw_tags = list(metadata.get("risk_tags") or [])
            labels = [RISK_TAG_LABELS.get(tag, "其他风险") for tag in raw_tags]
            sections = [
                ("反馈原文", redact_pii(row.uri or "")),
                ("场景", redact_pii(str(metadata.get("scenario") or ""))),
                ("期望结果", redact_pii(str(metadata.get("expected_outcome") or ""))),
                ("建议方案", redact_pii(str(metadata.get("solution_text") or ""))),
                ("风险标记", "、".join(labels) if labels else "无"),
            ]
            purposes = [
                (purpose, "feedback", row)
                for purpose in (
                    PURPOSE_FEEDBACK_APPROVE_SOLUTION,
                    PURPOSE_FEEDBACK_REJECT_SOLUTION,
                    PURPOSE_FEEDBACK_DEFER,
                    PURPOSE_FEEDBACK_MARK_DUPLICATE,
                    PURPOSE_FEEDBACK_REJECT_FEEDBACK,
                )
            ]
            return (
                sections,
                f"用户反馈：{_preview(row.uri)}",
                "方案已就绪，待你批准",
                f"反馈#{self._seq(self._feedback_rows(project.id), row.id)}",
                purposes,
            )

        if kind == "candidate":
            labels = [CANONICAL_TAG_LABELS.get(tag, "待分类") for tag in list(row.tags or [])]
            sections = [
                ("知识陈述", redact_pii(row.statement)),
                ("当前分类", "、".join(labels) if labels else "尚未分类"),
            ]
            if is_legacy_unclassified(row.tags):
                sections.append(("提示", "该候选尚未分类，需先分类后才能审定。"))
            purposes = [
                (purpose, "candidate", row)
                for purpose in self._candidate_decisions(row, scope, project.id)
            ]
            return (
                sections,
                f"知识候选：{_preview(row.statement)}",
                "公司级知识（只读）" if scope == COMPANY_SCOPE else "待你审定",
                f"知识候选#{self._seq(self._candidate_rows(project.id), row.id)}",
                purposes,
            )

        series_labels = self._series_labels(project.id)
        sections = [
            ("知识陈述", redact_pii(row.statement)),
            ("所属系列", series_labels.get(row.series_id, "系列")),
        ]
        purposes = (
            [(PURPOSE_KNOWLEDGE_DEACTIVATE_FACT, "fact", row)] if scope == project.id else []
        )
        return (
            sections,
            f"知识事实：{_preview(row.statement)}",
            "公司级知识（只读）" if scope == COMPANY_SCOPE else "待你停用",
            f"知识事实#{self._seq(self._fact_rows(project.id), row.id)}",
            purposes,
        )

    # -- audit-history presentation (defect D2, Issue #118 / #130) ----------
    # The owner-facing audit history must never show raw gateway/audit
    # identifiers. These closed allow-lists translate the stored ``actor`` /
    # ``action`` strings to bounded business Chinese. The AuditLog *storage* is
    # never rewritten -- no migration, no identity change, no weakened
    # auditability. Anything outside the allow-list collapses to a safe
    # fallback rather than leaking.
    _AUDIT_ACTION_LABELS: dict[str, str] = {
        # content
        "content_draft.create": "内容已创建",
        "content_draft.update": "内容已更新",
        "content_draft.independent_review": "独立复审完成",
        "content_draft.approve": "内容已批准",
        "content_draft.reject": "内容已驳回",
        "content.review_metric": "评分已记录",
        # customer service
        "cs.outbound_send": "外呼已发送",
        "cs.escalation": "已升级处理",
        "cs.lead_stage": "线索阶段已更新",
        # feedback
        "feedback.create": "反馈已创建",
        "feedback.amend": "反馈已修订",
        "feedback.stage_transition": "反馈阶段已流转",
        "feedback.submit_for_approval": "已提交审定",
        "feedback.owner_approve": "反馈已审定",
        "feedback.owner_reject": "反馈已退回",
        "feedback.invalidate_pending": "待定反馈已作废",
        "feedback.cluster_summary": "聚类摘要已生成",
        # knowledge
        "knowledge.candidate.created": "知识候选已创建",
        "knowledge.candidate.rejected": "知识候选已驳回",
        "knowledge.candidate.classified": "知识候选已分类",
        "knowledge.fact.approved": "知识事实已批准",
        "knowledge.fact.superseded": "知识事实已被更替",
        "knowledge.fact.deactivated": "知识事实已停用",
        "knowledge.fact.classified": "知识事实已分类",
        # agent interoperability gateway (#57)
        "agent.discover": "发现智能体",
        "agent.delegate": "委派任务",
        "agent.result_received": "已收到结果",
        "artifact.validated": "制品已校验",
        "delegation.failed": "委派失败",
        "delegation.cancelled": "委派已取消",
    }

    @staticmethod
    def _translate_audit_actor(raw: str) -> str:
        """Closed allow-list for the audit ``actor`` column (D2).

        The only owner in the OOL is the authenticated owner, addressed as
        "你". Agents are "AI 助手". The system and every internal component
        (gateway / scheduler / orchestrator / bootstrap / unknown) collapse to
        "系统" -- never the raw identifier.
        """
        if raw == "owner" or raw.startswith("owner:"):
            return "你"
        if raw == "agent" or raw.startswith("agent:"):
            return "AI 助手"
        return "系统"

    @staticmethod
    def _translate_audit_action(raw: str) -> str:
        """Closed allow-list for the audit ``action`` column (D2).

        Unknown actions fall back to the bounded "状态已更新" rather than the
        raw dotted identifier.
        """
        return OwnerInboxService._AUDIT_ACTION_LABELS.get(raw, "状态已更新")

    def _audit_entries(self, kind: str, row: Any, project_id: str) -> list[str]:
        resource_type = {
            "artifact": "artifact",
            "feedback": "artifact",
            "conversation": "conversation",
            "suggestion": "conversation",
            "candidate": "knowledge_candidate",
            "fact": "knowledge_fact",
        }[kind]
        rows = list(
            self.session.exec(
                select(AuditLog).where(
                    AuditLog.resource_type == resource_type,
                    AuditLog.resource_id == row.id,
                    AuditLog.project_id == project_id,
                )
            )
        )
        ordered = sorted(rows, key=lambda item: (item.created_at, item.id))
        return [
            f"{item.created_at.isoformat()} — "
            f"{self._translate_audit_actor(item.actor)} — "
            f"{self._translate_audit_action(item.action)}"
            for item in ordered
        ]

    # -- decisions (§2.1.2d + §7 action adapters) ---------------------------

    def decide(
        self,
        actor: ActorContext,
        *,
        action_token: str,
        inbox: str,
        payload: DecisionInput,
        context_token: str,
    ) -> DecisionResult:
        _assert_owner(actor)
        if inbox not in INBOX_KINDS:
            raise _untrusted()
        claims = resolve_sealed_token(
            action_token,
            actor=actor,
            endpoint=ENDPOINT_INBOX_DECIDE,
            method="POST",
            ring=self.ring,
            inbox=inbox,
        )
        project = self._load_live_project(str(claims["operating_project"]))
        row, _scope = self._resolve_bound_row(claims, mutating=True)
        self._assert_fresh(claims, row)

        purpose = str(claims["purpose"])
        message = self._dispatch(actor, purpose=purpose, row=row, project=project, payload=payload)
        return DecisionResult(message=message, context_token=context_token)

    def _dispatch(
        self,
        actor: ActorContext,
        *,
        purpose: str,
        row: Any,
        project: Project,
        payload: DecisionInput,
    ) -> str:
        if purpose.startswith("content."):
            return self._decide_content(actor, purpose, row, payload)
        if purpose.startswith("cs."):
            return self._decide_cs(actor, purpose, row, payload)
        if purpose.startswith("feedback."):
            return self._decide_feedback(actor, purpose, row, project, payload)
        return self._decide_knowledge(actor, purpose, row, project, payload)

    # content ---------------------------------------------------------------

    def _decide_content(
        self, actor: ActorContext, purpose: str, artifact: Artifact, payload: DecisionInput
    ) -> str:
        service = ContentDraftService(self.session)
        metadata = dict(artifact.metadata_json or {})
        topic = str(metadata.get("topic") or "该内容")
        if purpose == PURPOSE_CONTENT_APPROVE:
            review = dict(metadata.get("independent_review") or {})
            checksum = str(review.get("reviewed_checksum") or "")
            revision = review.get("reviewed_revision")
            if not checksum or not _is_int(revision):
                raise _stale()
            updated = service.approve_content_draft(
                artifact_id=artifact.id,
                actor=actor,
                review_checksum=checksum,
                review_revision=int(revision),
            )
            return f"已批准《{topic}》第 {updated.revision_count} 轮。"
        if purpose == PURPOSE_CONTENT_REJECT:
            reason = _require_text(payload.reason, MSG_NEED_REASON)
            service.reject_content_draft(artifact_id=artifact.id, actor=actor, reason=reason)
            return f"已驳回《{topic}》。"
        if purpose == PURPOSE_CONTENT_RESUBMIT:
            # UNVERIFIED only: submit ONLY -- never update_content_draft (P1-3).
            service.submit_content_draft(artifact_id=artifact.id, actor=actor)
            return f"已重新送审《{topic}》。"
        if purpose == PURPOSE_CONTENT_EDIT_AND_RESUBMIT:
            body = _require_text(payload.body, "请填写修改后的正文。")
            service.update_content_draft(artifact_id=artifact.id, actor=actor, body=body)
            # Partial-failure contract (§8): the edit is already committed by
            # ``update_content_draft``. If the follow-up submit fails we must NOT
            # tell the owner the whole step failed (they would edit twice) -- we
            # report the exact half-done state so the retry is "submit only".
            try:
                service.submit_content_draft(artifact_id=artifact.id, actor=actor)
            except ServiceError as error:
                raise ServiceError(error.status_code, MSG_CONTENT_PARTIAL) from error
            return f"已保存修改并重新送审《{topic}》。"
        raise _action_unavailable()  # pragma: no cover - purpose allowlisted upstream

    # customer service -------------------------------------------------------

    def _decide_cs(
        self, actor: ActorContext, purpose: str, row: Any, payload: DecisionInput
    ) -> str:
        service = CustomerService(self.session)
        if purpose == PURPOSE_CS_ADOPT_AND_SEND:
            suggestion: CsSuggestion = row
            edited = payload.text.strip() if payload.text else None
            service.owner_confirm_suggestion(
                actor,
                conversation_id=suggestion.conversation_id,
                suggestion_id=suggestion.id,
                edited_text=edited if edited and edited != suggestion.text else None,
            )
            return "已发送回复。"
        conversation: Conversation = row
        if purpose == PURPOSE_CS_ESCALATE:
            service.escalate(actor, conversation_id=conversation.id)
            return "已转人工。"
        if purpose == PURPOSE_CS_SET_LEAD_STAGE:
            label = _require_text(payload.stage_label, "请选择客户阶段。")
            stage = LEAD_STAGE_BY_LABEL.get(label)
            if stage is None:
                raise ServiceError(422, "请选择客户阶段。")
            service.set_lead_stage(actor, conversation_id=conversation.id, stage=stage)
            return f"已标记为「{label}」。"
        if purpose == PURPOSE_CS_ASSIGN_HUMAN:
            service.assign_human(actor, conversation_id=conversation.id)
            return "已分配负责人。"
        if purpose == PURPOSE_CS_CREATE_FOLLOWUP:
            title = _require_text(payload.title, "请填写跟进任务标题。")
            service.create_followup_task(actor, conversation_id=conversation.id, title=title)
            return "已创建跟进任务。"
        raise _action_unavailable()  # pragma: no cover

    # feedback ---------------------------------------------------------------

    _FEEDBACK_TRANSITIONS: dict[str, FeedbackTransition] = {
        PURPOSE_FEEDBACK_APPROVE_SOLUTION: FeedbackTransition.APPROVE_SOLUTION,
        PURPOSE_FEEDBACK_REJECT_SOLUTION: FeedbackTransition.REJECT_SOLUTION,
        PURPOSE_FEEDBACK_DEFER: FeedbackTransition.DEFER,
        PURPOSE_FEEDBACK_MARK_DUPLICATE: FeedbackTransition.MARK_DUPLICATE,
        PURPOSE_FEEDBACK_REJECT_FEEDBACK: FeedbackTransition.REJECT_FEEDBACK,
    }

    def _decide_feedback(
        self,
        actor: ActorContext,
        purpose: str,
        artifact: Artifact,
        project: Project,
        payload: DecisionInput,
    ) -> str:
        service = FeedbackService(self.session)
        transition = self._FEEDBACK_TRANSITIONS[purpose]
        canonical_id: str | None = None
        reason: str | None = payload.reason
        if purpose == PURPOSE_FEEDBACK_MARK_DUPLICATE:
            canonical_id = self._resolve_canonical_feedback(
                project.id, payload.canonical_choice, exclude_id=artifact.id
            )
        elif purpose != PURPOSE_FEEDBACK_APPROVE_SOLUTION:
            reason = _require_text(payload.reason, MSG_NEED_REASON)
        service.apply_transition(
            artifact_id=artifact.id,
            actor=actor,
            transition=transition,
            reason=reason,
            canonical_feedback_id=canonical_id,
        )
        return {
            PURPOSE_FEEDBACK_APPROVE_SOLUTION: "已批准该方案。",
            PURPOSE_FEEDBACK_REJECT_SOLUTION: "已驳回该方案。",
            PURPOSE_FEEDBACK_DEFER: "已暂缓该反馈。",
            PURPOSE_FEEDBACK_MARK_DUPLICATE: "已标记为重复反馈。",
            PURPOSE_FEEDBACK_REJECT_FEEDBACK: "已拒绝该反馈。",
        }[purpose]

    def _resolve_canonical_feedback(
        self, project_id: str, choice: str | None, *, exclude_id: str
    ) -> str:
        label = _require_text(choice, "请选择重复的目标反馈。")
        rows = sorted(self._feedback_rows(project_id), key=lambda row: (row.created_at, row.id))
        for index, row in enumerate(rows, start=1):
            if f"反馈#{index}" == label:
                if row.id == exclude_id:
                    raise ServiceError(422, "请选择另一条反馈作为重复目标。")
                return row.id
        raise ServiceError(422, "请选择重复的目标反馈。")

    # knowledge --------------------------------------------------------------

    def _decide_knowledge(
        self,
        actor: ActorContext,
        purpose: str,
        row: Any,
        project: Project,
        payload: DecisionInput,
    ) -> str:
        service = KnowledgeService(self.session)
        if purpose == PURPOSE_KNOWLEDGE_CLASSIFY_CANDIDATE:
            tags = [CANONICAL_TAG_BY_LABEL[label] for label in payload.tag_labels
                    if label in CANONICAL_TAG_BY_LABEL]
            if not tags:
                raise ServiceError(422, "请至少勾选一个知识分类。")
            service.classify_candidate_tags(row.id, tags, actor=actor)
            return "已完成分类。"
        if purpose == PURPOSE_KNOWLEDGE_REJECT_CANDIDATE:
            rationale = _require_text(payload.reason, MSG_NEED_REASON)
            service.review_candidate(
                row.id, KnowledgeReviewDecisionValue.REJECT, rationale, actor=actor
            )
            return "已驳回该知识候选。"
        if purpose == PURPOSE_KNOWLEDGE_APPROVE_CANDIDATE:
            rationale = _require_text(payload.reason, MSG_NEED_REASON)
            series_id = self._resolve_series_choice(project.id, payload.series_choice, row)
            version, head_id = service.next_version(series_id, row.project_id)
            service.review_candidate(
                row.id,
                KnowledgeReviewDecisionValue.APPROVE,
                rationale,
                actor=actor,
                series_id=series_id,
                version=version,
                supersedes_fact_id=head_id,
            )
            return "已审定通过该知识候选。"
        if purpose == PURPOSE_KNOWLEDGE_DEACTIVATE_FACT:
            rationale = _require_text(payload.reason, MSG_NEED_REASON)
            service.deactivate_fact(row.id, rationale, actor=actor)
            return "已停用该知识事实。"
        raise _action_unavailable()  # pragma: no cover

    def _resolve_series_choice(
        self, project_id: str, choice: str | None, candidate: KnowledgeCandidate
    ) -> str:
        """Map the owner's business-label series pick back to the internal series id."""
        label = (choice or "").strip()
        if not label or label == "新建系列":
            return f"series-{candidate.id}"
        for series_id, series_label in self._series_labels(project_id).items():
            if series_label == label:
                return series_id
        raise ServiceError(422, "请选择一个知识系列。")


# --------------------------------------------------------------------------
# 7. Small shared guards
# --------------------------------------------------------------------------


def _assert_owner(actor: ActorContext) -> None:
    if actor.kind != "owner" or not actor.owner_id:
        raise ServiceError(403, "owner role required")


def _require_text(value: str | None, message: str) -> str:
    text = (value or "").strip()
    if not text:
        raise ServiceError(422, message)
    return text


def token_ttl_seconds(ring: TokenKeyRing | None = None) -> int:
    """Effective sealed-token TTL (default 900 s, §2.1.1)."""
    return (ring or load_token_key_ring()).ttl_seconds


__all__ = [
    "ACTION_LABELS",
    "ACTION_PURPOSES",
    "COMPANY_SCOPE",
    "DecisionInput",
    "DecisionOption",
    "DecisionResult",
    "INBOX_KINDS",
    "InboxDetail",
    "InboxItem",
    "InboxPage",
    "MSG_ACTION_UNAVAILABLE",
    "MSG_CONTENT_PARTIAL",
    "MSG_KEY_UNAVAILABLE",
    "MSG_MISSING",
    "MSG_NEED_REASON",
    "MSG_STALE",
    "MSG_TOKEN_INVALID",
    "OwnerInboxService",
    "ProjectOption",
    "TokenKey",
    "TokenKeyRing",
    "canonical_facts_binding",
    "canonical_json",
    "load_token_key_ring",
    "project_display_binding",
    "resolve_sealed_token",
    "seal_token",
    "token_ttl_seconds",
]

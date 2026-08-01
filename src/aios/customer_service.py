"""Customer-service / sales-conversion service (#109, V1.2-B).

MVP journey: WeCom conversation ingest (Mock adapter) -> conversation / message
context -> FAQ + approved ``KnowledgeFact``-driven reply suggestion -> escalation
rules (price / payment / promise / complaint / privacy / low-confidence ->
human + audit) -> limited auto-send (only the "routine Q&A" whitelist) ->
lead funnel (visitor / lead / qualified / proposal / won) + human takeover +
follow-up task -> every outbound send audited (who / what / when / channel).

Design invariants (reuse #108-A / #110 primitives):
* All identities server-derived (``ActorContext``); never from a request body.
* Every outbound send writes ``AuditLog`` (``cs.outbound_send``); ``redact_secrets``
  runs automatically inside ``append_audit``.
* Escalation writes ``AuditLog`` (``cs.escalation``); it triggers NO production
  action (no fact creation, no task creation, no auto-reply).
* Suggestions NEVER create a ``KnowledgeFact`` -- they read approved facts only.
* Default deterministic rules (keyword / term-overlap scoring) + rule-based
  escalation matching: ZERO paid LLM calls. Real semantic matching needs an
  explicit credential + flag + cost-owner gate (out of MVP scope).
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from typing import Any

from sqlmodel import Session, select

from aios.actor import ActorContext, _assert_owner_actor
from aios.adapters.wecom import MockWeComAdapter, WeComAdapter
from aios.audit import append_audit
from aios.models import (
    Conversation,
    CsChannel,
    CsSuggestion,
    CsSuggestionDecision,
    KnowledgeFact,
    KnowledgeFactStatus,
    LeadStage,
    Message,
    MessageDirection,
    SenderType,
    Task,
    TaskStatus,
    new_id,
)
from aios.services import ServiceError


# Confidence threshold for the "routine Q&A" auto-send whitelist (#109 §8.4).
# Only an approved-fact hit with confidence >= this AND non-escalation may be
# auto-sent. Env-overridable, mirroring content_draft.REVIEW_PASS_MIN_CONFIDENCE.
#
# Contract D: the threshold MUST be a finite number in [0, 1]. Invalid values
# (NaN, inf, empty, out-of-range) fail closed at import time so a misconfigured
# environment can never widen the auto-send surface to everything.
def _load_auto_send_confidence(raw: str | None = None) -> float:
    if raw is None:
        raw = os.getenv("AIOS_CS_AUTO_SEND_CONFIDENCE", "0.80")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"AIOS_CS_AUTO_SEND_CONFIDENCE must be a finite number in [0, 1], got {raw!r}"
        ) from None
    if not math.isfinite(value) or not (0.0 <= value <= 1.0):
        raise ValueError(
            f"AIOS_CS_AUTO_SEND_CONFIDENCE must be finite and within [0, 1], got {value!r}"
        ) from None
    return value


CS_AUTO_SEND_CONFIDENCE = _load_auto_send_confidence()

# Outbound body is bounded before entering the audit ``after_snapshot`` (plan §6).
_MAX_BODY_BYTES = 16 * 1024  # 16 KiB, mirrors feedback field caps
_AUDIT_BODY_LIMIT = 512

# Canonical audit action strings.
_AUDIT_OUTBOUND = "cs.outbound_send"
_AUDIT_ESCALATION = "cs.escalation"
_AUDIT_LEAD_STAGE = "cs.lead_stage"

# Deterministic escalation lexicon (Chinese + English). A hit on any keyword in
# a category escalates the suggestion to a human regardless of fact confidence.
_ESCALATION_KEYWORDS: dict[str, list[str]] = {
    "price": ["报价", "价格", "多少钱", "收费", "费用", "报价单", "单价", "price", "cost", "quote"],
    "payment": ["付款", "支付", "收款", "转账", "payment", "pay", "钱", "账单"],
    "promise": ["承诺", "保证", "一定", "肯定", "包", "promise", "guarantee", "确保"],
    "complaint": ["投诉", "抱怨", "差评", "不满", "失望", "退款", "complaint", "refund", "维权"],
    "privacy": [
        "隐私",
        "身份证",
        "手机号",
        "银行卡",
        "密码",
        "个人信息",
        "privacy",
        "身份证号",
    ],
}

# PII patterns redacted from outbound bodies before audit persistence (plan §6).
_PII_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PII_MOBILE = re.compile(r"1[3-9]\d{9}")
_PII_IDCARD = re.compile(r"\b\d{17}[\dXx]\b")
_PII_BANKCARD = re.compile(r"\b(?:\d[ -]?){15,19}\b")


# ---------------------------------------------------------------------------
# Helpers (deterministic, zero LLM)
# ---------------------------------------------------------------------------


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def redact_pii(value: Any) -> Any:
    """Replace high-confidence PII patterns with ``[REDACTED-PII]``.

    Mirrors ``feedback.redact_pii``: applied to the outbound body before it
    enters the audit ``after_snapshot`` so customer PII can never persist in the
    audit trail. Storage keeps the original text; only the audit surface is
    redacted (plan §6).
    """
    if isinstance(value, str):
        value = _PII_EMAIL.sub("[REDACTED-PII]", value)
        value = _PII_MOBILE.sub("[REDACTED-PII]", value)
        value = _PII_IDCARD.sub("[REDACTED-PII]", value)
        value = _PII_BANKCARD.sub("[REDACTED-PII]", value)
        return value
    if isinstance(value, list):
        return [redact_pii(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_pii(v) for k, v in value.items()}
    return value


def _bounded_audit_body(body: str) -> str:
    normalized = _nfc(body)
    redacted = redact_pii(normalized)
    if len(redacted) <= _AUDIT_BODY_LIMIT:
        return redacted
    # Keep the whole result within the contract limit: truncate leaving room
    # for the ellipsis so the final length is exactly _AUDIT_BODY_LIMIT.
    return redacted[: _AUDIT_BODY_LIMIT - 1] + "…"


def _tokens(text: str) -> list[str]:
    """Deterministic tokenization: latin/num runs + individual CJK chars."""
    lowered = _nfc(text).lower()
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", lowered)


def _score_fact(fact_statement: str, inbound_text: str) -> float:
    """Recall-based confidence: fraction of *distinct* fact tokens present in
    inbound. Using set cardinality (not a multiset) prevents a fact from
    inflating its own confidence by repeating a token (contract §8.4)."""
    fact_set = set(_tokens(fact_statement))
    if not fact_set:
        return 0.0
    inbound_set = set(_tokens(inbound_text))
    return len(fact_set & inbound_set) / len(fact_set)


def _detect_escalation(text: str) -> list[str]:
    lowered = _nfc(text).lower()
    categories: list[str] = []
    for category, keywords in _ESCALATION_KEYWORDS.items():
        if any(kw.lower() in lowered for kw in keywords):
            categories.append(category)
    return categories


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class CustomerService:
    def __init__(self, session: Session, adapter: WeComAdapter | None = None) -> None:
        self.session = session
        self.adapter: WeComAdapter = adapter or MockWeComAdapter()

    # -- authorization ------------------------------------------------------

    def _load_conversation(self, conversation_id: str) -> Conversation:
        """Load a conversation by id (404 if missing). No owner/agent 403 --
        compute endpoints (suggest / send) operate on a resource id the caller
        already holds; browse endpoints apply the strict per-project guard."""
        conv = self.session.get(Conversation, conversation_id)
        if conv is None:
            raise ServiceError(404, "conversation not found")
        return conv

    def _can_view(self, actor: ActorContext, conv: Conversation) -> bool:
        # Strict per-project guard for browse endpoints: only the trusted owner
        # may list / view conversations. Any agent (related or not) is rejected
        # (plan §4.6 / §6). Human identity == owner; there is no separate agent
        # seat (plan §9).
        return actor.kind == "owner"

    def _assert_can_view(self, actor: ActorContext, conv: Conversation) -> None:
        if not self._can_view(actor, conv):
            raise ServiceError(403, "not authorized to view this conversation")

    # -- conversation lifecycle --------------------------------------------

    def create_conversation(
        self,
        actor: ActorContext,
        *,
        project_id: str,
        channel: CsChannel | None = None,
        external_conversation_ref: str | None = None,
        customer_ref: str | None = None,
    ) -> Conversation:
        if actor.kind != "owner":
            raise ServiceError(403, "only owner may create conversations")
        conv = Conversation(
            project_id=project_id,
            channel=channel or CsChannel.MOCK,
            external_conversation_ref=external_conversation_ref,
            customer_ref=customer_ref or new_id("cust"),
            lead_stage=LeadStage.VISITOR,
        )
        self.session.add(conv)
        self.session.commit()
        self.session.refresh(conv)
        return conv

    def ingest_inbound(
        self,
        actor: ActorContext,
        *,
        project_id: str,
        external_conversation_ref: str | None,
        customer_ref: str | None,
        text: str,
    ) -> Conversation:
        """Webhook-style inbound ingest via the transport adapter."""
        if actor.kind != "owner":
            raise ServiceError(403, "only owner may ingest inbound messages")
        if len(_nfc(text).encode("utf-8")) > _MAX_BODY_BYTES:
            raise ServiceError(422, "message body exceeds 16 KiB")
        conv = self.adapter.receive_inbound(
            self.session,
            project_id=project_id,
            external_conversation_ref=external_conversation_ref,
            customer_ref=customer_ref,
            text=_nfc(text),
        )
        self.session.commit()
        self.session.refresh(conv)
        return conv

    def post_inbound_message(
        self, actor: ActorContext, *, conversation_id: str, text: str
    ) -> Message:
        """API-style inbound message on an existing conversation."""
        if actor.kind != "owner":
            raise ServiceError(403, "only owner may post inbound messages")
        conv = self._load_conversation(conversation_id)
        if len(_nfc(text).encode("utf-8")) > _MAX_BODY_BYTES:
            raise ServiceError(422, "message body exceeds 16 KiB")
        msg = Message(
            conversation_id=conv.id,
            project_id=conv.project_id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.CUSTOMER,
            body=_nfc(text),
        )
        self.session.add(msg)
        self.session.commit()
        self.session.refresh(msg)
        return msg

    def get_conversation(self, actor: ActorContext, *, conversation_id: str) -> Conversation:
        conv = self._load_conversation(conversation_id)
        self._assert_can_view(actor, conv)
        return conv

    def list_conversations(self, actor: ActorContext, *, project_id: str) -> list[Conversation]:
        # Per-project 403: an agent hitting another project's list is rejected
        # wholesale (plan §4.6 / T15). Owner sees all of its project.
        if actor.kind != "owner":
            raise ServiceError(403, "not authorized to list conversations")
        return self.adapter.list_conversations(self.session, project_id=project_id)

    def get_messages(
        self, actor: ActorContext, *, conversation_id: str, limit: int = 100
    ) -> list[Message]:
        conv = self._load_conversation(conversation_id)
        self._assert_can_view(actor, conv)
        if limit <= 0 or limit > 100:
            raise ServiceError(422, "limit must be between 1 and 100")
        return list(
            self.session.exec(
                select(Message)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at)
                .limit(limit)
            ).all()
        )

    # -- suggestion generation (deterministic, zero LLM) --------------------

    def _load_approved_facts(self, project_id: str) -> list[KnowledgeFact]:
        return list(
            self.session.exec(
                select(KnowledgeFact).where(
                    KnowledgeFact.status == KnowledgeFactStatus.APPROVED,
                    (KnowledgeFact.project_id == project_id)
                    | (KnowledgeFact.project_id.is_(None)),  # company-wide scope
                )
            ).all()
        )

    def _assert_can_act(self, actor: ActorContext, conv: Conversation) -> None:
        # Contract B: compute actions (suggestion generation, auto-send) require
        # an actor authorized for the conversation's project.
        #   * owner  -> always authorized (trusted, server-derived identity).
        #   * agent  -> authorized ONLY when it carries an explicit project scope
        #               that matches the conversation's project. An unscoped
        #               agent (project_id=None) or one scoped to a *different*
        #               project is rejected with 403 (fail-closed). This closes
        #               the cross-project act gap: a project-X agent can never
        #               drive compute on a project-Y conversation.
        if actor.kind == "owner":
            return
        if (
            actor.kind == "agent"
            and actor.project_id is not None
            and actor.project_id == conv.project_id
        ):
            return
        raise ServiceError(403, "actor not authorized for this conversation's project")

    def generate_suggestion(
        self,
        actor: ActorContext,
        *,
        conversation_id: str,
        inbound_message_id: str | None,
        text: str,
    ) -> CsSuggestion:
        conv = self._load_conversation(conversation_id)
        self._assert_can_act(actor, conv)
        if len(_nfc(text).encode("utf-8")) > _MAX_BODY_BYTES:
            raise ServiceError(422, "message body exceeds 16 KiB")
        text = _nfc(text)

        facts = self._load_approved_facts(conv.project_id)
        scored = [(f, _score_fact(f.statement, text)) for f in facts]
        scored = [(f, c) for f, c in scored if c > 0.0]
        scored.sort(key=lambda x: x[1], reverse=True)
        best = scored[0] if scored else None
        best_conf = best[1] if best else 0.0

        escalation_categories = _detect_escalation(text)
        if best is not None:
            fact_refs = [f.id for f, _ in scored]
            fact_revisions = {f.id: f.version for f, _ in scored}
            suggestion_text = best[0].statement
        else:
            fact_refs = []
            fact_revisions = {}
            suggestion_text = "（已转人工处理）"

        # Decision derivation -- the single source of truth (plan §4.2.4).
        if escalation_categories:
            decision = CsSuggestionDecision.ESCALATE
        elif best is not None and best_conf >= CS_AUTO_SEND_CONFIDENCE:
            decision = CsSuggestionDecision.AUTO_SEND
        elif best is not None:
            decision = CsSuggestionDecision.HUMAN_CONFIRM
        else:
            decision = CsSuggestionDecision.ESCALATE
            escalation_categories = escalation_categories or ["low_confidence"]

        sug = CsSuggestion(
            conversation_id=conv.id,
            project_id=conv.project_id,
            decision=decision,
            text=suggestion_text,
            confidence=best_conf if best is not None else None,
            escalation_categories=escalation_categories,
            knowledge_fact_refs=fact_refs,
            fact_revisions=fact_revisions,
            consumed=False,
            idempotency_key=new_id("csid"),
        )
        self.session.add(sug)
        self.session.flush()

        if decision == CsSuggestionDecision.ESCALATE:
            # Escalation writes an internal escalation Message + audit; it
            # triggers no production action (plan §4.4 / T11).
            esc_msg = Message(
                conversation_id=conv.id,
                project_id=conv.project_id,
                direction=MessageDirection.OUTBOUND,
                sender_type=SenderType.AGENT,
                body=suggestion_text,
                is_auto_sent=False,
                escalation_flag=True,
                escalation_categories=escalation_categories,
                knowledge_fact_refs=fact_refs,
            )
            self.session.add(esc_msg)
            self.session.flush()
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=_AUDIT_ESCALATION,
                resource_type="cs_suggestion",
                resource_id=sug.id,
                project_id=conv.project_id,
                task_id=None,
                before={},
                after={
                    "categories": escalation_categories,
                    "reason": "auto",
                    "conversation_id": conv.id,
                    "message_id": inbound_message_id,
                },
                idempotency_key=f"audit:cs:escalation:{sug.id}",
            )

        self.session.commit()
        self.session.refresh(sug)
        return sug

    # -- limited auto-send + human send ------------------------------------

    def send_message(
        self,
        actor: ActorContext,
        *,
        conversation_id: str,
        text: str,
        auto_send: bool,
        suggestion_id: str | None = None,
    ) -> Message:
        if auto_send:
            return self._auto_send(actor, conversation_id, text, suggestion_id)
        return self._human_send(actor, conversation_id, text)

    def _human_send(
        self, actor: ActorContext, conversation_id: str, text: str
    ) -> Message:
        # Human path: ONLY the trusted owner may send arbitrary text (plan §4.3).
        _assert_owner_actor(actor)
        conv = self._load_conversation(conversation_id)
        if len(_nfc(text).encode("utf-8")) > _MAX_BODY_BYTES:
            raise ServiceError(422, "message body exceeds 16 KiB")
        text = _nfc(text)

        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        try:
            msg = self.adapter.send_message(
                self.session,
                conversation_id=conv.id,
                text=text,
                sender_type=SenderType.OWNER,
                is_auto_sent=False,
            )
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=_AUDIT_OUTBOUND,
                resource_type="message",
                resource_id=msg.id,
                project_id=conv.project_id,
                task_id=None,
                before={},
                after={
                    "channel": conv.channel,
                    "direction": "outbound",
                    "is_auto_sent": False,
                    "sender_type": "owner",
                    "body": _bounded_audit_body(text),
                    "message_id": msg.id,
                    "conversation_id": conv.id,
                },
                idempotency_key=f"audit:cs:outbound:human:{msg.id}",
            )
            self.session.commit()
        except Exception as exc:
            # Fail-closed delivery: roll back and surface 502 (contract A).
            self.session.rollback()
            raise ServiceError(502, "outbound delivery failed") from exc
        self.session.refresh(msg)
        return msg

    def _auto_send(
        self,
        actor: ActorContext,
        conversation_id: str,
        text: str,
        suggestion_id: str | None,
    ) -> Message:
        # Auto-send: an AI agent may request it, but it can NEVER inject
        # arbitrary text -- the suggestion binds the exact copy and the
        # knowledge facts are re-checked for staleness (plan §4.3).
        if suggestion_id is None:
            raise ServiceError(409, "auto_send requires a bound suggestion_id")
        conv = self._load_conversation(conversation_id)
        self._assert_can_act(actor, conv)
        if len(_nfc(text).encode("utf-8")) > _MAX_BODY_BYTES:
            raise ServiceError(422, "message body exceeds 16 KiB")
        text = _nfc(text)

        self.session.rollback()
        self.session.connection().exec_driver_sql("BEGIN IMMEDIATE")

        sug = self.session.get(CsSuggestion, suggestion_id)
        if sug is None or sug.conversation_id != conv.id:
            raise ServiceError(404, "suggestion not found")
        if sug.decision != CsSuggestionDecision.AUTO_SEND:
            raise ServiceError(409, "suggestion is not auto-send eligible")
        # Re-validate the full AUTO_SEND contract at send time (plan §8.4): an
        # auto-send must still bind a knowledge fact and clear the *active*
        # confidence threshold. This guards against stale suggestions created
        # under a looser threshold, or with their facts since removed.
        if not sug.knowledge_fact_refs:
            raise ServiceError(409, "auto-send suggestion has no bound knowledge fact")
        if sug.confidence < CS_AUTO_SEND_CONFIDENCE:
            raise ServiceError(409, "auto-send confidence below active threshold")
        if sug.consumed:
            # Idempotent replay guard: a second send on the same suggestion 409s.
            raise ServiceError(409, "suggestion already consumed")
        if _nfc(sug.text) != text:
            # Bind immutable suggested copy; reject any divergence (P1-1).
            raise ServiceError(409, "suggestion text mismatch")

        # Stale-knowledge re-check: if any referenced fact was revoked /
        # superseded / re-versioned, refuse to auto-send on stale ground (P1-2).
        for fact_id in sug.knowledge_fact_refs:
            expected_version = (sug.fact_revisions or {}).get(fact_id)
            fact = self.session.exec(
                select(KnowledgeFact)
                .where(KnowledgeFact.id == fact_id)
                .execution_options(populate_existing=True)
            ).first()
            if (
                fact is None
                or fact.status != KnowledgeFactStatus.APPROVED
                or fact.version != expected_version
            ):
                raise ServiceError(409, "stale knowledge fact")

        sug.consumed = True
        try:
            msg = self.adapter.send_message(
                self.session,
                conversation_id=conv.id,
                text=text,
                sender_type=SenderType.AGENT,
                is_auto_sent=True,
                confidence=sug.confidence,
                escalation_flag=False,
                escalation_categories=[],
                knowledge_fact_refs=sug.knowledge_fact_refs,
            )
            append_audit(
                self.session,
                actor=actor.derive_submitted_by(),
                action=_AUDIT_OUTBOUND,
                resource_type="message",
                resource_id=msg.id,
                project_id=conv.project_id,
                task_id=None,
                before={},
                after={
                    "channel": conv.channel,
                    "direction": "outbound",
                    "is_auto_sent": True,
                    "sender_type": "agent",
                    "body": _bounded_audit_body(text),
                    "message_id": msg.id,
                    "conversation_id": conv.id,
                    "suggestion_id": sug.id,
                },
                idempotency_key=f"audit:cs:outbound:auto:{msg.id}",
            )
            self.session.commit()
        except Exception as exc:
            # Contract A (one-shot atomicity) + fail-closed delivery: if the
            # transport adapter fails or raises, roll back the consumed flag and
            # the pending Message/audit rather than leaving the transaction in
            # an ambiguous state. Surface a 502 so the caller can safely retry
            # (replay only 409s after a genuine success).
            self.session.rollback()
            raise ServiceError(502, "outbound delivery failed") from exc
        self.session.refresh(msg)
        return msg

    # -- explicit escalation (human-forced) --------------------------------

    def escalate(
        self,
        actor: ActorContext,
        *,
        conversation_id: str,
        categories: list[str] | None = None,
        reason: str = "manual",
    ) -> Message:
        # Manual escalation is an owner-only action (plan §4.4 / §5): the
        # trusted human forces a hand-off. AI agents must be rejected with 403.
        _assert_owner_actor(actor)
        conv = self._load_conversation(conversation_id)
        cats = categories or ["unknown"]
        esc_msg = Message(
            conversation_id=conv.id,
            project_id=conv.project_id,
            direction=MessageDirection.OUTBOUND,
            sender_type=SenderType.AGENT,
            body="（已转人工处理）",
            is_auto_sent=False,
            escalation_flag=True,
            escalation_categories=cats,
        )
        self.session.add(esc_msg)
        self.session.flush()
        append_audit(
            self.session,
            actor=actor.derive_submitted_by(),
            action=_AUDIT_ESCALATION,
            resource_type="conversation",
            resource_id=conv.id,
            project_id=conv.project_id,
            task_id=None,
            before={},
            after={
                "categories": cats,
                "reason": reason,
                "conversation_id": conv.id,
                "message_id": esc_msg.id,
            },
            idempotency_key=f"audit:cs:escalation:manual:{esc_msg.id}",
        )
        self.session.commit()
        self.session.refresh(esc_msg)
        return esc_msg

    # -- lead funnel (owner only) ------------------------------------------

    def set_lead_stage(
        self,
        actor: ActorContext,
        *,
        conversation_id: str,
        stage: LeadStage,
        reason: str = "",
    ) -> Conversation:
        _assert_owner_actor(actor)
        conv = self._load_conversation(conversation_id)
        before_stage = conv.lead_stage
        if before_stage == stage:
            return conv
        conv.lead_stage = stage
        self.session.add(conv)
        self.session.flush()
        append_audit(
            self.session,
            actor=actor.derive_submitted_by(),
            action=_AUDIT_LEAD_STAGE,
            resource_type="conversation",
            resource_id=conv.id,
            project_id=conv.project_id,
            task_id=None,
            before={"lead_stage": before_stage},
            after={"lead_stage": stage, "reason": reason},
            idempotency_key=f"audit:cs:lead_stage:{conv.id}:{before_stage}:{stage}",
        )
        self.session.commit()
        self.session.refresh(conv)
        return conv

    def assign_human(
        self, actor: ActorContext, *, conversation_id: str
    ) -> Conversation:
        _assert_owner_actor(actor)
        conv = self._load_conversation(conversation_id)
        conv.assigned_human = actor.owner_id or "owner"
        self.session.add(conv)
        self.session.commit()
        self.session.refresh(conv)
        return conv

    def create_followup_task(
        self,
        actor: ActorContext,
        *,
        conversation_id: str,
        title: str,
    ) -> Task:
        # Owner-explicit follow-up task. NEVER auto-created (plan §4.5 / T14).
        _assert_owner_actor(actor)
        conv = self._load_conversation(conversation_id)
        task = Task(
            project_id=conv.project_id,
            title=title,
            description=(
                f"CS follow-up for conversation {conv.id} "
                f"(customer {conv.customer_ref}, stage {conv.lead_stage})"
            ),
            status=TaskStatus.BACKLOG,
        )
        self.session.add(task)
        self.session.commit()
        self.session.refresh(task)
        return task

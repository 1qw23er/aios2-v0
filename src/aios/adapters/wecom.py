"""WeCom (WeChat Work / WeChat Public) transport adapter for #109 (V1.2-B).

Defines the transport boundary ``WeComAdapter`` (ABC) plus the MVP
``MockWeComAdapter`` which writes directly to the database with no external
API and no real WeCom credentials. A future ``WeComAppAdapter`` /
``WeComKfAdapter`` (real WeChat Work application-message API / WeChat Customer
Service API) will implement the same interface without touching the service
layer.

Design note: the service layer (``aios.customer_service``) owns all business
logic, guards, decisioning and audit. This adapter only persists / delivers the
conversation transport records into the caller's ``Session`` -- it never
commits, so guard + message + audit stay atomic inside the service's single
``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import abc

from sqlmodel import Session, select

from aios.models import (
    Conversation,
    CsChannel,
    Message,
    MessageDirection,
    SenderType,
    new_id,
)


class WeComAdapter(abc.ABC):
    """Transport boundary between the CS service and WeCom.

    Every method receives the caller's ``Session`` and writes ORM objects into
    it (never commits). All three methods are deterministic and side-effect
    free apart from the DB rows they stage.
    """

    @abc.abstractmethod
    def receive_inbound(
        self,
        session: Session,
        *,
        project_id: str,
        external_conversation_ref: str | None,
        customer_ref: str | None,
        text: str,
    ) -> Conversation:
        """Ingest an inbound customer message; return its Conversation."""

    @abc.abstractmethod
    def send_message(
        self,
        session: Session,
        *,
        conversation_id: str,
        text: str,
        sender_type: SenderType,
        is_auto_sent: bool,
        confidence: float | None = None,
        escalation_flag: bool = False,
        escalation_categories: list[str] | None = None,
        knowledge_fact_refs: list[str] | None = None,
    ) -> Message:
        """Persist an outbound Message row; return it."""

    @abc.abstractmethod
    def list_conversations(
        self, session: Session, *, project_id: str
    ) -> list[Conversation]:
        """List conversations for a project (oldest first)."""


class MockWeComAdapter(WeComAdapter):
    """In-memory / DB-direct Mock adapter (MVP default).

    No external API, no real WeCom credentials -- CI stays green and the whole
    pipeline (conversation / reply / escalation / audit / funnel) is exercised
    independently. The Mock is idempotent on ``external_conversation_ref``:
    repeating the same external ref within a project re-uses the conversation.
    """

    def receive_inbound(
        self,
        session: Session,
        *,
        project_id: str,
        external_conversation_ref: str | None,
        customer_ref: str | None,
        text: str,
    ) -> Conversation:
        conv = None
        if external_conversation_ref is not None:
            conv = session.exec(
                select(Conversation).where(
                    Conversation.project_id == project_id,
                    Conversation.external_conversation_ref == external_conversation_ref,
                )
            ).first()
        if conv is None:
            conv = Conversation(
                project_id=project_id,
                channel=CsChannel.MOCK,
                external_conversation_ref=external_conversation_ref,
                customer_ref=customer_ref or new_id("cust"),
            )
            session.add(conv)
            session.flush()
        msg = Message(
            conversation_id=conv.id,
            project_id=project_id,
            direction=MessageDirection.INBOUND,
            sender_type=SenderType.CUSTOMER,
            body=text,
        )
        session.add(msg)
        session.flush()
        return conv

    def send_message(
        self,
        session: Session,
        *,
        conversation_id: str,
        text: str,
        sender_type: SenderType,
        is_auto_sent: bool,
        confidence: float | None = None,
        escalation_flag: bool = False,
        escalation_categories: list[str] | None = None,
        knowledge_fact_refs: list[str] | None = None,
    ) -> Message:
        project_id = session.exec(
            select(Conversation.project_id).where(Conversation.id == conversation_id)
        ).one()
        msg = Message(
            conversation_id=conversation_id,
            project_id=project_id,
            direction=MessageDirection.OUTBOUND,
            sender_type=sender_type,
            body=text,
            confidence=confidence,
            is_auto_sent=is_auto_sent,
            escalation_flag=escalation_flag,
            escalation_categories=escalation_categories or [],
            knowledge_fact_refs=knowledge_fact_refs or [],
        )
        session.add(msg)
        session.flush()
        return msg

    def list_conversations(
        self, session: Session, *, project_id: str
    ) -> list[Conversation]:
        return list(
            session.exec(
                select(Conversation)
                .where(Conversation.project_id == project_id)
                .order_by(Conversation.created_at)
            ).all()
        )

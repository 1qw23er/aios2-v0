"""Aimi lead-gen attribution (gap #2, EXPERIMENT #1) -- ZERO-MIGRATION.

This module closes the open loop between *published content* and *Aimi
signups* WITHOUT adding any new database table. Attribution metrics land in the
existing immutable ``AuditLog`` table (action ``content.aimi_attribution``),
reusing the same append-only, credential-safe, idempotency-keyed primitive that
every other retrospective metric in the system uses. This honors the audit
invariant -- the audit log is the system of record for non-authoritative,
retrospective metrics -- and prevents infrastructure creep / migration drift.

Two responsibilities:

1. Mint a per-article ``attribution_key`` and turn it into a trackable Aimi
   signup URL (the CTA payload that goes into the published article).
2. Record and aggregate signup counts attributed to a given article.

Identity rule: every function that writes an audit row takes a *server-derived*
``actor`` string (exactly like ``append_audit`` does). This module NEVER
resolves an identity itself and NEVER accepts identity from a request body --
the caller passes the already-resolved ``actor`` it received from the
authentication boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from aios.audit import AuditLog, append_audit
from aios.models import new_id

#: Audit action used for every Aimi attribution observation. All such rows are
#: aggregated by ``sum_aimi_attributions``; no new table is introduced.
AIMI_ATTRIBUTION_AUDIT = "content.aimi_attribution"

#: Default Aimi signup landing page. Override via ``AIMI_SIGNUP_BASE_URL`` when
#: the production register endpoint changes -- never hardcode the URL at call
#: sites, always go through ``build_aimi_signup_url``.
_DEFAULT_SIGNUP_BASE_URL = "https://aimi.quantv.com/register"


def _slugify(text: str | None) -> str:
    """Make an URL-safe, ASCII slug from a topic.

    Chinese / non-ASCII topics have no ASCII representation, so we fall back to
    a short stable hash of the original text (``t<hex6>``) rather than emitting
    empty or mojibake slugs. The trailing random hex in ``generate_attribution_key``
    still guarantees cross-article uniqueness.
    """
    ascii_part = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if not ascii_part:
        digest = hashlib.sha256((text or "draft").encode("utf-8")).hexdigest()[:6]
        ascii_part = f"t{digest}"
    # Cap keeps the key compact; the random hex suffix still guarantees
    # per-article uniqueness even if two long topics share a 48-char prefix.
    return ascii_part[:48]


def generate_attribution_key(topic: str | None = None) -> str:
    """Mint a per-article attribution key.

    Format: ``aios-<slug>-<hex8>`` where ``<hex8>`` is 8 bytes of
    ``secrets.token_hex`` (non-deterministic, collision-resistant). The key is
    safe to embed in a URL query parameter (``?ref=<key>``).

    Determinism note: the slug component is deterministic per topic, but the
    random suffix makes every call unique, so two drafts on the same topic get
    distinct keys (required for per-article attribution).

    Format uses underscores as the *component* separator and dashes only
    *inside* the slug, so ``key.split("_")`` always yields exactly
    ``["aios", <slug>, <hex8>]`` -- no ambiguity even when the slug itself
    contains dashes.
    """
    slug = _slugify(topic)
    rand = secrets.token_hex(4)  # 8 hex chars
    return f"aios_{slug}_{rand}"


def build_aimi_signup_url(attribution_key: str, base_url: str | None = None) -> str:
    """Build the trackable Aimi signup URL carrying ``attribution_key``.

    ``base_url`` defaults to the ``AIMI_SIGNUP_BASE_URL`` env var or the
    production register endpoint. The key is appended as the ``ref`` query
    parameter. An explicit ``base_url`` argument always wins over the env var.
    """
    base = (base_url or "").strip()
    if not base:
        base = os.environ.get("AIMI_SIGNUP_BASE_URL", _DEFAULT_SIGNUP_BASE_URL)
    base = base.rstrip("/")
    # ``ref`` is the canonical attribution query param consumed by Aimi.
    return f"{base}?ref={attribution_key}"


def _iso(value: Any) -> str | None:
    """Serialize a datetime to ISO-8601 UTC string; pass through None/str."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone().isoformat()
    return str(value)


def record_aimi_attribution(
    session: Session,
    *,
    project_id: str,
    actor: str,
    artifact_id: str,
    registrations: int,
    attribution_key: str | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    task_id: str | None = None,
    note: str | None = None,
) -> AuditLog:
    """Record one attribution observation (a signup-count snapshot) for an article.

    Lands an immutable ``content.aimi_attribution`` audit row. Each call is a
    discrete observation -- the unique ``idempotency_key`` embeds a fresh id so
    repeated weekly snapshots for the same article do not collide.

    Fail-closed: ``registrations`` MUST be an int; non-int values raise
    ``TypeError`` rather than silently persisting garbage. ``attribution_key``
    may be None (e.g. when the observation is keyed only by artifact).
    """
    if not isinstance(registrations, int) or isinstance(registrations, bool):
        raise TypeError(
            f"registrations must be an int, got {type(registrations).__name__}"
        )
    if not attribution_key:
        # Best-effort recovery: read the key minted at draft creation.
        artifact = session.get(_artifact_cls(), artifact_id)
        if artifact is not None:
            attribution_key = (artifact.metadata_json or {}).get("attribution_key")
    after: dict[str, Any] = {
        "registrations": registrations,
        "attribution_key": attribution_key,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "note": note,
    }
    idempotency_key = f"audit:aimi_attribution:{artifact_id}:{new_id('attr')}"
    return append_audit(
        session,
        actor=actor,
        action=AIMI_ATTRIBUTION_AUDIT,
        resource_type="artifact",
        resource_id=artifact_id,
        project_id=project_id,
        task_id=task_id,
        before={},
        after=after,
        idempotency_key=idempotency_key,
    )


def sum_aimi_attributions(session: Session, artifact_id: str) -> int:
    """Aggregate total attributed Aimi signups for an article (fail-closed).

    Reads every ``content.aimi_attribution`` audit row for ``artifact_id`` and
    sums ``after.registrations``. Rows whose snapshot is missing / malformed /
    non-int are counted as 0 (never raise) so a partial bad record cannot break
    the aggregate or inflate the number.
    """
    stmt = select(AuditLog).where(
        AuditLog.action == AIMI_ATTRIBUTION_AUDIT,
        AuditLog.resource_id == artifact_id,
    )
    total = 0
    for row in session.exec(stmt):
        regs = (row.after_snapshot or {}).get("registrations")
        if isinstance(regs, int) and not isinstance(regs, bool):
            total += regs
    return total


def _artifact_cls():  # local import shim to avoid a top-level cycle
    from aios.models import Artifact

    return Artifact

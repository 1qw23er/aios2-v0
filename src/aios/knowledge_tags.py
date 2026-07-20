"""Canonical knowledge tag registry + capability projection (Phase A, #67).

Design rules (locked by architecture review):

- ``CANONICAL_KNOWLEDGE_TAGS`` is the single source of truth for valid tags. It
  is a controlled mirror of the real ``V1_CAPABILITIES`` slugs and does NOT
  dynamically absorb new DB ``Capability`` rows -- a new capability becomes a
  legal tag only through review + a version bump of ``CAPABILITY_TAG_MAP_VERSION``.
- ``CAPABILITY_KNOWLEDGE_TAGS`` maps a capability slug to the set of knowledge
  tags it is allowed to see. Identity mapping for v1.
- ``normalize_tags`` rejects unknown tags (422), dedupes and sorts (deterministic).
- ``is_legacy_unclassified`` is the JSON-safe sentinel detector used by the
  backfill, the readiness gate and the DB triggers.
- Feature flag + readiness helpers implement fail-closed least-privilege rollout.
"""

from __future__ import annotations

import os

from sqlalchemy import func
from sqlmodel import Session, select

from aios.campaign import V1_CAPABILITIES
from aios.models import KnowledgeFact, KnowledgeFactStatus
from aios.services import ServiceError

# Sentinel used to backfill legacy untagged knowledge. NOT in CANONICAL_KNOWLEDGE_TAGS,
# so it is excluded from any projection until the owner classifies it.
LEGACY_UNCLASSIFIED = "__legacy_unclassified__"

CANONICAL_KNOWLEDGE_TAGS: frozenset[str] = frozenset(
    {
        "user_research",
        "positioning",
        "wechat_writing",
        "xhs_adaptation",
        "video_script",
        "packaging",
        "knowledge_capture",
    }
)

# Versioned, reviewed code configuration. Bump only after a reviewed change.
CAPABILITY_TAG_MAP_VERSION = "2026-07-20"

CAPABILITY_KNOWLEDGE_TAGS: dict[str, frozenset[str]] = {
    "user_research": frozenset({"user_research"}),
    "positioning": frozenset({"positioning"}),
    "wechat_writing": frozenset({"wechat_writing"}),
    "xhs_adaptation": frozenset({"xhs_adaptation"}),
    "video_script": frozenset({"video_script"}),
    "packaging": frozenset({"packaging"}),
    "knowledge_capture": frozenset({"knowledge_capture"}),
}


def normalize_tags(raw: list[str] | None) -> list[str]:
    """Validate, dedupe and sort tags deterministically.

    Rejects any tag not in ``CANONICAL_KNOWLEDGE_TAGS`` with 422. The canonical
    tag validation lives here (service layer) -- the DB trigger only judges the
    *shape* of the sentinel->canonical transition, never the tag vocabulary.
    """
    if raw is None:
        return []
    cleaned: list[str] = []
    for item in raw:
        tag = item.strip().lower()
        if not tag:
            continue
        if tag not in CANONICAL_KNOWLEDGE_TAGS:
            raise ServiceError(422, f"unknown knowledge tag: {tag!r}")
        cleaned.append(tag)
    return sorted(set(cleaned))


def is_legacy_unclassified(tags: list[str] | None) -> bool:
    """JSON-safe sentinel detector: exactly one element equal to the sentinel.

    Mirrors the SQLite check
    ``json_array_length(tags) = 1 AND json_extract(tags, '$[0]') = :sentinel``
    and must be kept semantically identical.
    """
    return tags is not None and len(tags) == 1 and tags[0] == LEGACY_UNCLASSIFIED


def assert_tag_registry_consistent() -> None:
    """Code -> DB existence check (NOT DB -> code).

    Every capability referenced by ``CAPABILITY_KNOWLEDGE_TAGS`` must exist in
    the seeded ``V1_CAPABILITIES`` definitions, and every mapped tag must be a
    member of ``CANONICAL_KNOWLEDGE_TAGS``.
    """
    seeded = {cap["name"] for cap in V1_CAPABILITIES}
    for cap, tags in CAPABILITY_KNOWLEDGE_TAGS.items():
        if cap not in seeded:
            raise ServiceError(
                500, f"capability tag map references unknown capability: {cap}"
            )
        for tag in tags:
            if tag not in CANONICAL_KNOWLEDGE_TAGS:
                raise ServiceError(
                    500, f"capability tag map references unknown tag: {tag}"
                )


def is_projection_enabled() -> bool:
    """Feature flag: least-privilege projection is OFF by default (fail-closed)."""
    return (
        os.getenv("KNOWLEDGE_LEAST_PRIVILEGE_ENABLED", "").strip().lower()
        in {"1", "true", "yes"}
    )


def _unclassified_approved_stmt():
    return (
        select(KnowledgeFact)
        .where(KnowledgeFact.status == KnowledgeFactStatus.APPROVED)
        .where(func.json_array_length(KnowledgeFact.tags) == 1)
        .where(func.json_extract(KnowledgeFact.tags, "$[0]") == LEGACY_UNCLASSIFIED)
    )


def count_unclassified_approved(session: Session) -> int:
    """JSON-safe count of approved facts still carrying the legacy sentinel.

    Uses ``json_array_length`` + ``json_extract`` (never ``LIKE`` / raw string
    equality) so an empty tag list or a coincidental substring can never match.
    """
    stmt = (
        select(func.count())
        .select_from(KnowledgeFact)
        .where(KnowledgeFact.status == KnowledgeFactStatus.APPROVED)
        .where(func.json_array_length(KnowledgeFact.tags) == 1)
        .where(func.json_extract(KnowledgeFact.tags, "$[0]") == LEGACY_UNCLASSIFIED)
    )
    return int(session.exec(stmt).one())


def ensure_knowledge_projection_ready(session: Session) -> None:
    """Refuse activation while any approved sentinel fact remains.

    Called on startup, on every flag flip, and before every projected
    ``TaskContext`` build when the flag is enabled. Prevents partial exposure of
    unclassified knowledge.
    """
    if is_projection_enabled() and count_unclassified_approved(session) > 0:
        raise ServiceError(
            409,
            "knowledge projection cannot activate while unclassified approved "
            "facts remain; classify them via classify_knowledge first",
        )


def report_unclassified_knowledge(session: Session) -> list[dict]:
    """Owner-visible list of remaining sentinel facts (for gradual classification)."""
    rows = session.exec(_unclassified_approved_stmt()).all()
    return [
        {
            "fact_id": row.id,
            "series_id": row.series_id,
            "version": row.version,
            "project_id": row.project_id,
            "source_project_id": row.source_project_id,
        }
        for row in rows
    ]

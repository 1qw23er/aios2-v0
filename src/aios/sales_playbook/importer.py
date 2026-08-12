"""Idempotent EBF sales-script importer (design §2 / §3 / §4 / §9, D1-D3).

This is a MANAGEMENT COMMAND, never an Alembic migration: the schema is created
by ``20260810_0001_sales_playbook_v0``, the *content* is loaded by calling
:func:`import_package`. Running it twice with the same package is a no-op.

Input contract
--------------
``export``
    The canonical structured export of the EBF package (a plain JSON-able
    mapping). Its exact bytes participate in ``extraction_manifest_hash``.
``raw_ebf_bytes``
    The original ``.ebf`` file bytes. Hashed into ``source_file_hash``.
``image_bytes``
    ``{image_ref: bytes}``. The importer hashes the BYTES itself rather than
    trusting a checksum supplied by the payload, which is what makes D1's
    "an image changed while its filename did not" detection real.

The real first package (``02_完整结构化导出.json``) is not present in this
sandbox, so every test drives the importer through this same fixture-shaped
interface. The first-package acceptance numbers (150 entries / 61·82·7 scope
split / 74 unique images / 76 references) are recorded as *audit data* in
``SalesScriptSource.metadata_json``; per design §10 cleanup A they are NEVER
hard-coded as importer constants, so any later package may have any counts.

Fail-closed validation (a corrupt package must not import "mostly fine"):
duplicate official entry id, dangling image reference, non-contiguous segment
ordering, cross-version fact binding, unknown source type, a fact span that is
not deterministically resolvable in the entry's own TEXT segments (P1-1), or a
payload trying to smuggle a fact ``status`` all raise
:class:`SalesPlaybookImportError` and roll the whole transaction back.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from aios.models import (
    SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
    SALES_SCRIPT_SOURCE_TYPES,
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    SalesScriptEntry,
    SalesScriptFactBinding,
    SalesScriptFactClass,
    SalesScriptFactStatus,
    SalesScriptScope,
    SalesScriptSegment,
    SalesScriptSegmentType,
    SalesScriptSource,
    SalesScriptSourceStatus,
)
from aios.sales_playbook.retrieval import suppress_span

__all__ = [
    "ImportResult",
    "SalesPlaybookImportError",
    "compute_extraction_manifest_hash",
    "compute_source_file_hash",
    "import_package",
]

# Field separator for every hash pre-image. U+001F (UNIT SEPARATOR) cannot occur
# in the normalised text we hash, so "a" + "bc" and "ab" + "c" can never collide.
_FS = "\x1f"
_RS = "\x1e"


class SalesPlaybookImportError(Exception):
    """A package is corrupt or violates an import contract. Nothing is written."""


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one :func:`import_package` call."""

    source_id: str
    source_file_hash: str
    extraction_manifest_hash: str
    skipped: bool
    entry_count: int = 0
    segment_count: int = 0
    artifact_count: int = 0
    fact_binding_count: int = 0
    superseded_source_id: str | None = None
    scope_distribution: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# normalisation + hashing
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    """NFC-normalise and trim -- applied to HASH INPUT ONLY.

    Stored text stays verbatim (D2: the entry is the official wording), so this
    never rewrites what an owner will read; it only makes the immutability proof
    insensitive to invisible encoding differences between two extractions.
    """
    return unicodedata.normalize("NFC", value).strip()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_source_file_hash(raw_ebf_bytes: bytes) -> str:
    """``source_file_hash`` = SHA256(raw EBF bytes) (design D1, anchor 1)."""
    return _sha256_hex(raw_ebf_bytes)


def compute_extraction_manifest_hash(
    export: Mapping[str, Any],
    image_bytes: Mapping[str, bytes],
    reference_mapping: Sequence[tuple[str, int, str]],
) -> str:
    """``extraction_manifest_hash`` (design D1, anchor 2).

    SHA256 over three ordered, independently-corruptible parts:

    1. the canonical export JSON,
    2. the image manifest ``ref -> SHA256(image bytes)`` sorted by ref,
    3. the image-reference mapping ``(source_entry_id, sequence, ref)`` sorted.

    Part 2 is what detects a swapped image whose filename never changed; part 3
    is what detects the same images being re-pointed at different entries.
    """
    manifest_lines = [
        f"{ref}{_FS}{_sha256_hex(image_bytes[ref])}" for ref in sorted(image_bytes)
    ]
    mapping_lines = [
        f"{entry_id}{_FS}{sequence}{_FS}{ref}"
        for entry_id, sequence, ref in sorted(reference_mapping)
    ]
    pre_image = (
        _canonical_json(export)
        + _RS.encode("utf-8")
        + _RS.join(manifest_lines).encode("utf-8")
        + _RS.encode("utf-8")
        + _RS.join(mapping_lines).encode("utf-8")
    )
    return _sha256_hex(pre_image)


def compute_entry_source_hash(
    normalised_segments: Sequence[tuple[int, str, str]],
) -> str:
    """``entry.source_hash`` = SHA256(segments normalised, ordered by sequence).

    Each tuple is ``(sequence, kind, payload)`` where ``payload`` is the
    normalised text for a TEXT segment and ``SHA256(image bytes)‖caption`` for an
    IMAGE segment -- so replacing an image's CONTENT changes the entry hash even
    though the ordering and captions are untouched (D1/D2 immutability proof).
    """
    lines = [
        f"{sequence}{_FS}{kind}{_FS}{payload}"
        for sequence, kind, payload in sorted(normalised_segments)
    ]
    return _sha256_hex(_RS.join(lines).encode("utf-8"))


def compute_binding_hash(entry_id: str, fact_key: str, raw_span: str) -> str:
    """``binding_hash`` = SHA256(entry_id ‖ fact_key ‖ normalised raw_span) (D3)."""
    pre_image = f"{entry_id}{_FS}{_norm(fact_key)}{_FS}{_norm(raw_span)}"
    return _sha256_hex(pre_image.encode("utf-8"))


# ---------------------------------------------------------------------------
# payload validation
# ---------------------------------------------------------------------------


def _require_str(container: Mapping[str, Any], key: str, where: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SalesPlaybookImportError(f"{where}: missing or empty '{key}'")
    return value


def _parse_scope(value: Any, where: str) -> SalesScriptScope:
    try:
        return SalesScriptScope(value)
    except ValueError as exc:
        raise SalesPlaybookImportError(
            f"{where}: unknown product scope {value!r}"
        ) from exc


def _parse_fact_class(value: Any, where: str) -> SalesScriptFactClass:
    try:
        return SalesScriptFactClass(value)
    except ValueError as exc:
        raise SalesPlaybookImportError(
            f"{where}: unknown fact class {value!r}"
        ) from exc


def _validated_segments(
    entry_payload: Mapping[str, Any],
    where: str,
    image_bytes: Mapping[str, bytes],
) -> list[dict[str, Any]]:
    raw_segments = entry_payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise SalesPlaybookImportError(f"{where}: 'segments' must be a non-empty list")

    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_segments):
        seg_where = f"{where} segment[{index}]"
        if not isinstance(raw, Mapping):
            raise SalesPlaybookImportError(f"{seg_where}: must be an object")
        sequence = raw.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
            raise SalesPlaybookImportError(
                f"{seg_where}: 'sequence' must be a non-negative integer"
            )
        try:
            segment_type = SalesScriptSegmentType(raw.get("type"))
        except ValueError as exc:
            raise SalesPlaybookImportError(
                f"{seg_where}: unknown segment type {raw.get('type')!r}"
            ) from exc

        if segment_type is SalesScriptSegmentType.TEXT:
            text = raw.get("text")
            if not isinstance(text, str) or not text.strip():
                raise SalesPlaybookImportError(
                    f"{seg_where}: TEXT segment needs non-empty 'text'"
                )
            if raw.get("image_ref") is not None:
                raise SalesPlaybookImportError(
                    f"{seg_where}: TEXT segment must not carry 'image_ref'"
                )
            parsed.append(
                {"sequence": sequence, "type": segment_type, "text": text}
            )
        else:
            image_ref = raw.get("image_ref")
            if not isinstance(image_ref, str) or not image_ref:
                raise SalesPlaybookImportError(
                    f"{seg_where}: IMAGE segment needs 'image_ref'"
                )
            if raw.get("text") is not None:
                raise SalesPlaybookImportError(
                    f"{seg_where}: IMAGE segment must not carry inline 'text'"
                )
            if image_ref not in image_bytes:
                # design §10: "0 missing references" is an acceptance fact, so a
                # dangling reference is a hard import failure, not a warning.
                raise SalesPlaybookImportError(
                    f"{seg_where}: dangling image reference {image_ref!r}"
                )
            caption = raw.get("caption")
            if caption is not None and not isinstance(caption, str):
                raise SalesPlaybookImportError(f"{seg_where}: 'caption' must be a string")
            parsed.append(
                {
                    "sequence": sequence,
                    "type": segment_type,
                    "image_ref": image_ref,
                    "caption": caption,
                }
            )

    sequences = [segment["sequence"] for segment in parsed]
    if sorted(sequences) != list(range(len(sequences))):
        # A gap means the extractor silently dropped a fragment; contiguity is
        # the cheapest way to notice that before it becomes "official wording".
        raise SalesPlaybookImportError(
            f"{where}: segment sequences must be contiguous 0..{len(sequences) - 1}, "
            f"got {sorted(sequences)}"
        )
    parsed.sort(key=lambda segment: segment["sequence"])
    return parsed


def _validated_facts(
    entry_payload: Mapping[str, Any],
    where: str,
    entry_scope: SalesScriptScope,
    segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_facts = entry_payload.get("facts", [])
    if not isinstance(raw_facts, list):
        raise SalesPlaybookImportError(f"{where}: 'facts' must be a list")

    # The authoritative TEXT body a fact span must resolve against. Image
    # captions are deliberately excluded: they are not the wording a suggestion
    # may quote, so a span that only lives in one is not suppressible where it
    # matters.
    text_corpus = "\n".join(
        str(segment["text"])
        for segment in segments
        if segment["type"] is SalesScriptSegmentType.TEXT
    )

    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_facts):
        fact_where = f"{where} fact[{index}]"
        if not isinstance(raw, Mapping):
            raise SalesPlaybookImportError(f"{fact_where}: must be an object")
        if "status" in raw:
            # D3: import status is server-determined. Accepting a payload status
            # would let a crafted package ship VERIFIED_CURRENT and bypass the
            # entire §6 assertion gate on day one.
            raise SalesPlaybookImportError(
                f"{fact_where}: payload must not set 'status' "
                "(import is always NEEDS_REVIEW)"
            )
        fact_key = _require_str(raw, "fact_key", fact_where)
        raw_span = _require_str(raw, "raw_span", fact_where)
        fact_class = _parse_fact_class(raw.get("fact_class"), fact_where)
        scope = _parse_scope(raw.get("scope"), fact_where)
        if scope is not SalesScriptScope.COMMON and scope != entry_scope:
            # Mirrors ck_ssfb_scope_compat so the message names the real problem
            # instead of surfacing a raw IntegrityError. The DB CHECK remains the
            # authority -- this is defence in depth, not a replacement.
            raise SalesPlaybookImportError(
                f"{fact_where}: cross-version binding -- fact scope {scope.value!r} "
                f"on a {entry_scope.value!r} entry"
            )
        identity = (_norm(fact_key), _norm(raw_span))
        if identity in seen:
            raise SalesPlaybookImportError(
                f"{fact_where}: duplicate fact occurrence {identity[0]!r}"
            )
        seen.add(identity)
        if suppress_span(text_corpus, raw_span) is None:
            # P1-1 at the door. Every binding imports as NEEDS_REVIEW, so every
            # span must be suppressible from day one; a span that cannot be
            # resolved deterministically would make the retrieval gate exclude
            # the whole entry for the package's entire life. Failing here names
            # the corrupt fact instead of silently shipping dead content.
            # Same resolver as the runtime gate -- one rule, no drift.
            raise SalesPlaybookImportError(
                f"{fact_where}: raw_span {raw_span!r} is not deterministically "
                "resolvable in this entry's TEXT segments, so it could never be "
                "suppressed -- fix the extraction rather than importing a fact "
                "whose span does not exist in the official wording"
            )
        parsed.append(
            {
                "fact_key": fact_key,
                "raw_span": raw_span,
                "fact_class": fact_class,
                "scope": scope,
            }
        )
    return parsed


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def import_package(
    session: Session,
    *,
    export: Mapping[str, Any],
    raw_ebf_bytes: bytes,
    image_bytes: Mapping[str, bytes],
    project_id: str,
    source_type: str = SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
) -> ImportResult:
    """Import one EBF package, idempotently and atomically (design §2, D1).

    1. Compute both integrity anchors.
    2. If ``extraction_manifest_hash`` already exists (ACTIVE **or** SUPERSEDED)
       the package has been seen -- return a skipped result and write nothing.
    3. Otherwise, inside ONE transaction: flip the previous ACTIVE package of the
       same ``source_type`` to SUPERSEDED, insert the new package as ACTIVE, then
       insert entries / segments / artifacts / fact bindings.

    The partial unique index ``uq_ssrc_single_active`` is the real guarantee that
    two live generations cannot coexist; step 3's ordering just avoids tripping
    it on the happy path.
    """
    if source_type not in SALES_SCRIPT_SOURCE_TYPES:
        raise SalesPlaybookImportError(f"unsupported source_type {source_type!r}")

    source_version = _require_str(export, "source_version", "export")
    original_ebf_filename = _require_str(export, "original_ebf_filename", "export")
    extracted_manifest_filename = _require_str(
        export, "extracted_manifest_filename", "export"
    )
    raw_entries = export.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise SalesPlaybookImportError("export: 'entries' must be a non-empty list")

    # -- validate the whole package BEFORE touching the session ---------------
    validated: list[dict[str, Any]] = []
    seen_entry_ids: set[str] = set()
    reference_mapping: list[tuple[str, int, str]] = []
    referenced_refs: set[str] = set()

    for index, raw_entry in enumerate(raw_entries):
        where = f"export entry[{index}]"
        if not isinstance(raw_entry, Mapping):
            raise SalesPlaybookImportError(f"{where}: must be an object")
        source_entry_id = _require_str(raw_entry, "source_entry_id", where)
        if source_entry_id in seen_entry_ids:
            raise SalesPlaybookImportError(
                f"{where}: duplicate source_entry_id {source_entry_id!r} in package"
            )
        seen_entry_ids.add(source_entry_id)
        product_scope = _parse_scope(raw_entry.get("product_scope"), where)
        category = _require_str(raw_entry, "category", where)
        title = _require_str(raw_entry, "title", where)
        segments = _validated_segments(raw_entry, where, image_bytes)
        facts = _validated_facts(raw_entry, where, product_scope, segments)

        for segment in segments:
            if segment["type"] is SalesScriptSegmentType.IMAGE:
                reference_mapping.append(
                    (source_entry_id, segment["sequence"], segment["image_ref"])
                )
                referenced_refs.add(segment["image_ref"])

        validated.append(
            {
                "source_entry_id": source_entry_id,
                "product_scope": product_scope,
                "category": category,
                "title": title,
                "segments": segments,
                "facts": facts,
            }
        )

    unused = sorted(set(image_bytes) - referenced_refs)
    if unused:
        # An unreferenced image means the export and the media set disagree; the
        # manifest hash would then cover bytes that no entry can ever surface.
        raise SalesPlaybookImportError(
            f"export: {len(unused)} image(s) supplied but never referenced: {unused[:5]}"
        )

    source_file_hash = compute_source_file_hash(raw_ebf_bytes)
    extraction_manifest_hash = compute_extraction_manifest_hash(
        export, image_bytes, reference_mapping
    )

    # -- idempotency: the same extraction has already been imported -----------
    existing = session.exec(
        select(SalesScriptSource).where(
            SalesScriptSource.extraction_manifest_hash == extraction_manifest_hash
        )
    ).first()
    if existing is not None:
        return ImportResult(
            source_id=existing.id,
            source_file_hash=existing.source_file_hash,
            extraction_manifest_hash=existing.extraction_manifest_hash,
            skipped=True,
            entry_count=existing.entry_count,
        )

    # -- atomic activation ----------------------------------------------------
    previous_active = session.exec(
        select(SalesScriptSource).where(
            SalesScriptSource.source_type == source_type,
            SalesScriptSource.status == SalesScriptSourceStatus.ACTIVE,
        )
    ).first()
    superseded_source_id: str | None = None
    if previous_active is not None:
        previous_active.status = SalesScriptSourceStatus.SUPERSEDED
        session.add(previous_active)
        # Flush BEFORE inserting the new ACTIVE row, otherwise the partial unique
        # index sees two ACTIVE rows for one source_type and aborts.
        session.flush()
        superseded_source_id = previous_active.id

    image_checksums = {ref: _sha256_hex(blob) for ref, blob in image_bytes.items()}
    scope_distribution: dict[str, int] = {}
    for entry in validated:
        key = entry["product_scope"].value
        scope_distribution[key] = scope_distribution.get(key, 0) + 1

    unique_image_checksums = len(set(image_checksums.values()))
    source = SalesScriptSource(
        source_type=source_type,
        original_ebf_filename=original_ebf_filename,
        extracted_manifest_filename=extracted_manifest_filename,
        source_file_hash=source_file_hash,
        extraction_manifest_hash=extraction_manifest_hash,
        source_version=source_version,
        status=SalesScriptSourceStatus.ACTIVE,
        entry_count=len(validated),
        # cleanup A: acceptance statistics for THIS package, computed from the
        # package. Never importer constants -- a later package may differ.
        metadata_json={
            "entry_count": len(validated),
            "scope_distribution": scope_distribution,
            "unique_image_count": unique_image_checksums,
            "image_reference_count": len(reference_mapping),
            "missing_reference_count": 0,
            "fact_binding_count": sum(len(entry["facts"]) for entry in validated),
            "superseded_source_id": superseded_source_id,
        },
    )
    session.add(source)
    session.flush()

    # One Artifact per unique image CONTENT, scoped to this generation: 76
    # references over 74 unique images in the first package means duplicates are
    # normal. Generations are kept independent (no artifact re-use across source
    # packages) so superseding one can never disturb another.
    artifact_by_checksum: dict[str, Artifact] = {}
    segment_count = 0
    fact_binding_count = 0

    for entry_payload in validated:
        normalised_segments: list[tuple[int, str, str]] = []
        for segment in entry_payload["segments"]:
            if segment["type"] is SalesScriptSegmentType.TEXT:
                normalised_segments.append(
                    (segment["sequence"], "text", _norm(segment["text"]))
                )
            else:
                checksum = image_checksums[segment["image_ref"]]
                caption = _norm(segment["caption"] or "")
                normalised_segments.append(
                    (segment["sequence"], "image", f"{checksum}{_FS}{caption}")
                )
        source_hash = compute_entry_source_hash(normalised_segments)

        entry = SalesScriptEntry(
            source_id=source.id,
            source_entry_id=entry_payload["source_entry_id"],
            product_scope=entry_payload["product_scope"],
            category=entry_payload["category"],
            title=entry_payload["title"],
            source_hash=source_hash,
        )
        session.add(entry)
        session.flush()

        for segment in entry_payload["segments"]:
            if segment["type"] is SalesScriptSegmentType.TEXT:
                session.add(
                    SalesScriptSegment(
                        entry_id=entry.id,
                        sequence=segment["sequence"],
                        segment_type=SalesScriptSegmentType.TEXT,
                        text_content=segment["text"],
                    )
                )
            else:
                ref = segment["image_ref"]
                checksum = image_checksums[ref]
                artifact = artifact_by_checksum.get(checksum)
                if artifact is None:
                    artifact = Artifact(
                        project_id=project_id,
                        type=ArtifactType.IMAGE,
                        uri=f"sales-playbook://{source.id}/{ref}",
                        checksum=checksum,
                        review_status=ArtifactReviewStatus.UNVERIFIED,
                        source="sales_playbook_import",
                        provenance_json={
                            "original_ebf_filename": original_ebf_filename,
                            "source_image_ref": ref,
                            "source_entry_id": entry_payload["source_entry_id"],
                            "sales_script_source_id": source.id,
                        },
                    )
                    session.add(artifact)
                    session.flush()
                    artifact_by_checksum[checksum] = artifact
                session.add(
                    SalesScriptSegment(
                        entry_id=entry.id,
                        sequence=segment["sequence"],
                        segment_type=SalesScriptSegmentType.IMAGE,
                        artifact_id=artifact.id,
                        caption=segment["caption"],
                    )
                )
            segment_count += 1

        for fact in entry_payload["facts"]:
            session.add(
                SalesScriptFactBinding(
                    entry_id=entry.id,
                    entry_scope=entry_payload["product_scope"],
                    fact_key=fact["fact_key"],
                    fact_class=fact["fact_class"],
                    raw_span=fact["raw_span"],
                    scope=fact["scope"],
                    # D3: NEVER auto-verified. Only an explicit owner review may
                    # promote a binding to VERIFIED_CURRENT.
                    status=SalesScriptFactStatus.NEEDS_REVIEW,
                    binding_hash=compute_binding_hash(
                        entry.id, fact["fact_key"], fact["raw_span"]
                    ),
                )
            )
            fact_binding_count += 1

    session.commit()

    return ImportResult(
        source_id=source.id,
        source_file_hash=source_file_hash,
        extraction_manifest_hash=extraction_manifest_hash,
        skipped=False,
        entry_count=len(validated),
        segment_count=segment_count,
        artifact_count=len(artifact_by_checksum),
        fact_binding_count=fact_binding_count,
        superseded_source_id=superseded_source_id,
        scope_distribution=scope_distribution,
    )

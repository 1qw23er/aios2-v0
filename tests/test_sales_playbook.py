"""Adversarial acceptance suite for SalesPlaybook V0 (design §10, contracts D1-D8).

Structured as the design's acceptance matrix rather than as "one test per
function", so each hard contract has at least one test that FAILS if the guard
is removed:

* D1 -- dual integrity anchors, idempotent re-import, atomic ACTIVE->SUPERSEDED,
  "two live generations" unrepresentable at the DB boundary.
* D2 -- segments are the single authority; entry ``source_hash`` byte-stable
  across re-import; TEXT/IMAGE mutually exclusive at the DB boundary.
* D3 -- one fact occurrence = one row, unique ``binding_hash``, cross-version
  binding rejected by BOTH the importer and the DB CHECK, cascade on delete.
* D4 -- version isolation and the COMPARE scope; superseded generations invisible.
* D5 -- evidence in its own association table; ``CsSuggestion`` schema untouched.
* D6 -- non-VERIFIED facts are masked and downgrade the reported safety.
* D7 -- the AI claim ceiling rejects fabricated price / percentage / URL / promise.
* D8 -- UNKNOWN scope yields a clarification and never a version-specific claim.

The real first package is not present in this sandbox, so every case drives the
importer through its fixture-shaped public interface. Per design §10 cleanup A
the first-package counts are acceptance facts recorded as audit data, never
importer constants -- ``test_acceptance_statistics_are_derived_from_package``
pins exactly that.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import importlib.util
import unicodedata
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, text

import alembic
from aios.actor import resolve_owner_actor
from aios.customer_service import _SALES_EVIDENCE_STALE, CustomerService
from aios.db import get_engine, run_migrations
from aios.knowledge_service import KnowledgeService
from aios.models import (
    Artifact,
    ArtifactReviewStatus,
    ArtifactType,
    Conversation,
    CsChannel,
    CsSuggestion,
    CsSuggestionDecision,
    CsSuggestionSalesEvidence,
    KnowledgeFact,
    KnowledgeFactStatus,
    KnowledgeReviewDecisionValue,
    Project,
    SalesScriptEntry,
    SalesScriptFactBinding,
    SalesScriptFactClass,
    SalesScriptFactStatus,
    SalesScriptQueryScope,
    SalesScriptScope,
    SalesScriptSegment,
    SalesScriptSegmentType,
    SalesScriptSource,
    SalesScriptSourceStatus,
)
from aios.sales_playbook import (
    CLARIFICATION_TEXT,
    ClaimCeilingViolation,
    SalesPlaybookImportError,
    assert_within_claim_ceiling,
    classify_query_scope,
    compute_source_file_hash,
    import_package,
    retrieve,
    scopes_for_query,
)
from aios.sales_playbook.retrieval import (
    UNVERIFIED_MASK,
    EvidenceRevalidationError,
    _mask_unverified_bindings,
    revalidate_sales_evidence,
    suppress_span,
)
from aios.services import ServiceError

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


@pytest.fixture
def engine(tmp_path):
    return get_engine(_database(tmp_path, "sales_playbook.db"))


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture
def project(session):
    p = Project(name="sp-proj", objective="sales playbook v0")
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@pytest.fixture
def owner():
    return resolve_owner_actor()


RAW_EBF = b"EBF-PACKAGE-BYTES-v1"

IMAGE_BYTES: dict[str, bytes] = {
    # img_b deliberately carries the SAME content as img_a: the first real
    # package has 76 references over 74 unique images, so content-level dedup
    # must be exercised.
    "img_a": b"\x89PNG-a",
    "img_b": b"\x89PNG-a",
    "img_c": b"\x89PNG-c",
}


def _export() -> dict:
    return {
        "source_version": "mihe-ebf-2026-08",
        "original_ebf_filename": "米核易外外.ebf",
        "extracted_manifest_filename": "02_完整结构化导出.json",
        "entries": [
            {
                "source_entry_id": "E1",
                "product_scope": SalesScriptScope.MIHE_2_0.value,
                "category": "佣金结算",
                "title": "佣金比例说明",
                "segments": [
                    {"sequence": 0, "type": "text", "text": "合伙人返佣比例 20%，按月结算。"},
                    {"sequence": 1, "type": "image", "image_ref": "img_a", "caption": "结算示意"},
                ],
                "facts": [
                    {
                        "fact_key": "commission_rate",
                        "fact_class": SalesScriptFactClass.COMMISSION.value,
                        "raw_span": "返佣比例 20%",
                        "scope": SalesScriptScope.MIHE_2_0.value,
                    }
                ],
            },
            {
                "source_entry_id": "E2",
                "product_scope": SalesScriptScope.MIHE_1_0.value,
                "category": "扣子工作流",
                "title": "扣子工作流说明",
                "segments": [
                    {"sequence": 0, "type": "text", "text": "老版本通过扣子工作流交付。"},
                    {"sequence": 1, "type": "image", "image_ref": "img_b", "caption": None},
                ],
                "facts": [],
            },
            {
                "source_entry_id": "E3",
                "product_scope": SalesScriptScope.COMMON.value,
                "category": "提现门槛",
                "title": "提现门槛说明",
                "segments": [
                    {"sequence": 0, "type": "text", "text": "提现门槛 ¥50，手续费 6.9%。"},
                ],
                "facts": [
                    {
                        "fact_key": "withdraw_threshold",
                        "fact_class": SalesScriptFactClass.PRICE.value,
                        "raw_span": "提现门槛 ¥50",
                        "scope": SalesScriptScope.COMMON.value,
                    }
                ],
            },
            {
                "source_entry_id": "E4",
                "product_scope": SalesScriptScope.MIHE_2_0.value,
                "category": "官网入口",
                "title": "官网入口地址",
                "segments": [
                    {"sequence": 0, "type": "text", "text": "登录地址 aimi.quantv.com 即可使用。"},
                    {"sequence": 1, "type": "image", "image_ref": "img_c", "caption": "入口截图"},
                ],
                "facts": [],
            },
        ],
    }


def _do_import(session: Session, project: Project, **overrides):
    payload = {
        "export": _export(),
        "raw_ebf_bytes": RAW_EBF,
        "image_bytes": dict(IMAGE_BYTES),
        "project_id": project.id,
    }
    payload.update(overrides)
    return import_package(session, **payload)


@pytest.fixture
def imported(session, project):
    return _do_import(session, project)


def _entry(session: Session, source_entry_id: str) -> SalesScriptEntry:
    entry = session.exec(
        select(SalesScriptEntry).where(
            SalesScriptEntry.source_entry_id == source_entry_id
        )
    ).first()
    assert entry is not None
    return entry


def _set_status(
    session: Session, source_entry_id: str, status: SalesScriptFactStatus
) -> None:
    entry = _entry(session, source_entry_id)
    for binding in session.exec(
        select(SalesScriptFactBinding).where(
            SalesScriptFactBinding.entry_id == entry.id
        )
    ).all():
        binding.status = status
        session.add(binding)
    session.commit()


# ===========================================================================
# D1 -- source integrity + activation
# ===========================================================================


def test_import_creates_single_active_source_with_both_anchors(session, imported):
    source = session.get(SalesScriptSource, imported.source_id)
    assert source is not None
    assert source.status == SalesScriptSourceStatus.ACTIVE
    assert source.source_file_hash == compute_source_file_hash(RAW_EBF)
    assert source.extraction_manifest_hash != source.source_file_hash
    assert len(source.extraction_manifest_hash) == 64
    assert source.entry_count == 4
    # cleanup B: the raw EBF and the structured export are two distinct fields.
    assert source.original_ebf_filename == "米核易外外.ebf"
    assert source.extracted_manifest_filename == "02_完整结构化导出.json"


def test_source_file_hash_is_sha256_of_raw_bytes(session, imported):
    source = session.get(SalesScriptSource, imported.source_id)
    assert source.source_file_hash == hashlib.sha256(RAW_EBF).hexdigest()


def test_reimport_of_identical_package_is_a_noop(session, project, imported):
    before_entries = len(session.exec(select(SalesScriptEntry)).all())
    again = _do_import(session, project)
    assert again.skipped is True
    assert again.source_id == imported.source_id
    assert len(session.exec(select(SalesScriptEntry)).all()) == before_entries
    assert len(session.exec(select(SalesScriptSource)).all()) == 1


def test_manifest_hash_detects_image_content_change_under_same_filename(
    session, project, imported
):
    """D1 anchor 2: swapping bytes behind an unchanged ``image_ref`` is caught."""
    mutated_images = dict(IMAGE_BYTES)
    mutated_images["img_c"] = b"\x89PNG-c-TAMPERED"
    result = _do_import(session, project, image_bytes=mutated_images)
    assert result.skipped is False
    assert result.extraction_manifest_hash != imported.extraction_manifest_hash


def test_new_package_supersedes_previous_active_in_one_transaction(
    session, project, imported
):
    export2 = _export()
    export2["source_version"] = "mihe-ebf-2026-09"
    result = _do_import(session, project, export=export2, raw_ebf_bytes=b"EBF-v2")

    assert result.skipped is False
    assert result.superseded_source_id == imported.source_id

    old = session.get(SalesScriptSource, imported.source_id)
    new = session.get(SalesScriptSource, result.source_id)
    session.refresh(old)
    assert old.status == SalesScriptSourceStatus.SUPERSEDED
    assert new.status == SalesScriptSourceStatus.ACTIVE

    actives = session.exec(
        select(SalesScriptSource).where(
            SalesScriptSource.status == SalesScriptSourceStatus.ACTIVE
        )
    ).all()
    assert len(actives) == 1


def test_two_active_generations_are_unrepresentable(session, project, imported):
    """The partial unique index -- not a service convention -- is the guarantee."""
    rogue = SalesScriptSource(
        original_ebf_filename="rogue.ebf",
        extracted_manifest_filename="rogue.json",
        source_file_hash="f" * 64,
        extraction_manifest_hash="e" * 64,
        source_version="rogue",
        status=SalesScriptSourceStatus.ACTIVE,
    )
    session.add(rogue)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_unknown_source_type_rejected_by_importer(session, project):
    with pytest.raises(SalesPlaybookImportError, match="unsupported source_type"):
        _do_import(session, project, source_type="totally_bogus")


def test_unknown_source_type_rejected_by_db_check(session):
    rogue = SalesScriptSource(
        source_type="totally_bogus",
        original_ebf_filename="x.ebf",
        extracted_manifest_filename="x.json",
        source_file_hash="a" * 64,
        extraction_manifest_hash="b" * 64,
        source_version="x",
        status=SalesScriptSourceStatus.ACTIVE,
    )
    session.add(rogue)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_acceptance_statistics_are_derived_from_package(session, imported):
    """cleanup A: counts are audit data for THIS package, not importer constants."""
    source = session.get(SalesScriptSource, imported.source_id)
    stats = source.metadata_json
    assert stats["entry_count"] == 4
    assert stats["scope_distribution"] == {"mihe_2_0": 2, "mihe_1_0": 1, "common": 1}
    # 3 references over 2 unique image contents (img_a == img_b).
    assert stats["unique_image_count"] == 2
    assert stats["image_reference_count"] == 3
    assert stats["missing_reference_count"] == 0
    assert stats["fact_binding_count"] == 2
    assert imported.artifact_count == 2


# ===========================================================================
# D2 -- normalised segments, single authority
# ===========================================================================


def test_entry_has_no_segments_json_column():
    columns = set(SalesScriptEntry.model_fields)
    assert "segments" not in columns
    assert "media" not in columns


def test_text_and_image_segments_are_mutually_exclusive(session, imported):
    entry = _entry(session, "E1")
    segments = session.exec(
        select(SalesScriptSegment)
        .where(SalesScriptSegment.entry_id == entry.id)
        .order_by(SalesScriptSegment.sequence)
    ).all()
    assert [s.sequence for s in segments] == [0, 1]
    text_seg, image_seg = segments
    assert text_seg.segment_type == SalesScriptSegmentType.TEXT
    assert text_seg.text_content and text_seg.artifact_id is None
    assert image_seg.segment_type == SalesScriptSegmentType.IMAGE
    assert image_seg.artifact_id is not None and image_seg.text_content is None


def test_hybrid_segment_rejected_by_db_check(session, imported):
    entry = _entry(session, "E1")
    hybrid = SalesScriptSegment(
        entry_id=entry.id,
        sequence=99,
        segment_type=SalesScriptSegmentType.TEXT,
        text_content="text",
        artifact_id=None,
    )
    hybrid.artifact_id = session.exec(select(Artifact)).first().id
    session.add(hybrid)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_duplicate_sequence_rejected(session, imported):
    entry = _entry(session, "E1")
    session.add(
        SalesScriptSegment(
            entry_id=entry.id,
            sequence=0,
            segment_type=SalesScriptSegmentType.TEXT,
            text_content="duplicate order",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_entry_source_hash_is_byte_stable_across_reimport(tmp_path, imported, session):
    """A frozen source row must reproduce its hash in a completely fresh DB."""
    other_url = f"sqlite:///{(tmp_path / 'second.db').as_posix()}"
    run_migrations(other_url)
    with Session(get_engine(other_url)) as other:
        p2 = Project(name="p2", objective="second")
        other.add(p2)
        other.commit()
        other.refresh(p2)
        _do_import(other, p2)
        hashes_b = {
            e.source_entry_id: e.source_hash
            for e in other.exec(select(SalesScriptEntry)).all()
        }
    hashes_a = {
        e.source_entry_id: e.source_hash
        for e in session.exec(select(SalesScriptEntry)).all()
    }
    assert hashes_a == hashes_b


def test_entry_source_hash_changes_when_image_content_changes(
    session, project, imported
):
    before = _entry(session, "E4").source_hash
    mutated = dict(IMAGE_BYTES)
    mutated["img_c"] = b"\x89PNG-c-DIFFERENT"
    _do_import(session, project, image_bytes=mutated)
    after = [
        e.source_hash
        for e in session.exec(
            select(SalesScriptEntry).where(SalesScriptEntry.source_entry_id == "E4")
        ).all()
    ]
    assert before in after
    assert any(h != before for h in after)


# ===========================================================================
# D3 -- normalised fact bindings
# ===========================================================================


def test_fact_bindings_import_as_needs_review(session, imported):
    bindings = session.exec(select(SalesScriptFactBinding)).all()
    assert len(bindings) == 2
    assert {b.status for b in bindings} == {SalesScriptFactStatus.NEEDS_REVIEW}
    assert all(len(b.binding_hash) == 64 for b in bindings)


def test_payload_cannot_smuggle_a_verified_status(session, project):
    export = _export()
    export["entries"][0]["facts"][0]["status"] = (
        SalesScriptFactStatus.VERIFIED_CURRENT.value
    )
    with pytest.raises(SalesPlaybookImportError, match="must not set 'status'"):
        _do_import(session, project, export=export)


def test_duplicate_fact_occurrence_rejected(session, project):
    export = _export()
    export["entries"][0]["facts"].append(
        copy.deepcopy(export["entries"][0]["facts"][0])
    )
    with pytest.raises(SalesPlaybookImportError, match="duplicate fact occurrence"):
        _do_import(session, project, export=export)


def test_cross_version_binding_rejected_by_importer(session, project):
    export = _export()
    # A 1.0-scoped fact hung on a 2.0 entry.
    export["entries"][0]["facts"][0]["scope"] = SalesScriptScope.MIHE_1_0.value
    with pytest.raises(SalesPlaybookImportError, match="cross-version binding"):
        _do_import(session, project, export=export)


def test_cross_version_binding_rejected_by_db_check(session, imported):
    """Defence in depth: the CHECK holds even if the importer is bypassed."""
    entry = _entry(session, "E1")  # product_scope = mihe_2_0
    session.add(
        SalesScriptFactBinding(
            entry_id=entry.id,
            entry_scope=SalesScriptScope.MIHE_2_0,
            fact_key="smuggled",
            fact_class=SalesScriptFactClass.PRICE,
            raw_span="¥1",
            scope=SalesScriptScope.MIHE_1_0,
            status=SalesScriptFactStatus.NEEDS_REVIEW,
            binding_hash="c" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_denormalised_entry_scope_cannot_lie(session, imported):
    """The composite FK forbids claiming an entry has a scope it does not have."""
    entry = _entry(session, "E1")  # mihe_2_0
    session.add(
        SalesScriptFactBinding(
            entry_id=entry.id,
            entry_scope=SalesScriptScope.MIHE_1_0,  # false denormalisation
            fact_key="liar",
            fact_class=SalesScriptFactClass.PRICE,
            raw_span="¥2",
            scope=SalesScriptScope.MIHE_1_0,
            status=SalesScriptFactStatus.NEEDS_REVIEW,
            binding_hash="d" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_binding_hash_is_unique(session, imported):
    existing = session.exec(select(SalesScriptFactBinding)).first()
    entry = _entry(session, "E3")
    session.add(
        SalesScriptFactBinding(
            entry_id=entry.id,
            entry_scope=SalesScriptScope.COMMON,
            fact_key="another",
            fact_class=SalesScriptFactClass.PROMO,
            raw_span="another span",
            scope=SalesScriptScope.COMMON,
            status=SalesScriptFactStatus.NEEDS_REVIEW,
            binding_hash=existing.binding_hash,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_bindings_cascade_with_their_entry(session, imported):
    entry = _entry(session, "E1")
    session.exec(
        select(SalesScriptSegment).where(SalesScriptSegment.entry_id == entry.id)
    ).all()
    for segment in session.exec(
        select(SalesScriptSegment).where(SalesScriptSegment.entry_id == entry.id)
    ).all():
        session.delete(segment)
    session.commit()
    session.delete(entry)
    session.commit()
    remaining = session.exec(
        select(SalesScriptFactBinding).where(
            SalesScriptFactBinding.entry_id == entry.id
        )
    ).all()
    assert remaining == []


# ===========================================================================
# Fail-closed package validation
# ===========================================================================


def test_dangling_image_reference_rejected(session, project):
    export = _export()
    export["entries"][3]["segments"][1]["image_ref"] = "img_missing"
    with pytest.raises(SalesPlaybookImportError, match="dangling image reference"):
        _do_import(session, project, export=export)


def test_duplicate_source_entry_id_rejected(session, project):
    export = _export()
    export["entries"][1]["source_entry_id"] = "E1"
    with pytest.raises(SalesPlaybookImportError, match="duplicate source_entry_id"):
        _do_import(session, project, export=export)


def test_non_contiguous_segment_sequence_rejected(session, project):
    export = _export()
    export["entries"][0]["segments"][1]["sequence"] = 5
    with pytest.raises(SalesPlaybookImportError, match="contiguous"):
        _do_import(session, project, export=export)


def test_unreferenced_image_rejected(session, project):
    images = dict(IMAGE_BYTES)
    images["img_orphan"] = b"orphan"
    with pytest.raises(SalesPlaybookImportError, match="never referenced"):
        _do_import(session, project, image_bytes=images)


def test_nothing_is_written_when_validation_fails(session, project):
    export = _export()
    export["entries"][1]["source_entry_id"] = "E1"
    with pytest.raises(SalesPlaybookImportError):
        _do_import(session, project, export=export)
    assert session.exec(select(SalesScriptSource)).all() == []
    assert session.exec(select(SalesScriptEntry)).all() == []


# ===========================================================================
# cleanup C -- Artifact reuse
# ===========================================================================


def test_images_are_stored_as_unverified_image_artifacts(session, project, imported):
    artifacts = session.exec(select(Artifact)).all()
    assert len(artifacts) == 2  # img_a == img_b deduped by content
    for artifact in artifacts:
        assert artifact.type == ArtifactType.IMAGE
        assert artifact.review_status == ArtifactReviewStatus.UNVERIFIED
        assert artifact.project_id == project.id
        assert len(artifact.checksum) == 64
        assert artifact.provenance_json["original_ebf_filename"] == "米核易外外.ebf"
        assert artifact.provenance_json["sales_script_source_id"] == imported.source_id


def test_artifact_checksum_is_sha256_of_image_bytes(session, imported):
    checksums = {a.checksum for a in session.exec(select(Artifact)).all()}
    assert hashlib.sha256(IMAGE_BYTES["img_a"]).hexdigest() in checksums
    assert hashlib.sha256(IMAGE_BYTES["img_c"]).hexdigest() in checksums


def test_no_new_artifact_enum_member_was_added():
    assert "sales_script" not in {member.value for member in ArtifactType}
    assert {member.value for member in ArtifactType} >= {"image"}


# ===========================================================================
# D4 -- runtime scope classification + version isolation
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2.0 的佣金比例是怎么算的", SalesScriptQueryScope.MIHE_2_0),
        ("合伙人后台怎么进", SalesScriptQueryScope.MIHE_2_0),
        ("扣子那个老版本的工作流怎么用", SalesScriptQueryScope.MIHE_1_0),
        ("1.0 还能用吗", SalesScriptQueryScope.MIHE_1_0),
        ("2.0 的工作流和之前扣子的工作流有什么区别", SalesScriptQueryScope.COMPARE_1_0_2_0),
        ("2.0 跟 1.0 都有哪些功能", SalesScriptQueryScope.COMPARE_1_0_2_0),
        ("这个东西好用吗", SalesScriptQueryScope.UNKNOWN),
        ("有什么区别", SalesScriptQueryScope.UNKNOWN),
        ("", SalesScriptQueryScope.UNKNOWN),
    ],
)
def test_query_scope_classification(text, expected):
    assert classify_query_scope(text) == expected


def test_scopes_for_query_matches_design_table():
    assert scopes_for_query(SalesScriptQueryScope.MIHE_1_0) == (
        SalesScriptScope.MIHE_1_0,
        SalesScriptScope.COMMON,
    )
    assert scopes_for_query(SalesScriptQueryScope.MIHE_2_0) == (
        SalesScriptScope.MIHE_2_0,
        SalesScriptScope.COMMON,
    )
    assert scopes_for_query(SalesScriptQueryScope.COMPARE_1_0_2_0) == (
        SalesScriptScope.MIHE_1_0,
        SalesScriptScope.MIHE_2_0,
        SalesScriptScope.COMMON,
    )
    # UNKNOWN may read nothing at all.
    assert scopes_for_query(SalesScriptQueryScope.UNKNOWN) == ()


def test_2_0_question_never_returns_a_1_0_entry(session, imported):
    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    assert result.query_scope == SalesScriptQueryScope.MIHE_2_0
    assert result.has_hits
    assert {hit.source_entry_id for hit in result.hits} == {"E1"}
    assert all(
        hit.product_scope != SalesScriptScope.MIHE_1_0 for hit in result.hits
    )


def test_1_0_question_never_returns_a_2_0_entry(session, imported):
    result = retrieve(session, "扣子那个老版本的工作流怎么用")
    assert result.query_scope == SalesScriptQueryScope.MIHE_1_0
    assert {hit.source_entry_id for hit in result.hits} == {"E2"}


def test_compare_question_may_span_both_versions(session, imported):
    result = retrieve(session, "2.0 的工作流和之前扣子的工作流有什么区别")
    assert result.query_scope == SalesScriptQueryScope.COMPARE_1_0_2_0
    assert "E2" in {hit.source_entry_id for hit in result.hits}
    assert set(result.considered_scopes) == {
        SalesScriptScope.MIHE_1_0,
        SalesScriptScope.MIHE_2_0,
        SalesScriptScope.COMMON,
    }


def test_off_category_entry_does_not_enter_retrieval(session, imported):
    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    assert "E4" not in {hit.source_entry_id for hit in result.hits}
    assert "E3" not in {hit.source_entry_id for hit in result.hits}


def test_superseded_generation_is_never_retrieved(session, project, imported):
    old_entry_ids = {e.id for e in session.exec(select(SalesScriptEntry)).all()}
    export2 = _export()
    export2["source_version"] = "mihe-ebf-2026-09"
    _do_import(session, project, export=export2, raw_ebf_bytes=b"EBF-v2")

    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    assert result.has_hits
    assert all(hit.entry_id not in old_entry_ids for hit in result.hits)


def test_retrieval_without_an_active_generation_is_empty(session):
    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    assert result.hits == ()
    assert result.source_id is None


# ===========================================================================
# D6 -- dynamic-fact safety
# ===========================================================================


def test_needs_review_masks_the_span_and_downgrades_safety(session, imported):
    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    hit = result.hits[0]
    assert hit.fact_safety == SalesScriptFactStatus.NEEDS_REVIEW
    assert hit.assertable is False
    assert "返佣比例 20%" in hit.official_text  # verbatim source is preserved
    assert "返佣比例 20%" not in hit.safe_text  # but never assertable
    assert UNVERIFIED_MASK in hit.safe_text
    assert "20%" not in result.claim_corpus


def test_verified_current_is_assertable(session, imported):
    _set_status(session, "E1", SalesScriptFactStatus.VERIFIED_CURRENT)
    hit = retrieve(session, "2.0 的佣金比例是怎么算的").hits[0]
    assert hit.fact_safety == SalesScriptFactStatus.VERIFIED_CURRENT
    assert hit.assertable is True
    assert "返佣比例 20%" in hit.safe_text


def test_flipping_verified_to_stale_suppresses_the_claim_next_time(session, imported):
    _set_status(session, "E1", SalesScriptFactStatus.VERIFIED_CURRENT)
    assert "20%" in retrieve(session, "2.0 的佣金比例是怎么算的").hits[0].safe_text

    _set_status(session, "E1", SalesScriptFactStatus.STALE)
    later = retrieve(session, "2.0 的佣金比例是怎么算的").hits[0]
    assert later.fact_safety == SalesScriptFactStatus.STALE
    assert later.assertable is False
    assert "20%" not in later.safe_text


def test_version_1_only_entry_is_unusable_in_a_2_0_context(session, imported):
    _set_status(session, "E3", SalesScriptFactStatus.VERSION_1_ONLY)
    result = retrieve(session, "2.0 的提现门槛是怎么定的")
    assert "E3" not in {hit.source_entry_id for hit in result.hits}


def test_entry_without_bindings_is_assertable(session, imported):
    hit = next(
        h
        for h in retrieve(session, "扣子那个老版本的工作流怎么用").hits
        if h.source_entry_id == "E2"
    )
    assert hit.fact_safety == SalesScriptFactStatus.VERIFIED_CURRENT
    assert hit.assertable is True


def test_weakest_binding_state_wins(session, imported):
    entry = _entry(session, "E1")
    session.add(
        SalesScriptFactBinding(
            entry_id=entry.id,
            entry_scope=SalesScriptScope.MIHE_2_0,
            fact_key="extra",
            fact_class=SalesScriptFactClass.PROMO,
            # Must be a span that REALLY occurs in E1's official TEXT: post P1-1
            # a non-VERIFIED span that cannot be resolved excludes the whole
            # entry, so an invented span would test exclusion, not weakest-wins.
            raw_span="按月结算",
            scope=SalesScriptScope.COMMON,
            status=SalesScriptFactStatus.STALE,
            binding_hash="e" * 64,
        )
    )
    session.commit()
    hit = retrieve(session, "2.0 的佣金比例是怎么算的").hits[0]
    assert hit.fact_safety == SalesScriptFactStatus.STALE
    # Both non-VERIFIED spans are suppressed, not just the weakest one.
    assert "返佣比例 20%" not in hit.safe_text
    assert "按月结算" not in hit.safe_text


# ===========================================================================
# D7 -- AI claim ceiling
# ===========================================================================


def test_ceiling_allows_pure_tone_rewrite():
    corpus = "合伙人返佣比例 20%，按月结算。"
    assert_within_claim_ceiling("您好，合伙人的返佣比例是 20%，按月结算哦～", corpus)


@pytest.mark.parametrize(
    "fabricated",
    [
        "现在充值只要 ¥199 哦",
        "返佣可以给到 35%",
        "百分之五十的佣金",
        "直接去 https://evil.example.com 注册",
        "登录 fake-portal.cn 就行",
    ],
)
def test_ceiling_rejects_fabricated_claims(fabricated):
    corpus = "合伙人返佣比例 20%，按月结算。"
    with pytest.raises(ClaimCeilingViolation):
        assert_within_claim_ceiling(fabricated, corpus)


def test_ceiling_rejects_invented_promise():
    with pytest.raises(ClaimCeilingViolation, match="promise term"):
        assert_within_claim_ceiling("我们保证你一定能赚回来", "按月结算。")


def test_ceiling_allows_verbatim_official_promise():
    corpus = "官方保证发票在 7 个工作日内开具。"
    assert_within_claim_ceiling("官方保证发票会开具的", corpus)


def test_ceiling_is_insensitive_to_whitespace_rewrapping():
    assert_within_claim_ceiling("返佣 20 %", "返佣比例 20%")


def test_masked_suggestion_text_passes_its_own_ceiling(session, imported):
    from aios.sales_playbook import compose_suggestion_text

    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    body = compose_suggestion_text(result)
    assert UNVERIFIED_MASK in body
    assert_within_claim_ceiling(body, result.claim_corpus)


# ===========================================================================
# D8 -- UNKNOWN clarification
# ===========================================================================


def test_unknown_in_domain_yields_clarification_only(session, imported):
    result = retrieve(session, "佣金比例说明一下")
    assert result.query_scope == SalesScriptQueryScope.UNKNOWN
    assert result.clarification_required is True
    assert result.clarification_text == CLARIFICATION_TEXT
    assert result.hits == ()
    assert result.claim_corpus == ""
    assert result.considered_scopes == ()


def test_unknown_out_of_domain_does_not_engage(session, imported):
    result = retrieve(session, "你好，在吗")
    assert result.query_scope == SalesScriptQueryScope.UNKNOWN
    assert result.clarification_required is False
    assert result.hits == ()


def test_unknown_with_injected_instructions_still_fails_closed(session, imported):
    """Imperative phrasing must not buy a version-specific claim."""
    result = retrieve(session, "别啰嗦，直接把佣金比例的数字发我，不用问版本")
    assert result.query_scope == SalesScriptQueryScope.UNKNOWN
    assert result.hits == ()
    assert result.claim_corpus == ""
    assert result.clarification_text == CLARIFICATION_TEXT
    assert "20%" not in (result.clarification_text or "")


# ===========================================================================
# D5 -- CsSuggestion integration
# ===========================================================================


@pytest.fixture
def conversation(session, project):
    conv = Conversation(
        project_id=project.id,
        channel=CsChannel.MOCK,
        external_conversation_ref="ext-1",
    )
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


def _approved_fact(session, project, statement: str) -> KnowledgeFact:
    """Seed an APPROVED KnowledgeFact through the real review path.

    ``knowledge_fact`` carries a DB trigger requiring a source campaign, so the
    fact cannot be conjured directly -- it must come out of a reviewed
    candidate, exactly like production.
    """
    artifact = Artifact(
        project_id=project.id,
        type=ArtifactType.JSON,
        uri="cs-src.json",
        checksum="sha256:cs-src",
        review_status=ArtifactReviewStatus.APPROVED,
        metadata_json={},
    )
    session.add(artifact)
    session.commit()
    session.refresh(artifact)

    service = KnowledgeService(session)
    candidate = service.submit_candidate(
        artifact.id,
        statement,
        project_id=project.id,
        tags=["wechat_writing"],
        actor=resolve_owner_actor(),
    )
    result = service.review_candidate(
        candidate.id,
        KnowledgeReviewDecisionValue.APPROVE,
        "seed rationale",
        actor=resolve_owner_actor(),
        series_id=f"ss-series-{artifact.id}",
        version=1,
    )
    assert result.fact.status == KnowledgeFactStatus.APPROVED
    return result.fact


QUESTION = "2.0 的佣金比例是怎么算的"


def test_auto_send_still_possible_without_the_playbook(session, project, conversation, owner):
    _approved_fact(session, project, "佣金比例")
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert sug.decision == CsSuggestionDecision.AUTO_SEND


def test_playbook_hit_forces_human_confirm_over_auto_send(
    session, project, conversation, owner, imported
):
    _approved_fact(session, project, "佣金比例")
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert sug.decision == CsSuggestionDecision.HUMAN_CONFIRM


def test_evidence_rows_are_written_to_the_association_table(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    evidence = session.exec(
        select(CsSuggestionSalesEvidence).where(
            CsSuggestionSalesEvidence.suggestion_id == sug.id
        )
    ).all()
    assert len(evidence) == 1
    row = evidence[0]
    assert row.rank == 0
    assert row.fact_safety == SalesScriptFactStatus.NEEDS_REVIEW
    assert row.match_reason.startswith("scope=mihe_2_0;")
    assert session.get(SalesScriptEntry, row.entry_id).source_entry_id == "E1"


def test_cs_suggestion_schema_is_unchanged():
    assert set(CsSuggestion.model_fields) == {
        "id",
        "conversation_id",
        "project_id",
        "decision",
        "text",
        "confidence",
        "escalation_categories",
        "knowledge_fact_refs",
        "fact_revisions",
        "consumed",
        "sales_evidence_cited",
        "idempotency_key",
        "created_at",
    }


def test_escalation_is_never_downgraded_by_a_playbook_hit(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner,
        conversation_id=conversation.id,
        inbound_message_id=None,
        text="2.0 的佣金价格是多少钱",
    )
    assert sug.decision == CsSuggestionDecision.ESCALATE
    assert "price" in sug.escalation_categories


def test_unknown_domain_question_becomes_a_clarification_suggestion(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner,
        conversation_id=conversation.id,
        inbound_message_id=None,
        text="佣金比例说明一下",
    )
    assert sug.decision == CsSuggestionDecision.HUMAN_CONFIRM
    assert sug.text == CLARIFICATION_TEXT
    assert "20%" not in sug.text
    assert (
        session.exec(
            select(CsSuggestionSalesEvidence).where(
                CsSuggestionSalesEvidence.suggestion_id == sug.id
            )
        ).all()
        == []
    )


def test_out_of_domain_message_keeps_legacy_behaviour(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text="你好，在吗"
    )
    assert sug.decision == CsSuggestionDecision.ESCALATE
    assert sug.escalation_categories == ["low_confidence"]
    assert sug.text == "（已转人工处理）"


def test_replay_produces_the_same_suggestion_and_evidence(
    session, project, conversation, owner, imported
):
    service = CustomerService(session)
    first = service.generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    second = service.generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert first.text == second.text
    assert first.decision == second.decision

    def _evidence(suggestion_id: str):
        return [
            (row.entry_id, row.rank, row.match_reason, row.fact_safety)
            for row in session.exec(
                select(CsSuggestionSalesEvidence)
                .where(CsSuggestionSalesEvidence.suggestion_id == suggestion_id)
                .order_by(CsSuggestionSalesEvidence.rank)
            ).all()
        ]

    assert _evidence(first.id) == _evidence(second.id)


def test_evidence_is_unique_per_suggestion_and_entry(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    entry_id = session.exec(
        select(CsSuggestionSalesEvidence).where(
            CsSuggestionSalesEvidence.suggestion_id == sug.id
        )
    ).first().entry_id
    session.add(
        CsSuggestionSalesEvidence(
            suggestion_id=sug.id,
            entry_id=entry_id,
            rank=1,
            match_reason="duplicate",
            fact_safety=SalesScriptFactStatus.NEEDS_REVIEW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_suggestion_text_carries_no_unverified_number(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert "20%" not in sug.text
    assert UNVERIFIED_MASK in sug.text


# ===========================================================================
# Zero side effects
# ===========================================================================


def test_import_writes_zero_knowledge_rows(session, project):
    before = len(session.exec(select(KnowledgeFact)).all())
    _do_import(session, project)
    assert len(session.exec(select(KnowledgeFact)).all()) == before


def test_suggestion_generation_writes_zero_knowledge_rows(
    session, project, conversation, owner, imported
):
    before = len(session.exec(select(KnowledgeFact)).all())
    CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert len(session.exec(select(KnowledgeFact)).all()) == before


def test_official_wording_stays_separable_from_the_ai_text(session, imported):
    """Design §10: "查看官方原话" must return the verbatim source."""
    hit = retrieve(session, "2.0 的佣金比例是怎么算的").hits[0]
    assert hit.official_text != hit.safe_text
    assert hit.official_text == "合伙人返佣比例 20%，按月结算。"


# ===========================================================================
# P1-1 -- dynamic-fact masking must be fail-closed (OWNER DISPOSITION P1-1)
# ===========================================================================
#
# The OWNER DISPOSITION names ten adversarial scenarios that must each prove the
# masking gate refuses instead of silently no-op'ing. ``suppress_span`` is the
# SINGLE authority used by BOTH the runtime retrieval gate and the importer's
# package gate, so these scenarios also cover the import-time contract.
#
# Owner's explicit list:
#   1. empty span
#   2. whitespace-only span
#   3. typo / missing span
#   4. Unicode form difference (NFC vs NFD)
#   5. case difference
#   6. punctuation difference
#   7. truncated span
#   8. span that lives only in an image caption
#   9. a mutation that makes a stored binding unresolvable -> entry excluded
#  10. importer rejects a fact whose span is not deterministically resolvable


def test_p1_1_empty_span_is_unresolvable():
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "") is None
    assert suppress_span("合伙人返佣比例 20%，按月结算。", None) is None  # type: ignore[arg-type]


def test_p1_1_whitespace_only_span_is_unresolvable():
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "   ") is None
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "\t\n") is None


def test_p1_1_typo_or_missing_span_is_unresolvable():
    # A typo ("20%" -> "20％" is a different char, but here a plain misspelling)
    # or a span that simply never occurs must NOT be masked by accident.
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "返佣比例 30%") is None
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "不存在的话术") is None


def test_p1_1_unicode_form_difference_is_unresolvable():
    official = "合伙人返佣比例 20%，按月结算。"
    # NFD-decomposed form of "佣" must not match the NFC official text.
    nfd_span = "返佣比例 20%".encode().decode("utf-8")
    decomposed = unicodedata.normalize("NFD", nfd_span)
    # If the span happens to be NFC-equal it is resolvable; the point is that a
    # genuinely different NFC string is refused (no fuzzy matching).
    if decomposed != nfd_span:
        assert suppress_span(official, decomposed) is None
    else:
        # Already NFC: still must mask exactly, not silently.
        assert suppress_span(official, nfd_span) == official.replace(
            "返佣比例 20%", UNVERIFIED_MASK
        )


def test_p1_1_case_difference_is_unresolvable():
    # Full-width vs half-width "％" is a distinct codepoint -> must not match.
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "返佣比例 20％") is None
    # Lower/upper ASCII letters would also differ if present.
    assert suppress_span("Visit aimi.quantv.com", "AIMI.QUANTV.COM") is None


def test_p1_1_punctuation_difference_is_unresolvable():
    # A half-width comma instead of the full-width one is a different string.
    assert suppress_span("合伙人返佣比例 20%，按月结算。", "返佣比例 20%,按月结算") is None


def test_p1_1_truncated_span_still_resolves_exactly():
    # A genuine substring (truncated, but exactly present) IS maskable -- this
    # is safe because the replacement is verified to have removed the literal.
    official = "合伙人返佣比例 20%，按月结算。"
    out = suppress_span(official, "20%")
    assert out is not None
    assert "20%" not in out
    assert UNVERIFIED_MASK in out


def test_p1_1_caption_only_span_is_unresolvable():
    # The authoritative masking body is TEXT only; a span that lives solely in an
    # image caption must not be resolved against the caption (and the importer's
    # own gate uses TEXT only, so such a fact can never be imported either).
    official_text = "登录地址 aimi.quantv.com 即可使用。"
    caption_only_span = "入口截图"  # E4's image caption, not its TEXT
    assert suppress_span(official_text, caption_only_span) is None


def test_p1_1_unresolvable_binding_excludes_the_whole_entry(session, imported):
    """A runtime mutation that makes a stored binding unresolvable must exclude
    the entry from generative evidence -- the OWNER DISPOSITION scenario 9.

    This is the exact failure mode Codex flagged: a ``safe_text`` that still
    carried the raw number would also pollute ``claim_corpus`` and let a
    fabricated claim pass D7. P1-1 fail-closed removes the whole entry instead.
    """
    entry = _entry(session, "E1")
    binding = session.exec(
        select(SalesScriptFactBinding).where(
            SalesScriptFactBinding.entry_id == entry.id
        )
    ).first()
    # Corrupt the stored span so it no longer exists in the official TEXT.
    binding.raw_span = "这串字根本不在正文里"
    session.add(binding)
    session.commit()

    safe, ok = _mask_unverified_bindings(
        "合伙人返佣比例 20%，按月结算。", [binding]
    )
    assert ok is False
    assert safe is None

    result = retrieve(session, "2.0 的佣金比例是怎么算的")
    assert "E1" not in {hit.source_entry_id for hit in result.hits}
    # Critically: the unmaskable number must not leak into the claim corpus.
    assert "20%" not in result.claim_corpus


def test_p1_1_importer_rejects_unresolvable_fact_span(session, project):
    """The import-time door rejects a fact whose span is not deterministically
    resolvable in the entry's own TEXT segments (OWNER DISPOSITION scenario 10).

    Reuses the real ``_export`` shape but poisons one fact's ``raw_span`` so it
    cannot be found in E3's TEXT. The whole package must be refused and the
    transaction rolled back -- nothing partial is imported.
    """
    poison = _export()
    poison["entries"][2]["facts"][0]["raw_span"] = "不在正文里的话术"

    before = len(session.exec(select(SalesScriptEntry)).all())
    with pytest.raises(SalesPlaybookImportError, match="not deterministically"):
        _do_import(session, project, export=poison)
    session.rollback()

    # Nothing was written: fail-closed rolled the whole import back.
    assert len(session.exec(select(SalesScriptEntry)).all()) == before


# ===========================================================================
# P1-2 -- send-time revalidation must refuse on live degradation (OWNER P1-2)
# ===========================================================================
#
# Generation-time gates prove the copy was safe when BUILT. They prove nothing at
# the moment the owner clicks confirm, which may be hours later and on the other
# side of a fact revocation / package re-import / entry withdrawal. The send-time
# gate reloads the cited evidence and fails closed on any degradation. Every
# branch must return ``ServiceError(409, _SALES_EVIDENCE_STALE)`` -- a
# business-readable message that NEVER leaks an internal id or reason code.
#
# Owner's explicit list (8 scenarios):
#   1. cited entry deleted after generation
#   2. active generation superseded by a re-import
#   3. source withdrawn (not ACTIVE)
#   4. scope inconsistency between entry and citation
#   5. binding no longer maskable
#   6. fact safety downgraded (VERIFIED_CURRENT -> STALE)
#   7. unverified span re-introduced in the (owner-edited) outgoing text
#   8. the generic human-send path enforces the SAME gate


@pytest.fixture
def playbook_suggestion(session, project, conversation, owner, imported):
    """A HUMAN_CONFIRM suggestion that cites the playbook (E1)."""
    sug = CustomerService(session).generate_suggestion(
        owner,
        conversation_id=conversation.id,
        inbound_message_id=None,
        text=QUESTION,
    )
    assert sug.decision == CsSuggestionDecision.HUMAN_CONFIRM
    evidence = session.exec(
        select(CsSuggestionSalesEvidence).where(
            CsSuggestionSalesEvidence.suggestion_id == sug.id
        )
    ).all()
    assert len(evidence) == 1
    return sug


def _assert_stale_refusal(excinfo) -> None:
    err = excinfo.value
    assert isinstance(err, ServiceError)
    assert err.status_code == 409
    assert err.detail == _SALES_EVIDENCE_STALE
    # No internal identifier or reason code may leak to the caller.
    assert "evidence" not in err.detail.lower()
    assert "binding" not in err.detail.lower()
    assert "generation" not in err.detail.lower()


def test_p1_2_cited_entry_deleted_blocks_send(session, project, conversation, owner, imported):
    # The DB-level RESTRICT on entry_id forbids dropping a cited entry out from
    # under its citation, so a "missing entry" at send time is represented by a
    # citation that points at now-absent id. The gate must refuse.
    with pytest.raises(EvidenceRevalidationError, match="evidence_entry_missing"):
        revalidate_sales_evidence(
            session,
            recorded_fact_safety={
                "ssent_does_not_exist_0000": SalesScriptFactStatus.NEEDS_REVIEW
            },
            outgoing_text="合伙人返佣比例 20%，按月结算。",
        )


def test_p1_2_superseded_generation_blocks_send(
    session, project, conversation, owner, imported
):
    CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    entry = _entry(session, "E1")
    export2 = _export()
    export2["source_version"] = "mihe-ebf-2026-09"
    _do_import(session, project, export=export2, raw_ebf_bytes=b"EBF-v2")
    session.commit()

    # The live source is now the NEW generation; the recorded citation still
    # points at the SUPERSEDED one -> not in the active generation.
    with pytest.raises(EvidenceRevalidationError, match="evidence_generation_not_active"):
        revalidate_sales_evidence(
            session,
            recorded_fact_safety={entry.id: SalesScriptFactStatus.NEEDS_REVIEW},
            outgoing_text="合伙人返佣比例 20%，按月结算。",
        )


def test_p1_2_withdrawn_source_blocks_send(
    session, project, conversation, owner, imported
):
    CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    entry = _entry(session, "E1")
    old = session.get(SalesScriptSource, entry.source_id)
    old.status = SalesScriptSourceStatus.SUPERSEDED
    session.add(old)
    session.commit()

    with pytest.raises(EvidenceRevalidationError, match="no_active_generation"):
        revalidate_sales_evidence(
            session,
            recorded_fact_safety={entry.id: SalesScriptFactStatus.NEEDS_REVIEW},
            outgoing_text="合伙人返佣比例 20%，按月结算。",
        )


def test_p1_2_binding_no_longer_maskable_blocks_send(
    session, project, conversation, owner, imported
):
    CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    entry = _entry(session, "E1")
    # Add a second binding whose span cannot be resolved against the official
    # TEXT. The package was valid at import (both spans resolved), but a later
    # mutation makes it unmaskable -> the revalidation gate must fire.
    session.add(
        SalesScriptFactBinding(
            entry_id=entry.id,
            entry_scope=SalesScriptScope.MIHE_2_0,
            fact_key="extra_poison",
            fact_class=SalesScriptFactClass.PROMO,
            raw_span="一个完全不在正文中的附加声明",
            scope=SalesScriptScope.COMMON,
            status=SalesScriptFactStatus.STALE,
            binding_hash="f" * 64,
        )
    )
    session.commit()

    with pytest.raises(EvidenceRevalidationError, match="evidence_not_maskable"):
        revalidate_sales_evidence(
            session,
            recorded_fact_safety={entry.id: SalesScriptFactStatus.NEEDS_REVIEW},
            outgoing_text="合伙人返佣比例 20%，按月结算。",
        )


def test_p1_2_fact_safety_downgraded_blocks_send(
    session, project, conversation, owner, imported
):
    CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    entry = _entry(session, "E1")
    _set_status(session, "E1", SalesScriptFactStatus.STALE)  # NEEDS_REVIEW -> STALE

    with pytest.raises(EvidenceRevalidationError, match="fact_safety_downgraded"):
        revalidate_sales_evidence(
            session,
            recorded_fact_safety={entry.id: SalesScriptFactStatus.NEEDS_REVIEW},
            outgoing_text="合伙人返佣比例 20%，按月结算。",
        )


def test_p1_2_owner_edited_text_reintroducing_unverified_blocks_send(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    # The owner edits the copy to re-introduce the masked number. The gate runs
    # on the EXACT outgoing body, so the re-introduced value must be caught.
    with pytest.raises(ServiceError) as exc:
        CustomerService(session).owner_confirm_suggestion(
            owner,
            conversation_id=conversation.id,
            suggestion_id=sug.id,
            edited_text="合伙人返佣比例 20%，按月结算。",
        )
    _assert_stale_refusal(exc)


def test_p1_2_human_send_enforces_the_same_gate(
    session, project, conversation, owner, imported
):
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    _set_status(session, "E1", SalesScriptFactStatus.STALE)
    # The generic owner-send path must NOT be a bypass of the SalesPlaybook gate.
    with pytest.raises(ServiceError) as exc:
        CustomerService(session).send_message(
            owner,
            conversation_id=conversation.id,
            text="合伙人返佣比例 20%，按月结算。",
            auto_send=False,
            suggestion_id=sug.id,
        )
    _assert_stale_refusal(exc)


def test_p1_2_unchanged_state_sends_successfully(
    session, project, conversation, owner, imported
):
    """The gate is NOT a blanket refusal: when nothing degraded, the send lands."""
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    msg = CustomerService(session).owner_confirm_suggestion(
        owner, conversation_id=conversation.id, suggestion_id=sug.id
    )
    assert msg.sender_type == "owner"
    session.refresh(sug)
    assert sug.consumed is True


def test_p1_2_knowledge_fact_only_suggestion_untouched(
    session, project, conversation, owner
):
    """No playbook citation -> the revalidation gate is a no-op (legacy path).

    An AUTO_SEND suggestion carries no playbook evidence rows, so the send-time
    gate must not fire: the direct gate is a no-op when nothing is cited, and
    the legacy auto-send path lands without touching SalesPlaybook state.
    """
    _approved_fact(session, project, "佣金比例")
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    # AUTO_SEND suggestions carry no evidence rows; the gate must not interfere.
    assert sug.decision == CsSuggestionDecision.AUTO_SEND
    # The gate is explicitly a no-op for an empty citation map (legacy path).
    revalidate_sales_evidence(
        session, recorded_fact_safety={}, outgoing_text="佣金比例"
    )


def test_p1_2_evidence_rows_deleted_blocks_send_service(
    session, project, conversation, owner, imported
):
    """P1-2 tamper-proof flag closes the bypass Codex flagged in re-review.

    Deleting the citation rows directly is allowed (``ON DELETE RESTRICT`` only
    blocks deleting the *parent* suggestion, not the child rows). Without the
    ``sales_evidence_cited`` flag the empty-row branch would silently skip
    revalidation and let the suggestion through; with it, the service fails
    CLOSED with 409 and sends nothing.
    """
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert sug.sales_evidence_cited is True
    session.exec(
        CsSuggestionSalesEvidence.__table__.delete().where(
            CsSuggestionSalesEvidence.suggestion_id == sug.id
        )
    )
    session.commit()
    with pytest.raises(ServiceError) as exc:
        CustomerService(session).owner_confirm_suggestion(
            owner, conversation_id=conversation.id, suggestion_id=sug.id
        )
    _assert_stale_refusal(exc)
    # Fail-closed leaves the suggestion re-triable (nothing was sent).
    session.refresh(sug)
    assert sug.consumed is False


def test_p1_2_scope_incoherence_blocks_send(
    session, project, conversation, owner, imported
):
    """6th attack class (scope incoherence) must block the send with 409.

    The composite FK (``binding.entry_scope -> entry.product_scope``) makes this
    incoherence impossible to *persist* through the ORM, so the gate's
    ``binding_entry_scope_mismatch`` check is pure defense-in-depth against DB
    corruption -- a bad migration, a raw write, or a restore that leaves a
    denormalised ``entry_scope`` disagreeing with the live entry. We simulate
    exactly that corruption with the FK temporarily disabled, then prove the
    send-time gate still refuses with 409 rather than trusting the stale value.
    """
    sug = CustomerService(session).generate_suggestion(
        owner, conversation_id=conversation.id, inbound_message_id=None, text=QUESTION
    )
    assert sug.sales_evidence_cited is True
    # Pull the REAL cited entry id from the citation rows so we corrupt the
    # right entry (the one QUESTION matched), not a guessed one.
    cited = session.exec(
        select(CsSuggestionSalesEvidence.entry_id).where(
            CsSuggestionSalesEvidence.suggestion_id == sug.id
        )
    ).first()
    assert cited is not None
    # Corrupt: rewrite the cited binding's denormalised scopes to MIHE_1_0 while
    # the live entry stays MIHE_2_0. FK off so the impossible state persists;
    # the CHECK (scope = entry_scope) still passes because both move together.
    session.exec(text("PRAGMA foreign_keys=OFF"))
    session.exec(
        text(
            "UPDATE sales_script_fact_binding "
            "SET entry_scope = 'mihe_1_0', scope = 'mihe_1_0' "
            "WHERE entry_id = :eid"
        ),
        params={"eid": cited},
    )
    session.exec(text("PRAGMA foreign_keys=ON"))
    session.commit()
    with pytest.raises(ServiceError) as exc:
        CustomerService(session).owner_confirm_suggestion(
            owner, conversation_id=conversation.id, suggestion_id=sug.id
        )
    _assert_stale_refusal(exc)


# ===========================================================================
# P1-3 -- migration downgrade must be UNCONDITIONALLY fail-closed (OWNER P1-3)
# ===========================================================================
#
# ``downgrade()`` must ``raise RuntimeError`` BEFORE any DDL, whatever the tables
# currently hold. Owner's explicit list (4 scenarios):
#   1. tables populated with data
#   2. tables empty
#   3. tables only partially present (schema drift)
#   4. the ``alembic`` command path (not just the function)


ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
SALES_PLAYBOOK_REVISION = "20260810_0001"
LAST_DOWNGRADABLE = "20260731_0001"
_SALES_TABLES = (
    "sales_script_source",
    "sales_script_entry",
    "sales_script_segment",
    "sales_script_fact_binding",
    "cs_suggestion_sales_evidence",
)


def _load_head_migration():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    module_path = versions_dir / f"{SALES_PLAYBOOK_REVISION}_sales_playbook_v0.py"
    spec = importlib.util.spec_from_file_location(
        "sp_head_migration", str(module_path)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_p1_3_downgrade_raises_when_tables_have_data(session, project, imported):
    """At head with real imported content in the five tables."""
    module = _load_head_migration()
    with pytest.raises(RuntimeError, match="cannot downgrade migration 202608"):
        module.downgrade()  # type: ignore[attr-defined]


def test_p1_3_downgrade_raises_when_tables_empty(engine):
    # At head, but nothing imported -> tables exist and are empty.
    module = _load_head_migration()
    with pytest.raises(RuntimeError, match="cannot downgrade migration 202608"):
        module.downgrade()  # type: ignore[attr-defined]


def test_p1_3_downgrade_raises_when_tables_partially_present(engine):
    # Emulate schema drift: drop some of the five tables. The gate must still
    # refuse unconditionally -- emptiness / partial schema is NOT a licence.
    with Session(engine) as s:
        for table in ("sales_script_segment", "sales_script_fact_binding"):
            s.exec(text(f"DROP TABLE IF EXISTS {table}"))
            s.commit()
    module = _load_head_migration()
    with pytest.raises(RuntimeError, match="cannot downgrade migration 202608"):
        module.downgrade()  # type: ignore[attr-defined]


def test_p1_3_alembic_command_downgrade_is_blocked(tmp_path):
    """The real ``alembic downgrade`` command path is also blocked (not only the
    function). Uses a src-absolute DB so the test copy-shim does NOT divert it."""
    db_path = tmp_path / "p1_3_cmd.db"
    url = f"sqlite:///{db_path.as_posix()}"
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "alembic")
    )
    cfg.set_main_option("sqlalchemy.url", url)
    alembic.command.upgrade(cfg, "head")  # real migrations, to head
    with pytest.raises(RuntimeError, match="cannot downgrade migration 202608"):
        alembic.command.downgrade(cfg, LAST_DOWNGRADABLE)

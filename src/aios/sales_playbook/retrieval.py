"""Deterministic SalesPlaybook retrieval + safety gates (design §5-§8, D4/D6/D7/D8).

Zero LLM calls, zero randomness: the same message against the same active
generation always yields the same hits, the same ranking and the same evidence
rows (design §10 "replay determinism").

Three gates live here:

* **Version isolation (D4)** -- ``classify_query_scope`` maps a message to a
  RUNTIME scope and ``scopes_for_query`` turns that into the persisted scopes the
  SQL filter may touch. ``UNKNOWN`` short-circuits *before* any query is built,
  so a version-specific claim cannot leak even if a later refactor mishandles
  the result object (D8).
* **Fact safety (D6)** -- an entry is assertable only when EVERY one of its fact
  bindings is ``VERIFIED_CURRENT``; otherwise the mutable spans are masked out of
  both the suggestion text and the allowed-evidence corpus.
* **AI claim ceiling (D7)** -- ``assert_within_claim_ceiling`` rejects any
  generated text carrying a price / percentage / URL / promise signal that is not
  present verbatim in the allowed corpus. Rewriting tone is fine; inventing a
  number is not.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sqlmodel import Session, select

from aios.models import (
    SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
    Artifact,
    SalesScriptEntry,
    SalesScriptFactBinding,
    SalesScriptFactStatus,
    SalesScriptQueryScope,
    SalesScriptScope,
    SalesScriptSegment,
    SalesScriptSegmentType,
    SalesScriptSource,
    SalesScriptSourceStatus,
)

__all__ = [
    "CLARIFICATION_TEXT",
    "ClaimCeilingViolation",
    "EvidenceRevalidationError",
    "RetrievalHit",
    "RetrievalResult",
    "active_source",
    "assert_within_claim_ceiling",
    "classify_query_scope",
    "compose_suggestion_text",
    "probe_domain",
    "retrieve",
    "revalidate_sales_evidence",
    "scopes_for_query",
    "suppress_span",
]

# Design §8 verbatim: the ONLY thing an UNKNOWN-scoped message may produce.
CLARIFICATION_TEXT = "你问的是之前的 1.0 扣子工作流版本，还是现在的 2.0？"

# Marker substituted for a mutable span whose fact binding is not VERIFIED_CURRENT.
UNVERIFIED_MASK = "［待核验］"

_UNVERIFIED_NOTICE = (
    "（含待核验信息：价格 / 佣金 / 会员 / 能力 / 链接 / 促销等具体内容未经核验，"
    "已隐去，请人工核对后再发送。）"
)

_DEFAULT_MIN_MATCH_SCORE = 0.34
_MIN_MATCH_SCORE_ENV = "AIOS_SALES_PLAYBOOK_MIN_SCORE"
DEFAULT_RETRIEVAL_LIMIT = 5


def _min_match_score() -> float:
    """Match floor, env-overridable, validated fail-closed (mirrors CS config)."""
    raw = os.getenv(_MIN_MATCH_SCORE_ENV)
    if raw is None:
        return _DEFAULT_MIN_MATCH_SCORE
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{_MIN_MATCH_SCORE_ENV} must be a float, got {raw!r}") from exc
    if not 0.0 < value <= 1.0:
        raise ValueError(f"{_MIN_MATCH_SCORE_ENV} must be in (0, 1], got {value}")
    return value


class ClaimCeilingViolation(Exception):
    """Generated text asserts something the evidence ceiling does not license."""


# ---------------------------------------------------------------------------
# tokenisation
# ---------------------------------------------------------------------------

# Deliberately a local copy of ``customer_service._tokens`` rather than an
# import: ``customer_service`` imports THIS module, so importing back would be a
# cycle, and the merged KnowledgeFact scorer must keep its exact behaviour.
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(unicodedata.normalize("NFC", value).lower()))


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


# ---------------------------------------------------------------------------
# D4 -- runtime scope classification
# ---------------------------------------------------------------------------

# Explicit evidence only. Weak or ambiguous wording must fall through to
# UNKNOWN: guessing a version is exactly the failure mode D4 forbids.
_V2_MARKERS: tuple[str, ...] = (
    "2.0",
    "2·0",
    "二点零",
    "新版本",
    "新版",
    "合伙人后台",
    "米核2",
    "米核 2",
)
_V1_MARKERS: tuple[str, ...] = (
    "1.0",
    "1·0",
    "一点零",
    "老版本",
    "旧版本",
    "老版",
    "旧版",
    "扣子",
    "coze",
    "米核1",
    "米核 1",
)
_COMPARE_MARKERS: tuple[str, ...] = (
    "区别",
    "差别",
    "对比",
    "相比",
    "比之前",
    "和之前",
    "跟之前",
    "与之前",
    "有什么不同",
    "哪里不一样",
    "升级了什么",
)


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    return any(needle in haystack for needle in needles)


def classify_query_scope(text: str) -> SalesScriptQueryScope:
    """Classify an inbound message into a RUNTIME query scope (design §5).

    Conservative by construction:

    * comparison intent (with or without an explicit version) -> ``COMPARE_1_0_2_0``
      so "和之前扣子那个有什么区别" is answerable instead of being forced to
      UNKNOWN;
    * both versions named at once -> ``COMPARE_1_0_2_0``;
    * exactly one version named -> that version;
    * anything else -> ``UNKNOWN`` (fail-closed).
    """
    lowered = _nfc(text).lower()
    has_v2 = _contains_any(lowered, _V2_MARKERS)
    has_v1 = _contains_any(lowered, _V1_MARKERS)
    wants_compare = _contains_any(lowered, _COMPARE_MARKERS)

    if wants_compare and (has_v1 or has_v2):
        return SalesScriptQueryScope.COMPARE_1_0_2_0
    if has_v1 and has_v2:
        return SalesScriptQueryScope.COMPARE_1_0_2_0
    if has_v2:
        return SalesScriptQueryScope.MIHE_2_0
    if has_v1:
        return SalesScriptQueryScope.MIHE_1_0
    # "区别" with no version anchor at all is still ambiguous -- different from
    # WHAT? -- so it stays UNKNOWN rather than silently comparing the two.
    return SalesScriptQueryScope.UNKNOWN


def scopes_for_query(
    query_scope: SalesScriptQueryScope,
) -> tuple[SalesScriptScope, ...]:
    """Persisted scopes a query scope is allowed to read (design §5 table)."""
    if query_scope is SalesScriptQueryScope.MIHE_1_0:
        return (SalesScriptScope.MIHE_1_0, SalesScriptScope.COMMON)
    if query_scope is SalesScriptQueryScope.MIHE_2_0:
        return (SalesScriptScope.MIHE_2_0, SalesScriptScope.COMMON)
    if query_scope is SalesScriptQueryScope.COMPARE_1_0_2_0:
        return (
            SalesScriptScope.MIHE_1_0,
            SalesScriptScope.MIHE_2_0,
            SalesScriptScope.COMMON,
        )
    return ()


# ---------------------------------------------------------------------------
# D6 -- fact safety
# ---------------------------------------------------------------------------

# Larger = weaker. The reported ``fact_safety`` is the WEAKEST state among an
# entry's bindings, so one stale price downgrades the whole entry.
_SAFETY_WEAKNESS: dict[SalesScriptFactStatus, int] = {
    SalesScriptFactStatus.VERIFIED_CURRENT: 0,
    SalesScriptFactStatus.NEEDS_REVIEW: 1,
    SalesScriptFactStatus.VERSION_1_ONLY: 2,
    SalesScriptFactStatus.STALE: 3,
}


def _weakest_status(
    bindings: Sequence[SalesScriptFactBinding],
) -> SalesScriptFactStatus:
    if not bindings:
        # No mutable-policy fact is bound, so there is no mutable claim to get
        # wrong. The entry is safe to quote as-is.
        return SalesScriptFactStatus.VERIFIED_CURRENT
    return max(
        (SalesScriptFactStatus(binding.status) for binding in bindings),
        key=lambda status: _SAFETY_WEAKNESS[status],
    )


@dataclass(frozen=True)
class RetrievalHit:
    """One matched official entry plus its safety verdict."""

    entry_id: str
    source_entry_id: str
    title: str
    category: str
    product_scope: SalesScriptScope
    rank: int
    score: float
    match_reason: str
    fact_safety: SalesScriptFactStatus
    assertable: bool
    # Verbatim official wording -- what "查看官方原话" must return (design §10).
    official_text: str
    # Same text with every non-VERIFIED_CURRENT span masked -- what the AI
    # suggestion is allowed to draw on.
    safe_text: str
    image_uris: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalResult:
    """Outcome of one retrieval pass, including the D7 evidence ceiling."""

    query_scope: SalesScriptQueryScope
    source_id: str | None
    hits: tuple[RetrievalHit, ...] = ()
    clarification_required: bool = False
    clarification_text: str | None = None
    # Everything the generated suggestion is licensed to assert (design §6 D7):
    # masked official wording + VERIFIED_CURRENT spans + caller-supplied approved
    # KnowledgeFact statements.
    claim_corpus: str = ""
    considered_scopes: tuple[SalesScriptScope, ...] = field(default_factory=tuple)

    @property
    def has_hits(self) -> bool:
        return bool(self.hits)


def active_source(
    session: Session, source_type: str = SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF
) -> SalesScriptSource | None:
    """The single ACTIVE generation, or ``None``. Never mixes two generations."""
    return session.exec(
        select(SalesScriptSource).where(
            SalesScriptSource.source_type == source_type,
            SalesScriptSource.status == SalesScriptSourceStatus.ACTIVE,
        )
    ).first()


def _label_score(entry: SalesScriptEntry, inbound_tokens: set[str]) -> tuple[float, int]:
    """Recall of the entry's own category+title labels inside the message.

    Same shape as the existing KnowledgeFact scorer (``_score_fact``): dividing by
    the ENTRY's token count -- not the message's -- is what makes an off-category
    entry drop out instead of scoring on incidental shared characters.
    """
    label_tokens = _tokens(f"{entry.category} {entry.title}")
    if not label_tokens:
        return (0.0, 0)
    return (len(label_tokens & inbound_tokens) / len(label_tokens), len(label_tokens))


def probe_domain(
    session: Session,
    text: str,
    *,
    source_type: str = SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
) -> bool:
    """Is this message a sales-playbook question at all? (D8 gating)

    Reads ONLY ``category`` / ``title`` labels of the ACTIVE generation and
    returns a BOOLEAN. No entry body, no fact binding, no image and no
    version-specific claim can escape through this function -- which is what lets
    an UNKNOWN-scoped message be answered with the fixed clarification instead of
    either (a) leaking a version-specific claim or (b) turning every unrelated
    customer-service message into a spurious version question.
    """
    source = active_source(session, source_type)
    if source is None:
        return False
    inbound_tokens = _tokens(text)
    if not inbound_tokens:
        return False
    floor = _min_match_score()
    entries = session.exec(
        select(SalesScriptEntry).where(SalesScriptEntry.source_id == source.id)
    ).all()
    return any(_label_score(entry, inbound_tokens)[0] >= floor for entry in entries)


def _load_segments(
    session: Session, entry_id: str
) -> list[SalesScriptSegment]:
    # ``populate_existing`` makes the DB the authority even when the identity
    # map already holds a copy of the row: a safety re-check that silently read
    # a stale in-memory object would defeat its own purpose (P1-2).
    return list(
        session.exec(
            select(SalesScriptSegment)
            .where(SalesScriptSegment.entry_id == entry_id)
            .order_by(SalesScriptSegment.sequence)
            .execution_options(populate_existing=True)
        ).all()
    )


def _official_text(segments: Sequence[SalesScriptSegment]) -> str:
    """The entry's authoritative TEXT body, ordered, images excluded."""
    parts = [
        segment.text_content or ""
        for segment in segments
        if segment.segment_type == SalesScriptSegmentType.TEXT
    ]
    return "\n".join(part for part in parts if part)


def suppress_span(official_text: str, raw_span: str) -> str | None:
    """Deterministically suppress ONE dynamic-fact span, or refuse (P1-1).

    The single authority for "can this span be resolved against the official
    wording, and can it be proven gone afterwards?". Both the retrieval gate
    (:func:`_mask_unverified_bindings`) and the importer's own package gate call
    THIS function, so there is exactly one resolution rule and it cannot drift
    between the two layers.

    Returns the text with EVERY occurrence of ``raw_span`` replaced by
    :data:`UNVERIFIED_MASK`, or ``None`` when the span cannot be resolved
    deterministically -- which is a hard fail-closed signal, never a hint to try
    harder. ``None`` is returned when the span is:

    * empty or whitespace-only -- unresolvable by construction;
    * absent from ``official_text`` under NFC -- a typo, a different Unicode
      form, different whitespace, different punctuation, a truncated body, or a
      span that lives outside the authoritative TEXT (e.g. only in an image
      caption);
    * still present after suppression -- e.g. the span overlaps
      :data:`UNVERIFIED_MASK` itself, so masking would re-introduce it.

    Fuzzy / partial / best-effort matching is NEVER used: a safety mechanism
    that silently no-ops is worse than no safety mechanism, because it reports
    success.
    """
    raw = _nfc(raw_span or "")
    if not raw.strip():
        return None
    if raw not in _nfc(official_text):
        return None
    suppressed = official_text.replace(raw, UNVERIFIED_MASK)
    if raw in _nfc(suppressed):
        return None
    return suppressed


def _mask_unverified_bindings(
    official_text: str,
    bindings: Sequence[SalesScriptFactBinding],
) -> tuple[str | None, bool]:
    """Fail-closed masking of non-VERIFIED_CURRENT dynamic facts (P1-1).

    Resolves each non-VERIFIED_CURRENT span ONLY against the authoritative TEXT
    (``official_text``) via :func:`suppress_span`.

    Returns ``(safe_text, ok)``:

    * ``ok is True``  -- every non-VERIFIED_CURRENT span was found exactly and
      masked; ``safe_text`` is the masked body with no unsafe span remaining.
    * ``ok is False`` -- at least one binding could not be resolved
      deterministically. The caller MUST exclude the whole entry from generative
      evidence; ``safe_text`` is ``None``.
    """
    safe_text = official_text
    for binding in bindings:
        if SalesScriptFactStatus(binding.status) is SalesScriptFactStatus.VERIFIED_CURRENT:
            continue
        suppressed = suppress_span(safe_text, binding.raw_span or "")
        if suppressed is None:
            return (None, False)
        safe_text = suppressed
    return (safe_text, True)


def retrieve(
    session: Session,
    text: str,
    *,
    limit: int = DEFAULT_RETRIEVAL_LIMIT,
    source_type: str = SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
    approved_fact_statements: Sequence[str] = (),
) -> RetrievalResult:
    """Retrieve official entries for one inbound message (design §5 + §7).

    Ranking is ``|entry label tokens ∩ message tokens| / |entry label tokens|``
    over the entry's category+title -- the same recall shape the existing
    KnowledgeFact scorer uses, which is what makes an off-category entry drop out
    instead of scoring on incidental shared characters. Ties break on a shorter
    label set, then on ``source_entry_id``, so replay is byte-stable.
    """
    query_scope = classify_query_scope(text)
    scopes = scopes_for_query(query_scope)

    if query_scope is SalesScriptQueryScope.UNKNOWN:
        # D8 / D4 fail-closed: return BEFORE building any entry statement. The
        # only thing consulted is the boolean domain probe, so no version
        # -specific claim can be read -- let alone leaked -- on this path.
        in_domain = probe_domain(session, text, source_type=source_type)
        return RetrievalResult(
            query_scope=query_scope,
            source_id=None,
            hits=(),
            clarification_required=in_domain,
            clarification_text=CLARIFICATION_TEXT if in_domain else None,
            claim_corpus="",
            considered_scopes=(),
        )

    source = active_source(session, source_type)
    if source is None:
        return RetrievalResult(
            query_scope=query_scope,
            source_id=None,
            hits=(),
            considered_scopes=scopes,
        )

    inbound_tokens = _tokens(text)
    if not inbound_tokens:
        return RetrievalResult(
            query_scope=query_scope,
            source_id=source.id,
            hits=(),
            considered_scopes=scopes,
        )

    entries = list(
        session.exec(
            select(SalesScriptEntry)
            .where(SalesScriptEntry.source_id == source.id)
            .where(
                SalesScriptEntry.product_scope.in_(  # type: ignore[attr-defined]
                    [scope.value for scope in scopes]
                )
            )
        ).all()
    )

    floor = _min_match_score()
    scored: list[tuple[float, int, str, SalesScriptEntry]] = []
    for entry in entries:
        score, label_size = _label_score(entry, inbound_tokens)
        if label_size == 0 or score < floor:
            continue
        scored.append((score, label_size, entry.source_entry_id, entry))

    # -score first (higher is better), then fewer label tokens (more focused),
    # then source_entry_id -- a total order, so no two runs can differ.
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    hits: list[RetrievalHit] = []
    corpus_parts: list[str] = [_nfc(statement) for statement in approved_fact_statements]

    for entry_score, _label_size, _source_entry_id, entry in scored:
        bindings = list(
            session.exec(
                select(SalesScriptFactBinding)
                .where(SalesScriptFactBinding.entry_id == entry.id)
                .order_by(SalesScriptFactBinding.binding_hash)
            ).all()
        )
        statuses = [SalesScriptFactStatus(binding.status) for binding in bindings]

        if query_scope is SalesScriptQueryScope.MIHE_2_0 and (
            SalesScriptFactStatus.VERSION_1_ONLY in statuses
        ):
            # Design §6: VERSION_1_ONLY in a 2.0 context is "unusable", not merely
            # unassertable -- the entry is dropped entirely.
            continue

        segments = _load_segments(session, entry.id)
        official_text = _official_text(segments)

        safe_text, masking_ok = _mask_unverified_bindings(official_text, bindings)
        if not masking_ok:
            # P1-1 fail-closed: this entry cannot be made deterministically
            # safe, so it is excluded from generative evidence entirely. No
            # unsafe span may survive in ``safe_text``, and nothing from this
            # entry is added to the D7 claim corpus.
            continue

        image_uris: list[str] = []
        for segment in segments:
            if segment.segment_type != SalesScriptSegmentType.IMAGE:
                continue
            if segment.artifact_id is None:
                continue
            artifact = session.get(Artifact, segment.artifact_id)
            if artifact is not None:
                image_uris.append(artifact.uri)

        fact_safety = _weakest_status(bindings)
        assertable = fact_safety is SalesScriptFactStatus.VERIFIED_CURRENT

        corpus_parts.append(_nfc(safe_text))
        corpus_parts.append(_nfc(f"{entry.title} {entry.category}"))
        for binding in bindings:
            if SalesScriptFactStatus(binding.status) is (
                SalesScriptFactStatus.VERIFIED_CURRENT
            ):
                corpus_parts.append(_nfc(binding.raw_span))

        hits.append(
            RetrievalHit(
                entry_id=entry.id,
                source_entry_id=entry.source_entry_id,
                title=entry.title,
                category=entry.category,
                product_scope=SalesScriptScope(entry.product_scope),
                rank=len(hits),
                score=entry_score,
                match_reason=(
                    f"scope={query_scope.value};"
                    f"category={entry.category};"
                    f"score={entry_score:.4f}"
                ),
                fact_safety=fact_safety,
                assertable=assertable,
                official_text=official_text,
                safe_text=safe_text,
                image_uris=tuple(image_uris),
            )
        )
        if len(hits) >= limit:
            break

    return RetrievalResult(
        query_scope=query_scope,
        source_id=source.id,
        hits=tuple(hits),
        claim_corpus="\n".join(corpus_parts),
        considered_scopes=scopes,
    )


# ---------------------------------------------------------------------------
# D7 -- AI claim ceiling
# ---------------------------------------------------------------------------

# Signals that a sentence ASSERTS a mutable business fact. Each occurrence must
# be present verbatim in the allowed corpus, otherwise the text invented it.
_CLAIM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[¥￥]\s*\d+(?:[.,]\d+)?"),
    re.compile(r"\d+(?:\.\d+)?\s*(?:元|块|折|%|％)"),
    re.compile(r"百分之[零一二三四五六七八九十百千]+"),
    re.compile(r"https?://\S+"),
    re.compile(r"(?<![\w.])[\w-]+\.(?:com|cn|net|org|io)(?:/\S*)?", re.IGNORECASE),
)

# Promise / performance language. Also corpus-gated rather than banned outright:
# quoting official wording verbatim is inside the ceiling (evidence item 2);
# INTRODUCING a promise is what D7 forbids.
_PROMISE_TERMS: tuple[str, ...] = (
    "保证",
    "承诺",
    "稳赚",
    "包赚",
    "无风险",
    "百分百",
    "百分之百",
    "最低价",
    "永久免费",
    "一定能",
    "肯定能",
    "躺赚",
)


def _collapse(value: str) -> str:
    return re.sub(r"\s+", "", _nfc(value))


def assert_within_claim_ceiling(text: str, corpus: str) -> None:
    """Raise :class:`ClaimCeilingViolation` if ``text`` exceeds the D7 ceiling.

    Rewriting tone, ordering, salutation or organisation is unrestricted -- none
    of that trips a claim pattern. Introducing a price, a commission percentage,
    a URL or a promise that is not in ``corpus`` does.
    """
    collapsed_corpus = _collapse(corpus)
    normalised = _nfc(text)

    for pattern in _CLAIM_PATTERNS:
        for match in pattern.finditer(normalised):
            claim = _collapse(match.group(0))
            if claim and claim not in collapsed_corpus:
                raise ClaimCeilingViolation(
                    f"fabricated claim {match.group(0)!r} is not in the evidence ceiling"
                )

    collapsed_text = _collapse(normalised)
    for term in _PROMISE_TERMS:
        if term in collapsed_text and term not in collapsed_corpus:
            raise ClaimCeilingViolation(
                f"promise term {term!r} is not in the evidence ceiling"
            )


def compose_suggestion_text(result: RetrievalResult) -> str:
    """Build the HUMAN_CONFIRM suggestion body from a retrieval result.

    Safe by construction: only masked official wording reaches the output, so the
    result always satisfies :func:`assert_within_claim_ceiling` against
    ``result.claim_corpus`` (the wiring asserts exactly that before persisting).
    """
    if result.clarification_required:
        return result.clarification_text or CLARIFICATION_TEXT
    if not result.hits:
        return ""

    blocks = ["以下为官方话术，请人工确认后再发送："]
    for hit in result.hits:
        body = hit.safe_text.strip()
        blocks.append(f"【{hit.title}】\n{body}" if body else f"【{hit.title}】")
    if any(not hit.assertable for hit in result.hits):
        blocks.append(_UNVERIFIED_NOTICE)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# P1-2 -- send-time revalidation against LIVE state
# ---------------------------------------------------------------------------


class EvidenceRevalidationError(Exception):
    """Live evidence no longer licenses the pending outbound text (P1-2).

    ``reason`` is an INTERNAL diagnostic code. It exists so the failure is
    debuggable from a log/audit trail; callers MUST map it to a business-readable
    message and must never surface the code, an entry id, a binding id or a
    generation id to an external actor.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def revalidate_sales_evidence(
    session: Session,
    *,
    recorded_fact_safety: Mapping[str, SalesScriptFactStatus | str],
    outgoing_text: str,
    source_type: str = SALES_SCRIPT_SOURCE_TYPE_MIHE_EBF,
) -> None:
    """Re-verify cited evidence against LIVE state immediately before sending.

    Validation at *generation* time is not enough: an owner may sit on a pending
    suggestion while a fact is revoked, a new generation is imported, or an entry
    is withdrawn. This function is the send-time gate, and it is fail-closed on
    every branch -- it raises :class:`EvidenceRevalidationError` and never
    "repairs", re-masks or silently drops part of the copy.

    ``recorded_fact_safety`` maps each cited ``SalesScriptEntry.id`` to the
    weakest fact state recorded when the suggestion was generated.
    ``outgoing_text`` is the text that would ACTUALLY be sent -- including an
    owner-edited body, so a human cannot re-introduce a value the live state no
    longer verifies.

    Checks, in order, per cited entry:

    1. the entry still exists;
    2. it still belongs to the single ACTIVE generation (a superseded /
       withdrawn generation invalidates every citation into it);
    3. its bindings are still structurally scope-coherent with the entry;
    4. the entry is still deterministically maskable (the P1-1 gate re-run on
       live rows, so a binding mutated into an unresolvable span blocks a send);
    5. its aggregate fact safety has not been DOWNGRADED since generation;
    6. no currently non-``VERIFIED_CURRENT`` span appears verbatim in the
       outgoing text.

    A no-op when nothing was cited: the KnowledgeFact-only path is untouched.
    """
    if not recorded_fact_safety:
        return

    source = active_source(session, source_type)
    if source is None:
        # The generation the suggestion was built from is no longer live.
        raise EvidenceRevalidationError("no_active_generation")

    normalised_outgoing = _nfc(outgoing_text)

    # Deterministic order: identical inputs must fail on the identical reason.
    for entry_id in sorted(recorded_fact_safety):
        recorded = SalesScriptFactStatus(recorded_fact_safety[entry_id])

        entry = session.exec(
            select(SalesScriptEntry)
            .where(SalesScriptEntry.id == entry_id)
            .execution_options(populate_existing=True)
        ).first()
        if entry is None:
            raise EvidenceRevalidationError("evidence_entry_missing")
        if entry.source_id != source.id:
            raise EvidenceRevalidationError("evidence_generation_not_active")

        entry_scope = SalesScriptScope(entry.product_scope)
        bindings = list(
            session.exec(
                select(SalesScriptFactBinding)
                .where(SalesScriptFactBinding.entry_id == entry.id)
                .order_by(SalesScriptFactBinding.binding_hash)
                .execution_options(populate_existing=True)
            ).all()
        )

        for binding in bindings:
            # Product-scope coherence (D3). The composite FK and CHECK already
            # carry this at the database boundary; re-asserting it here keeps
            # the send-time contract legible and independent of that boundary.
            if SalesScriptScope(binding.entry_scope) is not entry_scope:
                raise EvidenceRevalidationError("binding_entry_scope_mismatch")
            binding_scope = SalesScriptScope(binding.scope)
            if (
                binding_scope is not SalesScriptScope.COMMON
                and binding_scope is not entry_scope
            ):
                raise EvidenceRevalidationError("binding_scope_incompatible")

        _safe_text, masking_ok = _mask_unverified_bindings(
            _official_text(_load_segments(session, entry.id)), bindings
        )
        if not masking_ok:
            raise EvidenceRevalidationError("evidence_not_maskable")

        live = _weakest_status(bindings)
        if _SAFETY_WEAKNESS[live] > _SAFETY_WEAKNESS[recorded]:
            raise EvidenceRevalidationError("fact_safety_downgraded")

        for binding in bindings:
            if (
                SalesScriptFactStatus(binding.status)
                is SalesScriptFactStatus.VERIFIED_CURRENT
            ):
                continue
            raw = _nfc(binding.raw_span or "")
            if raw and raw in normalised_outgoing:
                raise EvidenceRevalidationError("unverified_fact_in_outgoing_text")

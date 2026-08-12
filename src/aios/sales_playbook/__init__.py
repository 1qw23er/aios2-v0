"""SalesPlaybook V0 -- read-only official sales-script evidence source.

Opt-in subpackage. Nothing here is imported by ``aios`` at startup except the
retrieval adapter that :mod:`aios.customer_service` wires in explicitly, so the
importer (a management command, NOT a migration) never runs implicitly.

Scope boundary (design §0 / §11): this is an EVIDENCE SOURCE for the existing
customer-service pipeline. It is NOT a CRM, NOT auto-send, NOT auto-sales, and
it writes ZERO ``KnowledgeFact`` / ``KnowledgeCandidate`` rows.
"""

from aios.sales_playbook.importer import (
    ImportResult,
    SalesPlaybookImportError,
    compute_extraction_manifest_hash,
    compute_source_file_hash,
    import_package,
)
from aios.sales_playbook.retrieval import (
    CLARIFICATION_TEXT,
    ClaimCeilingViolation,
    RetrievalHit,
    RetrievalResult,
    active_source,
    assert_within_claim_ceiling,
    classify_query_scope,
    compose_suggestion_text,
    probe_domain,
    retrieve,
    scopes_for_query,
)

__all__ = [
    "CLARIFICATION_TEXT",
    "ClaimCeilingViolation",
    "ImportResult",
    "RetrievalHit",
    "RetrievalResult",
    "SalesPlaybookImportError",
    "active_source",
    "assert_within_claim_ceiling",
    "classify_query_scope",
    "compose_suggestion_text",
    "compute_extraction_manifest_hash",
    "compute_source_file_hash",
    "import_package",
    "probe_domain",
    "retrieve",
    "scopes_for_query",
]

"""Content taxonomy controlled vocabulary (PILOT-2A).

Independent, versioned vocabulary for lead-gen / content experiments.

Deliberately decoupled from the architecture-locked ``CANONICAL_KNOWLEDGE_TAGS``
white-list used by the knowledge-capture path. Changing this vocabulary is a
pure in-Pilot code change and MUST NOT mutate the knowledge tag white-list.
"""
from __future__ import annotations

from enum import StrEnum

# Bumped only when the vocabulary itself changes (NOT the knowledge tags).
CONTENT_TAXONOMY_VERSION = 1


class TaxonomyDimension(StrEnum):
    TRACK = "track"
    AUDIENCE = "audience"
    USE_CASE = "use_case"
    VALUE_PROP = "value_prop"
    FORMAT = "format"
    HOOK = "hook"


# Six dimensions, fixed values per design v2 §3.2.
CONTENT_TAXONOMY: dict[TaxonomyDimension, list[str]] = {
    TaxonomyDimension.TRACK: ["ip", "leadgen"],
    TaxonomyDimension.AUDIENCE: [
        "apparel",
        "accessories",
        "home",
        "food",
        "general_ecom",
        "side_hustle",
    ],
    TaxonomyDimension.USE_CASE: [
        "hero_image",
        "model_image",
        "scene_image",
        "detail_page",
        "product_video",
    ],
    TaxonomyDimension.VALUE_PROP: [
        "save_money",
        "save_time",
        "no_design_skill",
        "better_creative",
        "batch_production",
    ],
    TaxonomyDimension.FORMAT: [
        "tutorial",
        "real_case",
        "before_after",
        "experiment",
        "pitfall",
        "tool_rec",
    ],
    TaxonomyDimension.HOOK: ["cost", "efficiency", "result", "curiosity", "pain"],
}


def validate_taxonomy(dim: TaxonomyDimension, value: str) -> bool:
    """True iff ``value`` is a legal term for ``dim``."""
    return value in CONTENT_TAXONOMY.get(dim, [])


def validate_content_record(record: dict) -> list[str]:
    """Return violation messages; empty list means the record is valid.

    ``record`` maps dimension name -> value for every dimension.
    """
    violations: list[str] = []
    for dim in TaxonomyDimension:
        v = record.get(dim.value)
        if v is None:
            violations.append(f"missing dimension: {dim.value}")
        elif not validate_taxonomy(dim, v):
            violations.append(f"invalid {dim.value}={v!r}")
    return violations

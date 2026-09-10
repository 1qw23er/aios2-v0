"""Model price table -- the price source LOCAL runs were missing (GAP-3 Stage 2).

Context
-------
``docs/Budget_Cost_Boundary.md`` (Stage 1) declares that
``Project.budget_used`` governs **delegated** runs carrying a MEASURED
currency cost, while LOCAL runs (``DelegationMode.LOCAL``, the in-process
``LLMExecutionAdapter``) record usage tokens but no cost: *visible, not
governed*. The reason is not architectural -- ``complete_local_run`` already
feeds the shared ``accrue_run_budget`` path -- but the absence of a price
source: tokens are not currency, and a fabricated conversion would pollute the
measured-spend SSoT.

Stage 2 supplies that source as an owner-supplied env JSON table:

.. code-block:: json

    {
      "deepseek-ai/deepseek-v4-pro": {"input_per_1m": 1.0, "output_per_1m": 2.0},
      "my-finetune": {"input_per_1m": 0.0, "output_per_1m": 0.0}
    }

* **Unit**: currency per **1,000,000** tokens (the industry convention),
  input and output priced separately. The currency is whatever the owner uses
  for ``Project.budget_limit`` -- this module never converts.
* **Zero migration**: no column, no table, no new entity. The model name is
  supplied by the caller at terminalization time (or read from the provider's
  own ``usage["model"]``), because a ``DelegatedRun`` row has no model column.
* **Fail-safe**: an unlisted model yields NO cost. There is no default price,
  no cross-model inference and no estimate fallback -- "no price" is reported
  as "no measurement", exactly like a provider that reports no cost. A
  malformed table disables pricing instead of raising: a price bug must never
  fail an execution.

Non-goals
---------
* No cost for REMOTE runs -- their cost is the provider's own measured value.
* No budget enforcement inside this module -- the derived cost flows through
  the existing ``accrue_run_budget`` and is therefore governed by
  ``check_budget`` like any other accrued cost.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

#: Env var holding the price table as JSON (model -> {input_per_1m, output_per_1m}).
MODEL_PRICING_ENV = "AIOS_MODEL_PRICING"

#: Token keys, in precedence order. ``usage`` schemas are heterogeneous
#: (OpenAI-style vs Anthropic-style); the first *usable* key wins.
INPUT_TOKEN_KEYS: tuple[str, ...] = ("prompt_tokens", "input_tokens")
OUTPUT_TOKEN_KEYS: tuple[str, ...] = ("completion_tokens", "output_tokens")

_TOKENS_PER_PRICE_UNIT = 1_000_000
_COST_DECIMALS = 6


class ModelPrice(NamedTuple):
    """Currency per 1M tokens, input and output priced separately."""

    input_per_1m: float
    output_per_1m: float


def load_model_pricing(env: Mapping[str, str] | None = None) -> dict[str, ModelPrice]:
    """Read and parse the price table (``{}`` when unset or unusable).

    ``env`` defaults to ``os.environ`` and exists so tests can pass an explicit
    mapping instead of monkeypatching the process environment.
    """
    source: Mapping[str, str] = os.environ if env is None else env
    return parse_model_pricing(source.get(MODEL_PRICING_ENV))


def parse_model_pricing(raw: str | None) -> dict[str, ModelPrice]:
    """Parse a price table payload; unusable entries are dropped, not fatal."""
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("%s is not valid JSON -- model pricing is DISABLED", MODEL_PRICING_ENV)
        return {}
    if not isinstance(payload, dict):
        logger.warning(
            "%s must be a JSON object (model -> price) -- model pricing is DISABLED",
            MODEL_PRICING_ENV,
        )
        return {}
    table: dict[str, ModelPrice] = {}
    for model, entry in payload.items():
        price = _coerce_price(entry)
        if price is None:
            logger.warning(
                "ignoring invalid %s entry for model %r (needs numeric, "
                "non-negative input_per_1m AND output_per_1m)",
                MODEL_PRICING_ENV,
                model,
            )
            continue
        table[model] = price
    return table


def derive_run_cost(
    usage: Mapping[str, Any] | None,
    *,
    model: str | None = None,
    env: Mapping[str, str] | None = None,
) -> float | None:
    """Derive a LOCAL run's currency cost from reported usage.

    Returns ``None`` when there is nothing trustworthy to charge: no usage, no
    token counts in it, no model identity, or no price entry for that model.
    ``0.0`` is a real answer (a genuinely free model) and is NOT ``None``.
    """
    if not isinstance(usage, Mapping):
        return None
    resolved = model
    if not resolved:
        # Fall back to the model the provider says it actually served.
        reported = usage.get("model")
        resolved = reported if isinstance(reported, str) and reported else None
    if not resolved:
        return None
    price = load_model_pricing(env=env).get(resolved)
    if price is None:
        # Never fabricate: an unlisted model has no trustworthy price.
        return None
    input_tokens = _token_count(usage, INPUT_TOKEN_KEYS)
    output_tokens = _token_count(usage, OUTPUT_TOKEN_KEYS)
    if input_tokens is None and output_tokens is None:
        return None
    cost = (
        (input_tokens or 0.0) * price.input_per_1m
        + (output_tokens or 0.0) * price.output_per_1m
    ) / _TOKENS_PER_PRICE_UNIT
    return round(cost, _COST_DECIMALS)


def _coerce_price(entry: Any) -> ModelPrice | None:
    if not isinstance(entry, Mapping):
        return None
    values: dict[str, float] = {}
    for key in ("input_per_1m", "output_per_1m"):
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if value < 0:
            return None
        values[key] = float(value)
    return ModelPrice(**values)


def _token_count(usage: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value < 0:
            continue
        return float(value)
    return None

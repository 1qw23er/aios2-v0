"""GAP-3 Stage 2 -- model price table (``AIOS_MODEL_PRICING``).

Why this exists
---------------
LOCAL runs (``DelegationMode.LOCAL``, the in-process ``LLMExecutionAdapter``)
record the provider's ``usage`` (tokens) but never a currency ``cost``, so
``Project.budget_used`` is blind to in-process LLM spend. Stage 1 declared
that boundary (``docs/Budget_Cost_Boundary.md``); Stage 2 supplies the missing
price source.

Design under test
-----------------
* Owner-supplied env JSON maps ``model -> {input_per_1m, output_per_1m}``
  (currency per 1,000,000 tokens, input and output priced separately).
* ``complete_local_run`` derives the cost from the usage the provider already
  reported and hands it to the EXISTING ``accrue_run_budget`` -- so LOCAL runs
  become billable without a new writer, a new column, or a migration.
* Fail-safe: no price entry == no cost. A price is never guessed, never
  defaulted from another model, and never inferred from an estimate.
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from aios.db import get_database_url, get_engine
from aios.execution import LLMExecutionAdapter
from aios.execution_run import complete_local_run, create_local_run
from aios.model_pricing import (
    MODEL_PRICING_ENV,
    derive_run_cost,
    load_model_pricing,
)
from aios.models import (
    DelegatedRun,
    DelegatedRunStatus,
    DelegationMode,
    Project,
    Task,
    TaskStatus,
)
from aios.usage_metering import budget_reconciliation, project_usage

MODEL = "test-model-1"

PRICING = {MODEL: {"input_per_1m": 1.0, "output_per_1m": 2.0}}


def _env_with_pricing(table: dict | None = None) -> dict[str, str]:
    """An explicit env mapping (no monkeypatching) for pure unit tests."""
    if table is None:
        return {}
    return {MODEL_PRICING_ENV: json.dumps(table)}


@pytest.fixture
def db(authenticated_client) -> Session:
    """A session bound to the migrated test database (``authenticated_client``
    sets the URL and runs the Alembic upgrade on its lifespan)."""
    with Session(get_engine(get_database_url())) as s:
        yield s


def _seed(session: Session, *, budget_limit: float = 10.0):
    project = Project(name="p", objective="o", budget_limit=budget_limit)
    session.add(project)
    session.commit()
    session.refresh(project)
    task = Task(
        project_id=project.id,
        title="t",
        description="d",
        status=TaskStatus.BACKLOG,
        output_schema={"type": "object"},
        estimated_cost=0.0,
    )
    session.add(task)
    session.commit()
    session.refresh(task)
    return project, task


def _local_run(session: Session, project, task, *, key: str = "k") -> DelegatedRun:
    return create_local_run(
        session,
        task_id=task.id,
        project_id=project.id,
        attempt=1,
        idempotency_key=key,
    )


def _refresh(session: Session, obj):
    session.expire_all()
    return session.get(type(obj), obj.id)


# --- Config parsing --------------------------------------------------------


def test_no_env_entry_yields_empty_table() -> None:
    assert load_model_pricing(env={}) == {}


def test_parses_flat_model_table() -> None:
    table = load_model_pricing(env=_env_with_pricing(PRICING))
    assert set(table) == {MODEL}
    assert table[MODEL].input_per_1m == 1.0
    assert table[MODEL].output_per_1m == 2.0


def test_malformed_json_is_ignored_not_fatal() -> None:
    # A broken price table must disable pricing, never crash an execution path.
    assert load_model_pricing(env={MODEL_PRICING_ENV: "{not json"}) == {}


def test_non_object_json_is_ignored() -> None:
    assert load_model_pricing(env={MODEL_PRICING_ENV: "[1, 2, 3]"}) == {}


@pytest.mark.parametrize(
    "entry",
    [
        {"input_per_1m": 1.0},  # missing output side
        {"input_per_1m": "1.0", "output_per_1m": 2.0},  # non-numeric
        {"input_per_1m": -1.0, "output_per_1m": 2.0},  # negative price
        "1.0",  # not an object
        None,
    ],
)
def test_invalid_entries_are_skipped(entry) -> None:
    table = load_model_pricing(env=_env_with_pricing({MODEL: entry}))
    assert table == {}


def test_valid_siblings_survive_an_invalid_entry() -> None:
    table = load_model_pricing(env=_env_with_pricing({MODEL: PRICING[MODEL], "bad": "x"}))
    assert set(table) == {MODEL}


# --- Cost derivation (pure) ------------------------------------------------


def test_derive_cost_openai_style_keys() -> None:
    # 1M input @1.0 + 2M output @2.0 = 1.0 + 4.0
    cost = derive_run_cost(
        {"prompt_tokens": 1_000_000, "completion_tokens": 2_000_000},
        model=MODEL,
        env=_env_with_pricing(PRICING),
    )
    assert cost == 5.0


def test_derive_cost_anthropic_style_keys() -> None:
    # 2M input @1.0 + 0.5M output @2.0 = 2.0 + 1.0
    cost = derive_run_cost(
        {"input_tokens": 2_000_000, "output_tokens": 500_000},
        model=MODEL,
        env=_env_with_pricing(PRICING),
    )
    assert cost == 3.0


def test_derive_cost_rounds_to_six_decimals() -> None:
    cost = derive_run_cost(
        {"prompt_tokens": 1, "completion_tokens": 1},
        model=MODEL,
        env=_env_with_pricing(PRICING),
    )
    # 1/1e6 * 1.0 + 1/1e6 * 2.0 = 0.000003
    assert cost == 0.000003


def test_unknown_model_yields_none() -> None:
    # Never fabricate: an unlisted model has no trustworthy price.
    cost = derive_run_cost(
        {"prompt_tokens": 10, "completion_tokens": 20},
        model="model-not-in-table",
        env=_env_with_pricing(PRICING),
    )
    assert cost is None


def test_empty_table_yields_none() -> None:
    cost = derive_run_cost(
        {"prompt_tokens": 10, "completion_tokens": 20},
        model=MODEL,
        env={},
    )
    assert cost is None


def test_missing_usage_yields_none() -> None:
    assert derive_run_cost(None, model=MODEL, env=_env_with_pricing(PRICING)) is None


def test_usage_without_token_keys_yields_none() -> None:
    # total-only usage cannot be split into input/output -> no price applied.
    cost = derive_run_cost(
        {"total_tokens": 30},
        model=MODEL,
        env=_env_with_pricing(PRICING),
    )
    assert cost is None


def test_non_numeric_token_values_are_ignored() -> None:
    cost = derive_run_cost(
        {"prompt_tokens": "10", "completion_tokens": 20},
        model=MODEL,
        env=_env_with_pricing(PRICING),
    )
    # Only the output side is measurable -> charged for output alone.
    assert cost == pytest.approx(0.00004)


def test_model_falls_back_to_usage_reported_model() -> None:
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0, "model": MODEL}
    assert derive_run_cost(usage, env=_env_with_pricing(PRICING)) == 1.0


def test_zero_priced_model_yields_zero_not_none() -> None:
    cost = derive_run_cost(
        {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000},
        model=MODEL,
        env=_env_with_pricing({MODEL: {"input_per_1m": 0.0, "output_per_1m": 0.0}}),
    )
    # A genuinely free model is 0.0 (a real measurement), not "unknown".
    assert cost == 0.0


# --- Terminalization + accrual (DB) ---------------------------------------


def test_local_run_with_price_table_accrues_budget(db, monkeypatch) -> None:
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-priced")
    ok = complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        model=MODEL,
    )
    assert ok is True
    persisted = _refresh(db, run)
    assert persisted.cost == 2.0  # 1.0 + 1.0
    assert persisted.delegation_mode == DelegationMode.LOCAL
    assert _refresh(db, project).budget_used == 2.0


def test_local_run_without_price_table_stays_outside_budget(db, monkeypatch) -> None:
    """Stage 1 behaviour is preserved when no price table is configured."""
    monkeypatch.delenv(MODEL_PRICING_ENV, raising=False)
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-unpriced")
    complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        model=MODEL,
    )
    persisted = _refresh(db, run)
    # usage is still recorded verbatim; no cost is fabricated.
    assert persisted.usage == {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    assert persisted.cost == 0.0
    assert _refresh(db, project).budget_used == 0.0


def test_unknown_model_does_not_accrue(db, monkeypatch) -> None:
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-unknown")
    complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        model="another-model",
    )
    assert _refresh(db, run).cost == 0.0
    assert _refresh(db, project).budget_used == 0.0


def test_explicit_cost_wins_over_derived(db, monkeypatch) -> None:
    """A real provider cost is authoritative; derivation is only a fallback."""
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-explicit")
    complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
        cost=7.5,
        model=MODEL,
    )
    assert _refresh(db, run).cost == 7.5
    assert _refresh(db, project).budget_used == 7.5


def test_derived_cost_accrues_exactly_once(db, monkeypatch) -> None:
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-once")
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
    assert complete_local_run(
        db, run_id=run.id, status=DelegatedRunStatus.SUCCEEDED, usage=usage, model=MODEL
    )
    # Second terminalization is a no-op (the run is already settled).
    assert not complete_local_run(
        db, run_id=run.id, status=DelegatedRunStatus.FAILED, usage=usage, model=MODEL
    )
    assert _refresh(db, project).budget_used == 2.0


def test_derived_cost_reconciles_through_the_single_writer(db, monkeypatch) -> None:
    """LOCAL spend now lands in budget_used via accrue_run_budget, so the
    reconciliation identity still holds (W6 untouched)."""
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-recon")
    complete_local_run(
        db,
        run_id=run.id,
        status=DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000},
        model=MODEL,
    )
    assert _refresh(db, project).budget_used == 4.0
    recon = budget_reconciliation(db, project_id=project.id)
    assert recon["matches"] is True
    assert recon["accrued_measured_spend"] == 4.0
    # ...and the run left the "no measured cost" blind spot entirely.
    projection = project_usage(db, project_id=project.id)
    assert projection["measured_spend"] == 4.0
    assert projection["no_measured_cost_run_count"] == 0


# --- Adapter wiring --------------------------------------------------------


def test_adapter_terminalization_passes_its_model(db, monkeypatch) -> None:
    """End-to-end wiring: the adapter's configured model reaches the price
    table through ``_finish_local_run`` (no separate plumbing)."""
    monkeypatch.setenv(MODEL_PRICING_ENV, json.dumps(PRICING))
    project, task = _seed(db)
    run = _local_run(db, project, task, key="k-adapter")
    adapter = LLMExecutionAdapter(model=MODEL, api_key="test-key")
    adapter._finish_local_run(
        # Contract: the captured run id (str), not the ORM instance -- by the
        # time terminalization runs the instance may already be detached.
        run.id,
        DelegatedRunStatus.SUCCEEDED,
        usage={"prompt_tokens": 1_000_000, "completion_tokens": 500_000},
    )
    assert _refresh(db, run).cost == 2.0
    assert _refresh(db, project).budget_used == 2.0

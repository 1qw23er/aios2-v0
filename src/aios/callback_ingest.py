"""Callback / Webhook Ingest P1 — authenticated, evidence-only inbound signal.

Scope
-----
A delegated remote agent may *push* its run outcome to AIOS instead of waiting
for AIOS to poll for it. This module owns the authentication of that push and
the persistence of the pushed **evidence**.

The single most important rule of this module:

    callback is EVIDENCE, never AUTHORITY.

Concretely, this module NEVER:

* writes ``DelegatedRun.status``;
* writes ``DelegatedRun.cost`` / ``usage`` / ``error``;
* writes ``Project.budget_used``;
* calls ``complete_run()`` or ``accrue_run_budget()``;
* bypasses or participates in lease fencing.

It ONLY records "the remote told us this, at this time, under this token" on
the run row. Terminalization remains the exclusive job of the existing
lease-owning execution path (``complete_run`` -> ``accrue_run_budget``), so
there is still exactly one terminal-status writer and exactly one budget
writer, exactly as the P0 lifecycle and C accrual invariants require.

A callback is therefore an **accelerator, never a correctness dependency**: if
it is lost, malformed, late, or never delivered, polling and recovery behave
exactly as they did before this module existed.

GAP-4 (late evidence): a callback that arrives after its run already settled
is no longer silently dropped. The FIRST late delivery stages its evidence
through the same ``callback_received_at`` CAS gate (first-wins, exactly like
the received path) while the terminal status, cost, usage, lease and budget
stay untouched; repeat deliveries classify as duplicate/conflict by ``jti``.

No new entity is introduced (no ``Callback`` / ``WebhookEvent`` / ``Ingest``
table): the evidence lives in two nullable columns on ``DelegatedRun``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlmodel import Session

from aios.audit import AuditEvent, append_audit, redact_secrets
from aios.execution_run import TERMINAL_RUN_STATUSES
from aios.models import DelegatedRun, new_id

# --- Token -----------------------------------------------------------------

#: Signing algorithm. Deliberately the same primitive as the existing bootstrap
#: token (``aios.api.security``) so no new key material or crypto is introduced.
CALLBACK_TOKEN_ALG = "HS256"

#: Type discriminator inside the signed header.
CALLBACK_TOKEN_TYP = "cbk"

#: Header carrying the run-scoped callback token. It is a HEADER, not a query
#: parameter: a token in a URL leaks into access logs, proxies and APM traces.
CALLBACK_TOKEN_HEADER = "X-AIOS-Callback-Token"

#: Extra lifetime added to the agent's delegation timeout so a callback that
#: arrives just as the run times out is still verifiable (clock skew + the
#: provider's own retry delay).
CALLBACK_TTL_GRACE_SECONDS: float = 300.0

#: Hard bound on the accepted callback body (mirrors the 16 KiB inbound limit
#: already enforced by the customer-service ingest path).
CALLBACK_MAX_BODY_BYTES: int = 16 * 1024

#: Env var + default for the AIOS-hosted callback base URL. The token is NEVER
#: placed in this URL.
CALLBACK_BASE_URL_ENV = "AIOS_CALLBACK_BASE_URL"
_DEFAULT_CALLBACK_BASE_URL = "http://127.0.0.1:8000"

#: Status vocabulary a provider may push. ``running`` is progress evidence only
#: and never terminates anything.
CALLBACK_STATUSES: frozenset[str] = frozenset(
    {"running", "succeeded", "failed", "cancelled"}
)
#: Subset that means "this run is over" (a terminal *signal*, not a status write).
CALLBACK_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled"}
)


class CallbackAuthError(Exception):
    """Raised when a callback token is missing / malformed / expired / forged.

    HTTP-agnostic on purpose: the API layer maps it to the existing ``401``
    authentication convention (which also guarantees no run-existence leak,
    because an unauthenticated caller can never name a run).
    """

    def __init__(self, reason: str, claims: CallbackClaims | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # Present only when the token was cryptographically valid but unusable
        # (expired, or bound to another run). It lets the API layer audit the
        # attempt against a real run id. A forged / malformed token carries no
        # claims, so an unauthenticated caller cannot plant arbitrary ids in the
        # audit trail.
        self.claims = claims


@dataclass(frozen=True)
class CallbackClaims:
    """Verified, run-scoped claims of a callback token."""

    run_id: str
    attempt: int
    agent_id: str
    jti: str
    exp: int


@dataclass(frozen=True)
class CallbackIngestResult:
    """Outcome of one authenticated callback delivery.

    ``outcome`` is one of: ``received`` / ``duplicate`` / ``conflict`` /
    ``late`` / ``unknown_run``. It is recorded in the audit trail; it is
    deliberately NOT reflected in the HTTP response body, so an external caller
    cannot tell "accepted" from "we already knew" from "no such run".
    """

    outcome: str
    run_id: str
    jti: str


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _signing_key() -> str:
    """The owner API key doubles as the HMAC secret (same as bootstrap tokens).

    Imported lazily: ``aios.api.security`` pulls in FastAPI, and this module is
    also used from the non-HTTP delegation path. An unconfigured owner key
    raises the same fail-closed ``503`` as ``authenticate_owner``.
    """
    from aios.api.security import _owner_signing_key

    return _owner_signing_key()


def callback_base_url() -> str:
    """Base URL of the AIOS-hosted callback endpoint (env-overridable)."""
    return (os.environ.get(CALLBACK_BASE_URL_ENV) or _DEFAULT_CALLBACK_BASE_URL).rstrip(
        "/"
    )


def build_callback_url(run_id: str) -> str:
    """The AIOS-hosted callback URL for one run.

    Note what is NOT here: the token. It travels in ``CALLBACK_TOKEN_HEADER``
    so it can never be captured by URL logging.
    """
    return f"{callback_base_url()}/runs/{run_id}/callback"


def mint_callback_token(
    *,
    run_id: str,
    attempt: int,
    agent_id: str,
    ttl_seconds: float,
    jti: str | None = None,
    key: str | None = None,
) -> str:
    """Mint a run-scoped HMAC-SHA256 callback token.

    Claims bind ``run_id`` + ``attempt`` + ``agent_id``, so a token cannot be
    replayed against another run, another retry attempt, or another agent. The
    token is never persisted anywhere -- only its ``jti`` is recorded (inside
    the stored evidence) for duplicate detection.
    """
    if key is None:
        key = _signing_key()
    now = int(time.time())
    header = {"alg": CALLBACK_TOKEN_ALG, "typ": CALLBACK_TOKEN_TYP}
    payload = {
        "run_id": run_id,
        "attempt": int(attempt),
        "agent_id": agent_id,
        "exp": now + int(ttl_seconds),
        "jti": jti or uuid.uuid4().hex,
    }
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = hmac.new(key.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def verify_callback_token(token: str | None, *, key: str | None = None) -> CallbackClaims:
    """Verify a callback token. Raises ``CallbackAuthError`` on any failure.

    Every failure mode collapses into an opaque reason: a caller can never
    distinguish "wrong signature" from "unknown run" from "expired".
    """
    if not token:
        raise CallbackAuthError("missing")
    parts = token.split(".")
    if len(parts) != 3:
        raise CallbackAuthError("malformed")
    header_b64, payload_b64, sig_b64 = parts
    if key is None:
        key = _signing_key()
    expected = hmac.new(
        key.encode("utf-8"), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001 - malformed base64 is a forgery signal
        raise CallbackAuthError("invalid_signature") from None
    if not hmac.compare_digest(expected, provided):
        raise CallbackAuthError("invalid_signature")
    try:
        payload = json.loads(_b64url_decode(payload_b64))
        claims = CallbackClaims(
            run_id=str(payload["run_id"]),
            attempt=int(payload["attempt"]),
            agent_id=str(payload["agent_id"]),
            jti=str(payload.get("jti") or ""),
            exp=int(payload["exp"]),
        )
    except Exception:  # noqa: BLE001 - any shape deviation is a forgery signal
        raise CallbackAuthError("invalid_claims") from None
    if int(time.time()) > claims.exp:
        raise CallbackAuthError("expired", claims)
    return claims


def claims_match(claims: CallbackClaims, *, run_id: str) -> bool:
    """Bind verified claims to the addressed run (no cross-run / cross-attempt use)."""
    return claims.run_id == run_id


# --- Payload ---------------------------------------------------------------


def normalise_callback_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Validate + normalise a provider payload. Raises ``ValueError`` if invalid.

    Only the signal is kept: provider status, optional error, optional cost /
    usage evidence and correlation data. ``cost`` / ``usage`` are stored as
    *evidence* and are only ever applied to the run by the lease-owning
    execution path.
    """
    if not isinstance(body, dict):
        raise ValueError("callback body must be a JSON object")
    status = body.get("status")
    if status not in CALLBACK_STATUSES:
        raise ValueError("callback status is not a recognised value")
    cost = body.get("cost")
    if cost is not None:
        cost = float(cost)
    usage = body.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise ValueError("callback usage must be an object")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("callback error must be a string")
    return {
        "status": status,
        "error": error,
        "cost": cost,
        "usage": usage,
        "remote_run_id": body.get("remote_run_id"),
        "finished_at": body.get("finished_at"),
    }


def _naive_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


# --- Ingest ----------------------------------------------------------------


def callback_signal(run: DelegatedRun) -> dict[str, Any] | None:
    """Read the staged callback evidence of a run, if any.

    Returns ``None`` when no callback has been received. Otherwise a plain dict
    with ``finished`` (bool) plus the provider signal. Reading this is how the
    lease-owning completion path notices a push without another remote poll --
    it never changes any state by itself.
    """
    payload = getattr(run, "callback_payload", None)
    if not isinstance(payload, dict) or not payload:
        return None
    status = payload.get("status")
    return {
        "finished": status in CALLBACK_TERMINAL_STATUSES,
        "status": status,
        "error": payload.get("error"),
        "cost": payload.get("cost"),
        "usage": payload.get("usage"),
        "remote_run_id": payload.get("remote_run_id"),
        "jti": payload.get("jti"),
    }


def _audit(
    session: Session,
    *,
    action: str,
    run_id: str,
    jti: str,
    outcome: str,
    project_id: str | None,
    task_id: str | None,
    nonce: str | None = None,
) -> None:
    """Record one callback audit entry.

    Only the ``jti`` is recorded -- never the token, and never the raw provider
    body (it is untrusted input and may carry credentials).
    """
    append_audit(
        session,
        actor="callback",
        action=action,
        resource_type="delegated_run",
        resource_id=run_id,
        project_id=project_id,
        task_id=task_id,
        before={},
        after={"outcome": outcome, "jti": jti},
        idempotency_key=(
            f"audit:callback:{run_id}:{jti or 'none'}:{outcome}"
            # Only the FIRST receipt is a deduplicated event (the CAS guarantees
            # exactly one writer). Repeat deliveries -- duplicate / conflict /
            # late / rejected -- are genuinely separate events, so they carry a
            # nonce instead of colliding on the unique idempotency_key.
            + (f":{nonce}" if nonce else "")
        ),
    )


def ingest_callback(
    session: Session,
    *,
    run_id: str,
    claims: CallbackClaims,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> CallbackIngestResult:
    """Persist one authenticated callback as evidence on its ``DelegatedRun``.

    Ordering is deliberate:

    1. unknown run            -> ``unknown_run`` (2xx, audited, no leak)
    2. same jti seen before   -> ``duplicate``   (2xx, audited, no rewrite)
    3. different evidence set -> ``conflict``    (2xx, audited, first wins)
    4. otherwise              -> evidence staged via the CAS gate:
         - run still active   -> ``received``    (nothing else changes)
         - run already terminal -> ``late``      (evidence kept, state frozen)

    The staging write is a single conditional UPDATE gated on
    ``callback_received_at IS NULL``, so concurrent deliveries race safely:
    exactly one wins, the rest are classified as duplicate/conflict. A
    ``late`` delivery therefore PERSISTS its evidence (first-wins) on the
    terminal run while changing no status, cost, usage, lease or budget:
    callback remains evidence, never authority, and the run is never reopened.
    """
    stamp = _naive_utc(now) if now is not None else _naive_utc(_now())
    jti = claims.jti
    run = session.get(DelegatedRun, run_id)

    if run is not None and (
        run.attempt != claims.attempt or run.agent_id != claims.agent_id
    ):
        # A token is bound to (run, attempt, agent). Stale-attempt or wrong-agent
        # tokens are acknowledged (never 401) so a provider cannot retry forever,
        # but they write NOTHING -- the mismatch is audited instead.
        _audit(
            session,
            action=AuditEvent.DELEGATION_CALLBACK_INVALID,
            run_id=run_id,
            jti=jti,
            outcome="binding_mismatch",
            project_id=run.project_id,
            task_id=run.task_id,
            nonce=uuid.uuid4().hex[:8],
        )
        session.commit()
        return CallbackIngestResult(outcome="binding_mismatch", run_id=run_id, jti=jti)

    if run is None:
        _audit(
            session,
            action=AuditEvent.DELEGATION_CALLBACK_RECEIVED,
            run_id=run_id,
            jti=jti,
            outcome="unknown_run",
            project_id=None,
            task_id=None,
            nonce=uuid.uuid4().hex[:8],
        )
        session.commit()
        return CallbackIngestResult(outcome="unknown_run", run_id=run_id, jti=jti)

    existing = run.callback_payload
    if isinstance(existing, dict) and existing:
        outcome = "duplicate" if existing.get("jti") == jti else "conflict"
        action = (
            AuditEvent.DELEGATION_CALLBACK_DUPLICATE
            if outcome == "duplicate"
            else AuditEvent.DELEGATION_CALLBACK_CONFLICT
        )
        _audit(
            session,
            action=action,
            run_id=run_id,
            jti=jti,
            outcome=outcome,
            project_id=run.project_id,
            task_id=run.task_id,
            nonce=uuid.uuid4().hex[:8],
        )
        session.commit()
        return CallbackIngestResult(outcome=outcome, run_id=run_id, jti=jti)

    # Evidence, not authority: the stored payload is redacted (a provider body is
    # untrusted input) and enriched with the token's jti for duplicate detection.
    evidence = dict(redact_secrets(payload))
    evidence["jti"] = jti
    evidence["attempt"] = claims.attempt
    evidence["agent_id"] = claims.agent_id
    evidence["received_at"] = stamp.isoformat()

    stmt = (
        update(DelegatedRun)
        .where(DelegatedRun.id == run_id)
        .where(DelegatedRun.callback_received_at.is_(None))
        .values(callback_received_at=stamp, callback_payload=evidence)
    )
    if session.execute(stmt).rowcount != 1:
        # Lost the race with a concurrent delivery of the same callback.
        session.refresh(run)
        existing = run.callback_payload
        outcome = (
            "duplicate"
            if isinstance(existing, dict) and existing.get("jti") == jti
            else "conflict"
        )
        action = (
            AuditEvent.DELEGATION_CALLBACK_DUPLICATE
            if outcome == "duplicate"
            else AuditEvent.DELEGATION_CALLBACK_CONFLICT
        )
        _audit(
            session,
            action=action,
            run_id=run_id,
            jti=jti,
            outcome=outcome,
            project_id=run.project_id,
            task_id=run.task_id,
            nonce=uuid.uuid4().hex[:8],
        )
        session.commit()
        return CallbackIngestResult(outcome=outcome, run_id=run_id, jti=jti)

    if run.status in TERMINAL_RUN_STATUSES:
        # GAP-4: this delivery lost the timing race against the run's own
        # completion. The evidence above is still staged (first-wins via the
        # same CAS gate), the outcome is audited as ``late``, and NOTHING else
        # moves: no status rewrite, no cost/usage apply, no accrual, no lease
        # touch. Evidence, not authority -- the run stays exactly as the
        # lease-owning completion path left it.
        _audit(
            session,
            action=AuditEvent.DELEGATION_CALLBACK_LATE,
            run_id=run_id,
            jti=jti,
            outcome="late",
            project_id=run.project_id,
            task_id=run.task_id,
            nonce=uuid.uuid4().hex[:8],
        )
        session.commit()
        return CallbackIngestResult(outcome="late", run_id=run_id, jti=jti)

    _audit(
        session,
        action=AuditEvent.DELEGATION_CALLBACK_RECEIVED,
        run_id=run_id,
        jti=jti,
        outcome="received",
        project_id=run.project_id,
        task_id=run.task_id,
    )
    session.commit()
    return CallbackIngestResult(outcome="received", run_id=run_id, jti=jti)


def _now() -> datetime:
    from aios.models import now_utc

    return now_utc()


def new_callback_jti() -> str:
    """Fresh, opaque identifier for one minted callback token."""
    return new_id("cbk")

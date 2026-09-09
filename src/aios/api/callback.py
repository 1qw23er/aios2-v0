"""Callback / Webhook Ingest P1 — the inbound HTTP surface.

One route: ``POST /runs/{run_id}/callback``.

Security posture (deliberate, and the reason the response shape is so boring):

* **Authentication first.** A run-scoped HMAC-SHA256 token in
  ``X-AIOS-Callback-Token`` decides everything. The token is a HEADER, never a
  URL query parameter, so it cannot be captured by access logs / proxies / APM.
* **No 404.** An authenticated-but-unknown run is acknowledged exactly like a
  successful one, so a caller cannot probe which run ids exist.
* **No 409.** A duplicate / late / conflicting callback is acknowledged with
  2xx and recorded in the audit trail instead, so well-behaved providers do not
  retry forever.
* **One response body for every authenticated outcome** (``{"received": true}``).
  The distinction (received / duplicate / conflict / late / unknown run) lives
  only in the audit trail.
* **Authentication failures follow the existing convention**: ``401`` with a
  constant detail (matching ``authenticate_agent`` / ``authenticate_bootstrap_token``),
  because an unauthenticated caller must not be able to name a run either.

This route NEVER terminalizes a run and NEVER accrues budget. It stages
evidence; the lease-owning execution path does the rest.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session

from aios.api.security import _AGENT_UNAUTH_HEADERS
from aios.audit import AuditEvent, append_audit
from aios.callback_ingest import (
    CALLBACK_MAX_BODY_BYTES,
    CALLBACK_TOKEN_HEADER,
    CallbackAuthError,
    claims_match,
    ingest_callback,
    normalise_callback_payload,
    verify_callback_token,
)
from aios.db import get_session

#: Constant detail for every authentication failure (no oracles).
_AUTH_FAILED_DETAIL = "invalid callback token"

#: The only body an authenticated caller ever sees. Identical for received /
#: duplicate / conflict / late / unknown-run, by design.
_ACK_BODY: dict[str, Any] = {"received": True}


def _audit_auth_failure(session: Session, error: CallbackAuthError) -> None:
    """Record a rejected callback, but only when it is attributable.

    A token that is cryptographically valid but unusable (expired, or bound to
    another run) carries claims, so we can audit it against a real run id. A
    forged or malformed token carries none -- auditing it would let an
    unauthenticated caller plant arbitrary ids in the audit trail, so it is
    silently rejected instead.
    """
    claims = error.claims
    if claims is None:
        return
    action = (
        AuditEvent.DELEGATION_CALLBACK_EXPIRED
        if error.reason == "expired"
        else AuditEvent.DELEGATION_CALLBACK_INVALID
    )
    append_audit(
        session,
        actor="callback",
        action=action,
        resource_type="delegated_run",
        resource_id=claims.run_id,
        project_id=None,
        task_id=None,
        before={},
        after={"reason": error.reason, "jti": claims.jti},
        idempotency_key=f"audit:callback:{claims.run_id}:{claims.jti}:rejected",
    )
    session.commit()


def register_callback_routes(application: Any) -> None:
    """Build and flat-attach the callback ingest route (mirrors ``execution_run``)."""
    router = APIRouter(
        tags=["execution-run"],
        dependency_overrides_provider=application,
    )

    @router.post("/runs/{run_id}/callback", response_model=dict[str, Any])
    async def receive_callback(
        run_id: str,
        request: Request,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        """Accept one authenticated provider callback as EVIDENCE (never authority).

        The token is verified before the body is even read; the body is size- and
        shape-checked; then the signal is persisted on the run. Terminalization
        and budget accrual remain the exclusive job of the lease-owning
        execution path.
        """
        token = request.headers.get(CALLBACK_TOKEN_HEADER)
        try:
            claims = verify_callback_token(token)
            if not claims_match(claims, run_id=run_id):
                raise CallbackAuthError("binding_mismatch", claims)
        except CallbackAuthError as error:
            _audit_auth_failure(session, error)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=_AUTH_FAILED_DETAIL,
                headers=_AGENT_UNAUTH_HEADERS,
            ) from None

        raw = await request.body()
        if len(raw) > CALLBACK_MAX_BODY_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="callback body too large",
            )
        try:
            body = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001 - malformed JSON is a client error
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="callback body must be a JSON object",
            ) from None
        try:
            payload = normalise_callback_payload(body)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(error),
            ) from None

        ingest_callback(
            session,
            run_id=run_id,
            claims=claims,
            payload=payload,
        )
        return dict(_ACK_BODY)

    for route in router.routes:
        application.router.routes.append(route)

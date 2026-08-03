"""Owner Operating Layer V0 (OOL V0) HTTP surface -- Issue #121 / PR #122, plan §11 Task 2.

Five owner-authenticated endpoints, all server-rendered HTML:

===============================  ======  ==================================
endpoint                         method  required sealed token
===============================  ======  ==================================
``/owner/project-picker``        GET     -- (mints ``project_select``)
``/owner/project-pick``          POST    ``select_token``  (project_select)
``/owner/inboxes/{kind}``        GET     ``?ctx=``         (project_context)
``/owner/inboxes/{kind}/detail`` POST    ``detail_token``  (detail_view)
``/owner/inboxes/{kind}/decide`` POST    ``action_token``  (inbox_action)
===============================  ======  ==================================

Hard contracts enforced *here* (plan §4.1 / §8 / T-D1 / T-P1):

* **No ``project_id`` request parameter anywhere.** The operating project only
  ever travels inside a sealed token (picker → ``project_select`` →
  ``project_context`` → ``detail_view`` → ``inbox_action``). There is **no**
  mutable ``selected_project`` session and no project cookie.
* **No raw resource id in any URL / route segment / query / referrer.** The
  detail view is a ``POST`` carrying an opaque ``detail_token``; there is no
  ``/{rid}`` route.
* **Every request carries exactly one valid, purpose-bound sealed token**,
  which is also the CSRF defense (§4.1 -- server-issued, unguessable,
  single-purpose, short TTL, only ever embedded in server-rendered forms).
* **Owner-facing error normalization (§8).** Internal ``ServiceError`` details
  are translated into business Chinese before rendering; nothing else reaches
  the owner surface. OOL-level resolution failures already arrive normalized
  (uniform 409) from :mod:`aios.owner_inbox`; a missing/invalid token key is a
  fail-closed ``503``.

This module owns **no** domain logic: it parses the request, calls
:class:`aios.owner_inbox.OwnerInboxService`, and renders.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, FastAPI, Form, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from sqlmodel import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from aios.actor import ActorContext
from aios.api.security import authenticate_owner
from aios.console import (
    owner_inbox_detail_html,
    owner_inbox_error_html,
    owner_inbox_hub_html,
    owner_inbox_page_html,
    owner_project_picker_html,
)
from aios.db import get_session
from aios.owner_inbox import (
    INBOX_KINDS,
    MSG_MISSING,
    MSG_TOKEN_INVALID,
    DecisionInput,
    OwnerInboxService,
)
from aios.services import ServiceError

__all__ = ["register_owner_inbox_routes"]


# --------------------------------------------------------------------------
# §8 owner-facing error translation
# --------------------------------------------------------------------------

# Exact service-error details that carry a *specific* owner meaning (plan §8).
# Anything not listed falls back to the per-status business sentence below, so
# an internal string can never reach the owner surface.
_SPECIFIC_MESSAGES: dict[str, str] = {
    "content changed since review; re-submit": "内容已被修改，请重新审阅最新一轮。",
    "suggestion already consumed": "这条回复已经处理，无需再次发送。",
    "stale knowledge fact": "知识已更新，请重新确认后再发送。",
    "auto-send confidence below active threshold": "该建议置信度不足，需你手动确认后发送。",
    "auto_send requires a bound suggestion_id": "该建议尚未准备好发送，请刷新收件箱。",
    "auto-send suggestion has no bound knowledge fact": "该建议尚未准备好发送，请刷新收件箱。",
    "suggestion text mismatch": "该条目已变更，请刷新收件箱。",
    "outbound delivery failed": "消息发送失败，请稍后重试（未重复发送）。",
}

_STATUS_MESSAGES: dict[int, str] = {
    403: "你无权操作该项目的此条目。",
    404: MSG_MISSING,
    409: "该条目已处理，无需重复操作。",
    422: "提交的信息不完整，请补充后重试。",
    502: "消息发送失败，请稍后重试（未重复发送）。",
    503: "系统暂时无法处理该操作，请稍后重试。",
}

_FALLBACK_MESSAGE = "系统暂时无法处理该操作，请稍后重试。"


def _is_owner_facing(detail: str) -> bool:
    """OOL / adapter messages are already business Chinese -- pass them through.

    Downstream services raise ASCII engineering strings ("Artifact not found",
    "stale knowledge fact", ...). Those must never reach the owner verbatim.
    """
    return any(ord(ch) > 0x2E7F for ch in detail)


def _business_message(error: ServiceError) -> str:
    """Translate any ``ServiceError`` into one owner-safe business sentence (§8)."""
    detail = (error.detail or "").strip()
    if detail in _SPECIFIC_MESSAGES:
        return _SPECIFIC_MESSAGES[detail]
    if _is_owner_facing(detail):
        return detail
    return _STATUS_MESSAGES.get(error.status_code, _FALLBACK_MESSAGE)


def _failure(error: ServiceError) -> HTMLResponse:
    """Render the uniform §8 failure page, preserving the normalized status.

    ``ServiceError.untrusted`` short-circuits translation entirely: a failure
    whose cause was decided by client-supplied input must never be described,
    only refused, and always with the *same bytes* (§4.1 rule 6 / §8). Routing
    it here -- rather than trusting each raise site to pick the right sentence
    -- keeps the guarantee at the single place that writes the response, so a
    future raise site cannot re-open the enumeration oracle by choosing a more
    "helpful" message.
    """
    if error.untrusted:
        return _untrusted_input()
    return HTMLResponse(
        owner_inbox_error_html(_business_message(error)),
        status_code=error.status_code,
    )


def _untrusted_input() -> HTMLResponse:
    """The single owner-facing response for *any* untrustworthy navigation input.

    Absent, malformed, expired, wrong ``token_type``, wrong purpose, wrong
    endpoint, unknown inbox kind -- all of them collapse to one byte-identical
    409 HTML page. The owner is told the page went stale and is given one safe
    way back; nothing distinguishes the causes, so the surface cannot be probed
    to learn which tokens or inbox kinds exist (§4.1 rule 6 / §8).
    """
    return HTMLResponse(owner_inbox_error_html(MSG_TOKEN_INVALID), status_code=409)


# --------------------------------------------------------------------------
# §8 request-validation boundary (OOL surface only)
# --------------------------------------------------------------------------


class _OwnerSurfaceRoute(APIRoute):
    """Keep FastAPI's machine-readable 422 off the owner surface.

    Validation itself is untouched -- every token parameter below stays
    **required**, the request still fails closed and the handler is never
    entered. Only the *response* is normalized: FastAPI would otherwise answer a
    missing form field with
    ``{"detail":[{"type":"missing","loc":["body","select_token"],...}]}``, which

    * hands a non-technical owner a validation dump instead of a business page,
    * leaks the internal parameter names and the request schema, and
    * makes "parameter missing" observably different from "token invalid",
      re-opening exactly the enumeration channel §4.1 rule 6 closes.

    **Scope.** This class is attached to the OOL router and nowhere else, so no
    other route in the application changes behaviour: a non-OOL validation
    failure still returns FastAPI's ordinary 422 JSON. This is presentation /
    error-class normalization on one surface, not a global redesign -- which is
    also why it is a route class rather than an application-wide exception
    handler.

    Two exception shapes reach here, and only these two are normalized:

    ``RequestValidationError``
        a missing or unparseable request parameter.
    ``HTTPException(400)``
        an unreadable request body -- either FastAPI's "there was an error
        parsing the body" or Starlette's "Missing boundary in multipart." OOL
        handlers never raise 400 themselves -- domain failures travel as
        :class:`~aios.services.ServiceError` and are rendered inside the
        handler -- so a 400 here is always a malformed request.

        The ``except`` clause binds **Starlette's** ``HTTPException`` on
        purpose. ``fastapi.HTTPException`` is a *subclass* of it, so catching
        the FastAPI one would silently miss the body-parser failure, which
        Starlette raises as the base class from ``Request.form()``. Catching the
        base covers both.

    Everything else propagates untouched. In particular ``authenticate_owner``'s
    401 (unauthenticated) and 503 (owner auth unconfigured) must stay visible:
    laundering them into a business page would hide the owner-surface auth guard.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original_handler = super().get_route_handler()

        async def normalized_handler(request: Request) -> Response:
            try:
                return await original_handler(request)
            except RequestValidationError:
                return _untrusted_input()
            except StarletteHTTPException as exc:
                if exc.status_code == 400:
                    return _untrusted_input()
                raise

        return normalized_handler


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def register_owner_inbox_routes(application: FastAPI) -> None:
    """Attach the five OOL V0 endpoints to ``application`` (called by ``create_app``).

    The endpoints live on their own :class:`~fastapi.APIRouter` so that
    :class:`_OwnerSurfaceRoute` -- and therefore the §8 normalization of
    FastAPI's request validation -- applies to these five routes and to
    nothing else in the application.
    """
    router = APIRouter(route_class=_OwnerSurfaceRoute)

    @router.get("/owner/project-picker", response_class=HTMLResponse)
    def owner_project_picker(
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Business labels only -- each option carries a sealed ``project_select`` token."""
        service = OwnerInboxService(session)
        try:
            options = service.list_project_options(actor)
        except ServiceError as error:
            return _failure(error)
        return HTMLResponse(owner_project_picker_html(options))

    @router.post("/owner/project-pick", response_class=HTMLResponse, response_model=None)
    def owner_project_pick(
        select_token: str = Form(...),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """§2.1.2a resolver: load the live Project row, then mint a ``project_context``.

        Stateless and *not* one-time (T-PS4): replaying the same
        ``project_select`` token only mints an equivalent fresh context token
        and writes nothing.
        """
        service = OwnerInboxService(session)
        try:
            project, context_token = service.resolve_project_select(actor, select_token)
        except ServiceError as error:
            return _failure(error)
        return HTMLResponse(owner_inbox_hub_html(project.name, context_token))

    @router.get("/owner/inboxes/{kind}", response_class=HTMLResponse)
    def owner_inbox_list(
        kind: str,
        ctx: str = Query(...),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """List one inbox for the project frozen inside ``ctx`` (a ``project_context``)."""
        if kind not in INBOX_KINDS:
            return _untrusted_input()
        service = OwnerInboxService(session)
        try:
            page = service.list_inbox(actor, ctx, kind)
        except ServiceError as error:
            return _failure(error)
        return HTMLResponse(owner_inbox_page_html(page))

    @router.post(
        "/owner/inboxes/{kind}/detail", response_class=HTMLResponse, response_model=None
    )
    def owner_inbox_detail(
        kind: str,
        detail_token: str = Form(...),
        ctx: str = Form(...),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Detail view -- the resource reference stays encrypted inside ``detail_token``.

        There is deliberately no ``GET /owner/inboxes/{kind}/{rid}`` route
        (P1-2 / T-D1): a raw id must never appear in a URL, route segment,
        referrer or access log.
        """
        if kind not in INBOX_KINDS:
            return _untrusted_input()
        service = OwnerInboxService(session)
        try:
            detail = service.resolve_detail_view(actor, detail_token, kind, ctx)
        except ServiceError as error:
            return _failure(error)
        return HTMLResponse(owner_inbox_detail_html(detail))

    @router.post(
        "/owner/inboxes/{kind}/decide", response_class=HTMLResponse, response_model=None
    )
    def owner_inbox_decide(
        kind: str,
        action_token: str = Form(...),
        ctx: str = Form(...),
        reason: str | None = Form(None),
        body: str | None = Form(None),
        text: str | None = Form(None),
        title: str | None = Form(None),
        stage_label: str | None = Form(None),
        canonical_choice: str | None = Form(None),
        series_choice: str | None = Form(None),
        tag_labels: list[str] = Form(default_factory=list),
        session: Session = Depends(get_session),
        actor: ActorContext = Depends(authenticate_owner),
    ) -> HTMLResponse:
        """Apply one owner decision, then re-render the inbox with the outcome.

        The owner only ever submits **business** values (a reason, a rewritten
        reply, a stage label, canonical tag labels...). Every identity the
        underlying service needs -- artifact id, conversation id, suggestion id,
        series, version, checksum, revision -- is unsealed from
        ``action_token`` server-side and is never accepted from the client.
        """
        if kind not in INBOX_KINDS:
            return _untrusted_input()
        service = OwnerInboxService(session)
        payload = DecisionInput(
            reason=reason,
            body=body,
            text=text,
            title=title,
            stage_label=stage_label,
            tag_labels=list(tag_labels),
            canonical_choice=canonical_choice,
            series_choice=series_choice,
        )
        try:
            result = service.decide(
                actor,
                action_token=action_token,
                inbox=kind,
                payload=payload,
                context_token=ctx,
            )
        except ServiceError as error:
            return _failure(error)
        # Re-render (not redirect): the outcome sentence must survive, and the
        # context token is far too long to be safe/pretty in a Location header.
        # A resubmitted POST is harmless -- the action token is already spent at
        # the service layer and collapses to the uniform "已处理" message.
        try:
            page = service.list_inbox(actor, result.context_token, kind)
        except ServiceError as error:
            return _failure(error)
        return HTMLResponse(owner_inbox_page_html(page, message=result.message))

    # Attached flat -- deliberately NOT via ``application.include_router``.
    #
    # Every other route in this application is registered directly on the app,
    # and FastAPI keeps an ``include_router`` call as a *nested* router node in
    # ``app.routes``. That nesting is invisible at request time, but it hides
    # these five endpoints from anything that walks ``app.routes`` -- including
    # the owner-auth route inventory (tests/test_owner_auth.py), whose entire
    # job is to prove that no ``/owner`` route ever ships without
    # ``authenticate_owner``. A security inventory that silently stops seeing a
    # surface is worse than no inventory at all, so the router above exists
    # only to carry ``route_class``; its finished routes are handed to the
    # application one by one, exactly like their neighbours.
    for route in router.routes:
        if not isinstance(route, _OwnerSurfaceRoute):  # pragma: no cover - defensive
            raise RuntimeError(
                f"owner-surface route {route!r} lost its normalization route class"
            )
        application.router.routes.append(route)

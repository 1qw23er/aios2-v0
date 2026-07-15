from sqlmodel import Session

from aios.audit import append_audit
from aios.models import TaskContext
from aios.services import ServiceError


def delete_context_for_retention(
    session: Session,
    context_id: str,
    *,
    actor: str,
    rationale: str,
) -> None:
    if not actor.strip() or not rationale.strip():
        raise ServiceError(422, "Administrative actor and rationale are required")
    context = session.get(TaskContext, context_id)
    if context is None:
        raise ServiceError(404, "TaskContext not found")
    try:
        append_audit(
            session,
            actor=actor,
            action="context.deleted",
            resource_type="task_context",
            resource_id=context.id,
            project_id=context.project_id,
            task_id=context.task_id,
            before={},
            after={
                "context_id": context.id,
                "context_hash": context.context_hash,
                "rationale": rationale,
            },
            idempotency_key=f"audit:context:{context.id}:deleted",
        )
        session.delete(context)
        session.commit()
    except Exception:
        session.rollback()
        raise

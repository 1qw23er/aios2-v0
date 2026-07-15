import json
from pathlib import Path

import pytest
from sqlmodel import Session, select

from aios.adapters.external import ExternalWorkstationAdapter, TaskPacket
from aios.audit import AuditLog
from aios.db import get_engine, run_migrations
from aios.external_service import import_external_result
from aios.models import (
    AdapterType,
    Agent,
    AgentCapability,
    AgentStatus,
    Capability,
    Event,
    ExecutionAssignment,
    Project,
    RoutingMode,
    Task,
    TaskStatus,
)
from aios.orchestrator import Orchestrator
from aios.scheduler import DeterministicScheduler, route_task


def database(tmp_path: Path, name: str) -> str:
    url = f"sqlite:///{(tmp_path / name).as_posix()}"
    run_migrations(url)
    return url


def add_agent(
    session: Session,
    *,
    agent_id: str,
    status: AgentStatus = AgentStatus.AVAILABLE,
    adapter_type: AdapterType = AdapterType.API,
) -> Agent:
    agent = Agent(
        id=agent_id,
        name=agent_id,
        role="worker",
        adapter_type=adapter_type,
        status=status,
    )
    session.add(agent)
    return agent


def add_profile(
    session: Session,
    agent: Agent,
    capability: Capability,
    priority: int,
) -> None:
    session.add(
        AgentCapability(
            agent_id=agent.id,
            capability_id=capability.id,
            priority=priority,
        )
    )


def test_fixed_route_is_backward_compatible(tmp_path: Path) -> None:
    url = database(tmp_path, "fixed.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        agent = add_agent(session, agent_id="agt_fixed")
        session.add(project)
        session.flush()
        task = Task(
            project_id=project.id,
            title="Legacy",
            description="Legacy fixed task",
            status=TaskStatus.READY,
            assigned_agent_id=agent.id,
        )
        session.add(task)
        session.commit()

        assignment = DeterministicScheduler(session).route_task(task.id, "route-fixed")

        assert assignment is not None
        assert assignment.selected_agent_id == agent.id
        assert assignment.fallback_used is False


@pytest.mark.parametrize("status", [AgentStatus.UNAVAILABLE, AgentStatus.MAINTENANCE])
def test_preferred_agent_unavailable_uses_fallback(tmp_path: Path, status: AgentStatus) -> None:
    url = database(tmp_path, f"preferred-{status.value}.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        capability = Capability(id="cap_write", name="writing")
        preferred = add_agent(session, agent_id="agt_preferred", status=status)
        fallback = add_agent(session, agent_id="agt_fallback")
        session.add_all([project, capability])
        session.flush()
        add_profile(session, preferred, capability, 100)
        add_profile(session, fallback, capability, 70)
        task = Task(
            project_id=project.id,
            title="Draft",
            description="Draft",
            status=TaskStatus.READY,
            preferred_agent_id=preferred.id,
            required_capabilities=[capability.id],
            routing_mode=RoutingMode.PREFERRED_WITH_FALLBACK,
        )
        session.add(task)
        session.commit()

        assignment = route_task(session, task.id, f"route-{status.value}")

        assert assignment is not None
        assert assignment.selected_agent_id == fallback.id
        assert assignment.fallback_used is True
        audit = session.exec(
            select(AuditLog).where(AuditLog.idempotency_key == f"audit:route-{status.value}")
        ).one()
        considered = audit.after_snapshot["considered_candidates"]
        rejected = next(item for item in considered if item["agent_id"] == preferred.id)
        assert rejected["eligible"] is False
        assert status.value in rejected["reasons"]
        assert audit.after_snapshot["routing_reason"] == assignment.routing_reason


def test_best_available_is_deterministic_and_retry_is_idempotent(tmp_path: Path) -> None:
    url = database(tmp_path, "best.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        writing = Capability(id="cap_write", name="writing")
        chinese = Capability(id="cap_zh", name="chinese")
        first = add_agent(session, agent_id="agt_a")
        second = add_agent(session, agent_id="agt_b")
        session.add_all([project, writing, chinese])
        session.flush()
        add_profile(session, first, writing, 90)
        add_profile(session, first, chinese, 60)
        add_profile(session, second, writing, 70)
        add_profile(session, second, chinese, 70)
        task = Task(
            project_id=project.id,
            title="Draft",
            description="Draft",
            status=TaskStatus.READY,
            required_capabilities=[writing.id, chinese.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        session.add(task)
        session.commit()

        assignment = route_task(session, task.id, "route-best")
        replay = route_task(session, task.id, "route-best")

        assert assignment is not None and replay is not None
        assert assignment.id == replay.id
        assert assignment.selected_agent_id == second.id
        assert len(list(session.exec(select(ExecutionAssignment)))) == 1
        assert len(list(session.exec(select(Event).where(Event.type == "task.assigned")))) == 1
        assert (
            len(list(session.exec(select(AuditLog).where(AuditLog.action == "routing.selected"))))
            == 1
        )


def test_manual_and_unavailable_fixed_routes_are_audited_without_assignment(
    tmp_path: Path,
) -> None:
    url = database(tmp_path, "blocked.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        unavailable = add_agent(session, agent_id="agt_down", status=AgentStatus.UNAVAILABLE)
        session.add(project)
        session.flush()
        fixed = Task(
            project_id=project.id,
            title="Fixed",
            description="Fixed",
            status=TaskStatus.READY,
            assigned_agent_id=unavailable.id,
        )
        manual = Task(
            project_id=project.id,
            title="Manual",
            description="Manual",
            status=TaskStatus.READY,
            routing_mode=RoutingMode.MANUAL,
        )
        session.add_all([fixed, manual])
        session.commit()

        assert route_task(session, fixed.id, "route-fixed-down") is None
        assert route_task(session, manual.id, "route-manual") is None
        assert route_task(session, manual.id, "route-manual") is None
        assert len(list(session.exec(select(ExecutionAssignment)))) == 0
        blocked = list(session.exec(select(AuditLog).where(AuditLog.action == "routing.blocked")))
        assert len(blocked) == 2


def test_fallback_completes_workflow_and_external_packet_is_unchanged(
    tmp_path: Path,
) -> None:
    url = database(tmp_path, "workflow.db")
    adapter = ExternalWorkstationAdapter(tmp_path / "outbox", tmp_path / "inbox")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        capability = Capability(id="cap_research", name="research")
        preferred = add_agent(session, agent_id="agt_closed", status=AgentStatus.UNAVAILABLE)
        external = add_agent(session, agent_id="agt_external", adapter_type=AdapterType.EXTERNAL)
        session.add_all([project, capability])
        session.flush()
        add_profile(session, preferred, capability, 100)
        add_profile(session, external, capability, 80)
        task = Task(
            project_id=project.id,
            title="Research",
            description="Research",
            status=TaskStatus.READY,
            preferred_agent_id=preferred.id,
            required_capabilities=[capability.id],
            routing_mode=RoutingMode.PREFERRED_WITH_FALLBACK,
        )
        session.add(task)
        session.flush()
        next_task = Task(
            project_id=project.id,
            title="Plan",
            description="Plan",
            depends_on=[task.id],
        )
        session.add(next_task)
        session.commit()

        assignment = route_task(session, task.id, "route-external")
        assert assignment is not None and assignment.selected_agent_id == external.id
        exported = adapter.export_task(
            TaskPacket(
                task_id=task.id,
                project={"id": project.id},
                role="researcher",
                instructions=task.description,
                output_schema={
                    "type": "object",
                    "required": ["result_id", "task_id", "summary", "claims", "artifacts"],
                },
            ),
            "legacy context",
        )
        assert exported.packet_path.is_file()
        result_path = adapter.inbox / "external-fallback.json"
        result_path.write_text(
            json.dumps(
                {
                    "result_id": "external-fallback",
                    "task_id": task.id,
                    "summary": "Completed research",
                    "claims": [],
                    "artifacts": [],
                }
            ),
            encoding="utf-8",
        )
        artifact = import_external_result(session, adapter, result_path)
        assert artifact.task_id == task.id
        Orchestrator(session).process_pending()
        assert session.get(Task, next_task.id).status == TaskStatus.READY


def test_assignment_transaction_rolls_back_when_audit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = database(tmp_path, "rollback.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        capability = Capability(id="cap_write", name="writing")
        agent = add_agent(session, agent_id="agt_writer")
        session.add_all([project, capability])
        session.flush()
        add_profile(session, agent, capability, 90)
        task = Task(
            project_id=project.id,
            title="Draft",
            description="Draft",
            status=TaskStatus.READY,
            required_capabilities=[capability.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        session.add(task)
        session.commit()

        def fail_audit(*args: object, **kwargs: object) -> None:
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr("aios.scheduler.append_audit", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            route_task(session, task.id, "route-rollback")

        assert len(list(session.exec(select(ExecutionAssignment)))) == 0
        assert len(list(session.exec(select(Event).where(Event.type == "task.assigned")))) == 0
        assert session.get(Task, task.id).assigned_agent_id is None


def test_best_available_tie_breaks_by_agent_id(tmp_path: Path) -> None:
    url = database(tmp_path, "tie.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        capability = Capability(id="cap_write", name="writing")
        later = add_agent(session, agent_id="agt_b")
        earlier = add_agent(session, agent_id="agt_a")
        session.add_all([project, capability])
        session.flush()
        add_profile(session, later, capability, 80)
        add_profile(session, earlier, capability, 80)
        task = Task(
            project_id=project.id,
            title="Draft",
            description="Draft",
            status=TaskStatus.READY,
            required_capabilities=[capability.id],
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        session.add(task)
        session.commit()

        assignment = route_task(session, task.id, "route-tie")

        assert assignment is not None
        assert assignment.selected_agent_id == earlier.id


def test_best_available_without_requirements_is_blocked_once(tmp_path: Path) -> None:
    url = database(tmp_path, "under-specified.db")
    with Session(get_engine(url)) as session:
        project = Project(name="P", objective="O")
        session.add(project)
        session.flush()
        task = Task(
            project_id=project.id,
            title="Draft",
            description="Draft",
            status=TaskStatus.READY,
            routing_mode=RoutingMode.BEST_AVAILABLE,
        )
        session.add(task)
        session.commit()

        assert route_task(session, task.id, "route-under-specified") is None
        assert route_task(session, task.id, "route-under-specified") is None
        audit = session.exec(
            select(AuditLog).where(AuditLog.idempotency_key == "audit:route-under-specified")
        ).one()
        assert audit.after_snapshot["routing_reason"] == "required_capabilities_missing"
        assert len(list(session.exec(select(ExecutionAssignment)))) == 0

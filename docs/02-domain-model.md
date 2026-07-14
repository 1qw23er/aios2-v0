# 领域模型

## Project
- id
- name
- objective
- status: proposed|active|blocked|completed|cancelled
- owner
- budget_limit
- success_metrics
- created_at / updated_at

## Task
- id
- project_id
- title
- description
- status: backlog|ready|running|waiting_external|review|approved|rejected|done|failed
- assigned_agent_id
- adapter_type: api|cli|external
- input_context_refs[]
- acceptance_criteria[]
- depends_on[]
- estimated_cost
- actual_cost
- retry_count
- created_at / updated_at

## Event
- id
- project_id
- task_id
- type
- payload
- idempotency_key
- created_at

## ContextItem
- id
- project_id (nullable for company-level)
- namespace
- key
- value
- source_artifact_id
- confidence
- approved_by
- version

## Artifact
- id
- project_id
- task_id
- type: markdown|json|image|video|dataset|link
- uri
- checksum
- metadata
- created_at

## Agent
- id
- name
- role
- adapter_type
- capabilities[]
- permissions[]
- cost_policy
- endpoint/config_ref
- enabled

## Approval
- id
- project_id
- task_id
- action_type
- risk_level
- status: pending|approved|rejected|expired
- requested_at / decided_at
- rationale

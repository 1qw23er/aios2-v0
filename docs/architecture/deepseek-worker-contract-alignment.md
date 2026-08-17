# DeepSeek Harness Worker Contract V1 Alignment

Real Harness verification accepted the SDK/JSON-RPC stdio transport at the fixed revision. V1 supports discover, submit, status, events, usage, runtime session/reference, and result as adapter-side aggregation of terminal Harness events. Harness exposes neither a cancellation RPC nor a resume RPC at this boundary, so capability negotiation reports `cancellation=false` and `checkpoint_resume=false` while retaining both operations in the vendor-neutral interface.

The approved Option B layering is:

```text
DeepSeekHarnessWorkerClient
  -> WorkerDelegatedAdapter
  -> existing DelegatedExecutionAdapter
  -> execute_task / Artifact / audit / orchestration
```

`WorkerDelegatedAdapter` implements the existing delegated hooks through composition. It does not own or duplicate the AIOS lifecycle. One AIOS `DelegatedRun` attempt creates at most one new Harness `session/prompt`; legitimate Harness model, tool, or context-overflow recovery remains inside that worker execution and its aggregate usage belongs to the same run.

Permission enforcement is conditional on a deterministic fixed-runner proof. The configured command's final positional argument must normalize to the exact hash-pinned Cordis manifest, and the child process receives the same absolute path through `DSH_CORDIS_CONFIG`, which the runner gives precedence over argv. Enabled top-level rows in that manifest itself must mount `@deepseek-ai/dsh-fs-sandbox` and `@deepseek-ai/dsh-sandbox-policy`; a parallel plugin list is diagnostic only and cannot admit execution. Runtime attestation is additional proof when available, not an assumed API. A command/path/hash/plugin mismatch or contradictory attestation fails before provider credential resolution, process launch, `initialize`, or `session/prompt`. The feature is explicitly configured and disabled by default. No domain model, migration, scheduler, orchestrator, retry system, secret store, worker registry, or Artifact path is added.

# DeepSeek Harness Worker Adapter V1

AIOS integrates the fixed DeepSeek Harness runtime as an optional worker through Option B. The provider client owns JSON-RPC transport and terminal-event aggregation. A vendor-neutral bridge supplies the existing `DelegatedAdapter` operations to `DelegatedExecutionAdapter`, which continues to own every durable AIOS lifecycle decision.

V1 explicitly rejects cancellation and checkpoint resume as unsupported capabilities. It does not translate process termination into cancellation or claim a restart as resume. One durable AIOS attempt permits one new Harness prompt submission; runtime-internal recovery does not create another submission or outer retry loop.

Harness filesystem access is admitted only when the fixed JSON-RPC runner command names the exact hash-pinned Cordis manifest as its final positional argument. The child environment pins the runner's higher-precedence `DSH_CORDIS_CONFIG` to the same absolute path. Enabled top-level manifest rows must mount `@deepseek-ai/dsh-fs-sandbox` and `@deepseek-ai/dsh-sandbox-policy`; a parallel plugin declaration is not admission evidence. Startup identity is validated and runtime plugin attestation is checked when available. Command/path/hash/plugin proof precedes credential resolution and process launch. This rejects default or alternate filesystem compositions.

The adapter is feature-flagged off by default. Existing Agent, `DelegatedRun`, Artifact, audit, budget, retry, secret reference, `TaskContext`, Scheduler, and Orchestrator semantics remain authoritative and unchanged.

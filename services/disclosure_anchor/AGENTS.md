# disclosure_anchor service contract

This service is the L1 disclosure/PDF path. It inherits the repository contract and adds only this service's
ownership and safety boundaries.

## Authority and scope

- Normative semantics: `docs/architecture/service-purpose.md`, subordinate to the repository protocol and L1
  plan. Current implementation, schemas, generated contracts, and observations are descriptive evidence.
- The service registers, acquires, archives, parses, units, publishes, and exposes disclosure assets. It does
  not implement L2 claim/evidence/forecast semantics.
- Detailed implementation navigation lives in `docs/implementation/README.md`; current worker/DB/AgentSSD facts
  live in the runbook or gitignored HANDOFF/RUNTIME records, never here.

## Hard boundaries

1. Write only `disclosure_core` and `disclosure_ops`. Consumers read the applicable versioned
   `disclosure_public` view (`document_units_v2` for new DB Unit consumers; `document_units_v1` only for
   deprecated compatibility), the current v1 Filing API, the change feed, or explicit source references;
   no cross-service private-table dependency.
2. Applied migrations are append-only. Public-view columns, exported contracts, error shapes, CLI/API behavior,
   and migration semantics change together with their tests and canonical checklist.
3. Runtime files live under `/Volumes/AgentSSD/agent_system/services/disclosure_anchor/`. Paths come from settings
   and path builders; absolute paths, raw parser internals, secrets, and private table shapes do not enter public
   contracts.
4. Preserve source access, provider/parser identity, content hashes, immutable raw artifacts, document/run/asset
   lineage, and stable public identifiers. Never manufacture provenance or synthetic defaults.
5. Keep domain free of IO/framework dependencies; application owns ports/use cases/UoW; adapters implement
   external mechanisms; API exposes only public/admin contracts. Nearer `AGENTS.md` files define local boundaries.
6. Use `unittest`. `make test` is the no-DB suite; DB/migration behavior uses `make test-integration`, whose
   scratch runner must fail closed rather than fall back to production DB or runtime roots.
7. Provider, parser, MinerU, worker, launchd, PG, and AgentSSD operations require the applicable workflow and
   explicit runtime scope. Do not infer permission from a code/doc edit request.

## Conditional work and validation

- Follow root `docs/agent-workflow.md` for durable state, write gates, worktrees, recovery, and shared-resource
  claims. Service tasks use the existing service HANDOFF only when that workflow triggers.
- Follow root `docs/agent-research-workflow.md` when behavior depends on a provider/parser/document mechanism or
  representative failures are not explained by local contracts and corpus evidence. Select comparison systems
  for the actual mechanism; do not impose a fixed vendor-count ritual.
- Before DB-backed parsing, publication, or retrieval behavior changes, inspect the real versioned public-view
  schema and representative active distributions with read-only SQL when available; compare them with a
  source-identity replay before encoding an invariant. Neither old published rows nor fixtures are the sole oracle.
- Fixture/corpus policy: `docs/implementation/checks/fixture-and-test-policy.md`. Public persistence/API contract:
  `docs/implementation/checks/contract-checklist.md`. Operations: `docs/implementation/runbooks/production-operations.md`.
- Default gate: `make agent-check`. Add `make test-integration`, source-identity replay, provider smoke, doctor,
  or visual review only when the changed boundary and required environment are in scope. Record exact blockers.

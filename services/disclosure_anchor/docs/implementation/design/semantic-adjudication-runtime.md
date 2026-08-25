# Semantic adjudication runtime

This is the current operational contract for the optional closed-vocabulary model stage in
`BuildUnits`. It is deliberately provider-neutral at the application boundary; subscription CLI
adapters are the current mechanisms, not permanent architecture.

## Composition and configuration

The default ordered chain is:

1. `luna-primary`: OpenAI Codex CLI, canonical model `gpt-5.6-luna`, profile `low`.
2. `sonnet-backup`: Claude Code CLI, canonical model `claude-sonnet-5`, profile `low`.

Override the complete chain with one secret-free JSON value. Order is execution order and provider
IDs must be unique:

```json
[
  {
    "id": "luna-primary",
    "kind": "codex_cli",
    "provider": "openai",
    "executable": "/absolute/path/to/codex",
    "canonical_model": "gpt-5.6-luna",
    "profile": "low",
    "timeout_seconds": 600,
    "max_concurrency": 1
  },
  {
    "id": "sonnet-backup",
    "kind": "claude_cli",
    "provider": "anthropic",
    "executable": "/absolute/path/to/claude",
    "canonical_model": "claude-sonnet-5",
    "profile": "low",
    "timeout_seconds": 600,
    "max_concurrency": 1
  }
]
```

Set that array as `DISCLOSURE_SEMANTIC_PROVIDERS_JSON`. The only policy currently accepted by
`DISCLOSURE_SEMANTIC_FAILOVER_POLICY` is `availability_only.v1`. Invalid JSON, an empty chain,
duplicate IDs, provider/adapter mismatch, or a non-canonical Sonnet alias fails configuration at
startup. A later API adapter can implement the same application port without changing routing,
receipt, cache, or processing-run semantics.

Both CLI adapters use an allowlisted subprocess environment and disable tools, MCP/apps, browser,
workspace mutation, session persistence, and interactive approval. The Claude adapter also verifies
the runtime-attested canonical model; `sonnet` is not accepted as stored identity.

## Failover matrix

Only these reason codes may advance to the next provider:

- `capacity_unavailable`
- `executable_unavailable`
- `not_authenticated`
- `runtime_io_failed`
- `timeout`
- `transport_unavailable`

Cancellation propagates immediately and does not consume a build retry. Unknown non-zero exits,
invalid/missing structured output, schema or model identity drift, forbidden capability attempts,
invalid decisions, cache identity conflicts, and every other protocol/security failure fail closed;
they never try a backup. If every configured provider ends in the availability allowlist, the base
Unit set is preserved with no invented route and the run ends as `degraded_unavailable`.
Adapters assign an availability reason only from typed subprocess failures, a closed structured
error field, or a provider-owned error event / stderr line matching a versioned complete diagnostic.
Every nonblank textual diagnostic atom from every inspected output channel must be recognized and
agree with the same provider-specific availability family; no channel is discarded. Structured
error events and envelopes use versioned closed key/type shapes. Typed structured status never lets
unknown, conflicting, schema, protocol, or security sibling evidence become availability.
Unrecognized non-zero output is `command_failed` or a more specific fail-closed reason; free-form
stdout and bare diagnostic substrings are never availability evidence.

## Cache and receipt

`semantic_route_cache.v2` is group-level and provider-specific. Its key binds the full provider
identity, model/profile, prompt and output-schema hashes, taxonomy/router versions, and exact group
hash. A process-local single-flight lock prevents duplicate calls for the same key. Malformed normal
cache bytes are quarantined and recomputed; symlinks, identity/hash conflicts, and nondeterministic
existing entries fail closed.

`semantic_route_receipt.v2` is the durable source of truth. Every affected Unit receipt copies the
ordered attempts, actual result attempt/identity/hash, policy, and group hash. An all-provider outage
has an empty `selected_keys` array and no synthetic `document_content` route. A cache-write failure may
return the validated model result only because the exact result and cache failure are frozen in this
receipt before DB success; receipt/artifact/DB failure still fails the whole build closed. Publish
replays the exact receipt and never invokes a model. Replay derives each historical v2 group from the
ordered receipt members that carry the same group hash, then recomputes that hash from their fresh
input hashes and requires identical attempt/result lineage on every member. It never re-chunks a v2
receipt with the current semantic batch size. Group coverage, ordering, and contiguity must be exact;
tampering fails closed. Legacy v1 receipts remain read-only compatible with their historical replay
path.

## Durable terminal state and remediation

`processing_run.semantic_adjudication_status` is one of:

- `not_required`
- `complete_primary`
- `complete_backup`
- `degraded_unavailable`
- `failed_closed`

The run also stores degraded-Unit and failover-group counts, a closed summary, and the explicit v2
receipt path/version/hash. `disclosure_ops.unit_build_terminal_v1`, `/health`, and doctor expose
unresolved build failures and active degradation. The worker emits a transition-deduplicated alert
when a build crosses the retry ceiling. Repair is an explicit `rebuild-units` generation after the
cause is fixed; there is no automatic tight loop and no mutation of an immutable historical run.

`DATABASE_URL` is the application writer DSN and must resolve to non-superuser `disclosure_app` for
runtime/doctor acceptance. `DISCLOSURE_MIGRATION_DATABASE_URL` is migration-only and is never a worker
or pipeline fallback.

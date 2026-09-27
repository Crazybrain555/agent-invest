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
    "max_concurrency": 1,
    "model_catalog_sha256": "sha256:<prepared catalog digest>"
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

Both CLI adapters use an allowlisted subprocess environment and disable MCP/apps, browser,
workspace mutation, session persistence, and interactive approval. Claude removes its tools with
`--tools ""` and verifies the runtime-attested canonical model; `sonnet` is not accepted as stored identity.
Claude's `--safe-mode` is essential: it excludes project/user instructions, auto-memory and hooks even
when the worker inherits the repository working directory. Offline 2.1.280 request/control captures
confirmed this; `StructuredOutput` is the sole schema-return mechanism left in the request. Pin the
versioned Claude executable in the provider configuration and requalify its envelope on upgrades. Keep
the qualified binary under service-owned runtime tools; the global updater may prune old versions.

Codex uses a prepared, content-addressed `model_catalog_json` containing exactly the configured model.
`scripts/prepare_codex_semantic_model_catalog.py` obtains that entry from the pinned CLI's
`debug models --bundled`, preserving every field except five tool-surface fields: `tool_mode=direct`,
`shell_type=disabled`, `apply_patch_tool_type=null`, `experimental_supported_tools=[]`, and
`supports_search_tool=false`. Explicit flags also disable goals, sleep and user-input tools. This is
necessary because Codex 0.156.1 gives model metadata precedence over disabled code-mode feature flags;
schema-constrained output and a read-only sandbox alone do not remove tools.

The generated file lives at `semantic/codex_model_catalogs/<sha256hex>.json` under the configured runtime
root, never in Git. Replace the digest placeholder above with the preparation command's exact output.
Every Codex provider requires `model_catalog_sha256`; Claude providers forbid it. For the default chain,
set `DISCLOSURE_SEMANTIC_CODEX_MODEL_CATALOG_SHA256`. Missing/invalid artifacts fail configuration before
admission. The loader checks the hash, model, shape and neutral tool fields, then each invocation receives
a private copy of those verified bytes. The full catalog hash is part of adapter identity and therefore
cache and receipt identity. Constructing identity never runs the CLI: a missing executable at call time
still produces the normal availability failure. Regenerate, inspect and qualify the catalog whenever the
pinned CLI or model changes; verify an empty request tool list and real structured output before promotion.
The original model instructions, business prompt, taxonomy and schema are preserved.

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

The executor classifies the closed reason before any stage-guard checkpoint: a failed-closed reason is
raised at once as a typed error with a `failed_closed` attempt, so a concurrent revocation cannot turn it
into lease loss; cancellation and availability still check the live guard first, so nothing falls back or
accepts a late answer after revocation. A failed group is never cached. The router passes its group
validation (exact Unit coverage, canonical routes and the per-Unit decision rules) to the executor as
the `validate` hook, which runs after a provider call before the cache write and on every cache hit
before a hit is claimed. A fresh answer that fails is a `failed_closed` attempt of that call (its
`provider_call_ended` note says so) and never tries the backup: `invalid_contract` when the decisions do
not name exactly the requested Units once each, otherwise `invalid_decision`. A stored entry that fails
is the same attempt shape with that entry's cache key, but no provider call, no `cache_hit` attempt,
a `cache_hit_invalid` note, and the entry left untouched: no quarantine or recompute. An unexpected
error from the hook itself propagates unchanged. In the staged V4 commit a
failed-closed error is therefore a public fault: the worker latches it as the first cause and stops
persistently until an explicit release, instead of re-driving the same uncached group
(`worker-operational-stop.md`). A cancellation stays retry-neutral and is not a fault. The legacy
`BuildUnits` path keeps its per-run build retry budget.
Adapters assign an availability reason only from typed subprocess failures, a closed structured
error field, or a provider-owned error event / stderr line matching a versioned complete diagnostic.
Every nonblank textual diagnostic atom from every inspected output channel must be recognized and
agree with the same provider-specific availability family; no channel is discarded, except the closed
benign-notice set named below. Structured
error events and envelopes use versioned closed key/type shapes. Typed structured status never lets
unknown, conflicting, schema, protocol, or security sibling evidence become availability.
Unrecognized non-zero output is `command_failed` or a more specific fail-closed reason; free-form
stdout and bare diagnostic substrings are never availability evidence.
The Codex capacity families are the 429/rate-limit/quota/credit-balance diagnostics and the account
usage limit ("You've hit your usage limit", with or without its purchase-credits and retry-time
clauses), which classify as `capacity_unavailable` and stay retryable. The exact
`codex_models_manager` "failed to refresh available models: timeout waiting for child process to exit"
stderr line remains a closed, timestamp-anchored compatibility notice with no provider verdict. A static
catalog does not refresh remote metadata. A disabled-code-mode startup warning now fails closed because
it contradicts the prepared direct-mode catalog; it is distinct from a later tool-router rejection.
Router errors retain content-free counts and hashes, never model-authored payloads.

Codex 0.156.1 emits `Reconnecting...` and WebSocket-to-HTTPS fallback notifications during its own
transport retry. A successful invocation may include only their recognized closed shapes with nonempty
diagnostic text, after `turn.started` and followed by `turn.completed`; the final result still passes
all JSON/schema/business checks. Unknown sibling errors, tool activity and incomplete recovery remain
fail-closed. This accepts an internally recovered call, not a new application retry, permission grant or
backup-provider exception. The existing process deadline bounds the CLI's retry duration. Re-verify these
versioned event shapes on a CLI upgrade.
The versioned prefix/envelope identifies a CLI-owned intermediate retry notification; its variable detail
is not classified again as a terminal failure. This also covers rate-limit and IO messages the CLI recovered
from, including recovered HTML error pages with embedded newlines. JSONL records split on physical LF,
not Unicode separators inside JSON strings. A standalone error, malformed notice, forbidden tool, reroute
or failed turn remains a separate veto.
On a nonzero exit, those same intermediate notices do not replace the terminal verdict. The exact
0.156.1 terminal 429 retry-limit (including its optional single-token request id), high-demand and overload
messages classify as capacity unavailable, while
the known prematurely closed response stream and request-timeout messages classify as transport
unavailable. Every remaining diagnostic must agree. The timestamped `responses_websocket` failed-connect
debug line is a connection-attempt notice; the JSONL terminal error decides the result. Unknown terminal
messages, conflicting diagnostics, and tool/protocol errors still stop the call without failover.
The 0.156.1 `RetryLimitReachedError` display forms for HTTP 500, 502, 503 and 504 identify transport
unavailability; the inspected pinned source constructs the 500 form for transport retry exhaustion.
Ordinary HTTP 500 follows the existing high-demand/capacity classification. A validated CLI-owned
JSONL error event with the exact `unexpected status` prefix for one of those four statuses is transport
evidence; its server response body may be multiline HTML and
is opaque diagnostic content, not another semantic verdict. This body rule never applies to joined
stderr. As with existing exact-message rules, an error item can supply this evidence on a nonzero
exit; the classifier does not require a final `turn.failed` event. This allows a validated backup call,
not acceptance of the failed primary's output. Independent sibling events and stderr diagnostics
still have to pass their existing checks;
400/403, malformed status messages, unknown errors and explicit invalid output-schema errors do not
gain failover permission. This is a classifier correction within `availability_only.v1`, not permission
to fail over every `command_failed` or every error carrying `retryable=True`.
Each nonzero Codex invocation logs a bounded `semantic_provider_failure.v1` record to the worker's
existing stderr capture: exit status, classification, observed terminal HTTP statuses, group hash,
timestamp, and channel byte counts/hashes. No prompt, result, server body, URL or credential is copied.
This keeps diagnostic facts after temporary call files are removed; it cannot recover details from
historical failures that did not retain them.
The Claude error envelope's closed key set follows Claude Code 2.1.274: besides the earlier metadata it
carries `queued_turn_count`, `result_index` (non-negative ints) and `subagent_stats` (closed counter
shape; the adjudicator runs with tools disabled, so any nonzero subagent counter is a forbidden
capability, not availability; on the success path, which validates no envelope shape, a present but
unreadable block is likewise a breach). The CLI's `authentication_failed` sentences surface in `result`
with `api_error_status` null — "Failed to authenticate: OAuth session expired and could not be
refreshed", "Login expired · Please run /login", "Authentication error · This may be a temporary
network issue, please try again", "Invalid API key · Fix external API key" — and belong to the Claude
`not_authenticated` family (retryable); the middle dot is part of the complete diagnostic. Any other
unknown key still fails closed as `invalid_runtime_protocol`; re-verify the set on a CLI upgrade.

## Cache and receipt

`semantic_route_cache.v2` is group-level and provider-specific. Its key binds the full provider
identity, model/profile, prompt and output-schema hashes, taxonomy/router versions, and exact group
hash. A process-local single-flight lock prevents duplicate calls for the same key. Malformed normal
cache bytes are quarantined and recomputed; symlinks, identity/hash conflicts, and nondeterministic
existing entries fail closed. Only answers that passed the router's group validation are written. A
well-formed entry whose decisions fail it on a later hit means tampering or a semantics change without
a version bump; it fails closed as described above and stays in place as evidence.

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

# MinerU result storage: physical budgets, grants and bounded recovery

Large documents are handled by slower, bounded processing with backpressure and retained results, not by
raising per-document limits. This is the contract of the result-storage runtime selected by
`mineru.capacity-config.v2`; `mineru.capacity-config.v1` processes keep their exact behaviour. It changes
where bytes may be written, when a task waits and how an interrupted step continues. Parser outputs,
quality, the provider envelope, Units and public contracts are unchanged.

## Policy and versions

`mineru.capacity-config.v2` carries the v1 compute limits (N, P, F, H, window, threads, ratio, locks) and one
nested `mineru.result-storage-policy.v1` instead of the v1 per-task reservation B and aggregate limit L.
Every value is root-chosen and verified: the policy binds both volumes by total size, and a process whose
output volume does not match refuses to start. No production constant is derived in code. The ledger charges a
completion physically against P + C and sources may fill P exactly, so the policy requires the hard result's
physical charge (allocation-rounded bytes plus per-file overhead), not only its logical bytes, to fit C.

| Policy field | Meaning |
| --- | --- |
| `native_volume_total_bytes`, `native_work_disk_limit_bytes` (D), `native_free_floor_bytes` (H) | D is the service's working quota on the output volume; H is the space the OS keeps |
| `native_source_pool_bytes` (P), `native_completion_escrow_bytes` (C), `native_metadata_reserve_bytes` (M) | D = P + C + M; producers never borrow C |
| `native_source_single_limit_bytes`, `native_growing_producer_limit` | One producer's growth permit; one unbounded producer at a time |
| `native_result_hard_limit_bytes`, `native_normal_unacked_target_bytes`, `initial_result_estimate_bytes` | The single-result hard envelope (its physical charge must fit C), the soft unacked target, the H0 estimate |
| `native_allocation_unit_bytes`, `native_file_overhead_bytes` | Physical charge = allocation-rounded bytes + per-file overhead |
| `source_pdf_bytes_limit` | The unchanged 128 MiB source bound |
| `mac_*` | Mac work quota and floor, soft output target, decode input J limit, decode working set W, expansion factor, finite decode stage seconds |
| `max_members`, `max_name_bytes`, `max_inventory_bytes` | Archive envelope used by the zlib 1.2.11 deflate bound |
| `transfer_logical_deadline_seconds`, `progress_window_seconds`, `minimum_progress_bytes` | The transfer budget; the minimum rate must deliver the hard result in the deadline |

Derived identities: `mineru.process-profile.v3` (B/L null, `result_storage_policy_sha256`), native
`mineru-task-runtime.v4` / `mineru-task-registry.v4` / `mineru.capacity-observation.v2`, Mac
`staged-resource-credit-policy.v3` (the v2 policy bytes and SHA are unchanged), `remote-terminal-receipt.v5`,
`remote-parse-materialization-intent.v5`, `stage-resource-grant.v1` and `mineru-v4-spool-owner.v2`. Old
versions decode and verify exactly as before; nothing reinterprets an old B or credit record, and a hard
result is never clamped to an old per-task value.

## Native side

**Ledger.** Each accepted task carries a `mineru.task-storage.v1` record in registry v4 with a phase
(`admitted`, `source_growing`, `source_sealed`, `zip_writing`, `zip_sealed`) and a wait reason. A producer
starts only after `source + permit <= P`, `source + result + permit <= P + C` and live free space
`>= H + outstanding promises + charge`; a sealed source waiting for completion space goes first. The
status envelope `mineru.task-storage-status.v1` exposes phase, wait reason and age, the block flag and the
sealed facts (selected bytes S, members M, inventory SHA, ZIP extent).

**Writes before growth.** Every parser output write goes through the generated `FileBasedDataWriter`, which
calls `SourceGrowthPermit.write_file`: the whole-file charge is checked first, then the bytes are written
through the pinned task-root descriptor, one no-follow directory at a time, to a no-follow leaf checked on its
own descriptor (regular, one link) before truncation. A write without a bound permit in a storage process
fails closed. The retained ZIP is written by `BudgetedSeekableWriter`, a high-water extent writer that
charges local-header rewrites and the close records and latches the first write error.

**Ingress before the body.** For `POST /tasks` the request middleware requires one Content-Length (411),
refuses a body above `source_pdf_bytes_limit` plus 64 KiB of form framing (413), and charges the framework
multipart spool plus the upload copy in one decision before any body byte is read (closed 429
`storage_capacity_wait` with its reason). The endpoint moves the upload share to the key it names; the spool
share ends with the request. Storage binding places Starlette's multipart spool in `.agent-ingress-spool`
on the output volume (the same device is verified; a non-stdlib spool class refuses startup). The installation
and collector quiescence check accepts this canonical directory only when it is empty, owned by the same
user as the output root, on the same device and not a symlink; its identity is pinned and rechecked. Other
directories and retained spool files still refuse quiescence. The empty spool does not increase file counts.

**Seal, ZIP, recovery.** After parsing, `mineru.result-inventory.v1` seals the selected members by content,
one member descriptor at a time, binding directory and member (device, inode). The ZIP extent grant is the
deflate bound of the sealed inventory; `zip_writing` → `zip_sealed` → completed. Capacity waits keep the same
task and source: no failure, no ACK and no reparse. A restart with a valid source seal re-verifies it and only
re-ZIPs. Growth past the hard envelope, an archive beyond the codec bound, a write whose path, root or
leaf is not the owned tree, or a seal that no longer verifies is a hold (`hard_envelope_exceeded`,
`codec_bound_exceeded`, `tree_integrity`, `seal_integrity`). Identity refusals raise a protocol error, not a
growth-exhaustion error, and are latched on the permit; a refusal the parser catches still holds the task, and
its incomplete output is never sealed. A held task stays processing with its bytes charged and visible in
health, and never resumes by itself; the Mac worker observes it as a site stop (below).

**Operator decision for a held task.** A held task also blocks the installer's idle check and a registry under
another policy, so a larger envelope cannot release it. Its one managed exit is an explicit, attributed terminal
decision, never automatic and never a resume: `POST /agent/storage-holds/{task_id}` (operator only; task
execution and the Mac worker never call it) previews the exact held record (identity, state, storage record,
registry schema and the process's capacity-config SHA) as `mineru.storage-hold-preview.v1` with its digest, and
`execute` with that digest and `decided_by`/`reason`/`fixed_by` fails the task under the registry's own lock
with the closed cause `storage_hold_terminated` (`hold_reason`, `decision_sha256`, retry class `permanent`),
durable before the answer. An in-flight producer, a live reader, a stale preview, a task that is not held,
completed or unknown is refused; the same decision replays to the same receipt and another decision is
refused. Bytes and seal stay charged until the ordinary failed-task ACK. The durable cause keeps the
operator's canonical decision (`mineru.storage-hold-decision.v1`: schema, preview digest, `decided_by`,
`reason`, `fixed_by`, the exact preimage of `decision_sha256`) beside its digest, so every reader verifies
it and a lost answer or a later reader recovers the original attribution, not only a hash: a replay returns
it from the durable record, and the Mac records it in its own durable failure message
(`provider_storage_hold_terminated`, `provider_terminal`). The Mac entry is the operator CLI
`disclosure_anchor.cli.storage_hold`, bound to the attempt's durable accepted task; a lost execute answer is
reconciled from the task's ordinary status (only a durable decision equal to its own completes it), and
`recover` rebuilds the receipt from that durable decision alone. Nothing re-admits a producer for a held task
(restart recovery hydrates it as processing and the one scheduling site takes pending tasks only), so the
in-flight check at decision time needs no further lock.

**Operator gate.** The route shares one TCP/SSH origin with ordinary work: the proxy relays every path, and
the worker's tunnel reaches it. Hiding it from OpenAPI, the state digest and `decided_by` are therefore not
authentication. The route first requires `Authorization: Bearer <credential>` for an operator credential
whose verifier (`sha256` of the credential, never the credential) is enrolled inside the API container at
`/run/agent-invest-operator/storage-hold-operator.json` (`mineru.storage-hold-operator.v1`, owner-only
0700/0600, container filesystem, not a bind mount). Only an operator with `docker exec` on the host enrolls or
revokes it, and a recreated container starts disabled. The check runs before any body byte is read or any
registry state is consulted:
- nothing or an unsafe file enrolled → `403 storage_hold_operator_disabled` / `storage_hold_operator_misconfigured`;
- a missing, malformed or wrong bearer → `401 storage_hold_operator_unauthorized` with `WWW-Authenticate: Bearer`,
  after a constant-time digest comparison;
- then a declared length over 16 KiB → 413, and the body is read in chunks, a chunk that would cross 16 KiB
  refused before it is copied.

The credential is a dedicated one for this destination, never the admin API token or the tunnel key. It is an
owner-only 0600 file the operator passes to the CLI (`--operator-token-file`), never a worker setting or
environment variable; `storage_hold operator-verifier` prints only its verifier for enrollment.

## Mac side

**Grants.** A storage-bound poll accepts results up to the hard limit and produces a v5 terminal receipt
(S, M, inventory, policy). When the verified result exceeds the H0 estimate the backend raises
`StageResourceGrantRequired`; the coordinator admits the grown reservation FIFO against contending
grants, closes new admission while a grant waits, and stops the site durably
(`coordinator_circuit/stage_grant_unsatisfiable`, see [worker operational stop](worker-operational-stop.md))
when no ledger can ever fit it. The grant fixes the decode envelope: RAM W, disk Z + S + W, output S + W. The v5 intent
embeds the grant; durable replay binds its basis to the terminal storage envelope. A pre-v3-credit
reservation (a legacy obligation) is granted only as a verified member of a qualified runtime upgrade and
names that upgrade and its original spec; a resumed allowance re-checks both against the approval active now.

**Mac volume and live space.** A materializer composed with the policy refuses a work volume whose total size
differs from `mac_volume_total_bytes`. A granted attempt asks for space only where it will write, after its
ownership is verified. Before creating or continuing the spool its promise is the grant's temporary disk less
the re-hashed durable prefix. Before unpacking it is the grant less the hash-verified spool and the
record-verified members. Replaying already promoted output asks for nothing. Live free space must cover the Mac
floor plus every in-flight promise in the worker process, or the same attempt waits
(`materialization_capacity_waiting`, no retry budget, nothing written for that step). The release binding
refuses Mac temporary-disk and terminal-output ceilings above the Mac work quota D.

**Work quota D.** The live free floor H and the business quota D are different invariants; per-dimension
credits alone bound neither the union of distinct files on the one work volume (Pro R3: every dimension
fits a legal D = 32 GiB policy while two documents own 36 GiB of distinct extents). The coordinator
therefore charges each attempt its distinct work-volume footprint, computed from its own durable credits
(verified ownership; recovery counts it once and never reserves it from free space again) plus the
provisional hold of its in-flight stage (the promise):
`snapshot + max(temp, compressed + output)` plus one document's allocation margin
(`(max_members + 8) × 4 KiB`, the APFS block; the materializer refuses a volume that allocates in larger
blocks). The source snapshot is its own file; a LOCAL grant covers spool, unpacked tree and serialized
outputs, and the promoted output is that tree renamed, so the larger of the grant and the retained spool
plus output counts, never both. A transition that would carry the sum past D waits, queued and visible as
`work_disk_bytes`; only growth is checked, so COMMIT, CLEANUP and ACK, which keep or release space, always
run and cleanup can never be stranded. Admission keeps one maximal grant of D free
(`work_disk_local_reserve_bytes`) and sets each new document's margin aside before it offers snapshot bytes,
so documents that own only their source snapshot never fill D past the point where a waiting grant can
start. A grant waiting at the head of its lane holds later new grants back, so it is never overtaken
indefinitely, and waits only for the LOCAL work ahead of it to drain, which needs no growth. The policy
refuses a D that cannot hold one maximal grant with its source snapshot and margin, so admission can always
take a document and no supported document is held forever by D. Snapshots recovered beyond D minus the
reserve (admitted under another policy) instead let fitting grants run past the head, so they drain. Readiness files are written to the published corpus, outside D: before its first write a
COMMIT promises exactly the readiness files not yet on disk against the same in-process free-floor promises
(`publication_capacity_waiting`, a healthy wait), and promoting the parser tree is a rename. The corpus is
the long-term owner of published bytes; an attempt's footprint leaves D when its cleanup releases its credits.

**Resumable spool.** A v5 intent downloads across stages. The owner receipt `mineru-v4-spool-owner.v2` is a
fixed identity header and two checksummed progress slots written alternately in place: sequence, durable
offset, prefix SHA-256, part identity, accumulated active transfer time, progress-window state and restarts.
Each record follows an fsync of the part. A reopen verifies the part identity, re-hashes the recorded prefix
and cuts only an unrecorded tail; an unproven owner or prefix, or a short part, is an integrity hold that
stops the site (`transfer_integrity_hold`), never a restart. A
resumed read is one `Range` with `If-Range` naming the result's strong ETag (its SHA-256); only an exact 206
suffix is appended. A 200 for the same identity or a 416 restarts from zero once, then holds; a 200 for other
bytes is identity drift. The transfer stops 10 s before its stage deadline or remaining logical budget: with
durable progress it continues without spending the retry budget, without progress it is an ordinary retry.
The logical deadline and the minimum progress per window are measured on accumulated active transfer time, so
restarts and wall-clock jumps cannot reset them; exhausting either is a visible per-attempt hold.

**Holds on the Mac.** A native `storage_blocked` answer, a transfer integrity hold and an unsatisfiable grant
stop the site with a typed first cause (F5 `coordinator_circuit`: `native_storage_hold`,
`transfer_integrity_hold`, `stage_grant_unsatisfiable`); nothing is failed, cleaned or ACKed. Decode-envelope,
transfer-budget and publication-envelope holds stay per attempt, claimed and visible, while other work runs,
unless such holds alone use up a ledger dimension with a positive limit (`capacity_holds_exhausted`).

**Unpack and decode.** A v5 unpack writes its one in-flight member aside, appends and fsyncs that member's
exact size and SHA-256 to a record journal, and only then publishes it into `.unpack` by exclusive rename; it
stops at a member boundary before the stage deadline. A reopen adopts only a clean partial unpack of the same
intent in which every published member matches its own record (size and SHA-256 from the pinned scan, never
the forgeable ZIP CRC); it cuts a torn trailing record and redoes the in-flight member. Anything else is
resolved by the unchanged staging classifier, which retains an ambiguous staging for an operator. The artifact root is
located from metadata and decoded exactly once; decode input above J and serialized outputs above W are held
before any mutation with the verified result kept. The LOCAL stage runs with the policy's finite decode
deadline and independent claim renewal, which never extends that deadline. The stage guard is checked right
after the decode, before staging is mutated and before promotion: a decode that crosses the deadline publishes
nothing, and the good ZIP and staging are kept.

**Heavy work.** LOCAL's one decode, and COMMIT's reopen, Unit build, readiness and promotion, hold whole
objects; the credits held after LOCAL do not. One coordinator-owned permit (`heavy_work_permits`, 1 until real
memory evidence justifies more) spans them across the separate lane pools. COMMIT takes it at dispatch; a
LOCAL stage runs its transfer and unpack without it and, if it reaches the decode without it, stops at the last
point the unpack still resumes exactly (every member durable with its record, nothing decoded), and is
dispatched again with the permit when it is free, COMMIT first. The permit ends with its stage whatever the
outcome (success, error, wait, cancellation), is never held by a waiting durable item, and never gates ACK,
CLEANUP, claim renewal or remote reconcile. CLEANUP decodes nothing: its transfer is bound by the durable
receipt's output inventory (every file's SHA-256, total bytes and file count), exactly as the transfer proves
it before and after the rename. Promotion is bound the same way and keeps only the one reopened object alive;
LOCAL releases its decoded projection before the final load and proves the promoted tree by the sealed
staging's file inventory instead of decoding it a second time; readiness reuses the artifact bytes it just
encoded for a preparation it created and compares requests by content address. W remains the admitted decode
working set, not an OS memory limit: the permit serializes the heavy phases, it does not measure their RSS.

**Publication envelope.** The canonical request (8 MiB), its preparation (24 MiB, embedding the request) and
the readiness manifest (8 MiB, every Unit binding) are fixed private envelopes. Readiness encodes every one of
them before its first write, so a document whose records do not fit is refused with
`PublicationEnvelopeExceededError` before any readiness write or transaction P, at the request builder or at
readiness. COMMIT holds that attempt per document (`stage_capacity_hold:publication_envelope`): the
materialized output stays exactly as it is, other documents continue, and nothing is truncated, failed as
content damage or retried in a loop. Its exit is a release whose envelope holds it. Reading bytes beyond an
envelope back from disk stays an integrity refusal.

## Pressure and remote execution

`mineru.stream-policy.v2` is the only executable pressure algorithm; see
[stream admission](mineru-stream-admission.md). The newly qualified native runtime carries the nine prepared
E7 obligations through `worker-qualified-runtime-upgrade.v1`; see
[local execution upgrade](local-execution-upgrade.md).

## Limits of this phase

- Windows scripts (installer capacity intake and idle health, collector probe) were not executed here; the
  release install/attest/canary chain is owed.
- Multipart spool placement on the output volume assumes the bind mount supports the stdlib spool's
  unlinked temporary files; live installation must confirm it.
- A parse-time ENOSPC despite a permit fails visibly; there is no internal replay.
- The Mac live-space gate counts promises of materializations in this worker process only, in exact logical
  bytes; per-file block slack is modelled only in D's allocation margin, which assumes a volume allocating in
  blocks of at most 4 KiB (binding refuses a larger `f_frsize`).
- The reserve is the policy's maximal grant, which bounds every v5 grant; an estimate-sized first LOCAL
  hold larger than it (possible only if the profile's estimate multipliers exceed the maximal grant) has no
  completion proof. Snapshots recovered beyond D minus the reserve drain through fitting grants; if every
  queued grant is larger than what they leave free, the lanes wait visibly on `work_disk_bytes` until the
  policy that admitted them is restored.
- The allocation margin covers a maximal-member document, and it is charged to every admitted document,
  including one that owns only its source snapshot. That is conservative: under the draft policy (D = 64 GiB,
  100,000 members, margin ~391 MiB, maximal grant ~31 GiB), admission holds at most (D − reserve − LOCAL
  work) / (snapshot + margin) documents: about 85 with no LOCAL work in progress, against 128 document credits.
- One heavy-work permit is a conservative choice, not a measured one: W is the admitted decode working set,
  not an OS memory limit, and no RSS bound for the heavy phases has been measured. D charges an attempt's
  output until its cleanup releases the credits; readiness files are covered by the free floor only when the
  readiness adapter is composed with the materializer's write space.
- The private publication envelopes (8 / 24 / 8 MiB) are unchanged. The published winner row keeps its 8 MiB
  DB check without a typed pre-transaction refusal; it is argued to be dominated by the request envelope,
  not separately witnessed.
- The original-key lookup evidence pins the bytes an operator captured with the read-only builder; its hash
  does not authenticate that the right API answered. Deployment must capture it supervised, against the
  identified origin API and its read-back key lifetime, before that API retires.
- v4 (legacy) materializations keep their exact stage-guard sequence; the post-decode checkpoints apply to v5.
- A result whose single member cannot be extracted within one stage retries and then opens the circuit; ZIP
  stream breakpoints are deferred.
- The qualified upgrade branch refuses an expired prepared key at preflight and again at the POST boundary,
  where the same approved lifetime is judged on the transport's wall clock just before the POST; the head
  stays `reconciling` and the coordinator stops visibly. Expired, never-submitted prepared obligations have a
  separate managed closure ([local execution upgrade](local-execution-upgrade.md)); requeuing their failed
  runs waits for a reviewed migration of the requeue-class constraint.
- A Mac per-attempt hold has no abandonment entry in this phase; its exit is a larger declared envelope.

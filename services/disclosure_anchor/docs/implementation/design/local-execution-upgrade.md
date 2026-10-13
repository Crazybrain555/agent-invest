# Local execution upgrade (U01)

This is the current contract for running reviewed new worker code (E1) on an immutable, already
qualified MinerU deployment (Q0) while the unresolved V4 obligations frozen under the old code (H0/spec0)
drain unchanged. It is one exact, explicitly configured edge, not a versioning system. Operator
procedures are in `../runbooks/production-operations.md` §1.1g; the F5 stop it reuses is in
`worker-operational-stop.md`.

## Goal and non-goals

- A local writer change (the legacy 42-member writer digest) stops being a reason to abandon a still
  fresh parent qualification, when an independent review approves the exact change, the exact release
  bytes and the exact list of old obligations.
- The parent qualification is inherited and visibly labelled `compatible_parent`. It is never
  re-labelled current or turned into a new PASS. Its age, canary cache, held-out receipts, capacity and
  policy are re-verified as history against the real clock.
- Every original unresolved responsibility continues from its own H0 and spec: the same request bytes,
  idempotency key, source, fence, runtime reference and checkpoint history. Accepted tasks resume through
  GET/poll/result/cache/ACK with zero POST. A valid never-submitted member may submit once under its
  original request and key; stream pressure and admission use the current runtime R1.
- No new H0 is admitted until every listed obligation is final.
- Not added: a second ledger or queue, a migration, a transitive upgrade chain, a hash allowlist, a
  rewrite of H0, spec or Q0 bytes, a change to the writer members, or an expired-key re-submission
  branch. Actual key-lifetime evidence decides that branch separately.

## Identities

| name | what | where |
|---|---|---|
| Q0 | parent qualification: smoke (M0 = its `runtime_manifest`, R0 = canonical SHA of M0, W0 = M0's writer), canary cache (original `passed_at_utc`), held-out receipt (service epoch, qualified API owners), P0, WP0, A0 | the existing smoke/canary/validation settings, plus the proposal's P0/A0 files |
| E1 | actual current release: every loadable file, W1, the worker interpreter's package set, M1/R1, P1, WP1, A1, v11 capacity | `worker-execution-release.v1` manifest and the current runtime/profile/activation settings |
| H0/spec0 | every unresolved V4 obligation captured before deployment | `worker-legacy-scope-inventory.v1` |
| U01 | the proposal binding Q0, E1 and the inventory (`worker-local-execution-upgrade.v1`) plus its independent GO review (`worker-local-execution-upgrade-review.v1`) | four settings, below |

E1's file scope is fixed in code, rooted at the loaded package's own location (never the current
directory; the worker runs with `PYTHONPATH=src`, which precedes the editable install). It covers:

- every regular file under `src/disclosure_anchor/`;
- every regular file directly under `scripts/`;
- every regular file under `scripts/launchd/`.

Only `__pycache__/` directories and `.DS_Store` are excluded. The import system ignores `__pycache__/`
bytecode without its source (PEP 3147). Refused, never skipped:

- bytecode anywhere else, which `SourcelessFileLoader` imports without a source;
- a symlink or non-regular entry;
- an unknown directory under `scripts/`. `scripts/windows/` is outside the scope because it runs only on
  the Windows node.

Third-party code in the worker interpreter is bound by its package set (every installed distribution's
name and version), not by file bytes. The only local package in that interpreter is the editable
`disclosure_anchor`.

## Configuration

`DISCLOSURE_WORKER_EXECUTION_UPGRADE_FILE` / `_SHA256` and
`DISCLOSURE_WORKER_EXECUTION_UPGRADE_REVIEW_FILE` / `_SHA256` are all-or-nothing. When absent, every
strict exact path is byte-for-byte the previous behaviour: exact worker-profile equality, the
writer-drift refusal and the runtime check in the stream guard. When present:

- the runtime identity, the process profile and the stream activation settings name R1/P1/A1;
- the smoke, canary-cache and validation settings still name Q0's original files;
- the capacity config is unchanged.

Every artifact is a regular, single-link file owned by the worker user. Proposal, review, E1, M1 and
the inventory must be mode 0600 (the gate's evidence reader). Profiles and activations use their
existing loaders. SHA-256 values are over exact file bytes.

## The one verifier

`verify_mineru_deployment_gate(..., accept_execution_upgrade=True)` is the only producer of
`VerifiedQualifiedExecution` (`application/contracts/worker_execution_upgrade.py`). With U01
configured, every caller without that flag refuses: worker once, pipeline, admin, staged
commission/campaign/recover and the recovery gate path's composition. Only the resident `worker loop`,
`worker deployment-preflight` and doctor accept it. Each of them constructs the same
`MinerUDeploymentChecker`. The verifier (`adapters/runtime/mineru_execution_upgrade.py`) requires, in
order, with no fallback to the exact path:

1. The pinned proposal and review. The review is `GO` for exactly the proposal file's SHA-256.
2. **E1.** The release manifest's pin, plus two-way set and byte equality with the tree. The recomputed
   W1 equals the manifest and the proposal. The running interpreter's package set matches.
3. **M1/R1.** Verified by the existing `verify_runtime_manifest_payload`: the configured R1, the
   measured MinerU client, the recomputed W1 and the explicit v11 capacity. P1 passes the existing
   capacity and manifest checks.
4. **Q0.** Re-verified by the existing smoke/canary/held-out verifier through a named
   `ParentQualificationExpectation(R0, W0)` and P0. Settings are never mutated. The Q0 bytes are pinned
   before and after, and freshness uses `DISCLOSURE_MINERU_CANARY_MAX_AGE_SECONDS` from the original
   dates.
5. **The same computation.** M0 equals M1 except `client.writer_code_sha256`, and W0 != W1. The fields
   `qualified_at_utc` and the service epoch equal Q0's own.
6. **The only moved references.**
   - P1 is P0 with only the runtime reference moved.
   - WP1 (the composed worker profile) is WP0 with only the process-profile reference moved.
   - A1 is A0 with only its two runtime references moved.
   - A1's owner is the native owner recorded by every Q0 held-out document.
7. **The inventory.** It matches its pin and the proposal's member count, and every member binds R0,
   P0 and WP0.

The result is `VerifiedMinerUDeployment(qualification_origin="compatible_parent",
runtime_identity_sha256=R1, canary_passed_at_utc=<Q0 original>, execution=<context>)`. After Q0's
real age expires, admission refuses exactly as it would for an exact deployment. From then on only a
new qualification of the current runtime (not another upgrade) restores parsing.

## Boundaries

| boundary | behaviour under U01 |
|---|---|
| resolver (`ProductionV4StageInputResolver(legacy_execution=)`), the single profile check in `_validate_closed_packet` for every lane including cleaned-source tails | The active WP1 → the exact path, unchanged. Any other profile → the head must be an inventory member bound to R0/P0/WP0, with an identical document, run, generation, fence, source, target, request, runtime, key, submission epoch, spec and H0. Its history must continue the captured, hash-linked prefix (see legacy progress). Every original closure check then runs unchanged. |
| submission command | The resolver attaches a non-persistent `VerifiedLegacyExecutionAuthorization`, issued by this context, to legacy members only. |
| POST boundary (`MinerUHttpRemoteV4(legacy_execution=)`, in `before_send`) | A legacy proof must come from this context and match the command's intent, attempt, fence, source, request hash and exact bytes, R0 and key. The stream guard then runs with R1. With a context present, a command without a proof must be R1's own. `recovery_only` still forbids every POST. A refusal raises before the first byte is sent: a stage fault, then F5. |
| new-H0 hold (`StagedV4NewWorkAdmitter(legacy_obligations=)`) | Prepared claims of existing heads continue. Before the ordinary scan (observation, source rejection, ingress), `PostgresLegacyObligationsGate` raises `LegacyObligationsOpen` until every member row is final and not current. Progress shows `blocked=admission_deferred:legacy obligations open (<open>/<total>)`. Closure is read from authoritative rows and cached once proven; recovery-scan completion never counts. |
| composition (`build_staged_worker_v4_runtime(verified_execution=)`) | Required exactly when U01 is configured, for the unscoped resident runtime only. It must equal the composed P1, WP1, R1, capacity and activation. |
| in-worker recheck | Under the singleton, before dependencies, reports, startup recovery or maintenance: re-hash E1, then one READ ONLY REPEATABLE READ snapshot. Every member is an exact continuation or final. While any member is still open, any unresolved head outside the inventory refuses as premature new work, even one bound to E1. After every member is final, each outside head must be bound exactly to E1's WP1/P1/R1, so ordinary restarts with new current work need no U01 change. A refusal is an F5 stop `startup_fatal`/`startup_recovery`/`execution_upgrade_scope_failed` (exit 78). A transport-level DB error exits unlatched, like any other pre-dependency startup failure. |
| POST proof | Before the admitted runtime is returned, the proof's fence, H0, spec, source, request, key and parent P0 are re-compared with its inventory member. Live claim validity stays with the stage guard. |
| legacy progress | The captured prefix `history[0..observed_version]` must be contiguous and hash-linked: each `previous_checkpoint_sha256` equals its predecessor's SHA-256, and the last equals the captured checkpoint. |
| per-boot receipt | After the singleton, the recheck, composition and staged startup verification, and before the coordinator runs, the worker writes one create-only 0600 `worker-execution-boot-receipt.v1` at `$DISCLOSURE_RUNTIME_ROOT/reports/execution-boot/<coordinator owner>.json` (FileStorePathBuilder, `publish_new_exact`). It binds the owner, pid, boot time, U01, the review, E1, W0/W1, R0/R1, P0/P1, WP0/WP1, capacity, A1, the parent date, the inventory and the observed scope counts. The log line `[execution-upgrade] boot receipt=<path> receipt_sha256=...` points to it. A failure to publish it is a staged startup stop (`startup_fatal`/`staged_startup_verification_failed`) and the runtime is closed. It is evidence only, never read back as authority. |

**Scope.** Scope is every current V4 head, globally, with no owner, document or count filter. A
non-current V4 `prepared` row (a staged superseder H0 awaiting activation) is a latent obligation this
edge cannot follow. Capture and verification therefore refuse while any exists; the exact path does not
change. The inventory never holds capability plaintext.

Capture checks that this database has no held worker singleton before and after
reading the inventory. The operator must keep the producer stopped through
deployment. Re-verification rejects any non-member whose database `created_at`
is at or before `captured_at_utc`, even after all listed members close and even
when an E1→E2 release preserves R/P/WP. Missing, naive or future creation times
also refuse. Both timestamps come from PostgreSQL; this is not a monotonic
clock guarantee and does not replace the stopped-producer discipline. A later
head still requires full legacy closure and exact current execution binding.

## Deployment preflight

`worker deployment-preflight [--format json|terminal] [--prepared-key-ttl-seconds N]` is technical
install eligibility, never a start authorization (`worker-deployment-preflight.v1`, exit 0 ready, 78
not ready). It runs:

- the resident loop's checker;
- the F5 control read (`OPERATOR_DISABLED` is reported, not blocking);
- `inspect_frozen_identity` (the resolver closure without a claim) for every unresolved head;
- the U01 scope check;
- the prepared-key age report;
- one live, read-only API owner sample compared with A1's owner (the API must be healthy and idle).

It opens one READ ONLY snapshot. It never writes, claims, recovers, runs maintenance, POSTs or records a
stop.

- **Control first.** Like the worker start gate, an unreadable control plane or any state other than
  `RUNNABLE`/`OPERATOR_DISABLED` refuses before the checker, the DB and the native probe.
- **Without U01.** The same closure applies to the active profile, so the old-profile/new-code mismatch
  refuses before any launchd change.
- **Legacy-sync mode.** Only the resident checker runs. `worker loop` constructs that checker before the
  singleton in every mode, so an install it rejects could only crash-loop.
- **Installer.** `scripts/install_launchd.sh` runs the preflight in every mode, after the F5 stop
  preflight and before any plist, enable or bootstrap, passing `--prepared-key-ttl-seconds` through.

**Prepared keys.** The lifetime is the provider's idempotency-key lifetime: the installed MinerU API's
key/tombstone TTL, enforced when `enforce_key_lifecycle` is on. It is not the task retention. Root reads
it from the running API and passes it; nothing in the product hardcodes or persists it.

- Prepared and reconciling heads report their key age: the preflight clock minus the key's
  `submission_epoch_unix`.
- Without a supplied lifetime the status is `unverified` and blocks; an unknown lifetime is never
  assumed POSTable.
- With a lifetime, an age at or beyond it is `expired` and blocks. Leave margin for clock skew and the
  drain time.
- v1/v2 upgrades have no runtime key-lifetime gate. A qualified upgrade re-checks its approved lifetime at
  the POST boundary (below). No version has an expired-key branch; existing per-attempt budgets and provider
  protocol handling are unchanged.

## Builders

`python -m disclosure_anchor.cli.execution_upgrade` (JSON on stdout; exit 0/64/65/70) writes new 0600
files only:

- `release-manifest --source-revision REV --output PATH` writes E1.
- `derive --parent-process-profile P0 --parent-activation A0 --output-dir DIR` writes M1, P1 and A1
  from Q0's M0 and the recomputed W1. It reloads them through the product loaders and prints the env
  values.
- `legacy-scope --output PATH` writes the READ ONLY inventory.
- `propose ... --output PATH` writes the proposal. It is run after the environment names R1/P1/A1 and
  before U01 is set.

The reviewer writes the review file. `derive` and `propose` default to v1; their v2 forms are below.

## U01 v2: qualification anchor, recovery origin and target

v1 couples two different facts. Q0 is where the computation was qualified; the parent's R0/P0/WP0 are
also where every legacy obligation was frozen. After one upgrade has run, the open obligations are
frozen under E1 (R1/P1/WP1), not under Q0, so a second v1 edge from Q0 cannot list them, and an edge
from E1 would present E1 as a qualification it never was. `worker-local-execution-upgrade.v2`
separates the roles; v1 bytes, decoding, checks and texts are unchanged.

| role | v2 field | verified from |
|---|---|---|
| Q0, qualification anchor | `qualification_anchor` (the v1 parent fields) | Q0's smoke/canary/held-out settings and the explicit P0/A0, exactly as a v1 parent: original `qualified_at_utc`, real-clock age limits, service epoch and held-out owners. Its date is never reset. |
| recovery origin | `recovery_origin`: release manifest file/SHA, source revision, writer, runtime bundle file/SHA, runtime identity, process profile file/SHA, worker profile SHA, capacity SHA, activation file/SHA | Only the archived files, each against its own pin: the release manifest decodes and names the origin writer and revision, the runtime bundle recomputes to the origin runtime and carries that writer. The origin is never compared with the current tree and no earlier upgrade file is read. |
| target | `current_execution` | The actual current tree, interpreter packages, writer, runtime, profiles and activation, exactly as v1's E1. |

The contract refuses a target whose release equals the origin release and an origin whose capacity
differs from the target. The verifier proves two direct relations, never a chain:

- **Q0 → target.** The same computation: the runtime manifests differ at most in the local writer. P,
  WP and A move only their runtime or process-profile references, and the target activation's owner is
  Q0's held-out owner.
- **origin → target.** The same computation again from the archived origin manifest, with the same
  reference-only moves for P, WP and A.

Unlike v1, the writer may stay. A release that changes no fingerprinted writer file moves only E: the
target keeps the origin's writer and runtime, and P, WP and A are unchanged. No fingerprint member is
added or removed to force a new runtime.

Every inventory member must be bound to the origin's runtime, process profile and worker profile; the
anchor never appears in a member proof. `VerifiedLegacyExecutionAuthorization` keeps its `parent_*`
field names, which carry the member's own execution (the v1 parent, the v2 origin). The new-H0 hold,
the in-worker recheck and the resolver classify heads by inventory membership first, so they behave
the same when the target runtime equals the origin runtime: no new H0 until every member is final,
and a legitimate `local_failed` closure is a final member state like `acked`.
`LegacyScopeObservation.closed_state_counts` reports the final states, and the boot receipt, the worker
log (`final_states=`) and doctor show them.

Summaries name each role: v2 uses `anchor_*`, `origin_*` and unprefixed target keys, while v1 keeps its
`parent_*` keys and adds only `upgrade_contract_version`. A v2 boot writes
`worker-execution-boot-receipt.v2` with every summary identity. The preflight terminal prints separate
qualification anchor, recovery origin and target lines.

v2 builders:

- `derive --contract-version v2 --anchor-process-profile P0 --anchor-activation A0 --output-dir DIR`
  derives the target runtime, process profile and activation from Q0's M0 and the local writer. The v1
  form requires a new writer; v2 also accepts Q0's writer and then derives Q0's own R, P and A. The
  output names the roles `anchor_*` and `target_*`.
- `propose --contract-version v2` takes `--anchor-process-profile`, `--anchor-activation`,
  `--origin-release-manifest`, `--origin-runtime-bundle`, `--origin-process-profile` and
  `--origin-activation` with the usual release, runtime bundle, inventory and evidence arguments. Mixing
  v1 `--parent-*` and v2 flags is a usage error.

## U01 v3: heavy-work permit forms

A `staged-worker-composition.v3` worker profile declares how many whole-object heavy phases may run at once.
See [worker dynamic scheduling](worker-dynamic-scheduling.md) §10 and
[result storage](mineru-result-storage.md), "Heavy work". v1 and v2 cannot carry it:
- their worker-profile rule moves only the process-profile reference;
- Q0's smoke, canary and held-out evidence records no worker profile.

So a v1 or v2 relation over a v3 composition would silently assert that Q0's composition was v3. Both
therefore refuse an active v3 composition, and every composition that was valid before keeps its outcome.

`worker-local-execution-upgrade.v3` is the v2 relation plus one closed section that names each role's form:

```json
"heavy_work_permits": {"anchor": null, "origin": null, "target": 2}
```

`null` is the v2 composition with its one implied permit; a count `1..2` is the v3 composition declaring it.
Everything else is v2, unchanged: roles, files, Q0 re-verification with its original date and real-clock
ages, the origin's archived pins, inventory binding, resolver and POST proofs, the new-H0 hold, the in-worker
recheck, preflight and summaries.

The worker-profile relation becomes:
- the active (target) composition must have the declared target form;
- the anchor's and the origin's worker profiles must be exactly the target with their own process-profile
  reference and their declared form;
- no other field may move, so lanes, poll, probe and the commit budget stay where Q0 had them;
- each role is checked against its own pinned hash, and the origin's is also bound by every inventory member's
  spec.

The contract (`HeavyWorkPermitForms`, `derive_relation_worker_profile`) and the worker profile's own mapping
(`heavy_work_permit_form`, `with_heavy_work_permit_form`) are pure functions.

A v3 target may keep its origin's release only when the origin and target forms differ. That allows enabling,
or rolling back, a permit count as a configuration-only edge: same release, runtime, P and A. Like any U01
edge it is captured with the producer stopped:
- in-flight stages are cancelled and keep their nonterminal heads;
- listed members continue with their original H0, spec and key under the target count;
- new H0 waits until every member is final.

No separate drain is needed. Once a v3 composition runs, every later release is again a v3 relation, for
example `{"anchor": null, "origin": 2, "target": 2}`.

Reporting:
- summaries add `anchor_heavy_work_permits`, `origin_heavy_work_permits` and `heavy_work_permits` (the target);
- a v3 boot writes `worker-execution-boot-receipt.v4`, and its log line ends with
  `heavy_work_permits=anchor:…,origin:…,target:…`;
- the preflight terminal prints a `heavy-work permits:` line;
- doctor and the worker log show the contract version.

v3 builders:
- `derive --contract-version v3` derives exactly as v2; the worker profile is composed, never derived.
- `propose --contract-version v3` takes the v2 flags plus `--anchor-heavy-work-permits`,
  `--origin-heavy-work-permits` and `--target-heavy-work-permits` (`implied` or a count). It refuses a target
  form that is not the active composition's, and inventory members not bound to the derived origin worker
  profile.
- Its output names the three worker-profile hashes. The anchor's must equal the deployed proposal's anchor
  worker profile: Q0's composition does not move.

## Qualified result runtime upgrade

`worker-qualified-runtime-upgrade.v1` (`transition_kind=newly_qualified_result_runtime`) is a separate branch
for a target whose native runtime changed and was qualified anew. It does not loosen
`require_computation_invariance`, `require_same_computation` or the v1/v2 mapping predicates, and it is
selected only by its own contract version. The same four upgrade settings and the same GO review contract
carry it.

| role | field | verified from |
|---|---|---|
| Qnew, the target's own qualification | `target_qualification` | The unchanged exact deployment path: the configured smoke/canary/held-out files must be exactly the proposal's pinned files (before and after), their runtime manifest is the target runtime, the canary date and service epoch match, and the configured activation owner is Qnew's held-out owner. The deployment is `exact`, and it ages from Qnew's own date. |
| recovery origin (E7) | `recovery_origin` | Only its archived files and pins, as in v2. |
| target | `current_execution` | The current tree, packages, runtime, profiles, capacity and activation. |
| moved manifest fields | `runtime_changes` | Exactly the recomputed difference between the origin and target runtime manifests; must be inside the closed result-storage set below. |
| original keys | `key_lookups` | `worker-legacy-key-lookup.v1`, pinned. |

The origin → target relation allows only:

- runtime manifest fields: the local writer, the native image digest, the heap-return and capacity
  compatibility layers, the service config, the capacity config with its SHA/source and the legacy B/L
  fields, and the Windows compose/collector hashes. Models, the inference server, MinerU versions,
  windows, ratios, locks, commands, endpoints and node identity must be equal;
- capacity: every v1 compute limit equal, target is `mineru.capacity-config.v2`;
- process profile: only its contract version, runtime and native image references, B/L, the storage policy
  reference and the Mac temporary disk, decoded payload and terminal output ceilings;
- worker profile: only the process-profile reference. The origin activation is archival only, because
  pressure control is always the current runtime's.

Scope: every inventory member must be `prepared` with no accepted submission and bound to the origin's
runtime, process profile and worker profile. Anything past `prepared` may have been accepted or be an unknown
POST, and it refuses the branch. Each member's original key has one closed 404 lookup answered by the origin
runtime, observed after the inventory capture and inside the provider key lifetime. The new-H0 hold, the
in-worker recheck, the resolver, the POST proof and the per-boot receipt (`worker-execution-boot-receipt.v3`)
are the same mechanisms as v2. A legacy member's storage grant names the upgrade SHA and its original
execution spec, and a resumed allowance re-checks both against the approval active now. A pre-storage
reservation is never granted storage outside this relation. The preflight evaluates prepared keys with the
approved lookup lifetime only: a different supplied lifetime is a blocker and never re-classifies a key.

A key that was valid at preflight can expire during a long wait, and an expired key's 404 no longer proves the
task was never accepted. The POST proof therefore re-checks the same approved lifetime at the instant the POST
would leave: `require_submission` takes the transport's wall clock and refuses when `now −
submission_epoch_unix` reaches the lifetime, or when no clock is given. Nothing is sent. The refusal surfaces
as a visible stage fault, so the head stays `reconciling`, nothing fails and no new key is made. A lookup that
finds the task reconciles it without reaching this check, and the poll, result and ACK lanes never consult it.

The lookup evidence is operator-captured. Its pinned hash fixes the bytes, not the external fact that the
identified origin API answered them. The deployment checklist therefore requires a supervised capture against
the identified origin API, with its key lifetime read back from that API, before the API retires.

Builders: `execution-upgrade legacy-key-lookups --inventory ... --key-ttl-seconds N --output ...` performs
the read-only lookups against the still-serving origin API, and `execution-upgrade propose-qualified
--release-manifest ... --runtime-bundle ... --origin-release-manifest ... --origin-runtime-bundle ...
--origin-process-profile ... --origin-activation ... --inventory ... --key-lookups ...` assembles the proposal
once the target holds Qnew. An expired prepared key has no path through this branch.

**Expired prepared closure (Pro VI.2).** Closing is conditional: a key still inside its actual lifetime stays
on the guarded original key. For expired ones, `python -m disclosure_anchor.cli.expired_prepared_closure`
(separate from this branch's approval and boot) is the managed entry.

- `preview` is read-only. It writes the canonical `worker-expired-prepared-closure-plan.v1` and its digest.
  It requires, for the fixed inventory under the named origin runtime and actual key lifetime:
  - supervised closed-404 lookups of every original key, answered by the origin after the capture and while
    the key was still alive (a 404 after expiry proves nothing);
  - a head that is still exactly the captured, never-submitted prepared H0 (V4 makes a submission intent
    durable before any POST).
- `execute` requires that exact plan digest and an attributed decision, and holds the worker singleton. Each
  expired member is claimed with the repository's exact CAS and failed before submission with the typed
  `original_key_expired` cause (retry class `original_key_lifetime`) through the ordinary failure. Its owned
  local cleanup then reaches `pre_submission_failed`, with the failed run, document and outbox in one commit.
- It never POSTs, looks up, alters the H0, extends the lifetime, makes a new key or ACKs.
- Members close one at a time, each through its own durable CAS commits; there is no batch transaction. A
  live claim, a moved head or another decision is refused at that member before anything is written for it,
  and earlier members stay closed. Re-running the same plan and decision is safe: closed members are
  reported unchanged, and an interrupted member continues only when its failure names exactly this decision.
- Requeue is a separate, explicit parse-requeue decision per failed run; closure never requeues. Revision
  0065 widens the requeue-decision table's closed class constraint (`ck_parse_requeue_decision_class`) by
  exactly `original_key_lifetime`, mirrored by the decision contract. Until a decision names the run, the
  queue's contract-failure gate keeps it out.

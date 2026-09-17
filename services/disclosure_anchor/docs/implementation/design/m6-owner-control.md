# M6 owner control

This private WP-D boundary carries the already accepted M6 run evidence. It does
not schedule business work or write publication state. The Python contract and
client are separate from qualification of the native Windows owner, its journal,
actual process lifetime and storage. A fixture response does not qualify a host.

## Bootstrap and identity

The native bootstrap captures T0 before run-specific input freezing, copying,
startup and warmup. Its immutable `m6.owner-anchor.v1` has the physical boot/QPC
clock, owner incarnation, owner source/device identities, original interval and
finite resource envelope. It deliberately has no corpus/spec hash. The controller
freezes those inputs after the anchor exists, constructs the complete `M6RunSpec`,
compares every shared anchor field and binds the exact spec hash. This resolves
the T0/spec hash cycle without moving input preparation outside measured cost.

The owner must report its stored spec and anchor, never echo caller identity as
an attestation. Before binding it cannot issue a normal status reply; authenticated
non-bind requests close without a success response and retain a bounded refusal
diagnostic. Rebinding different bytes is an error. `run_started` is sequence 1
with stamp T0; all subsequent real observations are received at current QPC.
Same-boot recovery verifies the original journal and deadline, uses an explicit
new incarnation and `owner_resumed`, and keeps admission stopped. No client silently
accepts another owner/anchor, and no cross-host UTC subtraction is used.

The client accepts a changed owner only with an explicit bounded chain of
owner-resumed records obtained from the independently read original journal.
Each record binds the same run/spec/boot/clock/T0/deadline and the preceding
incarnation. This authorizes drain and exact retries of known predecessor stamps;
it permanently disables new admission. A failed status may advance to closed
after cleanup, which confirms closure without making the run eligible for credit.

## Receipt deposit

The runner and the two verifiers may `deposit` one bounded canonical control
receipt (`admission_reconciliation`, `ownership_closure`, `resource_audit`,
`unresolved_claims`; whole request within the 64 KiB wire, receipt payload at
most 49152 bytes). The owner verifies hash, canonical JSON, contract version and
run/spec (and runner epoch where the receipt carries it), then writes it to the
private store under its hash-derived fixed name through the existing immutable
write (pending file, rename, reread). Canonical means the same bytes the Python
owner would produce: a JSON object with code-point-sorted keys, no whitespace,
minimal escapes and integer numbers only; the native owner re-serializes the
parsed receipt and requires byte equality, so a receipt Python would reject is
refused natively as well (`receipt_not_canonical`), and a receipt lacking a
binding member is refused as `receipt_binding_differs`. Identical bytes are idempotent; different
bytes under the same name are a conflict; budget exhaustion and over-bound
payloads are refusals. A lost reply is retried with the same bytes. Deposit
stamps no observation and confers no credit; `admission_closed` and `close`
still read the receipts by hash exactly as before.

## Lifecycle facts and the e2e assembly (runner events)

The staged V4 runtime reports four kinds of durable lifecycle facts through
`StagedLifecycleFactsPort` (`application/ports/staged_lifecycle_facts.py`):
attempt admitted (after the H0 claim or a prepared re-claim is durable),
remote accepted (after the accepted-submission receipt is appended),
publication committed (after the `publish_committed` head is confirmed; the
ledger sequence is read from the durable publish base row) and attempt final
(after the ACK lane or the non-ACK cleanup lane appends the terminal state).
The final fact binds the real provider ACK receipt, consumed or absent; a
terminal receipt is parse status, never closure. Facts are emitted only after
the durable transition exists and the port must not raise for storage or
transport failure; a fact that cannot be built is reported as unavailable. A
port failure right after the H0 claim surfaces through the existing
`AdmissionInterrupted` path, so owned work is never lost to notification.

`adapters/runtime/m6_e2e_assembly.py` turns facts into `e2e_runner` producer
events: `M6LifecycleSpool` appends each canonical event with its producer
sequence to a local fsynced JSONL spool (exact replays are recorded once, a
changed fact for the same attempt is a conflict, a spool that already exists
refuses to start), and `M6E2EAssemblyWorker` drains it on its own thread with
an owner client it constructs itself. Transport faults retry the identical
sequence and bytes a bounded number of times; each retry is a durable
`transport_retry` spool record with the sequence, attempt number and error
text, and a delivery that needed replays says so, so a later success never
erases the fault it recovered from. Every owner conflict or rejection marks
the run failed and stops delivery, because the owner journals the first
variant and never re-accepts.

Formal closure is composed from the same primitives. The controller
(`cli/m6_run_control.py`, or the campaign entry `cli/m6_campaign.py`) freezes
the run spec through the one pure factory
(`application/services/m6_run_spec_factory.build_run_spec`: the owner's anchor
proves T0/clock/interval/resources/owner identity, the declared run intent
supplies membership and phase, the release binding supplies the runtime
identity), binds it **by value** in the `m6.owner-request.v2` bind command and
opens admission. The spec file is written whole or not at all; a repeated bind
with byte-identical inputs replays the owner's idempotent bind (same status,
no new T0 or journal record) and checks the reply names this spec, while a
different spec is refused (`stored_run_spec_differs`), so a lost bind reply is
recovered by readback and never by an alternate spec. The owner-bound campaign
(`staged_campaign --m6-run-dir`) first requires the frozen spec to name its
manifest, scope, campaign and `e2e_publication` mode, then the loaded process
profile, runtime bundle and worker profile, before any lock, database or
admission; it then spools its lifecycle facts, lets a lapsed
owner lease close new admission only, and after the drain deposits its
resource audit, any unresolved claims and the admission reconciliation,
acknowledges `admission_closed` and deposits the ownership closure
(`application/contracts/m6_control_receipts.py`, native shapes and attempt-set
digest). The verifiers then run in dependency order: the public verifier
appends `public_confirmation` per attempt and leaves one public consumer
audit file per attempt; an exact hashed public-inputs manifest built from
those files feeds the quality verifier, which appends `document_qualified`
per attempt (the e2e plan requires `public_units_hash_match`, so quality
cannot run before public). `verifier_drained` is sent once, only by the run's
drain role (`public_verifier` in e2e, `quality_verifier` in service mode, as
the reducer and the native owner both require), after every attempt's
evidence; attempt evidence arriving after it is invalid. Each producer role
uses its own spool; `verifier_drained` names the digest of an
immutable `drain-receipt.json` written before the event, and the run summary
is a separate later file. A verifier whose database setup fails before any
attempt, whose attempt loop does not run to its end, or whose drain receipt
cannot be written whole closes its sender with an explicit abort and no drain
claim; the original failure stays the reported error. The supervisor accepts only this run's campaign receipt (contract,
campaign, manifest, scope, run, spec, anchor and runner incarnation all
bound) and requires a readable, started runner spool once that receipt
exists. Receipts are published atomically: the runner writes the complete
bytes under a hidden sibling name, fsyncs, links them to the final name
(never overwriting) and fsyncs the directory, so a present receipt name
always carries complete bytes and a malformed one is a failure rather than
something to wait out. The
controller then closes the owner with the runner's ownership receipt. A failed
step anywhere leaves that receipt or summary marked failed and the run
evidence-incomplete; nothing is retried blindly or declared closed from a
campaign receipt alone.

## Authentication, transport and retry

One pinned SSH session opens one persistent `direct-tcpip` channel to a configured
Windows loopback port. Use an explicitly qualified native management account;
the existing business forwarding-only account intentionally forbids exec. Preserve
the existing GPU exporter port. No new SSH/firewall permissions are implied.

Each request sends `M6-AUTH/1 <64 lowercase hex characters>` plus LF, then complete
canonical `m6.owner-request.v2` bytes plus LF. A response is one canonical
`m6.owner-reply.v1` line. The native protocol cap is 65,536 bytes per JSON body;
its configured event cap must leave room for the reply envelope. The v2 bind
command carries the exact canonical spec (`spec_utf8`, at most 49,152 UTF-8
bytes, no control characters, hash equal to the envelope `spec_sha256`, run id
equal to the envelope); the escaped whole request is bounded separately by the
65,536-byte wire cap, and an oversized escaped bind is refused by the client
before any exchange. Requests carrying `m6.owner-request.v1` are refused on both
sides; journals, replies, status and receipts stay `v1`, so earlier evidence is
readable offline and no live version fallback exists. The token comes
from an owned private file and never appears in durable models or diagnostic
exceptions. The owner must compare tokens without content-dependent early return,
bind each token to its caller role/incarnation, and close unauthenticated requests
without exposing status. Authentication remains a server boundary even though the
client rejects incompatible roles and admission acknowledgements locally.

The controller may bind/open/close. The selected runner may request a lease,
submit its observations, stop and acknowledge actual admission closure. Independent
verifiers submit only their own observations. All callers may read status.
The server additionally validates event kinds against authenticated roles.

Every control exchange has a fresh request ID; the reply binds its exact hash.
Producer retry identity is separate: identical producer incarnation/sequence/bytes
returns its original durable stamp, including an explicitly recovered predecessor
owner's stamp. Changed bytes under the same producer key must be durably retained
as a conflict. A rejected response cannot conceal an attached accepted record.
EOF, malformed/oversized data, identity drift and cleanup failures propagate;
there are no automatic retries. Lost responses require reconciliation of the exact
pending producer envelope. An exception permanently closes this client's admission
guard while leaving drain/control possible. Failed channel closure retains the
handle for cleanup but prohibits reuse. Closing a local SSH channel proves nothing
about remote owner/Job exit.

## Admission and stop propagation

The local guard requires a sleep-inclusive injected clock. On macOS this is
`mach_continuous_time` converted with `mach_timebase_info`; uptime-only clocks are
not a fallback. Apple documents that this clock continues while the system sleeps.
See [Apple's clock contract](https://developer.apple.com/documentation/kernel/1646199-mach_continuous_time).

A lease is anchored at request **send**, not response receipt. The client subtracts
its uncertainty/drift allowance and caps the grant at the original Windows
deadline. Successful non-lease exchanges may preserve the original unexpired
grant; they cannot extend it. The guard is supplemental, not a hard realtime or
physical-clock qualification claim.

The caller must explicitly supply `stop_propagation_reserve_ns`, derived from the
selected runner's bounded in-flight claim completion, observation append, control
poll and admission-closed ACK. The accepted grant must fit both the configured
lease maximum and `stop_admission_budget - propagation_reserve`. If that leaves
no usable lease, construction fails. A nominal one-second grant with a one-second
total stop budget is insufficient. Real runner integration must establish those
bounds; a synthetic test reserve is not operational qualification.

Windows reaching its deadline closes the owner grant gate and records
`stop_admission_requested`. The Mac runner closes new admission, waits for any
in-flight claim to return, retains every durable claim and stamps its observation,
then sends `M6AdmissionClosedAck` for its exact incarnation. Its immutable
reconciliation receipt identifies all claims and unresolved H0 responsibility.
Only after validating this acknowledgement may the owner record
`stop_admission_effective`. A nonzero unresolved count is a permanent measurement
incident. The acknowledgement and closure sidecars must be archived with the
journal; their hashes alone do not establish that reconciliation occurred.

The propagation reserve includes its clock-drift allowance. A bound owner that
has not opened admission answers lease requests with `ok` and a null lease. A
conflict reply contains the stamp of the submitted conflicting bytes, not the
predecessor observation. A lost SSH session requires a new explicitly composed
transport after closing the old one; there is no silent reconnect.

Late admissions still obey WP-B: responsibility/cost remains, eligible pages are
zero, and exceeding the stop budget makes the entire measurement incomplete.
No adapter drops late evidence to manufacture a complete receipt.

A newly refused, authenticated business observation with the correct role binds
one permanent observation_refused measurement incident to its producer-byte hash.
The owner stores at most one such incident per run, stops admission, and reports
failed; the host retains the refused raw requests in its bounded private
diagnostics. A typo or unexpected late observation cannot be silently discarded to
produce a complete run. Ordinary verifier_drain_pending is flow control and does
not create an incident. Before open and after actual effective stop, new admission
is rejected; a distinct sequence cannot admit the same attempt twice.

The journal reserves eight owner-event slots and their worst-case record bytes
before accepting another producer record. Bootstrap must make its journal limits
match the anchor, account for indexes within the actual process memory limit, and
bound recovery attempts explicitly. This reserve does not promise unlimited crash
recovery. Exhaustion, orphaned/torn sidecars and uncertain IO remain visible and
may require offline reconciliation. Native resource closure precedes immutable
closure-receipt publication; changed orphaned ACK/closure sidecars raise an IO
failure and close the connection. They are never rewritten into clean evidence.

## Native implementation qualification

The Windows implementation must independently prove strict wire parity, durable
append before ACK, conflict retention, damaged-tail preservation, original-deadline
recovery, finite storage/process bounds and actual child/resource closure. An
accepted client package does not establish these gates or automatic PDF/E2E/hour
acceptance. Public contracts and legacy telemetry clocks remain unchanged.

## Native startup prerequisites

The physical identity probe uses a typed Registry64 `BootId` DWORD, the actual
MachineGuid-derived node hash, and the pinned System32 NVML device UUID. Boot
identity v1 hashes the contract version, node hash and unsigned BootId; the outer
physical identity evidence is v2. Missing or mistyped values fail without a UTC
fallback. Microsoft documents this counter as incrementing on a successful boot
in [its Windows boot-counter documentation](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/oem-hvci-enablement).
This is a local identity label, not a tamper-proof attestation. WMI LastBootUpTime
remains a diagnostic field and its availability is still a startup prerequisite;
it does not enter the QPC clock-domain hash. Exactly one capture attempt is allowed
per process, including a failed attempt. Resumption requires a new process.

Native bootstrap validates the complete closed run-spec projection against the
Python contract and compares every shared anchor field before constructing the
already-bound control. Its bounded JSON reader accepts at most 65,536 bytes and
8,192 parsed values; a Python-valid larger spec is not qualified for this native
owner. Unicode carry-in ordering follows scalar order, not UTF-16 ordinal order.
The controller must reject unsupported native bounds before sending a bind.

The endpoint diagnostic sink aggregates no-body transport noise into a fixed set
of saturating counters. Sealing stores one bounded summary; post-seal no-body
noise is a bounded count for the exit-intent metadata. Authenticated raw bytes and
non-success control replies are retained in the private store. A storage budget or
IO failure propagates; it cannot become a success reply or a clean measurement.
These records exclude authentication headers. The fixed host wires this sink
into both endpoint refusals and non-success control replies. A tested standalone
sink does not prove that the host composition is installed.

Repeated `verifier_drain_pending` replies are a narrowly defined no-effect flow
control case: the same canonical command/producer bytes, run and spec retain the
first complete raw request, aggregate a saturating count, and bind all request
hashes in arrival order. Changing only the request nonce does not allocate a new
artifact pair. Changed business bytes get a separate original record; conflicts
and all other rejections remain fully retained. At most 32 distinct pending-drain
commands can be tracked, with no eviction. Summary v2 contains these groups.
A diagnostic storage failure poisons the sink; even summary sealing cannot be
retried after uncertain IO. The host must reserve fixed sidecar/summary capacity
before assigning the remaining diagnostic budget.

The fixed `mineru_m6_owner_host.cs` executable captures T0 at its first controlled
entry and enters its finite self-Job before reading the private deployment. Its
eight arguments bind configuration path/hash, executable hash, planned seconds,
close grace, memory bound, and the explicit original-deadline/anchor resume pair.
The private deployment is `m6.owner-deployment.v2`; its one closed addition,
`bootstrap_bind_seconds` (1..planned+grace, default 120 from the campaign
intent), is the bound within which an unbound owner must receive its bind. At
`T0 + bootstrap_bind_seconds` an unbound host writes the `bootstrap_bind_timeout`
diagnostic and exits 126 (distinct from the watchdog's 124/125); the bound is
fixed from the original T0 and never renewed.
Build all production dependencies into that executable before a measured run;
test libraries are not production dependencies. The immutable deployment binding
contains only configuration and executable hashes. It does not contain tokens.

The private deployment file must explicitly belong to the current Windows user,
with access limited to that user and SYSTEM. A protected directory DACL alone
does not guarantee the file owner: an elevated SSH token can create a file owned
by Administrators. Provision the file owner and DACL together, then independently
verify them and the content hash. Use the system machine identity, not optional
environment variables, for the deployment host check. These requirements do not
change SSH account policy or machine-wide PowerShell execution policy.

For a fresh run, the controller freezes the canonical spec only after observing
the original anchor and sends it inside the bind request; nothing is uploaded
ahead of the bind. The unbound host settles, in order and before any private
write: the authenticated principal is the controller (else `unauthorized`),
the command is `bind` (else `owner_not_bound`), run id, anchor hash, principal
epoch and payload hash agree (`bootstrap_binding_mismatch`), the spec passes the
closed canonical/anchor binding validation (`bootstrap_spec_invalid`), and any
already stored `spec.json` is byte-identical (`stored_run_spec_differs`). Only
then is `spec.json` written immutably, read back and control constructed. A
resumed process reads the existing spec and constructs recovered control before
READY. READY advertises a flushed journal prefix length
and hash. The controller independently reads exactly that prefix, verifies its
records and original owner chain, and only then constructs a recovered client.
The journal writer permits read sharing; a concurrent read-only handle must allow
ReadWrite sharing to coexist with it. Other writers and deletion remain denied.

The host reserves 64 artifact entries and 4 MiB for fixed closure/recovery records
when persisting ordinary diagnostics. Exhaustion propagates, including on resume.
The final closure callback seals diagnostics, releases receipt/binary read pins
and checks the native no-child invariant before the finite metadata tail. A
successful run then closes transport, journal, guard and credentials and writes an
exit-intent explicitly marked as not externally verified. The controller must
still verify the exact process handle and PID/birth. A watchdog exit, failed
startup, socket EOF or missing external proof cannot establish successful closure.

## Launcher parameter sets: Prepare, Run, Cancel

`scripts/windows/run_mineru_m6_owner_host.ps1` is the only production parent of
the owner host and has three parameter sets on one fresh campaign workspace
(`<workspace_root>\m6-<run_id>`):

- `-Prepare` creates `private`, `private\runs`, `private\staging` and
  `private\attempts` through the qualified binary's own `CreatePrivateDirectory`
  (owner SID + SYSTEM, protected DACL, must be new) and prints/writes
  `m6.owner-workspace-prepare.v1` with each directory's SDDL. No secret exists at
  this point; the controller validates the receipt (owner SID, `D:P`, only the
  owner and SYSTEM allowed) and only then uploads the private deployment into
  `private\staging` over the pinned sftp.
- `-Run` re-validates every private ancestor, commits the staged deployment
  (hash, closed `m6.owner-deployment.v2` structure, `run_id`, `run_root` equal
  to `private\runs`, loopback port, bootstrap bound; owner set to this account;
  atomic move; read-back of hash and ACL), creates the new attempt directory,
  writes `process-start.json` (`m6.owner-external-start.v2`) and spawns the host
  while holding its handle. The READY line must be exactly the seven fields with
  the expected status/run/interval within `ReadyWaitSeconds` (default 30) or the
  child is terminated through the held handle, reaped and the launcher exits 3.
  Both pipes are drained continuously into bounded head/tail retention; the
  attempt's `cancel.json` is polled every 250 ms; one deadline from spawn is
  never renewed. `process-exit.json` (`m6.owner-external-exit.v2`) records the
  actual exit code, forced termination and any accepted cancel.
- `-Cancel` derives `m6.owner-cancel.v1` from the attempt's own
  `process-start.json` (record hash, run, attempt, pid, creation time, binary
  and configuration hashes, reason) and writes `cancel.json` create-new; an
  identical repeat is idempotent, different content is refused (65). The running
  launcher honours a cancel only when every field names its exact live instance
  (pid and creation time of the handle it holds); a mismatching command is
  recorded as rejected and ignored. There is no PID-only kill.

Launcher exit codes: 0, 2 parameter, 3 READY deadline, 4 host non-zero, 65
identity/ACL, 70 launcher failure; the last stdout line is one
`M6-RESULT m6.owner-launcher-result.v1` JSON. The Mac composition root treats the
local transport exit as no proof: it fetches `process-exit.json` and compares.

## Independent qualification entrypoints

The independent tests replace the former individual owner/component/parent
scripts. Generate or verify `tests/fixtures/m6_owner/wire-vectors.v2.json` using
its documented generator. Those vectors establish Python/native wire parity;
the native suites separately assert semantic outcomes and observe real files,
process handles and Job accounting.

Run `scripts/windows/test_mineru_m6_native_suite.ps1` in a fresh 64-bit Windows
PowerShell 5.1 process with explicit source, fixture and new output roots. It
builds the twelve production sources into a standalone executable, then builds
the separate component tests and exercises the finite self-Job cases. The
resulting `runner-evidence.json` binds source, compiler, fixture and executable
bytes. An explicit self-Job skip does not qualify that boundary.

Run `scripts/windows/test_mineru_m6_owner_host_live.ps1` with that qualified
build root, a new output root, the actual source commit, GPU UUID and pinned
System32 NVML hash. Its independently implemented parent checks normal closure,
same-boot crash/resume and exact predecessor retry, damaged-tail refusal,
pre-bind refusal/pending drain, and unbound watchdog expiry. Authenticated
request evidence excludes credentials. Every launched child is bounded by an
owned unnamed Job and an original process handle; a deliberate injected crash
is distinguished from normal exit and unexpected forced cleanup. These are
zero-PDF control fixtures, with synthetic business receipts; they do not qualify
G2, document quality, public publication or a measured hour.

`scripts/windows/test_mineru_api_only_installer.ps1` separately exercises an
allowlisted set of installer functions with stateful Docker mocks. It does not
deploy or prove an actual service image rollback. All entrypoints retain failed
artifacts and require new disposable output roots. Use process-local
`RemoteSigned`; no machine policy change is part of qualification.
